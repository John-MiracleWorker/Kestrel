"""Authenticated terminal qualification receipt tests (Adaptive Flock plan, Task 12).

Every terminal run (``completed``, ``failed``, or ``cancelled``) records one
immutable authenticated receipt.  Only a ``completed`` receipt may contain
qualified scopes, qualification requires one unique replay projection digest
across twenty passes, and a receipt whose signing fails is never persisted:
no unsigned qualifying receipt is ever invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.control_plane_integrity import (
    ROUTING_INTEGRITY_KEY_NAME,
    ControlPlaneIntegrity,
    RoutingIntegrityError,
)
from nested_memvid_agent.routing.learned_router import LearnedRouterState
from nested_memvid_agent.routing.qualification_digest import canonical_digest
from nested_memvid_agent.routing.qualification_evaluator import ScopeQualificationResult
from nested_memvid_agent.routing.qualification_ledger import QualificationLedger
from nested_memvid_agent.routing.qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    QualificationScope,
    QualificationThresholds,
)
from nested_memvid_agent.routing.qualification_receipt import (
    TERMINAL_RECEIPT_SCHEMA,
    authenticate_terminal_receipt,
    build_terminal_receipt,
    verify_terminal_receipt,
)
from nested_memvid_agent.routing.qualification_records import (
    QualificationRun,
    QualificationRunDraft,
)
from nested_memvid_agent.routing.qualification_replay import ReplayResult
from nested_memvid_agent.state_store import AgentStateStore

CAPTURED_AT = "2026-08-02T00:00:00+00:00"


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


def _run_draft() -> QualificationRunDraft:
    scope = _scope()
    return QualificationRunDraft(
        run_id="qual_receipt",
        owner_principal="owner@example.test",
        scope=scope,
        corpus=CorpusManifest(
            schema_version=1,
            items=(
                CorpusItem(
                    item_id="corpus_item_1",
                    task_family="repository_inspection",
                    risk="low",
                    capabilities=("repository_inspection",),
                    task_contract_digest="a" * 64,
                    acceptance_plan_digest="b" * 64,
                    evidence_kind="real_project",
                ),
            ),
        ),
        thresholds=QualificationThresholds(),
        target_snapshot={"targets": list(scope.target_ids)},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
        project_authority={"principal": "owner@example.test"},
        build={"version": "0.5.0", "git": "bd2c182"},
        max_spend=MoneyMicros.from_usd_text("50.00"),
        effective_stop_cap=MoneyMicros.from_usd_text("25.00"),
        attempt_ceiling=MoneyMicros.from_usd_text("5.00"),
    )


def _result(state: str, *, selected: str | None = None) -> ScopeQualificationResult:
    return ScopeQualificationResult(
        scope_digest=_scope().digest,
        state=state,  # type: ignore[arg-type]
        static_target_id="target_a",
        selected_target_id=selected,
        total_support=10,
        selected_target_support=5,
        confidence=0.9,
        static_utility=0.5,
        learned_utility=0.7,
        utility_delta=0.2,
        cost_coverage=0.9,
        estimated_savings_usd=0.001,
        estimated_regret_usd=None,
        guardrail_violations=0,
        evaluated_target_ids=("target_a", "target_b"),
        reasons=() if state == "qualified" else ("sparse_evidence",),
        router_state=LearnedRouterState(config_digest="6" * 64),
        thresholds_digest=QualificationThresholds().digest,
    )


def qualified_scope() -> ScopeQualificationResult:
    return _result("qualified", selected="target_b")


def _replay(passed: bool, results: tuple[ScopeQualificationResult, ...] = ()) -> ReplayResult:
    digests = ("c" * 64,) * 20 if passed else ("c" * 64,) * 19 + ("d" * 64,)
    return ReplayResult(
        repeats=20,
        completed_repeats=20,
        successes_required=20,
        projection_digests=digests,
        results=results,
        reasons=() if passed else ("replay_drift",),
    )


@pytest.fixture
def state(tmp_path: Path) -> AgentStateStore:
    return AgentStateStore(tmp_path / "state" / "agent.db")


@pytest.fixture
def qualification_ledger(state: AgentStateStore) -> QualificationLedger:
    return QualificationLedger(state)


@pytest.fixture
def run(qualification_ledger: QualificationLedger) -> QualificationRun:
    return qualification_ledger.create_run(_run_draft())


# --- qualified-scope gating -----------------------------------------------------


def test_cancelled_receipt_cannot_contain_qualified_scope() -> None:
    with pytest.raises(ValueError, match="cancelled receipt"):
        build_terminal_receipt(status="cancelled", scopes=[qualified_scope()])


def test_failed_receipt_cannot_contain_qualified_scope() -> None:
    with pytest.raises(ValueError, match="failed receipt"):
        build_terminal_receipt(status="failed", scopes=[qualified_scope()])


def test_qualified_scope_requires_a_passed_replay(run: QualificationRun) -> None:
    with pytest.raises(ValueError, match="passed replay"):
        build_terminal_receipt(status="completed", run=run, scopes=[qualified_scope()])
    with pytest.raises(ValueError, match="passed replay"):
        build_terminal_receipt(
            status="completed",
            run=run,
            scopes=[qualified_scope()],
            replay=_replay(False, (qualified_scope(),)),
        )


def test_receipt_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="unsupported terminal status"):
        build_terminal_receipt(status="running")


# --- payload structure -----------------------------------------------------------


def test_completed_receipt_contains_every_required_section(run: QualificationRun) -> None:
    replay = _replay(True, (qualified_scope(),))
    receipt = build_terminal_receipt(
        status="completed",
        run=run,
        terminal_reason="matrix_exhausted",
        scopes=replay.results,
        replay=replay,
        attempt_summaries=(
            {
                "attempt_id": "att_1",
                "case_id": "case_1",
                "target_id": "target_a",
                "status": "completed",
                "validation_passed": True,
                "failure_category": None,
                "guardrail_state": "clear",
                "actual_cost_micros": 2000,
                "evidence_refs": ["workspace:lease-1"],
            },
        ),
        effective_cap_revisions=({"revision": 1, "effective_stop_cap_micros": 25_000_000},),
        details={"matrix_exhausted": True},
    )
    assert receipt["schema"] == TERMINAL_RECEIPT_SCHEMA
    assert receipt["status"] == "completed"
    assert receipt["terminal_reason"] == "matrix_exhausted"
    assert receipt["qualifying"] is True
    run_section = receipt["run"]
    assert run_section["run_id"] == "qual_receipt"
    assert run_section["owner_principal"] == "owner@example.test"
    assert run_section["project_id"] == "project-alpha"
    assert run_section["build"] == {"version": "0.5.0", "git": "bd2c182"}
    assert run_section["build_digest"] == run.build_digest
    assert run_section["created_at"] == run.created_at
    digests = receipt["digests"]
    for name in (
        "scope",
        "corpus",
        "target",
        "price",
        "policy",
        "learned",
        "project_authority",
        "thresholds",
    ):
        assert digests[name] == getattr(run, f"{name}_digest")
    caps = receipt["caps"]
    assert caps["max_spend_micros"] == 50_000_000
    assert caps["effective_stop_cap_micros"] == 25_000_000
    assert caps["effective_cap_revisions"] == [
        {"revision": 1, "effective_stop_cap_micros": 25_000_000}
    ]
    spend = receipt["spend"]
    assert spend["actual_spend_micros"] == run.actual_spend.micros
    assert spend["unresolved_reserve_micros"] == run.unresolved_reserve.micros
    assert receipt["attempts_terminal"] == 1
    assert receipt["attempts_succeeded"] == 1
    assert receipt["attempts"][0]["evidence_refs"] == ["workspace:lease-1"]
    assert receipt["failure_summary"] == {}
    assert receipt["guardrail_violations"] == 0
    assert receipt["scopes"][0]["state"] == "qualified"
    assert receipt["scopes"][0]["selected_target_id"] == "target_b"
    assert receipt["replay"]["unique_projection_digests"] == 1
    assert len(receipt["replay"]["projection_digests"]) == 20
    assert receipt["details"] == {"matrix_exhausted": True}


def test_abstained_completed_receipt_is_not_qualifying(run: QualificationRun) -> None:
    replay = _replay(True, (_result("abstained"),))
    receipt = build_terminal_receipt(
        status="completed", run=run, scopes=replay.results, replay=replay
    )
    assert receipt["status"] == "completed"
    assert receipt["qualifying"] is False
    assert receipt["scopes"][0]["state"] == "abstained"


def test_cancelled_receipt_preserves_evidence_without_scopes(run: QualificationRun) -> None:
    receipt = build_terminal_receipt(
        status="cancelled",
        run=run,
        terminal_reason="cancelled_by_owner",
        details={"evidence_preserved": True},
    )
    assert receipt["qualifying"] is False
    assert receipt["scopes"] == []
    assert receipt["replay"] is None
    assert receipt["details"]["evidence_preserved"] is True


# --- authentication envelope -------------------------------------------------------


def test_authentication_roundtrip_and_tamper_detection(tmp_path: Path) -> None:
    integrity = ControlPlaneIntegrity(tmp_path)
    receipt = build_terminal_receipt(status="cancelled", terminal_reason="cancelled_by_owner")
    authenticated = authenticate_terminal_receipt(receipt, integrity=integrity)
    assert authenticated["payload_digest"] == canonical_digest(receipt)
    envelope = authenticated["authentication"]
    assert envelope["algorithm"] == "hmac-sha256"
    assert envelope["key_id"] == integrity.key_id
    assert verify_terminal_receipt(authenticated, integrity=integrity) is True
    tampered = dict(authenticated)
    tampered["terminal_reason"] = "forged"
    assert verify_terminal_receipt(tampered, integrity=integrity) is False
    stripped = {key: value for key, value in authenticated.items() if key != "authentication"}
    assert verify_terminal_receipt(stripped, integrity=integrity) is False


def test_authentication_binds_run_status_and_payload_digest(tmp_path: Path) -> None:
    integrity = ControlPlaneIntegrity(tmp_path)
    receipt = build_terminal_receipt(status="failed", terminal_reason="budget_exhausted")
    authenticated = authenticate_terminal_receipt(receipt, integrity=integrity)
    projection = authenticated["authentication"]["payload"]
    assert projection["payload_digest"] == authenticated["payload_digest"]
    assert projection["status"] == "failed"
    assert projection["receipt_type"] == "run_terminal"
    assert projection["schema"] == TERMINAL_RECEIPT_SCHEMA


def test_authentication_rejects_re_signing(tmp_path: Path) -> None:
    integrity = ControlPlaneIntegrity(tmp_path)
    receipt = build_terminal_receipt(status="cancelled", terminal_reason="cancelled_by_owner")
    authenticated = authenticate_terminal_receipt(receipt, integrity=integrity)
    with pytest.raises(ValueError, match="already authenticated"):
        authenticate_terminal_receipt(authenticated, integrity=integrity)


# --- ledger finalization -------------------------------------------------------------


def test_finalize_persists_authenticated_terminal_receipt(
    qualification_ledger: QualificationLedger,
    run: QualificationRun,
    tmp_path: Path,
) -> None:
    replay = _replay(True, (qualified_scope(),))
    payload = build_terminal_receipt(
        status="completed",
        run=run,
        terminal_reason="matrix_exhausted",
        scopes=replay.results,
        replay=replay,
    )
    finalized = qualification_ledger.finalize_run_terminal(
        run.run_id,
        expected_revision=run.revision,
        terminal_status="completed",
        terminal_reason="matrix_exhausted",
        actual_spend=run.actual_spend,
        receipt_payload=payload,
    )
    assert finalized.status == "completed"
    receipts = qualification_ledger.list_receipts(run.run_id)
    terminal = [receipt for receipt in receipts if receipt.receipt_type == "run_terminal"]
    assert len(terminal) == 1
    stored = terminal[0].payload
    assert stored["qualifying"] is True
    integrity = ControlPlaneIntegrity(Path(state_db_path(qualification_ledger)).parent)
    assert verify_terminal_receipt(stored, integrity=integrity) is True
    assert qualification_ledger.verify_receipt_envelope(
        qualification_ledger.receipt_envelope(terminal[0].receipt_id)
    )
    event_types = [event.event_type for event in qualification_ledger.list_events(run.run_id)]
    assert "run_completed" in event_types


def state_db_path(ledger: QualificationLedger) -> str:
    return str(ledger.state.path)


def test_finalize_signing_failure_persists_nothing(
    qualification_ledger: QualificationLedger,
    run: QualificationRun,
    state: AgentStateStore,
) -> None:
    # Mint then corrupt the owner key so signing fails closed.
    state_dir = Path(state.path).parent
    ControlPlaneIntegrity(state_dir)
    (state_dir / ROUTING_INTEGRITY_KEY_NAME).write_text("!!!not-base64!!!")
    payload = build_terminal_receipt(status="failed", run=run, terminal_reason="boom")
    with pytest.raises(RoutingIntegrityError):
        qualification_ledger.finalize_run_terminal(
            run.run_id,
            expected_revision=run.revision,
            terminal_status="failed",
            terminal_reason="boom",
            actual_spend=run.actual_spend,
            receipt_payload=payload,
        )
    fresh = qualification_ledger.get_run(run.run_id)
    assert fresh is not None
    assert fresh.status == "draft"
    assert qualification_ledger.list_receipts(run.run_id) == []
    assert qualification_ledger.list_events(run.run_id) == []
