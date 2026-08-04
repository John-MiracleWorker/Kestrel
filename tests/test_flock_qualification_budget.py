"""Transactional hard-cap qualification budget tests (Adaptive Flock Task 7).

Admission is atomic (reserve and attempt transition in one SQLite
transaction), capped exactly against
``known_actual + unresolved + inflight + projected <= min(max, stop_cap)``,
and fail-closed: unknown prices reject before provider contact, missing
usage keeps the full reserve unresolved, overruns charge actual spend and
stop admissions, and synthetic fixtures never raise production cost
coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.routing.qualification_budget import (
    DEFAULT_IMMUTABLE_MAX_SPEND_MICROS,
    AttemptTokenCeilings,
    BudgetAdmissionRejected,
    QualificationBudget,
)
from nested_memvid_agent.routing.qualification_ledger import QualificationLedger
from nested_memvid_agent.routing.qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    PriceSnapshot,
    QualificationScope,
    QualificationThresholds,
)
from nested_memvid_agent.routing.qualification_records import (
    QualificationAttemptDraft,
    QualificationCaseDraft,
    QualificationRunDraft,
)
from nested_memvid_agent.state_store import AgentStateStore

CAPTURED_AT = "2026-08-02T00:00:00+00:00"


def _price(
    target_id: str,
    *,
    input_per_million: int = 1_000_000,
    output_per_million: int = 2_000_000,
) -> PriceSnapshot:
    return PriceSnapshot(
        target_id=target_id,
        source="operator_verified",
        captured_at=CAPTURED_AT,
        input_per_million=MoneyMicros(input_per_million),
        output_per_million=MoneyMicros(output_per_million),
    )


def _scope() -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk="low",
        capabilities=("repository_inspection",),
        policy_id="balanced",
        policy_revision=1,
        target_ids=("local-critic", "local-scout"),
        target_inventory_digest="1" * 64,
        price_digest="2" * 64,
        learned_config_digest="3" * 64,
        project_authority_digest="4" * 64,
    )


def _corpus(evidence_kind: str = "real_project") -> CorpusManifest:
    return CorpusManifest(
        schema_version=1,
        items=(
            CorpusItem(
                item_id="corpus_item_1",
                task_family="repository_inspection",
                risk="low",
                capabilities=("repository_inspection",),
                task_contract_digest="a" * 64,
                acceptance_plan_digest="b" * 64,
                evidence_kind=evidence_kind,  # type: ignore[arg-type]
            ),
        ),
    )


def _run_draft(evidence_kind: str = "real_project") -> QualificationRunDraft:
    return QualificationRunDraft(
        run_id="qual_run_1",
        owner_principal="owner@example.test",
        scope=_scope(),
        corpus=_corpus(evidence_kind),
        thresholds=QualificationThresholds(),
        target_snapshot={"targets": ["local-critic", "local-scout"]},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
        project_authority={"principal": "owner@example.test"},
        build={"version": "0.5.0", "git": "bd2c182"},
        max_spend=MoneyMicros(DEFAULT_IMMUTABLE_MAX_SPEND_MICROS),
        effective_stop_cap=MoneyMicros(DEFAULT_IMMUTABLE_MAX_SPEND_MICROS),
        attempt_ceiling=MoneyMicros(DEFAULT_IMMUTABLE_MAX_SPEND_MICROS),
    )


def _make_budget(root: Path, evidence_kind: str = "real_project") -> QualificationBudget:
    state = AgentStateStore(root / "state" / "agent.db")
    ledger = QualificationLedger(state)
    run = ledger.create_run(_run_draft(evidence_kind))
    ready = ledger.mark_ready(run.run_id, expected_revision=run.revision)
    ledger.mark_running(ready.run_id, expected_revision=ready.revision)
    ledger.create_case(
        QualificationCaseDraft(
            case_id="qual_case_1",
            run_id=run.run_id,
            item=_corpus(evidence_kind).items[0],
            repository_digest="c" * 64,
            privacy_eligible=True,
        )
    )
    return QualificationBudget(
        ledger,
        run.run_id,
        prices={
            "local-scout": _price("local-scout"),
            "local-critic": _price("local-critic"),
        },
        token_ceilings=AttemptTokenCeilings(
            max_input_tokens=1_000_000,
            max_output_tokens=1_000_000,
        ),
    )


@pytest.fixture
def budget(tmp_path: Path) -> QualificationBudget:
    return _make_budget(tmp_path)


def _attempt(
    attempt_id: str,
    attempt_number: int,
    *,
    reserve: int,
    target_id: str = "local-scout",
) -> QualificationAttemptDraft:
    return QualificationAttemptDraft(
        attempt_id=attempt_id,
        case_id="qual_case_1",
        attempt_number=attempt_number,
        target_id=target_id,
        target_digest="d" * 64,
        reservation=MoneyMicros(reserve),
    )


# --- Plan Step 1: exact-exhaustion and unknown-usage tests -------------------


def test_exact_cap_allows_equal_reservation_and_rejects_next(
    budget: QualificationBudget,
) -> None:
    budget.admit_attempt(_attempt("a", 1, reserve=40_000_000))
    budget.admit_attempt(_attempt("b", 2, reserve=10_000_000))
    state = budget.state()
    assert state.admitted_inflight_reserve_micros == 50_000_000
    with pytest.raises(BudgetAdmissionRejected, match="hard_cap_exhausted"):
        budget.admit_attempt(_attempt("c", 3, reserve=1))


def test_missing_usage_keeps_reservation_as_unresolved_cost(
    budget: QualificationBudget,
) -> None:
    budget.admit_attempt(_attempt("a", 1, reserve=2_500_000))
    state = budget.settle_attempt("a", usage=None, actual_cost=None)
    assert state.known_actual_spend_micros == 0
    assert state.unresolved_cost_reserve_micros == 2_500_000
    assert state.cost_coverage == 0.0


def test_owner_cannot_raise_cap_after_start(budget: QualificationBudget) -> None:
    with pytest.raises(ValueError, match="cannot be raised"):
        budget.lower_effective_stop_cap(MoneyMicros(60_000_000))


# --- Admission atomicity and reserve projection -------------------------------


def test_rejected_admission_leaves_no_attempt_and_no_reserve(
    budget: QualificationBudget,
) -> None:
    budget.admit_attempt(_attempt("a", 1, reserve=50_000_000))
    with pytest.raises(BudgetAdmissionRejected, match="hard_cap_exhausted"):
        budget.admit_attempt(_attempt("b", 2, reserve=1))
    assert budget.ledger.get_attempt("b") is None
    state = budget.state()
    assert state.admitted_inflight_reserve_micros == 50_000_000
    assert state.known_actual_spend_micros == 0
    assert state.unresolved_cost_reserve_micros == 0


def test_estimate_attempt_reserve_rounds_up_from_snapshotted_prices(
    budget: QualificationBudget,
) -> None:
    # 1_000_000 in * 1 USD/M + 1_000_000 out * 2 USD/M = exactly 3 USD.
    assert budget.estimate_attempt_reserve("local-scout") == MoneyMicros(3_000_000)

    fractional = QualificationBudget(
        budget.ledger,
        budget.run_id,
        prices={
            "local-scout": _price(
                "local-scout", input_per_million=333_333, output_per_million=1
            )
        },
        token_ceilings=AttemptTokenCeilings(max_input_tokens=3, max_output_tokens=1),
    )
    # ceil(999_999 / 1e6) + ceil(1 / 1e6) = 1 + 1 = 2 micro-USD.
    assert fractional.estimate_attempt_reserve("local-scout") == MoneyMicros(2)


def test_non_billed_local_price_yields_zero_known_reserve(
    budget: QualificationBudget,
) -> None:
    zero_priced = QualificationBudget(
        budget.ledger,
        budget.run_id,
        prices={
            "local-scout": PriceSnapshot(
                target_id="local-scout",
                source="operator_confirmed_non_billed_local",
                captured_at=CAPTURED_AT,
                input_per_million=MoneyMicros(0),
                output_per_million=MoneyMicros(0),
                confirmed_by="owner@example.test",
                confirmed_at=CAPTURED_AT,
            )
        },
        token_ceilings=AttemptTokenCeilings(
            max_input_tokens=1_000_000,
            max_output_tokens=1_000_000,
        ),
    )
    assert zero_priced.estimate_attempt_reserve("local-scout") == MoneyMicros(0)


def test_unknown_price_rejects_admission_before_provider_contact(
    budget: QualificationBudget,
) -> None:
    unknown = QualificationBudget(
        budget.ledger,
        budget.run_id,
        prices={
            "local-scout": PriceSnapshot(
                target_id="local-scout",
                source="unknown",
                captured_at=CAPTURED_AT,
            )
        },
        token_ceilings=AttemptTokenCeilings(
            max_input_tokens=1_000_000,
            max_output_tokens=1_000_000,
        ),
    )
    with pytest.raises(BudgetAdmissionRejected, match="price_unknown"):
        unknown.estimate_attempt_reserve("local-scout")
    with pytest.raises(BudgetAdmissionRejected, match="price_unknown"):
        unknown.admit_attempt(_attempt("a", 1, reserve=0))
    assert unknown.ledger.get_attempt("a") is None


# --- Settlement semantics ------------------------------------------------------


def test_settle_replaces_reserve_with_exact_rounded_up_actual(
    budget: QualificationBudget,
) -> None:
    budget.admit_attempt(_attempt("a", 1, reserve=3_000_000))
    budget.ledger.mark_attempt_running("a")
    state = budget.settle_attempt(
        "a",
        usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        actual_cost=None,
    )
    assert state.known_actual_spend_micros == 3_000_000
    assert state.admitted_inflight_reserve_micros == 0
    assert state.unresolved_cost_reserve_micros == 0
    assert state.cost_coverage == 1.0
    attempt = budget.ledger.get_attempt("a")
    assert attempt is not None
    assert attempt.status == "completed"
    assert attempt.actual_cost == MoneyMicros(3_000_000)


def test_actual_above_reserve_records_overrun_and_stops_admissions(
    budget: QualificationBudget,
) -> None:
    budget.admit_attempt(_attempt("a", 1, reserve=1_000_000))
    budget.ledger.mark_attempt_running("a")
    state = budget.settle_attempt(
        "a",
        usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        actual_cost=None,
    )
    # Actual (3 USD) is charged even though it exceeds the 1 USD reserve.
    assert state.known_actual_spend_micros == 3_000_000
    assert state.admitted_inflight_reserve_micros == 0

    events = budget.ledger.list_events(budget.run_id)
    overruns = [e for e in events if e.event_type == "budget_projection_overrun"]
    assert len(overruns) == 1
    assert overruns[0].payload["attempt_id"] == "a"
    assert overruns[0].payload["reserve_micros"] == 1_000_000
    assert overruns[0].payload["actual_micros"] == 3_000_000

    # The overrun makes the affected scope non-qualifying.
    attempt = budget.ledger.get_attempt("a")
    assert attempt is not None
    assert attempt.validation_passed is False
    assert "budget_projection_overrun" in attempt.validation_codes

    # Admissions are stopped fail-closed after the overrun.
    assert state.admissions_open is False
    with pytest.raises(BudgetAdmissionRejected, match="hard_cap_exhausted"):
        budget.admit_attempt(_attempt("b", 2, reserve=1))


def test_confirmed_zero_token_transport_failure_releases_reserve(
    budget: QualificationBudget,
) -> None:
    budget.admit_attempt(_attempt("a", 1, reserve=2_500_000))
    state = budget.settle_attempt(
        "a",
        usage=None,
        actual_cost=None,
        transport_failure=True,
        zero_tokens_confirmed=True,
    )
    assert state.known_actual_spend_micros == 0
    assert state.admitted_inflight_reserve_micros == 0
    assert state.unresolved_cost_reserve_micros == 0
    attempt = budget.ledger.get_attempt("a")
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.failure_category == "transport_failed_no_request_accepted"
    # Confirmed zero tokens is attributable known (zero) cost.
    assert state.cost_coverage == 1.0


def test_ambiguous_transport_outcome_keeps_reserve_unresolved(
    budget: QualificationBudget,
) -> None:
    budget.admit_attempt(_attempt("a", 1, reserve=2_500_000))
    state = budget.settle_attempt(
        "a",
        usage=None,
        actual_cost=None,
        transport_failure=True,
        zero_tokens_confirmed=False,
    )
    assert state.known_actual_spend_micros == 0
    assert state.admitted_inflight_reserve_micros == 0
    assert state.unresolved_cost_reserve_micros == 2_500_000
    assert state.cost_coverage == 0.0
    attempt = budget.ledger.get_attempt("a")
    assert attempt is not None
    assert attempt.status == "ambiguous"


def test_synthetic_attempts_do_not_increase_cost_coverage(tmp_path: Path) -> None:
    synthetic = _make_budget(tmp_path / "synthetic", evidence_kind="synthetic")
    synthetic.admit_attempt(_attempt("a", 1, reserve=3_000_000))
    synthetic.ledger.mark_attempt_running("a")
    state = synthetic.settle_attempt(
        "a",
        usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        actual_cost=None,
    )
    # Spend is still tracked exactly, but synthetic fixtures are not live
    # production attempt units and cannot raise cost coverage.
    assert state.known_actual_spend_micros == 3_000_000
    assert state.cost_coverage == 0.0


# --- Stop-cap tightening -------------------------------------------------------


def test_lowered_stop_cap_tightens_admission(budget: QualificationBudget) -> None:
    state = budget.lower_effective_stop_cap(MoneyMicros(40_000_000))
    assert state.effective_stop_cap_micros == 40_000_000
    budget.admit_attempt(_attempt("a", 1, reserve=40_000_000))
    with pytest.raises(BudgetAdmissionRejected, match="hard_cap_exhausted"):
        budget.admit_attempt(_attempt("b", 2, reserve=1))
