from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    RollbackPlan,
    TriggerSpec,
    ValidationPlan,
)
from .behavior_delta_ledger import BehaviorDeltaLedger
from .models import EvidenceRef, MemoryKind, MemoryLayer
from .nested_learning import LearningSignal


@dataclass(frozen=True)
class BehaviorDeltaExtractionResult:
    proposals: tuple[BehaviorDelta, ...]
    rejected: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "proposal_count": len(self.proposals),
            "rejected_count": len(self.rejected),
            "proposals": [delta.to_metadata() for delta in self.proposals],
            "rejected": list(self.rejected),
        }


class BehaviorDeltaExtractor:
    """Extract behavior-delta proposals from evidence without activating them."""

    def __init__(self, *, ledger: BehaviorDeltaLedger | None = None) -> None:
        self.ledger = ledger

    def propose_from_capsule(
        self,
        capsule: dict[str, object],
        *,
        run_id: str,
        dry_run: bool = True,
    ) -> list[BehaviorDelta]:
        deltas = extract_behavior_deltas_from_capsule(capsule, run_id=run_id)
        if not dry_run:
            if self.ledger is None:
                raise ValueError("A BehaviorDeltaLedger is required when dry_run is false")
            for delta in deltas:
                self.ledger.record_delta(delta)
        return deltas

    def propose_from_signals(
        self,
        signals: tuple[LearningSignal, ...],
        *,
        run_id: str,
        dry_run: bool = True,
    ) -> list[BehaviorDelta]:
        deltas = extract_behavior_deltas_from_signals(signals, run_id=run_id)
        if not dry_run:
            if self.ledger is None:
                raise ValueError("A BehaviorDeltaLedger is required when dry_run is false")
            for delta in deltas:
                self.ledger.record_delta(delta)
        return deltas


def extract_behavior_deltas_from_capsule(capsule: dict[str, object], *, run_id: str) -> list[BehaviorDelta]:
    """Extract proposal-only behavior deltas from a run capsule payload."""

    proposals: list[BehaviorDelta] = []
    proposals.extend(_deltas_from_capsule_items(capsule, run_id=run_id))
    proposals.extend(_tool_retry_deltas(capsule, run_id=run_id))
    return _dedupe_deltas(proposals)


def extract_behavior_deltas_from_signals(signals: tuple[LearningSignal, ...], *, run_id: str) -> list[BehaviorDelta]:
    proposals: list[BehaviorDelta] = []
    for index, signal in enumerate(signals, start=1):
        section = _section_for_signal(signal)
        if section is None:
            continue
        delta = _delta_from_text(
            str(signal.content),
            run_id=run_id,
            section=section,
            index=index,
            objective=str(signal.title),
            explicit_instruction=signal.explicit_instruction,
            validation_score=signal.computed_validation_score,
            repeat_count=signal.repeat_count,
        )
        if delta is not None:
            proposals.append(delta)
    return _dedupe_deltas(proposals)


def _deltas_from_capsule_items(capsule: dict[str, object], *, run_id: str) -> list[BehaviorDelta]:
    objective = str(capsule.get("objective", ""))
    proposals: list[BehaviorDelta] = []
    for section in (
        "candidate_policy_items",
        "candidate_procedures",
        "candidate_corrections",
        "reusable_lessons",
    ):
        for index, content in enumerate(_string_list(capsule.get(section)), start=1):
            delta = _delta_from_text(
                content,
                run_id=run_id,
                section=section,
                index=index,
                objective=objective,
                explicit_instruction=section == "candidate_policy_items",
            )
            if delta is not None:
                proposals.append(delta)
    return proposals


def _delta_from_text(
    content: str,
    *,
    run_id: str,
    section: str,
    index: int,
    objective: str,
    explicit_instruction: bool = False,
    validation_score: float | None = None,
    repeat_count: int | None = None,
) -> BehaviorDelta | None:
    content = " ".join(content.split())
    if not _is_specific_candidate(content):
        return None
    kind = _kind_for_section(section, content)
    target_layer = _target_layer_for_kind(kind)
    risk = _risk_for_kind(kind, content)
    validation_plan = _validation_plan_for(kind, risk, validation_score=validation_score, repeat_count=repeat_count)
    locator = f"{run_id}:{section}:{index}"
    evidence = (EvidenceRef(source="task_capsule", locator=locator, quote=content[:500]),)
    return BehaviorDelta(
        id=_delta_id(run_id, section, index, content),
        title=_title_for(kind, content),
        trigger=_trigger_for(content, kind=kind, target_layer=target_layer, objective=objective),
        behavior_change=_behavior_change_for(content, kind=kind),
        kind=kind,
        target_layer=target_layer,
        evidence_refs=evidence,
        risk=risk,
        validation_plan=validation_plan,
        rollback_plan=RollbackPlan(can_disable=True),
        status=BehaviorDeltaStatus.PROPOSED,
        confidence=0.74 if kind != BehaviorDeltaKind.POLICY else 0.82,
        importance=0.78 if kind != BehaviorDeltaKind.POLICY else 0.9,
        created_from_run_id=run_id,
        metadata={
            "extraction_source": section,
            "explicit_instruction": explicit_instruction,
        },
    )


