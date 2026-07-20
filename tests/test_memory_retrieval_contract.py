from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem, load_layer_specs
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery


def test_default_layer_contract_has_one_mv2_file_per_permanent_layer(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend)

    assert set(memory.backends) == set(MemoryLayer)
    assert {layer: spec.mv2_file for layer, spec in memory.specs.items()} == {
        MemoryLayer.WORKING: "working.mv2",
        MemoryLayer.EPISODIC: "episodic.mv2",
        MemoryLayer.SEMANTIC: "semantic.mv2",
        MemoryLayer.PROCEDURAL: "procedural.mv2",
        MemoryLayer.SELF: "self.mv2",
        MemoryLayer.POLICY: "policy.mv2",
    }
    assert (
        memory.specs[MemoryLayer.WORKING].min_write_confidence
        < memory.specs[MemoryLayer.SEMANTIC].min_write_confidence
    )
    assert (
        memory.specs[MemoryLayer.WORKING].retention_days
        < memory.specs[MemoryLayer.EPISODIC].retention_days
    )
    assert memory.specs[MemoryLayer.POLICY].search_mode == "lex"


def test_load_layer_specs_falls_back_to_lexical_without_complete_local_vector_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "layers.json"
    config_path.write_text(
        json.dumps(
            {
                "semantic": {
                    "search_mode": "hybrid",
                    "vector": {"enabled": True, "embedding_provider": "local"},
                },
                "procedural": {
                    "vector": {
                        "enabled": True,
                        "embedding_provider": "local",
                        "index_path": "procedural.vec",
                    }
                },
                "policy": {
                    "search_mode": "hybrid",
                    "vector": {
                        "enabled": True,
                        "embedding_provider": "local",
                        "index_path": "policy.vec",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    specs = load_layer_specs(config_path)

    assert specs[MemoryLayer.SEMANTIC].search_mode == "lex"
    assert specs[MemoryLayer.SEMANTIC].vector_search_enabled is False
    assert specs[MemoryLayer.PROCEDURAL].search_mode == "hybrid"
    assert specs[MemoryLayer.PROCEDURAL].hybrid_search_enabled is True
    assert specs[MemoryLayer.POLICY].search_mode == "lex"
    assert specs[MemoryLayer.POLICY].vector_search_enabled is False


def test_hybrid_retrieval_uses_rebuildable_sidecar_without_replacing_mv2(tmp_path: Path) -> None:
    config_path = tmp_path / "layers.json"
    config_path.write_text(
        json.dumps(
            {
                "semantic": {
                    "search_mode": "hybrid",
                    "vector": {
                        "enabled": True,
                        "embedding_provider": "local",
                        "embedding_model": "concept-test",
                        "index_path": "semantic.mv2.vector.sqlite",
                    },
                },
                "policy": {
                    "search_mode": "hybrid",
                    "vector": {
                        "enabled": True,
                        "embedding_provider": "local",
                        "embedding_model": "concept-test",
                        "index_path": "policy.mv2.vector.sqlite",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    specs = load_layer_specs(config_path)
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        InMemoryBackend,
        specs=specs,
        vector_embedder=ConceptEmbedder(),
        enforce_stable_write_integrity=False,
    )
    semantic = MemoryRecord(
        id="pythonpath-fix",
        title="Python path import fix",
        content="Set PYTHONPATH before pytest invocations.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.9,
    )
    policy = MemoryRecord(
        id="policy-exact",
        title="Policy exactness",
        content="Policy memory must stay lexical even when vector settings are supplied.",
        layer=MemoryLayer.POLICY,
        kind=MemoryKind.POLICY,
        confidence=0.99,
    )
    memory.put(semantic)
    memory.put(policy)

    lexical_hits = memory.retrieve(
        RetrievalQuery(
            query="module discovery needs sys route",
            layers=(MemoryLayer.SEMANTIC,),
            mode="lex",
        )
    )
    hybrid_hits = memory.retrieve(
        RetrievalQuery(
            query="module discovery needs sys route",
            layers=(MemoryLayer.SEMANTIC,),
            mode="hybrid",
        )
    )
    policy_hits = memory.retrieve(
        RetrievalQuery(
            query="rule guard fuzzy semantic route",
            layers=(MemoryLayer.POLICY,),
            mode="hybrid",
        )
    )

    assert lexical_hits == []
    assert hybrid_hits[0].record.id == "pythonpath-fix"
    assert hybrid_hits[0].source_backend == "vector_sidecar"
    assert specs[MemoryLayer.POLICY].search_mode == "lex"
    assert policy_hits == []
    status = memory.vector_index_status()[MemoryLayer.SEMANTIC]
    assert status.enabled is True
    assert status.indexed_count == 1
    assert status.path is not None
    assert status.path.endswith("semantic.mv2.vector.sqlite")
    assert (tmp_path / "memory" / "semantic.mv2").exists() or memory.backends[
        MemoryLayer.SEMANTIC
    ].path.name == "semantic.mv2"


def test_retrieve_searches_requested_layers_and_respects_per_layer_k(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
    sentinel = "sentinel_retrieval_contract_4c9d"
    for layer in MemoryLayer:
        spec = DEFAULT_LAYER_SPECS[layer]
        for index in range(2):
            memory.put(
                MemoryRecord(
                    id=f"{layer.value}-{index}",
                    title=f"{layer.value} sentinel {index}",
                    content=f"{sentinel} lives in {layer.value} memory record {index}.",
                    layer=layer,
                    kind=_kind_for_layer(layer),
                    confidence=max(
                        spec.min_write_confidence, 0.98 if layer == MemoryLayer.POLICY else 0.85
                    ),
                    importance=0.6 + (0.1 * index),
                    metadata={"frame_type": _frame_type_for_layer(layer)},
                )
            )

    hits = memory.retrieve(RetrievalQuery(query=sentinel, k_per_layer=1))

    hit_layers = [hit.record.layer for hit in hits]
    assert set(hit_layers) == set(MemoryLayer)
    assert all(hit_layers.count(layer) == 1 for layer in MemoryLayer)
    assert all(hit.record.metadata["last_retrieved_at"] for hit in hits)


def test_inactive_records_are_hidden_by_default_but_available_for_audit(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
    record_id = memory.put(
        MemoryRecord(
            id="sentinel-tombstone",
            title="Tombstone audit",
            content="sentinel_tombstone_audit_1f6b should disappear from normal retrieval.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )

    assert memory.tombstone(
        MemoryLayer.SEMANTIC, record_id, reason="superseded", superseded_by="replacement"
    )

    assert (
        memory.retrieve(
            RetrievalQuery(query="sentinel_tombstone_audit_1f6b", layers=(MemoryLayer.SEMANTIC,))
        )
        == []
    )
    inactive_hits = memory.retrieve(
        RetrievalQuery(
            query="sentinel_tombstone_audit_1f6b",
            layers=(MemoryLayer.SEMANTIC,),
            include_inactive=True,
        )
    )
    assert len(inactive_hits) == 1
    assert inactive_hits[0].record.metadata["active"] is False
    assert inactive_hits[0].record.metadata["superseded_by"] == "replacement"


def test_retrieval_writeback_throttles_last_retrieved_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            id="retrieval-clock",
            title="Retrieval clock",
            content="sentinel_retrieval_clock_8d2a updates at most hourly.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )
    base_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        current = base_time

        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return cls.current if tz is not None else cls.current.replace(tzinfo=None)

    monkeypatch.setattr("nested_memvid_agent.layers.datetime", FrozenDateTime)

    memory.retrieve(
        RetrievalQuery(query="sentinel_retrieval_clock_8d2a", layers=(MemoryLayer.SEMANTIC,))
    )
    first = memory.get_record(MemoryLayer.SEMANTIC, "retrieval-clock").metadata["last_retrieved_at"]  # type: ignore[union-attr]

    FrozenDateTime.current = base_time + timedelta(minutes=10)
    memory.retrieve(
        RetrievalQuery(query="sentinel_retrieval_clock_8d2a", layers=(MemoryLayer.SEMANTIC,))
    )
    second = memory.get_record(MemoryLayer.SEMANTIC, "retrieval-clock").metadata[
        "last_retrieved_at"
    ]  # type: ignore[union-attr]

    FrozenDateTime.current = base_time + timedelta(hours=1, minutes=1)
    memory.retrieve(
        RetrievalQuery(query="sentinel_retrieval_clock_8d2a", layers=(MemoryLayer.SEMANTIC,))
    )
    third = memory.get_record(MemoryLayer.SEMANTIC, "retrieval-clock").metadata["last_retrieved_at"]  # type: ignore[union-attr]

    assert first == base_time.isoformat()
    assert second == first
    assert third == FrozenDateTime.current.isoformat()


def _kind_for_layer(layer: MemoryLayer) -> MemoryKind:
    return {
        MemoryLayer.WORKING: MemoryKind.OBSERVATION,
        MemoryLayer.EPISODIC: MemoryKind.EVENT,
        MemoryLayer.SEMANTIC: MemoryKind.FACT,
        MemoryLayer.PROCEDURAL: MemoryKind.PROCEDURE,
        MemoryLayer.SELF: MemoryKind.FACT,
        MemoryLayer.POLICY: MemoryKind.POLICY,
    }[layer]


def _frame_type_for_layer(layer: MemoryLayer) -> str:
    return {
        MemoryLayer.WORKING: "raw_chunk",
        MemoryLayer.EPISODIC: "session_summary",
        MemoryLayer.SEMANTIC: "section_summary",
        MemoryLayer.PROCEDURAL: "skill_card",
        MemoryLayer.SELF: "self_model",
        MemoryLayer.POLICY: "trace_stub",
    }[layer]


class ConceptEmbedder:
    model_name = "concept-test"

    def embed(self, text: str) -> np.ndarray:
        concepts = {"import_path": 0, "policy_exact": 1}
        synonyms = {
            "pythonpath": "import_path",
            "module": "import_path",
            "sys": "import_path",
            "policy": "policy_exact",
            "rule": "policy_exact",
            "guard": "policy_exact",
        }
        vector = np.zeros(len(concepts), dtype=np.float32)
        for raw in text.lower().replace(".", " ").replace(":", " ").split():
            concept = synonyms.get(raw.strip())
            if concept in concepts:
                vector[concepts[concept]] = 1.0
        return vector
