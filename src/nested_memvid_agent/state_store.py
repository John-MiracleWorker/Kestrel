from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from threading import RLock
from time import sleep
from typing import Any

from .file_lock import lock_exclusive, unlock
from .platform_primitives import chmod_descriptor
from .private_artifacts import open_private_file_descriptor
from .routine_limits import (
    MAX_ROUTINE_INTERVAL_SECONDS,
    MAX_ROUTINE_MISFIRE_GRACE_SECONDS,
    MIN_ROUTINE_INTERVAL_SECONDS,
    MIN_ROUTINE_MISFIRE_GRACE_SECONDS,
    validate_routine_claim_ttl,
    validate_routine_history_limit,
    validate_routine_interval,
    validate_routine_misfire_grace,
    validate_routine_reconciliation_limit,
    validate_routines_per_tick,
)
from .security_boundary import redact_text

SCHEMA_VERSION = 19
DEFAULT_APPROVAL_TTL_SECONDS = 900.0
CAPABILITY_KINDS = frozenset({"tool", "mcp_server", "skill"})
_STATE_DIRECTORY_MODE = 0o700
_SQLITE_FILE_MODE = 0o600
_SQLITE_PRIVATE_SUFFIXES = ("", "-wal", "-shm", "-journal")
_SQLITE_CONNECTION_SETUP_ATTEMPTS = 5
_SQLITE_CONNECTION_SETUP_RETRY_BASE_SECONDS = 0.05
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
_ABORTED_RUN_STATUSES = {"failed", "cancelled"}
_SCHEMA_MIGRATION_LOCK = RLock()


class StateCapacityError(RuntimeError):
    """Raised when an atomic durable run admission would exceed configured capacity."""


class ApprovalConflictError(RuntimeError):
    """Raised when a run already has a different live exact-call approval."""

    def __init__(self, approval: dict[str, Any]) -> None:
        self.approval = approval
        super().__init__("Run already has a different pending exact-call approval")


class CapabilityConflictError(RuntimeError):
    """Raised when a capability override compare-and-swap revision is stale."""

    def __init__(self, current: dict[str, Any]) -> None:
        self.current = current
        super().__init__("capability_revision_conflict")


class RoutineConflictError(RuntimeError):
    """Raised when a routine compare-and-swap revision is stale."""

    def __init__(self, current: RoutineRecord) -> None:
        self.current = current
        super().__init__("routine_revision_conflict")


class RoutineRunNowConflictError(RuntimeError):
    """Raised when an owner-requested routine trigger cannot be admitted."""

    def __init__(
        self,
        code: str,
        *,
        current: RoutineRecord | None = None,
        occurrence: RoutineOccurrenceRecord | None = None,
    ) -> None:
        self.code = code
        self.current = current
        self.occurrence = occurrence
        super().__init__(code)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: str
    message: str
    session_id: str
    workspace: str
    provider: str
    model: str
    assistant_message: str = ""
    context_chars: int = 0
    tool_count: int = 0
    stop_reason: str = ""
    error: str | None = None
    lease_owner: str | None = None
    lease_generation: int = 0
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    interrupted_at: str | None = None
    recovery_reason: str = ""
    config_revision: str | None = None
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    turn_source: dict[str, Any] | None = None
    turn_origin: str = "primary_user"
    transcript_scope: str = "primary"
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class TaskNodeRecord:
    task_id: str
    run_id: str
    title: str
    goal: str
    profile: str
    status: str
    parent_id: str | None = None
    approved: bool = False
    plan: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    dependencies: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    risk: str = "low"
    acceptance_criteria: tuple[str, ...] = ()
    attempt_count: int = 0
    failure_reason: str = ""
    diagnosis: dict[str, Any] | None = None
    retry_strategy: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class SubagentRunRecord:
    subagent_id: str
    run_id: str
    profile: str
    goal: str
    status: str
    task_id: str | None = None
    result: str = ""
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class TraceSpanRecord:
    span_id: str
    run_id: str
    span_type: str
    name: str
    status: str
    parent_span_id: str | None = None
    metadata: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = ""
    ended_at: str | None = None


@dataclass(frozen=True)
class RoutineRecord:
    routine_id: str
    name: str
    prompt: str
    schedule_kind: str
    start_at: str
    interval_seconds: int | None
    enabled: bool
    revision: int
    next_run_at: str | None
    workspace: str | None = None
    provider: str | None = None
    model: str | None = None
    autonomy_mode: str = "background"
    misfire_grace_seconds: int = 60
    last_scheduled_at: str | None = None
    deleted_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RoutineOccurrenceRecord:
    occurrence_id: str
    routine_id: str
    routine_revision: int
    scheduled_for: str
    status: str
    run_id: str
    request: dict[str, Any] = field(default_factory=dict)
    trigger_kind: str = "scheduled"
    trigger_key_digest: str | None = None
    requested_at: str | None = None
    claim_owner: str | None = None
    claim_generation: int = 1
    claim_expires_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    skip_reason: str | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RoutineClaimBatch:
    claimed: tuple[RoutineOccurrenceRecord, ...] = ()
    skipped: tuple[RoutineOccurrenceRecord, ...] = ()


@dataclass(frozen=True)
class RoutineManualClaim:
    occurrence: RoutineOccurrenceRecord
    dispatch: bool
    created: bool


