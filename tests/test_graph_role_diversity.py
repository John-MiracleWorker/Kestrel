"""PR 6 — Planner/reviewer diversity tests.

Graph role assignment routes planner and reviewer calls independently from
the executor. An independent reviewer policy enforces different targets or
model families for review. Invalid provider review falls back to the
deterministic evidence gate. Reviewer model opinions cannot prove tests
passed without trusted validation receipts.
"""
from __future__ import annotations

import pytest

from nested_memvid_agent.routing.models import (
    AgentTaskContract,
    ModelTarget,
    ProviderProfile,
    RoutePolicy,
)
from nested_memvid_agent.routing.role_resolver import (
    GraphRoleAssignment,
    RoleAssignmentResolver,
    resolve_graph_roles,
)
from nested_memvid_agent.routing.router import RoutingUnavailableError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_contract(role: str = "executor", risk: str = "low") -> AgentTaskContract:
    return AgentTaskContract(
        task_id="t1",
        run_id="r1",
        role=role,
        task_family="mechanical_refactor",
        objective="rename a variable",
        complexity=0.3,
        ambiguity=0.2,
        risk=risk,
    )


def _make_profile(pid: str, adapter: str = "openai_compatible", locality: str = "cloud") -> ProviderProfile:
    return ProviderProfile(
        profile_id=pid,
        display_name=pid,
        adapter=adapter,
        base_url="https://example.com",
        secret_ref="SECRET",
        locality=locality,  # type: ignore[arg-type]
    )


def _make_target(
    tid: str,
    pid: str,
    *,
    model: str = "m1",
    quality_tier: int = 3,
    role_affinities: tuple[str, ...] = ("executor",),
    model_family: str | None = None,
    locality: str = "cloud",
    provider: str = "openai_compatible",
) -> ModelTarget:
    metadata: dict[str, object] = {}
    if model_family is not None:
        metadata["model_family"] = model_family
    return ModelTarget(
        target_id=tid,
        provider_profile_id=pid,
        provider=provider,
        model=model,
        quality_tier=quality_tier,
        role_affinities=role_affinities,
        locality=locality,  # type: ignore[arg-type]
        metadata=metadata,
    )


PROFILES = (
    _make_profile("prov-a"),
    _make_profile("prov-b"),
)

TARGETS = (
    _make_target("tgt-exec", "prov-a", model="exec-model", quality_tier=3,
                 role_affinities=("executor",), model_family="family-a"),
    _make_target("tgt-plan", "prov-b", model="plan-model", quality_tier=4,
                 role_affinities=("planner",), model_family="family-b"),
    _make_target("tgt-review", "prov-b", model="review-model", quality_tier=4,
                 role_affinities=("reviewer",), model_family="family-b"),
)

EXEC_CONTRACT = _make_contract("executor", "low")
PLAN_CONTRACT = _make_contract("planner", "medium")
REVIEW_CONTRACT = _make_contract("reviewer", "medium")


# ---------------------------------------------------------------------------
# Planner and reviewer can use different targets from the implementer
# ---------------------------------------------------------------------------

class TestGraphRoleAssignment:
    def test_role_resolver_assigns_planner_to_planner_affinity_target(self):
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=TARGETS,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        assert assignment.planner_decision.selected_target.target_id == "tgt-plan"
        assert assignment.executor_decision.selected_target.target_id != "tgt-plan"

    def test_role_resolver_assigns_reviewer_to_reviewer_affinity_target(self):
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=TARGETS,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        assert assignment.reviewer_decision.selected_target.target_id == "tgt-review"
        assert assignment.reviewer_decision.selected_target.target_id != assignment.executor_decision.selected_target.target_id

    def test_all_three_roles_can_use_different_targets(self):
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=TARGETS,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        exec_id = assignment.executor_decision.selected_target.target_id
        plan_id = assignment.planner_decision.selected_target.target_id
        review_id = assignment.reviewer_decision.selected_target.target_id
        assert len({exec_id, plan_id, review_id}) == 3, \
            f"Expected 3 distinct targets, got {exec_id}, {plan_id}, {review_id}"

    def test_review_diversity_policy_rejects_same_target(self):
        """When require_different_target_for_review is True, reviewer cannot
        use the same target as the executor."""
        policy = RoutePolicy(require_different_target_for_review=True)
        # Only one target available — review should be blocked
        single_target = (_make_target("tgt-only", "prov-a", role_affinities=("executor", "reviewer")),)
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=single_target,
            policy=policy,
            mode="constrained",
        )
        with pytest.raises(RoutingUnavailableError, match="no eligible routing target"):
            resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)

    def test_review_diversity_policy_rejects_same_model_family(self):
        """When require_different_model_family_for_review is True, reviewer
        cannot use the same model family as the executor."""
        policy = RoutePolicy(require_different_model_family_for_review=True)
        # Two targets in the same family — review should be blocked
        same_family_targets = (
            _make_target("tgt-a", "prov-a", model="a-model", model_family="samefam",
                         role_affinities=("executor",)),
            _make_target("tgt-b", "prov-b", model="b-model", model_family="samefam",
                         role_affinities=("reviewer",)),
        )
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=same_family_targets,
            policy=policy,
            mode="constrained",
        )
        with pytest.raises(RoutingUnavailableError, match="no eligible routing target"):
            resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)


