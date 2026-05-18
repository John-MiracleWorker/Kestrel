from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def test_in_memory_backend_contract_round_trips_mutation_and_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "semantic.mv2"
    backend = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    first_id = backend.put(
        MemoryRecord(
            id="backend-record",
            title="Backend record",
            content="sentinel_backend_consistency_40de original content.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
            metadata={"frame_id": "backend-frame"},
        )
    )
    upsert_id = backend.upsert(
        MemoryRecord(
            id="backend-record",
            title="Backend record",
            content="sentinel_backend_consistency_40de updated content.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.91,
            metadata={"frame_id": "backend-frame"},
        )
    )

    assert first_id == upsert_id == "backend-record"
    assert backend.get_record("backend-record").content.endswith("updated content.")
    assert backend.get_record("backend-frame").id == "backend-record"  # type: ignore[union-attr]
    assert [record.id for record in backend.iter_records()] == ["backend-record"]
    assert backend.find("sentinel_backend_consistency_40de", k=3)
    assert backend.verify() is True

    backend.tombstone("backend-record", reason="superseded", superseded_by="backend-record-2")
    assert backend.find("sentinel_backend_consistency_40de", include_inactive=False) == []
    assert backend.find("sentinel_backend_consistency_40de", include_inactive=True)
    assert list(backend.iter_records()) == []
    assert [record.id for record in backend.iter_records(include_inactive=True)] == ["backend-record"]
    backend.seal()
    assert path.with_suffix(".memory.json").exists()
    backend.close()

    InMemoryBackend._global_records.pop(str(path), None)
    reopened = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    try:
        inactive = reopened.get_record("backend-record", include_inactive=True)
        assert inactive is not None
        assert inactive.metadata["active"] is False
        assert inactive.metadata["tombstone_reason"] == "superseded"
        assert reopened.get_record("backend-record", include_inactive=False) is None
    finally:
        reopened.close()


def test_in_memory_backend_rejects_cross_layer_put_and_upsert(tmp_path: Path) -> None:
    backend = InMemoryBackend(path=tmp_path / "working.mv2", layer=MemoryLayer.WORKING)
    backend.open()
    wrong_layer = MemoryRecord(
        title="Wrong layer",
        content="A semantic record cannot be written to working backend.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.9,
    )

    for operation in (backend.put, backend.upsert):
        try:
            operation(wrong_layer)
        except ValueError as exc:
            assert "Cannot write" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("cross-layer write was accepted")
