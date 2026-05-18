from __future__ import annotations

import os
from pathlib import Path

import pytest

from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MEMVID_INTEGRATION") != "1",
    reason="Set RUN_MEMVID_INTEGRATION=1 and install memvid-sdk to run Memvid integration tests.",
)


@pytest.fixture(autouse=True)
def _require_memvid_sdk() -> None:
    pytest.importorskip("memvid_sdk")


def test_memvid_layered_memory_creates_one_mv2_per_layer_and_reopens_existing_files(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", MemvidBackend)
    try:
        assert {path.name for path in (tmp_path / "memory").glob("*.mv2")} == {
            "working.mv2",
            "episodic.mv2",
            "semantic.mv2",
            "procedural.mv2",
            "self.mv2",
            "policy.mv2",
        }
        memory.put(
            MemoryRecord(
                id="memvid-system-fact",
                title="Memvid system fact",
                content="sentinel_memvid_system_81aa survives seal and reopen.",
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                confidence=0.92,
                metadata={"frame_id": "memvid-system-frame", "frame_type": "section_summary"},
            )
        )
        memory.seal_all()
        assert memory.verify_all()[MemoryLayer.SEMANTIC] is True
    finally:
        memory.close_all()

    reopened = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", MemvidBackend)
    try:
        hits = reopened.retrieve(
            RetrievalQuery(query="sentinel_memvid_system_81aa", layers=(MemoryLayer.SEMANTIC,), k_per_layer=5)
        )
        assert hits
        assert hits[0].record.metadata["frame_id"] == "memvid-system-frame"
        assert reopened.tombstone(MemoryLayer.SEMANTIC, "memvid-system-fact", reason="integration", superseded_by="next")
        reopened.seal_all()
    finally:
        reopened.close_all()

    final = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", MemvidBackend)
    try:
        assert final.get_record(MemoryLayer.SEMANTIC, "memvid-system-fact", include_inactive=False) is None
        inactive = final.get_record(MemoryLayer.SEMANTIC, "memvid-system-fact", include_inactive=True)
        assert inactive is not None
        assert inactive.metadata["active"] is False
        inactive_hits = final.retrieve(
            RetrievalQuery(
                query="sentinel_memvid_system_81aa",
                layers=(MemoryLayer.SEMANTIC,),
                include_inactive=True,
            )
        )
        assert inactive_hits
    finally:
        final.close_all()
