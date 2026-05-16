from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .context_frames import MV2ContextFrame, estimate_tokens, from_memory_record
from .layers import LayeredMemorySystem
from .models import MemoryHit, MemoryLayer, MemoryRecord, RetrievalQuery

SUMMARY_FRAME_TYPES = frozenset({"section_summary", "task_summary", "session_summary", "skill_card", "trace_stub", "self_model"})
RAW_FRAME_TYPES = frozenset({"raw_chunk"})
CORRECTION_FRAME_TYPES = frozenset({"correction", "failure_note", "conflict_set"})
PACK_LAYER_ORDER = (
    MemoryLayer.POLICY,
    MemoryLayer.SELF,
    MemoryLayer.PROCEDURAL,
    MemoryLayer.SEMANTIC,
    MemoryLayer.EPISODIC,
    MemoryLayer.WORKING,
)


@dataclass(frozen=True)
class ContextPackRequest:
    objective: str
    query: str | None = None
    model_hint: str | None = None
    token_budget: int = 6000
    allowed_layers: tuple[MemoryLayer, ...] = PACK_LAYER_ORDER
    expand_raw: bool = False
    include_telemetry: bool = True
    k_per_layer: int = 8


@dataclass(frozen=True)
class PackedContextItem:
    hit: MemoryHit
    frame: MV2ContextFrame
    content: str
    token_count: int
    expanded: bool
    reason: str

    @property
    def evidence_ref(self) -> str:
        return self.frame.source_uri or f"memory://{self.frame.layer.value}/{self.frame.id}"


@dataclass(frozen=True)
class ContextPackResult:
    objective: str
    query: str
    prompt: str
    items: tuple[PackedContextItem, ...]
    token_estimate: int
    token_budget: int
    conflict_warnings: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    telemetry: dict[str, object] = field(default_factory=dict)

    @property
    def hits(self) -> tuple[MemoryHit, ...]:
        return tuple(item.hit for item in self.items)


