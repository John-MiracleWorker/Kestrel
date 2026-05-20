from __future__ import annotations

from dataclasses import dataclass
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
class CompiledBehaviorDeltas:
    text: str
    deltas: tuple[BehaviorDelta, ...]


class BehaviorCompiler:
    """Compile active behavior deltas into bounded runtime instructions.

    This compiler is intentionally separate from ContextPacker/ContextCompiler.
    It only renders active, relevant, evidence-backed deltas supplied by the
    ledger and can be disabled to produce an empty output.
    """

    def __init__(self, *, ledger: BehaviorDeltaLedger, config: BehaviorCompilerConfig | None = None) -> None:
        self.ledger = ledger
        self.config = config or BehaviorCompilerConfig()

    def compile(self, request: BehaviorCompileRequest) -> CompiledBehaviorDeltas:
        if not self.config.enabled:
            return CompiledBehaviorDeltas(text="", deltas=())

        candidates = self.ledger.list_deltas(status=BehaviorDeltaStatus.ACTIVE)
        relevant = [delta for delta in candidates if delta.evidence_refs and _matches(delta, request)]
        selected = self._select(relevant)
        if not selected:
            return CompiledBehaviorDeltas(text="", deltas=())

        sections = _render_sections(selected)
        text = "\n".join(sections).strip()
        if self.config.log_activations:
            self._record_activations(selected, request)
        return CompiledBehaviorDeltas(text=text, deltas=tuple(selected))

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


def _matches(delta: BehaviorDelta, request: BehaviorCompileRequest) -> bool:
    trigger = delta.trigger
    haystack = " ".join(part for part in (request.objective, request.query or "") if part).lower()
    if trigger.query_patterns and any(pattern.lower() in haystack for pattern in trigger.query_patterns):
        return True
    if request.task_type and request.task_type in trigger.task_types:
        return True
    if request.tool_names and set(request.tool_names).intersection(trigger.tool_names):
        return True
    if request.memory_layers and set(request.memory_layers).intersection(trigger.memory_layers):
        return True
    if request.path and trigger.path_globs and any(fnmatch(request.path, glob) for glob in trigger.path_globs):
        return True
    if trigger.risk_tags and any(tag.lower() in haystack for tag in trigger.risk_tags):
        return True
    return False


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
    matched = []
    if delta.trigger.query_patterns:
        matched.append("query_patterns")
    if request.task_type and request.task_type in delta.trigger.task_types:
        matched.append("task_type")
    if request.tool_names and set(request.tool_names).intersection(delta.trigger.tool_names):
        matched.append("tool_names")
    if request.memory_layers and set(request.memory_layers).intersection(delta.trigger.memory_layers):
        matched.append("memory_layers")
    return "matched " + ",".join(matched or ["semantic_context"])


def _activation_id(delta_id: str, run_id: str | None, task_id: str | None, section: str) -> str:
    raw = f"act_{delta_id}_{run_id or 'no_run'}_{task_id or 'no_task'}_{section.lower()}"
    return sub(r"[^a-zA-Z0-9_]+", "_", raw)[:240]
