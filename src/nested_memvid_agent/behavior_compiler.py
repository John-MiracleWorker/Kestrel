from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from re import sub

from .behavior_delta import BehaviorDelta, BehaviorDeltaKind, BehaviorDeltaStatus
from .behavior_delta_ledger import BehaviorDeltaActivation, BehaviorDeltaLedger
from .models import MemoryLayer
from .state_store import utc_now


@dataclass(frozen=True)
class BehaviorCompilerConfig:
    enabled: bool = False
    max_active_deltas_per_run: int = 8
    log_activations: bool = True

    def __post_init__(self) -> None:
        if self.max_active_deltas_per_run < 1:
            raise ValueError("max_active_deltas_per_run must be >= 1")


@dataclass(frozen=True)
class BehaviorCompileRequest:
    objective: str
    query: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    task_type: str | None = None
    tool_names: tuple[str, ...] = ()
    memory_layers: tuple[MemoryLayer, ...] = ()
    path: str | None = None


@dataclass(frozen=True)
class ToolPreflightContext:
    run_id: str | None
    task_id: str | None
    objective: str
    tool_name: str
    tool_arguments: dict[str, object]
    prior_failure_signature: str | None = None
    prior_failed_tool_name: str | None = None
    prior_failed_arguments_hash: str | None = None
    touched_paths: tuple[str, ...] = ()
    memory_layers: tuple[MemoryLayer, ...] = ()
    risk_tags: tuple[str, ...] = ()
    task_type: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class CompiledBehavior:
    text: str
    deltas: tuple[BehaviorDelta, ...]
    activation_reasons: dict[str, tuple[str, ...]] = field(default_factory=dict)


CompiledBehaviorDeltas = CompiledBehavior


class BehaviorCompiler:
    """Compile active behavior deltas into bounded runtime instructions.

    This compiler is intentionally separate from ContextPacker/ContextCompiler.
    It only renders active, relevant, evidence-backed deltas supplied by the
    ledger and can be disabled to produce an empty output.
    """

    def __init__(self, *, ledger: BehaviorDeltaLedger, config: BehaviorCompilerConfig | None = None) -> None:
        self.ledger = ledger
        self.config = config or BehaviorCompilerConfig()

    def compile(self, request: BehaviorCompileRequest) -> CompiledBehavior:
        if not self.config.enabled:
            return CompiledBehavior(text="", deltas=())

        candidates = self.ledger.list_deltas(status=BehaviorDeltaStatus.ACTIVE)
        relevant = [delta for delta in candidates if delta.evidence_refs and _matches(delta, request)]
        selected = self._select(relevant)
        if not selected:
            return CompiledBehavior(text="", deltas=())

        sections = _render_sections(selected)
        text = "\n".join(sections).strip()
        if self.config.log_activations:
            self._record_activations(selected, request)
        return CompiledBehavior(text=text, deltas=tuple(selected))

    def compile_for_tool_call(
        self,
        context: ToolPreflightContext,
        active_deltas: Sequence[BehaviorDelta],
        *,
        max_deltas: int = 5,
    ) -> CompiledBehavior:
        if not self.config.enabled:
            return CompiledBehavior(text="", deltas=())
        if max_deltas < 1:
            raise ValueError("max_deltas must be >= 1")

        matches: list[tuple[BehaviorDelta, tuple[str, ...]]] = []
        for delta in active_deltas:
            if delta.status != BehaviorDeltaStatus.ACTIVE or not delta.evidence_refs:
                continue
            reasons = _tool_call_match_reasons(delta, context)
            if reasons:
                matches.append((delta, reasons))
        selected = _select_tool_matches(matches, max_deltas=max_deltas)
        if not selected:
            return CompiledBehavior(text="", deltas=())

        selected_deltas = [delta for delta, _ in selected]
        reasons_by_delta = {delta.id: reasons for delta, reasons in selected}
        text = _render_tool_preflight_sections(selected_deltas)
        if self.config.log_activations:
            self._record_tool_activations(selected, context)
        return CompiledBehavior(
            text=text,
            deltas=tuple(selected_deltas),
            activation_reasons=reasons_by_delta,
        )

    def _select(self, deltas: list[BehaviorDelta]) -> list[BehaviorDelta]:
        deduped: list[BehaviorDelta] = []
        seen_changes: set[str] = set()
        for delta in sorted(deltas, key=_priority_key):
            normalized = " ".join(delta.behavior_change.split()).lower()
            if normalized in seen_changes:
                continue
            seen_changes.add(normalized)
            deduped.append(delta)
            if len(deduped) >= self.config.max_active_deltas_per_run:
                break
        return deduped

    def _record_activations(self, deltas: list[BehaviorDelta], request: BehaviorCompileRequest) -> None:
        for delta in deltas:
            if request.run_id and any(item.run_id == request.run_id for item in self.ledger.list_activations(delta.id)):
                continue
            section = _section_for(delta)
            activation_id = _activation_id(delta.id, request.run_id, request.task_id, section)
            self.ledger.record_activation(
                BehaviorDeltaActivation(
                    id=activation_id,
                    delta_id=delta.id,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    objective=request.objective,
                    activated_at=utc_now(),
                    activation_reason=_activation_reason(delta, request),
                    compiled_section=section,
                )
            )

    def _record_tool_activations(
        self,
        matches: list[tuple[BehaviorDelta, tuple[str, ...]]],
        context: ToolPreflightContext,
    ) -> None:
        for delta, reasons in matches:
            section = f"TOOL BEHAVIOR-DELTA PREFLIGHT: {_section_for(delta)}"
            activation_id = _tool_activation_id(delta.id, context, section)
            if any(item.id == activation_id for item in self.ledger.list_activations(delta.id)):
                continue
            self.ledger.record_activation(
                BehaviorDeltaActivation(
                    id=activation_id,
                    delta_id=delta.id,
                    run_id=context.run_id,
                    task_id=context.task_id,
                    objective=context.objective,
                    activated_at=utc_now(),
                    activation_reason=",".join(reasons),
                    compiled_section=section,
                )
            )


