"""Deterministic qualification replay tests (Adaptive Flock plan, Task 12).

Replaying the exact ordered evidence set plus the config digest must produce
byte-identical projection digests on every repeat.  Any single drifting
projection blocks qualification with an explicit ``replay_drift`` reason;
database query order is never a replay input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nested_memvid_agent.routing.qualification_evaluator import (
    ScopeAttemptEvidence,
    ScopeEvaluationInput,
    evaluate_scope,
)
from nested_memvid_agent.routing.qualification_models import (
    QualificationScope,
    QualificationThresholds,
)
from nested_memvid_agent.routing.qualification_replay import (
    QualificationReplayer,
    ReplayResult,
)

FROZEN_AT = datetime(2026, 8, 2, tzinfo=UTC)


def frozen_clock() -> datetime:
    return FROZEN_AT


def drifting_clock() -> object:
    """Clock that drifts exactly once across twenty repeats."""

    calls = {"count": 0}

    def tick() -> datetime:
        calls["count"] += 1
        if calls["count"] == 1:
            return FROZEN_AT
        return FROZEN_AT + timedelta(seconds=1)

    return tick


def _scope() -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk="low",
        capabilities=("repository_inspection",),
        policy_id="balanced",
        policy_revision=1,
        target_ids=("target_a", "target_b"),
        target_inventory_digest="1" * 64,
        price_digest="2" * 64,
        learned_config_digest="3" * 64,
        project_authority_digest="4" * 64,
    )


def _attempt(
    case_id: str,
    target_id: str,
    ordinal: int,
    *,
    passed: bool = True,
    created_at: str = "2026-08-01T00:00:00+00:00",
) -> ScopeAttemptEvidence:
    return ScopeAttemptEvidence(
        attempt_id=f"att_{case_id}_{target_id}_{ordinal}",
        case_id=case_id,
        scope_digest=_scope().digest,
        target_id=target_id,
        attempt_ordinal=ordinal,
        validation_passed=passed,
        execution_status="succeeded" if passed else "failed",
        failure_category=None if passed else "provider_outage",
        actual_cost_usd=0.002,
        latency_seconds=0.25,
        guardrail_state="clear",
        evidence_kind="real_project",
        trusted_acceptance=passed,
        task_family="repository_inspection",
        risk="low",
        contract_digest="a" * 64,
        project_id="project-alpha",
        capability_key="repository_inspection",
        created_at=created_at,
    )


def evidence_fixture() -> ScopeEvaluationInput:
    """Exact ordered evidence for one scope, intentionally shuffled."""

    return ScopeEvaluationInput(
        scope=_scope(),
        static_target_id="target_a",
        thresholds=QualificationThresholds(),
        attempts=(
            _attempt("case_2", "target_b", 2),
            _attempt("case_1", "target_a", 1),
            _attempt("case_1", "target_b", 3),
            _attempt("case_2", "target_a", 4),
        ),
    )


@pytest.fixture
def replayer() -> QualificationReplayer:
    return QualificationReplayer(clock=frozen_clock)


def test_twenty_replays_are_identical(replayer: QualificationReplayer) -> None:
    result = replayer.replay((evidence_fixture(),), repeats=20)
    assert result.completed_repeats == 20
    assert result.unique_projection_digests == 1
    assert result.passed is True
    assert result.reasons == ()
    assert len(result.projection_digests) == 20
    assert len(set(result.projection_digests)) == 1


def test_single_projection_drift_blocks_scope() -> None:
    replayer = QualificationReplayer(clock=drifting_clock())  # type: ignore[arg-type]
    result = replayer.replay((evidence_fixture(),), repeats=20)
    assert result.passed is False
    assert result.unique_projection_digests == 2
    assert "replay_drift" in result.reasons


def test_replay_matches_direct_evaluation(replayer: QualificationReplayer) -> None:
    bundle = evidence_fixture()
    result = replayer.replay((bundle,), repeats=20)
    direct = evaluate_scope(bundle)
    assert result.results == (direct,)
    assert direct.projection_digest


def test_replay_is_independent_of_database_order(
    replayer: QualificationReplayer,
) -> None:
    bundle = evidence_fixture()
    reversed_bundle = ScopeEvaluationInput(
        scope=bundle.scope,
        static_target_id=bundle.static_target_id,
        thresholds=bundle.thresholds,
        attempts=tuple(reversed(bundle.attempts)),
    )
    first = replayer.replay((bundle,), repeats=20)
    second = replayer.replay((reversed_bundle,), repeats=20)
    assert first.projection_digests == second.projection_digests


def test_changed_evidence_changes_projection(replayer: QualificationReplayer) -> None:
    bundle = evidence_fixture()
    changed = ScopeEvaluationInput(
        scope=bundle.scope,
        static_target_id=bundle.static_target_id,
        thresholds=bundle.thresholds,
        attempts=bundle.attempts[:-1]
        + (
            _attempt(
                "case_2",
                "target_a",
                4,
                passed=False,
                created_at="2026-08-01T00:00:00+00:00",
            ),
        ),
    )
    assert (
        replayer.replay((bundle,), repeats=20).projection_digests[0]
        != (replayer.replay((changed,), repeats=20).projection_digests[0])
    )


def test_replay_rejects_naive_clock() -> None:
    replayer = QualificationReplayer(clock=lambda: datetime(2026, 8, 2))
    with pytest.raises(ValueError, match="aware datetime"):
        replayer.replay((evidence_fixture(),), repeats=1)


def test_replay_rejects_invalid_repeats(replayer: QualificationReplayer) -> None:
    with pytest.raises(ValueError, match="repeats must be a positive integer"):
        replayer.replay((evidence_fixture(),), repeats=0)


def test_replay_rejects_empty_bundles(replayer: QualificationReplayer) -> None:
    with pytest.raises(ValueError, match="at least one scope evaluation input"):
        replayer.replay((), repeats=20)


def test_incomplete_replay_never_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(bundle: ScopeEvaluationInput) -> object:
        raise RuntimeError("evaluation exploded")

    monkeypatch.setattr("nested_memvid_agent.routing.qualification_replay.evaluate_scope", boom)
    replayer = QualificationReplayer(clock=frozen_clock)
    result = replayer.replay((evidence_fixture(),), repeats=3)
    assert result.completed_repeats == 0
    assert result.passed is False
    assert "replay_incomplete" in result.reasons
    assert result.results == ()


def test_successes_required_is_honored() -> None:
    replayer = QualificationReplayer(clock=frozen_clock)
    result = replayer.replay((evidence_fixture(),), repeats=20, successes_required=18)
    assert isinstance(result, ReplayResult)
    assert result.passed is True
    assert result.successes_required == 18
