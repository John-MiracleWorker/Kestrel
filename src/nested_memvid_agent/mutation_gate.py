from __future__ import annotations

from dataclasses import dataclass

from .behavior_delta import BehaviorDelta, BehaviorDeltaKind, BehaviorDeltaRisk, BehaviorDeltaStatus
from .models import MemoryLayer


@dataclass(frozen=True)
class MutationGateEvidence:
    """Operator/replay evidence used to gate behavior-delta activation.

    This is control-plane evidence only. It does not activate or compile runtime
    behavior; it lets callers ask whether a proposed delta is safe to stage or
    activate under the conservative risk rules.
    """

    validation_score: float = 0.0
    repeat_count: int = 1
    explicit_instruction: bool = False
    reviewed_rule: bool = False
    replay_passed: bool = False
    auto_activate_low_risk_enabled: bool = False
    policy_delta_activation_enabled: bool = False
    critical_delta_activation_enabled: bool = False
    exact_call_approved: bool = False
    human_approved: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.validation_score <= 1.0:
            raise ValueError("validation_score must be between 0.0 and 1.0")
        if self.repeat_count < 0:
            raise ValueError("repeat_count must be >= 0")


@dataclass(frozen=True)
class MutationDecision:
    accepted: bool
    status: BehaviorDeltaStatus
    reason: str
    requires_replay: bool
    requires_human_approval: bool
    requires_exact_call_approval: bool
    blocked_by: tuple[str, ...] = ()


class MutationGate:
    """Conservative gate for turning behavior deltas into active behavior.

    The gate sits above the existing nested-learning promotion kernel. It never
    writes memory, compiles prompts, or changes runtime behavior by itself.
    """

    def evaluate(self, delta: BehaviorDelta, evidence: MutationGateEvidence) -> MutationDecision:
        blockers: list[str] = []
        plan = delta.validation_plan
        is_policy_delta = delta.kind in {BehaviorDeltaKind.POLICY, BehaviorDeltaKind.APPROVAL_GATE_RULE}
        requires_replay = bool(plan.replay_scenarios)
        requires_human_approval = plan.requires_human_approval or delta.risk in {
            BehaviorDeltaRisk.HIGH,
            BehaviorDeltaRisk.CRITICAL,
        }
        requires_exact_call_approval = plan.requires_exact_call_approval or is_policy_delta or delta.risk in {
            BehaviorDeltaRisk.HIGH,
            BehaviorDeltaRisk.CRITICAL,
        }

        if is_policy_delta and delta.target_layer != MemoryLayer.POLICY:
            return MutationDecision(
                accepted=False,
                status=BehaviorDeltaStatus.REJECTED,
                reason="Policy and approval-gate deltas must target the policy layer.",
                requires_replay=requires_replay,
                requires_human_approval=requires_human_approval,
                requires_exact_call_approval=requires_exact_call_approval,
                blocked_by=("policy_delta_target_layer_mismatch",),
            )

        if delta.risk == BehaviorDeltaRisk.CRITICAL and not evidence.critical_delta_activation_enabled:
            blockers.append("critical_delta_activation_disabled")

        if not delta.evidence_refs:
            blockers.append("missing_evidence_refs")
        if not delta.rollback_plan.can_disable:
            blockers.append("rollback_not_disableable")
        if evidence.validation_score < plan.min_validation_score:
            blockers.append("validation_score_below_threshold")
        if evidence.repeat_count < plan.min_repeat_count:
            blockers.append("repeat_count_below_threshold")
        if requires_replay and not evidence.replay_passed:
            blockers.append("replay_not_passed")

        if is_policy_delta:
            self._add_policy_blockers(delta, evidence, blockers)
        else:
            if requires_human_approval and not evidence.human_approved:
                blockers.append("human_approval_missing")
            if requires_exact_call_approval and not evidence.exact_call_approved:
                blockers.append("exact_call_approval_missing")

        if delta.risk == BehaviorDeltaRisk.LOW and blockers:
            return MutationDecision(
                accepted=True,
                status=BehaviorDeltaStatus.STAGED,
                reason="Low-risk behavior delta staged; activation is blocked until gate requirements pass.",
                requires_replay=requires_replay and "replay_not_passed" in blockers,
                requires_human_approval=requires_human_approval
                and (
                    "human_approval_missing" in blockers
                    or "missing_explicit_policy_instruction" in blockers
                ),
                requires_exact_call_approval=requires_exact_call_approval
                and "exact_call_approval_missing" in blockers,
                blocked_by=tuple(blockers),
            )

        if delta.risk == BehaviorDeltaRisk.LOW and not evidence.auto_activate_low_risk_enabled:
            return MutationDecision(
                accepted=True,
                status=BehaviorDeltaStatus.STAGED,
                reason="Low-risk behavior deltas are staged for review; activation is not automatic.",
                requires_replay=requires_replay and "replay_not_passed" in blockers,
                requires_human_approval=requires_human_approval and "human_approval_missing" in blockers,
                requires_exact_call_approval=requires_exact_call_approval
                and "exact_call_approval_missing" in blockers,
                blocked_by=tuple(blockers),
            )

        if delta.risk == BehaviorDeltaRisk.LOW:
            return MutationDecision(
                accepted=True,
                status=BehaviorDeltaStatus.ACTIVE,
                reason="Low-risk behavior delta satisfies autonomous activation requirements.",
                requires_replay=False,
                requires_human_approval=False,
                requires_exact_call_approval=False,
                blocked_by=(),
            )

        if blockers:
            return MutationDecision(
                accepted=True,
                status=BehaviorDeltaStatus.STAGED,
                reason="Behavior delta staged; activation is blocked until gate requirements pass.",
                requires_replay="replay_not_passed" in blockers,
                requires_human_approval=(
                    "human_approval_missing" in blockers or "missing_explicit_policy_instruction" in blockers
                ),
                requires_exact_call_approval="exact_call_approval_missing" in blockers,
                blocked_by=tuple(blockers),
            )

        return MutationDecision(
            accepted=True,
            status=BehaviorDeltaStatus.ACTIVE,
            reason="Behavior delta satisfies mutation-gate activation requirements.",
            requires_replay=False,
            requires_human_approval=False,
            requires_exact_call_approval=False,
            blocked_by=(),
        )

    def _add_policy_blockers(
        self,
        delta: BehaviorDelta,
        evidence: MutationGateEvidence,
        blockers: list[str],
    ) -> None:
        if delta.kind not in {BehaviorDeltaKind.POLICY, BehaviorDeltaKind.APPROVAL_GATE_RULE}:
            blockers.append("policy_delta_kind_mismatch")
        if delta.target_layer != MemoryLayer.POLICY:
            blockers.append("policy_delta_target_layer_mismatch")
        if not (evidence.explicit_instruction or evidence.reviewed_rule):
            blockers.append("missing_explicit_policy_instruction")
        if not evidence.policy_delta_activation_enabled:
            blockers.append("policy_delta_activation_disabled")
        if not evidence.exact_call_approved:
            blockers.append("exact_call_approval_missing")
        if (delta.validation_plan.requires_human_approval or delta.risk in {BehaviorDeltaRisk.HIGH, BehaviorDeltaRisk.CRITICAL}) and not evidence.human_approved:
            blockers.append("human_approval_missing")
