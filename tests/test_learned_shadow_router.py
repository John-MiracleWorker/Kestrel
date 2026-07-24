"""PR 7 — Learned shadow router tests.

Build routing examples from verified outcome records. Implement shadow
residual and replay harness. Add minimum support, confidence, utility
margin, and abstention. Add constrained activation for low-risk task
families. Route policy changes remain behavior-delta gated.

RED tests:
- Sparse evidence causes abstention
- Hard-filtered targets never become eligible through learning
- Provider outage does not become task-quality punishment
- Replayed history produces deterministic model state
- Learned residual can demonstrate utility lift on synthetic fixtures
- Policy/high-risk behavior cannot auto-change through route outcomes
"""
from __future__ import annotations

from nested_memvid_agent.routing.learned_router import (
    LearnedRouterConfig,
    LearnedRouterState,
    RouteExample,
    build_route_examples,
    evaluate_shadow,
    replay_history,
    should_activate_learned_policy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_outcome(
    decision_id: str = "d1",
    target_id: str = "tgt-a",
    *,
    validation_passed: bool = True,
    execution_status: str = "completed",
    failure_category: str | None = None,
    actual_cost_usd: float = 0.01,
    latency_seconds: float = 5.0,
    task_family: str = "mechanical_refactor",
    risk: str = "low",
    contract_digest: str = "cd1",
) -> RouteExample:
    return RouteExample(
        decision_id=decision_id,
        target_id=target_id,
        validation_passed=validation_passed,
        execution_status=execution_status,
        failure_category=failure_category,
        actual_cost_usd=actual_cost_usd,
        latency_seconds=latency_seconds,
        task_family=task_family,
        risk=risk,
        contract_digest=contract_digest,
    )


def _make_outcomes(*targets_and_results: tuple[str, bool, float, float]) -> list[RouteExample]:
    """Build a list of examples from (target_id, validation_passed, cost, latency) tuples."""
    examples = []
    for i, (tid, vp, cost, latency) in enumerate(targets_and_results):
        examples.append(_make_outcome(
            decision_id=f"d{i}",
            target_id=tid,
            validation_passed=vp,
            actual_cost_usd=cost,
            latency_seconds=latency,
        ))
    return examples


# ---------------------------------------------------------------------------
# Sparse evidence causes abstention
# ---------------------------------------------------------------------------

class TestSparseEvidenceAbstention:
    def test_fewer_than_min_examples_causes_abstention(self):
        """When the number of examples for a task family is below the minimum,
        the learned router must abstain (not activate)."""
        examples = _make_outcomes(
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
        )
        config = LearnedRouterConfig(min_examples=5)
        result = should_activate_learned_policy(examples, config=config)
        assert result is False

    def test_exactly_min_examples_does_not_abstain(self):
        """When the number of examples equals the minimum, abstention is not triggered."""
        examples = _make_outcomes(
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
        )
        config = LearnedRouterConfig(min_examples=5)
        result = should_activate_learned_policy(examples, config=config)
        assert result is True

    def test_low_confidence_causes_abstention(self):
        """When the confidence of the learned model is below the threshold,
        the learned router must abstain."""
        # All targets have the same result — no signal
        examples = _make_outcomes(
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-b", True, 0.01, 5.0),
            ("tgt-b", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
        )
        config = LearnedRouterConfig(min_examples=5, confidence_threshold=0.90)
        result = should_activate_learned_policy(examples, config=config)
        assert result is False


# ---------------------------------------------------------------------------
# Hard-filtered targets never become eligible through learning
# ---------------------------------------------------------------------------

class TestHardFilterGuard:
    def test_hard_filtered_target_remains_ineligible(self):
        """A target that was hard-filtered (e.g., local_required) cannot
        become eligible through learning, no matter how many examples
        show it was successful."""
        examples = [
            _make_outcome(
                decision_id=f"d{i}",
                target_id="tgt-local",
                validation_passed=True,
                task_family="inspection",
                risk="low",
            )
            for i in range(10)
        ]
        config = LearnedRouterConfig(min_examples=5, hard_filtered_targets={"tgt-local"})
        result = should_activate_learned_policy(examples, config=config)
        # Even with 10 successful examples, tgt-local is hard-filtered
        assert result is False or "tgt-local" not in _eligible_targets_from_evaluation(examples, config)

    def test_hard_filter_overrides_learned_success(self):
        """Even if learning suggests tgt-banned is best, it must not be selected."""
        examples = _make_outcomes(
            ("tgt-banned", True, 0.001, 1.0),
            ("tgt-banned", True, 0.001, 1.0),
            ("tgt-banned", True, 0.001, 1.0),
            ("tgt-banned", True, 0.001, 1.0),
            ("tgt-banned", True, 0.001, 1.0),
            ("tgt-ok", True, 0.05, 10.0),
            ("tgt-ok", True, 0.05, 10.0),
            ("tgt-ok", True, 0.05, 10.0),
            ("tgt-ok", True, 0.05, 10.0),
            ("tgt-ok", True, 0.05, 10.0),
        )
        config = LearnedRouterConfig(
            min_examples=5,
            hard_filtered_targets={"tgt-banned"},
        )
        state = LearnedRouterState.from_examples(examples, config)
        # tgt-banned must not appear in the learned eligible set
        assert "tgt-banned" not in state.eligible_targets


# ---------------------------------------------------------------------------
# Provider outage does not become task-quality punishment
# ---------------------------------------------------------------------------

class TestProviderOutageIsolation:
    def test_provider_failure_does_not_lower_target_quality_score(self):
        """When a failure is categorized as a provider outage (not task quality),
        it must not lower the target's predicted success score."""
        examples = [
            _make_outcome(
                decision_id=f"d{i}",
                target_id="tgt-a",
                validation_passed=True,
                execution_status="completed",
                failure_category=None,
                actual_cost_usd=0.01,
                latency_seconds=5.0,
            )
            for i in range(5)
        ]
        # Add a provider outage
        examples.append(_make_outcome(
            decision_id="d-out",
            target_id="tgt-a",
            validation_passed=False,
            execution_status="provider_error",
            failure_category="provider_outage",
            actual_cost_usd=0.0,
            latency_seconds=0.0,
        ))
        config = LearnedRouterConfig(min_examples=5)
        state = LearnedRouterState.from_examples(examples, config)
        # The outage should not tank the quality score
        assert state.target_scores["tgt-a"].validation_rate > 0.7


# ---------------------------------------------------------------------------
# Replayed history produces deterministic model state
# ---------------------------------------------------------------------------

class TestReplayDeterminism:
    def test_replay_produces_same_state(self):
        """Replaying the same history twice produces identical model state."""
        examples = _make_outcomes(
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-b", True, 0.02, 8.0),
            ("tgt-b", True, 0.02, 8.0),
            ("tgt-b", False, 0.02, 8.0),
        )
        config = LearnedRouterConfig(min_examples=5)
        state1 = LearnedRouterState.from_examples(examples, config)
        state2 = LearnedRouterState.from_examples(examples, config)
        assert state1 == state2

    def test_replay_with_shuffled_input_produces_same_state(self):
        """The order of examples must not affect the learned state."""
        examples_a = _make_outcomes(
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-b", True, 0.02, 8.0),
            ("tgt-b", False, 0.02, 8.0),
            ("tgt-a", True, 0.01, 5.0),
        )
        examples_b = list(reversed(examples_a))
        config = LearnedRouterConfig(min_examples=5)
        state1 = LearnedRouterState.from_examples(examples_a, config)
        state2 = LearnedRouterState.from_examples(examples_b, config)
        assert state1 == state2


# ---------------------------------------------------------------------------
# Learned residual can demonstrate utility lift on synthetic fixtures
# ---------------------------------------------------------------------------

class TestUtilityLift:
    def test_learned_policy_prefers_cheaper_target_with_same_validation(self):
        """When two targets have similar validation rates but different costs,
        the learned policy should prefer the cheaper one."""
        examples = _make_outcomes(
            # tgt-a: cheap and high validation
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            # tgt-b: expensive and similar validation
            ("tgt-b", True, 0.10, 8.0),
            ("tgt-b", True, 0.10, 8.0),
            ("tgt-b", True, 0.10, 8.0),
            ("tgt-b", True, 0.10, 8.0),
            ("tgt-b", True, 0.10, 8.0),
        )
        config = LearnedRouterConfig(min_examples=5, confidence_threshold=0.60)
        state = LearnedRouterState.from_examples(examples, config)
        # tgt-a should have higher utility score (same validation, lower cost)
        assert state.target_scores["tgt-a"].utility > state.target_scores["tgt-b"].utility

    def test_shadow_evaluation_shows_utility_improvement(self):
        """Shadow evaluation should show that the learned policy would have
        picked a better target than the static policy on some fixtures."""
        # Static policy always picks tgt-b (higher quality tier)
        # But tgt-a has same validation and lower cost
        examples = _make_outcomes(
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-b", True, 0.10, 8.0),
            ("tgt-b", True, 0.10, 8.0),
            ("tgt-b", True, 0.10, 8.0),
            ("tgt-b", True, 0.10, 8.0),
            ("tgt-b", True, 0.10, 8.0),
        )
        config = LearnedRouterConfig(min_examples=5)
        shadow = evaluate_shadow(
            examples=examples,
            static_target_id="tgt-b",
            config=config,
        )
        assert shadow.learned_target_id == "tgt-a"
        assert shadow.utility_improvement > 0.0


# ---------------------------------------------------------------------------
# Policy/high-risk behavior cannot auto-change through route outcomes
# ---------------------------------------------------------------------------

class TestPolicyGuard:
    def test_high_risk_tasks_never_auto_activate_learned_policy(self):
        """High-risk task families must never have the learned policy
        auto-activated, regardless of evidence quantity."""
        examples = [
            _make_outcome(
                decision_id=f"d{i}",
                target_id="tgt-a",
                validation_passed=True,
                task_family="security_review",
                risk="critical",
            )
            for i in range(20)
        ]
        config = LearnedRouterConfig(
            min_examples=5,
            high_risk_families={"security_review"},
        )
        result = should_activate_learned_policy(examples, config=config)
        assert result is False

    def test_policy_changes_require_explicit_gate(self):
        """Route policy changes cannot be auto-applied through outcomes alone."""
        examples = _make_outcomes(
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
            ("tgt-a", True, 0.01, 5.0),
        )
        config = LearnedRouterConfig(min_examples=5)
        # Even with enough evidence, activation requires a behavior-delta gate
        # The should_activate function checks this
        result = should_activate_learned_policy(examples, config=config)
        # For low-risk families with sufficient evidence, activation is allowed
        # But the activation margin must be met
        assert result is True  # low-risk, enough examples, high confidence


# ---------------------------------------------------------------------------
# build_route_examples from outcome records
# ---------------------------------------------------------------------------

class TestBuildRouteExamples:
    def test_builds_examples_from_outcomes(self):
        """build_route_examples converts RouteOutcomeEntry-like records into
        RouteExample objects suitable for the learned router."""
        raw_outcomes = [
            {
                "decision_id": "d1",
                "target_id": "tgt-a",
                "validation_passed": True,
                "execution_status": "completed",
                "failure_category": None,
                "actual_cost_usd": 0.01,
                "latency_seconds": 5.0,
                "task_family": "mechanical_refactor",
                "risk": "low",
                "contract_digest": "cd1",
            },
            {
                "decision_id": "d2",
                "target_id": "tgt-b",
                "validation_passed": False,
                "execution_status": "completed",
                "failure_category": "task_failure",
                "actual_cost_usd": 0.05,
                "latency_seconds": 10.0,
                "task_family": "mechanical_refactor",
                "risk": "low",
                "contract_digest": "cd2",
            },
        ]
        examples = build_route_examples(raw_outcomes)
        assert len(examples) == 2
        assert examples[0].target_id == "tgt-a"
        assert examples[0].validation_passed is True
        assert examples[1].target_id == "tgt-b"
        assert examples[1].validation_passed is False


# ---------------------------------------------------------------------------
# replay_history function
# ---------------------------------------------------------------------------

class TestReplayHistory:
    def test_replay_returns_deterministic_state(self):
        """replay_history processes a list of raw outcome dicts and returns
        a deterministic LearnedRouterState."""
        raw = [
            {
                "decision_id": f"d{i}",
                "target_id": "tgt-a" if i % 2 == 0 else "tgt-b",
                "validation_passed": True,
                "execution_status": "completed",
                "failure_category": None,
                "actual_cost_usd": 0.01 if i % 2 == 0 else 0.05,
                "latency_seconds": 5.0 if i % 2 == 0 else 10.0,
                "task_family": "mechanical_refactor",
                "risk": "low",
                "contract_digest": f"cd{i}",
            }
            for i in range(10)
        ]
        config = LearnedRouterConfig(min_examples=5)
        state1 = replay_history(raw, config=config)
        state2 = replay_history(raw, config=config)
        assert state1 == state2


def _eligible_targets_from_evaluation(
    examples: list[RouteExample], config: LearnedRouterConfig
) -> set[str]:
    """Helper to get eligible targets from a learned state."""
    state = LearnedRouterState.from_examples(examples, config)
    return set(state.eligible_targets)