def _render_sections(deltas: list[BehaviorDelta]) -> list[str]:
    grouped: dict[str, list[BehaviorDelta]] = {}
    for delta in deltas:
        grouped.setdefault(_section_for(delta), []).append(delta)

    lines: list[str] = []
    for section in (
        "ACTIVE POLICY CONSTRAINTS",
        "ACTIVE SELF MODEL RULES",
        "ACTIVE PROCEDURES",
        "ACTIVE TOOL HEURISTICS",
        "ACTIVE CONTEXT-PACKING RULES",
        "ACTIVE RETRIEVAL PRIORITIES",
        "ACTIVE CORRECTION RULES",
        "ACTIVE SKILL CANDIDATES",
    ):
        section_deltas = grouped.get(section)
        if not section_deltas:
            continue
        lines.append(f"{section}:")
        for delta in section_deltas:
            lines.append(f"- {delta.behavior_change.strip()}")
        lines.append("")

    lines.append("DELTA EVIDENCE:")
    for delta in deltas:
        refs = "; ".join(f"{ref.source}:{ref.locator}" for ref in delta.evidence_refs)
        lines.append(f"- {delta.id}: {refs}")
    return lines


def _render_tool_preflight_sections(deltas: list[BehaviorDelta]) -> str:
    lines = [
        "TOOL BEHAVIOR-DELTA PREFLIGHT:",
        "- Advisory checklist for this tool call only.",
        "- Existing capability and approval gates remain authoritative.",
        "",
    ]
    lines.extend(_render_sections(deltas))
    return "\n".join(lines).strip()


def _match_reasons(delta: BehaviorDelta, request: BehaviorCompileRequest) -> tuple[str, ...]:
    trigger = delta.trigger
    haystack = " ".join(part for part in (request.objective, request.query or "") if part).lower()
    matched: list[str] = []
    if trigger.query_patterns and any(pattern.lower() in haystack for pattern in trigger.query_patterns):
        matched.append("matched_query_pattern")
    if request.task_type and request.task_type in trigger.task_types:
        matched.append("matched_task_type")
    if request.tool_names and set(request.tool_names).intersection(trigger.tool_names):
        matched.append("matched_tool_name")
    if request.memory_layers and set(request.memory_layers).intersection(trigger.memory_layers):
        matched.append("matched_memory_layer")
    if request.path and trigger.path_globs and any(fnmatch(request.path, glob) for glob in trigger.path_globs):
        matched.append("matched_path_glob")
    if trigger.risk_tags and any(tag.lower() in haystack for tag in trigger.risk_tags):
        matched.append("matched_risk_tag")
    return tuple(matched)


def _matches(delta: BehaviorDelta, request: BehaviorCompileRequest) -> bool:
    return bool(_match_reasons(delta, request))


