from __future__ import annotations

from dataclasses import dataclass

from .models import (
    AgentTaskContract,
    ModelTarget,
    RouteCandidate,
    RouteDecision,
    RoutePolicy,
    RoutingMode,
)


class RoutingUnavailableError(RuntimeError):
    def __init__(self, reason: str, *, reason_codes: tuple[str, ...] = ()) -> None:
        self.reason_codes = reason_codes
        super().__init__(reason)


@dataclass(frozen=True)
class ReviewDiversityContext:
    target_id: str | None = None
    provider_profile_id: str | None = None
    model_family: str | None = None


def route_task(
    contract: AgentTaskContract,
    targets: tuple[ModelTarget, ...] | list[ModelTarget],
    *,
    policy: RoutePolicy | None = None,
    mode: RoutingMode = "shadow",
    direct_target_id: str | None = None,
    review_context: ReviewDiversityContext | None = None,
) -> RouteDecision:
    active_policy = policy or RoutePolicy()
    candidates = tuple(
        _candidate(contract, target, active_policy, review_context=review_context)
        for target in sorted(targets, key=lambda item: item.target_id)
    )
    eligible = [candidate for candidate in candidates if candidate.eligible]
    if direct_target_id is not None:
        selected = next(
            (candidate for candidate in candidates if candidate.target.target_id == direct_target_id),
            None,
        )
        if selected is None:
            raise RoutingUnavailableError(
                f"unknown direct routing target: {direct_target_id}",
                reason_codes=("direct_target_unknown",),
            )
        if not selected.eligible:
            raise RoutingUnavailableError(
                f"direct routing target is ineligible: {direct_target_id}",
                reason_codes=selected.reason_codes,
            )
        return _decision(
            contract,
            candidates,
            selected,
            active_policy,
            mode,
            selection_kind="operator_override",
            reason_codes=("operator_override",),
        )
    if not eligible:
        rejected = tuple(
            sorted({code for candidate in candidates for code in candidate.reason_codes})
        )
        raise RoutingUnavailableError("no eligible routing target", reason_codes=rejected)
    selected = max(
        eligible,
        key=lambda candidate: (
            candidate.score if candidate.score is not None else float("-inf"),
            candidate.target.quality_tier,
            candidate.target.operator_priority,
            _reverse_stable_id(candidate.target.target_id),
        ),
    )
    return _decision(
        contract,
        candidates,
        selected,
        active_policy,
        mode,
        selection_kind="deterministic_router",
        reason_codes=("highest_admissible_score",),
    )


def _candidate(
    contract: AgentTaskContract,
    target: ModelTarget,
    policy: RoutePolicy,
    *,
    review_context: ReviewDiversityContext | None,
) -> RouteCandidate:
    reasons = _ineligibility_reasons(contract, target, policy, review_context=review_context)
    if reasons:
        return RouteCandidate(target=target, eligible=False, score=None, reason_codes=reasons)
    components = _score_components(contract, target, policy, review_context=review_context)
    score = round(sum(components.values()), 8)
    return RouteCandidate(
        target=target,
        eligible=True,
        score=score,
        reason_codes=("eligible",),
        components=components,
    )


