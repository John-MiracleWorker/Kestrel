from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def test_packer_prefers_summaries_over_raw_chunks(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        "Alpha summary",
        "alpha deployment summary uses the cached plan.",
        MemoryLayer.SEMANTIC,
        frame_type="section_summary",
    )
    _put(
        memory,
        "Alpha raw",
        "alpha raw log output has verbose exact shell and stack details.",
        MemoryLayer.SEMANTIC,
        frame_type="raw_chunk",
    )

    packed = ContextPacker(memory).pack(ContextPackRequest(objective="alpha deployment", query="alpha"))

    assert packed.items
    assert packed.items[0].frame.frame_type == "section_summary"
    assert "Alpha raw" not in [item.frame.title for item in packed.items]


def test_packer_respects_token_budget(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    for index in range(5):
        _put(
            memory,
            f"Long summary {index}",
            "budget " + ("long content " * 400),
            MemoryLayer.SEMANTIC,
            frame_type="section_summary",
        )

    packed = ContextPacker(memory).pack(
        ContextPackRequest(objective="budget", query="budget", token_budget=180)
    )

    assert packed.token_estimate <= 180
    assert "TRUNCATED_BY_CONTEXT_PACKER" in packed.prompt


def test_packer_includes_policy_and_procedural_first(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(memory, "Working note", "workspace setting from working memory.", MemoryLayer.WORKING, confidence=0.4)
    _put(memory, "Procedure", "workspace procedure says verify memory.", MemoryLayer.PROCEDURAL, confidence=0.9)
    _put(memory, "Policy", "workspace policy says do not bypass approval gates.", MemoryLayer.POLICY, confidence=0.98)

    packed = ContextPacker(memory).pack(ContextPackRequest(objective="workspace", query="workspace"))

    assert [item.frame.layer for item in packed.items[:2]] == [MemoryLayer.POLICY, MemoryLayer.PROCEDURAL]


def test_packer_places_self_layer_between_policy_and_procedural(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(memory, "Procedure", "identity procedure says inspect tools before self-editing.", MemoryLayer.PROCEDURAL, confidence=0.9)
    _put(memory, "Policy", "identity policy says do not bypass approval gates.", MemoryLayer.POLICY, confidence=0.98)
    _put(memory, "Soul identity", "Kestrel's self model says it is a local-first engineering agent.", MemoryLayer.SELF, confidence=0.88)

    packed = ContextPacker(memory).pack(ContextPackRequest(objective="identity", query="identity"))

    assert [item.frame.layer for item in packed.items[:3]] == [
        MemoryLayer.POLICY,
        MemoryLayer.SELF,
        MemoryLayer.PROCEDURAL,
    ]
    assert "SELF MEMORY" in packed.prompt


def test_packer_detects_conflict_metadata(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        "Feature flag",
        "flag alpha is enabled.",
        MemoryLayer.SEMANTIC,
        confidence=0.86,
        metadata={"conflict_group_id": "flag-alpha"},
    )
    _put(
        memory,
        "Feature flag correction",
        "flag alpha is not enabled.",
        MemoryLayer.SEMANTIC,
        confidence=0.88,
        metadata={"conflict_group_id": "flag-alpha"},
    )

    packed = ContextPacker(memory).pack(ContextPackRequest(objective="flag alpha", query="flag alpha"))

    assert packed.conflict_warnings
    assert "flag-alpha" in packed.conflict_warnings[0]
    assert "conflict_group_id=flag-alpha" in packed.prompt


def test_packer_surfaces_correction_provenance(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        "Feature alpha correction",
        "feature alpha is not enabled.",
        MemoryLayer.SEMANTIC,
        kind=MemoryKind.CORRECTION,
        frame_type="correction",
        metadata={"corrects": ["feature-alpha-old"]},
    )

    packed = ContextPacker(memory).pack(ContextPackRequest(objective="feature alpha", query="feature alpha"))

    assert "corrects=feature-alpha-old" in packed.prompt


def test_packer_deduplicates_repeated_content(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    content = "duplicate fact says context summaries should point to raw chunks."
    _put(memory, "Duplicate one", content, MemoryLayer.SEMANTIC)
    _put(memory, "Duplicate two", content, MemoryLayer.SEMANTIC)

    packed = ContextPacker(memory).pack(ContextPackRequest(objective="duplicate context", query="duplicate context"))

    assert len(packed.items) == 1


def test_packer_expands_raw_only_when_requested(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        "Beta summary",
        "beta summary points to supporting raw context.",
        MemoryLayer.EPISODIC,
        frame_type="task_summary",
    )
    _put(
        memory,
        "Beta raw",
        "beta raw exact evidence contains full command output and log details.",
        MemoryLayer.EPISODIC,
        frame_type="raw_chunk",
    )

    compact = ContextPacker(memory).pack(ContextPackRequest(objective="beta", query="beta", expand_raw=False))
    expanded = ContextPacker(memory).pack(ContextPackRequest(objective="beta", query="beta", expand_raw=True))

    assert "Beta raw" not in [item.frame.title for item in compact.items]
    assert "Beta raw" in [item.frame.title for item in expanded.items]


def test_packer_expands_child_raw_frame_from_summary_link(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        "Gamma summary",
        "gamma summary points to exact supporting evidence.",
        MemoryLayer.EPISODIC,
        frame_type="task_summary",
        metadata={"frame_id": "summary_gamma", "child_ids": ["raw_gamma_child"]},
    )
    _put(
        memory,
        "Gamma raw child",
        "unique child payload with command output that does not repeat the query term.",
        MemoryLayer.EPISODIC,
        frame_type="raw_chunk",
        metadata={"frame_id": "raw_gamma_child", "parent_ids": ["summary_gamma"]},
    )

    compact = ContextPacker(memory).pack(ContextPackRequest(objective="gamma", query="gamma"))
    expanded = ContextPacker(memory).pack(
        ContextPackRequest(objective="gamma", query="gamma", expand_raw=True)
    )

    assert "unique child payload" not in compact.prompt
    assert "unique child payload" in expanded.prompt
    assert expanded.items[0].reason == "expanded_child_frames"


def _memory(tmp_path: Path) -> LayeredMemorySystem:
    return LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)


def _put(
    memory: LayeredMemorySystem,
    title: str,
    content: str,
    layer: MemoryLayer,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    confidence: float = 0.8,
    frame_type: str = "section_summary",
    metadata: dict[str, object] | None = None,
) -> None:
    memory.put(
        MemoryRecord(
            title=title,
            content=content,
            layer=layer,
            kind=kind,
            confidence=confidence,
            importance=0.7,
            metadata={"frame_type": frame_type, **(metadata or {})},
        )
    )
