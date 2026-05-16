from __future__ import annotations

import os
from pathlib import Path

import pytest

from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.context_frames import MV2ContextFrame
from nested_memvid_agent.models import MemoryKind, MemoryLayer

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MEMVID_INTEGRATION") != "1",
    reason="Set RUN_MEMVID_INTEGRATION=1 and install memvid-sdk to run Memvid context-frame integration tests.",
)


def test_memvid_context_frame_metadata_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "semantic.mv2"
    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    backend.put_frame(
        MV2ContextFrame(
            id="frame_integration",
            frame_type="section_summary",
            title="Frame integration",
            content="Frame integration metadata survives seal and reopen.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            parent_ids=("raw_parent",),
            child_ids=("raw_child",),
            source_uri="file://integration",
            source_span={"line": 7},
            confidence=0.9,
            importance=0.7,
        )
    )
    backend.seal()
    assert backend.verify()
    backend.close()

    reopened = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    try:
        hits = reopened.find_frames("integration metadata", frame_types=("section_summary",), k=5)
        assert hits
        metadata = hits[0].record.metadata
        assert metadata["frame_id"] == "frame_integration"
        assert metadata["frame_type"] == "section_summary"
        assert metadata["parent_ids"] == ["raw_parent"]
        assert metadata["source_span"] == {"line": 7}
    finally:
        reopened.close()
