"""Durable Flock qualification run manager (Adaptive Flock plan, Task 9).

``QualificationRunner`` owns the
``create/ready/start/pause/resume/cancel/recover`` lifecycle on top of the
Task 2 ledger state machine:

- Fair matrix order: round-robin by case, then target, with stable ID
  tie-breaks (cases sorted by case ID, targets sorted by target ID).
- Lease-based execution: an attempt is persisted ``reserved`` with its
  budget reservation before work is submitted, transitions to ``running``
  only when the executor owns it, and every provider/validation result is
  persisted before the next admission.
- Configurable concurrency bounded by provider profile concurrency,
  project containment capacity, and a server maximum.
- Pause stops new admission and waits for bounded in-flight attempts;
  cancel does the same, terminalizes pending attempts ``cancelled``,
  preserves evidence, and records a non-qualifying cancelled receipt.
- A provider exception terminalizes only its own attempt with a typed
  category unless state integrity itself fails.
- Restart never repeats a running/ambiguous provider request
  automatically: ``reserved`` but never dispatched attempts return to
  ``pending`` with the reserve released, ``running`` attempts become
  ``ambiguous`` with the reserve retained and an owner-reconciliation
  blocker, ``pausing`` runs finish as ``paused``, and terminal runs stay
  immutable.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass
from typing import Any, Protocol

from ..state_store import AgentStateStore
from .qualification_budget import (
    AttemptTokenCeilings,
    BudgetAdmissionRejected,
    QualificationBudget,
)
from .qualification_digest import canonical_digest
from .qualification_executor import (
    AttemptEvidence,
    AttemptLease,
    QualificationAttemptBlocked,
)
from .qualification_ledger import QualificationLedger
from .qualification_models import MoneyMicros, PriceSnapshot
from .qualification_records import (
    QualificationAttempt,
    QualificationAttemptDraft,
    QualificationCase,
    QualificationRun,
    QualificationRunDraft,
)

__all__ = [
    "AttemptExecutor",
    "LeaseFactory",
    "QualificationRunView",
    "QualificationRunner",
    "RunWatch",
]

DEFAULT_SERVER_MAX_CONCURRENCY = 4
_RECOVERABLE_RUN_STATES = ("draft", "ready", "running", "pausing", "paused")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _require_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


class AttemptExecutor(Protocol):
    """Executor invoked under lease; the Task 8 executor satisfies this."""

    def execute(self, lease: AttemptLease) -> AttemptEvidence: ...


LeaseFactory = Callable[[QualificationRun, QualificationCase, QualificationAttempt], AttemptLease]


@dataclass(frozen=True)
class QualificationRunView:
    """One run plus the blockers an owner must clear."""

    run: QualificationRun
    blockers: tuple[str, ...]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.run, name)


@dataclass
class RunWatch:
    """UI watch token; closing it never changes run state."""

    _runner: QualificationRunner
    _watch_id: int
    _closed: bool = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._runner._release_watch(self._watch_id)


class _RunWorker:
    """Mutable per-run worker bookkeeping guarded by the runner lock."""

    def __init__(self) -> None:
        self.stop_mode: str | None = None
        self.done = threading.Event()
        self.thread = threading.Thread()


class QualificationRunner:
    """Durable run manager for Flock qualification runs."""

    def __init__(
        self,
        state: AgentStateStore,
        ledger: QualificationLedger,
        *,
        executor: AttemptExecutor | None = None,
        lease_factory: LeaseFactory | None = None,
        prices: Mapping[str, PriceSnapshot] | None = None,
        token_ceilings: AttemptTokenCeilings | None = None,
        concurrency: int = 1,
        containment_capacity: int = 1,
        target_concurrency: Mapping[str, int] | None = None,
        server_max_concurrency: int = DEFAULT_SERVER_MAX_CONCURRENCY,
        pause_timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(state, AgentStateStore):
            raise ValueError("state must be an AgentStateStore")
        if not isinstance(ledger, QualificationLedger):
            raise ValueError("ledger must be a QualificationLedger")
        if executor is not None and not hasattr(executor, "execute"):
            raise ValueError("executor must implement execute(AttemptLease)")
        if lease_factory is not None and not callable(lease_factory):
            raise ValueError("lease_factory must be callable")
        if prices is not None:
            for target_id, price in prices.items():
                if not isinstance(price, PriceSnapshot):
                    raise ValueError("prices must be PriceSnapshot values")
                if price.target_id != target_id:
                    raise ValueError("price snapshot must belong to its target key")
        if token_ceilings is not None and not isinstance(token_ceilings, AttemptTokenCeilings):
            raise ValueError("token_ceilings must be an AttemptTokenCeilings value")
        _require_positive_int(concurrency, "concurrency")
        _require_positive_int(containment_capacity, "containment_capacity")
        _require_positive_int(server_max_concurrency, "server_max_concurrency")
        if target_concurrency is not None:
            for target_id, limit in target_concurrency.items():
                _require_text(target_id, "target_concurrency key")
                _require_positive_int(limit, "target_concurrency value")
        if (
            isinstance(pause_timeout_seconds, bool)
            or not isinstance(pause_timeout_seconds, (int, float))
            or pause_timeout_seconds <= 0
        ):
            raise ValueError("pause_timeout_seconds must be a positive number")
        self._state = state
        self._ledger = ledger
        self._executor = executor
        self._lease_factory = lease_factory
        self._prices = None if prices is None else dict(prices)
        self._token_ceilings = token_ceilings
        self._concurrency = concurrency
        self._containment_capacity = containment_capacity
        self._target_concurrency = None if target_concurrency is None else dict(target_concurrency)
        self._server_max_concurrency = server_max_concurrency
        self._pause_timeout_seconds = float(pause_timeout_seconds)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._workers: dict[str, _RunWorker] = {}
        self._watches: set[int] = set()
        self._next_watch_id = 0
        self._shutdown = False

    # -- lifecycle ---------------------------------------------------------

    def create(self, draft: QualificationRunDraft) -> QualificationRun:
        """Create a draft qualification run."""

        return self._ledger.create_run(draft)

    def ready(self, run_id: str) -> QualificationRunView:
        """Move a draft run to ready (idempotent when already ready)."""

        run = self._require_run(run_id)
        if run.status == "draft":
            self._ledger.mark_ready(run_id, expected_revision=run.revision)
        elif run.status != "ready":
            raise ValueError(
                f"qualification run must be draft or ready to ready; current status is {run.status}"
            )
        return self.get(run_id)

    def start(self, run_id: str, *, wait: bool = True) -> QualificationRunView:
        """Ready (if needed), mark running, and drive the fair matrix.

        With ``wait`` (the default) this blocks until the run leaves
        ``running`` (paused or terminal), which keeps single-threaded
        callers deterministic.
        """

        run = self._require_run(run_id)
        if run.is_terminal:
            return self.get(run_id)
        if run.status == "paused":
            raise ValueError("paused runs resume with resume(), not start()")
        if run.status == "pausing":
            raise ValueError("a pause is already in progress for this run")
        if run.status == "draft":
            run = self._ledger.mark_ready(run_id, expected_revision=run.revision)
        if run.status == "ready":
            run = self._ledger.mark_running(run_id, expected_revision=run.revision)
        worker = self._ensure_worker(run_id)
        if wait:
            worker.thread.join()
        return self.get(run_id)

    def pause(self, run_id: str) -> QualificationRunView:
        """Stop new admission and wait for bounded in-flight attempts."""

        run = self._require_run(run_id)
        if run.is_terminal:
            return self.get(run_id)
        if run.status == "running":
            try:
                self._ledger.request_pause(run_id, expected_revision=run.revision)
            except ValueError:
                # Raced with terminalization; the final view is authoritative.
                return self.get(run_id)
        elif run.status not in ("pausing", "paused"):
            raise ValueError(
                f"qualification run must be running to pause; current status is {run.status}"
            )
        with self._condition:
            self._condition.wait_for(
                lambda: self._status_in(run_id, ("paused", "cancelled", "failed", "completed")),
                timeout=self._pause_timeout_seconds,
            )
        return self.get(run_id)

    def resume(self, run_id: str, *, wait: bool = True) -> QualificationRunView:
        """Resume a paused run (or re-attach a recovered running run)."""

        run = self._require_run(run_id)
        if run.is_terminal:
            return self.get(run_id)
        if run.status == "paused":
            self._ledger.mark_running(run_id, expected_revision=run.revision)
        elif run.status != "running":
            raise ValueError(
                f"qualification run must be paused or running to resume; "
                f"current status is {run.status}"
            )
        worker = self._ensure_worker(run_id)
        if wait:
            worker.thread.join()
        return self.get(run_id)

    def cancel(self, run_id: str) -> QualificationRunView:
        """Cancel: stop admission, wait bounded in-flight, preserve evidence.

        Pending attempts terminalize ``cancelled`` and the run records a
        non-qualifying cancelled receipt.
        """

        run = self._require_run(run_id)
        if run.is_terminal:
            return self.get(run_id)
        with self._condition:
            worker = self._workers.get(run_id)
            active = worker is not None and not worker.done.is_set()
            if active:
                assert worker is not None
                worker.stop_mode = "cancel"
                self._condition.notify_all()
        if active:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._status_in(run_id, ("cancelled", "failed", "completed")),
                    timeout=self._pause_timeout_seconds,
                )
        else:
            self._terminalize_cancel(run_id)
        return self.get(run_id)

    def lower_cap(self, run_id: str, new_cap: MoneyMicros) -> QualificationRunView:
        """Lower the effective stop cap while running; it can never be raised."""

        if not isinstance(new_cap, MoneyMicros):
            raise ValueError("new_cap must be a MoneyMicros value")
        run = self._require_run(run_id)
        if run.is_terminal:
            raise ValueError(f"qualification run {run_id} is terminal ({run.status})")
        if new_cap.micros > run.effective_stop_cap.micros:
            raise ValueError("effective stop cap cannot be raised after the run started")
        if new_cap.micros < run.effective_stop_cap.micros:
            self._ledger.update_effective_stop_cap(
                run_id, expected_revision=run.revision, new_cap=new_cap
            )
        return self.get(run_id)

    # -- views ---------------------------------------------------------------

    def get(self, run_id: str) -> QualificationRunView:
        run = self._require_run(run_id)
        return QualificationRunView(run=run, blockers=self._blockers(run))

    def get_attempt(self, attempt_id: str) -> QualificationAttempt | None:
        return self._ledger.get_attempt(attempt_id)

    def list_attempts(self, run_id: str) -> list[QualificationAttempt]:
        attempts: list[QualificationAttempt] = []
        for case in self._ledger.list_cases(run_id):
            attempts.extend(self._ledger.list_attempts(case.case_id))
        return attempts

    def effective_concurrency(self, run_id: str) -> int:
        """Bounded concurrency: profile, containment capacity, server max."""

        run = self._require_run(run_id)
        scope = json.loads(run.scope_json)
        targets = tuple(str(target) for target in scope.get("target_ids", ()))
        bounds = [
            self._concurrency,
            self._server_max_concurrency,
            self._containment_capacity,
        ]
        if self._target_concurrency is not None:
            # A target with no known profile concurrency fails closed to 1.
            bounds.append(
                min((self._target_concurrency.get(target, 1) for target in targets), default=1)
            )
        return max(1, min(bounds))

    # -- UI watches (renderer disconnect never changes run state) -------------

    def watch(self, run_id: str) -> RunWatch:
        self._require_run(run_id)
        with self._lock:
            self._next_watch_id += 1
            watch_id = self._next_watch_id
            self._watches.add(watch_id)
        return RunWatch(self, watch_id)

    @property
    def active_watches(self) -> int:
        with self._lock:
            return len(self._watches)

    def _release_watch(self, watch_id: int) -> None:
        with self._lock:
            self._watches.discard(watch_id)

    # -- recovery ---------------------------------------------------------------

    @classmethod
    def recover(
        cls,
        state: AgentStateStore,
        *,
        ledger: QualificationLedger | None = None,
        executor: AttemptExecutor | None = None,
        lease_factory: LeaseFactory | None = None,
        prices: Mapping[str, PriceSnapshot] | None = None,
        token_ceilings: AttemptTokenCeilings | None = None,
        **settings: Any,
    ) -> QualificationRunner:
        """Construct a runner over existing state and reconcile restarts.

        Recovery never dispatches provider work: an executor attached here
        observes zero calls until an owner explicitly resumes a run.
        """

        runner = cls(
            state,
            ledger if ledger is not None else QualificationLedger(state),
            executor=executor,
            lease_factory=lease_factory,
            prices=prices,
            token_ceilings=token_ceilings,
            **settings,
        )
        runner.recover_startup()
        return runner

    def recover_startup(self) -> None:
        """Sidecar-startup reconciliation for every non-terminal run.

        - ``reserved`` but never dispatched -> release the reservation and
          return the attempt to ``pending``;
        - ``running`` with no definitive receipt -> mark ``ambiguous``,
          retain the reserve as unresolved cost, and surface an
          owner-reconciliation blocker;
        - ``pausing`` -> finish as ``paused``;
        - terminal runs are never touched.
        """

        for run in self._ledger.list_runs(statuses=_RECOVERABLE_RUN_STATES):
            for case in self._ledger.list_cases(run.run_id):
                for attempt in self._ledger.list_attempts(case.case_id):
                    if attempt.status == "reserved":
                        self._ledger.release_attempt_reservation(attempt.attempt_id)
                    elif attempt.status == "running":
                        self._ledger.settle_attempt_with_budget(
                            attempt.attempt_id, outcome="ambiguous"
                        )
            fresh = self._require_run(run.run_id)
            if fresh.status == "pausing":
                self._ledger.mark_paused(run.run_id, expected_revision=fresh.revision)

    # -- lifecycle dependency contract ------------------------------------------

    def shutdown(self, timeout_seconds: float = 5.0) -> bool:
        """Stop admission on active runs, drain in-flight work, join threads."""

        with self._condition:
            self._shutdown = True
            workers = [worker for worker in self._workers.values() if not worker.done.is_set()]
            for worker in workers:
                if worker.stop_mode is None:
                    worker.stop_mode = "pause"
            self._condition.notify_all()
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        for worker in workers:
            remaining = deadline - time.monotonic()
            worker.thread.join(timeout=max(0.0, remaining))
        return all(not worker.thread.is_alive() for worker in workers)

    # -- worker management -------------------------------------------------------

    def _ensure_worker(self, run_id: str) -> _RunWorker:
        with self._condition:
            if self._shutdown:
                raise RuntimeError("qualification runner is shut down")
            self._require_execution_config()
            worker = self._workers.get(run_id)
            if worker is not None and not worker.done.is_set():
                return worker
            worker = _RunWorker()
            worker.thread = threading.Thread(
                target=self._run_loop,
                args=(run_id, worker),
                name=f"kestrel-qual-run-{run_id}",
                daemon=True,
            )
            self._workers[run_id] = worker
            worker.thread.start()
            return worker

    def _run_loop(self, run_id: str, worker: _RunWorker) -> None:
        try:
            self._drive_run(run_id, worker)
        except Exception:  # noqa: BLE001 - state integrity failure fails the run
            self._fail_run_best_effort(run_id)
        finally:
            with self._condition:
                worker.done.set()
                if self._workers.get(run_id) is worker:
                    del self._workers[run_id]
                self._condition.notify_all()

    # -- admission loop -----------------------------------------------------------

    def _drive_run(self, run_id: str, worker: _RunWorker) -> None:
        budget = self._budget(run_id)
        max_inflight = self.effective_concurrency(run_id)
        pool = ThreadPoolExecutor(
            max_workers=max_inflight,
            thread_name_prefix=f"kestrel-qual-attempt-{run_id}",
        )
        inflight: dict[Future[AttemptEvidence], QualificationAttempt] = {}
        try:
            while True:
                # Persist every finished provider/validation result before
                # admitting the next attempt.
                self._settle_finished(budget, inflight)
                run = self._require_run(run_id)
                if run.is_terminal:
                    return
                with self._lock:
                    mode = worker.stop_mode
                if mode == "pause" and run.status == "running":
                    run = self._ledger.request_pause(run_id, expected_revision=run.revision)
                if run.status == "pausing" or mode == "cancel":
                    if inflight:
                        self._wait_for_any(inflight)
                        continue
                    if mode == "cancel":
                        self._terminalize_cancel(run_id)
                        return
                    fresh = self._require_run(run_id)
                    if fresh.status == "pausing":
                        self._ledger.mark_paused(run_id, expected_revision=fresh.revision)
                    return
                if run.status != "running":
                    return
                position = self._next_position(run)
                if position is None:
                    if inflight:
                        self._wait_for_any(inflight)
                        continue
                    self._complete_run(run)
                    return
                if len(inflight) >= max_inflight:
                    self._wait_for_any(inflight)
                    continue
                case, target_id = position
                try:
                    self._admit(run, budget, case, target_id, pool, inflight)
                except BudgetAdmissionRejected as exc:
                    while inflight:
                        self._wait_for_any(inflight)
                        self._settle_finished(budget, inflight)
                    self._fail_run(
                        run_id,
                        terminal_reason=f"admission_rejected:{exc.reason}",
                        receipt={"qualifying": False, "reason": exc.reason},
                    )
                    return
        finally:
            pool.shutdown(wait=True)

    def _next_position(self, run: QualificationRun) -> tuple[QualificationCase, str] | None:
        """Next fair matrix position: cases by ID, then targets by ID.

        Positions with a terminal attempt (including ``ambiguous``) are
        never repeated.
        """

        scope = json.loads(run.scope_json)
        targets = sorted(str(target) for target in scope.get("target_ids", ()))
        cases = sorted(self._ledger.list_cases(run.run_id), key=lambda case: case.case_id)
        for case in cases:
            attempts = self._ledger.list_attempts(case.case_id)
            by_target: dict[str, list[QualificationAttempt]] = {}
            for attempt in attempts:
                by_target.setdefault(attempt.target_id, []).append(attempt)
            for target_id in targets:
                existing = by_target.get(target_id, [])
                if any(attempt.is_terminal for attempt in existing):
                    continue
                return case, target_id
        return None

    def _admit(
        self,
        run: QualificationRun,
        budget: QualificationBudget,
        case: QualificationCase,
        target_id: str,
        pool: ThreadPoolExecutor,
        inflight: dict[Future[AttemptEvidence], QualificationAttempt],
    ) -> None:
        attempts = [
            attempt
            for attempt in self._ledger.list_attempts(case.case_id)
            if attempt.target_id == target_id and not attempt.is_terminal
        ]
        pending = [attempt for attempt in attempts if attempt.status == "pending"]
        reserved = [attempt for attempt in attempts if attempt.status == "reserved"]
        if pending:
            # Re-admit a released reservation under the current hard cap.
            budget.estimate_attempt_reserve(target_id)
            attempt = self._ledger.admit_pending_attempt_with_budget(pending[0].attempt_id)
        elif reserved:
            attempt = reserved[0]
        else:
            reservation = budget.estimate_attempt_reserve(target_id)
            number = (
                max(
                    (a.attempt_number for a in self._ledger.list_attempts(case.case_id)),
                    default=0,
                )
                + 1
            )
            draft = QualificationAttemptDraft(
                attempt_id=(f"att_{run.run_id}_{case.case_id}_{target_id}_{number}"),
                case_id=case.case_id,
                attempt_number=number,
                target_id=target_id,
                target_digest=canonical_digest(
                    {
                        "run_id": run.run_id,
                        "case_id": case.case_id,
                        "target_id": target_id,
                        "target_snapshot_digest": run.target_digest,
                    }
                ),
                reservation=reservation,
            )
            # Persist reserved with its budget reservation before any work.
            attempt = budget.admit_attempt(draft)
        if attempt.status == "reserved":
            # The executor owns the attempt from this point on.
            attempt = self._ledger.mark_attempt_running(attempt.attempt_id)
        assert self._lease_factory is not None
        assert self._executor is not None
        lease = self._lease_factory(run, case, attempt)
        future = pool.submit(self._executor.execute, lease)
        inflight[future] = attempt

    def _settle_finished(
        self,
        budget: QualificationBudget,
        inflight: dict[Future[AttemptEvidence], QualificationAttempt],
    ) -> None:
        for future in [item for item in inflight if item.done()]:
            attempt = inflight.pop(future)
            self._settle_one(budget, attempt, future)

    def _settle_one(
        self,
        budget: QualificationBudget,
        attempt: QualificationAttempt,
        future: Future[AttemptEvidence],
    ) -> None:
        try:
            evidence = future.result()
        except QualificationAttemptBlocked as exc:
            # Blocked before provider contact: definitive zero-cost failure.
            self._ledger.settle_attempt_with_budget(
                attempt.attempt_id,
                outcome="transport_confirmed_zero",
                evidence_refs=(f"blocked:{exc.reason}",),
            )
            return
        except Exception:  # noqa: BLE001 - provider exception: ambiguous transport
            self._ledger.settle_attempt_with_budget(attempt.attempt_id, outcome="ambiguous")
            return
        self._budget_settle_evidence(budget, attempt, evidence)

    def _budget_settle_evidence(
        self,
        budget: QualificationBudget,
        attempt: QualificationAttempt,
        evidence: AttemptEvidence,
    ) -> None:
        usage = {
            "input_tokens": evidence.provider_attempt.input_tokens,
            "output_tokens": evidence.provider_attempt.output_tokens,
        }
        budget.settle_attempt(
            attempt.attempt_id,
            usage=usage,
            validation_passed=evidence.validation_passed,
            validation_codes=tuple(evidence.validation_codes),
            evidence_refs=tuple(evidence.evidence_refs),
        )

    def _wait_for_any(self, inflight: dict[Future[AttemptEvidence], QualificationAttempt]) -> None:
        wait_futures(tuple(inflight), timeout=0.05, return_when=FIRST_COMPLETED)

    # -- terminalization -----------------------------------------------------------

    def _complete_run(self, run: QualificationRun) -> None:
        fresh = self._require_run(run.run_id)
        if fresh.is_terminal:
            return
        attempts = self.list_attempts(run.run_id)
        terminal = [attempt for attempt in attempts if attempt.is_terminal]
        succeeded = [
            attempt
            for attempt in terminal
            if attempt.status == "completed" and attempt.validation_passed
        ]
        receipt = {
            "matrix_exhausted": True,
            "evaluation_status": "pending",
            "attempts_terminal": len(terminal),
            "attempts_succeeded": len(succeeded),
        }
        self._ledger.complete_run(
            run.run_id,
            expected_revision=fresh.revision,
            terminal_reason="matrix_exhausted",
            actual_spend=self._conservative_spend(fresh),
            terminal_receipt=receipt,
        )

    def _fail_run(
        self,
        run_id: str,
        *,
        terminal_reason: str,
        receipt: Mapping[str, Any],
    ) -> None:
        fresh = self._require_run(run_id)
        if fresh.is_terminal:
            return
        self._ledger.fail_run(
            run_id,
            expected_revision=fresh.revision,
            terminal_reason=terminal_reason,
            actual_spend=self._conservative_spend(fresh),
            terminal_receipt=receipt,
        )

    def _fail_run_best_effort(self, run_id: str) -> None:
        try:
            self._fail_run(
                run_id,
                terminal_reason="state_integrity_failure",
                receipt={"qualifying": False, "reason": "state_integrity_failure"},
            )
        except Exception:  # noqa: BLE001 - nothing safe left to persist
            pass

    def _terminalize_cancel(self, run_id: str) -> None:
        for case in self._ledger.list_cases(run_id):
            for attempt in self._ledger.list_attempts(case.case_id):
                if attempt.status in ("pending", "reserved"):
                    self._ledger.cancel_attempt(attempt.attempt_id)
                elif attempt.status == "running":
                    # No live worker owns it; the transport outcome is unknown.
                    self._ledger.settle_attempt_with_budget(attempt.attempt_id, outcome="ambiguous")
        fresh = self._require_run(run_id)
        if fresh.is_terminal:
            return
        terminal = [attempt for attempt in self.list_attempts(run_id) if attempt.is_terminal]
        self._ledger.cancel_run(
            run_id,
            expected_revision=fresh.revision,
            terminal_reason="cancelled_by_owner",
            actual_spend=self._conservative_spend(fresh),
            terminal_receipt={
                "qualifying": False,
                "terminal_reason": "cancelled_by_owner",
                "attempts_terminal": len(terminal),
                "evidence_preserved": True,
            },
        )

    def _conservative_spend(self, run: QualificationRun) -> MoneyMicros:
        """Known spend plus unresolved reserves; reserves are never released."""

        return MoneyMicros(
            run.actual_spend.micros + run.unresolved_reserve.micros + run.inflight_reserve.micros
        )

    # -- helpers -----------------------------------------------------------------

    def _blockers(self, run: QualificationRun) -> tuple[str, ...]:
        blockers: list[str] = []
        attempts = self.list_attempts(run.run_id)
        if any(attempt.status == "ambiguous" for attempt in attempts) or (
            run.unresolved_reserve.micros > 0
        ):
            blockers.append("owner_reconciliation_required")
        if not run.is_terminal and run.effective_stop_cap.micros == 0:
            blockers.append("budget_admissions_closed")
        return tuple(blockers)

    def _require_run(self, run_id: str) -> QualificationRun:
        _require_text(run_id, "run_id")
        run = self._ledger.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown qualification run: {run_id}")
        return run

    def _status_in(self, run_id: str, statuses: tuple[str, ...]) -> bool:
        run = self._ledger.get_run(run_id)
        return run is not None and run.status in statuses

    def _require_execution_config(self) -> None:
        missing = []
        if self._executor is None:
            missing.append("executor")
        if self._lease_factory is None:
            missing.append("lease_factory")
        if self._prices is None:
            missing.append("prices")
        if self._token_ceilings is None:
            missing.append("token_ceilings")
        if missing:
            raise RuntimeError("qualification execution requires: " + ", ".join(missing))

    def _budget(self, run_id: str) -> QualificationBudget:
        self._require_execution_config()
        assert self._prices is not None
        assert self._token_ceilings is not None
        return QualificationBudget(
            self._ledger,
            run_id,
            prices=self._prices,
            token_ceilings=self._token_ceilings,
        )
