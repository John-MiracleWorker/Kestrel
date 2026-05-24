from __future__ import annotations

from nested_memvid_agent.behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    RollbackPlan,
    TriggerSpec,
    ValidationPlan,
)
from nested_memvid_agent.models import EvidenceRef, MemoryLayer
from nested_memvid_agent.mutation_gate import MutationGate, MutationGateEvidence


def _evidence_ref() -> tuple[EvidenceRef, ...]:
    return (EvidenceRef(source="task_capsule", locator="run-123:lesson-1", quote="validated lesson"),)


def _delta(
    *,
    kind: BehaviorDeltaKind = BehaviorDeltaKind.TOOL_HEURISTIC,
    risk: BehaviorDeltaRisk = BehaviorDeltaRisk.MEDIUM,
    target_layer: MemoryLayer = MemoryLayer.PROCEDURAL,
    validation_plan: ValidationPlan | None = None,
    rollback_plan: RollbackPlan | None = None,
    evidence_refs: tuple[EvidenceRef, ...] | None = None,
    metadata: dict[str, object] | None = None,
) -> BehaviorDelta:
    return BehaviorDelta(
        id="delta_test",
        title="Test delta",
        trigger=TriggerSpec(query_patterns=("validation",), task_types=("debugging",)),
        behavior_change="Before retrying validation, compare the previous command and require a changed strategy.",
        kind=kind,
        target_layer=target_layer,
        evidence_refs=_evidence_ref() if evidence_refs is None else evidence_refs,
        risk=risk,
        validation_plan=validation_plan
        or ValidationPlan(
            required_checks=("unit_tests",),
            replay_scenarios=("retry_requires_changed_strategy",),
            min_validation_score=0.75,
            min_repeat_count=2,
        ),
        rollback_plan=rollback_plan or RollbackPlan(can_disable=True),
        metadata=metadata or {},
    )


def test_low_risk_delta_stages_without_replay_requirement() -> None:
    delta = _delta(
        risk=BehaviorDeltaRisk.LOW,
        validation_plan=ValidationPlan(min_validation_score=0.0, min_repeat_count=1),
    )

    decision = MutationGate().evaluate(delta, MutationGateEvidence(validation_score=0.0, repeat_count=1))

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.STAGED
    assert decision.requires_replay is False
    assert decision.requires_human_approval is False
    assert decision.requires_exact_call_approval is False
    assert decision.blocked_by == ()


def test_low_risk_delta_can_auto_activate_when_enabled_and_validated() -> None:
    delta = _delta(
        risk=BehaviorDeltaRisk.LOW,
        validation_plan=ValidationPlan(
            required_checks=("behavior_delta_review",),
            replay_scenarios=("low_risk_replay",),
            min_validation_score=0.75,
            min_repeat_count=2,
        ),
    )

    decision = MutationGate().evaluate(
        delta,
        MutationGateEvidence(
            validation_score=0.9,
            repeat_count=2,
            replay_passed=True,
            auto_activate_low_risk_enabled=True,
        ),
    )

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.ACTIVE
    assert decision.requires_replay is False
    assert decision.requires_human_approval is False
    assert decision.requires_exact_call_approval is False
    assert decision.blocked_by == ()


def test_low_risk_policy_delta_does_not_auto_activate_without_policy_gates() -> None:
    delta = _delta(
        kind=BehaviorDeltaKind.POLICY,
        risk=BehaviorDeltaRisk.LOW,
        target_layer=MemoryLayer.POLICY,
        validation_plan=ValidationPlan(min_validation_score=0.75, min_repeat_count=1),
    )

    decision = MutationGate().evaluate(
        delta,
        MutationGateEvidence(
            validation_score=0.95,
            repeat_count=1,
            replay_passed=True,
            auto_activate_low_risk_enabled=True,
        ),
    )

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.STAGED
    assert "missing_explicit_policy_instruction" in decision.blocked_by
    assert "policy_delta_activation_disabled" in decision.blocked_by


def test_medium_risk_delta_stages_and_requires_replay_when_validation_is_incomplete() -> None:
    delta = _delta(risk=BehaviorDeltaRisk.MEDIUM)

    decision = MutationGate().evaluate(delta, MutationGateEvidence(validation_score=0.25, repeat_count=1))

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.STAGED
    assert decision.requires_replay is True
    assert "validation_score_below_threshold" in decision.blocked_by
    assert "repeat_count_below_threshold" in decision.blocked_by


def test_medium_risk_delta_can_activate_after_validation_and_replay_pass() -> None:
    delta = _delta(risk=BehaviorDeltaRisk.MEDIUM)

    decision = MutationGate().evaluate(
        delta,
        MutationGateEvidence(validation_score=0.82, repeat_count=2, replay_passed=True),
    )

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.ACTIVE
    assert decision.requires_replay is False
    assert decision.blocked_by == ()


