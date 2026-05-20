from __future__ import annotations

import pytest

from nested_memvid_agent.behavior_delta import (
    ActivationStats,
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    RollbackPlan,
    TriggerSpec,
    ValidationPlan,
    behavior_delta_from_metadata,
    behavior_delta_to_memory_metadata,
    memory_kind_for_behavior_delta,
)
from nested_memvid_agent.models import EvidenceRef, MemoryKind, MemoryLayer


def _evidence() -> tuple[EvidenceRef, ...]:
    return (EvidenceRef(source="task_capsule", locator="run-123:lesson-1", quote="Use approval gates."),)


def test_behavior_delta_round_trip_serialization_preserves_fields() -> None:
    delta = BehaviorDelta(
        id="delta_policy_gate_check",
        title="Policy gate check",
        kind=BehaviorDeltaKind.POLICY,
        target_layer=MemoryLayer.POLICY,
        risk=BehaviorDeltaRisk.HIGH,
        status=BehaviorDeltaStatus.STAGED,
        trigger=TriggerSpec(
            query_patterns=("policy", "approval"),
            task_types=("repo_modification",),
            tool_names=("memory.learn",),
            memory_layers=(MemoryLayer.POLICY,),
            path_globs=("src/nested_memvid_agent/*.py",),
            risk_tags=("policy_write",),
            semantic_hint="Policy memory or approval-gate changes.",
        ),
        behavior_change="When modifying policy memory, require approval-gate tests first.",
        evidence_refs=_evidence(),
        validation_plan=ValidationPlan(
            required_checks=("approval_gate_tests",),
            replay_scenarios=("policy_write_requires_approval",),
            requires_human_approval=True,
            requires_exact_call_approval=True,
            min_validation_score=0.97,
            min_repeat_count=2,
        ),
        rollback_plan=RollbackPlan(
            can_disable=True,
            rollback_notes="Disable this policy delta and preserve the audit record.",
            tombstone_memory_record_id="mem_old",
            restore_delta_id="delta_previous",
        ),
        activation_stats=ActivationStats(
            activation_count=3,
            success_count=2,
            failure_count=1,
            correction_count=0,
            last_activated_at="2026-05-19T00:00:00+00:00",
        ),
        confidence=0.88,
        importance=0.91,
        created_from_run_id="run-123",
        created_at="2026-05-19T00:00:00+00:00",
        updated_at="2026-05-19T01:00:00+00:00",
        expires_at="2026-06-19T00:00:00+00:00",
        metadata={"source": "unit-test"},
    )

    payload = behavior_delta_to_memory_metadata(delta)
    restored = behavior_delta_from_metadata(payload["behavior_delta"])

    assert restored.id == delta.id
    assert restored.kind == delta.kind
    assert restored.target_layer == delta.target_layer
    assert restored.risk == delta.risk
    assert restored.status == delta.status
    assert restored.trigger == delta.trigger
    assert restored.behavior_change == delta.behavior_change
    assert restored.evidence_refs == delta.evidence_refs
    assert restored.validation_plan == delta.validation_plan
    assert restored.rollback_plan == delta.rollback_plan
    assert restored.activation_stats == delta.activation_stats
    assert restored.confidence == delta.confidence
    assert restored.importance == delta.importance
    assert restored.metadata["source"] == "unit-test"


def test_trigger_spec_serializes_memory_layers_as_strings() -> None:
    trigger = TriggerSpec(memory_layers=(MemoryLayer.SELF, MemoryLayer.POLICY))

    assert trigger.to_metadata()["memory_layers"] == ["self", "policy"]


def test_policy_deltas_map_to_policy_memory_kind() -> None:
    delta = BehaviorDelta(
        trigger=TriggerSpec(query_patterns=("policy",)),
        behavior_change="Preserve policy approval gates.",
        kind=BehaviorDeltaKind.POLICY,
        target_layer=MemoryLayer.POLICY,
        evidence_refs=_evidence(),
        risk=BehaviorDeltaRisk.HIGH,
        validation_plan=ValidationPlan(),
    )

    assert memory_kind_for_behavior_delta(delta) == MemoryKind.POLICY


