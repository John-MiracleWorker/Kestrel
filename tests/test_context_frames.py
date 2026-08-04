from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nested_memvid_agent.context_frames import (
    MV2ContextFrame,
    content_hash_for,
    direct_frame_lookup,
    estimate_tokens,
    from_memory_record,
    make_child_frame,
    make_conflict_set_frame,
    make_correction_frame,
    to_memory_record,
)
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def _project_frame(frame_id: str, project_id: str | None) -> MV2ContextFrame:
    return MV2ContextFrame(
        id=frame_id,
        frame_type="raw_chunk",
        title=f"Evidence {frame_id}",
        content=f"Evidence payload for {frame_id}.",
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.EVENT,
        project_id=project_id,
    )


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


def test_child_frame_inherits_parent_project_boundary() -> None:
    parent = _project_frame("parent_a", "project_a")

    child = make_child_frame(
        parent,
        frame_id="child_a",
        frame_type="section_summary",
        title="Child summary",
        content="Derived entirely from project A evidence.",
    )

    assert child.project_id == "project_a"
    assert child.parent_ids == ("parent_a",)


def test_child_frame_cannot_smuggle_cross_project_evidence() -> None:
    parent = _project_frame("parent_a", "project_a")

    with pytest.raises(ValueError, match="project boundary"):
        make_child_frame(
            parent,
            frame_id="child_b",
            frame_type="section_summary",
            title="Smuggled summary",
            content="Evidence harvested from project B.",
            project_id="project_b",
        )


def test_direct_frame_lookup_is_fenced_to_the_selected_project() -> None:
    frame_a = _project_frame("frame_a", "project_a")
    frame_b = _project_frame("frame_b", "project_b")
    frames = {frame_a.id: frame_a, frame_b.id: frame_b}

    assert direct_frame_lookup(frames, "frame_a", project_id="project_a") is frame_a
    with pytest.raises(KeyError, match="project boundary"):
        direct_frame_lookup(frames, "frame_b", project_id="project_a")
    with pytest.raises(KeyError, match="unknown frame"):
        direct_frame_lookup(frames, "frame_missing", project_id="project_a")


def test_direct_frame_lookup_rejects_unscoped_frames_fail_closed() -> None:
    unscoped = _project_frame("frame_legacy", None)

    with pytest.raises(KeyError, match="project boundary"):
        direct_frame_lookup({unscoped.id: unscoped}, "frame_legacy", project_id="project_a")


def test_project_id_round_trips_through_memory_record_metadata() -> None:
    frame = _project_frame("frame_scoped", "project_a")

    record = to_memory_record(frame)
    restored = from_memory_record(record)

    assert record.metadata["project_id"] == "project_a"
    assert restored.project_id == "project_a"
    unscoped = from_memory_record(to_memory_record(_project_frame("frame_plain", None)))
    assert unscoped.project_id is None
