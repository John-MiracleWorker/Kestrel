from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from time import sleep
from typing import Any

SCHEMA_VERSION = 15
DEFAULT_APPROVAL_TTL_SECONDS = 900.0
CAPABILITY_KINDS = frozenset({"tool", "mcp_server", "skill"})
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


class AgentStateStore:
    """SQLite control-plane state for runs, approvals, capabilities, and extensions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with _SCHEMA_MIGRATION_LOCK:
            self._migrate_schema()
            self._enable_wal_mode()

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
                    config_revision, config_snapshot_json, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, '', 0, 0, '', NULL, ?, ?, ?, ?)
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
                    now,
                    now,
                ),
            )
        return self.get_run(run_id)

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
            if current.lease_owner and current.lease_owner != owner and current_expiry and current_expiry > instant:
                return None
            generation = current.lease_generation
            if current.lease_owner != owner or current_expiry is None or current_expiry <= instant:
                generation += 1
            conn.execute(
                """
                UPDATE runs SET lease_owner = ?, lease_generation = ?, lease_expires_at = ?,
                    heartbeat_at = ?, updated_at = ? WHERE run_id = ?
                """,
                (owner, generation, expires_at.isoformat(), instant.isoformat(), instant.isoformat(), run_id),
            )
        return self.get_run(run_id)

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
                (expires_at.isoformat(), instant.isoformat(), instant.isoformat(), run_id, owner, generation, instant.isoformat()),
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

    def list_run_steps(self, run_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
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
    ) -> tuple[dict[str, Any], bool]:
        """Create one live approval per run.

        Concurrent retries for the same exact call reuse the existing pending
        record without extending its decision window. A different call cannot
        create an ambiguous second continuation while that record is pending.
        """

        now = utc_now()
        expiry = expires_at or (
            datetime.now(UTC) + timedelta(seconds=DEFAULT_APPROVAL_TTL_SECONDS)
        ).isoformat()
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
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sql = (
                "SELECT approval_id FROM approval_requests "
                "WHERE status = 'pending' AND (expires_at IS NULL "
                "OR julianday(expires_at) IS NULL "
                "OR julianday(expires_at) <= julianday(?))"
            )
            params: list[object] = [now]
            if approval_id is not None:
                sql += " AND approval_id = ?"
                params.append(approval_id)
            rows = conn.execute(sql, tuple(params)).fetchall()
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
            current = conn.execute(
                "SELECT status, result_json FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            if str(current["status"]) == "pending":
                return self.get_approval(approval_id)
            if current["result_json"] is not None:
                return self.get_approval(approval_id)
            conn.execute(
                """
                UPDATE approval_requests
                SET result_json = ?, updated_at = ?
                WHERE approval_id = ?
                """,
                (json.dumps(result), utc_now(), approval_id),
            )
        return self.get_approval(approval_id)

    def upsert_mcp_server(self, server: dict[str, Any]) -> dict[str, Any]:
        server_id = str(server["id"])
        now = utc_now()
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
            "updated_at": now,
        }
        with self._connect() as conn:
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
        now = utc_now()
        created_at = str(plugin.get("created_at") or now)
        with self._connect() as conn:
            current = conn.execute("SELECT created_at FROM plugin_registry WHERE id = ?", (plugin_id,)).fetchone()
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
        return self.get_plugin(plugin_id)

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM plugin_registry WHERE id = ?", (plugin_id,)).fetchone()
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

    def update_task_node(self, task_id: str, **fields: object) -> TaskNodeRecord:
        if not fields:
            return self.get_task_node(task_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{_validated_column('task_nodes', _task_column(key))} = ?" for key in fields)
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
            task_row = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            task = _task_from_row(task_row)
            if task.run_id != run_id or task.status not in {"queued", "approved"}:
                return None
            cursor = conn.execute(
                """
                UPDATE task_nodes SET approved = 1, status = 'approved', updated_at = ?
                WHERE task_id = ? AND run_id = ? AND status IN ('queued', 'approved')
                """,
                (now, task_id, run_id),
            )
            if cursor.rowcount != 1:
                return None
            updated = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
            return _task_from_row(updated) if updated is not None else None

    def claim_task_node(
        self,
        task_id: str,
        *,
        run_id: str,
        worker_owner: str,
        worker_claim_id: str,
        heartbeat_at: str | None = None,
    ) -> TaskNodeRecord | None:
        """Atomically claim one approved ready task for exactly one worker execution."""

        owner = worker_owner.strip()
        claim_id = worker_claim_id.strip()
        if not owner or not claim_id:
            raise ValueError("worker owner and claim id are required")
        now = heartbeat_at or utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run_row["status"]) in _TERMINAL_RUN_STATUSES:
                return None
            task_row = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            task = _task_from_row(task_row)
            if task.run_id != run_id or not task.approved or task.status not in {"queued", "approved"}:
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
            updated = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
            return _task_from_row(updated) if updated is not None else None

    def task_claim_matches(
        self,
        task_id: str,
        *,
        run_id: str,
        worker_owner: str,
        worker_claim_id: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        task = _task_from_row(row)
        result = task.result or {}
        return (
            task.run_id == run_id
            and task.status == "running"
            and result.get("worker_owner") == worker_owner
            and result.get("worker_claim_id") == worker_claim_id
        )

    def heartbeat_task_claim(
        self,
        task_id: str,
        *,
        run_id: str,
        worker_owner: str,
        worker_claim_id: str,
        heartbeat_at: str | None = None,
    ) -> bool:
        """Refresh a task heartbeat only if the exact execution claim is still active."""

        now = heartbeat_at or utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run_row["status"]) in _ABORTED_RUN_STATUSES:
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
        **fields: object,
    ) -> tuple[TaskNodeRecord, bool]:
        """Finish an exact task execution claim without allowing stale workers to overwrite state."""

        if status not in {"blocked", "completed", "failed", "cancelled", "skipped"}:
            raise ValueError(f"unsupported claimed task transition: {status}")
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
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
            if str(run_row["status"]) in _ABORTED_RUN_STATUSES:
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
            updated = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
            if updated is None:
                raise KeyError(f"Unknown task: {task_id}")
            return _task_from_row(updated), cursor.rowcount == 1

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
    ) -> SubagentRunRecord | None:
        """Persist a worker only while its run and exact task claim are still active."""

        if status not in {"queued", "running"}:
            raise ValueError(f"unsupported initial subagent status: {status}")
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run_row["status"]) in _TERMINAL_RUN_STATUSES:
                return None
            task_row = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
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
        assignments = ", ".join(
            f"{_validated_column('subagent_runs', key)} = ?" for key in updates
        )
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
            row = conn.execute("SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)).fetchone()
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
            if current < SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported schema migration target: {current} -> {SCHEMA_VERSION}")
            if current == SCHEMA_VERSION:
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
                with sqlite3.connect(self.path, timeout=5.0) as conn:
                    conn.execute("PRAGMA busy_timeout=5000")
                    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if mode is not None and str(mode[0]).lower() == "wal":
                    return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 9:
                    raise
                sleep(0.05 * (attempt + 1))
        raise RuntimeError("Unable to enable SQLite WAL journal mode.")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        _apply_connection_pragmas(conn)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _apply_schema_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
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

    conn.executescript(
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
    conn.executescript(
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
    conn.executescript(
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
    conn.executescript(
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
    conn.executescript(
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
    conn.execute(
        "UPDATE approval_requests SET expires_at = created_at WHERE expires_at IS NULL"
    )
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
            "ALTER TABLE approval_requests "
            "ADD COLUMN resource_digest TEXT NOT NULL DEFAULT ''"
        )
    conn.executescript(
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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")


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
        lease_owner=None if _row_get(row, "lease_owner") is None else str(_row_get(row, "lease_owner")),
        lease_generation=int(str(_row_get(row, "lease_generation", 0) or 0)),
        lease_expires_at=None
        if _row_get(row, "lease_expires_at") is None
        else str(_row_get(row, "lease_expires_at")),
        heartbeat_at=None if _row_get(row, "heartbeat_at") is None else str(_row_get(row, "heartbeat_at")),
        interrupted_at=None
        if _row_get(row, "interrupted_at") is None
        else str(_row_get(row, "interrupted_at")),
        recovery_reason=str(_row_get(row, "recovery_reason", "") or ""),
        config_revision=_optional_str(_row_get(row, "config_revision")),
        config_snapshot=_json_or_empty(_row_get(row, "config_snapshot_json", "{}")),
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
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


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
        "risk_policy": str(_row_get(row, "risk_policy", "approval_by_default") or "approval_by_default"),
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
        acceptance_criteria=tuple(json.loads(str(_row_get(row, "acceptance_criteria_json", "[]") or "[]"))),
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


def _lease_instant(value: datetime | None) -> datetime:
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("lease timestamps must be timezone-aware")
    return instant.astimezone(UTC)


def _positive_ttl(value: float) -> float:
    ttl = float(value)
    if ttl <= 0:
        raise ValueError("lease ttl_seconds must be positive")
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


def _json_or_none(value: object) -> Any | None:
    if value is None:
        return None
    return json.loads(str(value))


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
