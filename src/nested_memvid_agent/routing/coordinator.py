from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from ..config import AgentConfig
from ..lan_runtime_authority import LanRuntimeAuthorityResolver
from .activation_evaluator import ActivationEvaluation, ActivationEvaluator
from .contracts import TaskLike
from .learned_router import LearnedRouterConfig, build_route_examples, evaluate_shadow
from .ledger import (
    RoutingLedger,
    capability_scope_key,
    stable_decision_id,
    stable_outcome_id,
)
from .ledger_records import (
    RouteDecisionEntry,
    RouteOutcomeEntry,
    RoutingRevisionConflict,
    RoutingShadowDraft,
)
from .models import AgentTaskContract, PrivacyClass, RouteDecision, RoutingMode
from .qualification_evidence import PROVIDER_SIDE_FAILURE_CATEGORIES
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
        learned_config: LearnedRouterConfig | None = None,
        lan_runtime_authority_resolver: LanRuntimeAuthorityResolver | None = None,
        activation_evaluator: ActivationEvaluator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if mode == "off":
            raise ValueError(
                "DurableRoutingCoordinator requires shadow, constrained, or adaptive mode"
            )
        self.ledger = ledger
        self.policy_id = policy_id
        self.mode = mode
        self.learned_config = learned_config or LearnedRouterConfig()
        if lan_runtime_authority_resolver is not None and not callable(
            lan_runtime_authority_resolver
        ):
            raise TypeError("LAN runtime authority resolver must be callable")
        if activation_evaluator is not None and not isinstance(
            activation_evaluator, ActivationEvaluator
        ):
            raise TypeError("activation evaluator must be an ActivationEvaluator")
        if clock is not None and not callable(clock):
            raise TypeError("routing clock must be callable")
        self.lan_runtime_authority_resolver = lan_runtime_authority_resolver
        self.activation_evaluator = activation_evaluator
        self.clock = clock

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
        allowed_target_ids: Sequence[str] = (),
        forbidden_target_ids: Sequence[str] = (),
        allowed_provider_profiles: Sequence[str] = (),
        forbidden_provider_profiles: Sequence[str] = (),
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
            lan_runtime_authority_resolver=self.lan_runtime_authority_resolver,
            clock=self.clock,
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
                allowed_target_ids=allowed_target_ids,
                forbidden_target_ids=forbidden_target_ids,
                allowed_provider_profiles=allowed_provider_profiles,
                forbidden_provider_profiles=forbidden_provider_profiles,
                review_context=review_context,
            )

        (
            effective_direct_target_id,
            effective_forbidden_targets,
            escalation_reason,
            prior_target_id,
        ) = self._prior_failure_directive(
            task=task,
            subagent_id=subagent_id,
            attempt=attempt,
            direct_target_id=direct_target_id,
            forbidden_target_ids=forbidden_target_ids,
        )
        static_assignment = service.assign(
            base_config,
            task,
            planner_guidance=planner_guidance,
            default_privacy_class=default_privacy_class,
            local_required=local_required,
            maximum_cost_usd=maximum_cost_usd,
            allowed_target_ids=allowed_target_ids,
            forbidden_target_ids=effective_forbidden_targets,
            allowed_provider_profiles=allowed_provider_profiles,
            forbidden_provider_profiles=forbidden_provider_profiles,
            direct_target_id=effective_direct_target_id,
            review_context=review_context,
        )
        if escalation_reason == "capability_escalation":
            static_assignment = self._select_stronger_capability_target(
                service,
                base_config=base_config,
                task=task,
                current=static_assignment,
                previous_target_id=prior_target_id,
                planner_guidance=planner_guidance,
                default_privacy_class=default_privacy_class,
                local_required=local_required,
                maximum_cost_usd=maximum_cost_usd,
                allowed_target_ids=allowed_target_ids,
                forbidden_target_ids=effective_forbidden_targets,
                allowed_provider_profiles=allowed_provider_profiles,
                forbidden_provider_profiles=forbidden_provider_profiles,
                review_context=review_context,
            )
        if escalation_reason is not None:
            static_assignment = _annotate_assignment(
                service,
                base_config=base_config,
                assignment=static_assignment,
                selection_kind=escalation_reason,
                reason_code=escalation_reason,
            )
        assignment, shadow, authority = self._evaluate_learned_route(
            service,
            base_config=base_config,
            assignment=static_assignment,
            direct_target_pinned=effective_direct_target_id is not None,
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
            contract=assignment.contract,
            shadow=shadow,
            activation_grant_id=authority.grant_id if authority is not None else None,
            activation_receipt_id=authority.receipt_id if authority is not None else None,
            activation_effective=authority is not None and authority.effective,
            activation_reason=_activation_abstention_reason(authority),
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
        resolved_cost = (
            actual_cost_usd
            if actual_cost_usd is not None
            else _actual_cost_from_usage(
                durable.record,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        labels = list(outcome_labels)
        labels.append(
            "usage_attributed"
            if input_tokens is not None and output_tokens is not None
            else "usage_unavailable"
        )
        labels.append(
            "cost_attributed" if resolved_cost is not None else "cost_unavailable"
        )
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
            actual_cost_usd=resolved_cost,
            tool_count=tool_count,
            changed_file_count=changed_file_count,
            retry_count=retry_count,
            escalated=escalated,
            reward_components=reward_components,
            outcome_labels=tuple(dict.fromkeys(labels)),
            evidence_refs=evidence_refs,
        )

    def _evaluate_learned_route(
        self,
        service: AdaptiveFlockRoutingService,
        *,
        base_config: AgentConfig,
        assignment: RoutingAssignment,
        direct_target_pinned: bool = False,
    ) -> tuple[RoutingAssignment, RoutingShadowDraft, ActivationEvaluation | None]:
        contract = assignment.contract
        static_decision = assignment.decision
        static_target_id = static_decision.selected_target.target_id
        eligible_target_ids = tuple(
            sorted(
                candidate.target.target_id
                for candidate in static_decision.candidates
                if candidate.eligible
            )
        )
        all_target_ids = {target.target_id for target in service.targets}
        hard_filtered = frozenset(all_target_ids - set(eligible_target_ids))
        learned_config = replace(
            self.learned_config,
            hard_filtered_targets=hard_filtered,
        )
        project_id = self.ledger.state.get_run(contract.run_id).project_id
        capability_key = capability_scope_key(contract.required_capabilities)
        raw_outcomes = self.ledger.list_learning_outcomes(
            project_id=project_id,
            task_family=contract.task_family,
            risk=contract.risk,
            capability_key=capability_key,
            eligible_target_ids=eligible_target_ids,
        )
        evaluation = evaluate_shadow(
            examples=build_route_examples(raw_outcomes),
            static_target_id=static_target_id,
            config=learned_config,
        )
        learned_target_id = evaluation.learned_target_id
        activation_reason = evaluation.abstention_reason
        activate = False
        authority: ActivationEvaluation | None = None
        if self.mode == "shadow":
            activation_reason = activation_reason or "shadow_only"
        elif self.mode == "constrained":
            activation_reason = activation_reason or "constrained_shadow_only"
        elif contract.risk not in {"low", "medium"}:
            activation_reason = "high_risk"
        elif not learned_config.replay_gate_enabled:
            activation_reason = "replay_gate_not_enabled"
        elif learned_target_id == static_target_id:
            activation_reason = "learned_matches_static"
        elif direct_target_pinned:
            # An explicit direct-target pin (operator override or leased
            # qualification matrix target) is authoritative: the learned
            # layer may never substitute a different target for it.
            activation_reason = "direct_target_pinned"
        elif evaluation.should_activate and learned_target_id in eligible_target_ids:
            # Learned routing requires a durable effective grant.  The
            # environment flag is only a global permit inside the evaluator;
            # without a grant the decision stays static and the mission
            # continues on the deterministic route.
            authority = self._activation_authority(contract)
            if authority.effective:
                activate = True
            else:
                activation_reason = (
                    authority.reason_codes[0]
                    if authority.reason_codes
                    else "durable_grant_required"
                )

        effective_assignment = assignment
        if activate and learned_target_id is not None:
            target = next(
                item for item in service.targets if item.target_id == learned_target_id
            )
            candidate = next(
                item
                for item in static_decision.candidates
                if item.target.target_id == learned_target_id
            )
            learned_decision: RouteDecision = replace(
                static_decision,
                selected_target=target,
                selection_kind="learned_constrained",
                score=float(candidate.score or evaluation.learned_utility or 0.0),
                reason_codes=tuple(
                    dict.fromkeys(
                        (
                            *static_decision.reason_codes,
                            "learned_route_activated",
                            "verified_scope_evidence",
                            "hard_eligibility_preserved",
                        )
                    )
                ),
                actionable=True,
            )
            effective_assignment = RoutingAssignment(
                contract=contract,
                decision=learned_decision,
                config=service.apply_decision(base_config, learned_decision),
                executes_selected_target=True,
            )
            activation_reason = None

        actual_target_id = (
            effective_assignment.decision.selected_target.target_id
            if effective_assignment.executes_selected_target
            else None
        )
        actual_provider = (
            effective_assignment.decision.selected_target.provider
            if actual_target_id is not None
            else base_config.provider
        )
        actual_model = (
            effective_assignment.decision.selected_target.model
            if actual_target_id is not None
            else base_config.model
        )
        return effective_assignment, RoutingShadowDraft(
            project_id=project_id,
            task_family=contract.task_family,
            risk=contract.risk,
            capability_key=capability_key,
            static_target_id=static_target_id,
            learned_target_id=learned_target_id,
            actual_target_id=actual_target_id,
            actual_provider=actual_provider,
            actual_model=actual_model,
            evidence_count=evaluation.evidence_count,
            target_example_count=evaluation.target_example_count,
            cost_coverage=evaluation.cost_coverage,
            confidence=evaluation.confidence,
            static_utility=evaluation.static_utility,
            learned_utility=evaluation.learned_utility,
            utility_delta=evaluation.utility_improvement,
            estimated_savings_usd=evaluation.estimated_savings_usd,
            activated=activate,
            abstention_reason=activation_reason,
            config_digest=evaluation.config_digest,
        ), authority

    def _activation_authority(self, contract: AgentTaskContract) -> ActivationEvaluation:
        """Resolve durable grant authority for one new route decision.

        A missing evaluator (or any evaluation failure, including a malformed
        active grant) fails closed: learned routing is never authorized
        without a durable effective grant.
        """

        evaluator = self.activation_evaluator
        if evaluator is None:
            return ActivationEvaluation(
                effective=False,
                grant_id=None,
                receipt_id=None,
                reason_codes=("durable_grant_required",),
                learned_state=None,
            )
        try:
            return evaluator.evaluate(contract)
        except Exception:  # noqa: BLE001 - malformed grant state fails closed
            return ActivationEvaluation(
                effective=False,
                grant_id=None,
                receipt_id=None,
                reason_codes=("activation_evaluation_failed",),
                learned_state=None,
            )

    def _prior_failure_directive(
        self,
        *,
        task: TaskLike,
        subagent_id: str | None,
        attempt: int,
        direct_target_id: str | None,
        forbidden_target_ids: Sequence[str],
    ) -> tuple[str | None, tuple[str, ...], str | None, str | None]:
        forbidden = tuple(sorted(set(forbidden_target_ids)))
        if direct_target_id is not None or attempt <= 1:
            return direct_target_id, forbidden, None, None
        previous = self.ledger.get_attempt_decision(
            run_id=task.run_id,
            task_id=task.task_id,
            subagent_id=subagent_id,
            attempt=attempt - 1,
        )
        if previous is None:
            return None, forbidden, None, None
        outcome = self.ledger.get_outcome(previous.decision_id)
        if outcome is None:
            return None, forbidden, None, previous.selected_target_id
        if outcome.failure_category in PROVIDER_SIDE_FAILURE_CATEGORIES:
            return (
                previous.selected_target_id,
                forbidden,
                "transport_retry_same_target",
                previous.selected_target_id,
            )
        if outcome.failure_category == "capability_failure":
            return (
                None,
                tuple(sorted({*forbidden, previous.selected_target_id})),
                "capability_escalation",
                previous.selected_target_id,
            )
        if outcome.failure_category == "contract_failure":
            raise RoutingUnavailableError(
                "the previous attempt failed its task contract and requires replanning",
                reason_codes=("route_contract_replan_required",),
            )
        return None, forbidden, None, previous.selected_target_id

    def _select_stronger_capability_target(
        self,
        service: AdaptiveFlockRoutingService,
        *,
        base_config: AgentConfig,
        task: TaskLike,
        current: RoutingAssignment,
        previous_target_id: str | None,
        planner_guidance: dict[str, object] | None,
        default_privacy_class: PrivacyClass,
        local_required: bool,
        maximum_cost_usd: float | None,
        allowed_target_ids: Sequence[str],
        forbidden_target_ids: Sequence[str],
        allowed_provider_profiles: Sequence[str],
        forbidden_provider_profiles: Sequence[str],
        review_context: ReviewDiversityContext | None,
    ) -> RoutingAssignment:
        previous = next(
            (
                target
                for target in service.targets
                if target.target_id == previous_target_id
            ),
            None,
        )
        if previous is None:
            raise RoutingUnavailableError(
                "capability escalation references an unavailable previous target",
                reason_codes=("previous_route_target_unavailable",),
            )
        stronger = [
            candidate
            for candidate in current.decision.candidates
            if candidate.eligible
            and candidate.target.quality_tier > previous.quality_tier
        ]
        if not stronger:
            raise RoutingUnavailableError(
                "no stronger eligible target is available after capability failure",
                reason_codes=("no_stronger_capability_target",),
            )
        selected = max(
            stronger,
            key=lambda candidate: (
                float(candidate.score or 0.0),
                candidate.target.quality_tier,
                candidate.target.target_id,
            ),
        )
        return service.assign(
            base_config,
            task,
            planner_guidance=planner_guidance,
            default_privacy_class=default_privacy_class,
            local_required=local_required,
            maximum_cost_usd=maximum_cost_usd,
            allowed_target_ids=allowed_target_ids,
            forbidden_target_ids=forbidden_target_ids,
            allowed_provider_profiles=allowed_provider_profiles,
            forbidden_provider_profiles=forbidden_provider_profiles,
            direct_target_id=selected.target.target_id,
            review_context=review_context,
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
        allowed_target_ids: Sequence[str],
        forbidden_target_ids: Sequence[str],
        allowed_provider_profiles: Sequence[str],
        forbidden_provider_profiles: Sequence[str],
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
            allowed_target_ids=allowed_target_ids,
            forbidden_target_ids=forbidden_target_ids,
            allowed_provider_profiles=allowed_provider_profiles,
            forbidden_provider_profiles=forbidden_provider_profiles,
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
                else replace(base_config, lan_runtime_authority=None)
            ),
            executes_selected_target=existing.actionable,
        )
        return DurableRoutingAssignment(
            assignment=leased_assignment,
            record=existing,
            reused=True,
        )


def _activation_abstention_reason(authority: ActivationEvaluation | None) -> str | None:
    """Record why learned routing was not authorized at the activation gate.

    ``None`` means the gate was never consulted (shadow/constrained modes,
    earlier abstention rungs) or the grant was effective.
    """

    if authority is None or authority.effective:
        return None
    if authority.reason_codes:
        return authority.reason_codes[0]
    return "durable_grant_required"


def _actual_cost_from_usage(
    decision: RouteDecisionEntry,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    input_rate = decision.input_cost_per_million_usd
    output_rate = decision.output_cost_per_million_usd
    if input_rate is None or output_rate is None:
        return None
    return round(
        (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000.0,
        12,
    )


def _annotate_assignment(
    service: AdaptiveFlockRoutingService,
    *,
    base_config: AgentConfig,
    assignment: RoutingAssignment,
    selection_kind: str,
    reason_code: str,
) -> RoutingAssignment:
    decision = replace(
        assignment.decision,
        selection_kind=selection_kind,
        reason_codes=tuple(
            dict.fromkeys((*assignment.decision.reason_codes, reason_code))
        ),
    )
    return RoutingAssignment(
        contract=assignment.contract,
        decision=decision,
        config=(
            service.apply_decision(base_config, decision)
            if decision.actionable
            else replace(base_config, lan_runtime_authority=None)
        ),
        executes_selected_target=decision.actionable,
    )
