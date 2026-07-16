from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import LayeredMemorySystem, load_layer_specs
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery


def test_layer_write_threshold_blocks_low_confidence_semantic(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    with pytest.raises(ValueError):
        memory.put(
            MemoryRecord(
                title="Weak fact",
                content="Maybe the repo uses Kimi.",
                layer=MemoryLayer.SEMANTIC,
                confidence=0.2,
            )
        )


def test_retrieve_across_layers(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    memory.put(
        MemoryRecord(
            title="Working auth note",
            content="Kimi auth failed during startup.",
            layer=MemoryLayer.WORKING,
            confidence=0.3,
        )
    )
    memory.put(
        MemoryRecord(
            title="Semantic auth note",
            content="Provider-specific auth profiles should be checked before global variables.",
            layer=MemoryLayer.SEMANTIC,
            confidence=0.8,
        )
    )
    hits = memory.retrieve(RetrievalQuery(query="auth profiles"))
    assert {hit.record.layer for hit in hits} == {MemoryLayer.WORKING, MemoryLayer.SEMANTIC}


def test_default_memory_system_includes_self_layer(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)

    assert MemoryLayer.SELF in memory.backends
    assert memory.specs[MemoryLayer.SELF].mv2_file == "self.mv2"


def test_memory_system_closes_partially_opened_backends_when_startup_fails(
    tmp_path: Path,
) -> None:
    FailingOpenBackend.closed_layers = []

    with pytest.raises(RuntimeError, match="semantic open failed"):
        LayeredMemorySystem.from_backend_factory(tmp_path, FailingOpenBackend)

    assert FailingOpenBackend.closed_layers == [
        MemoryLayer.SEMANTIC,
        MemoryLayer.EPISODIC,
        MemoryLayer.WORKING,
    ]


def test_memory_system_closes_every_backend_when_sealing_fails(tmp_path: Path) -> None:
    FailingSealBackend.closed_layers = []
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, FailingSealBackend)

    with pytest.raises(RuntimeError, match="working seal failed"):
        memory.close_all()

    assert set(FailingSealBackend.closed_layers) == set(MemoryLayer)


def test_maybe_seal_all_defers_working_memory_until_threshold(tmp_path: Path) -> None:
    CountingBackend.seal_calls = 0
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, CountingBackend)
    memory.put(
        MemoryRecord(
            title="Working note",
            content="Working note can wait for threshold flush.",
            layer=MemoryLayer.WORKING,
            confidence=0.4,
        )
    )

    assert memory.maybe_seal_all(write_threshold=50, interval_seconds=10) is False
    assert CountingBackend.seal_calls == 0

    assert memory.maybe_seal_all(write_threshold=1, interval_seconds=10) is True
    assert CountingBackend.seal_calls == len(memory.backends)


def test_maybe_seal_all_flushes_durable_layers_immediately(tmp_path: Path) -> None:
    CountingBackend.seal_calls = 0
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, CountingBackend)
    memory.put(
        MemoryRecord(
            title="Stable fact",
            content="Stable semantic fact should flush immediately.",
            layer=MemoryLayer.SEMANTIC,
            confidence=0.8,
        )
    )

    assert memory.maybe_seal_all(write_threshold=50, interval_seconds=10) is True
    assert CountingBackend.seal_calls == len(memory.backends)


def test_backend_mutation_contract_hides_inactive_records_by_default(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    record = MemoryRecord(
        id="fact-1",
        title="Mutable fact",
        content="Mutable fact says alpha is enabled.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.86,
    )

    memory.upsert(record)
    replacement = MemoryRecord(
        id="fact-1",
        title="Mutable fact",
        content="Mutable fact says alpha is disabled.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.88,
    )
    memory.upsert(replacement)
    assert memory.get_record(MemoryLayer.SEMANTIC, "fact-1").content.endswith("disabled.")

    memory.tombstone(MemoryLayer.SEMANTIC, "fact-1", reason="superseded", superseded_by="fact-2")

    assert not memory.retrieve(RetrievalQuery(query="Mutable fact alpha", layers=(MemoryLayer.SEMANTIC,)))
    inactive_hits = memory.retrieve(
        RetrievalQuery(query="Mutable fact alpha", layers=(MemoryLayer.SEMANTIC,), include_inactive=True)
    )
    assert inactive_hits
    assert inactive_hits[0].record.metadata["active"] is False
    assert list(memory.iter_records(MemoryLayer.SEMANTIC)) == []
    assert memory.get_record(MemoryLayer.SEMANTIC, "fact-1").metadata["superseded_by"] == "fact-2"


def test_load_layer_specs_requires_explicit_local_vector_config_for_hybrid(tmp_path: Path) -> None:
    config_path = tmp_path / "layers.json"
    config_path.write_text(
        """
        {
          "semantic": {
            "description": "Facts",
            "mv2_file": "semantic.mv2",
            "update_cadence": "validated_fact",
            "retrieval_k": 4,
            "context_budget_chars": 1200,
            "min_write_confidence": 0.7,
            "promotion_threshold": 0.82,
            "min_repeat_count_for_promotion": 2,
            "retention_days": 365,
            "search_mode": "hybrid",
            "vector": {
              "enabled": true,
              "embedding_provider": "local",
              "index_path": "semantic.vec"
            }
          },
          "policy": {
            "description": "Policy",
            "mv2_file": "policy.mv2",
            "update_cadence": "rare",
            "retrieval_k": 2,
            "context_budget_chars": 1000,
            "min_write_confidence": 0.95,
            "promotion_threshold": 0.97,
            "min_repeat_count_for_promotion": 5,
            "retention_days": 730,
            "search_mode": "hybrid",
            "vector": {"enabled": true, "embedding_provider": "local", "index_path": "policy.vec"}
          }
        }
        """,
        encoding="utf-8",
    )

    specs = load_layer_specs(config_path)

    assert specs[MemoryLayer.SEMANTIC].search_mode == "hybrid"
    assert specs[MemoryLayer.SEMANTIC].vector_search_enabled is True
    assert specs[MemoryLayer.SEMANTIC].vector_embedding_provider == "local"
    assert specs[MemoryLayer.POLICY].search_mode == "lex"
    assert specs[MemoryLayer.POLICY].vector_search_enabled is False


class CountingBackend(InMemoryBackend):
    seal_calls = 0

    def seal(self) -> None:
        type(self).seal_calls += 1


class FailingOpenBackend(InMemoryBackend):
    closed_layers: list[MemoryLayer] = []

    def open(self) -> None:
        if self.layer == MemoryLayer.SEMANTIC:
            raise RuntimeError("semantic open failed")
        super().open()

    def close(self) -> None:
        type(self).closed_layers.append(self.layer)
        super().close()


class FailingSealBackend(InMemoryBackend):
    closed_layers: list[MemoryLayer] = []

    def seal(self) -> None:
        if self.layer == MemoryLayer.WORKING:
            raise RuntimeError("working seal failed")
        super().seal()

    def close(self) -> None:
        type(self).closed_layers.append(self.layer)
        super().close()
