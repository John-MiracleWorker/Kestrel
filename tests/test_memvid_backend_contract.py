from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.models import MemoryLayer, MemoryRecord


def test_backend_contract_put_find_verify(tmp_path: Path) -> None:
    backend = InMemoryBackend(path=tmp_path / "working.mv2", layer=MemoryLayer.WORKING)
    backend.open()
    record_id = backend.put(
        MemoryRecord(
            title="Contract test",
            content="The backend must retrieve auth profile memories.",
            layer=MemoryLayer.WORKING,
            confidence=0.5,
        )
    )
    hits = backend.find("auth profile", k=3)
    assert record_id
    assert hits
    assert backend.verify()
