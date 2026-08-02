"""Revision-checked SQLite ledger for explicit private-LAN scans."""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from time import monotonic
from typing import Any

from ..state_store import AgentStateStore, utc_now
from .lan_records import (
    ALLOWED_SCAN_TRANSITIONS,
    SCAN_STATES,
    TERMINAL_SCAN_STATES,
    LanObservationDraft,
    LanObservationRecord,
    LanScanEvent,
    LanScanRecord,
    LanScanRevisionConflict,
    LanScanTransitionError,
)
from .lan_serialization import (
    LAN_CANCEL_REASONS,
    LAN_MDNS_STATUSES,
    LAN_SCAN_PROGRESS_EVENT_SCHEMA,
    LAN_SCAN_RECEIPT_V2_SCHEMA,
    LAN_SCAN_TERMINAL_EVENT_SCHEMA,
    LAN_TERMINAL_REASONS,
    MAX_LAN_SCAN_OBSERVATIONS,
    MAX_RECEIPT_BYTES,
    bounded_error_category_counts,
    bounded_event_public_evidence,
    bounded_scan_limits,
    bounded_scan_preview_event,
    bounded_scan_progress_event,
    canonical_json,
    event_from_row,
    normalize_network,
    observation_from_row,
    observation_membership_digest,
    scan_from_row,
    sha256_digest,
    validate_digest,
    validate_exact_revision,
    validate_non_negative_count,
    validate_observation,
    validate_optional_text,
    validate_required_text,
)
from .ledger_schema import ROUTING_SCHEMA_VERSION, ensure_routing_schema

_TIMEOUT_ERROR_CATEGORIES = frozenset({"tcp_timeout", "http_timeout", "scan_deadline_exceeded"})


def _validate_exact_owner_principal(value: object) -> str:
    owner = validate_required_text(value, "owner_principal", maximum=256)
    if type(value) is not str or owner != value:
        raise ValueError("LAN owner principal must be exact")
    return owner


@dataclass(frozen=True)
class LanScanObservationPage:
    """One owner-qualified, revision-consistent scan detail snapshot."""

    scan: LanScanRecord
    observations: tuple[LanObservationRecord, ...]
    total_count: int
    truncated: bool