class ContextPacker:
    """Retrieval, summary-first expansion, conflict detection, and token packing."""

    def __init__(self, memory: LayeredMemorySystem) -> None:
        self.memory = memory

    def pack(self, request: ContextPackRequest) -> ContextPackResult:
        query = (request.query or request.objective).strip()
        if not request.objective.strip():
            raise ValueError("ContextPackRequest.objective cannot be empty")
        if not query:
            raise ValueError("ContextPackRequest.query cannot be empty")

        layers = request.allowed_layers or PACK_LAYER_ORDER
        hits = self.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=tuple(layers),
                k_per_layer=request.k_per_layer,
                objective=request.objective,
            )
        )
        ordered = sorted(hits, key=self._rank_key, reverse=True)
        conflict_warnings = _detect_conflicts(ordered)
        selected = self._select_items(ordered, request)
        prompt = self._render(request, query, selected, conflict_warnings)
        token_estimate = estimate_tokens(prompt, request.model_hint)
        if token_estimate > request.token_budget:
            prompt = _truncate_to_tokens(prompt, max(request.token_budget - 16, 1))
            token_estimate = estimate_tokens(prompt, request.model_hint)
        telemetry: dict[str, object] = {
            "retrieved": len(hits),
            "selected": len(selected),
            "budget_tokens": request.token_budget,
            "estimated_tokens": token_estimate,
            "layers": sorted({item.frame.layer.value for item in selected}),
            "summary_first": True,
            "expand_raw": request.expand_raw,
        }
        if not request.include_telemetry:
            telemetry = {}
        return ContextPackResult(
            objective=request.objective,
            query=query,
            prompt=prompt,
            items=tuple(selected),
            token_estimate=token_estimate,
            token_budget=request.token_budget,
            conflict_warnings=tuple(conflict_warnings),
            evidence_refs=tuple(item.evidence_ref for item in selected),
            telemetry=telemetry,
        )

    def _select_items(self, hits: list[MemoryHit], request: ContextPackRequest) -> list[PackedContextItem]:
        selected: list[PackedContextItem] = []
        seen_hashes: set[str] = set()
        used_tokens = estimate_tokens(_prompt_scaffold(request.objective), request.model_hint)
        has_summary = any(_frame_for(hit).frame_type in SUMMARY_FRAME_TYPES for hit in hits)
        needs_exact = _needs_exact_evidence(request.objective, request.query or "")

        for hit in hits:
            frame = _frame_for(hit)
            if frame.content_hash in seen_hashes:
                continue
            if _too_similar(frame.content, [item.content for item in selected]):
                continue
            should_expand = _should_expand_raw(
                frame,
                request_expand=request.expand_raw,
                has_summary=has_summary,
                needs_exact=needs_exact,
            )
            if frame.frame_type in RAW_FRAME_TYPES and not should_expand:
                continue
            expand_children = frame.frame_type in SUMMARY_FRAME_TYPES and (request.expand_raw or needs_exact)
            content = frame.content if should_expand or expand_children else hit.snippet or frame.content
            reason = _selection_reason(frame, should_expand)
            if should_expand:
                content = frame.content
            if expand_children:
                expanded_content = self._content_with_child_frames(frame, content)
                if expanded_content != content:
                    content = expanded_content
                    should_expand = True
                    reason = "expanded_child_frames"
            token_count = max(estimate_tokens(content, request.model_hint), frame.token_count if should_expand else 0)
            if used_tokens + token_count > request.token_budget:
                if selected:
                    continue
                content = _truncate_to_tokens(content, max(request.token_budget - used_tokens, 64))
                token_count = estimate_tokens(content, request.model_hint)
            selected.append(
                PackedContextItem(
                    hit=hit,
                    frame=frame,
                    content=content,
                    token_count=token_count,
                    expanded=should_expand,
                    reason=reason,
                )
            )
            used_tokens += token_count
            seen_hashes.add(frame.content_hash)
            if used_tokens >= request.token_budget:
                break

        return selected

    def _render(
        self,
        request: ContextPackRequest,
        query: str,
        items: list[PackedContextItem],
        conflict_warnings: list[str],
    ) -> str:
        by_layer: dict[MemoryLayer, list[PackedContextItem]] = defaultdict(list)
        for item in items:
            by_layer[item.frame.layer].append(item)

        lines = [
            "# MV2 PSEUDO-CONTEXT PACK",
            "",
            "## Current Objective",
            request.objective.strip(),
            "",
            "## Hard Policy Constraints",
            "POLICY MEMORY",
        ]
        lines.extend(_section_lines(by_layer.get(MemoryLayer.POLICY, []), empty="No matching policy memory retrieved."))
        lines.extend(["", "## Soul / Self Model", "SELF MEMORY"])
        lines.extend(_section_lines(by_layer.get(MemoryLayer.SELF, []), empty="No matching self memory retrieved."))
        lines.extend(["", "## Relevant Procedures", "PROCEDURAL MEMORY"])
        lines.extend(_section_lines(by_layer.get(MemoryLayer.PROCEDURAL, []), empty="No matching procedural memory retrieved."))
        lines.extend(["", "## Stable Facts", "SEMANTIC MEMORY"])
        lines.extend(_section_lines(by_layer.get(MemoryLayer.SEMANTIC, []), empty="No matching semantic memory retrieved."))
        lines.extend(["", "## Recent Episodic/Task State", "EPISODIC MEMORY"])
        lines.extend(_section_lines(by_layer.get(MemoryLayer.EPISODIC, []), empty="No matching episodic memory retrieved."))
        lines.extend(["", "## Working Memory", "WORKING MEMORY"])
        lines.extend(_section_lines(by_layer.get(MemoryLayer.WORKING, []), empty="No matching working memory retrieved."))
        lines.extend(["", "## Conflict Warnings"])
        lines.extend([f"- {warning}" for warning in conflict_warnings] or ["- No conflicts detected in retrieved memory metadata."])
        lines.extend(["", "## Evidence Pointers"])
        lines.extend([f"- {item.evidence_ref} ({item.frame.frame_type}, {item.reason})" for item in items] or ["- none"])
        if request.include_telemetry:
            lines.extend(
                [
                    "",
                    "## Retrieval Telemetry",
                    f"- query: {query}",
                    f"- selected_items: {len(items)}",
                    f"- token_budget: {request.token_budget}",
                    f"- expand_raw: {request.expand_raw}",
                ]
            )
        lines.extend(
            [
                "",
                "## Next-step Instruction",
                "Use the packed memory as bounded evidence. Retrieve or expand raw context before relying on missing exact details, and report conflicts instead of merging them silently.",
            ]
        )
        return "\n".join(lines).strip()

    @staticmethod
    def _rank_key(hit: MemoryHit) -> tuple[int, int, float, float, float]:
        frame = _frame_for(hit)
        layer_rank = len(PACK_LAYER_ORDER) - PACK_LAYER_ORDER.index(frame.layer) if frame.layer in PACK_LAYER_ORDER else 0
        frame_rank = 3 if frame.frame_type in SUMMARY_FRAME_TYPES else 2 if frame.frame_type in CORRECTION_FRAME_TYPES else 1
        return (layer_rank, frame_rank, hit.score, frame.importance, frame.confidence)

    def _content_with_child_frames(self, frame: MV2ContextFrame, base_content: str) -> str:
        if not frame.child_ids:
            return base_content
        lines = [base_content.strip()]
        for child_id in frame.child_ids:
            record = self._find_record_by_id(child_id)
            if record is None:
                continue
            child_frame = from_memory_record(record)
            if child_frame.frame_type not in RAW_FRAME_TYPES | CORRECTION_FRAME_TYPES:
                continue
            lines.append(f"[expanded child {child_frame.id} / {child_frame.frame_type}]\n{record.content.strip()}")
        return "\n\n".join(line for line in lines if line)

    def _find_record_by_id(self, lookup_id: str) -> MemoryRecord | None:
        for backend in self.memory.backends.values():
            records = getattr(backend, "records", None)
            if isinstance(records, list):
                for raw_record in records:
                    if not isinstance(raw_record, MemoryRecord):
                        continue
                    record = raw_record
                    metadata = getattr(record, "metadata", {})
                    if record.id == lookup_id or str(metadata.get("frame_id", "")) == lookup_id:
                        return record
        for hit in self.memory.retrieve(RetrievalQuery(query=lookup_id, k_per_layer=5)):
            metadata = hit.record.metadata
            if hit.record.id == lookup_id or str(metadata.get("frame_id", "")) == lookup_id or hit.frame_id == lookup_id:
                return hit.record
        return None