def _tool_call_match_reasons(delta: BehaviorDelta, context: ToolPreflightContext) -> tuple[str, ...]:
    trigger = delta.trigger
    matched: list[str] = []
    if trigger.tool_names and context.tool_name in trigger.tool_names:
        matched.append(f"matched_tool_name:{context.tool_name}")
    if trigger.path_globs:
        for path in context.touched_paths:
            if any(fnmatch(path, glob) for glob in trigger.path_globs):
                matched.append("matched_path_glob")
                break
    if trigger.memory_layers and set(context.memory_layers).intersection(trigger.memory_layers):
        matched.append("matched_memory_layer")
    if trigger.risk_tags and _normalized_intersection(context.risk_tags, trigger.risk_tags):
        matched.append("matched_risk_tag")
    if context.task_type and context.task_type in trigger.task_types:
        matched.append("matched_task_type")
    haystack = context.objective.lower()
    if trigger.query_patterns and any(pattern.lower() in haystack for pattern in trigger.query_patterns):
        matched.append("matched_query_pattern")
    return tuple(matched)


def _normalized_intersection(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return bool({item.lower() for item in left}.intersection(item.lower() for item in right))


def _select_tool_matches(
    matches: list[tuple[BehaviorDelta, tuple[str, ...]]],
    *,
    max_deltas: int,
) -> list[tuple[BehaviorDelta, tuple[str, ...]]]:
    selected: list[tuple[BehaviorDelta, tuple[str, ...]]] = []
    seen_changes: set[str] = set()
    for delta, reasons in sorted(matches, key=lambda item: _priority_key(item[0])):
        normalized = " ".join(delta.behavior_change.split()).lower()
        if normalized in seen_changes:
            continue
        seen_changes.add(normalized)
        selected.append((delta, reasons))
        if len(selected) >= max_deltas:
            break
    return selected


def _priority_key(delta: BehaviorDelta) -> tuple[int, float, float, str]:
    return (_layer_priority(delta.target_layer), -delta.importance, -delta.confidence, delta.id)


def _layer_priority(layer: MemoryLayer) -> int:
    order = {
        MemoryLayer.POLICY: 0,
        MemoryLayer.SELF: 1,
        MemoryLayer.PROCEDURAL: 2,
        MemoryLayer.SEMANTIC: 3,
        MemoryLayer.EPISODIC: 4,
        MemoryLayer.WORKING: 5,
    }
    return order[layer]


def _section_for(delta: BehaviorDelta) -> str:
    if delta.kind in {BehaviorDeltaKind.POLICY, BehaviorDeltaKind.APPROVAL_GATE_RULE}:
        return "ACTIVE POLICY CONSTRAINTS"
    if delta.kind == BehaviorDeltaKind.SELF_MODEL_RULE:
        return "ACTIVE SELF MODEL RULES"
    if delta.kind == BehaviorDeltaKind.TOOL_HEURISTIC:
        return "ACTIVE TOOL HEURISTICS"
    if delta.kind == BehaviorDeltaKind.CONTEXT_PACKING_RULE:
        return "ACTIVE CONTEXT-PACKING RULES"
    if delta.kind == BehaviorDeltaKind.RETRIEVAL_PRIOR:
        return "ACTIVE RETRIEVAL PRIORITIES"
    if delta.kind == BehaviorDeltaKind.CORRECTION_RULE:
        return "ACTIVE CORRECTION RULES"
    if delta.kind == BehaviorDeltaKind.SKILL_CANDIDATE:
        return "ACTIVE SKILL CANDIDATES"
    return "ACTIVE PROCEDURES"


def _activation_reason(delta: BehaviorDelta, request: BehaviorCompileRequest) -> str:
    return ",".join(_match_reasons(delta, request))


def _activation_id(delta_id: str, run_id: str | None, task_id: str | None, section: str) -> str:
    raw = f"act_{delta_id}_{run_id or 'no_run'}_{task_id or 'no_task'}_{section.lower()}"
    return sub(r"[^a-zA-Z0-9_]+", "_", raw)[:240]


def _tool_activation_id(delta_id: str, context: ToolPreflightContext, section: str) -> str:
    tool_call_key = context.tool_call_id or f"{context.tool_name}_{_arguments_hash(context.tool_arguments)}"
    raw = (
        f"act_{delta_id}_{context.run_id or 'no_run'}_{context.task_id or 'no_task'}_"
        f"{tool_call_key}_{section.lower()}"
    )
    return sub(r"[^a-zA-Z0-9_]+", "_", raw)[:240]


def _arguments_hash(arguments: dict[str, object]) -> str:
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
