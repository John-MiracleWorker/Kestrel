"""Revision-checked SQLite ledger for explicit private-LAN scans."""

from __future__ import annotations

import ipaddress
import sqlite3
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
    MAX_RECEIPT_BYTES,
    bounded_event_public_evidence,
    bounded_scan_limits,
    canonical_json,
    event_from_row,
    normalize_network,
    observation_from_row,
    scan_from_row,
    sha256_digest,
    validate_digest,
    validate_non_negative_count,
    validate_observation,
    validate_optional_text,
    validate_required_text,
)
from .ledger_schema import ensure_routing_schema


class LanDiscoveryLedger:
    """Own durable scan receipts without performing any network discovery."""

    def __init__(self, state: AgentStateStore) -> None:
        self.state = state
        ensure_routing_schema(state)

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
        normalized_owner = validate_required_text(
            owner_principal,
            "owner_principal",
            maximum=256,
        )
        normalized_interface = validate_digest(
            confirmed_interface_id,
            "confirmed_interface_id",
        )
        normalized_preview = validate_digest(preview_digest, "preview_digest")
        normalized_network = normalize_network(network)
        normalized_limits = bounded_scan_limits(limits)
        limits_json = canonical_json(normalized_limits)
        limits_digest = sha256_digest(normalized_limits)
        now = utc_now()
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
        now = utc_now()
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
        now = utc_now()
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
        now = utc_now()
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
        if (
            expected_revision is None
            or isinstance(expected_revision, bool)
            or expected_revision != scan.revision
        ):
            raise LanScanRevisionConflict(scan.scan_id, scan.revision)
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
