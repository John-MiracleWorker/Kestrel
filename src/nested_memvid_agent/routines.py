from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .routine_limits import validate_routine_claim_ttl, validate_routines_per_tick
from .security_boundary import redact_text
from .state_store import (
    AgentStateStore,
    RoutineOccurrenceRecord,
    RunRecord,
)

Clock = Callable[[], datetime]


class ScheduledRunManager(Protocol):
    def create_scheduled_routine_run(
        self,
        *,
        routine_id: str,
        occurrence_id: str,
        claim_owner: str,
        claim_generation: int,
        dispatch_at: datetime,
        message: str,
        workspace: Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        autonomy_mode: str = "background",
    ) -> RunRecord: ...


@dataclass(frozen=True)
class RoutineDispatchResult:
    occurrence_id: str
    routine_id: str
    run_id: str
    status: str
    error: str | None = None

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RoutineTickResult:
    ticked_at: str
    claim_owner: str
    claimed: int
    skipped: tuple[str, ...]
    reconciled: tuple[str, ...]
    dispatches: tuple[RoutineDispatchResult, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "ticked_at": self.ticked_at,
            "claim_owner": self.claim_owner,
            "claimed": self.claimed,
            "skipped": list(self.skipped),
            "reconciled": list(self.reconciled),
            "dispatches": [item.to_payload() for item in self.dispatches],
        }


@dataclass(frozen=True)
class RoutineRunNowResult:
    requested_at: str
    claim_owner: str
    idempotent_replay: bool
    occurrence: RoutineOccurrenceRecord
    dispatch: RoutineDispatchResult | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "requested_at": self.requested_at,
            "claim_owner": self.claim_owner,
            "idempotent_replay": self.idempotent_replay,
            "occurrence": asdict(self.occurrence),
            "dispatch": self.dispatch.to_payload() if self.dispatch is not None else None,
        }