class LanDiscoveryLedger:
    """Own durable scan receipts without performing any network discovery."""

    def __init__(
        self,
        state: AgentStateStore,
        *,
        precommit_hook: Callable[[str], None] | None = None,
        utc_clock: Callable[[], datetime] | None = None,
        _validate_initialized_schema: bool = False,
    ) -> None:
        self.state = state
        self._precommit_hook = precommit_hook
        self._utc_clock = utc_clock
        if _validate_initialized_schema:
            self._validate_initialized_schema()
        else:
            ensure_routing_schema(state)

    @classmethod
    def from_initialized_state(
        cls,
        state: AgentStateStore,
        *,
        precommit_hook: Callable[[str], None] | None = None,
        utc_clock: Callable[[], datetime] | None = None,
    ) -> LanDiscoveryLedger:
        """Build against exact v3 state using validation-only schema access."""

        return cls(
            state,
            precommit_hook=precommit_hook,
            utc_clock=utc_clock,
            _validate_initialized_schema=True,
        )

    def _validate_initialized_schema(self) -> None:
        try:
            with self.state._connect() as connection:
                row = connection.execute(
                    "SELECT version FROM routing_schema_version WHERE id = 1"
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("Routing schema 3 is required.") from exc
        if row is None or int(row["version"]) != ROUTING_SCHEMA_VERSION:
            raise RuntimeError("Routing schema 3 is required.")

    def create_scan(
        self,
        *,
        scan_id: str,
        owner_principal: str,
        confirmed_interface_id: str,
        network: str,
        limits: dict[str, object],
        preview_digest: str,
        expected_revision: int,
    ) -> LanScanRecord:
        normalized_scan_id = validate_required_text(scan_id, "scan_id", maximum=128)
        normalized_owner = _validate_exact_owner_principal(owner_principal)
        normalized_interface = validate_digest(
            confirmed_interface_id,
            "confirmed_interface_id",
        )
        normalized_preview = validate_digest(preview_digest, "preview_digest")
        normalized_network = normalize_network(network)
        normalized_limits = bounded_scan_limits(limits)
        limits_json = canonical_json(normalized_limits)
        limits_digest = sha256_digest(normalized_limits)
        now = self._now()
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                (normalized_scan_id,),
            ).fetchone()
            if existing is not None:
                raise LanScanRevisionConflict(
                    normalized_scan_id,
                    int(existing["revision"]),
                )
            if isinstance(expected_revision, bool) or expected_revision != 0:
                raise LanScanRevisionConflict(normalized_scan_id, 0)
            connection.execute(
                """
                INSERT INTO routing_lan_scans (
                    scan_id, status, revision, owner_principal,
                    confirmed_interface_id, network, limits_json, limits_digest,
                    preview_digest, created_at, updated_at, started_at, finished_at,
                    cancel_reason, terminal_reason, candidate_count, error_count,
                    timeout_count, terminal_receipt_json, terminal_receipt_digest
                ) VALUES (?, 'draft', 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                          NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    normalized_scan_id,
                    normalized_owner,
                    normalized_interface,
                    normalized_network,
                    limits_json,
                    limits_digest,
                    normalized_preview,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                (normalized_scan_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("lan_scan_write_lost")
        return scan_from_row(row)

    def get_scan(self, scan_id: str) -> LanScanRecord | None:
        with self.state._connect() as connection:
            row = connection.execute(
                "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()
        return None if row is None else scan_from_row(row)

    def list_scans(
        self,
        *,
        status: str | None = None,
        owner_principal: str | None = None,
        limit: int = 200,
    ) -> list[LanScanRecord]:
        if status is not None and status not in SCAN_STATES:
            raise ValueError("unknown LAN scan status")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("LAN scan list limit must be between 1 and 1000")
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if owner_principal is not None:
            clauses.append("owner_principal = ?")
            params.append(owner_principal)
        sql = "SELECT * FROM routing_lan_scans"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, scan_id ASC LIMIT ?"
        params.append(limit)
        with self.state._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [scan_from_row(row) for row in rows]

    def append_observation(
        self,
        scan_id: str,
        observation: LanObservationDraft,
        *,
        expected_revision: int | None = None,
    ) -> LanObservationRecord:
        values = validate_observation(observation)
        now = self._now()
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = self._mutable_scan(
                connection,
                scan_id,
                expected_revision=expected_revision,
            )
            if values["interface_id"] != scan.confirmed_interface_id:
                raise ValueError("observation interface does not match confirmed scan interface")
            address = ipaddress.ip_address(str(values["address"]))
            if address not in ipaddress.ip_network(scan.network, strict=True):
                raise ValueError("observation address does not belong to confirmed scan network")
            try:
                connection.execute(
                    """
                    INSERT INTO routing_lan_observations (
                        scan_id, endpoint_id, source, interface_id, address, port,
                        api_shape, tls_enabled, certificate_sha256, catalog_digest,
                        capability_digest, public_payload_json, freshness_timestamp,
                        error_category, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan.scan_id,
                        values["endpoint_id"],
                        values["source"],
                        values["interface_id"],
                        values["address"],
                        values["port"],
                        values["api_shape"],
                        1 if values["tls_enabled"] else 0,
                        values["certificate_sha256"],
                        values["catalog_digest"],
                        values["capability_digest"],
                        canonical_json(values["public_payload"]),
                        values["freshness_timestamp"],
                        values["error_category"],
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ValueError(
                        f"lan_observation_exists:{scan.scan_id}:{values['endpoint_id']}"
                    ) from exc
                raise
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET revision = ?, updated_at = ?
                WHERE scan_id = ? AND revision = ?
                """,
                (scan.revision + 1, now, scan.scan_id, scan.revision),
            )
            row = connection.execute(
                """
                SELECT * FROM routing_lan_observations
                WHERE scan_id = ? AND endpoint_id = ?
                """,
                (scan.scan_id, values["endpoint_id"]),
            ).fetchone()
        if row is None:
            raise RuntimeError("lan_observation_write_lost")
        return observation_from_row(row)

    def list_observations(self, scan_id: str) -> list[LanObservationRecord]:
        with self.state._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM routing_lan_observations
                WHERE scan_id = ? ORDER BY endpoint_id ASC
                """,
                (scan_id,),
            ).fetchall()
        return [observation_from_row(row) for row in rows]

    def read_scan_observation_page(
        self,
        scan_id: str,
        *,
        owner_principal: str,
        limit: int,
    ) -> LanScanObservationPage | None:
        """Read scan metadata, observation count, and one ordered page atomically."""

        owner = _validate_exact_owner_principal(owner_principal)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("LAN observation page limit must be between 1 and 200")
        with self.state._connect() as connection:
            # A deferred read transaction fixes the WAL snapshot at the first
            # SELECT while still allowing concurrent writers to commit.
            connection.execute("BEGIN")
            scan_row = connection.execute(
                """
                SELECT * FROM routing_lan_scans
                WHERE scan_id = ? AND owner_principal = ?
                """,
                (scan_id, owner),
            ).fetchone()
            if scan_row is None:
                return None
            count_row = connection.execute(
                """
                SELECT COUNT(*)
                FROM routing_lan_observations AS observation
                INNER JOIN routing_lan_scans AS scan
                    ON scan.scan_id = observation.scan_id
                WHERE scan.scan_id = ? AND scan.owner_principal = ?
                """,
                (scan_id, owner),
            ).fetchone()
            if count_row is None:
                raise RuntimeError("lan_observation_page_count_lost")
            rows = connection.execute(
                """
                SELECT observation.*
                FROM routing_lan_observations AS observation
                INNER JOIN routing_lan_scans AS scan
                    ON scan.scan_id = observation.scan_id
                WHERE scan.scan_id = ? AND scan.owner_principal = ?
                ORDER BY observation.endpoint_id ASC
                LIMIT ?
                """,
                (scan_id, owner, limit),
            ).fetchall()
        total_count = int(count_row[0])
        observations = tuple(observation_from_row(row) for row in rows)
        return LanScanObservationPage(
            scan=scan_from_row(scan_row),
            observations=observations,
            total_count=total_count,
            truncated=total_count > len(observations),
        )

    def append_event(
        self,
        scan_id: str,
        event_type: str,
        payload: dict[str, object],
        *,
        expected_revision: int | None = None,
    ) -> LanScanEvent:
        normalized_type = validate_required_text(event_type, "event_type", maximum=128)
        normalized_payload = bounded_event_public_evidence(payload)
        now = self._now()
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = self._mutable_scan(
                connection,
                scan_id,
                expected_revision=expected_revision,
            )
            sequence_row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                FROM routing_lan_scan_events WHERE scan_id = ?
                """,
                (scan.scan_id,),
            ).fetchone()
            if sequence_row is None:
                raise RuntimeError("lan_event_sequence_unavailable")
            sequence = int(sequence_row["next_sequence"])
            connection.execute(
                """
                INSERT INTO routing_lan_scan_events (
                    scan_id, sequence, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scan.scan_id,
                    sequence,
                    normalized_type,
                    canonical_json(normalized_payload),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET revision = ?, updated_at = ?
                WHERE scan_id = ? AND revision = ?
                """,
                (scan.revision + 1, now, scan.scan_id, scan.revision),
            )
            row = connection.execute(
                """
                SELECT * FROM routing_lan_scan_events
                WHERE scan_id = ? AND sequence = ?
                """,
                (scan.scan_id, sequence),
            ).fetchone()
        if row is None:
            raise RuntimeError("lan_event_write_lost")
        return event_from_row(row)

    def list_events(
        self,
        scan_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[LanScanEvent]:
        if isinstance(after_sequence, bool) or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if isinstance(limit, bool) or not 1 <= limit <= 2_000:
            raise ValueError("LAN event list limit must be between 1 and 2000")
        with self.state._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM routing_lan_scan_events
                WHERE scan_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (scan_id, after_sequence, limit),
            ).fetchall()
        return [event_from_row(row) for row in rows]

    def transition_scan(
        self,
        scan_id: str,
        new_status: str,
        *,
        expected_revision: int,
        cancel_reason: str | None = None,
        terminal_reason: str | None = None,
        candidate_count: int | None = None,
        error_count: int | None = None,
        timeout_count: int | None = None,
    ) -> LanScanRecord:
        if new_status not in SCAN_STATES:
            raise ValueError("unknown LAN scan status")
        normalized_cancel_reason = validate_optional_text(
            cancel_reason,
            "cancel_reason",
            maximum=512,
        )
        normalized_terminal_reason = validate_optional_text(
            terminal_reason,
            "terminal_reason",
            maximum=512,
        )
        now = self._now()
        finished_at: str | None
        candidates: int | None
        errors: int | None
        timeouts: int | None
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = self._mutable_scan(
                connection,
                scan_id,
                expected_revision=expected_revision,
            )
            if new_status not in ALLOWED_SCAN_TRANSITIONS[scan.status]:
                raise LanScanTransitionError(scan.scan_id, scan.status, new_status)

            terminal = new_status in TERMINAL_SCAN_STATES
            if terminal:
                if candidate_count is None or error_count is None or timeout_count is None:
                    raise ValueError("terminal LAN scans require all receipt counts")
                candidates = validate_non_negative_count(candidate_count, "candidate_count")
                errors = validate_non_negative_count(error_count, "error_count")
                timeouts = validate_non_negative_count(timeout_count, "timeout_count")
                if timeouts > errors:
                    raise ValueError("timeout_count cannot exceed error_count")
                effective_cancel_reason = normalized_cancel_reason or scan.cancel_reason
                effective_terminal_reason = normalized_terminal_reason
                if effective_terminal_reason is None and new_status == "cancelled":
                    effective_terminal_reason = effective_cancel_reason or "cancelled"
                if effective_terminal_reason is None:
                    raise ValueError("terminal_reason is required for a terminal LAN scan")
                finished_at = now
                started_at = scan.started_at
                receipt = self._terminal_receipt(
                    connection,
                    scan=scan,
                    status=new_status,
                    started_at=started_at,
                    finished_at=now,
                    cancel_reason=effective_cancel_reason,
                    terminal_reason=effective_terminal_reason,
                    candidate_count=candidates,
                    error_count=errors,
                    timeout_count=timeouts,
                )
                receipt_json = canonical_json(receipt)
                if len(receipt_json.encode("utf-8")) > MAX_RECEIPT_BYTES:
                    raise ValueError("terminal LAN receipt exceeds bounded storage")
                receipt_digest = sha256_digest(receipt)
            else:
                effective_cancel_reason = normalized_cancel_reason or scan.cancel_reason
                effective_terminal_reason = None
                if any(
                    value is not None
                    for value in (
                        terminal_reason,
                        candidate_count,
                        error_count,
                        timeout_count,
                    )
                ):
                    raise ValueError("terminal fields require a terminal LAN scan status")
                if normalized_cancel_reason is not None and new_status != "cancelling":
                    raise ValueError("cancel_reason requires cancelling or cancelled status")
                started_at = now if new_status == "running" else scan.started_at
                finished_at = None
                receipt_json = None
                receipt_digest = None
                candidates = errors = timeouts = None

            connection.execute(
                """
                UPDATE routing_lan_scans
                SET status = ?, revision = ?, updated_at = ?, started_at = ?,
                    finished_at = ?, cancel_reason = ?, terminal_reason = ?,
                    candidate_count = ?, error_count = ?, timeout_count = ?,
                    terminal_receipt_json = ?, terminal_receipt_digest = ?
                WHERE scan_id = ? AND revision = ?
                """,
                (
                    new_status,
                    scan.revision + 1,
                    now,
                    started_at,
                    finished_at,
                    effective_cancel_reason,
                    effective_terminal_reason,
                    candidates,
                    errors,
                    timeouts,
                    receipt_json,
                    receipt_digest,
                    scan.scan_id,
                    scan.revision,
                ),
            )
            row = connection.execute(
                "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                (scan.scan_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("lan_scan_transition_write_lost")
        return scan_from_row(row)

    def claim_scan_start(
        self,
        scan_id: str,
        *,
        owner_principal: str,
        expected_revision: int,
        preview_digest: str,
        authorized_preview_digest: str,
        preview_event: dict[str, object],
    ) -> LanScanRecord:
        owner = _validate_exact_owner_principal(owner_principal)
        requested_digest = validate_digest(preview_digest, "preview_digest")
        authorized_digest = validate_digest(
            authorized_preview_digest,
            "authorized_preview_digest",
        )
        event_payload = bounded_scan_preview_event(preview_event)
        if requested_digest != authorized_digest:
            raise ValueError("LAN preview authorization digest does not match request")
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = self._owned_mutable_scan(
                connection,
                scan_id,
                owner_principal=owner,
                expected_revision=expected_revision,
            )
            if scan.status != "draft":
                raise LanScanTransitionError(scan.scan_id, scan.status, "running")
            if scan.preview_digest != requested_digest:
                raise ValueError("LAN preview digest does not match durable draft")
            if (
                event_payload["owner_principal"] != scan.owner_principal
                or event_payload["interface_id"] != scan.confirmed_interface_id
                or event_payload["network"] != scan.network
                or event_payload["limits"] != scan.limits
                or event_payload["preview_digest"] != scan.preview_digest
            ):
                raise ValueError("LAN preview event does not match durable draft")
            active = connection.execute(
                """
                SELECT scan_id FROM routing_lan_scans
                WHERE owner_principal = ?
                  AND status IN ('running', 'cancelling')
                  AND scan_id <> ?
                LIMIT 1
                """,
                (owner, scan.scan_id),
            ).fetchone()
            if active is not None:
                raise RuntimeError("lan_scan_owner_already_active")
            now = self._now()
            self._require_live_preview_event(event_payload, now)
            self._insert_event_txn(
                connection,
                scan_id=scan.scan_id,
                event_type="scan_started",
                payload=event_payload,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET status = 'running', revision = ?, updated_at = ?, started_at = ?
                WHERE scan_id = ? AND revision = ?
                """,
                (scan.revision + 1, now, now, scan.scan_id, scan.revision),
            )
            self._before_commit("claim_scan_start")
            self._require_live_preview_event(event_payload, self._now())
            row = connection.execute(
                "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                (scan.scan_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("lan_scan_start_write_lost")
        return scan_from_row(row)

    def request_scan_cancel(
        self,
        scan_id: str,
        *,
        owner_principal: str,
        expected_revision: int,
        cancel_reason: str,
    ) -> LanScanRecord:
        owner = _validate_exact_owner_principal(owner_principal)
        if type(cancel_reason) is not str or cancel_reason not in LAN_CANCEL_REASONS:
            raise ValueError("LAN cancel reason is invalid")
        now = self._now()
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = self._owned_mutable_scan(
                connection,
                scan_id,
                owner_principal=owner,
                expected_revision=expected_revision,
            )
            if scan.status not in {"draft", "running"}:
                raise LanScanTransitionError(scan.scan_id, scan.status, "cancelling")
            if scan.status == "running":
                self._insert_event_txn(
                    connection,
                    scan_id=scan.scan_id,
                    event_type="scan_cancel_requested",
                    payload={"reason": cancel_reason},
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE routing_lan_scans
                    SET status = 'cancelling', revision = ?, updated_at = ?,
                        cancel_reason = ?
                    WHERE scan_id = ? AND revision = ?
                    """,
                    (
                        scan.revision + 1,
                        now,
                        cancel_reason,
                        scan.scan_id,
                        scan.revision,
                    ),
                )
            else:
                terminal_reason = cancel_reason
                observations: list[LanObservationRecord] = []
                receipt = self._terminal_receipt_v2(
                    scan=scan,
                    status="cancelled",
                    started_at=None,
                    finished_at=now,
                    cancel_reason=cancel_reason,
                    terminal_reason=terminal_reason,
                    mdns_status="unavailable",
                    planned_count=0,
                    admitted_count=0,
                    completed_count=0,
                    observations=observations,
                    error_category_counts={},
                    timeout_count=0,
                    evidence_complete=True,
                    unknown_inflight_count=0,
                )
                receipt_json = canonical_json(receipt)
                self._require_bounded_receipt(receipt_json)
                self._insert_event_txn(
                    connection,
                    scan_id=scan.scan_id,
                    event_type="scan_cancelled",
                    payload=self._terminal_event_payload(
                        status="cancelled",
                        terminal_reason=terminal_reason,
                        cancel_reason=cancel_reason,
                    ),
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE routing_lan_scans
                    SET status = 'cancelled', revision = ?, updated_at = ?,
                        finished_at = ?, cancel_reason = ?, terminal_reason = ?,
                        candidate_count = 0, error_count = 0, timeout_count = 0,
                        terminal_receipt_json = ?, terminal_receipt_digest = ?
                    WHERE scan_id = ? AND revision = ?
                    """,
                    (
                        scan.revision + 1,
                        now,
                        now,
                        cancel_reason,
                        terminal_reason,
                        receipt_json,
                        sha256_digest(receipt),
                        scan.scan_id,
                        scan.revision,
                    ),
                )
            self._before_commit("request_scan_cancel")
            row = connection.execute(
                "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                (scan.scan_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("lan_scan_cancel_write_lost")
        return scan_from_row(row)

    def record_scan_progress(
        self,
        scan_id: str,
        *,
        owner_principal: str,
        expected_revision: int,
        planned_count: int,
        admitted_count: int,
        completed_count: int,
        persisted_observation_count: int,
        error_category_counts: dict[str, int],
        timeout_count: int,
        mdns_status: str,
        observations: tuple[LanObservationDraft, ...] = (),
        absolute_deadline: float | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> LanScanRecord:
        owner = _validate_exact_owner_principal(owner_principal)
        payload = bounded_scan_progress_event(
            {
                "schema": LAN_SCAN_PROGRESS_EVENT_SCHEMA,
                "planned_count": planned_count,
                "admitted_count": admitted_count,
                "completed_count": completed_count,
                "persisted_observation_count": persisted_observation_count,
                "error_category_counts": error_category_counts,
                "timeout_count": timeout_count,
                "mdns_status": mdns_status,
            }
        )
        self._check_deadline(absolute_deadline, monotonic_clock)
        now = self._now()
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = self._owned_mutable_scan(
                connection,
                scan_id,
                owner_principal=owner,
                expected_revision=expected_revision,
            )
            if scan.status != "running":
                raise LanScanTransitionError(scan.scan_id, scan.status, "running")
            previous = self._last_progress_payload(connection, scan.scan_id)
            if previous is not None:
                self._require_monotonic_progress(previous, payload)
            inserted = 0
            for observation in observations:
                inserted += int(
                    self._insert_or_validate_observation_txn(
                        connection,
                        scan=scan,
                        observation=observation,
                        created_at=now,
                    )
                )
            durable_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM routing_lan_observations WHERE scan_id = ?",
                    (scan.scan_id,),
                ).fetchone()[0]
            )
            if durable_count != payload["persisted_observation_count"]:
                raise ValueError("persisted observation count does not match durable rows")
            if durable_count > MAX_LAN_SCAN_OBSERVATIONS:
                raise ValueError("LAN scan observation count exceeds the fixed ceiling")
            durable_errors, durable_timeouts = self._durable_error_counts(
                connection,
                scan.scan_id,
            )
            if (
                durable_errors != payload["error_category_counts"]
                or durable_timeouts != payload["timeout_count"]
            ):
                raise ValueError("LAN scan error counts do not match durable observations")
            if previous == payload and inserted == 0:
                return scan
            self._insert_event_txn(
                connection,
                scan_id=scan.scan_id,
                event_type="scan_progress",
                payload=payload,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET revision = ?, updated_at = ?
                WHERE scan_id = ? AND revision = ?
                """,
                (scan.revision + 1, now, scan.scan_id, scan.revision),
            )
            self._check_deadline(absolute_deadline, monotonic_clock)
            self._before_commit("record_scan_progress")
            self._check_deadline(absolute_deadline, monotonic_clock)
            row = connection.execute(
                "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                (scan.scan_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("lan_scan_progress_write_lost")
        return scan_from_row(row)

    def commit_scan_terminal(
        self,
        scan_id: str,
        *,
        owner_principal: str,
        expected_revision: int,
        status: str,
        terminal_reason: str,
        cancel_reason: str | None,
        observations: tuple[LanObservationDraft, ...],
        mdns_status: str,
        planned_count: int,
        admitted_count: int,
        completed_count: int,
        error_category_counts: dict[str, int],
        timeout_count: int,
        evidence_complete: bool,
        unknown_inflight_count: int | None,
        absolute_deadline: float | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> LanScanRecord:
        owner = _validate_exact_owner_principal(owner_principal)
        if type(status) is not str or status not in {"completed", "cancelled", "failed"}:
            raise ValueError("LAN terminal status is invalid")
        if type(terminal_reason) is not str or terminal_reason not in LAN_TERMINAL_REASONS:
            raise ValueError("LAN terminal reason is invalid")
        if cancel_reason is not None and (
            type(cancel_reason) is not str or cancel_reason not in LAN_CANCEL_REASONS
        ):
            raise ValueError("LAN cancel reason is invalid")
        if type(evidence_complete) is not bool or not evidence_complete:
            raise ValueError("ordinary LAN terminal evidence must be complete")
        if (
            type(unknown_inflight_count) is not int
            or unknown_inflight_count < 0
            or unknown_inflight_count != 0
        ):
            raise ValueError("ordinary LAN terminal in-flight count must be exact zero")
        if type(mdns_status) is not str or mdns_status not in LAN_MDNS_STATUSES:
            raise ValueError("LAN mDNS status is invalid")
        errors = bounded_error_category_counts(error_category_counts)
        if type(observations) is not tuple or len(observations) > MAX_LAN_SCAN_OBSERVATIONS:
            raise ValueError("LAN scan observations exceed the fixed ceiling")
        if status == "completed" and terminal_reason != "scan_complete":
            raise ValueError("completed LAN scans require scan_complete")
        if status == "cancelled" and (cancel_reason is None or terminal_reason != cancel_reason):
            raise ValueError("cancelled LAN scan reason is inconsistent")
        if status == "failed" and terminal_reason not in {"worker_error", "deadline_expired"}:
            raise ValueError("failed LAN scan reason is invalid")
        self._check_deadline(absolute_deadline, monotonic_clock)
        now = self._now()
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = self._owned_mutable_scan(
                connection,
                scan_id,
                owner_principal=owner,
                expected_revision=expected_revision,
            )
            legal = (scan.status == "running" and status in {"completed", "failed"}) or (
                scan.status == "cancelling" and status == "cancelled"
            )
            if not legal:
                raise LanScanTransitionError(scan.scan_id, scan.status, status)
            effective_cancel = cancel_reason or scan.cancel_reason
            if status == "cancelled" and effective_cancel != terminal_reason:
                raise ValueError("cancelled LAN scan reason changed after request")
            for observation in observations:
                self._insert_or_validate_observation_txn(
                    connection,
                    scan=scan,
                    observation=observation,
                    created_at=now,
                )
            rows = connection.execute(
                """
                SELECT * FROM routing_lan_observations
                WHERE scan_id = ? ORDER BY endpoint_id ASC
                """,
                (scan.scan_id,),
            ).fetchall()
            durable_observations = [observation_from_row(row) for row in rows]
            if len(durable_observations) > MAX_LAN_SCAN_OBSERVATIONS:
                raise ValueError("LAN scan observation count exceeds the fixed ceiling")
            durable_errors, durable_timeouts = self._durable_error_counts(
                connection,
                scan.scan_id,
            )
            if errors != durable_errors or timeout_count != durable_timeouts:
                raise ValueError("LAN scan error counts do not match durable observations")
            progress = bounded_scan_progress_event(
                {
                    "schema": LAN_SCAN_PROGRESS_EVENT_SCHEMA,
                    "planned_count": planned_count,
                    "admitted_count": admitted_count,
                    "completed_count": completed_count,
                    "persisted_observation_count": len(durable_observations),
                    "error_category_counts": errors,
                    "timeout_count": timeout_count,
                    "mdns_status": mdns_status,
                }
            )
            previous = self._last_progress_payload(connection, scan.scan_id)
            if previous is not None:
                self._require_monotonic_progress(previous, progress)
            if progress["completed_count"] != progress["admitted_count"]:
                raise ValueError("terminal LAN scan has unsettled admitted work")
            if len(durable_observations) != progress["completed_count"]:
                raise ValueError(
                    "terminal LAN scan complete evidence does not match completed work"
                )
            event_type = {
                "completed": "scan_completed",
                "cancelled": "scan_cancelled",
                "failed": "scan_failed",
            }[status]
            self._insert_event_txn(
                connection,
                scan_id=scan.scan_id,
                event_type=event_type,
                payload=self._terminal_event_payload(
                    status=status,
                    terminal_reason=terminal_reason,
                    cancel_reason=effective_cancel,
                ),
                created_at=now,
            )
            receipt = self._terminal_receipt_v2(
                scan=scan,
                status=status,
                started_at=scan.started_at,
                finished_at=now,
                cancel_reason=effective_cancel,
                terminal_reason=terminal_reason,
                mdns_status=mdns_status,
                planned_count=progress["planned_count"],
                admitted_count=progress["admitted_count"],
                completed_count=progress["completed_count"],
                observations=durable_observations,
                error_category_counts=errors,
                timeout_count=progress["timeout_count"],
                evidence_complete=True,
                unknown_inflight_count=0,
            )
            receipt_json = canonical_json(receipt)
            self._require_bounded_receipt(receipt_json)
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET status = ?, revision = ?, updated_at = ?, finished_at = ?,
                    cancel_reason = ?, terminal_reason = ?, candidate_count = ?,
                    error_count = ?, timeout_count = ?, terminal_receipt_json = ?,
                    terminal_receipt_digest = ?
                WHERE scan_id = ? AND revision = ?
                """,
                (
                    status,
                    scan.revision + 1,
                    now,
                    now,
                    effective_cancel,
                    terminal_reason,
                    len(durable_observations),
                    sum(errors.values()),
                    progress["timeout_count"],
                    receipt_json,
                    sha256_digest(receipt),
                    scan.scan_id,
                    scan.revision,
                ),
            )
            self._check_deadline(absolute_deadline, monotonic_clock)
            self._before_commit("commit_scan_terminal")
            self._check_deadline(absolute_deadline, monotonic_clock)
            row = connection.execute(
                "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                (scan.scan_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("lan_scan_terminal_write_lost")
        return scan_from_row(row)

    def interrupt_active_scans(self, *, owner_principal: str) -> list[LanScanRecord]:
        owner = _validate_exact_owner_principal(owner_principal)
        with self.state._connect() as connection:
            active_ids = [
                str(row["scan_id"])
                for row in connection.execute(
                    """
                    SELECT scan_id FROM routing_lan_scans
                    WHERE owner_principal = ? AND status IN ('running', 'cancelling')
                    ORDER BY scan_id ASC
                    """,
                    (owner,),
                ).fetchall()
            ]
        interrupted: list[LanScanRecord] = []
        for active_id in active_ids:
            now = self._now()
            with self.state._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                    (active_id,),
                ).fetchone()
                if row is None:
                    continue
                scan = scan_from_row(row)
                if scan.owner_principal != owner or scan.status not in {"running", "cancelling"}:
                    continue
                progress = self._last_progress_payload(connection, scan.scan_id)
                rows = connection.execute(
                    """
                    SELECT * FROM routing_lan_observations
                    WHERE scan_id = ? ORDER BY endpoint_id ASC
                    """,
                    (scan.scan_id,),
                ).fetchall()
                observations = [observation_from_row(item) for item in rows]
                if progress is None:
                    durable_errors, durable_timeouts = self._durable_error_counts(
                        connection,
                        scan.scan_id,
                    )
                    progress = bounded_scan_progress_event(
                        {
                            "schema": LAN_SCAN_PROGRESS_EVENT_SCHEMA,
                            "planned_count": len(observations),
                            "admitted_count": len(observations),
                            "completed_count": len(observations),
                            "persisted_observation_count": len(observations),
                            "error_category_counts": durable_errors,
                            "timeout_count": durable_timeouts,
                            "mdns_status": "unavailable",
                        }
                    )
                self._insert_event_txn(
                    connection,
                    scan_id=scan.scan_id,
                    event_type="scan_interrupted",
                    payload=self._terminal_event_payload(
                        status="interrupted",
                        terminal_reason="startup_interrupted",
                        cancel_reason=scan.cancel_reason,
                    ),
                    created_at=now,
                )
                errors = bounded_error_category_counts(progress["error_category_counts"])
                receipt = self._terminal_receipt_v2(
                    scan=scan,
                    status="interrupted",
                    started_at=scan.started_at,
                    finished_at=now,
                    cancel_reason=scan.cancel_reason,
                    terminal_reason="startup_interrupted",
                    mdns_status=str(progress["mdns_status"]),
                    planned_count=int(progress["planned_count"]),
                    admitted_count=int(progress["admitted_count"]),
                    completed_count=int(progress["completed_count"]),
                    observations=observations,
                    error_category_counts=errors,
                    timeout_count=int(progress["timeout_count"]),
                    evidence_complete=False,
                    unknown_inflight_count=None,
                )
                receipt_json = canonical_json(receipt)
                self._require_bounded_receipt(receipt_json)
                connection.execute(
                    """
                    UPDATE routing_lan_scans
                    SET status = 'interrupted', revision = ?, updated_at = ?,
                        finished_at = ?, terminal_reason = 'startup_interrupted',
                        candidate_count = ?, error_count = ?, timeout_count = ?,
                        terminal_receipt_json = ?, terminal_receipt_digest = ?
                    WHERE scan_id = ? AND revision = ?
                    """,
                    (
                        scan.revision + 1,
                        now,
                        now,
                        len(observations),
                        sum(errors.values()),
                        progress["timeout_count"],
                        receipt_json,
                        sha256_digest(receipt),
                        scan.scan_id,
                        scan.revision,
                    ),
                )
                self._before_commit("interrupt_active_scans")
                updated = connection.execute(
                    "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
                    (scan.scan_id,),
                ).fetchone()
            if updated is not None:
                interrupted.append(scan_from_row(updated))
        return interrupted

    @staticmethod
    def _require_live_preview_event(
        event_payload: dict[str, Any],
        now: str,
    ) -> None:
        try:
            current = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(UTC)
            expires_at = datetime.fromisoformat(
                str(event_payload["expires_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("LAN preview expiration is invalid") from exc
        if current >= expires_at:
            raise ValueError("LAN preview authorization expired")

    def _now(self) -> str:
        if self._utc_clock is None:
            return utc_now()
        value = self._utc_clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("LAN ledger UTC clock must return an aware datetime")
        return value.astimezone(UTC).isoformat()

    def _before_commit(self, operation: str) -> None:
        if self._precommit_hook is not None:
            self._precommit_hook(operation)

    @staticmethod
    def _check_deadline(
        absolute_deadline: float | None,
        monotonic_clock: Callable[[], float],
    ) -> None:
        if absolute_deadline is None:
            return
        if (
            isinstance(absolute_deadline, bool)
            or not isinstance(absolute_deadline, (int, float))
            or not isfinite(float(absolute_deadline))
        ):
            raise ValueError("LAN absolute deadline must be finite")
        current = monotonic_clock()
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not isfinite(float(current))
        ):
            raise ValueError("LAN monotonic clock must be finite")
        if float(current) >= float(absolute_deadline):
            raise TimeoutError("lan_scan_deadline_expired")

    def _owned_mutable_scan(
        self,
        connection: sqlite3.Connection,
        scan_id: str,
        *,
        owner_principal: str,
        expected_revision: int,
    ) -> LanScanRecord:
        scan = self._mutable_scan(
            connection,
            scan_id,
            expected_revision=expected_revision,
        )
        if scan.owner_principal != owner_principal:
            raise ValueError("LAN scan owner does not match authenticated owner")
        return scan

    @staticmethod
    def _insert_event_txn(
        connection: sqlite3.Connection,
        *,
        scan_id: str,
        event_type: str,
        payload: dict[str, object],
        created_at: str,
    ) -> None:
        encoded = canonical_json(payload)
        if len(encoded.encode("utf-8")) > 8_192:
            raise ValueError("LAN scan event exceeds bounded storage")
        sequence_row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM routing_lan_scan_events WHERE scan_id = ?
            """,
            (scan_id,),
        ).fetchone()
        if sequence_row is None:
            raise RuntimeError("lan_event_sequence_unavailable")
        connection.execute(
            """
            INSERT INTO routing_lan_scan_events (
                scan_id, sequence, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                int(sequence_row["next_sequence"]),
                event_type,
                encoded,
                created_at,
            ),
        )

    def _insert_or_validate_observation_txn(
        self,
        connection: sqlite3.Connection,
        *,
        scan: LanScanRecord,
        observation: LanObservationDraft,
        created_at: str,
    ) -> bool:
        values = validate_observation(observation)
        if values["interface_id"] != scan.confirmed_interface_id:
            raise ValueError("observation interface does not match confirmed scan interface")
        address = ipaddress.ip_address(str(values["address"]))
        if address not in ipaddress.ip_network(scan.network, strict=True):
            raise ValueError("observation address does not belong to confirmed scan network")
        existing_row = connection.execute(
            """
            SELECT * FROM routing_lan_observations
            WHERE scan_id = ? AND endpoint_id = ?
            """,
            (scan.scan_id, values["endpoint_id"]),
        ).fetchone()
        if existing_row is not None:
            existing = observation_from_row(existing_row)
            existing_values = validate_observation(
                LanObservationDraft(
                    endpoint_id=existing.endpoint_id,
                    source=existing.source,
                    interface_id=existing.interface_id,
                    address=existing.address,
                    port=existing.port,
                    api_shape=existing.api_shape,
                    tls_enabled=existing.tls_enabled,
                    certificate_sha256=existing.certificate_sha256,
                    catalog_digest=existing.catalog_digest,
                    capability_digest=existing.capability_digest,
                    public_payload=existing.public_payload,
                    freshness_timestamp=existing.freshness_timestamp,
                    error_category=existing.error_category,
                )
            )
            if existing_values != values:
                raise ValueError(f"conflicting LAN observation endpoint: {values['endpoint_id']}")
            return False
        connection.execute(
            """
            INSERT INTO routing_lan_observations (
                scan_id, endpoint_id, source, interface_id, address, port,
                api_shape, tls_enabled, certificate_sha256, catalog_digest,
                capability_digest, public_payload_json, freshness_timestamp,
                error_category, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan.scan_id,
                values["endpoint_id"],
                values["source"],
                values["interface_id"],
                values["address"],
                values["port"],
                values["api_shape"],
                1 if values["tls_enabled"] else 0,
                values["certificate_sha256"],
                values["catalog_digest"],
                values["capability_digest"],
                canonical_json(values["public_payload"]),
                values["freshness_timestamp"],
                values["error_category"],
                created_at,
            ),
        )
        return True

    @staticmethod
    def _last_progress_payload(
        connection: sqlite3.Connection,
        scan_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT payload_json FROM routing_lan_scan_events
            WHERE scan_id = ? AND event_type = 'scan_progress'
            ORDER BY sequence DESC LIMIT 1
            """,
            (scan_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except ValueError as exc:
            raise ValueError("durable LAN progress event is invalid") from exc
        return bounded_scan_progress_event(payload)

    @staticmethod
    def _require_monotonic_progress(
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        if int(current["planned_count"]) != int(previous["planned_count"]):
            raise ValueError("LAN scan planned count cannot change")
        for field in (
            "admitted_count",
            "completed_count",
            "persisted_observation_count",
            "timeout_count",
        ):
            if int(current[field]) < int(previous[field]):
                raise ValueError("LAN scan progress must be monotonic")
        previous_errors = dict(previous["error_category_counts"])
        current_errors = dict(current["error_category_counts"])
        for category in set(previous_errors) | set(current_errors):
            if int(current_errors.get(category, 0)) < int(previous_errors.get(category, 0)):
                raise ValueError("LAN scan error progress must be monotonic")
        if current["mdns_status"] != previous["mdns_status"]:
            raise ValueError("LAN mDNS status cannot change after progress is durable")

    @staticmethod
    def _durable_error_counts(
        connection: sqlite3.Connection,
        scan_id: str,
    ) -> tuple[dict[str, int], int]:
        rows = connection.execute(
            """
            SELECT error_category, COUNT(*) AS category_count
            FROM routing_lan_observations
            WHERE scan_id = ? AND error_category IS NOT NULL
            GROUP BY error_category ORDER BY error_category ASC
            """,
            (scan_id,),
        ).fetchall()
        counts = {str(row["error_category"]): int(row["category_count"]) for row in rows}
        timeout_count = sum(
            count for category, count in counts.items() if category in _TIMEOUT_ERROR_CATEGORIES
        )
        return counts, timeout_count

    @staticmethod
    def _terminal_event_payload(
        *,
        status: str,
        terminal_reason: str,
        cancel_reason: str | None,
    ) -> dict[str, object]:
        return {
            "schema": LAN_SCAN_TERMINAL_EVENT_SCHEMA,
            "status": status,
            "terminal_reason": terminal_reason,
            "cancel_reason": cancel_reason,
        }

    @staticmethod
    def _terminal_receipt_v2(
        *,
        scan: LanScanRecord,
        status: str,
        started_at: str | None,
        finished_at: str,
        cancel_reason: str | None,
        terminal_reason: str,
        mdns_status: str,
        planned_count: int,
        admitted_count: int,
        completed_count: int,
        observations: list[LanObservationRecord],
        error_category_counts: dict[str, int],
        timeout_count: int,
        evidence_complete: bool,
        unknown_inflight_count: int | None,
    ) -> dict[str, Any]:
        return {
            "schema": LAN_SCAN_RECEIPT_V2_SCHEMA,
            "version": 2,
            "scan_id": scan.scan_id,
            "status": status,
            "owner_principal": scan.owner_principal,
            "confirmed_interface_id": scan.confirmed_interface_id,
            "network": scan.network,
            "limits": scan.limits,
            "limits_digest": scan.limits_digest,
            "preview_digest": scan.preview_digest,
            "started_at": started_at,
            "finished_at": finished_at,
            "cancel_reason": cancel_reason,
            "terminal_reason": terminal_reason,
            "mdns_status": mdns_status,
            "planned_count": planned_count,
            "admitted_count": admitted_count,
            "completed_count": completed_count,
            "persisted_observation_count": len(observations),
            "error_count": sum(error_category_counts.values()),
            "timeout_count": timeout_count,
            "error_category_counts": dict(sorted(error_category_counts.items())),
            "observation_count": len(observations),
            "observation_membership_digest": observation_membership_digest(observations),
            "evidence_complete": evidence_complete,
            "unknown_inflight_count": unknown_inflight_count,
        }

    @staticmethod
    def _require_bounded_receipt(receipt_json: str) -> None:
        if len(receipt_json.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise ValueError("terminal LAN receipt exceeds bounded storage")

    def _mutable_scan(
        self,
        connection: sqlite3.Connection,
        scan_id: str,
        *,
        expected_revision: int | None,
    ) -> LanScanRecord:
        row = connection.execute(
            "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown LAN scan: {scan_id}")
        scan = scan_from_row(row)
        if scan.is_terminal:
            raise LanScanRevisionConflict(scan.scan_id, scan.revision)
        validate_exact_revision(
            expected_revision,
            actual_revision=scan.revision,
            scan_id=scan.scan_id,
        )
        return scan

    def _terminal_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        scan: LanScanRecord,
        status: str,
        started_at: str | None,
        finished_at: str,
        cancel_reason: str | None,
        terminal_reason: str | None,
        candidate_count: int,
        error_count: int,
        timeout_count: int,
    ) -> dict[str, Any]:
        observations = [
            observation_from_row(row).to_payload()
            for row in connection.execute(
                """
                SELECT * FROM routing_lan_observations
                WHERE scan_id = ? ORDER BY endpoint_id ASC
                """,
                (scan.scan_id,),
            ).fetchall()
        ]
        return {
            "scan_id": scan.scan_id,
            "status": status,
            "owner_principal": scan.owner_principal,
            "confirmed_interface_id": scan.confirmed_interface_id,
            "network": scan.network,
            "limits": scan.limits,
            "limits_digest": scan.limits_digest,
            "preview_digest": scan.preview_digest,
            "started_at": started_at,
            "finished_at": finished_at,
            "cancel_reason": cancel_reason,
            "terminal_reason": terminal_reason,
            "candidate_count": candidate_count,
            "error_count": error_count,
            "timeout_count": timeout_count,
            "observations": observations,
        }
