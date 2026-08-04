"""Exact per-scope qualification evaluation tests (Adaptive Flock plan, Task 11)."""
from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from nested_memvid_agent.routing.qualification_evaluator import (
    ScopeAttemptEvidence,
    ScopeEvaluationInput,
    evaluate_scope,
)
from nested_memvid_agent.routing.qualification_models import (
    EvidenceKind,
    QualificationScope,
    QualificationThresholds,
    RiskLevel,
)

STATIC_TARGET = "tgt-static"
LEARNED_TARGET = "tgt-learned"
STATIC_COST_USD = 1.0
LEARNED_COST_USD = 0.05
CREATED_AT = "2026-07-30T12:00:00Z"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _scope(risk: RiskLevel = "low") -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk=risk,
        capabilities=("repository_inspection",),
        policy_id="policy-1",
        policy_revision=1,
        target_ids=(LEARNED_TARGET, STATIC_TARGET),
        target_inventory_digest=_digest("inventory"),
        price_digest=_digest("price"),
        learned_config_digest=_digest("learned-config"),
        project_authority_digest=_digest("authority"),
    )


def _attempt(
    scope: QualificationScope,
    *,
    case_id: str,
    target_id: str,
    ordinal: int,
    cost_usd: float | None,
    guardrail_state: str = "clear",
    evidence_kind: EvidenceKind = "real_project",
    trusted_acceptance: bool = True,
) -> ScopeAttemptEvidence:
    return ScopeAttemptEvidence(
        attempt_id=f"attempt-{case_id}-{target_id}-{ordinal}",
        case_id=case_id,
        scope_digest=scope.digest,
        target_id=target_id,
        attempt_ordinal=ordinal,
        validation_passed=True,
        execution_status="completed",
        failure_category=None,
        actual_cost_usd=cost_usd,
        latency_seconds=1.0,
        guardrail_state=guardrail_state,
        evidence_kind=evidence_kind,
        trusted_acceptance=trusted_acceptance,
        task_family=scope.task_family,
        risk=scope.risk,
        contract_digest=_digest(f"contract-{case_id}"),
        project_id=scope.project_id,
        capability_key=scope.capability_key,
        created_at=CREATED_AT,
    )


def _base_attempts(scope: QualificationScope) -> list[ScopeAttemptEvidence]:
    attempts = [
        _attempt(
            scope,
            case_id=f"case-learned-{index}",
            target_id=LEARNED_TARGET,
            ordinal=1,
            cost_usd=LEARNED_COST_USD,
        )
        for index in range(7)
    ]
    attempts.extend(
        _attempt(
            scope,
            case_id=f"case-static-{index}",
            target_id=STATIC_TARGET,
            ordinal=1,
            cost_usd=STATIC_COST_USD,
        )
        for index in range(3)
    )
    return attempts


def _bundle(
    scope: QualificationScope,
    attempts: list[ScopeAttemptEvidence],
    *,
    thresholds: QualificationThresholds | None = None,
) -> ScopeEvaluationInput:
    return ScopeEvaluationInput(
        scope=scope,
        static_target_id=STATIC_TARGET,
        thresholds=thresholds or QualificationThresholds(),
        attempts=tuple(attempts),
    )


def mutated_evidence(mutation: str) -> ScopeEvaluationInput:
    """Build qualifying baseline evidence, then apply exactly one mutation."""
    risk: RiskLevel = "high" if mutation == "high_risk" else "low"
    scope = _scope(risk=risk)
    attempts = _base_attempts(scope)

    if mutation == "four_total_examples":
        attempts = attempts[:2] + attempts[7:9]
    elif mutation == "two_selected_target_examples":
        attempts = attempts[:2] + attempts[7:]
    elif mutation == "confidence_0_69":
        attempts = [
            _attempt(
                scope,
                case_id=f"case-learned-{index}",
                target_id=LEARNED_TARGET,
                ordinal=1,
                cost_usd=LEARNED_COST_USD,
            )
            for index in range(65)
        ] + [
            _attempt(
                scope,
                case_id=f"case-static-{index}",
                target_id=STATIC_TARGET,
                ordinal=1,
                cost_usd=STATIC_COST_USD,
            )
            for index in range(35)
        ]
    elif mutation == "margin_0_079":
        # 1/1.05 - 1/1.145 ~= 0.0790 utility delta, just below the 0.08 margin.
        attempts = attempts[:7] + [
            replace(attempt, actual_cost_usd=0.145) for attempt in attempts[7:]
        ]
    elif mutation == "cost_coverage_0_79":
        # Two of seven selected-target attempts have unresolved cost: 5/7 < 0.80.
        attempts = [
            replace(attempt, actual_cost_usd=None) if index < 2 else attempt
            for index, attempt in enumerate(attempts)
        ]
    elif mutation == "one_guardrail_violation":
        attempts[0] = replace(attempts[0], guardrail_state="violated")
    elif mutation == "one_target":
        attempts = attempts[:7]
    elif mutation == "fixture_only":
        attempts = [
            replace(
                attempt,
                evidence_kind="synthetic",
                trusted_acceptance=False,
            )
            for attempt in attempts
        ]
    elif mutation != "high_risk":
        raise ValueError(f"unknown mutation: {mutation}")
    return _bundle(scope, attempts)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("four_total_examples", "sparse_evidence"),
        ("two_selected_target_examples", "sparse_target_evidence"),
        ("confidence_0_69", "low_confidence"),
        ("margin_0_079", "insufficient_utility_margin"),
        ("cost_coverage_0_79", "insufficient_cost_coverage"),
        ("one_guardrail_violation", "guardrail_violation"),
        ("high_risk", "high_risk_deterministic_only"),
        ("one_target", "comparative_target_coverage_missing"),
        ("fixture_only", "real_project_evidence_required"),
    ],
)
def test_scope_abstains_with_exact_reason(mutation: str, reason: str) -> None:
    result = evaluate_scope(mutated_evidence(mutation))
    assert result.qualified is False
    assert reason in result.reasons


