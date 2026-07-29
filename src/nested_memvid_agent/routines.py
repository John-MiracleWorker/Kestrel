from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .routine_limits import validate_routine_claim_ttl, validate_routines_per_tick
from .security_boundary import redact_secrets, redact_text
from .state_store import (
    AgentStateStore,
    RoutineDeliveryRecord,
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


class RoutineResultDelivery(Protocol):
    def deliver_routine_result(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...


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
        delivery: RoutineResultDelivery | None = None,
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
        self.delivery = delivery

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
        self._reconcile_deliveries(instant)
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
        reconciled = self._reconcile_running(instant)
        self._reconcile_deliveries(instant)
        return reconciled

    def reconcile_delivery(
        self,
        delivery_id: str,
        *,
        expected_attempt_count: int,
        resolution: str,
        receipt: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> RoutineDeliveryRecord:
        instant = _utc_instant(now if now is not None else self.clock())
        current = self.state.reconcile_routine_delivery(
            delivery_id,
            expected_attempt_count=expected_attempt_count,
            resolution=resolution,
            receipt=receipt,
            now=instant,
        )
        if current.status == "pending":
            return self._attempt_delivery(current, instant)
        return current

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
        if completed and current.status == "completed":
            self._deliver_completed_occurrence(current, run, instant)
        return current

    def _deliver_completed_occurrence(
        self,
        occurrence: RoutineOccurrenceRecord,
        run: RunRecord,
        instant: datetime,
    ) -> RoutineDeliveryRecord | None:
        destination = occurrence.request.get("delivery")
        if not isinstance(destination, dict) or not destination:
            return None
        delivery = self.state.ensure_routine_delivery(
            occurrence.occurrence_id,
            destination=destination,
            now=instant,
        )
        if delivery.status == "delivered":
            return delivery
        return self._attempt_delivery(delivery, instant, run=run)

    def _reconcile_deliveries(self, instant: datetime) -> tuple[str, ...]:
        reconciled = list(self.state.expire_routine_delivery_claims(now=instant))
        pending = self.state.list_routine_deliveries(
            statuses=("pending", "failed"),
            limit=max(100, self.max_occurrences_per_tick * 10),
        )
        for delivery in pending:
            if delivery.attempt_count >= 3:
                continue
            current = self._attempt_delivery(delivery, instant)
            if current.status != delivery.status:
                reconciled.append(current.delivery_id)
        return tuple(dict.fromkeys(reconciled))

    def _attempt_delivery(
        self,
        delivery: RoutineDeliveryRecord,
        instant: datetime,
        *,
        run: RunRecord | None = None,
    ) -> RoutineDeliveryRecord:
        claimed, acquired = self.state.claim_routine_delivery(
            delivery.delivery_id,
            claim_owner=self.claim_owner,
            lease_ttl_seconds=self.claim_ttl_seconds,
            now=instant,
        )
        if not acquired:
            return claimed
        destination = claimed.destination
        if run is None:
            try:
                run = self.state.get_run(claimed.run_id)
            except KeyError:
                return self._finish_delivery(
                    claimed,
                    status="failed",
                    error="routine delivery run record is missing",
                    now=instant,
                )
        if self.delivery is None:
            return self._finish_delivery(
                claimed,
                status="blocked",
                error="routine delivery manager is unavailable",
                now=instant,
            )
        text = _routine_delivery_text(destination, run)
        try:
            receipt = self.delivery.deliver_routine_result(
                channel_id=str(destination["channel_id"]),
                conversation_id=str(destination["conversation_id"]),
                text=text,
                idempotency_key=claimed.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 - ambiguity is preserved durably
            return self._finish_delivery(
                claimed,
                status="uncertain",
                error=redact_text(f"{type(exc).__name__}: {exc}"),
                now=instant,
            )
        public_receipt = _redacted_mapping(receipt)
        delivery_payload = receipt.get("delivery")
        delivery_detail = (
            delivery_payload if isinstance(delivery_payload, dict) else receipt
        )
        if bool(receipt.get("ok")) or bool(delivery_detail.get("sent")):
            status = "delivered"
            error = None
        elif bool(delivery_detail.get("dry_run")) or delivery_detail.get(
            "blocked_reason"
        ):
            status = "blocked"
            error = str(
                delivery_detail.get("blocked_reason") or "routine delivery blocked"
            )
        elif delivery_detail.get("status_code") is None:
            status = "uncertain"
            error = str(delivery_detail.get("error") or "delivery outcome uncertain")
        else:
            status = "failed"
            error = str(delivery_detail.get("error") or "routine delivery failed")
        return self._finish_delivery(
            claimed,
            status=status,
            receipt=public_receipt,
            error=redact_text(error) if error else None,
            now=instant,
        )

    def _finish_delivery(
        self,
        delivery: RoutineDeliveryRecord,
        *,
        status: str,
        receipt: dict[str, Any] | None = None,
        error: str | None = None,
        now: datetime,
    ) -> RoutineDeliveryRecord:
        current, _ = self.state.finish_routine_delivery(
            delivery.delivery_id,
            claim_owner=self.claim_owner,
            claim_generation=delivery.claim_generation,
            status=status,
            receipt=receipt,
            error=error,
            now=now,
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


def _routine_delivery_text(destination: dict[str, Any], run: RunRecord) -> str:
    result = run.assistant_message.strip() or run.stop_reason or run.status
    template = str(destination.get("template") or "{result}")
    replacements = {
        "{result}": result,
        "{run_id}": run.run_id,
        "{run_status}": run.status,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template[:20_000]


def _redacted_mapping(value: dict[str, Any]) -> dict[str, Any]:
    def clean(item: object) -> object:
        if isinstance(item, dict):
            return {str(key): clean(nested) for key, nested in item.items()}
        if isinstance(item, list):
            return [clean(nested) for nested in item]
        if isinstance(item, tuple):
            return [clean(nested) for nested in item]
        if isinstance(item, str):
            return redact_text(item)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return redact_text(str(item))

    cleaned = clean(redact_secrets(value))
    return cleaned if isinstance(cleaned, dict) else {}