def test_policy_delta_cannot_activate_without_explicit_instruction_config_replay_exact_approval() -> None:
    delta = _delta(
        kind=BehaviorDeltaKind.POLICY,
        risk=BehaviorDeltaRisk.HIGH,
        target_layer=MemoryLayer.POLICY,
        validation_plan=ValidationPlan(
            required_checks=("policy_write_gate_check",),
            replay_scenarios=("policy_write_requires_approval",),
            requires_human_approval=True,
            requires_exact_call_approval=True,
            min_validation_score=0.97,
            min_repeat_count=1,
        ),
    )

    decision = MutationGate().evaluate(delta, MutationGateEvidence(validation_score=1.0, repeat_count=1))

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.STAGED
    assert decision.requires_replay is True
    assert decision.requires_human_approval is True
    assert decision.requires_exact_call_approval is True
    assert "missing_explicit_policy_instruction" in decision.blocked_by
    assert "policy_delta_activation_disabled" in decision.blocked_by
    assert "replay_not_passed" in decision.blocked_by
    assert "exact_call_approval_missing" in decision.blocked_by


def test_policy_delta_can_activate_only_when_all_hard_gate_conditions_pass() -> None:
    delta = _delta(
        kind=BehaviorDeltaKind.POLICY,
        risk=BehaviorDeltaRisk.HIGH,
        target_layer=MemoryLayer.POLICY,
        validation_plan=ValidationPlan(
            required_checks=("policy_write_gate_check",),
            replay_scenarios=("policy_write_requires_approval",),
            requires_human_approval=True,
            requires_exact_call_approval=True,
            min_validation_score=0.97,
            min_repeat_count=1,
        ),
    )

    decision = MutationGate().evaluate(
        delta,
        MutationGateEvidence(
            validation_score=0.99,
            repeat_count=1,
            explicit_instruction=True,
            replay_passed=True,
            policy_delta_activation_enabled=True,
            exact_call_approved=True,
            human_approved=True,
        ),
    )

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.ACTIVE
    assert decision.blocked_by == ()


def test_approval_gate_rule_must_target_policy_layer() -> None:
    delta = _delta(
        kind=BehaviorDeltaKind.APPROVAL_GATE_RULE,
        risk=BehaviorDeltaRisk.HIGH,
        target_layer=MemoryLayer.PROCEDURAL,
        validation_plan=ValidationPlan(requires_exact_call_approval=True, min_validation_score=0.97),
    )

    decision = MutationGate().evaluate(
        delta,
        MutationGateEvidence(
            validation_score=1.0,
            repeat_count=1,
            explicit_instruction=True,
            replay_passed=True,
            policy_delta_activation_enabled=True,
            exact_call_approved=True,
            human_approved=True,
        ),
    )

    assert decision.accepted is False
    assert decision.status == BehaviorDeltaStatus.REJECTED
    assert "policy_delta_target_layer_mismatch" in decision.blocked_by


def test_critical_delta_is_recommendation_only_without_explicit_enablement() -> None:
    delta = _delta(
        kind=BehaviorDeltaKind.SELF_MODEL_RULE,
        risk=BehaviorDeltaRisk.CRITICAL,
        target_layer=MemoryLayer.SELF,
        validation_plan=ValidationPlan(min_validation_score=1.0, min_repeat_count=1),
    )

    decision = MutationGate().evaluate(
        delta,
        MutationGateEvidence(validation_score=1.0, repeat_count=1, replay_passed=True),
    )

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.STAGED
    assert "critical_delta_activation_disabled" in decision.blocked_by


def test_active_delta_requires_evidence_and_disableable_rollback() -> None:
    no_rollback = _delta(
        risk=BehaviorDeltaRisk.MEDIUM,
        rollback_plan=RollbackPlan(can_disable=False, rollback_notes="Manual restoration required."),
    )

    decision = MutationGate().evaluate(
        no_rollback,
        MutationGateEvidence(validation_score=1.0, repeat_count=2, replay_passed=True),
    )

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.STAGED
    assert "rollback_not_disableable" in decision.blocked_by

    draft_without_evidence = _delta(
        risk=BehaviorDeltaRisk.MEDIUM,
        evidence_refs=(),
        validation_plan=ValidationPlan(min_validation_score=1.0, min_repeat_count=1),
        metadata={"draft": True},
    )

    decision = MutationGate().evaluate(
        draft_without_evidence,
        MutationGateEvidence(validation_score=1.0, repeat_count=1, replay_passed=True),
    )

    assert decision.accepted is True
    assert decision.status == BehaviorDeltaStatus.STAGED
    assert "missing_evidence_refs" in decision.blocked_by