def test_qualifying_scope_is_qualified_with_transparent_metrics() -> None:
    scope = _scope()
    result = evaluate_scope(_bundle(scope, _base_attempts(scope)))

    assert result.qualified is True
    assert result.state == "qualified"
    assert result.reasons == ()
    assert result.scope_digest == scope.digest
    assert result.selected_target_id == LEARNED_TARGET
    assert result.static_target_id == STATIC_TARGET
    assert result.total_support == 10
    assert result.selected_target_support == 7
    assert result.confidence == pytest.approx(0.70)
    assert result.static_utility == pytest.approx(0.5)
    assert result.learned_utility == pytest.approx(1.0 / 1.05, abs=1e-8)
    assert result.utility_delta == pytest.approx(1.0 / 1.05 - 0.5, abs=1e-8)
    assert result.cost_coverage == pytest.approx(1.0)
    assert result.estimated_savings_usd == pytest.approx(STATIC_COST_USD - LEARNED_COST_USD)
    assert result.estimated_regret_usd == pytest.approx(0.0)
    assert result.guardrail_violations == 0
    assert result.evaluated_target_ids == (LEARNED_TARGET, STATIC_TARGET)
    assert result.thresholds_digest == QualificationThresholds().digest


def test_high_risk_scope_is_deterministic_only() -> None:
    result = evaluate_scope(mutated_evidence("high_risk"))

    assert result.state == "deterministic_only"
    assert result.qualified is False
    assert result.selected_target_id is None
    assert "high_risk_deterministic_only" in result.reasons


def test_abstained_scope_has_no_selected_learned_target() -> None:
    result = evaluate_scope(mutated_evidence("margin_0_079"))

    assert result.state == "abstained"
    assert result.selected_target_id is None


def test_fixture_only_scope_requires_trusted_real_project_acceptance() -> None:
    result = evaluate_scope(mutated_evidence("fixture_only"))

    assert "real_project_evidence_required" in result.reasons
    assert "trusted_acceptance_missing" in result.reasons


def test_evaluation_is_deterministic_and_order_independent() -> None:
    scope = _scope()
    attempts = _base_attempts(scope)
    shuffled = list(reversed(attempts))

    first = evaluate_scope(_bundle(scope, attempts))
    second = evaluate_scope(_bundle(scope, shuffled))

    assert first == second
    assert first.projection_digest == second.projection_digest
    assert first.to_payload() == second.to_payload()


def test_attempt_evidence_must_bind_to_the_scope() -> None:
    scope = _scope()
    attempts = _base_attempts(scope)
    foreign = replace(attempts[0], scope_digest=_digest("foreign-scope"))

    with pytest.raises(ValueError, match="does not bind to the qualification scope"):
        _bundle(scope, [foreign, *attempts[1:]])


def test_attempt_target_must_be_snapshotted_eligible_target() -> None:
    scope = _scope()
    attempts = _base_attempts(scope)
    outsider = replace(attempts[0], target_id="tgt-outsider")

    with pytest.raises(ValueError, match="not a snapshotted eligible target"):
        _bundle(scope, [outsider, *attempts[1:]])


def test_attempt_scope_fields_must_match_scope_binding() -> None:
    scope = _scope()
    attempts = _base_attempts(scope)
    mismatched = replace(attempts[0], project_id="project-beta")

    with pytest.raises(ValueError, match="does not bind to the qualification scope"):
        _bundle(scope, [mismatched, *attempts[1:]])


def test_empty_evidence_abstains_with_sparse_evidence() -> None:
    scope = _scope()
    result = evaluate_scope(_bundle(scope, []))

    assert result.qualified is False
    assert "sparse_evidence" in result.reasons
    assert "comparative_target_coverage_missing" in result.reasons


def test_guardrail_state_and_evidence_kind_are_validated() -> None:
    scope = _scope()
    with pytest.raises(ValueError, match="guardrail_state"):
        _attempt(
            scope,
            case_id="case-x",
            target_id=LEARNED_TARGET,
            ordinal=1,
            cost_usd=0.05,
            guardrail_state="unknown",
        )
    with pytest.raises(ValueError, match="evidence_kind"):
        _attempt(
            scope,
            case_id="case-x",
            target_id=LEARNED_TARGET,
            ordinal=1,
            cost_usd=0.05,
            evidence_kind="fixture",  # type: ignore[arg-type]
        )
