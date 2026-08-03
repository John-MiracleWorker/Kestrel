"""Durable Flock qualification and activation grant ledger (schema v4).

Runs move ``draft -> ready -> running -> (pausing -> paused) -> terminal``
with revision compare-and-swap, and terminalize exactly once. Attempts move
``pending -> reserved -> running -> terminal``. Receipts, activation grant
base rows, and activation transitions are immutable once written; the
database triggers installed by :mod:`.ledger_schema` are the backstop and
these methods reject the same mutations with clearer errors.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..control_plane_integrity import (
    AuthenticatedPayload,
    ControlPlaneIntegrity,
    authenticated_payload_digest,
)
from ..state_store import AgentStateStore, utc_now
from .ledger_schema import ensure_routing_schema
from .qualification_digest import canonical_digest, canonical_json
from .qualification_models import MoneyMicros
from .qualification_receipt import authenticate_terminal_receipt
from .qualification_records import (
    ATTEMPT_STATES,
    GRANT_TRANSITION_TYPES,
    RUN_STATES,
    TERMINAL_ATTEMPT_STATES,
    TERMINAL_RUN_STATES,
    ActivationGrant,
    ActivationGrantDraft,
    ActivationTransition,
    QualificationAttempt,
    QualificationAttemptDraft,
    QualificationCase,
    QualificationCaseDraft,
    QualificationEvent,
    QualificationReceipt,
    QualificationRevisionConflict,
    QualificationRun,
    QualificationRunDraft,
)
from .qualification_serialization import (
    attempt_from_row,
    bounded_evidence_refs,
    bounded_validation_codes,
    canonical_payload,
    case_from_row,
    event_from_row,
    grant_from_row,
    receipt_from_row,
    run_from_row,
    transition_from_row,
)

__all__ = ["BUDGET_SETTLEMENT_OUTCOMES", "QualificationLedger"]

BUDGET_SETTLEMENT_OUTCOMES: tuple[str, ...] = (
    "completed",
    "overrun",
    "missing_usage",
    "transport_confirmed_zero",
    "ambiguous",
)

_RUN_COLUMNS = """
    run_id, status, revision, owner_principal,
    scope_json, scope_digest, corpus_json, corpus_digest,
    target_json, target_digest, price_json, price_digest,
    policy_json, policy_digest, learned_json, learned_digest,
    project_authority_json, project_authority_digest,
    build_json, build_digest, thresholds_json, thresholds_digest,
    max_spend_micros, effective_stop_cap_micros, actual_spend_micros,
    unresolved_reserve_micros, inflight_reserve_micros, attempt_ceiling_micros,
    created_at, updated_at, started_at, finished_at, terminal_reason
