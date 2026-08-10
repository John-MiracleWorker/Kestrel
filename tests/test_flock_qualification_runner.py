"""Durable Flock qualification run manager tests (Adaptive Flock Task 9).

The runner owns the create/ready/start/pause/resume/cancel/recover
lifecycle on top of the Task 2 ledger state machine. Admission is fair
(round-robin by case, then target, with stable ID tie-breaks), every
attempt is persisted ``reserved`` with a budget reservation before work is
submitted and ``running`` only once the executor owns it, and each
provider/validation result is persisted before the next admission.
Recovery never repeats a running/ambiguous provider request: reserved but
never dispatched attempts return to pending with their reserve released,
running attempts become ambiguous with the reserve retained and an
owner-reconciliation blocker, pausing runs finish as paused, and terminal
runs stay immutable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from nested_memvid_agent.control_plane_integrity import ControlPlaneIntegrity
from nested_memvid_agent.routing.contracts import compile_task_contract
from nested_memvid_agent.routing.qualification_budget import AttemptTokenCeilings
from nested_memvid_agent.routing.qualification_executor import (
    AttemptEvidence,
    AttemptLease,
    ExecutorRouteDecision,
    ProviderAttempt,
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
from nested_memvid_agent.routing.qualification_receipt import verify_terminal_receipt
from nested_memvid_agent.routing.qualification_records import (
    QualificationAttempt,
    QualificationAttemptDraft,
    QualificationCase,
    QualificationCaseDraft,
    QualificationRun,
    QualificationRunDraft,
)
from nested_memvid_agent.routing.qualification_runner import (
    LeaseFactory,
    QualificationRunner,
)
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord

CAPTURED_AT = "2026-08-02T00:00:00+00:00"
RESERVE_MICROS = 2_000  # $1/M * 1000 in + $2/M * 500 out, rounded up
TOKEN_CEILINGS = AttemptTokenCeilings(max_input_tokens=1_000, max_output_tokens=500)


def _price(target_id: str) -> PriceSnapshot:
    return PriceSnapshot(
        target_id=target_id,
        source="operator_verified",
        captured_at=CAPTURED_AT,
        input_per_million=MoneyMicros(1_000_000),
        output_per_million=MoneyMicros(2_000_000),
    )


def _scope(targets: tuple[str, ...]) -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk="low",
        capabilities=("repository_inspection",),
        policy_id="balanced",
        policy_revision=1,
        target_ids=targets,
        target_inventory_digest="1" * 64,
        price_digest="2" * 64,
        learned_config_digest="3" * 64,
        project_authority_digest="4" * 64,
    )


def _corpus(n_cases: int) -> CorpusManifest:
    return CorpusManifest(
        schema_version=1,
        items=tuple(
            CorpusItem(
                item_id=f"corpus_item_{index}",
                task_family="repository_inspection",
                risk="low",
                capabilities=("repository_inspection",),
                task_contract_digest="a" * 64,
                acceptance_plan_digest="b" * 64,
                evidence_kind="real_project",
            )
            for index in range(1, n_cases + 1)
        ),
    )


def _run_draft(
    run_id: str,
    targets: tuple[str, ...],
    n_cases: int,
    *,
    effective_stop_cap: str = "25.00",
) -> QualificationRunDraft:
    return QualificationRunDraft(
        run_id=run_id,
        owner_principal="owner@example.test",
        scope=_scope(targets),
        corpus=_corpus(n_cases),
        thresholds=QualificationThresholds(),
        target_snapshot={"targets": list(targets)},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
        project_authority={"principal": "owner@example.test"},
        build={"version": "0.5.0", "git": "bd2c182"},
        max_spend=MoneyMicros.from_usd_text("50.00"),
        effective_stop_cap=MoneyMicros.from_usd_text(effective_stop_cap),
        attempt_ceiling=MoneyMicros.from_usd_text("5.00"),
    )


@dataclass
class RunnerScenario:
    """One created qualification run plus its per-case routing tasks."""

    run_id: str
    targets: tuple[str, ...]
    tasks: dict[str, TaskNodeRecord]


def run_with_cases(
    state: AgentStateStore,
    ledger: QualificationLedger,
    n_cases: int,
    *,
    targets: tuple[str, ...] = ("a", "b", "c"),
    run_id: str = "qual_1",
    effective_stop_cap: str = "25.00",
    tmp_path: Path,
) -> RunnerScenario:
    """Create a draft run with ``n_cases`` cases and routing-visible tasks."""

    state.create_run(
        run_id=run_id,
        message="Qualify flock targets",
        session_id=f"session-{run_id}",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    run = ledger.create_run(
        _run_draft(run_id, targets, n_cases, effective_stop_cap=effective_stop_cap)
    )
    assert run.run_id == run_id
    tasks: dict[str, TaskNodeRecord] = {}
    for item in _corpus(n_cases).items:
        case_id = f"case_{item.item_id.rsplit('_', 1)[-1]}"
        ledger.create_case(
            QualificationCaseDraft(
                case_id=case_id,
                run_id=run_id,
                item=item,
                repository_digest="c" * 64,
                privacy_eligible=True,
            )
        )
        tasks[case_id] = state.create_task_node(
            task_id=f"task-{case_id}",
            run_id=run_id,
            title="Inspect repository context",
            goal="Gather relevant repository context without changing files.",
            profile="worker",
            approved=True,
            required_tools=("repo.search", "repo.map"),
            risk="low",
            acceptance_criteria=(),
        )
    return RunnerScenario(run_id=run_id, targets=targets, tasks=tasks)


class RecordingExecutor:
    """Executor double recording started attempts; never random or timed."""

    def __init__(
        self,
        *,
        gate: threading.Event | None = None,
        block_after: int | None = None,
        fail_on: frozenset[str] = frozenset(),
    ) -> None:
        self.calls: list[AttemptLease] = []
        self.started_attempts: list[tuple[str, str]] = []
        self.entered = threading.Event()
        self._gate = gate
        self._block_after = block_after
        self._fail_on = fail_on

    def execute(self, lease: AttemptLease) -> AttemptEvidence:
        self.calls.append(lease)
        self.started_attempts.append((lease.case_id, lease.target_id))
        if self._gate is not None and self._block_after is not None:
            if len(self.calls) > self._block_after:
                self.entered.set()
                assert self._gate.wait(timeout=15.0), "test gate was never released"
        failure = "provider_outage" if lease.target_id in self._fail_on else None
        attempt = ProviderAttempt(
            target_id=lease.target_id,
            provider="recording",
            model=f"model-{lease.target_id}",
            output="" if failure else "fixture output",
            input_tokens=1_000,
            output_tokens=500,
            latency_seconds=0.1,
            failure_category=failure,
        )
        return AttemptEvidence(
            lease_id=lease.lease_id,
            attempt_id=lease.attempt_id,
            run_id=lease.run_id,
            case_id=lease.case_id,
            actual_target_id=lease.target_id,
            containment=lease.containment,
            workspace_ref=f"workspace:{lease.lease_id}",
            route_decision=ExecutorRouteDecision(
                decision_id=f"decision-{lease.attempt_id}",
                selected_target_id=lease.target_id,
                selection_kind="direct_pin",
                actionable=True,
                reason_codes=(),
                hard_filter_reasons=(),
            ),
            provider_attempt=attempt,
            validation_passed=failure is None,
            validation_codes=("accepted",) if failure is None else ("provider_failure",),
            failure_category=failure,
            evidence_refs=(f"workspace:{lease.lease_id}",),
        )


class ExplodingExecutor(RecordingExecutor):
    """Executor double raising a provider exception for scripted targets."""

    def execute(self, lease: AttemptLease) -> AttemptEvidence:
        if lease.target_id in self._fail_on:
            self.calls.append(lease)
            self.started_attempts.append((lease.case_id, lease.target_id))
            raise RuntimeError(f"provider exploded for {lease.target_id}")
        return super().execute(lease)


def recording_executor(**kwargs: object) -> RecordingExecutor:
    return RecordingExecutor(**kwargs)  # type: ignore[arg-type]


def _lease_factory(scenario: RunnerScenario) -> LeaseFactory:
    def build(
        run: QualificationRun,
        case: QualificationCase,
        attempt: QualificationAttempt,
    ) -> AttemptLease:
        task = scenario.tasks[case.case_id]
        contract = compile_task_contract(task)
        return AttemptLease(
            lease_id=f"lease-{attempt.attempt_id}",
            run_id=run.run_id,
            case_id=case.case_id,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            target_id=attempt.target_id,
            task=task,
            task_contract_digest=contract.digest,
            project_digest="a" * 64,
            tree_digest="b" * 64,
            target_digest=attempt.target_digest,
            price_digest="d" * 64,
            policy_digest="e" * 64,
            config_digest="f" * 64,
            reservation=attempt.reservation,
            containment="isolated_worktree",
        )

    return build


def _make_runner(
    state: AgentStateStore,
    ledger: QualificationLedger,
    executor: RecordingExecutor,
    scenario: RunnerScenario,
    **kwargs: object,
) -> QualificationRunner:
    return QualificationRunner(
        state,
        ledger,
        executor=executor,
        lease_factory=_lease_factory(scenario),
        prices={target: _price(target) for target in scenario.targets},
        token_ceilings=TOKEN_CEILINGS,
        pause_timeout_seconds=15.0,
        **kwargs,  # type: ignore[arg-type]
    )


def _wait_until(predicate: object, timeout: float = 15.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before the deadline")


@pytest.fixture
def state(tmp_path: Path) -> AgentStateStore:
    return AgentStateStore(tmp_path / "state" / "agent.db")


@pytest.fixture
def qualification_ledger(state: AgentStateStore) -> QualificationLedger:
    return QualificationLedger(state)


# --- Fair matrix admission ---------------------------------------------------


def test_matrix_admission_is_stable_and_round_robin_by_target(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(
        state, qualification_ledger, 2, targets=("a", "b", "c"), tmp_path=tmp_path
    )
    executor = recording_executor()
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    runner.start(scenario.run_id)
    assert executor.started_attempts == [
        ("case_1", "a"),
        ("case_1", "b"),
        ("case_1", "c"),
        ("case_2", "a"),
        ("case_2", "b"),
        ("case_2", "c"),
    ]
    view = runner.get(scenario.run_id)
    assert view.status == "completed"
    assert view.blockers == ()
    for case_id in ("case_1", "case_2"):
        attempts = qualification_ledger.list_attempts(case_id)
        assert [attempt.status for attempt in attempts] == ["completed"] * 3
        assert all(attempt.actual_cost == MoneyMicros(RESERVE_MICROS) for attempt in attempts)


def test_completion_initiates_terminal_receipt(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    runner = _make_runner(state, qualification_ledger, recording_executor(), scenario)
    runner.start(scenario.run_id)
    view = runner.get(scenario.run_id)
    assert view.status == "completed"
    receipts = qualification_ledger.list_receipts(scenario.run_id)
    terminal = [receipt for receipt in receipts if receipt.receipt_type == "run_terminal"]
    assert len(terminal) == 1
    payload = terminal[0].payload
    # Task 12: the receipt is finalized, evaluated, replayed, and signed.
    assert payload["schema"] == "kestrel.flock_qualification_terminal_receipt.v1"
    assert payload["status"] == "completed"
    assert payload["qualifying"] is False
    assert payload["attempts_terminal"] == 2
    assert payload["attempts_succeeded"] == 2
    assert payload["replay"]["unique_projection_digests"] == 1
    assert len(payload["replay"]["projection_digests"]) == 20
    assert len(payload["scopes"]) == 1
    integrity = ControlPlaneIntegrity(Path(state.path).parent)
    assert verify_terminal_receipt(payload, integrity=integrity) is True
    event_types = [event.event_type for event in qualification_ledger.list_events(scenario.run_id)]
    assert "run_completed" in event_types


def test_receipt_finalization_failure_fails_run_with_recovery_blocker(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    runner = _make_runner(state, qualification_ledger, recording_executor(), scenario)

    def broken_finalize(*args: object, **kwargs: object) -> object:
        raise RuntimeError("signing backend unavailable")

    monkeypatch.setattr(qualification_ledger, "finalize_run_terminal", broken_finalize)
    runner.start(scenario.run_id)
    view = runner.get(scenario.run_id)
    assert view.status == "failed"
    assert view.run.terminal_reason == "receipt_finalization_failure"
    assert "terminal_receipt_recovery_required" in view.blockers
    receipts = qualification_ledger.list_receipts(scenario.run_id)
    terminal = [receipt for receipt in receipts if receipt.receipt_type == "run_terminal"]
    assert len(terminal) == 1
    assert terminal[0].payload["qualifying"] is False


def test_cancel_records_authenticated_non_qualifying_receipt(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    runner = _make_runner(state, qualification_ledger, recording_executor(), scenario)
    runner.cancel(scenario.run_id)
    view = runner.get(scenario.run_id)
    assert view.status == "cancelled"
    receipts = qualification_ledger.list_receipts(scenario.run_id)
    terminal = [receipt for receipt in receipts if receipt.receipt_type == "run_terminal"]
    assert len(terminal) == 1
    payload = terminal[0].payload
    assert payload["status"] == "cancelled"
    assert payload["qualifying"] is False
    assert payload["scopes"] == []
    integrity = ControlPlaneIntegrity(Path(state.path).parent)
    assert verify_terminal_receipt(payload, integrity=integrity) is True


# --- Pause / resume lifecycle ------------------------------------------------


def test_pause_waits_for_bounded_inflight_then_marks_paused(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(
        state, qualification_ledger, 1, targets=("a", "b", "c"), tmp_path=tmp_path
    )
    gate = threading.Event()
    executor = recording_executor(gate=gate, block_after=1)
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    starter = threading.Thread(target=runner.start, args=(scenario.run_id,))
    starter.start()
    assert executor.entered.wait(timeout=15.0)
    pauser = threading.Thread(target=runner.pause, args=(scenario.run_id,))
    pauser.start()
    _wait_until(lambda: runner.get(scenario.run_id).status == "pausing")
    # The in-flight attempt is bounded; no new admission happens while pausing.
    assert executor.started_attempts == [("case_1", "a"), ("case_1", "b")]
    gate.set()
    pauser.join(timeout=15.0)
    starter.join(timeout=15.0)
    assert not pauser.is_alive()
    assert runner.get(scenario.run_id).status == "paused"
    assert executor.started_attempts == [("case_1", "a"), ("case_1", "b")]


def test_completion_wins_pause_revision_race_without_leaking_conflict(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = run_with_cases(
        state,
        qualification_ledger,
        1,
        targets=("a", "b"),
        tmp_path=tmp_path,
    )
    gate = threading.Event()
    executor = recording_executor(gate=gate, block_after=1)
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    pause_request_entered = threading.Event()
    release_pause_request = threading.Event()
    request_pause = qualification_ledger.request_pause

    def request_after_completion(
        run_id: str,
        *,
        expected_revision: int,
    ) -> QualificationRun:
        pause_request_entered.set()
        assert release_pause_request.wait(timeout=15.0)
        return request_pause(run_id, expected_revision=expected_revision)

    monkeypatch.setattr(
        qualification_ledger,
        "request_pause",
        request_after_completion,
    )
    starter = threading.Thread(target=runner.start, args=(scenario.run_id,))
    starter.start()
    assert executor.entered.wait(timeout=15.0)

    def finish_before_pause_cas() -> None:
        assert pause_request_entered.wait(timeout=15.0)
        gate.set()
        try:
            _wait_until(lambda: runner.get(scenario.run_id).status == "completed")
        finally:
            release_pause_request.set()

    finisher = threading.Thread(target=finish_before_pause_cas)
    finisher.start()
    view = runner.pause(scenario.run_id)
    finisher.join(timeout=15.0)
    starter.join(timeout=15.0)

    assert not finisher.is_alive()
    assert not starter.is_alive()
    assert view.status == "completed"
    assert runner.get(scenario.run_id).status == "completed"


def test_pause_wins_completion_revision_race_without_failing_run(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = run_with_cases(
        state,
        qualification_ledger,
        1,
        targets=("a", "b"),
        tmp_path=tmp_path,
    )
    runner = _make_runner(
        state,
        qualification_ledger,
        recording_executor(),
        scenario,
    )
    finalize_entered = threading.Event()
    release_finalize = threading.Event()
    finalize_run_terminal = qualification_ledger.finalize_run_terminal

    def finalize_after_pause(*args: object, **kwargs: object) -> object:
        if not finalize_entered.is_set():
            finalize_entered.set()
            assert release_finalize.wait(timeout=15.0)
        return finalize_run_terminal(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        qualification_ledger,
        "finalize_run_terminal",
        finalize_after_pause,
    )
    starter = threading.Thread(target=runner.start, args=(scenario.run_id,))
    starter.start()
    assert finalize_entered.wait(timeout=15.0)
    pauser = threading.Thread(target=runner.pause, args=(scenario.run_id,))
    pauser.start()
    _wait_until(lambda: runner.get(scenario.run_id).status == "pausing")
    release_finalize.set()
    pauser.join(timeout=15.0)
    starter.join(timeout=15.0)
    assert not pauser.is_alive()
    assert not starter.is_alive()
    assert runner.get(scenario.run_id).status == "paused"
    assert qualification_ledger.list_receipts(scenario.run_id) == []

    runner.resume(scenario.run_id)

    assert runner.get(scenario.run_id).status == "completed"
    terminal = [
        receipt
        for receipt in qualification_ledger.list_receipts(scenario.run_id)
        if receipt.receipt_type == "run_terminal"
    ]
    assert len(terminal) == 1


def test_resume_skips_terminal_and_inflight_positions(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(
        state, qualification_ledger, 1, targets=("a", "b", "c"), tmp_path=tmp_path
    )
    gate = threading.Event()
    executor = recording_executor(gate=gate, block_after=1)
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    starter = threading.Thread(target=runner.start, args=(scenario.run_id,))
    starter.start()
    assert executor.entered.wait(timeout=15.0)
    pauser = threading.Thread(target=runner.pause, args=(scenario.run_id,))
    pauser.start()
    _wait_until(lambda: runner.get(scenario.run_id).status == "pausing")
    gate.set()
    pauser.join(timeout=15.0)
    starter.join(timeout=15.0)
    assert runner.get(scenario.run_id).status == "paused"
    runner.resume(scenario.run_id)
    assert runner.get(scenario.run_id).status == "completed"
    # Completed attempts are never repeated after a resume.
    assert executor.started_attempts == [
        ("case_1", "a"),
        ("case_1", "b"),
        ("case_1", "c"),
    ]
    attempts = qualification_ledger.list_attempts("case_1")
    assert len(attempts) == 3
    assert [attempt.status for attempt in attempts] == ["completed"] * 3


# --- Lower cap while running -------------------------------------------------


def test_lower_cap_while_running_stops_admission_fail_closed(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(
        state, qualification_ledger, 1, targets=("a", "b", "c"), tmp_path=tmp_path
    )
    gate = threading.Event()
    executor = recording_executor(gate=gate, block_after=0)
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    starter = threading.Thread(target=runner.start, args=(scenario.run_id,))
    starter.start()
    assert executor.entered.wait(timeout=15.0)
    with pytest.raises(ValueError, match="cannot be raised"):
        runner.lower_cap(scenario.run_id, MoneyMicros.from_usd_text("50.00"))
    # Cap below spend+reserve projection of the next attempt: 2000 actual
    # plus 2000 projected reserve exceeds a 2500 micro-USD cap.
    runner.lower_cap(scenario.run_id, MoneyMicros(2_500))
    gate.set()
    starter.join(timeout=15.0)
    assert not starter.is_alive()
    view = runner.get(scenario.run_id)
    assert view.status == "failed"
    assert "hard_cap_exhausted" in (view.run.terminal_reason or "")
    assert executor.started_attempts == [("case_1", "a")]
    receipts = qualification_ledger.list_receipts(scenario.run_id)
    terminal = [receipt for receipt in receipts if receipt.receipt_type == "run_terminal"]
    assert len(terminal) == 1
    assert terminal[0].payload["qualifying"] is False


# --- Cancellation ------------------------------------------------------------


def test_cancel_terminalizes_pending_and_records_non_qualifying_receipt(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(
        state, qualification_ledger, 1, targets=("a", "b", "c"), tmp_path=tmp_path
    )
    gate = threading.Event()
    executor = recording_executor(gate=gate, block_after=0)
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    starter = threading.Thread(target=runner.start, args=(scenario.run_id,))
    starter.start()
    assert executor.entered.wait(timeout=15.0)
    canceller = threading.Thread(target=runner.cancel, args=(scenario.run_id,))
    canceller.start()
    # Cancel waits for the bounded in-flight attempt instead of abandoning it.
    assert runner.get(scenario.run_id).status == "running"
    gate.set()
    canceller.join(timeout=15.0)
    starter.join(timeout=15.0)
    assert not canceller.is_alive()
    view = runner.get(scenario.run_id)
    assert view.status == "cancelled"
    # The in-flight attempt's evidence is preserved; the never-admitted
    # positions left no rows, and nothing was repeated.
    assert executor.started_attempts == [("case_1", "a")]
    attempts = qualification_ledger.list_attempts("case_1")
    assert len(attempts) == 1
    assert attempts[0].status == "completed"
    receipts = qualification_ledger.list_receipts(scenario.run_id)
    terminal = [receipt for receipt in receipts if receipt.receipt_type == "run_terminal"]
    assert len(terminal) == 1
    assert terminal[0].payload["qualifying"] is False
    assert terminal[0].payload["terminal_reason"] == "cancelled_by_owner"


def test_cancel_from_ready_terminalizes_pending_attempts(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    executor = recording_executor()
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    runner.ready(scenario.run_id)
    qualification_ledger.create_attempt(
        QualificationAttemptDraft(
            attempt_id="pending_1",
            case_id="case_1",
            attempt_number=1,
            target_id="a",
            target_digest="d" * 64,
            reservation=MoneyMicros(RESERVE_MICROS),
        )
    )
    runner.cancel(scenario.run_id)
    view = runner.get(scenario.run_id)
    assert view.status == "cancelled"
    assert executor.started_attempts == []
    pending_attempt = runner.get_attempt("pending_1")
    assert pending_attempt is not None
    assert pending_attempt.status == "cancelled"


# --- Worker failure ------------------------------------------------------------


def test_worker_failure_terminalizes_only_its_attempt(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(
        state, qualification_ledger, 1, targets=("a", "b", "c"), tmp_path=tmp_path
    )
    executor = ExplodingExecutor(fail_on=frozenset({"b"}))
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    runner.start(scenario.run_id)
    assert executor.started_attempts == [
        ("case_1", "a"),
        ("case_1", "b"),
        ("case_1", "c"),
    ]
    attempts = {
        attempt.target_id: attempt for attempt in qualification_ledger.list_attempts("case_1")
    }
    assert attempts["a"].status == "completed"
    assert attempts["b"].status == "ambiguous"
    assert attempts["b"].failure_category == "transport_outcome_ambiguous"
    assert attempts["c"].status == "completed"
    view = runner.get(scenario.run_id)
    assert view.status == "completed"
    # The ambiguous attempt's reserve is never silently released: run
    # terminalization folds the unresolved reserve into the conservative
    # actual spend, and the run requires owner reconciliation.
    assert view.run.actual_spend == MoneyMicros(3 * RESERVE_MICROS)
    assert "owner_reconciliation_required" in view.blockers


# --- UI disconnect ------------------------------------------------------------


def test_ui_disconnect_does_not_change_run_state(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    gate = threading.Event()
    executor = recording_executor(gate=gate, block_after=0)
    runner = _make_runner(state, qualification_ledger, executor, scenario)
    watch = runner.watch(scenario.run_id)
    starter = threading.Thread(target=runner.start, args=(scenario.run_id,))
    starter.start()
    assert executor.entered.wait(timeout=15.0)
    watch.close()
    assert runner.active_watches == 0
    assert runner.get(scenario.run_id).status == "running"
    gate.set()
    starter.join(timeout=15.0)
    assert runner.get(scenario.run_id).status == "completed"
    assert executor.started_attempts == [("case_1", "a"), ("case_1", "b")]


# --- Concurrency bounds --------------------------------------------------------


def test_concurrency_bounded_by_profile_capacity_and_server_max(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    scenario = run_with_cases(
        state, qualification_ledger, 1, targets=("a", "b", "c"), tmp_path=tmp_path
    )
    executor = recording_executor()
    runner = _make_runner(
        state,
        qualification_ledger,
        executor,
        scenario,
        concurrency=8,
        containment_capacity=3,
        server_max_concurrency=4,
        target_concurrency={"a": 2, "b": 2, "c": 5},
    )
    # Provider profile concurrency is the tightest bound.
    assert runner.effective_concurrency(scenario.run_id) == 2
    unbounded_profiles = _make_runner(
        state,
        qualification_ledger,
        executor,
        scenario,
        concurrency=8,
        containment_capacity=3,
        server_max_concurrency=4,
    )
    # Without profile data the containment capacity binds.
    assert unbounded_profiles.effective_concurrency(scenario.run_id) == 3
    saturated = _make_runner(
        state,
        qualification_ledger,
        executor,
        scenario,
        concurrency=1,
        containment_capacity=1,
        server_max_concurrency=1,
    )
    assert saturated.effective_concurrency(scenario.run_id) == 1


# --- Recovery ------------------------------------------------------------------


def _running_run(ledger: QualificationLedger, run_id: str = "qual_1") -> QualificationRun:
    run = ledger.get_run(run_id)
    assert run is not None
    ready = ledger.mark_ready(run_id, expected_revision=run.revision)
    return ledger.mark_running(run_id, expected_revision=ready.revision)


def test_restart_does_not_repeat_ambiguous_attempt(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    _running_run(qualification_ledger)
    qualification_ledger.admit_attempt_with_budget(
        QualificationAttemptDraft(
            attempt_id="attempt_1",
            case_id="case_1",
            attempt_number=1,
            target_id="a",
            target_digest="d" * 64,
            reservation=MoneyMicros(RESERVE_MICROS),
        )
    )
    qualification_ledger.mark_attempt_running("attempt_1")
    executor = recording_executor()
    runner = QualificationRunner.recover(state, executor=executor)
    # A restart never repeats a running/ambiguous provider request.
    assert executor.calls == []
    attempt = runner.get_attempt("attempt_1")
    assert attempt is not None
    assert attempt.status == "ambiguous"
    assert attempt.failure_category == "transport_outcome_ambiguous"
    view = runner.get("qual_1")
    assert "owner_reconciliation_required" in view.blockers
    # The reserve is retained as unresolved cost, never silently released.
    assert view.run.unresolved_reserve == MoneyMicros(RESERVE_MICROS)
    assert view.run.inflight_reserve == MoneyMicros(0)


def test_recovery_releases_never_dispatched_reservation(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    _running_run(qualification_ledger)
    qualification_ledger.admit_attempt_with_budget(
        QualificationAttemptDraft(
            attempt_id="attempt_2",
            case_id="case_1",
            attempt_number=1,
            target_id="a",
            target_digest="d" * 64,
            reservation=MoneyMicros(RESERVE_MICROS),
        )
    )
    runner = QualificationRunner.recover(state, executor=recording_executor())
    attempt = runner.get_attempt("attempt_2")
    assert attempt is not None
    # Reserved but never dispatched: release the reservation, back to pending.
    assert attempt.status == "pending"
    view = runner.get("qual_1")
    assert view.run.inflight_reserve == MoneyMicros(0)
    assert view.run.unresolved_reserve == MoneyMicros(0)
    assert view.blockers == ()


def test_recovery_finishes_pausing_as_paused(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    running = _running_run(qualification_ledger)
    qualification_ledger.request_pause("qual_1", expected_revision=running.revision)
    runner = QualificationRunner.recover(state, executor=recording_executor())
    assert runner.get("qual_1").status == "paused"


def test_recovery_leaves_terminal_runs_immutable(
    state: AgentStateStore,
    qualification_ledger: QualificationLedger,
    tmp_path: Path,
) -> None:
    run_with_cases(state, qualification_ledger, 1, targets=("a", "b"), tmp_path=tmp_path)
    running = _running_run(qualification_ledger)
    completed = qualification_ledger.complete_run(
        "qual_1",
        expected_revision=running.revision,
        terminal_reason="all_cases_scored",
        actual_spend=MoneyMicros(RESERVE_MICROS),
        terminal_receipt={"qualified": True, "target_id": "a"},
    )
    receipts_before = qualification_ledger.list_receipts("qual_1")
    runner = QualificationRunner.recover(state, executor=recording_executor())
    view = runner.get("qual_1")
    assert view.status == "completed"
    assert view.run.revision == completed.revision
    assert qualification_ledger.list_receipts("qual_1") == receipts_before