class RoutineService:
    """Claim due UTC routines and dispatch internally scoped durable runs.

    This service intentionally contains no polling thread. Callers may invoke
    ``tick`` manually with an injected instant or place it behind a separately
    configured lifecycle loop. SQLite claims make concurrent ticks safe.
    """

    def __init__(
        self,
        state: AgentStateStore,
        runs: ScheduledRunManager,
        *,
        clock: Clock | None = None,
        claim_owner: str | None = None,
        claim_ttl_seconds: float = 30.0,
        max_occurrences_per_tick: int = 10,
    ) -> None:
        self.state = state
        self.runs = runs
        self.clock = clock or (lambda: datetime.now(UTC))
        self.claim_owner = claim_owner or f"routine_{os.getpid()}_{uuid4().hex}"
        self.claim_ttl_seconds = validate_routine_claim_ttl(
            claim_ttl_seconds,
            field_name="claim_ttl_seconds",
        )
        self.max_occurrences_per_tick = validate_routines_per_tick(
            max_occurrences_per_tick,
            field_name="max_occurrences_per_tick",
        )

    def tick(self, now: datetime | None = None) -> RoutineTickResult:
        instant = _utc_instant(now if now is not None else self.clock())
        reconciled = self.reconcile(now=instant)
        batch = self.state.claim_due_routine_occurrences(
            now=instant,
            claim_owner=self.claim_owner,
            lease_ttl_seconds=self.claim_ttl_seconds,
            limit=self.max_occurrences_per_tick,
        )
        dispatches = tuple(self._dispatch(item, instant) for item in batch.claimed)
        reconciled += self._reconcile_running(instant)
        return RoutineTickResult(
            ticked_at=instant.isoformat(),
            claim_owner=self.claim_owner,
            claimed=len(batch.claimed),
            skipped=tuple(item.occurrence_id for item in batch.skipped),
            reconciled=tuple(dict.fromkeys(reconciled)),
            dispatches=dispatches,
        )

    def run_now(
        self,
        routine_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> RoutineRunNowResult:
        """Idempotently dispatch one owner-selected routine without ticking others."""

        instant = _utc_instant(now if now is not None else self.clock())
        claim = self.state.claim_manual_routine_occurrence(
            routine_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            now=instant,
            claim_owner=self.claim_owner,
            lease_ttl_seconds=self.claim_ttl_seconds,
        )
        dispatch = self._dispatch(claim.occurrence, instant) if claim.dispatch else None
        occurrence = self.state.get_routine_occurrence(claim.occurrence.occurrence_id)
        if occurrence.status == "running":
            try:
                run = self.state.get_run(occurrence.run_id)
            except KeyError:
                run = None
            if run is not None and run.status in {"completed", "failed", "cancelled"}:
                occurrence = self._finish_from_run(occurrence, run, instant)
        return RoutineRunNowResult(
            requested_at=occurrence.requested_at or instant.isoformat(),
            claim_owner=self.claim_owner,
            idempotent_replay=not claim.created,
            occurrence=occurrence,
            dispatch=dispatch,
        )

    def reconcile(self, now: datetime | None = None) -> tuple[str, ...]:
        """Terminalize linked occurrences without claiming any new work."""

        instant = _utc_instant(now if now is not None else self.clock())
        expire_approvals = getattr(self.runs, "expire_pending_approvals", None)
        if callable(expire_approvals):
            expire_approvals()
        return self._reconcile_running(instant)

    def _dispatch(
        self,
        occurrence: RoutineOccurrenceRecord,
        instant: datetime,
    ) -> RoutineDispatchResult:
        request = occurrence.request
        try:
            routine = self.state.get_routine(occurrence.routine_id)
        except KeyError:
            current, _ = self.state.skip_routine_occurrence(
                occurrence.occurrence_id,
                reason="routine_missing_before_dispatch",
                now=instant,
            )
            return _dispatch_result(current)
        if routine.deleted_at is not None:
            current, _ = self.state.skip_routine_occurrence(
                occurrence.occurrence_id,
                reason="routine_deleted_before_dispatch",
                now=instant,
            )
            return _dispatch_result(current)
        if not routine.enabled:
            current, _ = self.state.skip_routine_occurrence(
                occurrence.occurrence_id,
                reason="routine_disabled_before_dispatch",
                now=instant,
            )
            return _dispatch_result(current)
        if routine.revision != occurrence.routine_revision:
            current, _ = self.state.skip_routine_occurrence(
                occurrence.occurrence_id,
                reason="routine_changed_before_dispatch",
                now=instant,
            )
            return _dispatch_result(current)
        try:
            run = self.runs.create_scheduled_routine_run(
                routine_id=occurrence.routine_id,
                occurrence_id=occurrence.occurrence_id,
                claim_owner=self.claim_owner,
                claim_generation=occurrence.claim_generation,
                dispatch_at=instant,
                message=str(request["prompt"]),
                workspace=(
                    Path(str(request["workspace"]))
                    if request.get("workspace")
                    else None
                ),
                provider=_optional_request_string(request.get("provider")),
                model=_optional_request_string(request.get("model")),
                autonomy_mode=str(request.get("autonomy_mode") or "background"),
            )
        except Exception as exc:  # noqa: BLE001 - occurrence records preserve dispatch failures
            error = redact_text(f"{type(exc).__name__}: {exc}")
            current = self.state.get_routine_occurrence(occurrence.occurrence_id)
            if current.status == "claimed":
                self.state.release_routine_occurrence_claim(
                    occurrence.occurrence_id,
                    claim_owner=self.claim_owner,
                    claim_generation=occurrence.claim_generation,
                    error=error,
                    now=instant,
                )
                return RoutineDispatchResult(
                    occurrence_id=occurrence.occurrence_id,
                    routine_id=occurrence.routine_id,
                    run_id=occurrence.run_id,
                    status="deferred",
                    error=error,
                )
            if current.status != "running":
                return _dispatch_result(current)
            current, _ = self.state.finish_routine_occurrence(
                occurrence.occurrence_id,
                run_id=occurrence.run_id,
                status="failed",
                error=error,
                now=instant,
            )
            return _dispatch_result(current)
        current = self.state.get_routine_occurrence(occurrence.occurrence_id)
        if run.status in {"completed", "failed", "cancelled"}:
            current = self._finish_from_run(current, run, instant)
        return _dispatch_result(current)

    def _reconcile_running(self, instant: datetime) -> tuple[str, ...]:
        reconciled: list[str] = []
        occurrences = self.state.list_reconcilable_routine_occurrences(
            limit=max(100, self.max_occurrences_per_tick * 10),
        )
        for occurrence in occurrences:
            try:
                run = self.state.get_run(occurrence.run_id)
            except KeyError:
                current, applied = self.state.finish_routine_occurrence(
                    occurrence.occurrence_id,
                    run_id=occurrence.run_id,
                    status="failed",
                    error="scheduled routine run record is missing",
                    now=instant,
                )
                if applied:
                    reconciled.append(current.occurrence_id)
                continue
            if run.status not in {"completed", "failed", "cancelled"}:
                continue
            current = self._finish_from_run(occurrence, run, instant)
            reconciled.append(current.occurrence_id)
        return tuple(reconciled)

    def _finish_from_run(
        self,
        occurrence: RoutineOccurrenceRecord,
        run: RunRecord,
        instant: datetime,
    ) -> RoutineOccurrenceRecord:
        completed = run.status == "completed"
        current, _ = self.state.finish_routine_occurrence(
            occurrence.occurrence_id,
            run_id=run.run_id,
            status="completed" if completed else "failed",
            result={
                "run_status": run.status,
                "stop_reason": run.stop_reason,
            },
            error=None if completed else redact_text(run.error or run.stop_reason or run.status),
            now=instant,
        )
        return current


def _utc_instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("routine tick time must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _optional_request_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _dispatch_result(occurrence: RoutineOccurrenceRecord) -> RoutineDispatchResult:
    return RoutineDispatchResult(
        occurrence_id=occurrence.occurrence_id,
        routine_id=occurrence.routine_id,
        run_id=occurrence.run_id,
        status=occurrence.status,
        error=occurrence.error,
    )
