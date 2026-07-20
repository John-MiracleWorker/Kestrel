from __future__ import annotations

import json
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
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path,
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
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
    memory.put(
        MemoryRecord(
            title="Dirty working record",
            content="Closing must still close every backend when the dirty layer fails to seal.",
            layer=MemoryLayer.WORKING,
            confidence=0.4,
        )
    )

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
    assert CountingBackend.seal_calls == 1


def test_maybe_seal_all_flushes_durable_layers_immediately(tmp_path: Path) -> None:
    CountingBackend.seal_calls = 0
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path,
        CountingBackend,
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            title="Stable fact",
            content="Stable semantic fact should flush immediately.",
            layer=MemoryLayer.SEMANTIC,
            confidence=0.8,
        )
    )

    assert memory.maybe_seal_all(write_threshold=50, interval_seconds=10) is True
    assert CountingBackend.seal_calls == 1


def test_backend_mutation_contract_hides_inactive_records_by_default(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path,
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
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

    assert not memory.retrieve(
        RetrievalQuery(query="Mutable fact alpha", layers=(MemoryLayer.SEMANTIC,))
    )
    inactive_hits = memory.retrieve(
        RetrievalQuery(
            query="Mutable fact alpha", layers=(MemoryLayer.SEMANTIC,), include_inactive=True
        )
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


@pytest.mark.parametrize(
    "overrides",
    [
        {"semantic": {"mv2_file": "working.mv2"}},
        {"semantic": {"mv2_file": "../outside.mv2"}},
        {"semantic": {"mv2_file": r"nested\outside.mv2"}},
        {
            "working": {"mv2_file": "Shared.mv2"},
            "episodic": {"mv2_file": "shared.mv2"},
        },
        {
            "semantic": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": "../outside.sqlite",
                }
            }
        },
        {
            "semantic": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": "shared.sqlite",
                }
            },
            "procedural": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": "shared.sqlite",
                }
            },
        },
        {
            "semantic": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": "shared.sqlite",
                }
            },
            "procedural": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": "shared.sqlite-wal",
                }
            },
        },
        {
            "semantic": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": ".working.mv2.kestrel.lock",
                }
            }
        },
        {
            "semantic": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": "semantic.mv2.records.json",
                }
            }
        },
        {
            "semantic": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": "Shared.sqlite",
                }
            },
            "procedural": {
                "vector": {
                    "enabled": True,
                    "embedding_provider": "local",
                    "index_path": "shared.sqlite-wal",
                }
            },
        },
    ],
)
def test_load_layer_specs_rejects_escaping_or_duplicate_artifact_paths(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    config_path = tmp_path / "layers.json"
    config_path.write_text(json.dumps(overrides), encoding="utf-8")

    with pytest.raises(ValueError, match="single filename|Duplicate"):
        load_layer_specs(config_path)


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
