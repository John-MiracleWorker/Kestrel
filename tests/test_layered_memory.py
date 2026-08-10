from __future__ import annotations

import json
from collections.abc import Collection, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from nested_memvid_agent.backends.base import MemoryBackend
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


def test_initial_write_reservation_uses_one_bulk_identity_check_per_layer(
    tmp_path: Path,
) -> None:
    RecordingIdentityBackend.identity_checks = []
    RecordingIdentityBackend.get_record_calls = 0
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, RecordingIdentityBackend)
    candidate_ids = frozenset({"turn-1-user", "turn-1-assistant", "turn-1-error"})

    with memory.reserve_record_ids_for_initial_write(candidate_ids) as available:
        assert available is True

    assert RecordingIdentityBackend.identity_checks == [
        (layer, candidate_ids) for layer in sorted(MemoryLayer, key=lambda item: item.value)
    ]
    assert RecordingIdentityBackend.get_record_calls == 0


def test_initial_write_reservation_supports_legacy_custom_backend_record_lookup(
    tmp_path: Path,
) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, LegacyLookupBackend)
    memory.put(
        MemoryRecord(
            id="legacy-occupied-id",
            title="Existing record",
            content="The legacy backend only exposes record lookup for identity checks.",
            layer=MemoryLayer.WORKING,
            confidence=0.5,
        )
    )

    with memory.reserve_record_ids_for_initial_write({"legacy-occupied-id"}) as available:
        assert available is False


def test_initial_write_reservation_holds_every_layer_lock_through_first_write(
    tmp_path: Path,
) -> None:
    ReservationProbeBackend.active_layers = set()
    ReservationProbeBackend.first_write_layers = frozenset()
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, ReservationProbeBackend)
    first_layer = min(MemoryLayer, key=lambda item: item.value)
    first_backend = memory.backends[first_layer]
    assert isinstance(first_backend, ReservationProbeBackend)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with memory.reserve_record_ids_for_initial_write({"turn-probe-user"}) as available:
            assert available is True
            assert executor.submit(first_backend.competing_lock_available).result(timeout=5) is False
            memory.put(
                MemoryRecord(
                    id="turn-probe-user",
                    title="Reservation probe",
                    content="The first write remains inside every layer reservation.",
                    layer=MemoryLayer.WORKING,
                    confidence=0.5,
                )
            )
            assert ReservationProbeBackend.first_write_layers == frozenset(MemoryLayer)
            assert executor.submit(first_backend.competing_lock_available).result(timeout=5) is False

        assert executor.submit(first_backend.competing_lock_available).result(timeout=5) is True


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


def test_memory_system_retains_backends_until_failed_seal_can_be_retried(
    tmp_path: Path,
) -> None:
    FailingSealBackend.closed_layers = []
    FailingSealBackend.fail_seal = True
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

    assert FailingSealBackend.closed_layers == []

    FailingSealBackend.fail_seal = False
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


class RecordingIdentityBackend(InMemoryBackend):
    identity_checks: list[tuple[MemoryLayer, frozenset[str]]] = []
    get_record_calls = 0

    def has_any_record_identity(self, record_ids: Collection[str]) -> bool:
        type(self).identity_checks.append((self.layer, frozenset(record_ids)))
        return super().has_any_record_identity(record_ids)

    def get_record(
        self,
        record_id: str,
        *,
        include_inactive: bool = True,
    ) -> MemoryRecord | None:
        type(self).get_record_calls += 1
        return super().get_record(record_id, include_inactive=include_inactive)


class LegacyLookupBackend(InMemoryBackend):
    """A pre-bulk-identity custom backend that retains the legacy lookup API."""

    has_any_record_identity = MemoryBackend.has_any_record_identity


class ReservationProbeBackend(InMemoryBackend):
    active_layers: set[MemoryLayer] = set()
    first_write_layers: frozenset[MemoryLayer] = frozenset()

    @contextmanager
    def identity_reservation(self) -> Iterator[None]:
        with super().identity_reservation():
            type(self).active_layers.add(self.layer)
            try:
                yield
            finally:
                type(self).active_layers.remove(self.layer)

    def has_any_record_identity(self, record_ids: Collection[str]) -> bool:
        assert type(self).active_layers == set(MemoryLayer)
        return super().has_any_record_identity(record_ids)

    def put(self, record: MemoryRecord) -> str:
        if record.id == "turn-probe-user":
            type(self).first_write_layers = frozenset(type(self).active_layers)
        return super().put(record)

    def competing_lock_available(self) -> bool:
        acquired = self._state_lock.acquire(blocking=False)
        if acquired:
            self._state_lock.release()
        return acquired


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
    fail_seal = True

    def seal(self) -> None:
        if self.layer == MemoryLayer.WORKING and type(self).fail_seal:
            raise RuntimeError("working seal failed")
        super().seal()

    def close(self) -> None:
        type(self).closed_layers.append(self.layer)
        super().close()