class AgentStateStore:
    """SQLite control-plane state for runs, approvals, capabilities, and extensions."""

    def __init__(
        self,
        path: Path,
        *,
        routine_admission_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self._routine_admission_clock = routine_admission_clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        with _SCHEMA_MIGRATION_LOCK:
            with _state_initialization_lock(self.path):
                _prepare_private_sqlite_storage(self.path)
                self._migrate_schema()
                self._enable_wal_mode()
                _harden_private_sqlite_files(self.path)

    def create_run(
        self,
        *,
        run_id: str,
        message: str,
        session_id: str,
        workspace: str,
        model: str,
        provider: str = "mock",
        config_revision: str | None = None,
        config_snapshot: dict[str, Any] | None = None,
        turn_source: dict[str, Any] | None = None,
        turn_origin: str = "primary_user",
        transcript_scope: str = "primary",
        max_nonterminal_runs: int | None = None,
    ) -> RunRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if max_nonterminal_runs is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM runs WHERE status IN ('queued', 'running', 'blocked')"
                ).fetchone()
                count = int(row["count"]) if row is not None else 0
                if count >= max(1, max_nonterminal_runs):
                    raise StateCapacityError("durable_run_capacity_exhausted")
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, status, message, session_id, workspace, provider, model,
                    assistant_message, context_chars, tool_count, stop_reason, error,
                    config_revision, config_snapshot_json, turn_source_json,
                    turn_origin, transcript_scope, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, '', 0, 0, '', NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    message,
                    session_id,
                    workspace,
                    provider,
                    model,
                    config_revision,
                    _encode(config_snapshot or {}),
                    _encode(turn_source) if turn_source is not None else None,
                    turn_origin,
                    transcript_scope,
                    now,
                    now,
                ),
            )
        return self.get_run(run_id)

    def create_run_for_routine_occurrence(
        self,
        *,
        occurrence_id: str,
        claim_owner: str,
        claim_generation: int,
        dispatch_at: datetime,
        run_id: str,
        message: str,
        session_id: str,
        workspace: str,
        model: str,
        provider: str,
        config_revision: str,
        config_snapshot: dict[str, Any],
        max_nonterminal_runs: int | None = None,
    ) -> tuple[RunRecord, bool]:
        """Atomically fence a routine claim, persist its run, and mark it running."""

        instant = _lease_instant(dispatch_at)
        owner = claim_owner.strip()
        if not owner:
            raise ValueError("routine claim owner is required")
        if isinstance(claim_generation, bool) or claim_generation <= 0:
            raise ValueError("routine claim generation must be positive")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            admission_instant = _lease_instant(self._routine_admission_clock())
            occurrence_row = conn.execute(
                """
                SELECT o.*, r.enabled AS routine_enabled,
                    r.revision AS current_revision, r.deleted_at AS routine_deleted_at
                FROM routine_occurrences AS o
                JOIN routines AS r ON r.routine_id = o.routine_id
                WHERE o.occurrence_id = ?
                """,
                (occurrence_id,),
            ).fetchone()
            if occurrence_row is None:
                raise KeyError(f"Unknown routine occurrence: {occurrence_id}")
            occurrence = _routine_occurrence_from_row(occurrence_row)
            expected_run_id = routine_run_id(
                occurrence.routine_id,
                occurrence.occurrence_id,
            )
            if occurrence.run_id != expected_run_id or run_id != expected_run_id:
                raise ValueError("routine occurrence run identity mismatch")
            if occurrence_row["routine_deleted_at"] is not None:
                raise ValueError("routine deleted before dispatch")
            if not bool(occurrence_row["routine_enabled"]):
                raise ValueError("routine disabled before dispatch")
            if int(occurrence_row["current_revision"]) != occurrence.routine_revision:
                raise ValueError("routine changed before dispatch")
            routine_provenance = {
                "routine_id": occurrence.routine_id,
                "occurrence_id": occurrence.occurrence_id,
                "routine_revision": occurrence.routine_revision,
                "scheduled_for": occurrence.scheduled_for,
                "claim_generation": occurrence.claim_generation,
            }
            if occurrence.trigger_kind == "manual":
                routine_provenance.update(
                    {
                        "trigger_kind": "manual",
                        "trigger_key_digest": occurrence.trigger_key_digest,
                        "requested_at": occurrence.requested_at,
                    }
                )
            existing_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if existing_row is not None:
                existing = _run_from_row(existing_row)
                if (
                    existing.message != message
                    or existing.session_id != session_id
                    or existing.turn_source is not None
                    or existing.turn_origin != "scheduled_routine"
                    or existing.transcript_scope != "internal"
                    or existing.config_snapshot.get("routine_provenance") != routine_provenance
                ):
                    raise ValueError("scheduled_routine_run_identity_conflict")
                if occurrence.status not in {"running", "completed", "failed"}:
                    raise ValueError("scheduled routine run has invalid occurrence state")
                return existing, False
            if occurrence.status != "claimed":
                raise ValueError("routine occurrence is not claimed")
            if occurrence.claim_owner != owner or occurrence.claim_generation != claim_generation:
                raise ValueError("routine occurrence claim fence lost")
            expiry = _parse_timestamp(occurrence.claim_expires_at)
            if expiry is None or expiry <= admission_instant:
                raise ValueError("routine occurrence claim expired")
            if max_nonterminal_runs is not None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM runs
                    WHERE status IN ('queued', 'running', 'blocked')
                    """
                ).fetchone()
                count = int(row["count"]) if row is not None else 0
                if count >= max(1, max_nonterminal_runs):
                    raise StateCapacityError("durable_run_capacity_exhausted")
            now = instant.isoformat()
            persisted_config_snapshot = dict(config_snapshot)
            persisted_config_snapshot["routine_provenance"] = routine_provenance
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, status, message, session_id, workspace, provider, model,
                    assistant_message, context_chars, tool_count, stop_reason, error,
                    config_revision, config_snapshot_json, turn_source_json,
                    turn_origin, transcript_scope, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, '', 0, 0, '', NULL, ?, ?, NULL,
                    'scheduled_routine', 'internal', ?, ?)
                """,
                (
                    run_id,
                    message,
                    session_id,
                    workspace,
                    provider,
                    model,
                    config_revision,
                    _encode(persisted_config_snapshot),
                    now,
                    now,
                ),
            )
            cursor = conn.execute(
                """
                UPDATE routine_occurrences SET status = 'running', started_at = ?,
                    claim_expires_at = NULL, error = NULL, updated_at = ?
                WHERE occurrence_id = ? AND status = 'claimed'
                    AND claim_owner = ? AND claim_generation = ?
                """,
                (now, now, occurrence_id, owner, claim_generation),
            )
            if cursor.rowcount != 1:
                raise ValueError("routine occurrence claim fence lost")
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise RuntimeError("scheduled routine run insert did not persist")
            return _run_from_row(run_row), True

    def update_run(self, run_id: str, **fields: object) -> RunRecord:
        if not fields:
            return self.get_run(run_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{_validated_column('runs', key)} = ?" for key in fields)
        values = [_encode(value) for value in fields.values()]
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE run_id = ?", values)  # nosec
        return self.get_run(run_id)

    def record_cancelled_run_durability_failure(
        self,
        run_id: str,
        *,
        error: str,
        recovery_reason: str,
    ) -> tuple[RunRecord, bool]:
        """Annotate an immutable cancelled run when its final close was not durable."""

        if not error.strip():
            raise ValueError("cancelled run durability error is required")
        if not recovery_reason.strip():
            raise ValueError("cancelled run durability recovery reason is required")
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE runs SET error = ?, recovery_reason = ?, updated_at = ?
                WHERE run_id = ? AND status = 'cancelled'
                """,
                (error, recovery_reason, now, run_id),
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            return _run_from_row(row), cursor.rowcount == 1

    def transition_run(
        self,
        run_id: str,
        status: str,
        *,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
        transition_at: datetime | None = None,
        expected_statuses: tuple[str, ...] | None = None,
        expected_stop_reason: str | None = None,
        **fields: object,
    ) -> RunRecord:
        """Apply a guarded and optionally lease-fenced run lifecycle transition."""
        if (lease_owner is None) != (lease_generation is None):
            raise ValueError("lease_owner and lease_generation must be provided together")
        if expected_statuses is not None and not expected_statuses:
            raise ValueError("expected_statuses cannot be empty")
        transition_instant = _lease_instant(transition_at)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if current_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            current = _run_from_row(current_row)
            if current.status in _TERMINAL_RUN_STATUSES:
                return current
            if expected_statuses is not None and current.status not in expected_statuses:
                return current
            if expected_stop_reason is not None and current.stop_reason != expected_stop_reason:
                return current
            if lease_owner is not None and (
                current.lease_owner != lease_owner or current.lease_generation != lease_generation
            ):
                return current
            if lease_owner is not None:
                expires_at = _parse_timestamp(current.lease_expires_at)
                if expires_at is None or expires_at <= transition_instant:
                    return current
            if not _run_transition_allowed(current.status, status):
                return current
            updates = dict(fields)
            updates["status"] = status
            if status in _TERMINAL_RUN_STATUSES or status == "blocked":
                updates.update(lease_owner=None, lease_expires_at=None, heartbeat_at=None)
            updates["updated_at"] = transition_instant.isoformat()
            assignments = ", ".join(f"{_validated_column('runs', key)} = ?" for key in updates)
            values = [_encode(value) for value in updates.values()]
            values.extend([run_id, current.status])
            predicates = ["run_id = ?", "status = ?"]
            if lease_owner is not None:
                predicates.extend(
                    ["lease_owner = ?", "lease_generation = ?", "lease_expires_at > ?"]
                )
                values.extend([lease_owner, lease_generation, transition_instant.isoformat()])
            cursor = conn.execute(  # nosec
                f"UPDATE runs SET {assignments} WHERE {' AND '.join(predicates)}",
                values,
            )
            if cursor.rowcount != 1:
                row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Unknown run: {run_id}")
                return _run_from_row(row)
            updated_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if updated_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            return _run_from_row(updated_row)

    def acquire_run_lease(
        self,
        run_id: str,
        *,
        owner: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> RunRecord | None:
        """Acquire or refresh exclusive execution ownership for a non-terminal run."""
        owner = owner.strip()
        if not owner:
            raise ValueError("lease owner is required")
        instant = _lease_instant(now)
        expires_at = instant + timedelta(seconds=_positive_ttl(ttl_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            current = _run_from_row(row)
            if current.status in _TERMINAL_RUN_STATUSES or current.status == "blocked":
                return None
            current_expiry = _parse_timestamp(current.lease_expires_at)
            if (
                current.lease_owner
                and current.lease_owner != owner
                and current_expiry
                and current_expiry > instant
            ):
                return None
            generation = current.lease_generation
            if current.lease_owner != owner or current_expiry is None or current_expiry <= instant:
                generation += 1
            conn.execute(
                """
                UPDATE runs SET lease_owner = ?, lease_generation = ?, lease_expires_at = ?,
                    heartbeat_at = ?, updated_at = ? WHERE run_id = ?
                """,
                (
                    owner,
                    generation,
                    expires_at.isoformat(),
                    instant.isoformat(),
                    instant.isoformat(),
                    run_id,
                ),
            )
        return self.get_run(run_id)

    def claim_run_for_startup_recovery(
        self,
        run_id: str,
        *,
        expected_status: str,
        expected_lease_owner: str | None,
        expected_lease_generation: int,
        expected_lease_expires_at: str | None,
        owner: str,
        ttl_seconds: float,
        allow_unexpired_observed_lease: bool = False,
        now: datetime | None = None,
    ) -> RunRecord | None:
        """Claim an exact stale run snapshot before startup mutates its state."""

        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("startup recovery owner is required")
        instant = _lease_instant(now)
        expires_at = instant + timedelta(seconds=_positive_ttl(ttl_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            current = _run_from_row(row)
            if current.status in _TERMINAL_RUN_STATUSES:
                return None
            if (
                current.status != expected_status
                or current.lease_owner != expected_lease_owner
                or current.lease_generation != expected_lease_generation
                or current.lease_expires_at != expected_lease_expires_at
            ):
                return None
            current_expiry = _parse_timestamp(current.lease_expires_at)
            if (
                current.lease_owner
                and current_expiry is not None
                and current_expiry > instant
                and not allow_unexpired_observed_lease
            ):
                return None
            generation = current.lease_generation + 1
            cursor = conn.execute(
                """
                UPDATE runs SET lease_owner = ?, lease_generation = ?, lease_expires_at = ?,
                    heartbeat_at = ?, updated_at = ?
                WHERE run_id = ? AND status = ? AND lease_generation = ?
                    AND lease_owner IS ? AND lease_expires_at IS ?
                """,
                (
                    normalized_owner,
                    generation,
                    expires_at.isoformat(),
                    instant.isoformat(),
                    instant.isoformat(),
                    run_id,
                    expected_status,
                    expected_lease_generation,
                    expected_lease_owner,
                    expected_lease_expires_at,
                ),
            )
            if cursor.rowcount != 1:
                return None
            updated = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if updated is None:
                raise RuntimeError("startup recovery run claim did not persist")
            return _run_from_row(updated)

    def recover_pending_approval_wait(
        self,
        run_id: str,
        approval_id: str,
        *,
        recovery_owner: str,
        recovery_generation: int,
        task_id: str | None = None,
        subagent_id: str | None = None,
        worker_owner: str | None = None,
        worker_claim_id: str | None = None,
    ) -> tuple[RunRecord, TaskNodeRecord | None, SubagentRunRecord | None, bool]:
        """Atomically restore a pending approval waiter and block its claimed run."""

        if (task_id is None) != (subagent_id is None):
            raise ValueError("pending approval task and subagent bindings must be paired")
        if task_id is not None and (
            not str(worker_owner or "").strip() or not str(worker_claim_id or "").strip()
        ):
            raise ValueError("pending approval worker identity is required")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            current_run = _run_from_row(run_row)
            if not _run_execution_lease_matches(
                run_row,
                owner=recovery_owner,
                generation=recovery_generation,
                instant=now,
                allowed_statuses={"queued", "running", "blocked"},
            ):
                return current_run, None, None, False
            approval_row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if approval_row is None:
                return current_run, None, None, False
            approval = _approval_from_row(approval_row)
            if approval["run_id"] != run_id or approval["status"] != "pending":
                return current_run, None, None, False

            current_task: TaskNodeRecord | None = None
            current_subagent: SubagentRunRecord | None = None
            if task_id is not None and subagent_id is not None:
                task_row = conn.execute(
                    "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
                ).fetchone()
                subagent_row = conn.execute(
                    "SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)
                ).fetchone()
                if task_row is None or subagent_row is None:
                    return current_run, None, None, False
                current_task = _task_from_row(task_row)
                current_subagent = _subagent_from_row(subagent_row)
                task_result = dict(current_task.result or {})
                continuation = task_result.get("approval_continuation")
                pair_status = (current_task.status, current_subagent.status)
                if (
                    current_task.run_id != run_id
                    or current_subagent.run_id != run_id
                    or current_subagent.task_id != task_id
                    or pair_status not in {("running", "running"), ("blocked", "blocked")}
                    or (
                        pair_status == ("running", "running")
                        and (
                            task_result.get("worker_owner") != worker_owner
                            or task_result.get("worker_claim_id") != worker_claim_id
                        )
                    )
                    or not isinstance(continuation, dict)
                    or continuation.get("approval_id") != approval_id
                    or continuation.get("tool_call_id") != approval.get("tool_call_id")
                    or continuation.get("task_id") != task_id
                    or continuation.get("subagent_id") != subagent_id
                    or continuation.get("worker_owner") != worker_owner
                    or continuation.get("worker_claim_id") != worker_claim_id
                ):
                    return current_run, current_task, current_subagent, False
                if pair_status == ("running", "running"):
                    task_cursor = conn.execute(
                        """
                        UPDATE task_nodes SET status = 'blocked', updated_at = ?
                        WHERE task_id = ? AND run_id = ? AND status = 'running'
                        """,
                        (now_text, task_id, run_id),
                    )
                    subagent_cursor = conn.execute(
                        """
                        UPDATE subagent_runs SET status = 'blocked', result = ?, updated_at = ?
                        WHERE subagent_id = ? AND run_id = ? AND task_id = ?
                            AND status = 'running'
                        """,
                        ("Approval required.", now_text, subagent_id, run_id, task_id),
                    )
                    if task_cursor.rowcount != 1 or subagent_cursor.rowcount != 1:
                        conn.rollback()
                        return current_run, current_task, current_subagent, False

            run_cursor = conn.execute(
                """
                UPDATE runs SET status = 'blocked', stop_reason = 'approval_required',
                    recovery_reason = 'preserved_pending_approval', lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL, updated_at = ?
                WHERE run_id = ? AND lease_owner = ? AND lease_generation = ?
                    AND lease_expires_at > ?
                    AND status IN ('queued', 'running', 'blocked')
                """,
                (
                    now_text,
                    run_id,
                    recovery_owner,
                    recovery_generation,
                    now_text,
                ),
            )
            if run_cursor.rowcount != 1:
                conn.rollback()
                return current_run, current_task, current_subagent, False
            updated_run_row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if updated_run_row is None:
                raise RuntimeError("pending approval run recovery did not persist")
            if task_id is not None and subagent_id is not None:
                updated_task_row = conn.execute(
                    "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
                ).fetchone()
                updated_subagent_row = conn.execute(
                    "SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)
                ).fetchone()
                if updated_task_row is None or updated_subagent_row is None:
                    raise RuntimeError("pending approval pair recovery did not persist")
                current_task = _task_from_row(updated_task_row)
                current_subagent = _subagent_from_row(updated_subagent_row)
            return _run_from_row(updated_run_row), current_task, current_subagent, True

    def claim_blocked_run_for_approval(
        self,
        run_id: str,
        *,
        owner: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> RunRecord | None:
        """Atomically publish an approval handoff and its new execution lease."""

        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("lease owner is required")
        instant = _lease_instant(now)
        expires_at = instant + timedelta(seconds=_positive_ttl(ttl_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            current = _run_from_row(row)
            if current.status not in {"blocked", "queued"}:
                return None
            current_expiry = _parse_timestamp(current.lease_expires_at)
            if current.lease_owner and current_expiry and current_expiry > instant:
                return None
            generation = current.lease_generation + 1
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = 'running', stop_reason = 'scheduler_approval_handoff',
                    lease_owner = ?, lease_generation = ?, lease_expires_at = ?,
                    heartbeat_at = ?, updated_at = ?
                WHERE run_id = ? AND status IN ('blocked', 'queued')
                """,
                (
                    normalized_owner,
                    generation,
                    expires_at.isoformat(),
                    instant.isoformat(),
                    instant.isoformat(),
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            updated = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if updated is None:
                raise RuntimeError("approval run handoff did not persist")
            return _run_from_row(updated)

    def renew_run_lease(
        self,
        run_id: str,
        *,
        owner: str,
        generation: int,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> RunRecord | None:
        instant = _lease_instant(now)
        expires_at = instant + timedelta(seconds=_positive_ttl(ttl_seconds))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE run_id = ? AND lease_owner = ? AND lease_generation = ?
                  AND lease_expires_at > ?
                  AND status NOT IN ('completed', 'failed', 'cancelled', 'blocked')
                """,
                (
                    expires_at.isoformat(),
                    instant.isoformat(),
                    instant.isoformat(),
                    run_id,
                    owner,
                    generation,
                    instant.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_run(run_id)

    def release_run_lease(self, run_id: str, *, owner: str, generation: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, updated_at = ?
                WHERE run_id = ? AND lease_owner = ? AND lease_generation = ?
                """,
                (utc_now(), run_id, owner, generation),
            )
        return cursor.rowcount == 1

    def run_lease_matches(self, run_id: str, *, owner: str, generation: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM runs WHERE run_id = ? AND lease_owner = ? AND lease_generation = ?
                  AND lease_expires_at > ?
                  AND status NOT IN ('completed', 'failed', 'cancelled', 'blocked')
                """,
                (run_id, owner, generation, utc_now()),
            ).fetchone()
        return row is not None

    def list_nonterminal_runs(self) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM runs WHERE status NOT IN ('completed', 'failed', 'cancelled')
                ORDER BY created_at ASC, run_id ASC"""
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def run_status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM runs GROUP BY status ORDER BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        return _run_from_row(row)

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def list_runs_for_session(self, session_id: str) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE session_id = ?
                ORDER BY created_at ASC, run_id ASC
                """,
                (session_id,),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def create_routine(
        self,
        *,
        routine_id: str,
        name: str,
        prompt: str,
        schedule_kind: str,
        start_at: datetime | str,
        interval_seconds: int | None = None,
        enabled: bool = False,
        workspace: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        autonomy_mode: str = "background",
        misfire_grace_seconds: int = 60,
    ) -> RoutineRecord:
        if enabled is not False:
            raise ValueError("routines must be created disabled")
        normalized = _normalize_routine_fields(
            routine_id=routine_id,
            name=name,
            prompt=prompt,
            schedule_kind=schedule_kind,
            start_at=start_at,
            interval_seconds=interval_seconds,
            enabled=enabled,
            workspace=workspace,
            provider=provider,
            model=model,
            autonomy_mode=autonomy_mode,
            misfire_grace_seconds=misfire_grace_seconds,
        )
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO routines (
                    routine_id, name, prompt, schedule_kind, start_at,
                    interval_seconds, enabled, revision, next_run_at, workspace,
                    provider, model, autonomy_mode, misfire_grace_seconds,
                    last_scheduled_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    normalized["routine_id"],
                    normalized["name"],
                    normalized["prompt"],
                    normalized["schedule_kind"],
                    normalized["start_at"],
                    normalized["interval_seconds"],
                    1 if normalized["enabled"] is True else 0,
                    normalized["start_at"],
                    normalized["workspace"],
                    normalized["provider"],
                    normalized["model"],
                    normalized["autonomy_mode"],
                    normalized["misfire_grace_seconds"],
                    now,
                    now,
                ),
            )
        return self.get_routine(str(normalized["routine_id"]))

    def get_routine(self, routine_id: str) -> RoutineRecord:
        normalized_id = _routine_identifier(routine_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routines WHERE routine_id = ?", (normalized_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown routine: {normalized_id}")
        return _routine_from_row(row)

    def list_routines(self) -> list[RoutineRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM routines WHERE deleted_at IS NULL
                ORDER BY created_at ASC, routine_id ASC
                """
            ).fetchall()
        return [_routine_from_row(row) for row in rows]

    def update_routine(
        self,
        routine_id: str,
        *,
        expected_revision: int,
        **fields: object,
    ) -> RoutineRecord:
        normalized_id = _routine_identifier(routine_id)
        expected_revision = _positive_routine_revision(
            expected_revision,
            field_name="expected_revision",
        )
        allowed = {
            "name",
            "prompt",
            "schedule_kind",
            "start_at",
            "interval_seconds",
            "enabled",
            "workspace",
            "provider",
            "model",
            "autonomy_mode",
            "misfire_grace_seconds",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"unsupported routine fields: {', '.join(unknown)}")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM routines WHERE routine_id = ?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown routine: {normalized_id}")
            current = _routine_from_row(row)
            if current.deleted_at is not None:
                raise ValueError("routine_deleted")
            if current.revision != expected_revision:
                raise RoutineConflictError(current)
            if not fields:
                return current
            merged: dict[str, object] = {
                "routine_id": current.routine_id,
                "name": current.name,
                "prompt": current.prompt,
                "schedule_kind": current.schedule_kind,
                "start_at": current.start_at,
                "interval_seconds": current.interval_seconds,
                "enabled": current.enabled,
                "workspace": current.workspace,
                "provider": current.provider,
                "model": current.model,
                "autonomy_mode": current.autonomy_mode,
                "misfire_grace_seconds": current.misfire_grace_seconds,
            }
            merged.update(fields)
            normalized = _normalize_routine_fields(**merged)
            schedule_changed = bool({"schedule_kind", "start_at", "interval_seconds"} & set(fields))
            claimed_once = conn.execute(
                """
                SELECT scheduled_for FROM routine_occurrences
                WHERE routine_id = ? AND routine_revision = ?
                  AND status = 'claimed'
                ORDER BY scheduled_for ASC LIMIT 1
                """,
                (normalized_id, current.revision),
            ).fetchone()
            next_run_at = str(normalized["start_at"]) if schedule_changed else current.next_run_at
            if (
                not schedule_changed
                and normalized["schedule_kind"] == "once"
                and normalized["enabled"] is True
                and claimed_once is not None
            ):
                # A due one-shot has already cleared next_run_at. If an owner
                # revision fences that still-claimed occurrence, carry its UTC
                # slot into the new revision so the one-shot is not lost.
                next_run_at = str(claimed_once["scheduled_for"])
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE routines SET name = ?, prompt = ?, schedule_kind = ?,
                    start_at = ?, interval_seconds = ?, enabled = ?, revision = ?,
                    next_run_at = ?, workspace = ?, provider = ?, model = ?,
                    autonomy_mode = ?, misfire_grace_seconds = ?, updated_at = ?
                WHERE routine_id = ? AND revision = ?
                """,
                (
                    normalized["name"],
                    normalized["prompt"],
                    normalized["schedule_kind"],
                    normalized["start_at"],
                    normalized["interval_seconds"],
                    1 if normalized["enabled"] is True else 0,
                    current.revision + 1,
                    next_run_at,
                    normalized["workspace"],
                    normalized["provider"],
                    normalized["model"],
                    normalized["autonomy_mode"],
                    normalized["misfire_grace_seconds"],
                    now,
                    normalized_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                latest = conn.execute(
                    "SELECT * FROM routines WHERE routine_id = ?", (normalized_id,)
                ).fetchone()
                if latest is None:
                    raise KeyError(f"Unknown routine: {normalized_id}")
                raise RoutineConflictError(_routine_from_row(latest))
            updated = conn.execute(
                "SELECT * FROM routines WHERE routine_id = ?", (normalized_id,)
            ).fetchone()
            if updated is None:
                raise KeyError(f"Unknown routine: {normalized_id}")
            _skip_claimed_occurrences_for_routine(
                conn,
                normalized_id,
                instant=_require_timestamp(now, "updated_at"),
                reason=(
                    "routine_disabled_before_dispatch"
                    if not bool(normalized["enabled"])
                    else "routine_changed_before_dispatch"
                ),
            )
            return _routine_from_row(updated)

    def delete_routine(
        self,
        routine_id: str,
        *,
        expected_revision: int,
    ) -> RoutineRecord:
        normalized_id = _routine_identifier(routine_id)
        expected_revision = _positive_routine_revision(
            expected_revision,
            field_name="expected_revision",
        )
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM routines WHERE routine_id = ?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown routine: {normalized_id}")
            current = _routine_from_row(row)
            if current.revision != expected_revision:
                raise RoutineConflictError(current)
            if current.deleted_at is not None:
                return current
            cursor = conn.execute(
                """
                UPDATE routines SET enabled = 0, revision = ?, next_run_at = NULL,
                    deleted_at = ?, updated_at = ?
                WHERE routine_id = ? AND revision = ? AND deleted_at IS NULL
                """,
                (
                    current.revision + 1,
                    now,
                    now,
                    normalized_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                latest = conn.execute(
                    "SELECT * FROM routines WHERE routine_id = ?", (normalized_id,)
                ).fetchone()
                if latest is None:
                    raise KeyError(f"Unknown routine: {normalized_id}")
                raise RoutineConflictError(_routine_from_row(latest))
            _skip_claimed_occurrences_for_routine(
                conn,
                normalized_id,
                instant=_require_timestamp(now, "deleted_at"),
                reason="routine_deleted_before_dispatch",
            )
            deleted = conn.execute(
                "SELECT * FROM routines WHERE routine_id = ?", (normalized_id,)
            ).fetchone()
            if deleted is None:
                raise KeyError(f"Unknown routine: {normalized_id}")
            return _routine_from_row(deleted)

    def claim_manual_routine_occurrence(
        self,
        routine_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        now: datetime,
        claim_owner: str,
        lease_ttl_seconds: float = 30.0,
    ) -> RoutineManualClaim:
        """Create or recover one idempotent owner-requested routine occurrence.

        The idempotency key is hashed before persistence. Replays return the
        original occurrence, while only a caller holding its live claim may
        dispatch it. This path never advances the definition's schedule.
        """

        normalized_id = _routine_identifier(routine_id)
        revision = _positive_routine_revision(
            expected_revision,
            field_name="expected_revision",
        )
        owner = claim_owner.strip()
        if not owner:
            raise ValueError("routine claim owner is required")
        instant = _lease_instant(now)
        claim_ttl = validate_routine_claim_ttl(
            lease_ttl_seconds,
            field_name="routine claim lease_ttl_seconds",
        )
        expires_at = instant + timedelta(seconds=claim_ttl)
        key_digest = _routine_trigger_key_digest(idempotency_key)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_row = conn.execute(
                """
                SELECT * FROM routine_occurrences
                WHERE routine_id = ? AND trigger_kind = 'manual'
                  AND trigger_key_digest = ?
                """,
                (normalized_id, key_digest),
            ).fetchone()
            if existing_row is not None:
                existing = _routine_occurrence_from_row(existing_row)
                if existing.routine_revision != revision:
                    current_row = conn.execute(
                        "SELECT * FROM routines WHERE routine_id = ?",
                        (normalized_id,),
                    ).fetchone()
                    raise RoutineRunNowConflictError(
                        "routine_idempotency_key_reused",
                        current=(
                            _routine_from_row(current_row)
                            if current_row is not None
                            else None
                        ),
                        occurrence=existing,
                    )
                if existing.status != "claimed":
                    return RoutineManualClaim(existing, dispatch=False, created=False)

                routine_row = conn.execute(
                    "SELECT * FROM routines WHERE routine_id = ?",
                    (normalized_id,),
                ).fetchone()
                if routine_row is None:
                    _skip_occurrence_row(
                        conn,
                        existing.occurrence_id,
                        instant=instant,
                        reason="routine_missing_before_dispatch",
                    )
                    return RoutineManualClaim(
                        _require_occurrence_row(conn, existing.occurrence_id),
                        dispatch=False,
                        created=False,
                    )
                routine = _routine_from_row(routine_row)
                invalid_reason = _routine_dispatch_invalid_reason(
                    routine,
                    occurrence_revision=existing.routine_revision,
                )
                if invalid_reason is not None:
                    _skip_occurrence_row(
                        conn,
                        existing.occurrence_id,
                        instant=instant,
                        reason=invalid_reason,
                    )
                    return RoutineManualClaim(
                        _require_occurrence_row(conn, existing.occurrence_id),
                        dispatch=False,
                        created=False,
                    )

                claim_expiry = _parse_timestamp(existing.claim_expires_at)
                if claim_expiry is not None and claim_expiry > instant:
                    return RoutineManualClaim(
                        existing,
                        dispatch=existing.claim_owner == owner,
                        created=False,
                    )
                cursor = conn.execute(
                    """
                    UPDATE routine_occurrences SET claim_owner = ?,
                        claim_generation = claim_generation + 1,
                        claim_expires_at = ?, error = NULL, updated_at = ?
                    WHERE occurrence_id = ? AND status = 'claimed'
                      AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                    """,
                    (
                        owner,
                        expires_at.isoformat(),
                        instant.isoformat(),
                        existing.occurrence_id,
                        instant.isoformat(),
                    ),
                )
                current = _require_occurrence_row(conn, existing.occurrence_id)
                return RoutineManualClaim(
                    current,
                    dispatch=cursor.rowcount == 1,
                    created=False,
                )

            routine_row = conn.execute(
                "SELECT * FROM routines WHERE routine_id = ?",
                (normalized_id,),
            ).fetchone()
            if routine_row is None:
                raise KeyError(f"Unknown routine: {normalized_id}")
            routine = _routine_from_row(routine_row)
            if routine.revision != revision:
                raise RoutineConflictError(routine)
            if routine.deleted_at is not None:
                raise RoutineRunNowConflictError("routine_deleted", current=routine)
            if not routine.enabled:
                raise RoutineRunNowConflictError("routine_disabled", current=routine)

            scheduled_for = _manual_occurrence_identity_instant(
                conn,
                routine_id=normalized_id,
                routine_revision=revision,
                requested_at=instant,
                trigger_key_digest=key_digest,
            )
            occurrence_id = routine_manual_occurrence_id(normalized_id, key_digest)
            run_id = routine_run_id(normalized_id, occurrence_id)
            request = {
                **_routine_request_snapshot(routine),
                "trigger_kind": "manual",
                "trigger_key_digest": key_digest,
                "requested_at": instant.isoformat(),
            }
            active = conn.execute(
                """
                SELECT 1 FROM routine_occurrences
                WHERE routine_id = ? AND status IN ('claimed', 'running')
                LIMIT 1
                """,
                (normalized_id,),
            ).fetchone()
            skip_reason = "overlap_active" if active is not None else None
            result = {
                "trigger_kind": "manual",
                "requested_at": instant.isoformat(),
            }
            conn.execute(
                """
                INSERT INTO routine_occurrences (
                    occurrence_id, routine_id, routine_revision,
                    scheduled_for, status, run_id, request_json,
                    trigger_kind, trigger_key_digest, requested_at,
                    claim_owner, claim_generation, claim_expires_at,
                    started_at, finished_at,
                    skip_reason, error, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, 1, ?, NULL, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    occurrence_id,
                    normalized_id,
                    revision,
                    scheduled_for.isoformat(),
                    "skipped" if skip_reason else "claimed",
                    run_id,
                    _encode(request),
                    key_digest,
                    instant.isoformat(),
                    None if skip_reason else owner,
                    None if skip_reason else expires_at.isoformat(),
                    instant.isoformat() if skip_reason else None,
                    skip_reason,
                    _encode(result),
                    instant.isoformat(),
                    instant.isoformat(),
                ),
            )
            occurrence = _require_occurrence_row(conn, occurrence_id)
            return RoutineManualClaim(
                occurrence,
                dispatch=skip_reason is None,
                created=True,
            )

    def claim_due_routine_occurrences(
        self,
        *,
        now: datetime,
        claim_owner: str,
        lease_ttl_seconds: float = 30.0,
        limit: int = 10,
    ) -> RoutineClaimBatch:
        instant = _lease_instant(now)
        owner = claim_owner.strip()
        if not owner:
            raise ValueError("routine claim owner is required")
        limit = validate_routines_per_tick(limit, field_name="routine claim limit")
        claim_ttl = validate_routine_claim_ttl(
            lease_ttl_seconds,
            field_name="routine claim lease_ttl_seconds",
        )
        expires_at = instant + timedelta(seconds=claim_ttl)
        claimed: list[RoutineOccurrenceRecord] = []
        skipped: list[RoutineOccurrenceRecord] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale_rows = conn.execute(
                """
                SELECT o.*, r.enabled AS routine_enabled,
                    r.revision AS current_revision, r.deleted_at AS routine_deleted_at
                FROM routine_occurrences AS o
                JOIN routines AS r ON r.routine_id = o.routine_id
                WHERE o.status = 'claimed' AND o.claim_expires_at <= ?
                ORDER BY o.scheduled_for ASC, o.occurrence_id ASC
                LIMIT ?
                """,
                (instant.isoformat(), limit),
            ).fetchall()
            for row in stale_rows:
                occurrence = _routine_occurrence_from_row(row)
                if row["routine_deleted_at"] is not None:
                    _skip_occurrence_row(
                        conn,
                        occurrence.occurrence_id,
                        instant=instant,
                        reason="routine_deleted_before_dispatch",
                    )
                    skipped.append(_require_occurrence_row(conn, occurrence.occurrence_id))
                    continue
                if not bool(row["routine_enabled"]):
                    _skip_occurrence_row(
                        conn,
                        occurrence.occurrence_id,
                        instant=instant,
                        reason="routine_disabled_before_dispatch",
                    )
                    skipped.append(_require_occurrence_row(conn, occurrence.occurrence_id))
                    continue
                if int(row["current_revision"]) != occurrence.routine_revision:
                    _skip_occurrence_row(
                        conn,
                        occurrence.occurrence_id,
                        instant=instant,
                        reason="routine_changed_before_dispatch",
                    )
                    skipped.append(_require_occurrence_row(conn, occurrence.occurrence_id))
                    continue
                conn.execute(
                    """
                    UPDATE routine_occurrences SET claim_owner = ?,
                        claim_generation = claim_generation + 1,
                        claim_expires_at = ?, error = NULL, updated_at = ?
                    WHERE occurrence_id = ? AND status = 'claimed'
                    """,
                    (
                        owner,
                        expires_at.isoformat(),
                        instant.isoformat(),
                        occurrence.occurrence_id,
                    ),
                )
                claimed.append(_require_occurrence_row(conn, occurrence.occurrence_id))
            remaining = max(0, limit - len(claimed))
            if remaining == 0:
                return RoutineClaimBatch(tuple(claimed), tuple(skipped))
            due_rows = conn.execute(
                """
                SELECT * FROM routines
                WHERE enabled = 1 AND deleted_at IS NULL
                  AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC, routine_id ASC
                LIMIT ?
                """,
                (instant.isoformat(), remaining),
            ).fetchall()
            for row in due_rows:
                routine = _routine_from_row(row)
                scheduled = _require_timestamp(routine.next_run_at, "next_run_at")
                occurrence_id = routine_occurrence_id(
                    routine.routine_id,
                    routine.revision,
                    scheduled.isoformat(),
                )
                run_id = routine_run_id(routine.routine_id, occurrence_id)
                request = _routine_request_snapshot(routine)
                active = conn.execute(
                    """
                    SELECT 1 FROM routine_occurrences
                    WHERE routine_id = ? AND status IN ('claimed', 'running')
                    LIMIT 1
                    """,
                    (routine.routine_id,),
                ).fetchone()
                lateness = max(0.0, (instant - scheduled).total_seconds())
                skip_reason: str | None = None
                if active is not None:
                    skip_reason = "overlap_active"
                elif lateness > routine.misfire_grace_seconds:
                    skip_reason = "misfire_grace_exceeded"
                next_run_at, missed_intervals = _next_routine_run_at(
                    routine, scheduled=scheduled, now=instant
                )
                result = {
                    "lateness_seconds": lateness,
                    "missed_intervals": missed_intervals,
                }
                try:
                    conn.execute(
                        """
                        INSERT INTO routine_occurrences (
                            occurrence_id, routine_id, routine_revision,
                            scheduled_for, status, run_id, request_json,
                            claim_owner, claim_generation, claim_expires_at,
                            started_at, finished_at,
                            skip_reason, error, result_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, NULL, ?, ?, ?)
                        """,
                        (
                            occurrence_id,
                            routine.routine_id,
                            routine.revision,
                            scheduled.isoformat(),
                            "skipped" if skip_reason else "claimed",
                            run_id,
                            _encode(request),
                            None if skip_reason else owner,
                            None if skip_reason else expires_at.isoformat(),
                            instant.isoformat() if skip_reason else None,
                            skip_reason,
                            _encode(result),
                            instant.isoformat(),
                            instant.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    pass
                conn.execute(
                    """
                    UPDATE routines SET next_run_at = ?, last_scheduled_at = ?,
                        updated_at = ? WHERE routine_id = ? AND revision = ?
                    """,
                    (
                        next_run_at,
                        scheduled.isoformat(),
                        instant.isoformat(),
                        routine.routine_id,
                        routine.revision,
                    ),
                )
                occurrence = _require_occurrence_row(conn, occurrence_id)
                if occurrence.status == "claimed":
                    claimed.append(occurrence)
                elif occurrence.status == "skipped":
                    skipped.append(occurrence)
        return RoutineClaimBatch(tuple(claimed), tuple(skipped))

    def get_routine_occurrence(self, occurrence_id: str) -> RoutineOccurrenceRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routine_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown routine occurrence: {occurrence_id}")
        return _routine_occurrence_from_row(row)

    def list_routine_occurrences(
        self,
        routine_id: str | None = None,
        *,
        statuses: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[RoutineOccurrenceRecord]:
        limit = validate_routine_history_limit(limit)
        predicates: list[str] = []
        values: list[object] = []
        if routine_id is not None:
            predicates.append("routine_id = ?")
            values.append(_routine_identifier(routine_id))
        if statuses:
            normalized_statuses = tuple(_routine_occurrence_status(item) for item in statuses)
            predicates.append("status IN (" + ", ".join("?" for _ in normalized_statuses) + ")")
            values.extend(normalized_statuses)
        where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
        values.append(limit)
        with self._connect() as conn:
            rows = conn.execute(  # nosec - predicates are fixed strings above
                f"SELECT * FROM routine_occurrences {where} "
                "ORDER BY scheduled_for DESC, occurrence_id DESC LIMIT ?",
                values,
            ).fetchall()
        return [_routine_occurrence_from_row(row) for row in rows]

    def list_reconcilable_routine_occurrences(
        self,
        *,
        limit: int = 100,
    ) -> list[RoutineOccurrenceRecord]:
        """Return running occurrences whose linked run is terminal or missing.

        Filtering in SQLite prevents newer blocked/running runs from starving
        older terminal reconciliation behind a bounded in-memory scan.
        """

        limit = validate_routine_reconciliation_limit(limit)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT o.* FROM routine_occurrences AS o
                LEFT JOIN runs AS r ON r.run_id = o.run_id
                WHERE o.status = 'running'
                  AND (r.run_id IS NULL OR r.status IN ('completed', 'failed', 'cancelled'))
                ORDER BY o.scheduled_for ASC, o.occurrence_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_routine_occurrence_from_row(row) for row in rows]

    def mark_routine_occurrence_running(
        self,
        occurrence_id: str,
        *,
        claim_owner: str,
        claim_generation: int,
        run_id: str,
        now: datetime | None = None,
    ) -> tuple[RoutineOccurrenceRecord, bool]:
        instant = _lease_instant(now)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE routine_occurrences SET status = 'running', started_at = ?,
                    claim_expires_at = NULL, error = NULL, updated_at = ?
                WHERE occurrence_id = ? AND status = 'claimed'
                  AND claim_owner = ? AND claim_generation = ?
                  AND run_id = ? AND claim_expires_at > ?
                """,
                (
                    instant.isoformat(),
                    instant.isoformat(),
                    occurrence_id,
                    claim_owner,
                    claim_generation,
                    run_id,
                    instant.isoformat(),
                ),
            )
        return self.get_routine_occurrence(occurrence_id), cursor.rowcount == 1

    def finish_routine_occurrence(
        self,
        occurrence_id: str,
        *,
        run_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> tuple[RoutineOccurrenceRecord, bool]:
        normalized_status = _routine_terminal_occurrence_status(status)
        instant = _lease_instant(now)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE routine_occurrences SET status = ?, finished_at = ?,
                    claim_owner = NULL, claim_expires_at = NULL, result_json = ?,
                    error = ?, updated_at = ?
                WHERE occurrence_id = ? AND run_id = ?
                  AND status = 'running'
                """,
                (
                    normalized_status,
                    instant.isoformat(),
                    _encode(result or {}),
                    error,
                    instant.isoformat(),
                    occurrence_id,
                    run_id,
                ),
            )
        return self.get_routine_occurrence(occurrence_id), cursor.rowcount == 1

    def skip_routine_occurrence(
        self,
        occurrence_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> tuple[RoutineOccurrenceRecord, bool]:
        instant = _lease_instant(now)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE routine_occurrences SET status = 'skipped', skip_reason = ?,
                    claim_owner = NULL, claim_expires_at = NULL, finished_at = ?,
                    updated_at = ? WHERE occurrence_id = ? AND status = 'claimed'
                """,
                (reason, instant.isoformat(), instant.isoformat(), occurrence_id),
            )
        return self.get_routine_occurrence(occurrence_id), cursor.rowcount == 1

    def release_routine_occurrence_claim(
        self,
        occurrence_id: str,
        *,
        claim_owner: str,
        claim_generation: int,
        error: str,
        now: datetime | None = None,
    ) -> bool:
        instant = _lease_instant(now)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE routine_occurrences SET claim_expires_at = ?, error = ?,
                    updated_at = ? WHERE occurrence_id = ? AND status = 'claimed'
                    AND claim_owner = ?
                    AND claim_generation = ?
                """,
                (
                    instant.isoformat(),
                    error,
                    instant.isoformat(),
                    occurrence_id,
                    claim_owner,
                    claim_generation,
                ),
            )
        return cursor.rowcount == 1

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?",
                (max(limit * 20, limit),),
            ).fetchall()

        sessions: dict[str, dict[str, Any]] = {}
        for row in rows:
            run = _run_from_row(row)
            current = sessions.get(run.session_id)
            if current is None:
                current = {
                    "session_id": run.session_id,
                    "run_count": 0,
                    "status_counts": {},
                    "latest_run_id": run.run_id,
                    "latest_status": run.status,
                    "latest_message": run.message,
                    "created_at": run.created_at,
                    "updated_at": run.updated_at,
                }
                sessions[run.session_id] = current
            current["run_count"] = int(current["run_count"]) + 1
            status_counts = current["status_counts"]
            if isinstance(status_counts, dict):
                status_counts[run.status] = int(status_counts.get(run.status, 0)) + 1
            current["created_at"] = min(str(current["created_at"]), run.created_at)
            current["updated_at"] = max(str(current["updated_at"]), run.updated_at)

        ordered = sorted(sessions.values(), key=lambda item: str(item["updated_at"]), reverse=True)
        return ordered[:limit]

    def append_run_step(self, run_id: str, type: str, payload: dict[str, Any]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO run_steps (run_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (run_id, type, json.dumps(payload), utc_now()),
            )
            return int(cursor.lastrowid or 0)

    def list_run_steps(
        self, run_id: str, after_id: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, type, payload_json, created_at
                FROM run_steps
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, after_id, limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "run_id": str(row["run_id"]),
                "type": str(row["type"]),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def create_approval(
        self,
        *,
        approval_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk: str,
        expires_at: str | None = None,
        principal: str = "owner",
        capability_revision: int = 0,
        resource_digest: str = "",
    ) -> dict[str, Any]:
        approval, _created = self.create_approval_once(
            approval_id=approval_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            risk=risk,
            expires_at=expires_at,
            principal=principal,
            capability_revision=capability_revision,
            resource_digest=resource_digest,
        )
        return approval

    def create_approval_once(
        self,
        *,
        approval_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk: str,
        expires_at: str | None = None,
        principal: str = "owner",
        capability_revision: int = 0,
        resource_digest: str = "",
        scheduler_continuation: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create one live approval per run.

        Concurrent retries for the same exact call reuse the existing pending
        record without extending its decision window. A different call cannot
        create an ambiguous second continuation while that record is pending.
        """

        now = utc_now()
        expiry = (
            expires_at
            or (datetime.now(UTC) + timedelta(seconds=DEFAULT_APPROVAL_TTL_SECONDS)).isoformat()
        )
        if _parse_timestamp(expiry) is None:
            raise ValueError("Approval expiration must be an ISO-8601 timestamp")
        normalized_principal = principal.strip()
        if not normalized_principal:
            raise ValueError("Approval principal cannot be empty")
        normalized_capability_revision = _capability_revision(capability_revision)
        normalized_resource_digest = (
            _capability_metadata(resource_digest, "resource_digest", optional=True) or ""
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE run_id = ? AND status = 'pending'
                ORDER BY created_at ASC, approval_id ASC
                """,
                (run_id,),
            ).fetchall()
            live_rows: list[sqlite3.Row] = []
            now_instant = _parse_timestamp(now)
            for row in rows:
                row_expiry = _parse_timestamp(_optional_str(_row_get(row, "expires_at")))
                if row_expiry is not None and now_instant is not None and row_expiry > now_instant:
                    live_rows.append(row)
                    continue
                conn.execute(
                    """
                    UPDATE approval_requests
                    SET status = 'expired', decision_json = ?, updated_at = ?
                    WHERE approval_id = ? AND status = 'pending'
                    """,
                    (
                        json.dumps({"approved": False, "reason": "approval_expired"}),
                        now,
                        str(row["approval_id"]),
                    ),
                )
            if live_rows:
                current = _approval_from_row(live_rows[0])
                expected = {
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "risk": risk,
                    "principal": normalized_principal,
                    "capability_revision": normalized_capability_revision,
                    "resource_digest": normalized_resource_digest,
                }
                actual = {key: current[key] for key in expected}
                if actual != expected:
                    raise ApprovalConflictError(current)
                # Older stores could already contain duplicates. Keep the first
                # identity stable and retire the rest before returning it.
                for duplicate in live_rows[1:]:
                    conn.execute(
                        """
                        UPDATE approval_requests
                        SET status = 'expired', decision_json = ?, updated_at = ?
                        WHERE approval_id = ? AND status = 'pending'
                        """,
                        (
                            json.dumps(
                                {
                                    "approved": False,
                                    "reason": "duplicate_pending_approval_superseded",
                                }
                            ),
                            now,
                            str(duplicate["approval_id"]),
                        ),
                    )
                if scheduler_continuation is not None:
                    _bind_scheduler_approval_continuation(
                        conn,
                        run_id=run_id,
                        approval_id=str(current["approval_id"]),
                        tool_call_id=tool_call_id,
                        continuation=scheduler_continuation,
                        now=now,
                    )
                return current, False
            conn.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, run_id, tool_call_id, tool_name, arguments_json, risk,
                    status, decision_json, result_json, principal, expires_at,
                    capability_revision, resource_digest, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    run_id,
                    tool_call_id,
                    tool_name,
                    json.dumps(arguments),
                    risk,
                    normalized_principal,
                    expiry,
                    normalized_capability_revision,
                    normalized_resource_digest,
                    now,
                    now,
                ),
            )
            created = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if created is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            if scheduler_continuation is not None:
                _bind_scheduler_approval_continuation(
                    conn,
                    run_id=run_id,
                    approval_id=approval_id,
                    tool_call_id=tool_call_id,
                    continuation=scheduler_continuation,
                    now=now,
                )
            return _approval_from_row(created), True

    def get_approval(self, approval_id: str, *, expire: bool = True) -> dict[str, Any]:
        if expire:
            self.expire_pending_approvals(approval_id=approval_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown approval: {approval_id}")
        return _approval_from_row(row)

    def list_approvals(
        self,
        status: str | None = None,
        *,
        expire: bool = True,
    ) -> list[dict[str, Any]]:
        if expire:
            self.expire_pending_approvals()
        sql = "SELECT * FROM approval_requests"
        params: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_approval_from_row(row) for row in rows]

    def expire_pending_approvals(
        self,
        *,
        approval_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically expire pending exact-call approvals whose deadline passed."""

        now = utc_now()
        due_predicate = (
            "status = 'pending' AND (expires_at IS NULL "
            "OR julianday(expires_at) IS NULL "
            "OR julianday(expires_at) <= julianday(?))"
        )
        params: tuple[object, ...] = (now,)
        if approval_id is not None:
            due_predicate += " AND approval_id = ?"
            params = (now, approval_id)

        # Approval reads are part of the scheduler's ordinary observation
        # path. Avoid taking a reserved writer slot unless the same sampled
        # instant proves that reconciliation is actually due. WAL readers can
        # complete this probe while an unrelated writer is active.
        with self._connect() as conn:
            due = conn.execute(
                f"SELECT 1 FROM approval_requests WHERE {due_predicate} LIMIT 1",
                params,
            ).fetchone()
        if due is None:
            return []

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT approval_id FROM approval_requests WHERE {due_predicate}",
                params,
            ).fetchall()
            identifiers = [str(row["approval_id"]) for row in rows]
            if not identifiers:
                return []
            placeholders = ",".join("?" for _ in identifiers)
            decision = json.dumps({"approved": False, "reason": "approval_expired"})
            conn.execute(
                f"""
                UPDATE approval_requests
                SET status = 'expired', decision_json = ?, updated_at = ?
                WHERE status = 'pending' AND approval_id IN ({placeholders})
                """,
                (decision, now, *identifiers),
            )
            expired = conn.execute(
                f"SELECT * FROM approval_requests WHERE approval_id IN ({placeholders})",
                tuple(identifiers),
            ).fetchall()
        return [_approval_from_row(row) for row in expired]

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: str,
        decision: dict[str, Any],
        result: dict[str, Any] | None = None,
        principal: str = "owner",
    ) -> dict[str, Any]:
        approval, _applied = self.decide_approval_once(
            approval_id,
            status=status,
            decision=decision,
            result=result,
            principal=principal,
        )
        return approval

    def decide_approval_once(
        self,
        approval_id: str,
        *,
        status: str,
        decision: dict[str, Any],
        result: dict[str, Any] | None = None,
        principal: str = "owner",
    ) -> tuple[dict[str, Any], bool]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            expires_at = _parse_timestamp(_optional_str(_row_get(current, "expires_at")))
            if str(current["status"]) == "pending" and (
                expires_at is None or expires_at <= datetime.now(UTC)
            ):
                now = utc_now()
                conn.execute(
                    """
                    UPDATE approval_requests
                    SET status = 'expired', decision_json = ?, updated_at = ?
                    WHERE approval_id = ? AND status = 'pending'
                    """,
                    (
                        json.dumps(
                            {
                                "approved": False,
                                "reason": "approval_expired",
                                "principal": str(_row_get(current, "principal", "owner")),
                            }
                        ),
                        now,
                        approval_id,
                    ),
                )
                expired = conn.execute(
                    "SELECT * FROM approval_requests WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if expired is None:
                    raise KeyError(f"Unknown approval: {approval_id}")
                return _approval_from_row(expired), False
            if str(current["status"]) != "pending":
                return _approval_from_row(current), False
            expected_principal = str(_row_get(current, "principal", "owner"))
            if principal != expected_principal:
                raise ValueError("Approval principal does not match the requested approval owner")
            cursor = conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, decision_json = ?, result_json = ?, updated_at = ?
                WHERE approval_id = ? AND status = 'pending'
                """,
                (
                    status,
                    json.dumps(decision),
                    json.dumps(result) if result is not None else None,
                    utc_now(),
                    approval_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if updated is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            return _approval_from_row(updated), cursor.rowcount == 1

    def record_approval_result(self, approval_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            if str(current["status"]) == "pending":
                return _approval_from_row(current)
            if current["result_json"] is not None:
                return _approval_from_row(current)
            if current["execution_claim_id"] is not None:
                return _approval_from_row(current)
            conn.execute(
                """
                UPDATE approval_requests
                SET result_json = ?, updated_at = ?
                WHERE approval_id = ? AND result_json IS NULL
                    AND execution_claim_id IS NULL
                """,
                (json.dumps(result), utc_now(), approval_id),
            )
        return self.get_approval(approval_id)

    def claim_approval_execution(
        self,
        approval_id: str,
        *,
        run_id: str,
        tool_call_id: str,
        owner: str,
        claim_id: str,
        ttl_seconds: float,
        task_id: str | None = None,
        subagent_id: str | None = None,
        run_lease_owner: str | None = None,
        run_lease_generation: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically fence one approved exact-call side effect."""

        normalized_owner = owner.strip()
        normalized_claim_id = claim_id.strip()
        if not normalized_owner or not normalized_claim_id:
            raise ValueError("approval execution owner and claim id are required")
        if (task_id is None) != (subagent_id is None):
            raise ValueError("approval execution task and subagent bindings must be paired")
        if (run_lease_owner is None) != (run_lease_generation is None):
            raise ValueError("run lease owner and generation must be provided together")
        if run_lease_owner is not None and run_lease_owner != normalized_owner:
            raise ValueError("approval execution owner must hold the run lease")
        instant = datetime.now(UTC)
        now = instant.isoformat()
        expires_at = (instant + timedelta(seconds=_positive_ttl(ttl_seconds))).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if run_lease_owner is not None and run_lease_generation is not None:
                if not _run_execution_lease_matches(
                    run_row,
                    owner=run_lease_owner,
                    generation=run_lease_generation,
                    instant=instant,
                ):
                    current = conn.execute(
                        "SELECT * FROM approval_requests WHERE approval_id = ?",
                        (approval_id,),
                    ).fetchone()
                    if current is None:
                        raise KeyError(f"Unknown approval: {approval_id}")
                    return _approval_from_row(current), False
            elif str(run_row["status"]) != "completed" or task_id is not None:
                current = conn.execute(
                    "SELECT * FROM approval_requests WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(f"Unknown approval: {approval_id}")
                return _approval_from_row(current), False
            else:
                bound_task = conn.execute(
                    """
                    SELECT 1 FROM task_nodes
                    WHERE run_id = ?
                      AND json_extract(result_json, '$.approval_continuation.approval_id') = ?
                    LIMIT 1
                    """,
                    (run_id, approval_id),
                ).fetchone()
                if bound_task is not None:
                    current = conn.execute(
                        "SELECT * FROM approval_requests WHERE approval_id = ?",
                        (approval_id,),
                    ).fetchone()
                    if current is None:
                        raise KeyError(f"Unknown approval: {approval_id}")
                    return _approval_from_row(current), False
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            decision = _json_or_none(current["decision_json"])
            if (
                str(current["status"]) != "approved"
                or not isinstance(decision, dict)
                or decision.get("approved") is not True
                or str(current["run_id"]) != run_id
                or str(current["tool_call_id"]) != tool_call_id
                or current["result_json"] is not None
                or current["execution_claim_id"] is not None
            ):
                return _approval_from_row(current), False
            if task_id is not None and subagent_id is not None:
                task_row = conn.execute(
                    "SELECT * FROM task_nodes WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                subagent_row = conn.execute(
                    "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                    (subagent_id,),
                ).fetchone()
                if task_row is None or subagent_row is None:
                    return _approval_from_row(current), False
                task = _task_from_row(task_row)
                subagent = _subagent_from_row(subagent_row)
                task_result = task.result or {}
                continuation = task_result.get("approval_continuation")
                if (
                    task.run_id != run_id
                    or task.status != "running"
                    or task_result.get("worker_owner") != normalized_owner
                    or task_result.get("worker_claim_id") != subagent_id
                    or not isinstance(continuation, dict)
                    or continuation.get("approval_id") != approval_id
                    or continuation.get("task_id") != task_id
                    or continuation.get("subagent_id") != subagent_id
                    or subagent.run_id != run_id
                    or subagent.task_id != task_id
                    or subagent.status != "running"
                ):
                    return _approval_from_row(current), False
            cursor = conn.execute(
                """
                UPDATE approval_requests
                SET execution_claim_owner = ?, execution_claim_id = ?,
                    execution_claim_started_at = ?, execution_claim_expires_at = ?,
                    execution_claim_task_id = ?, execution_claim_subagent_id = ?,
                    updated_at = ?
                WHERE approval_id = ? AND status = 'approved'
                    AND result_json IS NULL AND execution_claim_id IS NULL
                """,
                (
                    normalized_owner,
                    normalized_claim_id,
                    now,
                    expires_at,
                    task_id,
                    subagent_id,
                    now,
                    approval_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if updated is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            return _approval_from_row(updated), cursor.rowcount == 1

    def renew_approval_execution_claim(
        self,
        approval_id: str,
        *,
        owner: str,
        claim_id: str,
        ttl_seconds: float,
    ) -> bool:
        """Heartbeat one in-flight approved side-effect claim."""

        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=_positive_ttl(ttl_seconds))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE approval_requests
                SET execution_claim_expires_at = ?, updated_at = ?
                WHERE approval_id = ? AND status = 'approved'
                    AND result_json IS NULL
                    AND execution_claim_owner = ? AND execution_claim_id = ?
                """,
                (
                    expires_at.isoformat(),
                    now.isoformat(),
                    approval_id,
                    owner,
                    claim_id,
                ),
            )
        return cursor.rowcount == 1

    def record_claimed_approval_result(
        self,
        approval_id: str,
        *,
        owner: str,
        claim_id: str,
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Finalize an approval result only for its durable execution claimant."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            if current["result_json"] is not None:
                return _approval_from_row(current), False
            if (
                str(_row_get(current, "execution_claim_owner", "")) != owner
                or str(_row_get(current, "execution_claim_id", "")) != claim_id
            ):
                return _approval_from_row(current), False
            cursor = conn.execute(
                """
                UPDATE approval_requests
                SET result_json = ?, execution_claim_owner = NULL,
                    execution_claim_id = NULL, execution_claim_started_at = NULL,
                    execution_claim_expires_at = NULL,
                    updated_at = ?
                WHERE approval_id = ? AND result_json IS NULL
                    AND execution_claim_owner = ? AND execution_claim_id = ?
                """,
                (json.dumps(result), utc_now(), approval_id, owner, claim_id),
            )
            updated = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if updated is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            return _approval_from_row(updated), cursor.rowcount == 1

    def fail_approval_execution_claim(
        self,
        approval_id: str,
        *,
        owner: str,
        claim_id: str,
        expected_expires_at: str | None,
        result: dict[str, Any],
        reason: str,
    ) -> tuple[
        dict[str, Any],
        TaskNodeRecord | None,
        SubagentRunRecord | None,
        bool,
    ]:
        """Atomically close an interrupted claim and its exact scheduler pair."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            approval_row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if approval_row is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            current_approval = _approval_from_row(approval_row)
            if (
                current_approval["status"] != "approved"
                or current_approval["result"] is not None
                or current_approval.get("execution_claim_owner") != owner
                or current_approval.get("execution_claim_id") != claim_id
                or current_approval.get("execution_claim_expires_at") != expected_expires_at
            ):
                return current_approval, None, None, False

            raw_task_id = current_approval.get("execution_claim_task_id")
            raw_subagent_id = current_approval.get("execution_claim_subagent_id")
            task_id = raw_task_id if isinstance(raw_task_id, str) else None
            subagent_id = raw_subagent_id if isinstance(raw_subagent_id, str) else None
            updated_task: TaskNodeRecord | None = None
            updated_subagent: SubagentRunRecord | None = None
            if task_id is not None or subagent_id is not None:
                task_row = None
                subagent_row = None
                if task_id is not None:
                    task_row = conn.execute(
                        "SELECT * FROM task_nodes WHERE task_id = ?",
                        (task_id,),
                    ).fetchone()
                if subagent_id is not None:
                    subagent_row = conn.execute(
                        "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                        (subagent_id,),
                    ).fetchone()
                task = _task_from_row(task_row) if task_row is not None else None
                subagent = _subagent_from_row(subagent_row) if subagent_row is not None else None
                if task is not None and task.run_id != current_approval["run_id"]:
                    task = None
                if subagent is not None and (
                    subagent.run_id != current_approval["run_id"]
                    or (task_id is not None and subagent.task_id != task_id)
                ):
                    subagent = None
                terminal_task_statuses = {"completed", "failed", "cancelled", "skipped"}
                terminal_subagent_statuses = {"completed", "failed", "cancelled"}
                active_task_statuses = {"queued", "approved", "running", "blocked"}
                active_subagent_statuses = {"queued", "running", "blocked"}
                if task is not None and task.status in active_task_statuses:
                    task_result = dict(task.result or {})
                    task_result["approval_execution_failure"] = {
                        "approval_id": approval_id,
                        "claim_id": claim_id,
                        "reason": reason,
                    }
                    task_cursor = conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'failed', attempt_count = attempt_count + 1,
                            failure_reason = ?, result_json = ?, updated_at = ?
                        WHERE task_id = ? AND run_id = ?
                            AND status IN ('queued', 'approved', 'running', 'blocked')
                        """,
                        (
                            reason,
                            _encode(task_result),
                            now,
                            task.task_id,
                            current_approval["run_id"],
                        ),
                    )
                    if task_cursor.rowcount != 1:
                        conn.rollback()
                        return current_approval, None, None, False
                elif task is not None and task.status not in terminal_task_statuses:
                    task = None
                if subagent is not None and subagent.status in active_subagent_statuses:
                    subagent_cursor = conn.execute(
                        """
                        UPDATE subagent_runs
                        SET status = 'failed', error = ?, updated_at = ?
                        WHERE subagent_id = ? AND run_id = ?
                            AND status IN ('queued', 'running', 'blocked')
                        """,
                        (
                            reason,
                            now,
                            subagent.subagent_id,
                            current_approval["run_id"],
                        ),
                    )
                    if subagent_cursor.rowcount != 1:
                        conn.rollback()
                        return current_approval, None, None, False
                elif subagent is not None and subagent.status not in terminal_subagent_statuses:
                    subagent = None

            approval_cursor = conn.execute(
                """
                UPDATE approval_requests
                SET result_json = ?, execution_claim_owner = NULL,
                    execution_claim_id = NULL, execution_claim_started_at = NULL,
                    execution_claim_expires_at = NULL,
                    execution_claim_task_id = NULL,
                    execution_claim_subagent_id = NULL,
                    updated_at = ?
                WHERE approval_id = ? AND status = 'approved'
                    AND result_json IS NULL
                    AND execution_claim_owner = ? AND execution_claim_id = ?
                """,
                (json.dumps(result), now, approval_id, owner, claim_id),
            )
            if approval_cursor.rowcount != 1:
                conn.rollback()
                return current_approval, None, None, False
            updated_approval_row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if updated_approval_row is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            if task_id is not None:
                final_task_row = conn.execute(
                    "SELECT * FROM task_nodes WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if final_task_row is not None:
                    updated_task = _task_from_row(final_task_row)
            if subagent_id is not None:
                final_subagent_row = conn.execute(
                    "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                    (subagent_id,),
                ).fetchone()
                if final_subagent_row is not None:
                    updated_subagent = _subagent_from_row(final_subagent_row)
            return (
                _approval_from_row(updated_approval_row),
                updated_task,
                updated_subagent,
                True,
            )

    def upsert_mcp_server(self, server: dict[str, Any]) -> dict[str, Any]:
        server_id = str(server["id"])
        with self._connect() as conn:
            _upsert_mcp_server_row(conn, server)
        return self.get_mcp_server(server_id)

    def get_mcp_server(self, server_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown MCP server: {server_id}")
        return _mcp_from_row(row)

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM mcp_servers ORDER BY name ASC").fetchall()
        return [_mcp_from_row(row) for row in rows]

    def delete_mcp_server(self, server_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))

    def upsert_skill(self, skill: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(skill["id"])
        with self._connect() as conn:
            _upsert_skill_row(conn, skill)
        return self.get_skill(skill_id)

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM skill_registry WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown skill: {skill_id}")
        return _skill_from_row(row)

    def list_skills(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM skill_registry ORDER BY name ASC").fetchall()
        return [_skill_from_row(row) for row in rows]

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "UPDATE skill_registry SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, utc_now(), skill_id),
            )
        return self.get_skill(skill_id)

    def delete_skill(self, skill_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM skill_registry WHERE id = ?", (skill_id,))

    def upsert_plugin(self, plugin: dict[str, Any]) -> dict[str, Any]:
        plugin_id = str(plugin["id"])
        with self._connect() as conn:
            _upsert_plugin_row(conn, plugin)
        return self.get_plugin(plugin_id)

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plugin_registry WHERE id = ?", (plugin_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown plugin: {plugin_id}")
        return _plugin_from_row(row)

    def list_plugins(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM plugin_registry ORDER BY name ASC").fetchall()
        return [_plugin_from_row(row) for row in rows]

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "UPDATE plugin_registry SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, utc_now(), plugin_id),
            )
        return self.get_plugin(plugin_id)

    def delete_plugin(self, plugin_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM plugin_registry WHERE id = ?", (plugin_id,))

    def replace_plugin_bundle(
        self,
        plugin: dict[str, Any],
        *,
        skills: list[dict[str, Any]],
        mcp_servers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically replace one plugin and every namespaced extension row."""

        plugin_id = str(plugin["id"])
        prefix = f"plugin.{plugin_id}."
        _validate_plugin_bundle_ids(prefix, skills=skills, mcp_servers=mcp_servers)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_skills, existing_servers = _plugin_extension_rows(conn, prefix)
            desired_skill_ids = {str(skill["id"]) for skill in skills}
            desired_server_ids = {str(server["id"]) for server in mcp_servers}
            desired_tool_ids = {
                str(tool["name"])
                for server in mcp_servers
                for tool in server.get("tools", [])
                if isinstance(tool, dict) and tool.get("name")
            }

            _upsert_plugin_row(conn, plugin)
            for skill_id in sorted(existing_skills.keys() - desired_skill_ids):
                _delete_capability_override_row(conn, "skill", skill_id)
                _delete_capability_override_row(conn, "tool", f"skill.{skill_id}.run")
                conn.execute("DELETE FROM skill_registry WHERE id = ?", (skill_id,))
            for server_id in sorted(existing_servers.keys() - desired_server_ids):
                _delete_capability_override_row(conn, "mcp_server", server_id)
                for tool_id in _mcp_tool_ids(existing_servers[server_id]):
                    _delete_capability_override_row(conn, "tool", tool_id)
                conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
            old_tool_ids = {
                tool_id
                for server in existing_servers.values()
                for tool_id in _mcp_tool_ids(server)
            }
            for tool_id in sorted(old_tool_ids - desired_tool_ids):
                _delete_capability_override_row(conn, "tool", tool_id)
            for skill in skills:
                _upsert_skill_row(conn, skill)
            for server in mcp_servers:
                _upsert_mcp_server_row(conn, server)
            row = conn.execute(
                "SELECT * FROM plugin_registry WHERE id = ?", (plugin_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("plugin_bundle_write_lost")
            result = _plugin_from_row(row)
        return result

    def delete_plugin_bundle(self, plugin_id: str) -> None:
        """Atomically delete one plugin and its namespaced extension rows."""

        prefix = f"plugin.{plugin_id}."
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_skills, existing_servers = _plugin_extension_rows(conn, prefix)
            for skill_id in sorted(existing_skills):
                _delete_capability_override_row(conn, "skill", skill_id)
                _delete_capability_override_row(conn, "tool", f"skill.{skill_id}.run")
                conn.execute("DELETE FROM skill_registry WHERE id = ?", (skill_id,))
            for server_id, server in sorted(existing_servers.items()):
                _delete_capability_override_row(conn, "mcp_server", server_id)
                for tool_id in _mcp_tool_ids(server):
                    _delete_capability_override_row(conn, "tool", tool_id)
                conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
            conn.execute("DELETE FROM plugin_registry WHERE id = ?", (plugin_id,))

    def quiesce_plugin_bundle(self, plugin_id: str) -> dict[str, Any] | None:
        """Disable one live plugin generation without changing row timestamps."""

        prefix = f"plugin.{plugin_id}."
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            plugin = conn.execute(
                "SELECT id, enabled FROM plugin_registry WHERE id = ?", (plugin_id,)
            ).fetchone()
            if plugin is None:
                return None
            existing_skills, existing_servers = _plugin_extension_rows(conn, prefix)
            token: dict[str, Any] = {
                "plugin_id": plugin_id,
                "plugin_enabled": bool(plugin["enabled"]),
                "skills": {
                    skill_id: bool(skill["enabled"])
                    for skill_id, skill in existing_skills.items()
                },
                "mcp_servers": {
                    server_id: bool(server["enabled"])
                    for server_id, server in existing_servers.items()
                },
            }
            conn.execute("UPDATE plugin_registry SET enabled = 0 WHERE id = ?", (plugin_id,))
            for skill_id in existing_skills:
                conn.execute("UPDATE skill_registry SET enabled = 0 WHERE id = ?", (skill_id,))
            for server_id in existing_servers:
                conn.execute("UPDATE mcp_servers SET enabled = 0 WHERE id = ?", (server_id,))
            return token

    def restore_quiesced_plugin_bundle(self, token: dict[str, Any]) -> None:
        """Restore exact enablement after a failed filesystem transaction."""

        plugin_id = str(token["plugin_id"])
        skills = dict(token.get("skills", {}))
        servers = dict(token.get("mcp_servers", {}))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            plugin = conn.execute(
                "SELECT enabled FROM plugin_registry WHERE id = ?", (plugin_id,)
            ).fetchone()
            if plugin is None or bool(plugin["enabled"]):
                raise RuntimeError("plugin_quiesce_restore_conflict")
            current_skills = {
                str(row["id"]): bool(row["enabled"])
                for row in conn.execute("SELECT id, enabled FROM skill_registry").fetchall()
                if str(row["id"]).startswith(f"plugin.{plugin_id}.")
            }
            current_servers = {
                str(row["id"]): bool(row["enabled"])
                for row in conn.execute("SELECT id, enabled FROM mcp_servers").fetchall()
                if str(row["id"]).startswith(f"plugin.{plugin_id}.")
            }
            if (
                set(current_skills) != set(skills)
                or set(current_servers) != set(servers)
                or any(current_skills.values())
                or any(current_servers.values())
            ):
                raise RuntimeError("plugin_quiesce_restore_conflict")
            conn.execute(
                "UPDATE plugin_registry SET enabled = ? WHERE id = ?",
                (1 if bool(token["plugin_enabled"]) else 0, plugin_id),
            )
            for skill_id, enabled in skills.items():
                conn.execute(
                    "UPDATE skill_registry SET enabled = ? WHERE id = ?",
                    (1 if bool(enabled) else 0, skill_id),
                )
            for server_id, enabled in servers.items():
                conn.execute(
                    "UPDATE mcp_servers SET enabled = ? WHERE id = ?",
                    (1 if bool(enabled) else 0, server_id),
                )

    def get_capability_override(
        self,
        kind: str,
        capability_id: str,
        *,
        default_enabled: bool,
    ) -> dict[str, Any]:
        normalized_kind = _capability_kind(kind)
        normalized_id = _capability_id(capability_id)
        normalized_default = _capability_bool(default_enabled, "default_enabled")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM capability_overrides
                WHERE kind = ? AND capability_id = ?
                """,
                (normalized_kind, normalized_id),
            ).fetchone()
        if row is None:
            return _default_capability_override(
                normalized_kind,
                normalized_id,
                default_enabled=normalized_default,
            )
        return _capability_override_from_row(row)

    def set_capability_override(
        self,
        kind: str,
        capability_id: str,
        enabled: bool,
        *,
        expected_revision: int,
        default_enabled: bool,
        resource_digest: str | None = None,
        updated_by: str = "owner",
    ) -> dict[str, Any]:
        """Atomically create or update one capability override.

        A missing override has revision zero and inherits ``default_enabled``.
        Every successful write increments the revision and appends an immutable
        change-log row. Callers must re-read after a conflict instead of
        silently overwriting a newer owner decision.
        """

        normalized_kind = _capability_kind(kind)
        normalized_id = _capability_id(capability_id)
        normalized_enabled = _capability_bool(enabled, "enabled")
        normalized_default = _capability_bool(default_enabled, "default_enabled")
        normalized_revision = _capability_revision(expected_revision)
        normalized_digest = _capability_metadata(resource_digest, "resource_digest", optional=True)
        normalized_actor = _capability_metadata(updated_by, "updated_by", optional=False)
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM capability_overrides
                WHERE kind = ? AND capability_id = ?
                """,
                (normalized_kind, normalized_id),
            ).fetchone()
            current = (
                _capability_override_from_row(row)
                if row is not None
                else _default_capability_override(
                    normalized_kind,
                    normalized_id,
                    default_enabled=normalized_default,
                )
            )
            if int(current["revision"]) != normalized_revision:
                raise CapabilityConflictError(current)

            next_revision = normalized_revision + 1
            if row is None:
                created_at = now
                conn.execute(
                    """
                    INSERT INTO capability_overrides (
                        kind, capability_id, enabled, revision, resource_digest,
                        updated_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_kind,
                        normalized_id,
                        1 if normalized_enabled else 0,
                        next_revision,
                        normalized_digest,
                        normalized_actor,
                        created_at,
                        now,
                    ),
                )
            else:
                if normalized_digest is None:
                    normalized_digest = _optional_str(row["resource_digest"])
                cursor = conn.execute(
                    """
                    UPDATE capability_overrides
                    SET enabled = ?, revision = ?, resource_digest = ?,
                        updated_by = ?, updated_at = ?
                    WHERE kind = ? AND capability_id = ? AND revision = ?
                    """,
                    (
                        1 if normalized_enabled else 0,
                        next_revision,
                        normalized_digest,
                        normalized_actor,
                        now,
                        normalized_kind,
                        normalized_id,
                        normalized_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    latest = conn.execute(
                        """
                        SELECT * FROM capability_overrides
                        WHERE kind = ? AND capability_id = ?
                        """,
                        (normalized_kind, normalized_id),
                    ).fetchone()
                    latest_payload = (
                        _capability_override_from_row(latest)
                        if latest is not None
                        else _default_capability_override(
                            normalized_kind,
                            normalized_id,
                            default_enabled=normalized_default,
                        )
                    )
                    raise CapabilityConflictError(latest_payload)

            conn.execute(
                """
                INSERT INTO capability_change_log (
                    kind, capability_id, previous_enabled, enabled,
                    previous_revision, revision, resource_digest, updated_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_kind,
                    normalized_id,
                    1 if bool(current["enabled"]) else 0,
                    1 if normalized_enabled else 0,
                    normalized_revision,
                    next_revision,
                    normalized_digest,
                    normalized_actor,
                    now,
                ),
            )
            updated = conn.execute(
                """
                SELECT * FROM capability_overrides
                WHERE kind = ? AND capability_id = ?
                """,
                (normalized_kind, normalized_id),
            ).fetchone()
            if updated is None:
                raise RuntimeError("capability_override_write_lost")
            return _capability_override_from_row(updated)

    def delete_capability_override(
        self,
        kind: str,
        capability_id: str,
        *,
        updated_by: str = "system",
    ) -> bool:
        """Remove an override when its underlying resource is deleted.

        Deletion is audited as a fail-closed revocation so a later resource
        using the same identifier cannot inherit an old enable grant.
        """

        normalized_kind = _capability_kind(kind)
        normalized_id = _capability_id(capability_id)
        normalized_actor = _capability_metadata(updated_by, "updated_by", optional=False)
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM capability_overrides
                WHERE kind = ? AND capability_id = ?
                """,
                (normalized_kind, normalized_id),
            ).fetchone()
            if row is None:
                return False
            current = _capability_override_from_row(row)
            next_revision = int(current["revision"]) + 1
            conn.execute(
                """
                INSERT INTO capability_change_log (
                    kind, capability_id, previous_enabled, enabled,
                    previous_revision, revision, resource_digest, updated_by, created_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_kind,
                    normalized_id,
                    1 if bool(current["enabled"]) else 0,
                    int(current["revision"]),
                    next_revision,
                    current.get("resource_digest"),
                    normalized_actor,
                    now,
                ),
            )
            deleted = conn.execute(
                """
                DELETE FROM capability_overrides
                WHERE kind = ? AND capability_id = ?
                """,
                (normalized_kind, normalized_id),
            )
            return deleted.rowcount == 1

    def list_capability_overrides(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM capability_overrides"
        params: tuple[object, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (_capability_kind(kind),)
        sql += " ORDER BY kind ASC, capability_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_capability_override_from_row(row) for row in rows]

    def list_capability_changes(
        self,
        *,
        kind: str | None = None,
        capability_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        predicates: list[str] = []
        params: list[object] = []
        if kind is not None:
            predicates.append("kind = ?")
            params.append(_capability_kind(kind))
        if capability_id is not None:
            predicates.append("capability_id = ?")
            params.append(_capability_id(capability_id))
        sql = "SELECT * FROM capability_change_log"
        if predicates:
            sql += f" WHERE {' AND '.join(predicates)}"
        sql += " ORDER BY change_id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_capability_change_from_row(row) for row in rows]

    def create_task_node(
        self,
        *,
        task_id: str,
        run_id: str,
        title: str,
        goal: str,
        profile: str = "planner",
        status: str = "queued",
        parent_id: str | None = None,
        approved: bool = False,
        plan: dict[str, Any] | None = None,
        dependencies: list[str] | tuple[str, ...] = (),
        required_tools: list[str] | tuple[str, ...] = (),
        risk: str = "low",
        acceptance_criteria: list[str] | tuple[str, ...] = (),
        attempt_count: int = 0,
        failure_reason: str = "",
        diagnosis: dict[str, Any] | None = None,
        retry_strategy: dict[str, Any] | None = None,
    ) -> TaskNodeRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_nodes (
                    task_id, run_id, parent_id, title, goal, profile, status, approved,
                    plan_json, result_json, dependencies_json, required_tools_json, risk,
                    acceptance_criteria_json, attempt_count, failure_reason, diagnosis_json,
                    retry_strategy_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    run_id,
                    parent_id,
                    title,
                    goal,
                    profile,
                    status,
                    1 if approved else 0,
                    json.dumps(plan or {}),
                    json.dumps(list(dependencies)),
                    json.dumps(list(required_tools)),
                    risk,
                    json.dumps(list(acceptance_criteria)),
                    attempt_count,
                    failure_reason,
                    json.dumps(diagnosis) if diagnosis is not None else None,
                    json.dumps(retry_strategy) if retry_strategy is not None else None,
                    now,
                    now,
                ),
            )
        return self.get_task_node(task_id)

    def create_task_graph_once(
        self,
        *,
        run_id: str,
        tasks: list[TaskNodeRecord] | tuple[TaskNodeRecord, ...],
    ) -> tuple[list[TaskNodeRecord], bool]:
        """Atomically create one complete starter task graph for a queued run.

        Run admission and graph construction are separate durable transactions.
        This all-or-none insert lets startup recovery safely repair a crash in
        that narrow window without duplicating roots or child tasks.
        """

        if not tasks:
            raise ValueError("task graph must contain at least one task")
        identifiers = [task.task_id for task in tasks]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("task graph task ids must be unique")
        known_ids = set(identifiers)
        for task in tasks:
            if task.run_id != run_id:
                raise ValueError("task graph contains a task for another run")
            if task.parent_id is not None and task.parent_id not in known_ids:
                raise ValueError("task graph parent must belong to the same graph")
            if any(dependency not in known_ids for dependency in task.dependencies):
                raise ValueError("task graph dependency must belong to the same graph")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            existing = conn.execute(
                "SELECT * FROM task_nodes WHERE run_id = ? ORDER BY created_at ASC, task_id ASC",
                (run_id,),
            ).fetchall()
            if existing:
                return [_task_from_row(row) for row in existing], False
            if str(run_row["status"]) != "queued":
                raise ValueError("starter task graph requires a queued run")

            created = datetime.now(UTC)
            for index, task in enumerate(tasks):
                timestamp = (created + timedelta(microseconds=index)).isoformat()
                conn.execute(
                    """
                    INSERT INTO task_nodes (
                        task_id, run_id, parent_id, title, goal, profile, status, approved,
                        plan_json, result_json, dependencies_json, required_tools_json, risk,
                        acceptance_criteria_json, attempt_count, failure_reason, diagnosis_json,
                        retry_strategy_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        run_id,
                        task.parent_id,
                        task.title,
                        task.goal,
                        task.profile,
                        task.status,
                        1 if task.approved else 0,
                        _encode(task.plan or {}),
                        _encode(task.result) if task.result is not None else None,
                        _encode(task.dependencies),
                        _encode(task.required_tools),
                        task.risk,
                        _encode(task.acceptance_criteria),
                        task.attempt_count,
                        task.failure_reason,
                        _encode(task.diagnosis) if task.diagnosis is not None else None,
                        _encode(task.retry_strategy) if task.retry_strategy is not None else None,
                        timestamp,
                        timestamp,
                    ),
                )
            rows = conn.execute(
                "SELECT * FROM task_nodes WHERE run_id = ? ORDER BY created_at ASC, task_id ASC",
                (run_id,),
            ).fetchall()
        return [_task_from_row(row) for row in rows], True

    def update_task_node(self, task_id: str, **fields: object) -> TaskNodeRecord:
        if not fields:
            return self.get_task_node(task_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(
            f"{_validated_column('task_nodes', _task_column(key))} = ?" for key in fields
        )
        values = [_encode(value) for value in fields.values()]
        values.append(task_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE task_nodes SET {assignments} WHERE task_id = ?", values)  # nosec
        return self.get_task_node(task_id)

    def approve_task_node(self, task_id: str, *, run_id: str) -> TaskNodeRecord | None:
        """Approve a queued task only while its owning run still accepts work."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run_row["status"]) in _TERMINAL_RUN_STATUSES:
                return None
            task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            task = _task_from_row(task_row)
            if task.run_id != run_id or task.status != "queued":
                return None
            cursor = conn.execute(
                """
                UPDATE task_nodes SET approved = 1, status = 'approved', updated_at = ?
                WHERE task_id = ? AND run_id = ? AND status = 'queued'
                """,
                (now, task_id, run_id),
            )
            if cursor.rowcount != 1:
                return None
            updated = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            return _task_from_row(updated) if updated is not None else None

    def claim_task_node(
        self,
        task_id: str,
        *,
        run_id: str,
        worker_owner: str,
        worker_claim_id: str,
        heartbeat_at: str | None = None,
        run_lease_owner: str | None = None,
        run_lease_generation: int | None = None,
    ) -> TaskNodeRecord | None:
        """Atomically claim one approved ready task for exactly one worker execution."""

        owner = worker_owner.strip()
        claim_id = worker_claim_id.strip()
        if not owner or not claim_id:
            raise ValueError("worker owner and claim id are required")
        if (run_lease_owner is None) != (run_lease_generation is None):
            raise ValueError("run lease owner and generation must be provided together")
        if run_lease_owner is not None and run_lease_owner != owner:
            raise ValueError("task worker owner must hold the run lease")
        instant = datetime.now(UTC)
        now = heartbeat_at or instant.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run_row["status"]) in _TERMINAL_RUN_STATUSES:
                return None
            if run_lease_owner is not None and not _run_execution_lease_matches(
                run_row,
                owner=run_lease_owner,
                generation=run_lease_generation,
                instant=instant,
            ):
                return None
            task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            task = _task_from_row(task_row)
            if (
                task.run_id != run_id
                or not task.approved
                or task.status not in {"queued", "approved"}
            ):
                return None
            if task.dependencies:
                placeholders = ", ".join("?" for _ in task.dependencies)
                dependency_rows = conn.execute(
                    f"SELECT task_id, run_id, status FROM task_nodes "  # nosec
                    f"WHERE task_id IN ({placeholders})",
                    tuple(task.dependencies),
                ).fetchall()
                dependency_statuses = {
                    str(row["task_id"]): (
                        str(row["run_id"]),
                        str(row["status"]),
                    )
                    for row in dependency_rows
                }
                if any(
                    dependency_statuses.get(dependency) != (run_id, "completed")
                    for dependency in task.dependencies
                ):
                    return None
            result = dict(task.result or {})
            result.update(
                {
                    "worker_owner": owner,
                    "worker_claim_id": claim_id,
                    "worker_heartbeat_at": now,
                }
            )
            cursor = conn.execute(
                """
                UPDATE task_nodes SET status = 'running', result_json = ?, updated_at = ?
                WHERE task_id = ? AND run_id = ? AND approved = 1 AND status = ?
                """,
                (_encode(result), now, task_id, run_id, task.status),
            )
            if cursor.rowcount != 1:
                return None
            updated = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            return _task_from_row(updated) if updated is not None else None

    def task_claim_matches(
        self,
        task_id: str,
        *,
        run_id: str,
        worker_owner: str,
        worker_claim_id: str,
        run_lease_owner: str | None = None,
        run_lease_generation: int | None = None,
    ) -> bool:
        if (run_lease_owner is None) != (run_lease_generation is None):
            raise ValueError("run lease owner and generation must be provided together")
        if run_lease_owner is not None and run_lease_owner != worker_owner:
            raise ValueError("task worker owner must hold the run lease")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT task_nodes.*, runs.status AS _run_status,
                    runs.lease_owner AS _run_lease_owner,
                    runs.lease_generation AS _run_lease_generation,
                    runs.lease_expires_at AS _run_lease_expires_at
                FROM task_nodes JOIN runs ON runs.run_id = task_nodes.run_id
                WHERE task_nodes.task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        task = _task_from_row(row)
        result = task.result or {}
        matches = (
            task.run_id == run_id
            and task.status == "running"
            and result.get("worker_owner") == worker_owner
            and result.get("worker_claim_id") == worker_claim_id
        )
        if not matches or run_lease_owner is None:
            return matches
        return (
            str(row["_run_status"]) in {"queued", "running"}
            and _optional_str(row["_run_lease_owner"]) == run_lease_owner
            and int(row["_run_lease_generation"]) == run_lease_generation
            and (
                _parse_timestamp(_optional_str(row["_run_lease_expires_at"]))
                or datetime.min.replace(tzinfo=UTC)
            )
            > datetime.now(UTC)
        )

    def heartbeat_task_claim(
        self,
        task_id: str,
        *,
        run_id: str,
        worker_owner: str,
        worker_claim_id: str,
        heartbeat_at: str | None = None,
        run_lease_owner: str | None = None,
        run_lease_generation: int | None = None,
    ) -> bool:
        """Refresh a task heartbeat only if the exact execution claim is still active."""

        if (run_lease_owner is None) != (run_lease_generation is None):
            raise ValueError("run lease owner and generation must be provided together")
        if run_lease_owner is not None and run_lease_owner != worker_owner:
            raise ValueError("task worker owner must hold the run lease")
        instant = datetime.now(UTC)
        now = heartbeat_at or instant.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run_row["status"]) in _ABORTED_RUN_STATUSES:
                return False
            if run_lease_owner is not None and not _run_execution_lease_matches(
                run_row,
                owner=run_lease_owner,
                generation=run_lease_generation,
                instant=instant,
            ):
                return False
            row = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            task = _task_from_row(row)
            result = dict(task.result or {})
            if (
                task.run_id != run_id
                or task.status != "running"
                or result.get("worker_owner") != worker_owner
                or result.get("worker_claim_id") != worker_claim_id
            ):
                return False
            result["worker_heartbeat_at"] = now
            cursor = conn.execute(
                """
                UPDATE task_nodes SET result_json = ?, updated_at = ?
                WHERE task_id = ? AND run_id = ? AND status = 'running'
                """,
                (_encode(result), now, task_id, run_id),
            )
            return cursor.rowcount == 1

    def transition_task_claim(
        self,
        task_id: str,
        status: str,
        *,
        run_id: str,
        worker_owner: str,
        worker_claim_id: str,
        increment_attempt: bool = False,
        run_lease_owner: str | None = None,
        run_lease_generation: int | None = None,
        **fields: object,
    ) -> tuple[TaskNodeRecord, bool]:
        """Finish an exact task execution claim without allowing stale workers to overwrite state."""

        if status not in {"blocked", "completed", "failed", "cancelled", "skipped"}:
            raise ValueError(f"unsupported claimed task transition: {status}")
        if (run_lease_owner is None) != (run_lease_generation is None):
            raise ValueError("run lease owner and generation must be provided together")
        if run_lease_owner is not None and run_lease_owner != worker_owner:
            raise ValueError("task worker owner must hold the run lease")
        instant = datetime.now(UTC)
        now = instant.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if run_lease_owner is not None and not _run_execution_lease_matches(
                run_row,
                owner=run_lease_owner,
                generation=run_lease_generation,
                instant=instant,
            ):
                row = conn.execute(
                    "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown task: {task_id}")
                return _task_from_row(row), False
            row = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            task = _task_from_row(row)
            claim = task.result or {}
            if (
                task.run_id != run_id
                or task.status != "running"
                or claim.get("worker_owner") != worker_owner
                or claim.get("worker_claim_id") != worker_claim_id
            ):
                return task, False
            if str(run_row["status"]) in _ABORTED_RUN_STATUSES and status != "cancelled":
                return task, False
            updates = dict(fields)
            updates["status"] = status
            if increment_attempt:
                updates["attempt_count"] = task.attempt_count + 1
            updates["updated_at"] = now
            assignments = ", ".join(
                f"{_validated_column('task_nodes', _task_column(key))} = ?" for key in updates
            )
            values = [_encode(value) for value in updates.values()]
            values.extend([task_id, run_id])
            cursor = conn.execute(  # nosec
                f"UPDATE task_nodes SET {assignments} "
                "WHERE task_id = ? AND run_id = ? AND status = 'running'",
                values,
            )
            updated = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            if updated is None:
                raise KeyError(f"Unknown task: {task_id}")
            return _task_from_row(updated), cursor.rowcount == 1

    def resume_blocked_task_for_approval(
        self,
        task_id: str,
        *,
        run_id: str,
        subagent_id: str,
        approval_id: str,
        worker_owner: str,
        worker_claim_id: str,
        heartbeat_at: str | None = None,
        run_lease_owner: str | None = None,
        run_lease_generation: int | None = None,
    ) -> tuple[TaskNodeRecord, SubagentRunRecord] | None:
        """Atomically restore the exact scheduler task and subagent after approval."""

        owner = worker_owner.strip()
        claim_id = worker_claim_id.strip()
        if not owner or not claim_id:
            raise ValueError("worker owner and claim id are required")
        if (run_lease_owner is None) != (run_lease_generation is None):
            raise ValueError("run lease owner and generation must be provided together")
        if run_lease_owner is not None and run_lease_owner != owner:
            raise ValueError("task worker owner must hold the run lease")
        instant = datetime.now(UTC)
        now = heartbeat_at or instant.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run_row["status"]) != "running":
                return None
            if run_lease_owner is not None and not _run_execution_lease_matches(
                run_row,
                owner=run_lease_owner,
                generation=run_lease_generation,
                instant=instant,
            ):
                return None

            task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            task = _task_from_row(task_row)
            task_result = dict(task.result or {})
            continuation = task_result.get("approval_continuation")
            if (
                task.run_id != run_id
                or task.status != "blocked"
                or not isinstance(continuation, dict)
                or continuation.get("approval_id") != approval_id
                or continuation.get("task_id") != task_id
                or continuation.get("subagent_id") != subagent_id
                or continuation.get("worker_claim_id") != subagent_id
                or not str(continuation.get("worker_owner") or "").strip()
            ):
                return None

            subagent_row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
            if subagent_row is None:
                raise KeyError(f"Unknown subagent run: {subagent_id}")
            subagent = _subagent_from_row(subagent_row)
            if (
                subagent.run_id != run_id
                or subagent.task_id != task_id
                or subagent.status != "blocked"
            ):
                return None

            # Keep the exact approval binding durable while the worker is
            # running. The terminal/blocking pair transition replaces this
            # result, and startup can still reconcile a crash before the tool
            # execution claim is acquired.
            task_result.update(
                {
                    "worker_owner": owner,
                    "worker_claim_id": claim_id,
                    "worker_heartbeat_at": now,
                }
            )
            task_cursor = conn.execute(
                """
                UPDATE task_nodes SET status = 'running', result_json = ?, updated_at = ?
                WHERE task_id = ? AND run_id = ? AND status = 'blocked'
                """,
                (_encode(task_result), now, task_id, run_id),
            )
            subagent_cursor = conn.execute(
                """
                UPDATE subagent_runs SET status = 'running', error = NULL, updated_at = ?
                WHERE subagent_id = ? AND run_id = ? AND task_id = ? AND status = 'blocked'
                """,
                (now, subagent_id, run_id, task_id),
            )
            if task_cursor.rowcount != 1 or subagent_cursor.rowcount != 1:
                conn.rollback()
                return None
            updated_task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            updated_subagent_row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
            if updated_task_row is None or updated_subagent_row is None:
                raise RuntimeError("approval task continuation did not persist")
            return _task_from_row(updated_task_row), _subagent_from_row(updated_subagent_row)

    def transition_scheduler_task_and_subagent(
        self,
        task_id: str,
        status: str,
        *,
        run_id: str,
        subagent_id: str,
        worker_owner: str,
        worker_claim_id: str,
        task_fields: dict[str, object] | None = None,
        subagent_result: str = "",
        subagent_error: str | None = None,
        increment_attempt: bool = False,
        consumed_approval_id: str | None = None,
        run_lease_owner: str | None = None,
        run_lease_generation: int | None = None,
    ) -> tuple[TaskNodeRecord, SubagentRunRecord, bool]:
        """Atomically terminalize or block one exact scheduler worker pair."""

        if status not in {"blocked", "completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported scheduler worker transition: {status}")
        if (run_lease_owner is None) != (run_lease_generation is None):
            raise ValueError("run lease owner and generation must be provided together")
        if run_lease_owner is not None and run_lease_owner != worker_owner:
            raise ValueError("task worker owner must hold the run lease")
        instant = datetime.now(UTC)
        now = instant.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            subagent_row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            if subagent_row is None:
                raise KeyError(f"Unknown subagent run: {subagent_id}")
            task = _task_from_row(task_row)
            subagent = _subagent_from_row(subagent_row)
            if run_lease_owner is not None and not _run_execution_lease_matches(
                run_row,
                owner=run_lease_owner,
                generation=run_lease_generation,
                instant=instant,
            ):
                return task, subagent, False
            claim = task.result or {}
            eligible_subagent_statuses = (
                {"queued", "running"} if status == "cancelled" else {"running"}
            )
            if (
                (str(run_row["status"]) in _ABORTED_RUN_STATUSES and status != "cancelled")
                or task.run_id != run_id
                or task.status != "running"
                or claim.get("worker_owner") != worker_owner
                or claim.get("worker_claim_id") != worker_claim_id
                or subagent.run_id != run_id
                or subagent.task_id != task_id
                or subagent.status not in eligible_subagent_statuses
            ):
                return task, subagent, False
            if consumed_approval_id is not None:
                approval_row = conn.execute(
                    "SELECT * FROM approval_requests WHERE approval_id = ?",
                    (consumed_approval_id,),
                ).fetchone()
                if approval_row is None:
                    return task, subagent, False
                consumed = _approval_from_row(approval_row)
                if (
                    consumed["run_id"] != run_id
                    or consumed.get("result") is None
                    or consumed.get("execution_claim_id") is not None
                    or consumed.get("execution_claim_task_id") != task_id
                    or consumed.get("execution_claim_subagent_id") != subagent_id
                ):
                    return task, subagent, False

            updates = dict(task_fields or {})
            updates["status"] = status
            if increment_attempt:
                updates["attempt_count"] = task.attempt_count + 1
            updates["updated_at"] = now
            assignments = ", ".join(
                f"{_validated_column('task_nodes', _task_column(key))} = ?" for key in updates
            )
            values = [_encode(value) for value in updates.values()]
            values.extend([task_id, run_id])
            task_cursor = conn.execute(  # nosec
                f"UPDATE task_nodes SET {assignments} "
                "WHERE task_id = ? AND run_id = ? AND status = 'running'",
                values,
            )
            subagent_cursor = conn.execute(
                """
                UPDATE subagent_runs
                SET status = ?, result = ?, error = ?, updated_at = ?
                WHERE subagent_id = ? AND run_id = ? AND task_id = ? AND status = ?
                """,
                (
                    status,
                    subagent_result,
                    subagent_error,
                    now,
                    subagent_id,
                    run_id,
                    task_id,
                    subagent.status,
                ),
            )
            if task_cursor.rowcount != 1 or subagent_cursor.rowcount != 1:
                conn.rollback()
                return task, subagent, False
            if consumed_approval_id is not None:
                approval_cursor = conn.execute(
                    """
                    UPDATE approval_requests
                    SET execution_claim_task_id = NULL,
                        execution_claim_subagent_id = NULL,
                        updated_at = ?
                    WHERE approval_id = ? AND run_id = ?
                        AND result_json IS NOT NULL AND execution_claim_id IS NULL
                        AND execution_claim_task_id = ?
                        AND execution_claim_subagent_id = ?
                    """,
                    (
                        now,
                        consumed_approval_id,
                        run_id,
                        task_id,
                        subagent_id,
                    ),
                )
                if approval_cursor.rowcount != 1:
                    conn.rollback()
                    return task, subagent, False
            updated_task = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            updated_subagent = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
            if updated_task is None or updated_subagent is None:
                raise RuntimeError("scheduler worker transition did not persist")
            return (
                _task_from_row(updated_task),
                _subagent_from_row(updated_subagent),
                True,
            )

    def fail_scheduler_task_for_approval(
        self,
        task_id: str,
        *,
        run_id: str,
        subagent_id: str,
        approval_id: str,
        reason: str,
        expected_run_lease: tuple[str | None, int, str | None] | None = None,
    ) -> tuple[TaskNodeRecord, SubagentRunRecord] | None:
        """Atomically terminalize the scheduler worker bound to a denied grant."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            approval_row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            run_row = conn.execute(
                "SELECT lease_owner, lease_generation, lease_expires_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            subagent_row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
            if approval_row is None or run_row is None or task_row is None or subagent_row is None:
                return None
            if (
                expected_run_lease is not None
                and (
                    _optional_str(run_row["lease_owner"]),
                    int(run_row["lease_generation"]),
                    _optional_str(run_row["lease_expires_at"]),
                )
                != expected_run_lease
            ):
                return None
            approval = _approval_from_row(approval_row)
            task = _task_from_row(task_row)
            subagent = _subagent_from_row(subagent_row)
            task_result = dict(task.result or {})
            continuation = task_result.get("approval_continuation")
            active_task_statuses = {"running", "blocked"}
            terminal_task_statuses = {"completed", "failed", "cancelled", "skipped"}
            active_subagent_statuses = {"running", "blocked"}
            terminal_subagent_statuses = {"completed", "failed", "cancelled"}
            if (
                task.run_id != run_id
                or task.status not in active_task_statuses | terminal_task_statuses
                or subagent.run_id != run_id
                or subagent.task_id != task_id
                or subagent.status not in active_subagent_statuses | terminal_subagent_statuses
                or not isinstance(continuation, dict)
                or continuation.get("approval_id") != approval_id
                or continuation.get("task_id") != task_id
                or continuation.get("subagent_id") != subagent_id
            ):
                return None
            bound_task_id = approval.get("execution_claim_task_id")
            bound_subagent_id = approval.get("execution_claim_subagent_id")
            if (bound_task_id is not None or bound_subagent_id is not None) and (
                bound_task_id != task_id or bound_subagent_id != subagent_id
            ):
                return None
            task_result["approval_denial"] = {
                "approval_id": approval_id,
                "reason": reason,
            }
            if task.status in active_task_statuses:
                task_cursor = conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'failed', attempt_count = attempt_count + 1,
                        failure_reason = ?, result_json = ?, updated_at = ?
                    WHERE task_id = ? AND run_id = ? AND status IN ('running', 'blocked')
                    """,
                    (reason, _encode(task_result), now, task_id, run_id),
                )
                if task_cursor.rowcount != 1:
                    conn.rollback()
                    return None
            if subagent.status in active_subagent_statuses:
                subagent_cursor = conn.execute(
                    """
                    UPDATE subagent_runs SET status = 'failed', error = ?, updated_at = ?
                    WHERE subagent_id = ? AND run_id = ? AND task_id = ?
                      AND status IN ('running', 'blocked')
                    """,
                    (reason, now, subagent_id, run_id, task_id),
                )
                if subagent_cursor.rowcount != 1:
                    conn.rollback()
                    return None
            if bound_task_id is not None and bound_subagent_id is not None:
                binding_cursor = conn.execute(
                    """
                    UPDATE approval_requests
                    SET execution_claim_task_id = NULL,
                        execution_claim_subagent_id = NULL,
                        updated_at = ?
                    WHERE approval_id = ? AND run_id = ?
                        AND execution_claim_task_id = ?
                        AND execution_claim_subagent_id = ?
                    """,
                    (now, approval_id, run_id, task_id, subagent_id),
                )
                if binding_cursor.rowcount != 1:
                    conn.rollback()
                    return None
            updated_task = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            updated_subagent = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
            if updated_task is None or updated_subagent is None:
                raise RuntimeError("scheduler approval denial did not persist")
            return _task_from_row(updated_task), _subagent_from_row(updated_subagent)

    def cancel_tasks_for_run(self, run_id: str) -> list[str]:
        """Atomically cancel every non-terminal task attached to a cancelled run."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT task_id FROM task_nodes
                WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                ORDER BY created_at ASC, task_id ASC
                """,
                (run_id,),
            ).fetchall()
            task_ids = [str(row["task_id"]) for row in rows]
            if task_ids:
                conn.execute(
                    """
                    UPDATE task_nodes SET status = 'cancelled', updated_at = ?
                    WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                    """,
                    (now, run_id),
                )
            return task_ids

    def record_task_failure(
        self,
        task_id: str,
        *,
        failure_reason: str,
        diagnosis: dict[str, Any] | None = None,
        retry_strategy: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> TaskNodeRecord:
        task = self.get_task_node(task_id)
        return self.update_task_node(
            task_id,
            status="failed",
            attempt_count=task.attempt_count + 1,
            failure_reason=failure_reason,
            diagnosis=diagnosis or {},
            retry_strategy=retry_strategy or {},
            result=result or task.result,
        )

    def get_task_node(self, task_id: str) -> TaskNodeRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return _task_from_row(row)

    def list_task_nodes(self, run_id: str) -> list[TaskNodeRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_nodes WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def create_subagent_run(
        self,
        *,
        subagent_id: str,
        run_id: str,
        profile: str,
        goal: str,
        status: str = "queued",
        task_id: str | None = None,
    ) -> SubagentRunRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subagent_runs (
                    subagent_id, run_id, task_id, profile, goal, status, result, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', NULL, ?, ?)
                """,
                (subagent_id, run_id, task_id, profile, goal, status, now, now),
            )
        return self.get_subagent_run(subagent_id)

    def create_subagent_run_for_claim(
        self,
        *,
        subagent_id: str,
        run_id: str,
        task_id: str,
        profile: str,
        goal: str,
        status: str,
        worker_owner: str,
        worker_claim_id: str,
        run_lease_owner: str | None = None,
        run_lease_generation: int | None = None,
    ) -> SubagentRunRecord | None:
        """Persist a worker only while its run and exact task claim are still active."""

        if status not in {"queued", "running"}:
            raise ValueError(f"unsupported initial subagent status: {status}")
        if (run_lease_owner is None) != (run_lease_generation is None):
            raise ValueError("run lease owner and generation must be provided together")
        if run_lease_owner is not None and run_lease_owner != worker_owner:
            raise ValueError("task worker owner must hold the run lease")
        instant = datetime.now(UTC)
        now = instant.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run_row["status"]) in _TERMINAL_RUN_STATUSES:
                return None
            if run_lease_owner is not None and not _run_execution_lease_matches(
                run_row,
                owner=run_lease_owner,
                generation=run_lease_generation,
                instant=instant,
            ):
                return None
            task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            task = _task_from_row(task_row)
            claim = task.result or {}
            if (
                task.run_id != run_id
                or task.status != "running"
                or claim.get("worker_owner") != worker_owner
                or claim.get("worker_claim_id") != worker_claim_id
            ):
                return None
            conn.execute(
                """
                INSERT INTO subagent_runs (
                    subagent_id, run_id, task_id, profile, goal, status, result, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', NULL, ?, ?)
                """,
                (subagent_id, run_id, task_id, profile, goal, status, now, now),
            )
            row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("subagent insert did not persist")
            return _subagent_from_row(row)

    def update_subagent_run(self, subagent_id: str, **fields: object) -> SubagentRunRecord:
        if not fields:
            return self.get_subagent_run(subagent_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{_validated_column('subagent_runs', key)} = ?" for key in fields)
        values = [_encode(value) for value in fields.values()]
        values.append(subagent_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE subagent_runs SET {assignments} WHERE subagent_id = ?", values)  # nosec
        return self.get_subagent_run(subagent_id)

    def transition_subagent_run(
        self,
        subagent_id: str,
        status: str,
        *,
        expected_statuses: tuple[str, ...],
        **fields: object,
    ) -> tuple[SubagentRunRecord, bool]:
        """Compare-and-set a subagent status so cancellation remains terminal."""

        if not expected_statuses:
            raise ValueError("expected subagent statuses are required")
        updates = dict(fields)
        updates["status"] = status
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{_validated_column('subagent_runs', key)} = ?" for key in updates)
        values = [_encode(value) for value in updates.values()]
        placeholders = ", ".join("?" for _ in expected_statuses)
        values.extend([subagent_id, *expected_statuses])
        with self._connect() as conn:
            cursor = conn.execute(  # nosec
                f"UPDATE subagent_runs SET {assignments} "
                f"WHERE subagent_id = ? AND status IN ({placeholders})",
                values,
            )
        return self.get_subagent_run(subagent_id), cursor.rowcount == 1

    def cancel_subagents_for_run(self, run_id: str) -> list[str]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT subagent_id FROM subagent_runs
                WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
                ORDER BY created_at ASC, subagent_id ASC
                """,
                (run_id,),
            ).fetchall()
            subagent_ids = [str(row["subagent_id"]) for row in rows]
            if subagent_ids:
                conn.execute(
                    """
                    UPDATE subagent_runs SET status = 'cancelled', updated_at = ?
                    WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
                    """,
                    (now, run_id),
                )
            return subagent_ids

    def get_subagent_run(self, subagent_id: str) -> SubagentRunRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown subagent run: {subagent_id}")
        return _subagent_from_row(row)

    def list_subagent_runs(self, run_id: str) -> list[SubagentRunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subagent_runs WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [_subagent_from_row(row) for row in rows]

    def list_nonterminal_subagent_runs(self) -> list[SubagentRunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subagent_runs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [_subagent_from_row(row) for row in rows]

    def fail_stale_worker_pair(
        self,
        *,
        run_id: str,
        task_id: str,
        subagent_id: str,
        worker_owner: str | None,
        worker_claim_id: str | None,
        expected_heartbeat_at: str | None,
        reason: str,
    ) -> tuple[TaskNodeRecord, SubagentRunRecord, bool]:
        """Atomically fail an exact stale worker snapshot and its subagent."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            subagent_row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            if subagent_row is None:
                raise KeyError(f"Unknown subagent run: {subagent_id}")
            task = _task_from_row(task_row)
            subagent = _subagent_from_row(subagent_row)
            result = dict(task.result or {})
            if (
                task.run_id != run_id
                or task.status != "running"
                or result.get("worker_owner") != worker_owner
                or result.get("worker_claim_id") != worker_claim_id
                or _optional_str(result.get("worker_heartbeat_at")) != expected_heartbeat_at
                or subagent.run_id != run_id
                or subagent.task_id != task_id
                or subagent.status not in {"queued", "running"}
            ):
                return task, subagent, False
            result["worker_recovery"] = {
                "reason": reason,
                "worker_owner": worker_owner,
                "worker_claim_id": worker_claim_id,
                "heartbeat_at": expected_heartbeat_at,
            }
            task_cursor = conn.execute(
                """
                UPDATE task_nodes SET status = 'failed', failure_reason = ?,
                    result_json = ?, updated_at = ?
                WHERE task_id = ? AND run_id = ? AND status = 'running'
                """,
                (reason, _encode(result), now, task_id, run_id),
            )
            subagent_cursor = conn.execute(
                """
                UPDATE subagent_runs SET status = 'failed', error = ?, updated_at = ?
                WHERE subagent_id = ? AND run_id = ? AND task_id = ?
                    AND status IN ('queued', 'running')
                """,
                (reason, now, subagent_id, run_id, task_id),
            )
            if task_cursor.rowcount != 1 or subagent_cursor.rowcount != 1:
                conn.rollback()
                return task, subagent, False
            updated_task_row = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            updated_subagent_row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)
            ).fetchone()
            if updated_task_row is None or updated_subagent_row is None:
                raise RuntimeError("stale worker recovery did not persist")
            return _task_from_row(updated_task_row), _subagent_from_row(updated_subagent_row), True

    def fail_stale_subagent_run(
        self,
        subagent_id: str,
        *,
        run_id: str,
        expected_status: str,
        expected_updated_at: str,
        reason: str,
    ) -> tuple[SubagentRunRecord, bool]:
        """Fail one exact orphaned subagent snapshot without clobbering a renewal."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown subagent run: {subagent_id}")
            current = _subagent_from_row(row)
            if (
                current.run_id != run_id
                or current.status != expected_status
                or current.updated_at != expected_updated_at
            ):
                return current, False
            cursor = conn.execute(
                """
                UPDATE subagent_runs SET status = 'failed', error = ?, updated_at = ?
                WHERE subagent_id = ? AND run_id = ? AND status = ? AND updated_at = ?
                """,
                (
                    reason,
                    now,
                    subagent_id,
                    run_id,
                    expected_status,
                    expected_updated_at,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)
            ).fetchone()
            if updated is None:
                raise KeyError(f"Unknown subagent run: {subagent_id}")
            return _subagent_from_row(updated), cursor.rowcount == 1

    def subagent_status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM subagent_runs GROUP BY status ORDER BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def create_trace_span(
        self,
        *,
        span_id: str,
        run_id: str,
        span_type: str,
        name: str,
        parent_span_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceSpanRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trace_spans (
                    span_id, run_id, parent_span_id, span_type, name, status,
                    metadata_json, output_json, error, started_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, NULL, NULL, ?, NULL)
                """,
                (span_id, run_id, parent_span_id, span_type, name, json.dumps(metadata or {}), now),
            )
        return self.get_trace_span(span_id)

    def finish_trace_span(
        self,
        span_id: str,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TraceSpanRecord:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trace_spans
                SET status = ?, output_json = ?, error = ?, ended_at = ?
                WHERE span_id = ?
                """,
                (status, json.dumps(output or {}), error, utc_now(), span_id),
            )
        return self.get_trace_span(span_id)

    def get_trace_span(self, span_id: str) -> TraceSpanRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM trace_spans WHERE span_id = ?", (span_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown trace span: {span_id}")
        return _trace_span_from_row(row)

    def list_trace_spans(self, run_id: str) -> list[TraceSpanRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trace_spans
                WHERE run_id = ?
                ORDER BY started_at ASC, span_id ASC
                """,
                (run_id,),
            ).fetchall()
        return [_trace_span_from_row(row) for row in rows]

    def schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        return 0 if row is None else int(row["version"])

    def health_snapshot(self) -> dict[str, object]:
        integrity = "unknown"
        error_type: str | None = None
        try:
            with self._connect() as conn:
                row = conn.execute("PRAGMA quick_check(1)").fetchone()
            integrity = str(row[0]) if row is not None else "missing_result"
        except (OSError, sqlite3.DatabaseError) as exc:
            integrity = "error"
            error_type = type(exc).__name__
        parent_writable = os.access(self.path.parent, os.W_OK)
        file_writable = not self.path.exists() or os.access(self.path, os.W_OK)
        return {
            "ok": integrity == "ok" and parent_writable and file_writable,
            "integrity": integrity,
            "schema_version": self.schema_version() if integrity != "error" else None,
            "writable": parent_writable and file_writable,
            "error_type": error_type,
        }

    def _migrate_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
            current = 0 if row is None else int(row["version"])
            migration_required = current != SCHEMA_VERSION
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"State database schema {current} is newer than supported schema {SCHEMA_VERSION}."
                )
            if current < 1:
                _apply_schema_v1(conn)
                current = 1
            if current < 2:
                _apply_schema_v2(conn)
                current = 2
            if current < 3:
                _apply_schema_v3(conn)
                current = 3
            if current < 4:
                _apply_schema_v4(conn)
                current = 4
            if current < 5:
                _apply_schema_v5(conn)
                current = 5
            if current < 6:
                _apply_schema_v6(conn)
                current = 6
            if current < 7:
                _apply_schema_v7(conn)
                current = 7
            if current < 8:
                _apply_schema_v8(conn)
                current = 8
            if current < 9:
                _apply_schema_v9(conn)
                current = 9
            if current < 10:
                _apply_schema_v10(conn)
                current = 10
            if current < 11:
                _apply_schema_v11(conn)
                current = 11
            if current < 12:
                _apply_schema_v12(conn)
                current = 12
            if current < 13:
                _apply_schema_v13(conn)
                current = 13
            if current < 14:
                _apply_schema_v14(conn)
                current = 14
            if current < 15:
                _apply_schema_v15(conn)
                current = 15
            if current < 16:
                _apply_schema_v16(conn)
                current = 16
            if current < 17:
                _apply_schema_v17(conn)
                current = 17
            if current < 18:
                _apply_schema_v18(conn)
                current = 18
            if current < 19:
                _apply_schema_v19(conn)
                current = 19
            if current < SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported schema migration target: {current} -> {SCHEMA_VERSION}"
                )
            if current == SCHEMA_VERSION and migration_required:
                conn.execute(
                    """
                    INSERT INTO schema_version (id, version, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        version = excluded.version,
                        updated_at = excluded.updated_at
                    """,
                    (current, utc_now()),
                )

    def _enable_wal_mode(self) -> None:
        for attempt in range(10):
            try:
                conn = sqlite3.connect(self.path, timeout=5.0)
                try:
                    conn.execute("PRAGMA busy_timeout=5000")
                    mode = conn.execute("PRAGMA journal_mode").fetchone()
                    if mode is None or str(mode[0]).lower() != "wal":
                        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                finally:
                    conn.close()
                if mode is not None and str(mode[0]).lower() == "wal":
                    return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 9:
                    raise
                sleep(0.05 * (attempt + 1))
        raise RuntimeError("Unable to enable SQLite WAL journal mode.")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._open_configured_connection()
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _open_configured_connection(self) -> sqlite3.Connection:
        """Open a fresh handle, retrying only transient setup-time BUSY errors."""

        for attempt in range(_SQLITE_CONNECTION_SETUP_ATTEMPTS):
            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
                conn.row_factory = sqlite3.Row
                _apply_connection_pragmas(conn)
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    conn.close()
                if not _is_sqlite_busy(exc) or attempt == _SQLITE_CONNECTION_SETUP_ATTEMPTS - 1:
                    raise
                sleep(_SQLITE_CONNECTION_SETUP_RETRY_BASE_SECONDS * (attempt + 1))
            except BaseException:
                if conn is not None:
                    conn.close()
                raise
            else:
                return conn
        raise RuntimeError("Unable to configure SQLite state connection.")


def _execute_schema_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute migration DDL without ``executescript``'s implicit pre-commit."""

    if not conn.in_transaction:
        raise RuntimeError("schema scripts require an active transaction")
    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        statement = "".join(pending)
        if not sqlite3.complete_statement(statement):
            continue
        if statement.strip():
            conn.execute(statement)
        pending.clear()
    remainder = "".join(pending).strip()
    if remainder:
        raise RuntimeError("incomplete SQL statement in schema migration")
    if not conn.in_transaction:
        raise RuntimeError("schema script unexpectedly ended its transaction")


def _apply_schema_v1(conn: sqlite3.Connection) -> None:
    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            message TEXT NOT NULL,
            session_id TEXT NOT NULL,
            workspace TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'mock',
            model TEXT NOT NULL,
            assistant_message TEXT NOT NULL DEFAULT '',
            context_chars INTEGER NOT NULL DEFAULT 0,
            tool_count INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT NOT NULL DEFAULT '',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS approval_requests (
            approval_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            risk TEXT NOT NULL,
            status TEXT NOT NULL,
            decision_json TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            transport TEXT NOT NULL,
            command TEXT,
            args_json TEXT NOT NULL,
            env_json TEXT NOT NULL,
            url TEXT,
            enabled INTEGER NOT NULL,
            tools_json TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skill_registry (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            path TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
        CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_run_steps_run_id_id ON run_steps(run_id, id);
        CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests(status);
        CREATE INDEX IF NOT EXISTS idx_approval_requests_run_id ON approval_requests(run_id);
        CREATE INDEX IF NOT EXISTS idx_mcp_servers_enabled ON mcp_servers(enabled);
        CREATE INDEX IF NOT EXISTS idx_skill_registry_enabled ON skill_registry(enabled);
        """
    )


def _apply_schema_v2(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "mcp_servers")
    for name, definition in {
        "last_synced_at": "TEXT",
        "last_seen_at": "TEXT",
        "tool_count": "INTEGER NOT NULL DEFAULT 0",
        "capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
        "risk_policy": "TEXT NOT NULL DEFAULT 'default'",
        "secret_env_json": "TEXT NOT NULL DEFAULT '{}'",  # nosec B105
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE mcp_servers ADD COLUMN {name} {definition}")

    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS task_nodes (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            parent_id TEXT,
            title TEXT NOT NULL,
            goal TEXT NOT NULL,
            profile TEXT NOT NULL,
            status TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            plan_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subagent_runs (
            subagent_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT,
            profile TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT NOT NULL DEFAULT '',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_task_nodes_run_id ON task_nodes(run_id);
        CREATE INDEX IF NOT EXISTS idx_task_nodes_status ON task_nodes(status);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_run_id ON subagent_runs(run_id);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_status ON subagent_runs(status);
        """
    )


def _apply_schema_v3(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "mcp_servers")
    for name, definition in {
        "session_state": "TEXT NOT NULL DEFAULT 'disconnected'",
        "last_call_at": "TEXT",
        "last_error_at": "TEXT",
        "failure_count": "INTEGER NOT NULL DEFAULT 0",
        "last_latency_ms": "INTEGER",
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE mcp_servers ADD COLUMN {name} {definition}")


def _apply_schema_v4(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "task_nodes")
    for name, definition in {
        "dependencies_json": "TEXT NOT NULL DEFAULT '[]'",
        "required_tools_json": "TEXT NOT NULL DEFAULT '[]'",
        "risk": "TEXT NOT NULL DEFAULT 'low'",
        "acceptance_criteria_json": "TEXT NOT NULL DEFAULT '[]'",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "failure_reason": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE task_nodes ADD COLUMN {name} {definition}")


def _apply_schema_v5(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "mcp_servers")
    if "vetting_json" not in existing:
        conn.execute("ALTER TABLE mcp_servers ADD COLUMN vetting_json TEXT NOT NULL DEFAULT '{}'")


def _apply_schema_v6(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "task_nodes")
    for name, definition in {
        "diagnosis_json": "TEXT",
        "retry_strategy_json": "TEXT",
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE task_nodes ADD COLUMN {name} {definition}")


def _apply_schema_v7(conn: sqlite3.Connection) -> None:
    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS plugin_registry (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_ref TEXT,
            commit_sha TEXT NOT NULL,
            install_path TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            risk_report_json TEXT NOT NULL,
            install_status TEXT NOT NULL,
            format TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_plugin_registry_enabled ON plugin_registry(enabled);
        """
    )


def _apply_schema_v8(conn: sqlite3.Connection) -> None:
    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS trace_spans (
            span_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            parent_span_id TEXT,
            span_type TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT,
            error TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_trace_spans_run_id ON trace_spans(run_id);
        CREATE INDEX IF NOT EXISTS idx_trace_spans_type ON trace_spans(span_type);
        CREATE INDEX IF NOT EXISTS idx_trace_spans_parent ON trace_spans(parent_span_id);
        """
    )


def _apply_schema_v9(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "runs")
    if "provider" not in existing:
        conn.execute("ALTER TABLE runs ADD COLUMN provider TEXT NOT NULL DEFAULT 'mock'")


def _apply_schema_v10(conn: sqlite3.Connection) -> None:
    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS promotion_ledger (
            promotion_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            source_layer TEXT NOT NULL,
            target_layer TEXT NOT NULL,
            decision_reason TEXT NOT NULL,
            validation_score REAL NOT NULL,
            repeat_count INTEGER NOT NULL,
            explicit_instruction INTEGER NOT NULL,
            optimizer_trace_json TEXT NOT NULL,
            promoted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS promotion_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promotion_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            evidence_record_id TEXT,
            notes TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (promotion_id) REFERENCES promotion_ledger(promotion_id)
        );

        CREATE INDEX IF NOT EXISTS idx_promotion_ledger_target_layer ON promotion_ledger(target_layer);
        CREATE INDEX IF NOT EXISTS idx_promotion_ledger_promoted_at ON promotion_ledger(promoted_at);
        CREATE INDEX IF NOT EXISTS idx_promotion_outcomes_promotion_id ON promotion_outcomes(promotion_id);
        """
    )


def _apply_schema_v11(conn: sqlite3.Connection) -> None:
    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS behavior_delta_ledger (
            delta_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            target_layer TEXT NOT NULL,
            risk TEXT NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            trigger_json TEXT NOT NULL,
            behavior_change TEXT NOT NULL,
            validation_plan_json TEXT NOT NULL,
            rollback_plan_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            importance REAL NOT NULL,
            created_from_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS behavior_delta_activations (
            id TEXT PRIMARY KEY,
            delta_id TEXT NOT NULL,
            run_id TEXT,
            task_id TEXT,
            objective TEXT,
            activated_at TEXT NOT NULL,
            activation_reason TEXT NOT NULL,
            compiled_section TEXT NOT NULL,
            FOREIGN KEY(delta_id) REFERENCES behavior_delta_ledger(delta_id)
        );

        CREATE TABLE IF NOT EXISTS behavior_delta_outcomes (
            id TEXT PRIMARY KEY,
            delta_id TEXT NOT NULL,
            run_id TEXT,
            outcome TEXT NOT NULL,
            evidence_ref_json TEXT,
            notes TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(delta_id) REFERENCES behavior_delta_ledger(delta_id)
        );

        CREATE INDEX IF NOT EXISTS idx_behavior_delta_ledger_status ON behavior_delta_ledger(status);
        CREATE INDEX IF NOT EXISTS idx_behavior_delta_ledger_kind ON behavior_delta_ledger(kind);
        CREATE INDEX IF NOT EXISTS idx_behavior_delta_ledger_target_layer ON behavior_delta_ledger(target_layer);
        CREATE INDEX IF NOT EXISTS idx_behavior_delta_activations_delta_id ON behavior_delta_activations(delta_id);
        CREATE INDEX IF NOT EXISTS idx_behavior_delta_outcomes_delta_id ON behavior_delta_outcomes(delta_id);
        """
    )


def _apply_schema_v12(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "runs")
    for name, definition in {
        "lease_owner": "TEXT",
        "lease_generation": "INTEGER NOT NULL DEFAULT 0",
        "lease_expires_at": "TEXT",
        "heartbeat_at": "TEXT",
        "interrupted_at": "TEXT",
        "recovery_reason": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_lease_expires_at ON runs(lease_expires_at)")


def _apply_schema_v13(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "runs")
    if "config_revision" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN config_revision TEXT")
    if "config_snapshot_json" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN config_snapshot_json TEXT NOT NULL DEFAULT '{}'")


def _apply_schema_v14(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "approval_requests")
    if "principal" not in columns:
        conn.execute(
            "ALTER TABLE approval_requests ADD COLUMN principal TEXT NOT NULL DEFAULT 'owner'"
        )
    if "expires_at" not in columns:
        conn.execute("ALTER TABLE approval_requests ADD COLUMN expires_at TEXT")
    # Existing undecided approvals predate the expiry contract and cannot be
    # trusted indefinitely. Expire them on the first post-v14 access.
    conn.execute("UPDATE approval_requests SET expires_at = created_at WHERE expires_at IS NULL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_expires_at "
        "ON approval_requests(expires_at)"
    )


def _apply_schema_v15(conn: sqlite3.Connection) -> None:
    approval_columns = _columns(conn, "approval_requests")
    if "capability_revision" not in approval_columns:
        conn.execute(
            "ALTER TABLE approval_requests "
            "ADD COLUMN capability_revision INTEGER NOT NULL DEFAULT 0"
        )
    if "resource_digest" not in approval_columns:
        conn.execute(
            "ALTER TABLE approval_requests ADD COLUMN resource_digest TEXT NOT NULL DEFAULT ''"
        )
    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS capability_overrides (
            kind TEXT NOT NULL CHECK (kind IN ('tool', 'mcp_server', 'skill')),
            capability_id TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            revision INTEGER NOT NULL CHECK (revision > 0),
            resource_digest TEXT,
            updated_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (kind, capability_id)
        );

        CREATE TABLE IF NOT EXISTS capability_change_log (
            change_id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('tool', 'mcp_server', 'skill')),
            capability_id TEXT NOT NULL,
            previous_enabled INTEGER NOT NULL CHECK (previous_enabled IN (0, 1)),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            previous_revision INTEGER NOT NULL CHECK (previous_revision >= 0),
            revision INTEGER NOT NULL CHECK (revision > 0),
            resource_digest TEXT,
            updated_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_capability_overrides_kind
            ON capability_overrides(kind, capability_id);
        CREATE INDEX IF NOT EXISTS idx_capability_change_log_capability
            ON capability_change_log(kind, capability_id, change_id);
        """
    )


def _apply_schema_v16(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "runs")
    # Some narrowly scoped legacy fixtures contain only the table introduced in
    # the version under test. A missing runs table has no run rows to migrate.
    if not columns:
        return
    if "turn_source_json" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN turn_source_json TEXT")
    if "turn_origin" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN turn_origin TEXT NOT NULL DEFAULT 'primary_user'")
    if "transcript_scope" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN transcript_scope TEXT NOT NULL DEFAULT 'primary'")


def _apply_schema_v17(conn: sqlite3.Connection) -> None:
    _execute_schema_script(
        conn,
        f"""
        CREATE TABLE IF NOT EXISTS routines (
            routine_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule_kind TEXT NOT NULL CHECK (schedule_kind IN ('once', 'interval')),
            start_at TEXT NOT NULL,
            interval_seconds INTEGER,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            next_run_at TEXT,
            workspace TEXT,
            provider TEXT,
            model TEXT,
            autonomy_mode TEXT NOT NULL DEFAULT 'background'
                CHECK (autonomy_mode IN ('background', 'manual', 'autonomous')),
            misfire_grace_seconds INTEGER NOT NULL DEFAULT 60
                CHECK (
                    misfire_grace_seconds BETWEEN
                        {MIN_ROUTINE_MISFIRE_GRACE_SECONDS}
                        AND {MAX_ROUTINE_MISFIRE_GRACE_SECONDS}
                ),
            last_scheduled_at TEXT,
            deleted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (schedule_kind = 'once' AND interval_seconds IS NULL)
                OR (
                    schedule_kind = 'interval'
                    AND interval_seconds BETWEEN {MIN_ROUTINE_INTERVAL_SECONDS}
                        AND {MAX_ROUTINE_INTERVAL_SECONDS}
                )
            )
        );

        CREATE TABLE IF NOT EXISTS routine_occurrences (
            occurrence_id TEXT PRIMARY KEY,
            routine_id TEXT NOT NULL,
            routine_revision INTEGER NOT NULL CHECK (routine_revision > 0),
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('claimed', 'running', 'completed', 'failed', 'skipped')),
            run_id TEXT NOT NULL,
            request_json TEXT NOT NULL DEFAULT '{{}}',
            claim_owner TEXT,
            claim_generation INTEGER NOT NULL DEFAULT 1 CHECK (claim_generation > 0),
            claim_expires_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            skip_reason TEXT,
            error TEXT,
            result_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (routine_id, routine_revision, scheduled_for),
            FOREIGN KEY (routine_id) REFERENCES routines(routine_id)
        );

        CREATE INDEX IF NOT EXISTS idx_routines_due
            ON routines(enabled, deleted_at, next_run_at);
        CREATE INDEX IF NOT EXISTS idx_routine_occurrences_routine
            ON routine_occurrences(routine_id, scheduled_for);
        CREATE INDEX IF NOT EXISTS idx_routine_occurrences_claim
            ON routine_occurrences(status, claim_expires_at);
        CREATE INDEX IF NOT EXISTS idx_routine_occurrences_run
            ON routine_occurrences(run_id);
        """
    )


def _apply_schema_v18(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "approval_requests")
    if not columns:
        return
    for name in (
        "execution_claim_owner",
        "execution_claim_id",
        "execution_claim_started_at",
        "execution_claim_expires_at",
        "execution_claim_task_id",
        "execution_claim_subagent_id",
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE approval_requests ADD COLUMN {name} TEXT")


def _apply_schema_v19(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "routine_occurrences")
    if not columns:
        return
    if "trigger_kind" not in columns:
        conn.execute(
            """
            ALTER TABLE routine_occurrences
            ADD COLUMN trigger_kind TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (trigger_kind IN ('scheduled', 'manual'))
            """
        )
    if "trigger_key_digest" not in columns:
        conn.execute(
            "ALTER TABLE routine_occurrences ADD COLUMN trigger_key_digest TEXT"
        )
    if "requested_at" not in columns:
        conn.execute("ALTER TABLE routine_occurrences ADD COLUMN requested_at TEXT")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_occurrences_manual_trigger
            ON routine_occurrences(routine_id, trigger_key_digest)
            WHERE trigger_kind = 'manual' AND trigger_key_digest IS NOT NULL
        """
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _state_initialization_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.kestrel-state-init.lock")


@contextmanager
def _state_initialization_lock(path: Path) -> Iterator[None]:
    """Serialize sensitive SQLite initialization across OS processes."""

    created_directory = _create_state_directory(path.parent)
    if os.name != "nt":
        directory_fd = _open_owned_state_directory(path.parent)
        try:
            if created_directory:
                chmod_descriptor(directory_fd, _STATE_DIRECTORY_MODE)
        finally:
            os.close(directory_fd)
    descriptor = open_private_file_descriptor(_state_initialization_lock_path(path))
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        lock_exclusive(handle)
        try:
            yield
        finally:
            unlock(handle)


def _prepare_private_sqlite_storage(path: Path) -> None:
    directory = path.parent
    created_directory = _create_state_directory(directory)
    if os.name == "nt":
        return
    directory_fd = _open_owned_state_directory(directory)
    try:
        if created_directory:
            chmod_descriptor(directory_fd, _STATE_DIRECTORY_MODE)
        _harden_sqlite_entry(
            directory_fd,
            path.name,
            display_path=path,
            create=True,
        )
        for suffix in _SQLITE_PRIVATE_SUFFIXES[1:]:
            _harden_sqlite_entry(
                directory_fd,
                f"{path.name}{suffix}",
                display_path=Path(f"{path}{suffix}"),
                create=False,
            )
    finally:
        os.close(directory_fd)


def _harden_private_sqlite_files(path: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = _open_owned_state_directory(path.parent)
    try:
        for suffix in _SQLITE_PRIVATE_SUFFIXES:
            _harden_sqlite_entry(
                directory_fd,
                f"{path.name}{suffix}",
                display_path=Path(f"{path}{suffix}"),
                create=False,
            )
    finally:
        os.close(directory_fd)


def _create_state_directory(directory: Path) -> bool:
    """Create only the immediate state directory and report who won the race."""

    try:
        directory.mkdir(mode=_STATE_DIRECTORY_MODE)
    except FileNotFoundError:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir(mode=_STATE_DIRECTORY_MODE)
        except FileExistsError:
            return False
    except FileExistsError:
        return False
    return True


def _open_owned_state_directory(directory: Path) -> int:
    if directory.is_symlink():
        raise ValueError(f"state directory must not be a symbolic link: {directory}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(directory, flags)
    except OSError as exc:
        if directory.is_symlink():
            raise ValueError(f"state directory must not be a symbolic link: {directory}") from exc
        raise
    try:
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"state directory must be a directory: {directory}")
        _require_current_owner(metadata, directory)
    except Exception:
        os.close(directory_fd)
        raise
    return directory_fd


def _harden_sqlite_entry(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
    create: bool,
) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        if not create:
            return
        try:
            descriptor = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                _SQLITE_FILE_MODE,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
    except PermissionError:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_sqlite_file(metadata, display_path)
        os.chmod(
            name,
            _SQLITE_FILE_MODE,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            raise exc from None
        _validate_sqlite_file(metadata, display_path)
        raise
    try:
        metadata = os.fstat(descriptor)
        _validate_sqlite_file(metadata, display_path)
        chmod_descriptor(descriptor, _SQLITE_FILE_MODE)
    finally:
        os.close(descriptor)


def _validate_sqlite_file(metadata: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"SQLite state files must not be symbolic links: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"SQLite state files must be regular files: {path}")
    if metadata.st_nlink > 1:
        raise ValueError(f"SQLite state files must not be hard-linked: {path}")
    _require_current_owner(metadata, path)


def _require_current_owner(metadata: os.stat_result, path: Path) -> None:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and metadata.st_uid != geteuid():
        raise PermissionError(f"state storage must be owned by the current user: {path}")


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")


def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) == sqlite3.SQLITE_BUSY


_ALLOWED_UPDATE_COLUMNS = {
    "runs": {
        "status",
        "message",
        "session_id",
        "workspace",
        "provider",
        "model",
        "assistant_message",
        "context_chars",
        "tool_count",
        "stop_reason",
        "error",
        "lease_owner",
        "lease_generation",
        "lease_expires_at",
        "heartbeat_at",
        "interrupted_at",
        "recovery_reason",
        "config_revision",
        "config_snapshot_json",
        "turn_source_json",
        "turn_origin",
        "transcript_scope",
        "updated_at",
    },
    "task_nodes": {
        "run_id",
        "parent_id",
        "title",
        "goal",
        "profile",
        "status",
        "approved",
        "plan_json",
        "result_json",
        "dependencies_json",
        "required_tools_json",
        "risk",
        "acceptance_criteria_json",
        "attempt_count",
        "failure_reason",
        "diagnosis_json",
        "retry_strategy_json",
        "updated_at",
    },
    "subagent_runs": {
        "run_id",
        "task_id",
        "profile",
        "goal",
        "status",
        "result",
        "error",
        "updated_at",
    },
}


def _validated_column(table: str, column: str) -> str:
    allowed = _ALLOWED_UPDATE_COLUMNS.get(table, set())
    if column not in allowed:
        raise ValueError(f"Unknown {table} column: {column}")
    return column


def _run_transition_allowed(current: str, target: str) -> bool:
    allowed = {
        "queued": {"running", "blocked", "cancelled", "failed"},
        "running": {"blocked", "completed", "failed", "cancelled"},
        "blocked": {"queued", "running", "cancelled", "failed"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    }
    if current == target:
        return True
    return target in allowed.get(current, set())


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        message=str(row["message"]),
        session_id=str(row["session_id"]),
        workspace=str(row["workspace"]),
        provider=str(_row_get(row, "provider", "mock") or "mock"),
        model=str(row["model"]),
        assistant_message=str(row["assistant_message"]),
        context_chars=int(row["context_chars"]),
        tool_count=int(row["tool_count"]),
        stop_reason=str(row["stop_reason"]),
        error=None if row["error"] is None else str(row["error"]),
        lease_owner=None
        if _row_get(row, "lease_owner") is None
        else str(_row_get(row, "lease_owner")),
        lease_generation=int(str(_row_get(row, "lease_generation", 0) or 0)),
        lease_expires_at=None
        if _row_get(row, "lease_expires_at") is None
        else str(_row_get(row, "lease_expires_at")),
        heartbeat_at=None
        if _row_get(row, "heartbeat_at") is None
        else str(_row_get(row, "heartbeat_at")),
        interrupted_at=None
        if _row_get(row, "interrupted_at") is None
        else str(_row_get(row, "interrupted_at")),
        recovery_reason=str(_row_get(row, "recovery_reason", "") or ""),
        config_revision=_optional_str(_row_get(row, "config_revision")),
        config_snapshot=_json_or_empty(_row_get(row, "config_snapshot_json", "{}")),
        turn_source=_json_dict_or_none(_row_get(row, "turn_source_json")),
        turn_origin=str(_row_get(row, "turn_origin", "primary_user") or "primary_user"),
        transcript_scope=str(_row_get(row, "transcript_scope", "primary") or "primary"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _approval_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "approval_id": str(row["approval_id"]),
        "run_id": str(row["run_id"]),
        "tool_call_id": str(row["tool_call_id"]),
        "tool_name": str(row["tool_name"]),
        "arguments": json.loads(str(row["arguments_json"])),
        "risk": str(row["risk"]),
        "principal": str(_row_get(row, "principal", "owner")),
        "expires_at": _optional_str(_row_get(row, "expires_at")),
        "capability_revision": int(str(_row_get(row, "capability_revision", 0) or 0)),
        "resource_digest": str(_row_get(row, "resource_digest", "") or ""),
        "status": str(row["status"]),
        "decision": _json_or_none(row["decision_json"]),
        "result": _json_or_none(row["result_json"]),
        "execution_claim_owner": _optional_str(_row_get(row, "execution_claim_owner")),
        "execution_claim_id": _optional_str(_row_get(row, "execution_claim_id")),
        "execution_claim_started_at": _optional_str(_row_get(row, "execution_claim_started_at")),
        "execution_claim_expires_at": _optional_str(_row_get(row, "execution_claim_expires_at")),
        "execution_claim_task_id": _optional_str(_row_get(row, "execution_claim_task_id")),
        "execution_claim_subagent_id": _optional_str(_row_get(row, "execution_claim_subagent_id")),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _upsert_mcp_server_row(conn: sqlite3.Connection, server: dict[str, Any]) -> None:
    server_id = str(server["id"])
    tools = list(server.get("tools", []))
    capabilities = server.get("capabilities") or sorted(
        {
            str(capability)
            for tool in tools
            for capability in list(dict(tool).get("capabilities", []))
        }
    )
    payload = {
        "name": server.get("name", server_id),
        "transport": server.get("transport", "stdio"),
        "command": server.get("command"),
        "args_json": json.dumps(server.get("args", [])),
        "env_json": json.dumps(server.get("env", {})),
        "url": server.get("url"),
        "enabled": 1 if server.get("enabled", True) else 0,
        "tools_json": json.dumps(tools),
        "status": server.get("status", "configured"),
        "error": server.get("error"),
        "last_synced_at": server.get("last_synced_at"),
        "last_seen_at": server.get("last_seen_at"),
        "tool_count": int(server.get("tool_count", len(tools))),
        "capabilities_json": json.dumps(capabilities),
        "risk_policy": server.get("risk_policy", "approval_by_default"),
        "secret_env_json": json.dumps(server.get("secret_env", {})),
        "session_state": server.get("session_state", "disconnected"),
        "last_call_at": server.get("last_call_at"),
        "last_error_at": server.get("last_error_at"),
        "failure_count": int(server.get("failure_count", 0)),
        "last_latency_ms": server.get("last_latency_ms"),
        "vetting_json": json.dumps(server.get("vetting", {})),
        "updated_at": utc_now(),
    }
    conn.execute(
        """
        INSERT INTO mcp_servers (
            id, name, transport, command, args_json, env_json, url, enabled,
            tools_json, status, error, last_synced_at, last_seen_at, tool_count,
            capabilities_json, risk_policy, secret_env_json, session_state, last_call_at,
            last_error_at, failure_count, last_latency_ms, vetting_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            transport = excluded.transport,
            command = excluded.command,
            args_json = excluded.args_json,
            env_json = excluded.env_json,
            url = excluded.url,
            enabled = excluded.enabled,
            tools_json = excluded.tools_json,
            status = excluded.status,
            error = excluded.error,
            last_synced_at = excluded.last_synced_at,
            last_seen_at = excluded.last_seen_at,
            tool_count = excluded.tool_count,
            capabilities_json = excluded.capabilities_json,
            risk_policy = excluded.risk_policy,
            secret_env_json = excluded.secret_env_json,
            session_state = excluded.session_state,
            last_call_at = excluded.last_call_at,
            last_error_at = excluded.last_error_at,
            failure_count = excluded.failure_count,
            last_latency_ms = excluded.last_latency_ms,
            vetting_json = excluded.vetting_json,
            updated_at = excluded.updated_at
        """,
        (server_id, *payload.values()),
    )


def _upsert_skill_row(conn: sqlite3.Connection, skill: dict[str, Any]) -> None:
    skill_id = str(skill["id"])
    conn.execute(
        """
        INSERT INTO skill_registry (id, name, description, path, manifest_json, enabled, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            path = excluded.path,
            manifest_json = excluded.manifest_json,
            enabled = excluded.enabled,
            updated_at = excluded.updated_at
        """,
        (
            skill_id,
            skill.get("name", skill_id),
            skill.get("description", ""),
            skill.get("path", ""),
            json.dumps(skill.get("manifest", {})),
            1 if skill.get("enabled", True) else 0,
            utc_now(),
        ),
    )


def _upsert_plugin_row(conn: sqlite3.Connection, plugin: dict[str, Any]) -> None:
    plugin_id = str(plugin["id"])
    now = utc_now()
    created_at = str(plugin.get("created_at") or now)
    current = conn.execute(
        "SELECT created_at FROM plugin_registry WHERE id = ?", (plugin_id,)
    ).fetchone()
    if current is not None:
        created_at = str(current["created_at"])
    conn.execute(
        """
        INSERT INTO plugin_registry (
            id, name, description, source_url, source_ref, commit_sha, install_path,
            manifest_json, capabilities_json, enabled, risk_report_json,
            install_status, format, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            source_url = excluded.source_url,
            source_ref = excluded.source_ref,
            commit_sha = excluded.commit_sha,
            install_path = excluded.install_path,
            manifest_json = excluded.manifest_json,
            capabilities_json = excluded.capabilities_json,
            enabled = excluded.enabled,
            risk_report_json = excluded.risk_report_json,
            install_status = excluded.install_status,
            format = excluded.format,
            updated_at = excluded.updated_at
        """,
        (
            plugin_id,
            plugin.get("name", plugin_id),
            plugin.get("description", ""),
            plugin.get("source_url", ""),
            plugin.get("source_ref"),
            plugin.get("commit_sha", ""),
            plugin.get("install_path", ""),
            json.dumps(plugin.get("manifest", {})),
            json.dumps(plugin.get("capabilities", [])),
            1 if plugin.get("enabled", False) else 0,
            json.dumps(plugin.get("risk_report", {})),
            plugin.get("install_status", "installed"),
            plugin.get("format", "kestrel"),
            created_at,
            now,
        ),
    )


def _validate_plugin_bundle_ids(
    prefix: str,
    *,
    skills: list[dict[str, Any]],
    mcp_servers: list[dict[str, Any]],
) -> None:
    skill_ids = [str(skill["id"]) for skill in skills]
    server_ids = [str(server["id"]) for server in mcp_servers]
    if len(set(skill_ids)) != len(skill_ids) or any(
        not skill_id.startswith(prefix) for skill_id in skill_ids
    ):
        raise ValueError("Plugin skill bundle contains an invalid or duplicate id.")
    if len(set(server_ids)) != len(server_ids) or any(
        not server_id.startswith(prefix) for server_id in server_ids
    ):
        raise ValueError("Plugin MCP bundle contains an invalid or duplicate id.")


def _plugin_extension_rows(
    conn: sqlite3.Connection,
    prefix: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    skill_rows = conn.execute("SELECT * FROM skill_registry").fetchall()
    server_rows = conn.execute("SELECT * FROM mcp_servers").fetchall()
    skills = {
        str(row["id"]): _skill_from_row(row)
        for row in skill_rows
        if str(row["id"]).startswith(prefix)
    }
    servers = {
        str(row["id"]): _mcp_from_row(row)
        for row in server_rows
        if str(row["id"]).startswith(prefix)
    }
    return skills, servers


def _mcp_tool_ids(server: dict[str, Any]) -> set[str]:
    return {
        str(tool["name"])
        for tool in server.get("tools", [])
        if isinstance(tool, dict) and tool.get("name")
    }


def _delete_capability_override_row(
    conn: sqlite3.Connection,
    kind: str,
    capability_id: str,
) -> None:
    row = conn.execute(
        """
        SELECT * FROM capability_overrides
        WHERE kind = ? AND capability_id = ?
        """,
        (kind, capability_id),
    ).fetchone()
    if row is None:
        return
    current = _capability_override_from_row(row)
    next_revision = int(current["revision"]) + 1
    now = utc_now()
    conn.execute(
        """
        INSERT INTO capability_change_log (
            kind, capability_id, previous_enabled, enabled,
            previous_revision, revision, resource_digest, updated_by, created_at
        ) VALUES (?, ?, ?, 0, ?, ?, ?, 'system', ?)
        """,
        (
            kind,
            capability_id,
            1 if bool(current["enabled"]) else 0,
            int(current["revision"]),
            next_revision,
            current.get("resource_digest"),
            now,
        ),
    )
    conn.execute(
        "DELETE FROM capability_overrides WHERE kind = ? AND capability_id = ?",
        (kind, capability_id),
    )


def _mcp_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "transport": str(row["transport"]),
        "command": None if row["command"] is None else str(row["command"]),
        "args": json.loads(str(row["args_json"])),
        "env": json.loads(str(row["env_json"])),
        "url": None if row["url"] is None else str(row["url"]),
        "enabled": bool(row["enabled"]),
        "tools": json.loads(str(row["tools_json"])),
        "status": str(row["status"]),
        "error": None if row["error"] is None else str(row["error"]),
        "last_synced_at": _row_get(row, "last_synced_at"),
        "last_seen_at": _row_get(row, "last_seen_at"),
        "tool_count": int(str(_row_get(row, "tool_count", 0) or 0)),
        "capabilities": json.loads(str(_row_get(row, "capabilities_json", "[]") or "[]")),
        "risk_policy": str(
            _row_get(row, "risk_policy", "approval_by_default") or "approval_by_default"
        ),
        "secret_env": json.loads(str(_row_get(row, "secret_env_json", "{}") or "{}")),
        "session_state": str(_row_get(row, "session_state", "disconnected") or "disconnected"),
        "last_call_at": _row_get(row, "last_call_at"),
        "last_error_at": _row_get(row, "last_error_at"),
        "failure_count": int(str(_row_get(row, "failure_count", 0) or 0)),
        "last_latency_ms": _row_get(row, "last_latency_ms"),
        "vetting": json.loads(str(_row_get(row, "vetting_json", "{}") or "{}")),
        "updated_at": str(row["updated_at"]),
    }


def _skill_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "description": str(row["description"]),
        "path": str(row["path"]),
        "manifest": json.loads(str(row["manifest_json"])),
        "enabled": bool(row["enabled"]),
        "updated_at": str(row["updated_at"]),
    }


def _plugin_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "description": str(row["description"]),
        "source_url": str(row["source_url"]),
        "source_ref": None if row["source_ref"] is None else str(row["source_ref"]),
        "commit_sha": str(row["commit_sha"]),
        "install_path": str(row["install_path"]),
        "manifest": json.loads(str(row["manifest_json"])),
        "capabilities": json.loads(str(row["capabilities_json"])),
        "enabled": bool(row["enabled"]),
        "risk_report": json.loads(str(row["risk_report_json"])),
        "install_status": str(row["install_status"]),
        "format": str(row["format"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _default_capability_override(
    kind: str,
    capability_id: str,
    *,
    default_enabled: bool,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "capability_id": capability_id,
        "enabled": default_enabled,
        "revision": 0,
        "resource_digest": None,
        "updated_by": None,
        "created_at": None,
        "updated_at": None,
        "persisted": False,
    }


def _capability_override_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": str(row["kind"]),
        "capability_id": str(row["capability_id"]),
        "enabled": bool(row["enabled"]),
        "revision": int(row["revision"]),
        "resource_digest": _optional_str(row["resource_digest"]),
        "updated_by": str(row["updated_by"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "persisted": True,
    }


def _capability_change_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "change_id": int(row["change_id"]),
        "kind": str(row["kind"]),
        "capability_id": str(row["capability_id"]),
        "previous_enabled": bool(row["previous_enabled"]),
        "enabled": bool(row["enabled"]),
        "previous_revision": int(row["previous_revision"]),
        "revision": int(row["revision"]),
        "resource_digest": _optional_str(row["resource_digest"]),
        "updated_by": str(row["updated_by"]),
        "created_at": str(row["created_at"]),
    }


def _capability_kind(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in CAPABILITY_KINDS:
        raise ValueError(
            f"unsupported capability kind: {normalized or '<empty>'}; "
            f"expected one of {', '.join(sorted(CAPABILITY_KINDS))}"
        )
    return normalized


def _capability_id(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("capability_id is required")
    if len(normalized) > 512:
        raise ValueError("capability_id must be at most 512 characters")
    if not normalized.isprintable():
        raise ValueError("capability_id contains non-printable characters")
    return normalized


def _capability_bool(value: bool, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _capability_revision(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    return value


def _capability_metadata(
    value: str | None,
    field: str,
    *,
    optional: bool,
) -> str | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field} is required")
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        if optional:
            return None
        raise ValueError(f"{field} is required")
    if len(normalized) > 256:
        raise ValueError(f"{field} must be at most 256 characters")
    if not normalized.isprintable():
        raise ValueError(f"{field} contains non-printable characters")
    return normalized


def _task_from_row(row: sqlite3.Row) -> TaskNodeRecord:
    return TaskNodeRecord(
        task_id=str(row["task_id"]),
        run_id=str(row["run_id"]),
        parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
        title=str(row["title"]),
        goal=str(row["goal"]),
        profile=str(row["profile"]),
        status=str(row["status"]),
        approved=bool(row["approved"]),
        plan=json.loads(str(row["plan_json"])),
        result=_json_or_none(row["result_json"]),
        dependencies=tuple(json.loads(str(_row_get(row, "dependencies_json", "[]") or "[]"))),
        required_tools=tuple(json.loads(str(_row_get(row, "required_tools_json", "[]") or "[]"))),
        risk=str(_row_get(row, "risk", "low") or "low"),
        acceptance_criteria=tuple(
            json.loads(str(_row_get(row, "acceptance_criteria_json", "[]") or "[]"))
        ),
        attempt_count=int(str(_row_get(row, "attempt_count", 0) or 0)),
        failure_reason=str(_row_get(row, "failure_reason", "") or ""),
        diagnosis=_json_or_none(_row_get(row, "diagnosis_json")),
        retry_strategy=_json_or_none(_row_get(row, "retry_strategy_json")),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _subagent_from_row(row: sqlite3.Row) -> SubagentRunRecord:
    return SubagentRunRecord(
        subagent_id=str(row["subagent_id"]),
        run_id=str(row["run_id"]),
        task_id=None if row["task_id"] is None else str(row["task_id"]),
        profile=str(row["profile"]),
        goal=str(row["goal"]),
        status=str(row["status"]),
        result=str(row["result"]),
        error=None if row["error"] is None else str(row["error"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _trace_span_from_row(row: sqlite3.Row) -> TraceSpanRecord:
    return TraceSpanRecord(
        span_id=str(row["span_id"]),
        run_id=str(row["run_id"]),
        parent_span_id=None if row["parent_span_id"] is None else str(row["parent_span_id"]),
        span_type=str(row["span_type"]),
        name=str(row["name"]),
        status=str(row["status"]),
        metadata=json.loads(str(row["metadata_json"])),
        output=_json_or_none(row["output_json"]),
        error=None if row["error"] is None else str(row["error"]),
        started_at=str(row["started_at"]),
        ended_at=None if row["ended_at"] is None else str(row["ended_at"]),
    )


def _routine_from_row(row: sqlite3.Row) -> RoutineRecord:
    return RoutineRecord(
        routine_id=str(row["routine_id"]),
        name=str(row["name"]),
        prompt=str(row["prompt"]),
        schedule_kind=str(row["schedule_kind"]),
        start_at=str(row["start_at"]),
        interval_seconds=(
            None if row["interval_seconds"] is None else int(row["interval_seconds"])
        ),
        enabled=bool(row["enabled"]),
        revision=int(row["revision"]),
        next_run_at=_optional_str(row["next_run_at"]),
        workspace=_optional_str(row["workspace"]),
        provider=_optional_str(row["provider"]),
        model=_optional_str(row["model"]),
        autonomy_mode=str(row["autonomy_mode"]),
        misfire_grace_seconds=int(row["misfire_grace_seconds"]),
        last_scheduled_at=_optional_str(row["last_scheduled_at"]),
        deleted_at=_optional_str(row["deleted_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _routine_occurrence_from_row(row: sqlite3.Row) -> RoutineOccurrenceRecord:
    return RoutineOccurrenceRecord(
        occurrence_id=str(row["occurrence_id"]),
        routine_id=str(row["routine_id"]),
        routine_revision=int(row["routine_revision"]),
        scheduled_for=str(row["scheduled_for"]),
        status=str(row["status"]),
        run_id=str(row["run_id"]),
        request=_json_or_empty(row["request_json"]),
        trigger_kind=str(row["trigger_kind"]),
        trigger_key_digest=_optional_str(row["trigger_key_digest"]),
        requested_at=_optional_str(row["requested_at"]),
        claim_owner=_optional_str(row["claim_owner"]),
        claim_generation=int(row["claim_generation"]),
        claim_expires_at=_optional_str(row["claim_expires_at"]),
        started_at=_optional_str(row["started_at"]),
        finished_at=_optional_str(row["finished_at"]),
        skip_reason=_optional_str(row["skip_reason"]),
        error=_optional_str(row["error"]),
        result=_json_or_empty(row["result_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def routine_occurrence_id(
    routine_id: str,
    routine_revision: int,
    scheduled_for: str,
) -> str:
    payload = json.dumps(
        [
            _routine_identifier(routine_id),
            _positive_routine_revision(routine_revision),
            _routine_datetime(scheduled_for, "scheduled_for").isoformat(),
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "occ_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def routine_run_id(routine_id: str, occurrence_id: str) -> str:
    payload = json.dumps(
        [_routine_identifier(routine_id), str(occurrence_id)],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "run_routine_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def routine_manual_occurrence_id(routine_id: str, trigger_key_digest: str) -> str:
    payload = json.dumps(
        [_routine_identifier(routine_id), str(trigger_key_digest)],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "occ_manual_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def routine_session_id(routine_id: str) -> str:
    normalized = _routine_identifier(routine_id)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"routine:{digest}"


def _normalize_routine_fields(
    *,
    routine_id: object,
    name: object,
    prompt: object,
    schedule_kind: object,
    start_at: object,
    interval_seconds: object = None,
    enabled: object = False,
    workspace: object = None,
    provider: object = None,
    model: object = None,
    autonomy_mode: object = "background",
    misfire_grace_seconds: object = 60,
) -> dict[str, object]:
    normalized_kind = str(schedule_kind).strip().lower()
    if normalized_kind not in {"once", "interval"}:
        raise ValueError("schedule_kind must be once or interval")
    interval: int | None = None
    if normalized_kind == "interval":
        interval = validate_routine_interval(interval_seconds)
    elif interval_seconds is not None:
        raise ValueError("once routines cannot set interval_seconds")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    misfire_grace = validate_routine_misfire_grace(misfire_grace_seconds)
    normalized_autonomy = str(autonomy_mode).strip().lower()
    if normalized_autonomy not in {"background", "manual", "autonomous"}:
        raise ValueError("autonomy_mode must be background, manual, or autonomous")
    return {
        "routine_id": _routine_identifier(routine_id),
        "name": _secret_safe_required_text(name, "name", 200),
        "prompt": _secret_safe_required_text(prompt, "prompt", 20_000),
        "schedule_kind": normalized_kind,
        "start_at": _routine_datetime(start_at, "start_at").isoformat(),
        "interval_seconds": interval,
        "enabled": enabled,
        "workspace": _secret_safe_optional_text(workspace, "workspace", 4096),
        "provider": _secret_safe_optional_text(provider, "provider", 256),
        "model": _secret_safe_optional_text(model, "model", 256),
        "autonomy_mode": normalized_autonomy,
        "misfire_grace_seconds": misfire_grace,
    }


def _routine_identifier(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("routine_id must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("routine_id must be between 1 and 128 characters")
    if not all(character.isalnum() or character in "._-" for character in normalized):
        raise ValueError(
            "routine_id may contain only letters, numbers, dot, underscore, and hyphen"
        )
    return normalized


def _positive_routine_revision(
    value: object,
    *,
    field_name: str = "routine_revision",
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _routine_trigger_key_digest(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("idempotency_key must be a string")
    normalized = value.strip()
    if not 16 <= len(normalized) <= 128:
        raise ValueError("idempotency_key must be between 16 and 128 characters")
    if not all(character.isalnum() or character in "._:-" for character in normalized):
        raise ValueError(
            "idempotency_key may contain only letters, numbers, dot, underscore, colon, and hyphen"
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _bounded_required_text(value: object, field_name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > limit:
        raise ValueError(f"{field_name} must be at most {limit} characters")
    return normalized


def _bounded_optional_text(value: object, field_name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > limit:
        raise ValueError(f"{field_name} must be at most {limit} characters")
    return normalized


def _secret_safe_required_text(value: object, field_name: str, limit: int) -> str:
    normalized = _bounded_required_text(value, field_name, limit)
    _reject_raw_secret(normalized, field_name)
    return normalized


def _secret_safe_optional_text(
    value: object,
    field_name: str,
    limit: int,
) -> str | None:
    normalized = _bounded_optional_text(value, field_name, limit)
    if normalized is not None:
        _reject_raw_secret(normalized, field_name)
    return normalized


def _reject_raw_secret(value: str, field_name: str) -> None:
    if redact_text(value) != value:
        raise ValueError(f"{field_name} may not contain raw secrets; use secret:// references")


def _routine_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        _reject_raw_secret(raw, field_name)
        try:
            parsed = datetime.fromisoformat(raw)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _require_timestamp(value: object, field_name: str) -> datetime:
    if value is None:
        raise ValueError(f"{field_name} is required")
    return _routine_datetime(value, field_name)


def _routine_request_snapshot(routine: RoutineRecord) -> dict[str, Any]:
    return {
        "schema": "kestrel.routine_request.v1",
        "routine_id": routine.routine_id,
        "routine_revision": routine.revision,
        "prompt": routine.prompt,
        "workspace": routine.workspace,
        "provider": routine.provider,
        "model": routine.model,
        "autonomy_mode": routine.autonomy_mode,
    }


def _next_routine_run_at(
    routine: RoutineRecord,
    *,
    scheduled: datetime,
    now: datetime,
) -> tuple[str | None, int]:
    if routine.schedule_kind == "once":
        return None, 0
    interval = routine.interval_seconds
    if interval is None or interval <= 0:
        raise ValueError("interval routine is missing interval_seconds")
    candidate = scheduled + timedelta(seconds=interval)
    missed = 0
    if candidate <= now:
        missed = int((now - candidate).total_seconds() // interval) + 1
        candidate += timedelta(seconds=missed * interval)
    return candidate.astimezone(UTC).isoformat(), missed


def _manual_occurrence_identity_instant(
    conn: sqlite3.Connection,
    *,
    routine_id: str,
    routine_revision: int,
    requested_at: datetime,
    trigger_key_digest: str,
) -> datetime:
    """Choose a collision-free legacy schedule identity; requested_at stays exact."""

    offset = int(trigger_key_digest[:12], 16) % 1_000_000
    candidate = requested_at + timedelta(microseconds=offset)
    for _ in range(1_024):
        row = conn.execute(
            """
            SELECT 1 FROM routine_occurrences
            WHERE routine_id = ? AND routine_revision = ? AND scheduled_for = ?
            """,
            (routine_id, routine_revision, candidate.isoformat()),
        ).fetchone()
        if row is None:
            return candidate
        candidate += timedelta(microseconds=1)
    raise RuntimeError("routine manual trigger identity space exhausted")


def _routine_dispatch_invalid_reason(
    routine: RoutineRecord,
    *,
    occurrence_revision: int,
) -> str | None:
    if routine.deleted_at is not None:
        return "routine_deleted_before_dispatch"
    if not routine.enabled:
        return "routine_disabled_before_dispatch"
    if routine.revision != occurrence_revision:
        return "routine_changed_before_dispatch"
    return None


def _require_occurrence_row(
    conn: sqlite3.Connection,
    occurrence_id: str,
) -> RoutineOccurrenceRecord:
    row = conn.execute(
        "SELECT * FROM routine_occurrences WHERE occurrence_id = ?",
        (occurrence_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown routine occurrence: {occurrence_id}")
    return _routine_occurrence_from_row(row)


def _skip_occurrence_row(
    conn: sqlite3.Connection,
    occurrence_id: str,
    *,
    instant: datetime,
    reason: str,
) -> None:
    conn.execute(
        """
        UPDATE routine_occurrences SET status = 'skipped', skip_reason = ?,
            claim_owner = NULL, claim_expires_at = NULL, finished_at = ?,
            updated_at = ? WHERE occurrence_id = ? AND status = 'claimed'
        """,
        (reason, instant.isoformat(), instant.isoformat(), occurrence_id),
    )


def _skip_claimed_occurrences_for_routine(
    conn: sqlite3.Connection,
    routine_id: str,
    *,
    instant: datetime,
    reason: str,
) -> None:
    conn.execute(
        """
        UPDATE routine_occurrences SET status = 'skipped', skip_reason = ?,
            claim_owner = NULL, claim_expires_at = NULL, finished_at = ?,
            updated_at = ? WHERE routine_id = ? AND status = 'claimed'
        """,
        (
            reason,
            instant.isoformat(),
            instant.isoformat(),
            routine_id,
        ),
    )


def _routine_occurrence_status(value: str) -> str:
    normalized = str(value).strip().lower()
    allowed = {"claimed", "running", "completed", "failed", "skipped"}
    if normalized not in allowed:
        raise ValueError(f"unsupported routine occurrence status: {normalized}")
    return normalized


def _routine_terminal_occurrence_status(value: str) -> str:
    normalized = _routine_occurrence_status(value)
    if normalized not in {"completed", "failed"}:
        raise ValueError("routine occurrence terminal status must be completed or failed")
    return normalized


def _lease_instant(value: datetime | None) -> datetime:
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("lease timestamps must be timezone-aware")
    return instant.astimezone(UTC)


def _run_execution_lease_matches(
    row: sqlite3.Row,
    *,
    owner: str,
    generation: int | None,
    instant: datetime,
    allowed_statuses: set[str] | None = None,
) -> bool:
    """Return whether a run row carries the exact unexpired execution fence."""

    statuses = allowed_statuses or {"queued", "running"}
    expiry = _parse_timestamp(_optional_str(_row_get(row, "lease_expires_at")))
    return (
        str(row["status"]) in statuses
        and _optional_str(_row_get(row, "lease_owner")) == owner
        and int(str(_row_get(row, "lease_generation", 0) or 0)) == generation
        and expiry is not None
        and expiry > instant
    )


def _positive_ttl(value: float) -> float:
    ttl = float(value)
    if not isfinite(ttl) or ttl <= 0:
        raise ValueError("lease ttl_seconds must be finite and positive")
    return ttl


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _json_or_empty(value: object) -> dict[str, Any]:
    parsed = _json_or_none(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_dict_or_none(value: object) -> dict[str, Any] | None:
    parsed = _json_or_none(value)
    return dict(parsed) if isinstance(parsed, dict) else None


def _json_or_none(value: object) -> Any | None:
    if value is None:
        return None
    return json.loads(str(value))


def _bind_scheduler_approval_continuation(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    approval_id: str,
    tool_call_id: str,
    continuation: dict[str, str],
    now: str,
) -> None:
    task_id = str(continuation.get("task_id") or "").strip()
    subagent_id = str(continuation.get("subagent_id") or "").strip()
    worker_owner = str(continuation.get("worker_owner") or "").strip()
    worker_claim_id = str(continuation.get("worker_claim_id") or "").strip()
    if not all((task_id, subagent_id, worker_owner, worker_claim_id)):
        raise ValueError("scheduler approval continuation identity is incomplete")
    if worker_claim_id != subagent_id:
        raise ValueError("scheduler approval worker claim must match its subagent")

    task_row = conn.execute(
        "SELECT * FROM task_nodes WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    subagent_row = conn.execute(
        "SELECT * FROM subagent_runs WHERE subagent_id = ?",
        (subagent_id,),
    ).fetchone()
    if task_row is None or subagent_row is None:
        raise ValueError("scheduler approval continuation target is missing")
    task = _task_from_row(task_row)
    subagent = _subagent_from_row(subagent_row)
    task_result = dict(task.result or {})
    if (
        task.run_id != run_id
        or task.status != "running"
        or task_result.get("worker_owner") != worker_owner
        or task_result.get("worker_claim_id") != worker_claim_id
        or subagent.run_id != run_id
        or subagent.task_id != task_id
        or subagent.status != "running"
    ):
        raise ValueError("scheduler approval continuation execution fence lost")
    task_result["approval_continuation"] = {
        "approval_id": approval_id,
        "tool_call_id": tool_call_id,
        "task_id": task_id,
        "subagent_id": subagent_id,
        "worker_owner": worker_owner,
        "worker_claim_id": worker_claim_id,
    }
    cursor = conn.execute(
        """
        UPDATE task_nodes SET result_json = ?, updated_at = ?
        WHERE task_id = ? AND run_id = ? AND status = 'running'
        """,
        (_encode(task_result), now, task_id, run_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("scheduler approval continuation bind failed")


def _encode(value: object) -> object:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    return value


def _row_get(row: sqlite3.Row, key: str, default: object = None) -> object:
    return row[key] if key in row.keys() else default


def _task_column(field: str) -> str:
    if field == "plan":
        return "plan_json"
    if field == "result":
        return "result_json"
    if field == "dependencies":
        return "dependencies_json"
    if field == "required_tools":
        return "required_tools_json"
    if field == "acceptance_criteria":
        return "acceptance_criteria_json"
    if field == "diagnosis":
        return "diagnosis_json"
    if field == "retry_strategy":
        return "retry_strategy_json"
    return field