def _frame_for(hit: MemoryHit) -> MV2ContextFrame:
    frame_type = str(hit.record.metadata.get("frame_type") or ("correction" if hit.record.kind.value == "correction" else "raw_chunk"))
    return from_memory_record(hit.record, frame_type=frame_type)


def _section_lines(items: list[PackedContextItem], *, empty: str) -> list[str]:
    if not items:
        return [empty]
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        frame = item.frame
        lines.append(
            f"{index}. {frame.title} "
            f"[layer={frame.layer.value}, kind={frame.kind.value}, frame={frame.frame_type}, "
            f"score={item.hit.score:.3f}, confidence={frame.confidence:.2f}]"
        )
        lines.append(f"   {item.content.strip()}")
    return lines


def _prompt_scaffold(objective: str) -> str:
    return f"# MV2 PSEUDO-CONTEXT PACK\n## Current Objective\n{objective}\n## Next-step Instruction\n"


def _should_expand_raw(
    frame: MV2ContextFrame,
    *,
    request_expand: bool,
    has_summary: bool,
    needs_exact: bool,
) -> bool:
    if frame.frame_type in CORRECTION_FRAME_TYPES:
        return True
    if frame.frame_type not in RAW_FRAME_TYPES:
        return False
    if request_expand or needs_exact:
        return True
    if frame.confidence < 0.55:
        return True
    return not has_summary


def _needs_exact_evidence(objective: str, query: str) -> bool:
    text = f"{objective} {query}".lower()
    markers = ("exact", "quote", "code", "diff", "stack trace", "error", "evidence", "line ")
    return any(marker in text for marker in markers)


def _selection_reason(frame: MV2ContextFrame, expanded: bool) -> str:
    if expanded:
        return "expanded_raw_or_correction"
    if frame.frame_type in SUMMARY_FRAME_TYPES:
        return "summary_first"
    return "ranked_retrieval"


def _truncate_to_tokens(text: str, token_budget: int) -> str:
    max_chars = max(token_budget * 4, 0)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[TRUNCATED_BY_CONTEXT_PACKER]"


def _too_similar(content: str, existing: list[str]) -> bool:
    candidate_tokens = set(_tokens(content))
    if not candidate_tokens:
        return False
    for other in existing:
        other_tokens = set(_tokens(other))
        if not other_tokens:
            continue
        overlap = len(candidate_tokens & other_tokens) / max(min(len(candidate_tokens), len(other_tokens)), 1)
        if overlap >= 0.88:
            return True
    return False


def _detect_conflicts(hits: list[MemoryHit]) -> list[str]:
    warnings: list[str] = []
    by_group: dict[str, list[MemoryHit]] = defaultdict(list)
    by_title: dict[str, list[MemoryHit]] = defaultdict(list)
    for hit in hits:
        metadata = hit.record.metadata
        group_id = metadata.get("conflict_group_id")
        if group_id:
            by_group[str(group_id)].append(hit)
        if hit.record.confidence >= 0.75:
            by_title[_normalize_claim_key(hit.record.title)].append(hit)

    for group_id, grouped in sorted(by_group.items()):
        if len(grouped) > 1 or any(_frame_for(hit).frame_type == "conflict_set" for hit in grouped):
            titles = "; ".join(hit.record.title for hit in grouped[:4])
            warnings.append(f"conflict_group_id={group_id} has {len(grouped)} retrieved memories: {titles}")

    for title_key, grouped in by_title.items():
        if len(grouped) < 2:
            continue
        polarities = {_polarity(hit.record.content) for hit in grouped}
        if "positive" in polarities and "negative" in polarities:
            titles = "; ".join(hit.record.title for hit in grouped[:4])
            warnings.append(f"possible high-confidence disagreement around `{title_key}`: {titles}")

    return warnings


def _normalize_claim_key(title: str) -> str:
    return " ".join(_tokens(title))[:80] or "untitled"


def _polarity(text: str) -> str:
    lowered = text.lower()
    negative_markers = (" not ", " never ", " no longer ", " incorrect", " false", " avoid ", " do not ")
    return "negative" if any(marker in f" {lowered} " for marker in negative_markers) else "positive"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_]+", text.lower())
