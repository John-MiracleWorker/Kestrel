from __future__ import annotations

from datetime import UTC, datetime

from nested_memvid_agent.context_frames import (
    MV2ContextFrame,
    content_hash_for,
    estimate_tokens,
    from_memory_record,
    make_conflict_set_frame,
    make_correction_frame,
    to_memory_record,
)
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def test_frame_converts_to_and_from_memory_record() -> None:
    frame = MV2ContextFrame(
        id="frame_1",
        frame_type="section_summary",
        title="Auth summary",
        content="Auth profiles live in provider-specific config.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        parent_ids=("raw_1",),
        child_ids=("raw_2",),
        source_uri="file://README.md",
        source_span={"line_start": 1, "line_end": 3},
        confidence=0.88,
        importance=0.7,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        tags={"topic": "auth"},
    )

    record = to_memory_record(frame)
    restored = from_memory_record(record)

    assert record.id == "frame_1"
    assert record.metadata["mv2_ctx_version"] == "0.1"
    assert restored.frame_type == "section_summary"
    assert restored.parent_ids == ("raw_1",)
    assert restored.child_ids == ("raw_2",)
    assert restored.source_span == {"line_start": 1, "line_end": 3}


def test_token_estimate_exists_and_content_hash_is_stable() -> None:
    text = "The context frame token estimator is intentionally approximate."

    assert estimate_tokens(text) > 0
    assert content_hash_for(text) == content_hash_for(text)
    assert content_hash_for(text) != content_hash_for(text + " changed")


def test_parent_child_metadata_preserved_from_record() -> None:
    record = MemoryRecord(
        id="record_1",
        title="Raw evidence",
        content="Raw evidence supporting a summary.",
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.EVENT,
        metadata={
            "frame_type": "raw_chunk",
            "frame_id": "frame_raw",
            "parent_ids": ["summary_1"],
            "child_ids": ["line_1", "line_2"],
            "source_uri": "tool://shell.run",
            "source_span": {"offset": 12},
        },
        confidence=0.7,
    )

    frame = from_memory_record(record)

    assert frame.id == "frame_raw"
    assert frame.parent_ids == ("summary_1",)
    assert frame.child_ids == ("line_1", "line_2")
    assert frame.source_uri == "tool://shell.run"


def test_correction_and_conflict_frames_preserve_links() -> None:
    correction = make_correction_frame(
        target_record_id="fact-1",
        layer=MemoryLayer.SEMANTIC,
        correction_text="Feature alpha is not enabled.",
        evidence=[],
    )
    conflict = make_conflict_set_frame(
        layer=MemoryLayer.SEMANTIC,
        conflict_group_id="conflict-feature-alpha",
        member_ids=("fact-1", correction.id),
        reason="polarity mismatch",
    )

    assert correction.frame_type == "correction"
    assert correction.parent_ids == ("fact-1",)
    assert correction.metadata["corrects"] == ["fact-1"]
    assert conflict.frame_type == "conflict_set"
    assert conflict.metadata["conflict_group_id"] == "conflict-feature-alpha"
    assert conflict.parent_ids == ("fact-1", correction.id)
