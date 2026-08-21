"""Graph role assignment resolver.

Routes planner and reviewer calls independently from the executor so graph
roles can use different targets, model families, and provider profiles.

The resolver is a pure decision boundary — it owns no lifecycle state.
RunManager and graph runtime nodes remain the authority for execution,
approvals, cancellation, and terminal transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import (
    AgentTaskContract,
    ModelTarget,
    ProviderProfile,
    RouteDecision,
    RoutePolicy,
    RoutingMode,
)
from .router import ReviewDiversityContext, RoutingUnavailableError, route_task


class ReviewAuthority(StrEnum):
    """Closed, durable label for *how* the reviewer role was satisfied.

    JOURNEY-004 requires durable role assignments to show either a distinct,
    qualified reviewer target/model family **or** an explicit non-independent
    deterministic fallback.  This enum is the single source of truth for that
    label so the graph runtime can persist it without inferring it.
    """

    INDEPENDENT_TARGET = "independent_target"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"
    OFF_MODE_ABSTAINED = "off_mode_abstained"


@dataclass(frozen=True)
class GraphRoleAssignment:
    """Resolved route decisions for each graph role.

    ``review_authority`` is the durable JOURNEY-004 label for how the reviewer
    role was satisfied: ``INDEPENDENT_TARGET`` when a distinct, eligible
    reviewer target was chosen, ``DETERMINISTIC_FALLBACK`` when no independent
    reviewer target exists so the deterministic evidence gate is the review
    authority, and ``OFF_MODE_ABSTAINED`` when routing mode is ``off`` and the
    reviewer was intentionally not routed.  The label is total: exactly one of
    the three is always present.
    """

    executor_decision: RouteDecision
    planner_decision: RouteDecision | None
    reviewer_decision: RouteDecision | None
    review_fallback: bool
    off_mode_abstained: bool = False
    review_authority: ReviewAuthority = ReviewAuthority.DETERMINISTIC_FALLBACK
    review_rejection_reasons: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "executor": self.executor_decision.to_payload(),
            "planner": self.planner_decision.to_payload() if self.planner_decision else None,
            "reviewer": self.reviewer_decision.to_payload() if self.reviewer_decision else None,
            "review_fallback": self.review_fallback,
            "off_mode_abstained": self.off_mode_abstained,
            "review_authority": self.review_authority.value,
            "review_rejection_reasons": tuple(self.review_rejection_reasons),
            "executor_context": self._executor_context(),
        }

    def _executor_context(self) -> dict[str, object]:
        """Durable executor identity the reviewer was evaluated against.

        Carrying the executor's target/profile/model-family into the assignment
        lets the graph runtime prove the reviewer was routed *independently of*
        (or truthfully against) the same executor, without re-deriving it.
        """
        target = self.executor_decision.selected_target
        family = str(target.metadata.get("model_family", "")).strip()
        return {
            "executor_target_id": target.target_id,
            "executor_provider_profile_id": target.provider_profile_id,
            "executor_model_family": family or None,
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

        # Off mode must abstain on review: the reviewer is not routed at all,
        # and the abstention is durably labeled rather than claimed as a
        # deterministic fallback.
        if self.mode == "off":
            return GraphRoleAssignment(
                executor_decision=executor_decision,
                planner_decision=planner_decision,
                reviewer_decision=None,
                review_fallback=False,
                off_mode_abstained=True,
                review_authority=ReviewAuthority.OFF_MODE_ABSTAINED,
            )

        reviewer_context = ReviewDiversityContext(
            target_id=executor_decision.selected_target.target_id,
            provider_profile_id=executor_decision.selected_target.provider_profile_id,
            model_family=str(
                executor_decision.selected_target.metadata.get("model_family", "")
            ).strip()
            or None,
        )

        reviewer_decision: RouteDecision | None = None
        review_fallback = False
        review_authority = ReviewAuthority.DETERMINISTIC_FALLBACK
        review_rejection_reasons: tuple[str, ...] = ()

        # Distinguish soft fallback (no reviewer-affinity targets exist) from
        # independence rejection (reviewer targets exist but the configured
        # independence policy rejects them all).  Both are *truthful* outcomes
        # that must surface as a labeled deterministic fallback, never as a
        # hard error the graph cannot label (S4 / JOURNEY-004).
        has_reviewer_targets = any(
            "reviewer" in t.role_affinities for t in self._eligible_targets()
        )
        if not has_reviewer_targets:
            review_fallback = True
            review_rejection_reasons = ("reviewer_role_has_no_eligible_target",)
        else:
            try:
                reviewer_decision = self._route_role(
                    reviewer_contract,
                    review_context=reviewer_context,
                )
                review_authority = ReviewAuthority.INDEPENDENT_TARGET
            except RoutingUnavailableError as exc:
                # Configured independence (different target / model family) or a
                # hard constraint (privacy / cost) rejected every candidate:
                # fall back to the deterministic evidence gate, label it
                # truthfully, and preserve the rejection reason for audit.
                reviewer_decision = None
                review_fallback = True
                review_authority = ReviewAuthority.DETERMINISTIC_FALLBACK
                review_rejection_reasons = tuple(exc.reason_codes or ("review_rejected",))

        return GraphRoleAssignment(
            executor_decision=executor_decision,
            planner_decision=planner_decision,
            reviewer_decision=reviewer_decision,
            review_fallback=review_fallback,
            review_authority=review_authority,
            review_rejection_reasons=review_rejection_reasons,
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