def _ineligibility_reasons(
    contract: AgentTaskContract,
    target: ModelTarget,
    policy: RoutePolicy,
    *,
    review_context: ReviewDiversityContext | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not target.enabled:
        reasons.append("target_disabled")
    if target.health in {"open", "unavailable"}:
        reasons.append(f"target_health_{target.health}")
    if contract.local_required and target.locality != "local":
        reasons.append("local_required")
    if target.provider_profile_id in contract.forbidden_provider_profiles:
        reasons.append("provider_profile_forbidden")
    if set(target.capability_tags) & set(contract.forbidden_target_tags):
        reasons.append("target_tag_forbidden")
    if "tools" in contract.required_capabilities and not target.supports_tools:
        reasons.append("tools_unsupported")
    if contract.structured_output_required and not target.supports_json:
        reasons.append("structured_output_unsupported")
    if "image" in contract.required_modalities and not target.supports_vision:
        reasons.append("vision_unsupported")
    if "reasoning" in contract.required_capabilities and not target.supports_reasoning:
        reasons.append("reasoning_unsupported")
    if (
        contract.minimum_context_tokens is not None
        and target.max_context_tokens is not None
        and target.max_context_tokens < contract.minimum_context_tokens
    ):
        reasons.append("context_too_small")
    if contract.minimum_context_tokens is not None and target.max_context_tokens is None:
        reasons.append("context_unknown")
    required_quality = policy.minimum_quality_by_risk.get(contract.risk, 1)
    if target.quality_tier < required_quality:
        reasons.append("quality_below_risk_floor")
    if (
        contract.maximum_cost_usd is not None
        and target.estimated_cost_usd is not None
        and target.estimated_cost_usd > contract.maximum_cost_usd
    ):
        reasons.append("task_cost_budget_exceeded")
    if contract.maximum_cost_usd is not None and target.estimated_cost_usd is None:
        reasons.append("cost_unknown_under_hard_budget")
    if contract.role == "reviewer" and review_context is not None:
        if policy.require_different_target_for_review and target.target_id == review_context.target_id:
            reasons.append("review_target_not_independent")
        target_family = str(target.metadata.get("model_family", "")).strip()
        if (
            policy.require_different_model_family_for_review
            and review_context.model_family
            and target_family
            and target_family == review_context.model_family
        ):
            reasons.append("review_model_family_not_independent")
    return tuple(sorted(set(reasons)))


def _score_components(
    contract: AgentTaskContract,
    target: ModelTarget,
    policy: RoutePolicy,
    *,
    review_context: ReviewDiversityContext | None,
) -> dict[str, float]:
    predicted_success = (
        target.predicted_success
        if target.predicted_success is not None
        else min(0.95, 0.35 + target.quality_tier * 0.11)
    )
    quality = policy.quality_weight * predicted_success

    role_match = 1.0 if contract.role in target.role_affinities else 0.0
    family_match = 1.0 if contract.task_family in target.task_family_affinities else 0.0
    preferred_tags = set(contract.preferred_target_tags)
    tag_match = (
        len(preferred_tags & set(target.capability_tags)) / len(preferred_tags)
        if preferred_tags
        else 0.0
    )
    affinity = policy.affinity_weight * ((role_match + family_match + tag_match) / 3.0)

    health_value = {"healthy": 1.0, "degraded": 0.55, "unknown": 0.4}.get(target.health, 0.0)
    health = policy.health_weight * health_value

    context_value = 0.5
    if contract.minimum_context_tokens is not None and target.max_context_tokens is not None:
        context_value = min(1.0, target.max_context_tokens / contract.minimum_context_tokens / 2.0)
    context = policy.context_weight * context_value

    locality_value = 0.0
    if target.locality == "local" and (contract.local_preferred or contract.local_required):
        locality_value = 1.0
    elif target.locality == "local":
        locality_value = 0.35
    locality = policy.locality_weight * locality_value

    operator = policy.operator_weight * max(-1.0, min(1.0, target.operator_priority / 10.0))

    if target.estimated_cost_usd is None:
        normalized_cost = 0.5
    elif contract.maximum_cost_usd and contract.maximum_cost_usd > 0:
        normalized_cost = min(1.0, target.estimated_cost_usd / contract.maximum_cost_usd)
    else:
        normalized_cost = min(1.0, target.estimated_cost_usd)
    cost = -policy.cost_weight * normalized_cost

    normalized_latency = (target.latency_tier - 1) / 4.0
    latency = -policy.latency_weight * normalized_latency
    failure = -policy.failure_weight * target.recent_failure_rate

    diversity = 0.0
    if (
        contract.role == "reviewer"
        and review_context is not None
        and policy.prefer_different_provider_for_review
        and target.provider_profile_id != review_context.provider_profile_id
    ):
        diversity = 0.03

    return {
        "quality": round(quality, 8),
        "affinity": round(affinity, 8),
        "health": round(health, 8),
        "context": round(context, 8),
        "locality": round(locality, 8),
        "operator": round(operator, 8),
        "cost": round(cost, 8),
        "latency": round(latency, 8),
        "failure": round(failure, 8),
        "review_diversity": round(diversity, 8),
    }


def _decision(
    contract: AgentTaskContract,
    candidates: tuple[RouteCandidate, ...],
    selected: RouteCandidate,
    policy: RoutePolicy,
    mode: RoutingMode,
    *,
    selection_kind: str,
    reason_codes: tuple[str, ...],
) -> RouteDecision:
    if selected.score is None:
        raise RoutingUnavailableError("selected routing candidate has no score")
    return RouteDecision(
        mode=mode,
        policy_id=policy.policy_id,
        contract_digest=contract.digest,
        selected_target=selected.target,
        selection_kind=selection_kind,
        score=selected.score,
        reason_codes=reason_codes,
        candidates=candidates,
        actionable=mode in {"constrained", "adaptive"},
    )


def _reverse_stable_id(value: str) -> tuple[int, ...]:
    return tuple(-ord(character) for character in value)
