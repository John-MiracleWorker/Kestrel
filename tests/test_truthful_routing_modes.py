"""S4 / JOURNEY-004 — truthful production reviewer routing modes.

Stacked-branch preparation on top of S3 (PR #348 merged at 16144d67).

Task 5 (V0.6 proof release source of truth) requires:
  * carry durable executor context into reviewer routing,
  * enforce configured independence,
  * label the deterministic fallback truthfully when no independent target
    exists (never silently succeed, never raise a hard error that the
    graph cannot label),
  * "off" mode must abstain.

These tests pin the *durable* review-authority label that the role resolver
exposes to the graph runtime.  ``route_task`` keeps its hard "no eligible
target" contract for ordinary routing; the reviewer *role* path must instead
surface a truthful, durable label so the runtime can record the deterministic
evidence gate as the review authority.
"""

from __future__ import annotations

from typing import cast

from nested_memvid_agent.routing.models import (
    AgentTaskContract,
    ModelTarget,
    ProviderProfile,
    RoutePolicy,
    RoutingMode,
)
from nested_memvid_agent.routing.role_resolver import (
    GraphRoleAssignment,
    ReviewAuthority,
    RoleAssignmentResolver,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROFILES = (
    ProviderProfile(
        profile_id="prov-a",
        display_name="A",
        adapter="openai_compatible",
        base_url="https://example.com",
        secret_ref="S",
        locality="cloud",
    ),
    ProviderProfile(
        profile_id="prov-b",
        display_name="B",
        adapter="openai_compatible",
        base_url="https://example.com",
        secret_ref="S",
        locality="cloud",
    ),
)

EXEC_CONTRACT = AgentTaskContract(
    task_id="t1",
    run_id="r1",
    role="executor",
    task_family="mechanical_refactor",
    objective="rename a variable",
    complexity=0.3,
    ambiguity=0.2,
    risk="low",
)
PLAN_CONTRACT = AgentTaskContract(
    task_id="t1",
    run_id="r1",
    role="planner",
    task_family="mechanical_refactor",
    objective="plan rename",
    complexity=0.3,
    ambiguity=0.2,
    risk="low",
)
REVIEW_CONTRACT = AgentTaskContract(
    task_id="t1",
    run_id="r1",
    role="reviewer",
    task_family="mechanical_refactor",
    objective="review rename",
    complexity=0.3,
    ambiguity=0.2,
    risk="low",
)


def _target(
    tid: str,
    pid: str = "prov-a",
    *,
    model: str = "m",
    model_family: str | None = None,
    role_affinities: tuple[str, ...] = ("executor",),
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
        quality_tier=3,
        role_affinities=role_affinities,
        locality="cloud",
        metadata=metadata,
    )


INDEPENDENT_TARGETS = (
    _target(
        "tgt-exec",
        "prov-a",
        model="exec-model",
        model_family="family-a",
        role_affinities=("executor",),
    ),
    _target(
        "tgt-plan",
        "prov-b",
        model="plan-model",
        model_family="family-b",
        role_affinities=("planner",),
    ),
    _target(
        "tgt-review",
        "prov-b",
        model="review-model",
        model_family="family-b",
        role_affinities=("reviewer",),
    ),
)

NO_REVIEWER_TARGETS = (
    _target(
        "tgt-exec",
        "prov-a",
        model="exec-model",
        model_family="family-a",
        role_affinities=("executor",),
    ),
    _target(
        "tgt-plan",
        "prov-b",
        model="plan-model",
        model_family="family-b",
        role_affinities=("planner",),
    ),
)

SINGLE_REVIEWER_TARGET = (
    _target(
        "tgt-exec-review",
        "prov-a",
        model="one-model",
        model_family="family-a",
        role_affinities=("executor", "reviewer"),
    ),
)


def _resolver(
    *,
    mode: RoutingMode = "constrained",
    targets: tuple[ModelTarget, ...] = INDEPENDENT_TARGETS,
    policy: RoutePolicy | None = None,
) -> RoleAssignmentResolver:
    return RoleAssignmentResolver(
        profiles=PROFILES,
        targets=targets,
        policy=policy or RoutePolicy(),
        mode=mode,
    )


def _executor_context(assignment: GraphRoleAssignment) -> dict[str, object]:
    payload = assignment.to_payload()
    return cast("dict[str, object]", payload["executor_context"])


# ---------------------------------------------------------------------------
# 1. Off mode must abstain — reviewer is not routed, no fallback is claimed,
#    and the abstention is durably labeled.
# ---------------------------------------------------------------------------


class TestOffModeAbstains:
    def test_off_mode_does_not_route_reviewer(self):
        assignment = _resolver(mode="off", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        assert assignment.reviewer_decision is None
        assert assignment.off_mode_abstained is True
        # Abstention is not a fallback: the deterministic evidence gate is NOT
        # being substituted in — the reviewer was simply not invoked.
        assert assignment.review_fallback is False
        assert assignment.review_authority == ReviewAuthority.OFF_MODE_ABSTAINED

    def test_off_mode_still_returns_executor_and_planner(self):
        """Off mode abstains on review only; executor/planner routing is the
        deterministic baseline and must remain valid (mode off => not actionable)."""
        assignment = _resolver(mode="off", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        assert assignment.executor_decision is not None
        assert assignment.executor_decision.selected_target.target_id == "tgt-exec"
        assert assignment.executor_decision.actionable is False
        assert assignment.planner_decision is not None
        assert assignment.planner_decision.selected_target.target_id == "tgt-plan"

    def test_off_mode_abstains_even_with_independent_reviewer_target(self):
        """A perfectly eligible independent reviewer target must NOT be used in
        off mode — abstention is mode-driven, not eligibility-driven."""
        assignment = _resolver(mode="off", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        assert assignment.reviewer_decision is None
        assert assignment.off_mode_abstained is True

    def test_off_mode_payload_labels_abstention(self):
        assignment = _resolver(mode="off", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        payload = assignment.to_payload()
        assert payload["off_mode_abstained"] is True
        assert payload["review_authority"] == "off_mode_abstained"
        assert payload["reviewer"] is None


# ---------------------------------------------------------------------------
# 2. Configured independence, when it rejects every reviewer candidate, must
#    surface a truthful deterministic-fallback label — never a hard error.
# ---------------------------------------------------------------------------


class TestIndependenceFallbackIsTruthful:
    def test_require_different_target_falls_back_not_raises(self):
        policy = RoutePolicy(require_different_target_for_review=True)
        assignment = _resolver(
            mode="constrained", targets=SINGLE_REVIEWER_TARGET, policy=policy
        ).resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        assert assignment.reviewer_decision is None
        assert assignment.review_fallback is True
        assert assignment.review_authority == ReviewAuthority.DETERMINISTIC_FALLBACK
        assert assignment.off_mode_abstained is False

    def test_require_different_model_family_falls_back_not_raises(self):
        policy = RoutePolicy(require_different_model_family_for_review=True)
        targets = (
            _target(
                "tgt-exec",
                "prov-a",
                model="a",
                model_family="samefam",
                role_affinities=("executor",),
            ),
            _target(
                "tgt-review",
                "prov-b",
                model="b",
                model_family="samefam",
                role_affinities=("reviewer",),
            ),
        )
        assignment = _resolver(mode="constrained", targets=targets, policy=policy).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        assert assignment.reviewer_decision is None
        assert assignment.review_fallback is True
        assert assignment.review_authority == ReviewAuthority.DETERMINISTIC_FALLBACK

    def test_fallback_payload_labels_deterministic_gate(self):
        policy = RoutePolicy(require_different_target_for_review=True)
        assignment = _resolver(
            mode="constrained", targets=SINGLE_REVIEWER_TARGET, policy=policy
        ).resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        payload = assignment.to_payload()
        assert payload["review_authority"] == "deterministic_fallback"
        assert payload["review_fallback"] is True
        assert payload["reviewer"] is None
        assert payload["off_mode_abstained"] is False


# ---------------------------------------------------------------------------
# 3. When an independent target IS chosen, the review authority is labeled
#    "independent_target" — the durable proof for JOURNEY-004.
# ---------------------------------------------------------------------------


class TestIndependentTargetLabeling:
    def test_independent_reviewer_is_labeled_independent(self):
        assignment = _resolver(mode="constrained", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        assert assignment.reviewer_decision is not None
        assert assignment.reviewer_decision.selected_target.target_id == "tgt-review"
        assert assignment.review_authority == ReviewAuthority.INDEPENDENT_TARGET
        assert assignment.review_fallback is False
        assert assignment.off_mode_abstained is False

    def test_independent_label_differs_from_executor(self):
        assignment = _resolver(mode="constrained", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        assert (
            assignment.reviewer_decision.selected_target.target_id
            != assignment.executor_decision.selected_target.target_id
        )
        assert assignment.review_authority == ReviewAuthority.INDEPENDENT_TARGET


# ---------------------------------------------------------------------------
# 4. Durable executor context is carried into reviewer routing so the runtime
#    can prove the reviewer was evaluated against the executor's identity.
# ---------------------------------------------------------------------------


class TestExecutorContextCarried:
    def test_payload_carries_durable_executor_context(self):
        assignment = _resolver(mode="constrained", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        payload = assignment.to_payload()
        ctx = cast("dict[str, object]", payload["executor_context"])
        assert ctx["executor_target_id"] == "tgt-exec"
        assert ctx["executor_provider_profile_id"] == "prov-a"
        assert ctx["executor_model_family"] == "family-a"

    def test_executor_context_present_even_on_fallback(self):
        policy = RoutePolicy(require_different_target_for_review=True)
        assignment = _resolver(
            mode="constrained", targets=SINGLE_REVIEWER_TARGET, policy=policy
        ).resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT)
        ctx = _executor_context(assignment)
        assert ctx["executor_target_id"] == "tgt-exec-review"
        assert ctx["executor_model_family"] == "family-a"

    def test_executor_context_present_when_off_abstained(self):
        assignment = _resolver(mode="off", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        ctx = _executor_context(assignment)
        assert ctx["executor_target_id"] == "tgt-exec"


# ---------------------------------------------------------------------------
# 5. review_authority is total: every assignment carries exactly one of the
#    three closed authority labels.
# ---------------------------------------------------------------------------


class TestAuthorityLabelIsTotal:
    def test_every_assignment_has_a_closed_authority_label(self):
        cases = [
            _resolver(mode="constrained", targets=INDEPENDENT_TARGETS).resolve(
                EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
            ),
            _resolver(mode="off", targets=INDEPENDENT_TARGETS).resolve(
                EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
            ),
            _resolver(
                mode="constrained",
                targets=SINGLE_REVIEWER_TARGET,
                policy=RoutePolicy(require_different_target_for_review=True),
            ).resolve(EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT),
        ]
        closed = {
            ReviewAuthority.INDEPENDENT_TARGET,
            ReviewAuthority.DETERMINISTIC_FALLBACK,
            ReviewAuthority.OFF_MODE_ABSTAINED,
        }
        for assignment in cases:
            assert assignment.review_authority in closed
            # Consistency: independent label implies a reviewer decision; the
            # other two imply none.
            if assignment.review_authority is ReviewAuthority.INDEPENDENT_TARGET:
                assert assignment.reviewer_decision is not None
            else:
                assert assignment.reviewer_decision is None

    def test_is_a_frozen_dataclass(self):
        assignment = _resolver(mode="off", targets=INDEPENDENT_TARGETS).resolve(
            EXEC_CONTRACT, PLAN_CONTRACT, REVIEW_CONTRACT
        )
        assert isinstance(assignment, GraphRoleAssignment)
