from __future__ import annotations

import os
from pathlib import Path

import pytest

from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.context_frames import MV2ContextFrame
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.task_capsule import summarize_run_capsule, write_run_capsule

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


def test_memvid_summary_expands_linked_raw_child_on_demand(
    tmp_path: Path,
) -> None:
    memory = build_memory_system(
        "memvid",
        tmp_path / "memory",
        enforce_stable_write_integrity=False,
    )
    try:
        marker = "memvid-linked-summary-4b2a"
        summary_id = "memvid_linked_summary"
        raw_id = "memvid_linked_raw"
        raw_payload = "child-only-payload-8f31 includes the complete command output."
        memory.put(
            MemoryRecord(
                id=summary_id,
                title=f"{marker} summary",
                content=f"{marker} compact result.",
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                confidence=0.8,
                importance=0.8,
                metadata={
                    "frame_type": "task_summary",
                    "frame_id": summary_id,
                    "child_ids": [raw_id],
                },
            )
        )
        memory.put(
            MemoryRecord(
                id=raw_id,
                title="Supporting raw evidence",
                content=raw_payload,
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.EVENT,
                confidence=0.8,
                importance=0.7,
                metadata={
                    "frame_type": "raw_chunk",
                    "frame_id": raw_id,
                    "parent_ids": [summary_id],
                },
            )
        )

        compact = ContextPacker(memory).pack(
            ContextPackRequest(objective=marker, query=marker)
        )
        expanded = ContextPacker(memory).pack(
            ContextPackRequest(objective=marker, query=marker, expand_raw=True)
        )

        assert raw_payload not in compact.prompt
        assert raw_payload in expanded.prompt
        summary_item = next(item for item in expanded.items if item.frame.id == summary_id)
        assert summary_item.reason == "expanded_child_frames"
    finally:
        memory.close_all()


def test_memvid_capsule_summary_reads_complete_mv2(tmp_path: Path) -> None:
    path = write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="run_memvid_capsule",
        objective="Summarize real Memvid capsule.",
        final_response="Capsule sealed.",
        backend="memvid",
        candidate_facts=("Memvid capsule summaries query complete.mv2 directly.",),
        candidate_procedures=("Repeated validated capsule steps become skill cards.",),
    )

    summary = summarize_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="run_memvid_capsule",
        backend="memvid",
    )

    assert path.name == "complete.mv2"
    assert not path.with_suffix(".memory.json").exists()
    assert "Objective: Summarize real Memvid capsule." in summary.summary
    assert {signal.kind for signal in summary.learning_signals} >= {MemoryKind.FACT, MemoryKind.PROCEDURE}
