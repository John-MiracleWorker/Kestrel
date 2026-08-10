from __future__ import annotations

import os
from pathlib import Path

import pytest

from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MEMVID_INTEGRATION") != "1",
    reason="Set RUN_MEMVID_INTEGRATION=1 and install memvid-sdk to run Memvid integration tests.",
)


@pytest.fixture(autouse=True)
def _require_memvid_sdk() -> None:
    pytest.importorskip("memvid_sdk")


def test_memvid_retrieval_cases_are_visible_after_explicit_seal(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    expected_layer_files = {
        "working.mv2",
        "episodic.mv2",
        "semantic.mv2",
        "procedural.mv2",
        "self.mv2",
        "policy.mv2",
    }
    summary_id = "golden_memvid_summary_5e71"
    raw_id = "golden_memvid_raw_5e71"
    sentinel = "golden_memvid_retrieval_5e71"
    raw_evidence = "exact supporting command output exit=0 from the sealed Memvid record"

    memory = LayeredMemorySystem.from_backend_factory(
        memory_dir,
        MemvidBackend,
        enforce_stable_write_integrity=False,
    )
    try:
        memory.put(
            MemoryRecord(
                id=summary_id,
                title="Golden Memvid summary",
                content=f"{sentinel} summary points to its bounded raw evidence.",
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                confidence=0.95,
                importance=0.9,
                metadata={
                    "frame_id": summary_id,
                    "frame_type": "task_summary",
                    "child_ids": [raw_id],
                },
            )
        )
        memory.put(
            MemoryRecord(
                id=raw_id,
                title="Golden Memvid raw evidence",
                content=raw_evidence,
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.EVENT,
                confidence=0.9,
                importance=0.8,
                metadata={
                    "frame_id": raw_id,
                    "frame_type": "raw_chunk",
                    "parent_ids": [summary_id],
                },
            )
        )
        memory.seal_all()
    finally:
        memory.close_all()

    assert {path.name for path in memory_dir.glob("*.mv2")} == expected_layer_files

    reopened = LayeredMemorySystem.from_backend_factory(memory_dir, MemvidBackend)
    try:
        compact = ContextPacker(reopened).pack(
            ContextPackRequest(
                objective=f"Recall {sentinel}",
                query=sentinel,
                expand_raw=False,
                k_per_layer=16,
            )
        )
        expanded = ContextPacker(reopened).pack(
            ContextPackRequest(
                objective=f"Recall exact evidence for {sentinel}",
                query=sentinel,
                expand_raw=True,
                k_per_layer=16,
            )
        )

        assert compact.hits
        assert compact.prompt
        assert any(item.frame.id == summary_id for item in compact.items)
        assert raw_evidence not in compact.prompt
        assert raw_evidence in expanded.prompt
        assert any(item.reason == "expanded_child_frames" for item in expanded.items)
    finally:
        reopened.close_all()

    assert {path.name for path in memory_dir.glob("*.mv2")} == expected_layer_files
