from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryHit, MemoryKind, MemoryLayer, MemoryRecord


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


def test_packer_conflict_output_is_independent_of_backend_tie_order_and_snippets() -> None:
    marker = "golden seeded conflict"
    enabled = MemoryRecord(
        id="conflict-enabled",
        title=f"{marker} enabled",
        content=f"{marker}: Feature gamma is enabled.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.88,
        importance=0.8,
        metadata={"conflict_group_id": marker},
    )
    disabled = MemoryRecord(
        id="conflict-disabled",
        title=f"{marker} disabled",
        content=f"{marker}: Feature gamma is not enabled.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.88,
        importance=0.8,
        metadata={"conflict_group_id": marker},
    )
    enabled_hit = MemoryHit(
        record=enabled,
        score=2.0,
        source_backend="memvid",
        snippet=enabled.content + " created_at=volatile-a uri=volatile-a",
    )
    disabled_hit = MemoryHit(
        record=disabled,
        score=2.0,
        source_backend="memvid",
        snippet=disabled.content + " created_at=volatile-b uri=volatile-b",
    )

    class OrderedMemory:
        def __init__(self, hits: list[MemoryHit]) -> None:
            self.hits = hits

        def retrieve(self, _query: object) -> list[MemoryHit]:
            return list(self.hits)

        def get_record(
            self,
            _layer: MemoryLayer | None,
            _record_id: str,
            *,
            include_inactive: bool = True,
        ) -> MemoryRecord | None:
            del include_inactive
            return None

    request = ContextPackRequest(objective=marker, query=marker)
    forward = ContextPacker(OrderedMemory([enabled_hit, disabled_hit])).pack(request)  # type: ignore[arg-type]
    reverse = ContextPacker(OrderedMemory([disabled_hit, enabled_hit])).pack(request)  # type: ignore[arg-type]

    assert [item.frame.id for item in forward.items] == [
        item.frame.id for item in reverse.items
    ]
    assert forward.conflict_warnings == reverse.conflict_warnings
    assert forward.prompt == reverse.prompt


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


def test_packer_does_not_infer_exact_evidence_intent_from_identifier_fragments(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path)
    marker = "sentinel_exact_evidence_45aa"
    _put(
        memory,
        "Identifier summary",
        f"{marker} compact summary.",
        MemoryLayer.EPISODIC,
        frame_type="task_summary",
    )
    _put(
        memory,
        "Identifier raw",
        f"{marker} raw details.",
        MemoryLayer.EPISODIC,
        frame_type="raw_chunk",
    )

    packed = ContextPacker(memory).pack(
        ContextPackRequest(objective=marker, query=marker)
    )

    assert "Identifier raw" not in [item.frame.title for item in packed.items]


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


def test_packer_excludes_the_current_turn_frame_from_recalled_hits(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        "Current turn duplicate",
        "current-turn-duplicate-91af should not be recalled alongside the live user message.",
        MemoryLayer.WORKING,
        frame_type="raw_chunk",
        metadata={"frame_id": "turn_current_user"},
    )

    packed = ContextPacker(memory).pack(
        ContextPackRequest(
            objective="current-turn-duplicate-91af",
            query="current-turn-duplicate-91af",
            excluded_record_ids=frozenset({"turn_current_user"}),
        )
    )

    assert packed.hits == ()
    assert packed.telemetry["excluded"] == 1


def test_packer_isolates_project_memory_and_keeps_global_self_separate(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        "Project A fact",
        "sharedtoken belongs to project A.",
        MemoryLayer.SEMANTIC,
        metadata={"project_id": "project-a"},
    )
    _put(
        memory,
        "Project B fact",
        "sharedtoken belongs to project B.",
        MemoryLayer.SEMANTIC,
        metadata={"project_id": "project-b"},
    )
    _put(
        memory,
        "Global fact",
        "sharedtoken is portable global knowledge.",
        MemoryLayer.PROCEDURAL,
        confidence=0.9,
    )
    _put(
        memory,
        "Global self",
        "sharedtoken global user preference.",
        MemoryLayer.SELF,
    )
    _put(
        memory,
        "Invalid scoped self",
        "sharedtoken must never become project-owned self memory.",
        MemoryLayer.SELF,
        metadata={"project_id": "project-a"},
    )

    project_a = ContextPacker(memory).pack(
        ContextPackRequest(
            objective="sharedtoken",
            query="sharedtoken",
            project_id="project-a",
        )
    )
    unbound = ContextPacker(memory).pack(
        ContextPackRequest(objective="sharedtoken", query="sharedtoken")
    )

    project_titles = {item.frame.title for item in project_a.items}
    unbound_titles = {item.frame.title for item in unbound.items}
    assert "Project A fact" in project_titles
    assert "Project B fact" not in project_titles
    assert "Global fact" in project_titles
    assert "Global self" in project_titles
    assert "Invalid scoped self" not in project_titles
    assert "Project A fact" not in unbound_titles
    assert "Project B fact" not in unbound_titles


def test_project_summary_cannot_expand_a_cross_project_child(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _put(
        memory,
        "Project A summary",
        "crosschild summary.",
        MemoryLayer.EPISODIC,
        frame_type="task_summary",
        metadata={
            "frame_id": "summary-a",
            "child_ids": ["raw-b"],
            "project_id": "project-a",
        },
    )
    _put(
        memory,
        "Project B raw",
        "secret-cross-project-child",
        MemoryLayer.EPISODIC,
        frame_type="raw_chunk",
        metadata={
            "frame_id": "raw-b",
            "parent_ids": ["summary-a"],
            "project_id": "project-b",
        },
    )

    packed = ContextPacker(memory).pack(
        ContextPackRequest(
            objective="crosschild",
            query="crosschild",
            expand_raw=True,
            project_id="project-a",
        )
    )

    assert "Project A summary" in packed.prompt
    assert "secret-cross-project-child" not in packed.prompt


def _memory(tmp_path: Path) -> LayeredMemorySystem:
    return LayeredMemorySystem.from_backend_factory(
        tmp_path,
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )


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
