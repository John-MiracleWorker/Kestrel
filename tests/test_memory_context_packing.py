from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.context_compiler import ContextCompiler, ContextCompilerConfig
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def test_context_pack_sections_render_in_trust_order_with_evidence_pointers(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    sentinel = "sentinel_context_order_76ab"
    _put(memory, MemoryLayer.WORKING, "Active observation", f"{sentinel} active observation.", confidence=0.4)
    _put(memory, MemoryLayer.EPISODIC, "Recent event", f"{sentinel} recent event.", kind=MemoryKind.EVENT)
    _put(memory, MemoryLayer.SEMANTIC, "Stable fact", f"{sentinel} stable fact.")
    _put(memory, MemoryLayer.PROCEDURAL, "Recipe", f"{sentinel} repeatable recipe.", kind=MemoryKind.PROCEDURE)
    _put(memory, MemoryLayer.SELF, "User workflow preference", f"{sentinel} workflow preference.")
    _put(memory, MemoryLayer.POLICY, "Hard constraint", f"{sentinel} hard constraint.", kind=MemoryKind.POLICY, confidence=0.98)

    packed = ContextPacker(memory).pack(ContextPackRequest(objective=sentinel, query=sentinel))

    sections = [
        "## Hard Policy Constraints",
        "## Soul / Self Model",
        "## Relevant Procedures",
        "## Stable Facts",
        "## Recent Episodic/Task State",
        "## Working Memory",
    ]
    assert [packed.prompt.index(section) for section in sections] == sorted(packed.prompt.index(section) for section in sections)
    assert [item.frame.layer for item in packed.items] == [
        MemoryLayer.POLICY,
        MemoryLayer.SELF,
        MemoryLayer.PROCEDURAL,
        MemoryLayer.SEMANTIC,
        MemoryLayer.EPISODIC,
        MemoryLayer.WORKING,
    ]
    assert "## Evidence Pointers" in packed.prompt
    assert "test://policy" in packed.evidence_refs


def test_summary_first_omits_raw_but_keeps_correction_and_failure_frames(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    sentinel = "sentinel_summary_first_18ef"
    _put(memory, MemoryLayer.EPISODIC, "Task summary", f"{sentinel} compact task summary.", frame_type="task_summary")
    _put(memory, MemoryLayer.EPISODIC, "Raw transcript", f"{sentinel} raw transcript with exact command output.", frame_type="raw_chunk")
    _put(memory, MemoryLayer.EPISODIC, "Correction frame", f"{sentinel} correction remains visible.", kind=MemoryKind.CORRECTION, frame_type="correction")
    _put(memory, MemoryLayer.EPISODIC, "Failure frame", f"{sentinel} failure remains visible.", kind=MemoryKind.FAILURE, frame_type="failure_note")

    packed = ContextPacker(memory).pack(ContextPackRequest(objective=sentinel, query=sentinel, expand_raw=False))
    titles = [item.frame.title for item in packed.items]

    assert "Task summary" in titles
    assert "Raw transcript" not in titles
    assert "Correction frame" in titles
    assert "Failure frame" in titles


def test_exact_evidence_requests_expand_raw_and_summary_children(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    sentinel = "sentinel_exact_evidence_45aa"
    _put(
        memory,
        MemoryLayer.EPISODIC,
        "Linked summary",
        f"{sentinel} summary points to raw child.",
        frame_type="task_summary",
        metadata={"frame_id": "summary-exact", "child_ids": ["raw-exact-child"]},
    )
    _put(
        memory,
        MemoryLayer.EPISODIC,
        "Linked raw",
        f"{sentinel} raw child includes exact diff and line-level evidence.",
        frame_type="raw_chunk",
        metadata={"frame_id": "raw-exact-child", "parent_ids": ["summary-exact"]},
    )

    compact = ContextPacker(memory).pack(ContextPackRequest(objective=sentinel, query=sentinel))
    exact = ContextPacker(memory).pack(
        ContextPackRequest(
            objective=f"Need exact quote and line-level evidence for {sentinel}",
            query=sentinel,
        )
    )

    assert "Linked raw" not in [item.frame.title for item in compact.items]
    assert "expanded child raw-exact-child" in exact.prompt
    assert "raw child includes exact diff" in exact.prompt


def test_conflicting_semantic_facts_emit_prompt_warning_and_ledger_metadata(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    first_id = memory.put(
        MemoryRecord(
            id="alpha-enabled",
            title="Feature alpha status",
            content="Feature alpha is enabled for sentinel_conflict_alpha_62.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
            metadata={"promotion_id": "promotion-alpha", "promotion_status": "confirmed"},
        )
    )
    second_id = memory.put(
        MemoryRecord(
            id="alpha-disabled",
            title="Feature alpha status",
            content="Feature alpha is not enabled for sentinel_conflict_alpha_62.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.91,
        )
    )

    packed = ContextPacker(memory).pack(
        ContextPackRequest(objective="Resolve feature alpha status", query="sentinel_conflict_alpha_62")
    )
    first = memory.get_record(MemoryLayer.SEMANTIC, first_id)
    second = memory.get_record(MemoryLayer.SEMANTIC, second_id)

    assert packed.conflict_warnings
    assert "Feature alpha status" in packed.conflict_warnings[0]
    assert "report conflicts instead of merging them silently" in packed.prompt
    assert first is not None and first.metadata["conflict_group_id"]
    assert second is not None and second.metadata["conflict_group_id"] == first.metadata["conflict_group_id"]


def test_context_compiler_propagates_packer_warnings_and_exact_expansion(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        MemoryLayer.SEMANTIC,
        "Compiler conflict",
        "Compiler flag is enabled for sentinel_compiler_conflict_51.",
        confidence=0.9,
    )
    _put(
        memory,
        MemoryLayer.SEMANTIC,
        "Compiler conflict",
        "Compiler flag is not enabled for sentinel_compiler_conflict_51.",
        confidence=0.91,
    )
    _put(
        memory,
        MemoryLayer.EPISODIC,
        "Compiler raw",
        "sentinel_compiler_conflict_51 raw stack trace with exact evidence.",
        frame_type="raw_chunk",
    )

    compiled = ContextCompiler(
        memory,
        config=ContextCompilerConfig(total_budget_chars=5000, context_pack_token_budget=800, expand_raw=True),
    ).compile("Need exact evidence for sentinel_compiler_conflict_51")

    assert compiled.objective == "Need exact evidence for sentinel_compiler_conflict_51"
    assert compiled.warnings
    assert "Compiler raw" in compiled.prompt
    assert compiled.total_chars <= compiled.budget_chars


def _memory(tmp_path: Path) -> LayeredMemorySystem:
    return LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )


def _put(
    memory: LayeredMemorySystem,
    layer: MemoryLayer,
    title: str,
    content: str,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    confidence: float = 0.85,
    frame_type: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    effective_frame = frame_type or {
        MemoryLayer.WORKING: "raw_chunk",
        MemoryLayer.EPISODIC: "session_summary",
        MemoryLayer.SEMANTIC: "section_summary",
        MemoryLayer.PROCEDURAL: "skill_card",
        MemoryLayer.SELF: "self_model",
        MemoryLayer.POLICY: "trace_stub",
    }[layer]
    memory.put(
        MemoryRecord(
            title=title,
            content=content,
            layer=layer,
            kind=kind,
            confidence=confidence,
            importance=0.75,
            metadata={
                "frame_type": effective_frame,
                "source_uri": f"test://{layer.value}",
                **(metadata or {}),
            },
        )
    )