def _tool_retry_deltas(capsule: dict[str, object], *, run_id: str) -> list[BehaviorDelta]:
    tool_calls = capsule.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for item in tool_calls:
        if not isinstance(item, dict):
            continue
        if bool(item.get("success", True)):
            continue
        tool_name = str(item.get("tool", "")).strip()
        if not tool_name:
            continue
        args_fingerprint = json.dumps(item.get("arguments", {}), sort_keys=True, default=str)
        grouped[(tool_name, args_fingerprint)].append(item)
    proposals: list[BehaviorDelta] = []
    for index, ((tool_name, args_fingerprint), attempts) in enumerate(grouped.items(), start=1):
        if len(attempts) < 2:
            continue
        evidence = (
            EvidenceRef(
                source="task_capsule",
                locator=f"{run_id}:tool_calls:{tool_name}:{index}",
                quote=f"{len(attempts)} failed attempts with unchanged arguments {args_fingerprint[:240]}",
            ),
        )
        proposals.append(
            BehaviorDelta(
                id=_delta_id(run_id, "tool_retry", index, f"{tool_name}:{args_fingerprint}"),
                title=f"Block unchanged retries for {tool_name}",
                trigger=TriggerSpec(
                    query_patterns=("retry", "failure", "validation", tool_name),
                    task_types=("debugging", "repair", "validation"),
                    tool_names=(tool_name,),
                    memory_layers=(MemoryLayer.PROCEDURAL,),
                    risk_tags=("repeated_failure",),
                    semantic_hint="Repeated failed tool calls with identical arguments.",
                ),
                behavior_change=(
                    f"When {tool_name} fails with a given argument set, block unchanged retries "
                    "until a changed strategy or changed arguments are supplied."
                ),
                kind=BehaviorDeltaKind.TOOL_HEURISTIC,
                target_layer=MemoryLayer.PROCEDURAL,
                evidence_refs=evidence,
                risk=BehaviorDeltaRisk.MEDIUM,
                validation_plan=ValidationPlan(
                    required_checks=("retry_strategy_changed",),
                    replay_scenarios=("repeated_tool_failure_requires_changed_strategy",),
                    min_validation_score=0.75,
                    min_repeat_count=len(attempts),
                ),
                rollback_plan=RollbackPlan(can_disable=True),
                status=BehaviorDeltaStatus.PROPOSED,
                confidence=0.76,
                importance=0.8,
                created_from_run_id=run_id,
                metadata={
                    "extraction_source": "tool_calls",
                    "tool_name": tool_name,
                    "repeat_count": len(attempts),
                },
            )
        )
    return proposals


def _section_for_signal(signal: LearningSignal) -> str | None:
    metadata = signal.metadata or {}
    capsule_signal = str(metadata.get("capsule_signal", ""))
    if capsule_signal == "policy_candidate" or signal.kind == MemoryKind.POLICY:
        return "candidate_policy_items"
    if capsule_signal == "procedure" or signal.kind == MemoryKind.PROCEDURE:
        return "candidate_procedures"
    if capsule_signal == "correction" or signal.kind == MemoryKind.CORRECTION:
        return "candidate_corrections"
    if capsule_signal == "lesson":
        return "reusable_lessons"
    return None


def _kind_for_section(section: str, content: str) -> BehaviorDeltaKind:
    lowered = content.lower()
    if section == "candidate_policy_items":
        if "approval" in lowered or "gate" in lowered:
            return BehaviorDeltaKind.APPROVAL_GATE_RULE
        return BehaviorDeltaKind.POLICY
    if section == "candidate_corrections":
        return BehaviorDeltaKind.CORRECTION_RULE
    if section == "candidate_procedures":
        return BehaviorDeltaKind.PROCEDURE
    if "tool" in lowered or "command" in lowered or "retry" in lowered:
        return BehaviorDeltaKind.TOOL_HEURISTIC
    return BehaviorDeltaKind.PROCEDURE


def _target_layer_for_kind(kind: BehaviorDeltaKind) -> MemoryLayer:
    if kind in {BehaviorDeltaKind.POLICY, BehaviorDeltaKind.APPROVAL_GATE_RULE}:
        return MemoryLayer.POLICY
    if kind == BehaviorDeltaKind.SELF_MODEL_RULE:
        return MemoryLayer.SELF
    return MemoryLayer.PROCEDURAL