"""

_NEXT_GRANT_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"activated"}),
    "activated": frozenset({"suspended", "revoked"}),
    "suspended": frozenset({"resumed", "revoked"}),
    "resumed": frozenset({"suspended", "revoked"}),
    "revoked": frozenset(),
}


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _receipt_authentication_projection(row: sqlite3.Row) -> dict[str, Any]:
    """Canonical receipt projection bound by the authentication envelope."""

    attempt_id = row["attempt_id"]
    return {
        "receipt_id": str(row["receipt_id"]),
        "run_id": str(row["run_id"]),
        "attempt_id": None if attempt_id is None else str(attempt_id),
        "receipt_type": str(row["receipt_type"]),
        "payload_digest": str(row["payload_digest"]),
        "created_at": str(row["created_at"]),
        "payload": json.loads(str(row["payload_json"])),
    }


class QualificationLedger:
    """Flock qualification runs, cases, attempts, receipts, and grants."""

    def __init__(
        self,
        state: AgentStateStore,
        *,
        integrity: ControlPlaneIntegrity | None = None,
    ) -> None:
        self.state = state
        self._integrity = integrity
        ensure_routing_schema(self.state)

    def schema_version(self) -> int:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT version FROM routing_schema_version WHERE id = 1"
            ).fetchone()
        return 0 if row is None else int(row["version"])

    # -- runs -----------------------------------------------------------------

    def create_run(self, draft: QualificationRunDraft) -> QualificationRun:
        payload_columns: dict[str, tuple[str, str]] = {}
        payloads: dict[str, Any] = {
            "scope": draft.scope.to_payload(),
            "corpus": asdict(draft.corpus),
            "target": draft.target_snapshot,
            "price": draft.price_snapshot,
            "policy": draft.policy_payload,
            "learned": draft.learned_payload,
            "project_authority": draft.project_authority,
            "build": draft.build,
            "thresholds": asdict(draft.thresholds),
        }
        for name, payload in payloads.items():
            payload_columns[name] = canonical_payload(payload)
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""
                INSERT INTO routing_qualification_runs ({_RUN_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.run_id,
                    "draft",
                    1,
                    draft.owner_principal,
                    payload_columns["scope"][0],
                    payload_columns["scope"][1],
                    payload_columns["corpus"][0],
                    payload_columns["corpus"][1],
                    payload_columns["target"][0],
                    payload_columns["target"][1],
                    payload_columns["price"][0],
                    payload_columns["price"][1],
                    payload_columns["policy"][0],
                    payload_columns["policy"][1],
                    payload_columns["learned"][0],
                    payload_columns["learned"][1],
                    payload_columns["project_authority"][0],
                    payload_columns["project_authority"][1],
                    payload_columns["build"][0],
                    payload_columns["build"][1],
                    payload_columns["thresholds"][0],
                    payload_columns["thresholds"][1],
                    draft.max_spend.micros,
                    draft.effective_stop_cap.micros,
                    0,
                    0,
                    0,
                    draft.attempt_ceiling.micros,
                    now,
                    now,
                    None,
                    None,
                    None,
                ),
            )
            row = self._fetch_run(conn, draft.run_id)
        assert row is not None
        return run_from_row(row)

    def get_run(self, run_id: str) -> QualificationRun | None:
        with self.state._connect() as conn:
            row = self._fetch_run(conn, run_id)
        return None if row is None else run_from_row(row)

    def list_runs(
        self, *, statuses: tuple[str, ...] | None = None
    ) -> list[QualificationRun]:
        """List qualification runs, optionally restricted to given statuses."""

        if statuses is not None:
            for status in statuses:
                if status not in RUN_STATES:
                    raise ValueError(f"unsupported run status: {status}")
        with self.state._connect() as conn:
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                rows = conn.execute(
                    f"""
                    SELECT * FROM routing_qualification_runs
                    WHERE status IN ({placeholders})
                    ORDER BY created_at, run_id
                    """,
                    statuses,
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM routing_qualification_runs
                    ORDER BY created_at, run_id
                    """
                ).fetchall()
        return [run_from_row(row) for row in rows]

    def mark_ready(self, run_id: str, *, expected_revision: int) -> QualificationRun:
        return self._run_transition(
            run_id,
            expected_revision=expected_revision,
            allowed=("draft",),
            target="ready",
        )

    def mark_running(self, run_id: str, *, expected_revision: int) -> QualificationRun:
        return self._run_transition(
            run_id,
            expected_revision=expected_revision,
            allowed=("ready", "paused"),
            target="running",
            set_started=True,
        )

    def request_pause(self, run_id: str, *, expected_revision: int) -> QualificationRun:
        return self._run_transition(
            run_id,
            expected_revision=expected_revision,
            allowed=("running",),
            target="pausing",
        )

    def mark_paused(self, run_id: str, *, expected_revision: int) -> QualificationRun:
        return self._run_transition(
            run_id,
            expected_revision=expected_revision,
            allowed=("pausing",),
            target="paused",
        )

    def update_effective_stop_cap(
        self,
        run_id: str,
        *,
        expected_revision: int,
        new_cap: MoneyMicros,
    ) -> QualificationRun:
        if not isinstance(new_cap, MoneyMicros):
            raise ValueError("new_cap must be a MoneyMicros value")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._run_row_for_update(conn, run_id, expected_revision)
            if int(row["max_spend_micros"]) < new_cap.micros:
                raise ValueError(
                    "effective stop cap cannot exceed the immutable max spend"
                )
            conn.execute(
                """
                UPDATE routing_qualification_runs
                SET effective_stop_cap_micros = ?, revision = revision + 1,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (new_cap.micros, now, run_id),
            )
            updated = self._fetch_run(conn, run_id)
        assert updated is not None
        return run_from_row(updated)

    def complete_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        terminal_reason: str,
        actual_spend: MoneyMicros,
        terminal_receipt: Mapping[str, Any],
    ) -> QualificationRun:
        return self._terminalize(
            run_id,
            expected_revision=expected_revision,
            terminal_status="completed",
            terminal_reason=terminal_reason,
            actual_spend=actual_spend,
            receipt_payload=terminal_receipt,
        )

    def fail_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        terminal_reason: str,
        actual_spend: MoneyMicros,
        terminal_receipt: Mapping[str, Any],
    ) -> QualificationRun:
        return self._terminalize(
            run_id,
            expected_revision=expected_revision,
            terminal_status="failed",
            terminal_reason=terminal_reason,
            actual_spend=actual_spend,
            receipt_payload=terminal_receipt,
        )

    def cancel_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        terminal_reason: str,
        actual_spend: MoneyMicros,
        terminal_receipt: Mapping[str, Any],
    ) -> QualificationRun:
        return self._terminalize(
            run_id,
            expected_revision=expected_revision,
            terminal_status="cancelled",
            terminal_reason=terminal_reason,
            actual_spend=actual_spend,
            receipt_payload=terminal_receipt,
        )

    def finalize_run_terminal(
        self,
        run_id: str,
        *,
        expected_revision: int,
        terminal_status: str,
        terminal_reason: str,
        actual_spend: MoneyMicros,
        receipt_payload: Mapping[str, Any],
    ) -> QualificationRun:
        """Authenticate the terminal receipt and finalize in one transaction.

        Task 12: signing happens before the transaction opens, so a signing
        failure persists nothing and no unsigned qualifying receipt is ever
        invented.  The transaction itself writes the receipt, links the run,
        marks it terminal, and appends the terminal event atomically.
        """

        authenticated = authenticate_terminal_receipt(
            receipt_payload,
            integrity=self._control_plane_integrity(),
        )
        return self._terminalize(
            run_id,
            expected_revision=expected_revision,
            terminal_status=terminal_status,
            terminal_reason=terminal_reason,
            actual_spend=actual_spend,
            receipt_payload=authenticated,
        )

    # -- events and receipts ----------------------------------------------------

    def append_event(
        self, run_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> QualificationEvent:
        _require_text(event_type, "event_type")
        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be a mapping")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._live_run_row(conn, run_id)
            sequence = self._insert_event_row(conn, run_id, event_type, payload, now)
            row = conn.execute(
                """
                SELECT * FROM routing_qualification_events
                WHERE run_id = ? AND sequence = ?
                """,
                (run_id, sequence),
            ).fetchone()
        return event_from_row(row)

    def list_events(self, run_id: str) -> list[QualificationEvent]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM routing_qualification_events
                WHERE run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        return [event_from_row(row) for row in rows]

    def append_receipt(
        self,
        run_id: str,
        receipt_type: str,
        payload: Mapping[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> QualificationReceipt:
        _require_text(receipt_type, "receipt_type")
        if not isinstance(payload, Mapping):
            raise ValueError("receipt payload must be a mapping")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._live_run_row(conn, run_id)
            receipt_id = self._insert_receipt_row(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                receipt_type=receipt_type,
                payload=payload,
                now=now,
            )
            row = conn.execute(
                "SELECT * FROM routing_qualification_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        return receipt_from_row(row)

    def list_receipts(self, run_id: str) -> list[QualificationReceipt]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM routing_qualification_receipts
                WHERE run_id = ? ORDER BY created_at, receipt_id
                """,
                (run_id,),
            ).fetchall()
        return [receipt_from_row(row) for row in rows]

    # -- receipt authentication (owner-only control-plane key material) ---------

    def receipt_envelope(self, receipt_id: str) -> AuthenticatedPayload:
        """Sign the canonical projection of one stored immutable receipt."""

        _require_text(receipt_id, "receipt_id")
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_qualification_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown qualification receipt: {receipt_id}")
        return self._control_plane_integrity().sign(
            _receipt_authentication_projection(row)
        )

    def verify_receipt_envelope(self, envelope: Mapping[str, Any]) -> bool:
        """Verify an envelope cryptographically and against the stored row.

        Verification never creates key material: a missing or unsafe key fails
        closed, and a new key is never generated over existing receipts.
        """

        integrity = self._integrity
        if integrity is None:
            try:
                integrity = ControlPlaneIntegrity(
                    Path(self.state.path).parent,
                    create_if_missing=False,
                )
            except (OSError, ValueError):
                return False
        if not integrity.verify(envelope):
            return False
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return False
        receipt_id = payload.get("receipt_id")
        if not isinstance(receipt_id, str):
            return False
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_qualification_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return False
        projection_digest = authenticated_payload_digest(
            _receipt_authentication_projection(row)
        )
        return hmac.compare_digest(str(envelope.get("payload_digest")), projection_digest)

    def _control_plane_integrity(self) -> ControlPlaneIntegrity:
        # First signing mints the owner key atomically-once.  A key lost after
        # envelopes were issued is surfaced by desktop recovery reporting and
        # by the backup state/key pairing gate, never by silently minting a
        # replacement on a read-only path.
        if self._integrity is None:
            self._integrity = ControlPlaneIntegrity(Path(self.state.path).parent)
        return self._integrity

    # -- cases ------------------------------------------------------------------

    def create_case(self, draft: QualificationCaseDraft) -> QualificationCase:
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = self._live_run_row(conn, draft.run_id)
            scope = self._run_scope_payload(run_row)
            if (
                draft.item.task_family != scope["task_family"]
                or draft.item.risk != scope["risk"]
            ):
                raise ValueError(
                    "case item does not match the qualification run scope"
                )
            conn.execute(
                """
                INSERT INTO routing_qualification_cases (
                    case_id, run_id, item_id, task_family, risk,
                    task_contract_digest, acceptance_plan_digest,
                    repository_digest, privacy_eligible, scope_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.case_id,
                    draft.run_id,
                    draft.item.item_id,
                    draft.item.task_family,
                    draft.item.risk,
                    draft.item.task_contract_digest,
                    draft.item.acceptance_plan_digest,
                    draft.repository_digest,
                    1 if draft.privacy_eligible else 0,
                    str(run_row["scope_digest"]),
                    now,
                ),
            )
            row = self._fetch_case(conn, draft.case_id)
        assert row is not None
        return case_from_row(row)

    def get_case(self, case_id: str) -> QualificationCase | None:
        with self.state._connect() as conn:
            row = self._fetch_case(conn, case_id)
        return None if row is None else case_from_row(row)

    def list_cases(self, run_id: str) -> list[QualificationCase]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM routing_qualification_cases
                WHERE run_id = ? ORDER BY created_at, case_id
                """,
                (run_id,),
            ).fetchall()
        return [case_from_row(row) for row in rows]

    # -- attempts ---------------------------------------------------------------

    def create_attempt(self, draft: QualificationAttemptDraft) -> QualificationAttempt:
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            case_row, _ = self._attempt_draft_context(conn, draft)
            self._insert_attempt_row(conn, draft, case_row, status="pending", now=now)
            row = self._fetch_attempt(conn, draft.attempt_id)
        assert row is not None
        return attempt_from_row(row)

    # -- transactional hard-cap admission (Adaptive Flock Task 7) ----------------

    def admit_attempt_with_budget(
        self, draft: QualificationAttemptDraft
    ) -> QualificationAttempt:
        """Create and reserve an attempt atomically under the hard-cap condition.

        The budget check, the attempt row insert (already ``reserved``), and
        the run inflight-reserve increment happen in one SQLite transaction,
        so a rejected admission leaves no attempt row and no reserve.
        """

        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            case_row, run_row = self._attempt_draft_context(conn, draft)
            self._assert_budget_admission(run_row, draft.reservation)
            self._insert_attempt_row(conn, draft, case_row, status="reserved", now=now)
            conn.execute(
                """
                UPDATE routing_qualification_runs
                SET inflight_reserve_micros = inflight_reserve_micros + ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (draft.reservation.micros, now, str(case_row["run_id"])),
            )
            row = self._fetch_attempt(conn, draft.attempt_id)
        assert row is not None
        return attempt_from_row(row)

    def admit_pending_attempt_with_budget(
        self, attempt_id: str
    ) -> QualificationAttempt:
        """Reserve an existing pending attempt atomically under the hard cap.

        The budget check, the ``pending -> reserved`` transition, and the run
        inflight-reserve increment happen in one SQLite transaction, so a
        rejected re-admission leaves the attempt pending with no reserve.
        """

        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._attempt_row_for_transition(
                conn, attempt_id, ("pending",), "admit"
            )
            run_row = self._live_run_row(conn, str(row["run_id"]))
            reservation = MoneyMicros(int(row["reservation_micros"]))
            self._assert_budget_admission(run_row, reservation)
            conn.execute(
                """
                UPDATE routing_qualification_attempts
                SET status = 'reserved', revision = revision + 1, updated_at = ?
                WHERE attempt_id = ?
                """,
                (now, attempt_id),
            )
            conn.execute(
                """
                UPDATE routing_qualification_runs
                SET inflight_reserve_micros = inflight_reserve_micros + ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (reservation.micros, now, str(row["run_id"])),
            )
            updated = self._fetch_attempt(conn, attempt_id)
        assert updated is not None
        return attempt_from_row(updated)

    def release_attempt_reservation(self, attempt_id: str) -> QualificationAttempt:
        """Recovery: return a never-dispatched reserved attempt to pending.

        Releases the run inflight reserve in the same transaction. Only the
        startup reconciliation path uses this: a ``reserved`` attempt whose
        owner process died before dispatch was never submitted to a provider,
        so the reservation can be released exactly once.
        """

        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._attempt_row_for_transition(
                conn, attempt_id, ("reserved",), "release"
            )
            reserve = int(row["reservation_micros"])
            conn.execute(
                """
                UPDATE routing_qualification_attempts
                SET status = 'pending', revision = revision + 1, updated_at = ?
                WHERE attempt_id = ?
                """,
                (now, attempt_id),
            )
            conn.execute(
                """
                UPDATE routing_qualification_runs
                SET inflight_reserve_micros = inflight_reserve_micros - ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (reserve, now, str(row["run_id"])),
            )
            updated = self._fetch_attempt(conn, attempt_id)
        assert updated is not None
        return attempt_from_row(updated)

    def settle_attempt_with_budget(
        self,
        attempt_id: str,
        *,
        outcome: str,
        usage: Mapping[str, Any] | None = None,
        actual_cost: MoneyMicros | None = None,
        validation_passed: bool = False,
        validation_codes: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
        guardrail_violated: bool = False,
    ) -> QualificationAttempt:
        """Terminalize an attempt and reconcile run budget columns atomically.

        Outcomes (see :data:`BUDGET_SETTLEMENT_OUTCOMES`):

        - ``completed``: replace the reserve with the exact known actual.
        - ``overrun``: charge the actual, record ``budget_projection_overrun``,
          and stop admissions by pinning the effective stop cap to zero.
        - ``missing_usage``/``ambiguous``: move the full reserve to unresolved.
        - ``transport_confirmed_zero``: release the reserve with known zero
          cost (the provider adapter proved no request was accepted).
        """

        if outcome not in BUDGET_SETTLEMENT_OUTCOMES:
            raise ValueError(f"unsupported budget settlement outcome: {outcome}")
        if usage is not None and not isinstance(usage, Mapping):
            raise ValueError("usage must be a mapping")
        if actual_cost is not None and not isinstance(actual_cost, MoneyMicros):
            raise ValueError("actual_cost must be a MoneyMicros value")
        if not isinstance(validation_passed, bool):
            raise ValueError("validation_passed must be a boolean")
        codes = bounded_validation_codes(validation_codes)
        refs = bounded_evidence_refs(evidence_refs)
        guardrail_state = "violated" if guardrail_violated else "clear"
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            allowed = (
                ("running",)
                if outcome in ("completed", "overrun")
                else ("reserved", "running")
            )
            row = self._attempt_row_for_transition(conn, attempt_id, allowed, "settle")
            run_row = self._live_run_row(conn, str(row["run_id"]))
            run_id = str(row["run_id"])
            reserve = int(row["reservation_micros"])
            if outcome in ("completed", "overrun"):
                if actual_cost is None:
                    raise ValueError("completed settlement requires an actual cost")
                conn.execute(
                    """
                    UPDATE routing_qualification_attempts
                    SET status = 'completed', revision = revision + 1,
                        updated_at = ?, finished_at = ?, usage_json = ?,
                        actual_cost_micros = ?, unresolved_cost_micros = 0,
                        validation_passed = ?, validation_codes_json = ?,
                        guardrail_state = ?, evidence_refs_json = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        now,
                        now,
                        canonical_json(usage if usage is not None else {}),
                        actual_cost.micros,
                        1 if validation_passed else 0,
                        canonical_json(list(codes)),
                        guardrail_state,
                        canonical_json(list(refs)),
                        attempt_id,
                    ),
                )
                if outcome == "overrun":
                    self._insert_event_row(
                        conn,
                        run_id,
                        "budget_projection_overrun",
                        {
                            "attempt_id": attempt_id,
                            "reserve_micros": reserve,
                            "actual_micros": actual_cost.micros,
                            "scope_digest": str(run_row["scope_digest"]),
                        },
                        now,
                    )
                    conn.execute(
                        """
                        UPDATE routing_qualification_runs
                        SET actual_spend_micros = actual_spend_micros + ?,
                            inflight_reserve_micros = inflight_reserve_micros - ?,
                            effective_stop_cap_micros = 0,
                            revision = revision + 1, updated_at = ?
                        WHERE run_id = ?
                        """,
                        (actual_cost.micros, reserve, now, run_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE routing_qualification_runs
                        SET actual_spend_micros = actual_spend_micros + ?,
                            inflight_reserve_micros = inflight_reserve_micros - ?,
                            updated_at = ?
                        WHERE run_id = ?
                        """,
                        (actual_cost.micros, reserve, now, run_id),
                    )
            elif outcome == "transport_confirmed_zero":
                conn.execute(
                    """
                    UPDATE routing_qualification_attempts
                    SET status = 'failed', revision = revision + 1,
                        updated_at = ?, finished_at = ?, usage_json = ?,
                        actual_cost_micros = 0, unresolved_cost_micros = 0,
                        failure_category = ?, guardrail_state = ?,
                        evidence_refs_json = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        now,
                        now,
                        canonical_json({"input_tokens": 0, "output_tokens": 0}),
                        "transport_failed_no_request_accepted",
                        guardrail_state,
                        canonical_json(list(refs)),
                        attempt_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE routing_qualification_runs
                    SET inflight_reserve_micros = inflight_reserve_micros - ?,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (reserve, now, run_id),
                )
            else:
                reason = (
                    "usage_unreported"
                    if outcome == "missing_usage"
                    else "transport_outcome_ambiguous"
                )
                conn.execute(
                    """
                    UPDATE routing_qualification_attempts
                    SET status = 'ambiguous', revision = revision + 1,
                        updated_at = ?, finished_at = ?,
                        unresolved_cost_micros = ?, failure_category = ?,
                        guardrail_state = ?, evidence_refs_json = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        now,
                        now,
                        reserve,
                        reason,
                        guardrail_state,
                        canonical_json(list(refs)),
                        attempt_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE routing_qualification_runs
                    SET inflight_reserve_micros = inflight_reserve_micros - ?,
                        unresolved_reserve_micros = unresolved_reserve_micros + ?,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (reserve, reserve, now, run_id),
                )
            updated = self._fetch_attempt(conn, attempt_id)
        assert updated is not None
        return attempt_from_row(updated)

    def get_attempt(self, attempt_id: str) -> QualificationAttempt | None:
        with self.state._connect() as conn:
            row = self._fetch_attempt(conn, attempt_id)
        return None if row is None else attempt_from_row(row)

    def list_attempts(self, case_id: str) -> list[QualificationAttempt]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM routing_qualification_attempts
                WHERE case_id = ? ORDER BY attempt_number
                """,
                (case_id,),
            ).fetchall()
        return [attempt_from_row(row) for row in rows]

    def reserve_attempt(self, attempt_id: str) -> QualificationAttempt:
        return self._attempt_transition(attempt_id, allowed=("pending",), target="reserved")

    def mark_attempt_running(self, attempt_id: str) -> QualificationAttempt:
        return self._attempt_transition(
            attempt_id, allowed=("reserved",), target="running", set_started=True
        )

    def complete_attempt(
        self,
        attempt_id: str,
        *,
        usage: Mapping[str, Any],
        actual_cost: MoneyMicros,
        validation_passed: bool,
        validation_codes: tuple[str, ...],
        evidence_refs: tuple[str, ...],
        provider_receipts: tuple[Mapping[str, Any], ...] = (),
        guardrail_violated: bool = False,
    ) -> QualificationAttempt:
        if not isinstance(usage, Mapping):
            raise ValueError("usage must be a mapping")
        if not isinstance(actual_cost, MoneyMicros):
            raise ValueError("actual_cost must be a MoneyMicros value")
        if not isinstance(validation_passed, bool):
            raise ValueError("validation_passed must be a boolean")
        codes = bounded_validation_codes(validation_codes)
        refs = bounded_evidence_refs(evidence_refs)
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._attempt_row_for_transition(conn, attempt_id, ("running",), "complete")
            conn.execute(
                """
                UPDATE routing_qualification_attempts
                SET status = 'completed', revision = revision + 1, updated_at = ?,
                    finished_at = ?, usage_json = ?, actual_cost_micros = ?,
                    unresolved_cost_micros = 0, validation_passed = ?,
                    validation_codes_json = ?, provider_receipts_json = ?,
                    guardrail_state = ?, evidence_refs_json = ?
                WHERE attempt_id = ?
                """,
                (
                    now,
                    now,
                    canonical_json(usage),
                    actual_cost.micros,
                    1 if validation_passed else 0,
                    canonical_json(list(codes)),
                    canonical_json([dict(receipt) for receipt in provider_receipts]),
                    "violated" if guardrail_violated else "clear",
                    canonical_json(list(refs)),
                    str(row["attempt_id"]),
                ),
            )
            updated = self._fetch_attempt(conn, attempt_id)
        assert updated is not None
        return attempt_from_row(updated)

    def fail_attempt(
        self,
        attempt_id: str,
        *,
        failure_category: str,
        evidence_refs: tuple[str, ...] = (),
        guardrail_violated: bool = False,
    ) -> QualificationAttempt:
        _require_text(failure_category, "failure_category")
        refs = bounded_evidence_refs(evidence_refs)
        return self._attempt_terminalize(
            attempt_id,
            allowed=("reserved", "running"),
            target="failed",
            failure_category=failure_category,
            evidence_refs=refs,
            guardrail_violated=guardrail_violated,
        )

    def cancel_attempt(self, attempt_id: str) -> QualificationAttempt:
        return self._attempt_terminalize(
            attempt_id,
            allowed=("pending", "reserved", "running"),
            target="cancelled",
            failure_category=None,
            evidence_refs=(),
            guardrail_violated=False,
        )

    def mark_attempt_ambiguous(self, attempt_id: str, *, reason: str) -> QualificationAttempt:
        _require_text(reason, "reason")
        return self._attempt_terminalize(
            attempt_id,
            allowed=("running",),
            target="ambiguous",
            failure_category=reason,
            evidence_refs=(),
            guardrail_violated=False,
        )

    # -- activation grants --------------------------------------------------------

    def create_grant(self, draft: ActivationGrantDraft) -> ActivationGrant:
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = self._fetch_run(conn, draft.run_id)
            if run_row is None:
                raise ValueError(f"unknown qualification run: {draft.run_id}")
            if str(run_row["status"]) != "completed":
                raise ValueError(
                    "activation grant requires a completed qualification run"
                )
            receipt_row = conn.execute(
                "SELECT run_id FROM routing_qualification_receipts WHERE receipt_id = ?",
                (draft.qualification_receipt_id,),
            ).fetchone()
            if receipt_row is None or str(receipt_row["run_id"]) != draft.run_id:
                raise ValueError(
                    "qualification receipt does not belong to the qualification run"
                )
            scope = self._run_scope_payload(run_row)
            conn.execute(
                """
                INSERT INTO routing_activation_grants (
                    grant_id, run_id, target_id, scope_json, scope_digest,
                    policy_id, policy_revision, qualification_receipt_id,
                    created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.grant_id,
                    draft.run_id,
                    draft.target_id,
                    str(run_row["scope_json"]),
                    str(run_row["scope_digest"]),
                    str(scope["policy_id"]),
                    int(scope["policy_revision"]),
                    draft.qualification_receipt_id,
                    draft.created_by,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM routing_activation_grants WHERE grant_id = ?",
                (draft.grant_id,),
            ).fetchone()
        return grant_from_row(row)

    def get_grant(self, grant_id: str) -> ActivationGrant | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_activation_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
        return None if row is None else grant_from_row(row)

    def append_transition(
        self,
        grant_id: str,
        transition_type: str,
        reason: str,
        *,
        receipt_id: str | None = None,
    ) -> ActivationTransition:
        if transition_type not in GRANT_TRANSITION_TYPES:
            raise ValueError(
                f"transition_type must be one of {', '.join(GRANT_TRANSITION_TYPES)}"
            )
        _require_text(reason, "reason")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            grant_row = conn.execute(
                "SELECT grant_id FROM routing_activation_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            if grant_row is None:
                raise ValueError(f"unknown activation grant: {grant_id}")
            last = conn.execute(
                """
                SELECT transition_type, sequence FROM routing_activation_transitions
                WHERE grant_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (grant_id,),
            ).fetchone()
            last_type = None if last is None else str(last["transition_type"])
            if last_type == "revoked":
                raise ValueError(f"activation grant {grant_id} is revoked")
            if transition_type not in _NEXT_GRANT_TRANSITIONS[last_type]:
                if last_type is None:
                    raise ValueError(
                        f"first transition must be 'activated', got "
                        f"'{transition_type}'"
                    )
                raise ValueError(
                    f"transition '{transition_type}' is not allowed after "
                    f"'{last_type}'"
                )
            sequence = 1 if last is None else int(last["sequence"]) + 1
            transition_id = f"{grant_id}:{sequence}"
            conn.execute(
                """
                INSERT INTO routing_activation_transitions (
                    transition_id, grant_id, sequence, transition_type,
                    reason, receipt_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    grant_id,
                    sequence,
                    transition_type,
                    reason,
                    receipt_id,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM routing_activation_transitions WHERE transition_id = ?",
                (transition_id,),
            ).fetchone()
        return transition_from_row(row)

    def list_transitions(self, grant_id: str) -> list[ActivationTransition]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM routing_activation_transitions
                WHERE grant_id = ? ORDER BY sequence
                """,
                (grant_id,),
            ).fetchall()
        return [transition_from_row(row) for row in rows]

    # -- internals ----------------------------------------------------------------

    def _fetch_run(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM routing_qualification_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return row

    def _fetch_case(self, conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM routing_qualification_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        return row

    def _fetch_attempt(
        self, conn: sqlite3.Connection, attempt_id: str
    ) -> sqlite3.Row | None:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM routing_qualification_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return row

    def _attempt_draft_context(
        self, conn: sqlite3.Connection, draft: QualificationAttemptDraft
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        case_row = self._fetch_case(conn, draft.case_id)
        if case_row is None:
            raise ValueError(f"unknown qualification case: {draft.case_id}")
        run_row = self._live_run_row(conn, str(case_row["run_id"]))
        scope = self._run_scope_payload(run_row)
        if draft.target_id not in tuple(scope["target_ids"]):
            raise ValueError(
                "attempt target is outside the qualification run scope"
            )
        if draft.reservation.micros > int(run_row["attempt_ceiling_micros"]):
            raise ValueError(
                "attempt reservation exceeds the per-attempt ceiling"
            )
        return case_row, run_row

    def _insert_attempt_row(
        self,
        conn: sqlite3.Connection,
        draft: QualificationAttemptDraft,
        case_row: sqlite3.Row,
        *,
        status: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO routing_qualification_attempts (
                attempt_id, case_id, run_id, attempt_number, status, revision,
                target_id, target_digest, routing_decision_id, routing_lease_id,
                provider_receipts_json, usage_json, reservation_micros,
                actual_cost_micros, unresolved_cost_micros, validation_passed,
                validation_codes_json, failure_category, guardrail_state,
                evidence_refs_json, created_at, updated_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?)
            """,
            (
                draft.attempt_id,
                draft.case_id,
                str(case_row["run_id"]),
                draft.attempt_number,
                status,
                1,
                draft.target_id,
                draft.target_digest,
                draft.routing_decision_id,
                draft.routing_lease_id,
                "[]",
                None,
                draft.reservation.micros,
                None,
                0,
                None,
                "[]",
                None,
                "clear",
                "[]",
                now,
                now,
                None,
                None,
            ),
        )

    def _assert_budget_admission(
        self, run_row: sqlite3.Row, reservation: MoneyMicros
    ) -> None:
        """Fail closed unless the projected spend fits under the hard cap.

        known_actual_spend + unresolved_cost_reserve + admitted_inflight
        + projected_attempt_reserve <= min(immutable_max_spend,
        effective_stop_cap)
        """

        cap = min(
            int(run_row["max_spend_micros"]),
            int(run_row["effective_stop_cap_micros"]),
        )
        projected = (
            int(run_row["actual_spend_micros"])
            + int(run_row["unresolved_reserve_micros"])
            + int(run_row["inflight_reserve_micros"])
            + reservation.micros
        )
        if projected > cap:
            from .qualification_budget import BudgetAdmissionRejected

            raise BudgetAdmissionRejected(
                "hard_cap_exhausted",
                f"projected {projected} micro-USD exceeds hard cap {cap} "
                "micro-USD",
            )

    def _run_row_for_update(
        self, conn: sqlite3.Connection, run_id: str, expected_revision: int
    ) -> sqlite3.Row:
        row = self._fetch_run(conn, run_id)
        if row is None:
            raise ValueError(f"unknown qualification run: {run_id}")
        current_revision = int(row["revision"])
        if current_revision != expected_revision:
            raise QualificationRevisionConflict(
                "qualification_run", run_id, current_revision
            )
        status = str(row["status"])
        if status in TERMINAL_RUN_STATES:
            raise ValueError(f"qualification run {run_id} is terminal ({status})")
        return row

    def _live_run_row(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = self._fetch_run(conn, run_id)
        if row is None:
            raise ValueError(f"unknown qualification run: {run_id}")
        status = str(row["status"])
        if status in TERMINAL_RUN_STATES:
            raise ValueError(
                f"qualification run {run_id} is terminal; evidence append rejected"
            )
        return row

    def _run_scope_payload(self, run_row: sqlite3.Row) -> dict[str, Any]:
        scope = json.loads(str(run_row["scope_json"]))
        if not isinstance(scope, dict):
            raise ValueError("run scope payload must be a JSON object")
        return scope

    def _run_transition(
        self,
        run_id: str,
        *,
        expected_revision: int,
        allowed: tuple[str, ...],
        target: str,
        set_started: bool = False,
    ) -> QualificationRun:
        if target not in RUN_STATES:
            raise ValueError(f"unsupported run status: {target}")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._run_row_for_update(conn, run_id, expected_revision)
            status = str(row["status"])
            if status not in allowed:
                raise ValueError(
                    f"qualification run must be {' or '.join(allowed)} to reach "
                    f"'{target}'; current status is {status}"
                )
            started_clause = ", started_at = ?" if set_started else ""
            params: tuple[Any, ...] = (
                (target, now, now, run_id) if set_started else (target, now, run_id)
            )
            conn.execute(
                f"""
                UPDATE routing_qualification_runs
                SET status = ?, revision = revision + 1, updated_at = ?
                    {started_clause}
                WHERE run_id = ?
                """,
                params,
            )
            updated = self._fetch_run(conn, run_id)
        assert updated is not None
        return run_from_row(updated)

    def _terminalize(
        self,
        run_id: str,
        *,
        expected_revision: int,
        terminal_status: str,
        terminal_reason: str,
        actual_spend: MoneyMicros,
        receipt_payload: Mapping[str, Any],
    ) -> QualificationRun:
        if terminal_status not in TERMINAL_RUN_STATES:
            raise ValueError(f"unsupported terminal status: {terminal_status}")
        _require_text(terminal_reason, "terminal_reason")
        if not isinstance(actual_spend, MoneyMicros):
            raise ValueError("actual_spend must be a MoneyMicros value")
        if not isinstance(receipt_payload, Mapping):
            raise ValueError("terminal receipt payload must be a mapping")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._run_row_for_update(conn, run_id, expected_revision)
            self._insert_receipt_row(
                conn,
                run_id=run_id,
                attempt_id=None,
                receipt_type="run_terminal",
                payload=receipt_payload,
                now=now,
            )
            self._insert_event_row(
                conn,
                run_id,
                f"run_{terminal_status}",
                {"terminal_reason": terminal_reason},
                now,
            )
            conn.execute(
                """
                UPDATE routing_qualification_runs
                SET status = ?, revision = revision + 1, updated_at = ?,
                    finished_at = ?, terminal_reason = ?, actual_spend_micros = ?,
                    unresolved_reserve_micros = 0, inflight_reserve_micros = 0
                WHERE run_id = ?
                """,
                (
                    terminal_status,
                    now,
                    now,
                    terminal_reason,
                    actual_spend.micros,
                    run_id,
                ),
            )
            updated = self._fetch_run(conn, run_id)
        assert updated is not None
        return run_from_row(updated)

    def _attempt_row_for_transition(
        self, conn: sqlite3.Connection, attempt_id: str, allowed: tuple[str, ...], action: str
    ) -> sqlite3.Row:
        row = self._fetch_attempt(conn, attempt_id)
        if row is None:
            raise ValueError(f"unknown qualification attempt: {attempt_id}")
        status = str(row["status"])
        if status in TERMINAL_ATTEMPT_STATES:
            raise ValueError(
                f"qualification attempt {attempt_id} is terminal ({status})"
            )
        if status not in allowed:
            raise ValueError(
                f"qualification attempt must be {' or '.join(allowed)} to "
                f"{action}; current status is {status}"
            )
        return row

    def _attempt_transition(
        self,
        attempt_id: str,
        *,
        allowed: tuple[str, ...],
        target: str,
        set_started: bool = False,
    ) -> QualificationAttempt:
        if target not in ATTEMPT_STATES:
            raise ValueError(f"unsupported attempt status: {target}")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._attempt_row_for_transition(conn, attempt_id, allowed, f"reach '{target}'")
            started_clause = ", started_at = ?" if set_started else ""
            params: tuple[Any, ...] = (
                (target, now, now, attempt_id) if set_started else (target, now, attempt_id)
            )
            conn.execute(
                f"""
                UPDATE routing_qualification_attempts
                SET status = ?, revision = revision + 1, updated_at = ?
                    {started_clause}
                WHERE attempt_id = ?
                """,
                params,
            )
            updated = self._fetch_attempt(conn, str(row["attempt_id"]))
        assert updated is not None
        return attempt_from_row(updated)

    def _attempt_terminalize(
        self,
        attempt_id: str,
        *,
        allowed: tuple[str, ...],
        target: str,
        failure_category: str | None,
        evidence_refs: tuple[str, ...],
        guardrail_violated: bool,
    ) -> QualificationAttempt:
        if target not in TERMINAL_ATTEMPT_STATES:
            raise ValueError(f"unsupported terminal attempt status: {target}")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._attempt_row_for_transition(conn, attempt_id, allowed, f"reach '{target}'")
            conn.execute(
                """
                UPDATE routing_qualification_attempts
                SET status = ?, revision = revision + 1, updated_at = ?,
                    finished_at = ?, failure_category = ?, guardrail_state = ?,
                    evidence_refs_json = ?
                WHERE attempt_id = ?
                """,
                (
                    target,
                    now,
                    now,
                    failure_category,
                    "violated" if guardrail_violated else "clear",
                    canonical_json(list(evidence_refs)),
                    str(row["attempt_id"]),
                ),
            )
            updated = self._fetch_attempt(conn, attempt_id)
        assert updated is not None
        return attempt_from_row(updated)

    def _insert_event_row(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: str,
    ) -> int:
        row = conn.execute(
            "SELECT MAX(sequence) AS max_sequence FROM routing_qualification_events "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        sequence = 1 if row["max_sequence"] is None else int(row["max_sequence"]) + 1
        conn.execute(
            """
            INSERT INTO routing_qualification_events (
                run_id, sequence, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, sequence, event_type, canonical_json(payload), now),
        )
        return sequence

    def _insert_receipt_row(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt_id: str | None,
        receipt_type: str,
        payload: Mapping[str, Any],
        now: str,
    ) -> str:
        payload_json, payload_digest = canonical_payload(payload)
        receipt_id = "rcpt_" + canonical_digest(
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "receipt_type": receipt_type,
                "payload_digest": payload_digest,
            }
        )[:24]
        try:
            conn.execute(
                """
                INSERT INTO routing_qualification_receipts (
                    receipt_id, run_id, attempt_id, receipt_type,
                    payload_json, payload_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    run_id,
                    attempt_id,
                    receipt_type,
                    payload_json,
                    payload_digest,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"receipt already recorded: {receipt_id}") from exc
        return receipt_id
