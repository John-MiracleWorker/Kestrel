"""Graph role assignment resolver.

Routes planner and reviewer calls independently from the executor so graph
roles can use different targets, model families, and provider profiles.

The resolver is a pure decision boundary — it owns no lifecycle state.
RunManager and graph runtime nodes remain the authority for execution,
approvals, cancellation, and terminal transitions.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import (
    AgentTaskContract,
    ModelTarget,
    ProviderProfile,
    RouteDecision,
    RoutePolicy,
    RoutingMode,
)
from .router import ReviewDiversityContext, RoutingUnavailableError, route_task


@dataclass(frozen=True)
class GraphRoleAssignment:
    """Resolved route decisions for each graph role."""

    executor_decision: RouteDecision
    planner_decision: RouteDecision | None
    reviewer_decision: RouteDecision | None
    review_fallback: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "executor": self.executor_decision.to_payload(),
            "planner": self.planner_decision.to_payload() if self.planner_decision else None,
            "reviewer": self.reviewer_decision.to_payload() if self.reviewer_decision else None,
            "review_fallback": self.review_fallback,
        }


class RoleAssignmentResolver:
    """Resolve route decisions for planner, executor, and reviewer graph roles.

    Each role gets an independent route decision. When no eligible target
    exists for the reviewer role, the resolver sets ``review_fallback=True``
    and returns ``reviewer_decision=None`` so the graph runtime knows to use
    the deterministic evidence gate instead of a model-opinion review.
    """

    def __init__(
        self,
        *,
        profiles: tuple[ProviderProfile, ...] | list[ProviderProfile],
        targets: tuple[ModelTarget, ...] | list[ModelTarget],
        policy: RoutePolicy | None = None,
        mode: RoutingMode = "shadow",
    ) -> None:
        self.profiles = tuple(profiles)
        self.targets = tuple(targets)
        self.policy = policy or RoutePolicy()
        self.mode = mode

    def resolve(
        self,
        executor_contract: AgentTaskContract,
        planner_contract: AgentTaskContract,
        reviewer_contract: AgentTaskContract,
    ) -> GraphRoleAssignment:
        """Resolve all three role decisions in one call.

        The executor decision is always required. If the planner or reviewer
        has no eligible target, the corresponding decision is ``None`` and the
        caller must handle the fallback.
        """
        executor_decision = self._route_role(executor_contract, review_context=None)

        planner_decision: RouteDecision | None = None
        try:
            planner_decision = self._route_role(planner_contract, review_context=None)
        except RoutingUnavailableError:
            planner_decision = None

        reviewer_context = ReviewDiversityContext(
            target_id=executor_decision.selected_target.target_id,
            provider_profile_id=executor_decision.selected_target.provider_profile_id,
            model_family=str(
                executor_decision.selected_target.metadata.get("model_family", "")
            ).strip() or None,
        )

        reviewer_decision: RouteDecision | None = None
        review_fallback = False

        # Distinguish soft fallback (no reviewer-affinity targets exist) from
        # hard rejection (targets exist but diversity policy rejects them all).
        has_reviewer_targets = any(
            "reviewer" in t.role_affinities for t in self._eligible_targets()
        )
        if not has_reviewer_targets:
            review_fallback = True
        else:
            reviewer_decision = self._route_role(
                reviewer_contract,
                review_context=reviewer_context,
            )

        return GraphRoleAssignment(
            executor_decision=executor_decision,
            planner_decision=planner_decision,
            reviewer_decision=reviewer_decision,
            review_fallback=review_fallback,
        )

    def _route_role(
        self,
        contract: AgentTaskContract,
        *,
        review_context: ReviewDiversityContext | None,
    ) -> RouteDecision:
        eligible_targets = self._eligible_targets()
        return route_task(
            contract,
            eligible_targets,
            policy=self.policy,
            mode=self.mode,
            review_context=review_context,
        )

    def _eligible_targets(self) -> tuple[ModelTarget, ...]:
        profile_ids = {p.profile_id for p in self.profiles if p.enabled}
        return tuple(t for t in self.targets if t.provider_profile_id in profile_ids)


def resolve_graph_roles(
    *,
    executor_contract: AgentTaskContract,
    planner_contract: AgentTaskContract,
    reviewer_contract: AgentTaskContract,
    profiles: tuple[ProviderProfile, ...] | list[ProviderProfile],
    targets: tuple[ModelTarget, ...] | list[ModelTarget],
    policy: RoutePolicy | None = None,
    mode: RoutingMode = "shadow",
) -> GraphRoleAssignment:
    """Convenience wrapper around ``RoleAssignmentResolver.resolve``."""
    resolver = RoleAssignmentResolver(
        profiles=profiles,
        targets=targets,
        policy=policy,
        mode=mode,
    )
    return resolver.resolve(executor_contract, planner_contract, reviewer_contract)