# ---------------------------------------------------------------------------
# Review diversity never violates privacy policy
# ---------------------------------------------------------------------------

class TestReviewPrivacyPolicy:
    def test_reviewer_respects_local_required_constraint(self):
        """A local_required task cannot route review to a cloud target."""
        local_contract = AgentTaskContract(
            task_id="t1",
            run_id="r1",
            role="reviewer",
            task_family="inspection",
            objective="review local-only work",
            complexity=0.4,
            ambiguity=0.3,
            risk="medium",
            local_required=True,
        )
        cloud_only_targets = (_make_target("tgt-cloud", "prov-a", locality="cloud",
                                           role_affinities=("reviewer",)),)
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=cloud_only_targets,
            policy=RoutePolicy(),
            mode="constrained",
        )
        with pytest.raises(RoutingUnavailableError) as exc_info:
            resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, local_contract)
        assert "local_required" in exc_info.value.reason_codes

    def test_reviewer_never_routes_to_forbidden_provider(self):
        """A reviewer contract with a forbidden provider profile is rejected."""
        review_contract = AgentTaskContract(
            task_id="t1",
            run_id="r1",
            role="reviewer",
            task_family="inspection",
            objective="review work",
            complexity=0.4,
            ambiguity=0.3,
            risk="medium",
            forbidden_provider_profiles=("prov-a",),
        )
        targets = (
            _make_target("tgt-a", "prov-a", role_affinities=("reviewer",), model_family="fa"),
            _make_target("tgt-b", "prov-b", role_affinities=("reviewer",), model_family="fb"),
        )
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=targets,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, review_contract)
        assert assignment.reviewer_decision.selected_target.provider_profile_id == "prov-b"


# ---------------------------------------------------------------------------
# Invalid provider review falls back to deterministic evidence gate
# ---------------------------------------------------------------------------

class TestDeterministicEvidenceFallback:
    def test_role_resolver_with_no_reviewer_targets_still_returns_executor_and_planner(self):
        """When no target has reviewer affinity, the resolver returns executor
        and planner decisions and marks review as requiring deterministic fallback."""
        targets = (
            _make_target("tgt-exec", "prov-a", role_affinities=("executor",)),
            _make_target("tgt-plan", "prov-b", role_affinities=("planner",)),
        )
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=targets,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        assert assignment.executor_decision is not None
        assert assignment.planner_decision is not None
        assert assignment.reviewer_decision is None
        assert assignment.review_fallback is True

    def test_review_fallback_is_explicit_when_reviewer_unavailable(self):
        """The GraphRoleAssignment must explicitly flag that review will use
        the deterministic evidence gate rather than silently succeeding."""
        targets = (
            _make_target("tgt-exec", "prov-a", role_affinities=("executor",)),
        )
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=targets,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        assert assignment.review_fallback is True
        assert assignment.reviewer_decision is None


# ---------------------------------------------------------------------------
# Reviewer model opinion cannot prove tests passed without trusted receipts
# ---------------------------------------------------------------------------

class TestReviewerOpinionGuard:
    def test_review_decision_records_reviewer_target_separately(self):
        """The GraphRoleAssignment records the reviewer target separately from
        the executor so downstream code can enforce receipt validation."""
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=TARGETS,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        assert assignment.reviewer_decision is not None
        assert assignment.reviewer_decision.selected_target.target_id != \
               assignment.executor_decision.selected_target.target_id

    def test_review_fallback_does_not_claim_tests_passed(self):
        """When review falls back to deterministic evidence, it must not
        emit a review artifact that claims tests passed via model opinion."""
        targets = (
            _make_target("tgt-exec", "prov-a", role_affinities=("executor",)),
        )
        resolver = RoleAssignmentResolver(
            profiles=PROFILES,
            targets=targets,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        assert assignment.review_fallback is True
        # The fallback flag means downstream must use evidence gate, not model opinion
        # The assignment must not carry a reviewer decision that could be mistaken for model review
        assert assignment.reviewer_decision is None


# ---------------------------------------------------------------------------
# resolve_graph_roles convenience function
# ---------------------------------------------------------------------------

class TestResolveGraphRoles:
    def test_resolve_graph_roles_returns_complete_assignment(self):
        assignment = resolve_graph_roles(
            executor_contract=EXEC_CONTRACT,
            planner_contract=PLAN_CONTRACT,
            reviewer_contract=REVIEW_CONTRACT,
            profiles=PROFILES,
            targets=TARGETS,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assert isinstance(assignment, GraphRoleAssignment)
        assert assignment.executor_decision is not None
        assert assignment.planner_decision is not None
        assert assignment.reviewer_decision is not None
        assert assignment.review_fallback is False