@pytest.mark.parametrize(
    "kind",
    [
        BehaviorDeltaKind.PROCEDURE,
        BehaviorDeltaKind.TOOL_HEURISTIC,
        BehaviorDeltaKind.SKILL_CANDIDATE,
    ],
)
def test_procedure_like_deltas_map_to_procedure_memory_kind(kind: BehaviorDeltaKind) -> None:
    delta = BehaviorDelta(
        trigger=TriggerSpec(query_patterns=("pytest",)),
        behavior_change="Run targeted validation before full suite.",
        kind=kind,
        target_layer=MemoryLayer.PROCEDURAL,
        evidence_refs=_evidence(),
        risk=BehaviorDeltaRisk.MEDIUM,
        validation_plan=ValidationPlan(),
    )

    assert memory_kind_for_behavior_delta(delta) == MemoryKind.PROCEDURE


def test_self_model_delta_maps_to_fact_on_self_layer() -> None:
    delta = BehaviorDelta(
        trigger=TriggerSpec(task_types=("self_inspection",)),
        behavior_change="Prefer explicit self-profile evidence over guesses.",
        kind=BehaviorDeltaKind.SELF_MODEL_RULE,
        target_layer=MemoryLayer.SELF,
        evidence_refs=_evidence(),
        risk=BehaviorDeltaRisk.MEDIUM,
        validation_plan=ValidationPlan(),
    )

    assert memory_kind_for_behavior_delta(delta) == MemoryKind.FACT


def test_empty_behavior_change_raises_value_error() -> None:
    with pytest.raises(ValueError, match="behavior_change"):
        BehaviorDelta(
            trigger=TriggerSpec(query_patterns=("policy",)),
            behavior_change="   ",
            kind=BehaviorDeltaKind.POLICY,
            target_layer=MemoryLayer.POLICY,
            evidence_refs=_evidence(),
            risk=BehaviorDeltaRisk.HIGH,
            validation_plan=ValidationPlan(),
        )


def test_empty_evidence_refs_rejected_unless_proposed_draft() -> None:
    with pytest.raises(ValueError, match="evidence_refs"):
        BehaviorDelta(
            trigger=TriggerSpec(query_patterns=("policy",)),
            behavior_change="Require evidence before policy promotion.",
            kind=BehaviorDeltaKind.POLICY,
            target_layer=MemoryLayer.POLICY,
            evidence_refs=(),
            risk=BehaviorDeltaRisk.HIGH,
            validation_plan=ValidationPlan(),
        )

    draft = BehaviorDelta(
        trigger=TriggerSpec(query_patterns=("policy",)),
        behavior_change="Draft proposal awaiting evidence attachment.",
        kind=BehaviorDeltaKind.POLICY,
        target_layer=MemoryLayer.POLICY,
        evidence_refs=(),
        risk=BehaviorDeltaRisk.HIGH,
        validation_plan=ValidationPlan(),
        status=BehaviorDeltaStatus.PROPOSED,
        metadata={"draft": True},
    )

    assert draft.evidence_refs == ()


def test_defaults_are_safe() -> None:
    delta = BehaviorDelta(
        trigger=TriggerSpec(query_patterns=("repair",)),
        behavior_change="Check prior repair lessons before repeating validation.",
        kind=BehaviorDeltaKind.TOOL_HEURISTIC,
        target_layer=MemoryLayer.PROCEDURAL,
        evidence_refs=_evidence(),
        risk=BehaviorDeltaRisk.LOW,
        validation_plan=ValidationPlan(),
    )

    assert delta.status == BehaviorDeltaStatus.PROPOSED
    assert delta.rollback_plan.can_disable is True
    assert delta.activation_stats.activation_count == 0
