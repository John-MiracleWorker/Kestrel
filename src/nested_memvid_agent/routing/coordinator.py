from __future__ import annotations

from dataclasses import dataclass, replace

from ..config import AgentConfig
from .contracts import TaskLike
from .ledger import RoutingLedger, stable_decision_id, stable_outcome_id
from .ledger_records import (
    RouteDecisionEntry,
    RouteOutcomeEntry,
    RoutingRevisionConflict,
)
from .models import PrivacyClass, RouteDecision, RoutingMode
from .router import ReviewDiversityContext, RoutingUnavailableError
from .service import AdaptiveFlockRoutingService, RoutingAssignment


class RoutingLeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class DurableRoutingAssignment:
    assignment: RoutingAssignment
    record: RouteDecisionEntry
    reused: bool


class DurableRoutingCoordinator:
    """Bind deterministic Adaptive Flock decisions to durable task attempts."""

    def __init__(
        self,
        ledger: RoutingLedger,
        *,
        policy_id: str = "balanced",
        mode: RoutingMode = "shadow",
    ) -> None:
        if mode == "off":
            raise ValueError(
                "DurableRoutingCoordinator requires shadow, constrained, or adaptive mode"
            )
        self.ledger = ledger
        self.policy_id = policy_id
        self.mode = mode

    def assign(
        self,
        base_config: AgentConfig,
        task: TaskLike,
        *,
        subagent_id: str | None,
        attempt: int,
        planner_guidance: dict[str, object] | None = None,
        default_privacy_class: PrivacyClass = "approved_cloud",
        local_required: bool = False,
        maximum_cost_usd: float | None = None,
        direct_target_id: str | None = None,
        review_context: ReviewDiversityContext | None = None,
    ) -> DurableRoutingAssignment:
        if isinstance(attempt, bool) or attempt < 1:
            raise ValueError("route attempt must be a positive integer")
        policy_entry = self.ledger.get_policy(self.policy_id)
        if policy_entry is None or not policy_entry.enabled:
            raise RoutingUnavailableError(
                f"route policy is unavailable: {self.policy_id}",
                reason_codes=("route_policy_unavailable",),
            )
        service = AdaptiveFlockRoutingService(
            profiles=[entry.profile for entry in self.ledger.list_provider_profiles()],
            targets=[entry.target for entry in self.ledger.list_model_targets()],
            policy=policy_entry.policy,
            mode=self.mode,
        )
        existing = self.ledger.get_attempt_decision(
            run_id=task.run_id,
            task_id=task.task_id,
            subagent_id=subagent_id,
            attempt=attempt,
        )
        if existing is not None:
            return self._reuse_assignment(
                service,
                base_config,
                task,
                existing,
                planner_guidance=planner_guidance,
                default_privacy_class=default_privacy_class,
                local_required=local_required,
                maximum_cost_usd=maximum_cost_usd,
                review_context=review_context,
            )

        assignment = service.assign(
            base_config,
            task,
            planner_guidance=planner_guidance,
            default_privacy_class=default_privacy_class,
            local_required=local_required,
            maximum_cost_usd=maximum_cost_usd,
            direct_target_id=direct_target_id,
            review_context=review_context,
        )
        decision_id = stable_decision_id(
            run_id=task.run_id,
            task_id=task.task_id,
            subagent_id=subagent_id,
            attempt=attempt,
            contract_digest=assignment.contract.digest,
            policy_id=self.policy_id,
        )
        record = self.ledger.record_decision(
            decision_id=decision_id,
            run_id=task.run_id,
            task_id=task.task_id,
            subagent_id=subagent_id,
            attempt=attempt,
            decision=assignment.decision,
            policy_revision=policy_entry.revision,
        )
        return DurableRoutingAssignment(assignment=assignment, record=record, reused=False)

    def mark_started(self, durable: DurableRoutingAssignment) -> RouteDecisionEntry:
        return self.ledger.mark_decision_started(durable.record.decision_id)

    def record_outcome(
        self,
        durable: DurableRoutingAssignment,
        *,
        execution_status: str,
        validation_passed: bool,
        validation_codes: tuple[str, ...] = (),
        failure_category: str | None = None,
        provider_failure_code: str | None = None,
        latency_seconds: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        actual_cost_usd: float | None = None,
        tool_count: int = 0,
        changed_file_count: int | None = None,
        retry_count: int = 0,
        escalated: bool = False,
        reward_components: dict[str, float] | None = None,
        outcome_labels: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
    ) -> RouteOutcomeEntry:
        return self.ledger.record_outcome(
            outcome_id=stable_outcome_id(durable.record.decision_id),
            decision_id=durable.record.decision_id,
            execution_status=execution_status,
            validation_passed=validation_passed,
            validation_codes=validation_codes,
            failure_category=failure_category,
            provider_failure_code=provider_failure_code,
            latency_seconds=latency_seconds,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            actual_cost_usd=actual_cost_usd,
            tool_count=tool_count,
            changed_file_count=changed_file_count,
            retry_count=retry_count,
            escalated=escalated,
            reward_components=reward_components,
            outcome_labels=outcome_labels,
            evidence_refs=evidence_refs,
        )

    def _reuse_assignment(
        self,
        service: AdaptiveFlockRoutingService,
        base_config: AgentConfig,
        task: TaskLike,
        existing: RouteDecisionEntry,
        *,
        planner_guidance: dict[str, object] | None,
        default_privacy_class: PrivacyClass,
        local_required: bool,
        maximum_cost_usd: float | None,
        review_context: ReviewDiversityContext | None,
    ) -> DurableRoutingAssignment:
        target_entry = self.ledger.get_model_target(existing.selected_target_id)
        profile_entry = self.ledger.get_provider_profile(existing.selected_profile_id)
        policy_entry = self.ledger.get_policy(existing.policy_id)
        if target_entry is None or profile_entry is None or policy_entry is None:
            raise RoutingLeaseConflict("route lease references deleted routing inventory")
        if target_entry.revision != existing.selected_target_revision:
            raise RoutingRevisionConflict(
                "model_target", existing.selected_target_id, target_entry.revision
            )
        if profile_entry.revision != existing.selected_profile_revision:
            raise RoutingRevisionConflict(
                "provider_profile", existing.selected_profile_id, profile_entry.revision
            )
        if policy_entry.revision != existing.policy_revision:
            raise RoutingRevisionConflict("route_policy", existing.policy_id, policy_entry.revision)
        if existing.mode != self.mode:
            raise RoutingLeaseConflict("route lease mode does not match coordinator mode")

        fresh = service.assign(
            base_config,
            task,
            planner_guidance=planner_guidance,
            default_privacy_class=default_privacy_class,
            local_required=local_required,
            maximum_cost_usd=maximum_cost_usd,
            direct_target_id=existing.selected_target_id,
            review_context=review_context,
        )
        if fresh.contract.digest != existing.contract_digest:
            raise RoutingLeaseConflict("route task contract changed after decision persistence")
        leased_decision: RouteDecision = replace(
            fresh.decision,
            selection_kind=existing.selection_kind,
            score=existing.score,
            reason_codes=existing.reason_codes,
            actionable=existing.actionable,
        )
        leased_assignment = RoutingAssignment(
            contract=fresh.contract,
            decision=leased_decision,
            config=(
                service.apply_decision(base_config, leased_decision)
                if existing.actionable
                else base_config
            ),
            executes_selected_target=existing.actionable,
        )
        return DurableRoutingAssignment(
            assignment=leased_assignment,
            record=existing,
            reused=True,
        )