def _risk_for_kind(kind: BehaviorDeltaKind, content: str) -> BehaviorDeltaRisk:
    lowered = content.lower()
    if kind in {BehaviorDeltaKind.POLICY, BehaviorDeltaKind.APPROVAL_GATE_RULE}:
        return BehaviorDeltaRisk.HIGH
    if any(
        term in lowered
        for term in ("policy", "approval", "gate", "commit", "repair", "shell", "write", "validation", "retry", "command")
    ):
        return BehaviorDeltaRisk.MEDIUM
    return BehaviorDeltaRisk.LOW


def _validation_plan_for(
    kind: BehaviorDeltaKind,
    risk: BehaviorDeltaRisk,
    *,
    validation_score: float | None,
    repeat_count: int | None,
) -> ValidationPlan:
    del validation_score
    if kind in {BehaviorDeltaKind.POLICY, BehaviorDeltaKind.APPROVAL_GATE_RULE}:
        return ValidationPlan(
            required_checks=("explicit_instruction_check", "policy_write_gate_check"),
            replay_scenarios=("policy_delta_requires_approval",),
            requires_human_approval=True,
            requires_exact_call_approval=True,
            min_validation_score=0.97,
            min_repeat_count=max(1, repeat_count or 1),
        )
    if risk == BehaviorDeltaRisk.MEDIUM:
        return ValidationPlan(
            required_checks=("behavior_delta_replay",),
            replay_scenarios=("procedural_delta_improves_replay",),
            min_validation_score=0.75,
            min_repeat_count=max(2, repeat_count or 1),
        )
    return ValidationPlan(required_checks=("behavior_delta_review",), min_validation_score=0.6)


def _trigger_for(content: str, *, kind: BehaviorDeltaKind, target_layer: MemoryLayer, objective: str) -> TriggerSpec:
    keywords = _keywords(content)
    task_types = ["memory_design"] if kind == BehaviorDeltaKind.POLICY else ["debugging", "repair"]
    if "validation" in keywords:
        task_types.append("validation")
    return TriggerSpec(
        query_patterns=tuple(keywords[:8]),
        task_types=tuple(dict.fromkeys(task_types)),
        memory_layers=(target_layer,),
        risk_tags=(_risk_tag_for(kind),),
        semantic_hint=objective or content[:160],
    )


def _behavior_change_for(content: str, *, kind: BehaviorDeltaKind) -> str:
    if content.lower().startswith(("when ", "do ", "never ", "always ", "before ")):
        return content
    prefix = "When a future task matches this trigger"
    if kind == BehaviorDeltaKind.POLICY:
        prefix = "For future policy-relevant tasks"
    return f"{prefix}, apply this behavior: {content}"


def _is_specific_candidate(content: str) -> bool:
    lowered = content.lower().strip()
    if len(lowered) < 24:
        return False
    vague = {
        "be more careful next time.",
        "be more careful next time",
        "improve things.",
        "improve things",
        "do better next time.",
        "do better next time",
    }
    if lowered in vague:
        return False
    actionable_markers = (
        "when ",
        "before ",
        "after ",
        "require ",
        "do not ",
        "never ",
        "always ",
        "preserve ",
        "block ",
        "compare ",
        "inspect ",
        "mark ",
    )
    return any(marker in lowered for marker in actionable_markers)


def _keywords(content: str) -> list[str]:
    lowered = content.lower()
    keywords: list[str] = []
    if ".mv2" in lowered:
        keywords.append(".mv2")
    if "memvid" in lowered:
        keywords.append("memvid")
    tokens = re.findall(r"[a-z][a-z0-9_\-]{2,}", lowered)
    stop = {"when", "future", "task", "this", "that", "with", "before", "after", "from", "into", "next"}
    for token, _count in Counter(tokens).most_common():
        if token not in stop and token not in keywords:
            keywords.append(token)
        if len(keywords) >= 10:
            break
    return keywords or ["behavior"]


def _risk_tag_for(kind: BehaviorDeltaKind) -> str:
    if kind in {BehaviorDeltaKind.POLICY, BehaviorDeltaKind.APPROVAL_GATE_RULE}:
        return "policy_delta"
    if kind == BehaviorDeltaKind.TOOL_HEURISTIC:
        return "tool_heuristic"
    if kind == BehaviorDeltaKind.CORRECTION_RULE:
        return "correction_rule"
    return "procedure_delta"


def _title_for(kind: BehaviorDeltaKind, content: str) -> str:
    compact = " ".join(content.split())
    return f"{kind.value.replace('_', ' ').title()}: {compact[:72]}"


def _delta_id(run_id: str, section: str, index: int, content: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{run_id}_{section}_{index}_{content[:32].lower()}").strip("_")
    return f"delta_{slug[:96]}"


def _dedupe_deltas(deltas: list[BehaviorDelta]) -> list[BehaviorDelta]:
    seen: set[tuple[BehaviorDeltaKind, str]] = set()
    deduped: list[BehaviorDelta] = []
    for delta in deltas:
        key = (delta.kind, delta.behavior_change.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(delta)
    return deduped


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
