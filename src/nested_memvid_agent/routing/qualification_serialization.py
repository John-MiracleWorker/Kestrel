"""Row serialization helpers for the Flock qualification ledger (schema v4).

Every stored JSON column is canonical JSON and every ``*_digest`` column is
the SHA-256 digest of that exact canonical payload. Evidence references and
validation codes are bounded so rows stay small and replayable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from .qualification_digest import canonical_digest, canonical_json
from .qualification_models import MoneyMicros
from .qualification_records import (
    ActivationGrant,
    ActivationTransition,
    QualificationAttempt,
    QualificationCase,
    QualificationEvent,
    QualificationReceipt,
    QualificationRun,
)

__all__ = [
    "attempt_from_row",
    "bounded_evidence_refs",
    "bounded_validation_codes",
    "canonical_payload",
    "case_from_row",
    "event_from_row",
    "grant_from_row",
    "receipt_from_row",
    "run_from_row",
    "transition_from_row",
]

_MAX_EVIDENCE_REFS = 32
_MAX_EVIDENCE_REF_LENGTH = 256
_MAX_VALIDATION_CODES = 32


def canonical_payload(payload: Any) -> tuple[str, str]:
    """Return ``(canonical_json_text, sha256_digest)`` for *payload*."""

    text = canonical_json(payload)
    return text, canonical_digest(payload)


def _require_payload_object(raw: str, name: str) -> Mapping[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must store a JSON object")
    return value


def bounded_evidence_refs(refs: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if len(refs) > _MAX_EVIDENCE_REFS:
        raise ValueError(f"evidence references are bounded to {_MAX_EVIDENCE_REFS} entries")
    bounded: list[str] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("evidence references must be non-empty strings")
        if len(ref) > _MAX_EVIDENCE_REF_LENGTH:
            raise ValueError(
                f"evidence references are bounded to {_MAX_EVIDENCE_REF_LENGTH} characters"
            )
        bounded.append(ref)
    return tuple(bounded)


def bounded_validation_codes(codes: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if len(codes) > _MAX_VALIDATION_CODES:
        raise ValueError(f"validation codes are bounded to {_MAX_VALIDATION_CODES} entries")
    for code in codes:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("validation codes must be non-empty strings")
    return tuple(codes)


def _money(value: Any, name: str) -> MoneyMicros:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be stored as a non-negative integer micro-USD value")
    return MoneyMicros(value)


def _money_or_none(value: Any, name: str) -> MoneyMicros | None:
    if value is None:
        return None
    return _money(value, name)


def _str_tuple(raw: str, name: str) -> tuple[str, ...]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must store a JSON string array")
    return tuple(value)


def _payload_tuple(raw: str, name: str) -> tuple[Mapping[str, Any], ...]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{name} must store a JSON object array")
    return tuple(value)


def run_from_row(row: sqlite3.Row) -> QualificationRun:
    return QualificationRun(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        owner_principal=str(row["owner_principal"]),
        scope_json=str(row["scope_json"]),
        scope_digest=str(row["scope_digest"]),
        corpus_json=str(row["corpus_json"]),
        corpus_digest=str(row["corpus_digest"]),
        target_json=str(row["target_json"]),
        target_digest=str(row["target_digest"]),
        price_json=str(row["price_json"]),
        price_digest=str(row["price_digest"]),
        policy_json=str(row["policy_json"]),
        policy_digest=str(row["policy_digest"]),
        learned_json=str(row["learned_json"]),
        learned_digest=str(row["learned_digest"]),
        project_authority_json=str(row["project_authority_json"]),
        project_authority_digest=str(row["project_authority_digest"]),
        build_json=str(row["build_json"]),
        build_digest=str(row["build_digest"]),
        thresholds_json=str(row["thresholds_json"]),
        thresholds_digest=str(row["thresholds_digest"]),
        max_spend=_money(row["max_spend_micros"], "max_spend_micros"),
        effective_stop_cap=_money(row["effective_stop_cap_micros"], "effective_stop_cap_micros"),
        actual_spend=_money(row["actual_spend_micros"], "actual_spend_micros"),
        unresolved_reserve=_money(row["unresolved_reserve_micros"], "unresolved_reserve_micros"),
        inflight_reserve=_money(row["inflight_reserve_micros"], "inflight_reserve_micros"),
        attempt_ceiling=_money(row["attempt_ceiling_micros"], "attempt_ceiling_micros"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        terminal_reason=row["terminal_reason"],
    )


def case_from_row(row: sqlite3.Row) -> QualificationCase:
    return QualificationCase(
        case_id=str(row["case_id"]),
        run_id=str(row["run_id"]),
        item_id=str(row["item_id"]),
        task_family=str(row["task_family"]),
        risk=str(row["risk"]),
        task_contract_digest=str(row["task_contract_digest"]),
        acceptance_plan_digest=str(row["acceptance_plan_digest"]),
        repository_digest=str(row["repository_digest"]),
        privacy_eligible=bool(row["privacy_eligible"]),
        scope_digest=str(row["scope_digest"]),
        created_at=str(row["created_at"]),
    )


def attempt_from_row(row: sqlite3.Row) -> QualificationAttempt:
    usage_raw = row["usage_json"]
    return QualificationAttempt(
        attempt_id=str(row["attempt_id"]),
        case_id=str(row["case_id"]),
        run_id=str(row["run_id"]),
        attempt_number=int(row["attempt_number"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        target_id=str(row["target_id"]),
        target_digest=str(row["target_digest"]),
        routing_decision_id=row["routing_decision_id"],
        routing_lease_id=row["routing_lease_id"],
        provider_receipts=_payload_tuple(str(row["provider_receipts_json"]), "provider_receipts"),
        usage=None if usage_raw is None else _require_payload_object(str(usage_raw), "usage"),
        reservation=_money(row["reservation_micros"], "reservation_micros"),
        actual_cost=_money_or_none(row["actual_cost_micros"], "actual_cost_micros"),
        unresolved_cost=_money(row["unresolved_cost_micros"], "unresolved_cost_micros"),
        validation_passed=(
            None if row["validation_passed"] is None else bool(row["validation_passed"])
        ),
        validation_codes=_str_tuple(str(row["validation_codes_json"]), "validation_codes"),
        failure_category=row["failure_category"],
        guardrail_state=str(row["guardrail_state"]),
        evidence_refs=_str_tuple(str(row["evidence_refs_json"]), "evidence_refs"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def event_from_row(row: sqlite3.Row) -> QualificationEvent:
    return QualificationEvent(
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),
        payload=_require_payload_object(str(row["payload_json"]), "event payload"),
        created_at=str(row["created_at"]),
    )


def receipt_from_row(row: sqlite3.Row) -> QualificationReceipt:
    return QualificationReceipt(
        receipt_id=str(row["receipt_id"]),
        run_id=str(row["run_id"]),
        attempt_id=row["attempt_id"],
        receipt_type=str(row["receipt_type"]),
        payload=_require_payload_object(str(row["payload_json"]), "receipt payload"),
        payload_digest=str(row["payload_digest"]),
        created_at=str(row["created_at"]),
    )


def grant_from_row(row: sqlite3.Row) -> ActivationGrant:
    return ActivationGrant(
        grant_id=str(row["grant_id"]),
        run_id=str(row["run_id"]),
        target_id=str(row["target_id"]),
        scope_json=str(row["scope_json"]),
        scope_digest=str(row["scope_digest"]),
        policy_id=str(row["policy_id"]),
        policy_revision=int(row["policy_revision"]),
        qualification_receipt_id=str(row["qualification_receipt_id"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def transition_from_row(row: sqlite3.Row) -> ActivationTransition:
    return ActivationTransition(
        transition_id=str(row["transition_id"]),
        grant_id=str(row["grant_id"]),
        sequence=int(row["sequence"]),
        transition_type=str(row["transition_type"]),
        reason=str(row["reason"]),
        receipt_id=row["receipt_id"],
        created_at=str(row["created_at"]),
    )
