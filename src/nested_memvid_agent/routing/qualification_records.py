"""Records and drafts for the Flock qualification ledger (schema v4).

Money is carried as integer micro-USD via :class:`MoneyMicros`. Run and
attempt state machines use the exact state vocabularies from the Adaptive
Flock plan; receipts, activation grant base rows, and activation transitions
are immutable once written (enforced by database triggers).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    QualificationScope,
    QualificationThresholds,
)

__all__ = [
    "ATTEMPT_STATES",
    "GRANT_TRANSITION_TYPES",
    "RUN_STATES",
    "TERMINAL_ATTEMPT_STATES",
    "TERMINAL_RUN_STATES",
    "ActivationGrant",
    "ActivationGrantDraft",
    "ActivationTransition",
    "QualificationAttempt",
    "QualificationAttemptDraft",
    "QualificationCase",
    "QualificationCaseDraft",
    "QualificationEvent",
    "QualificationReceipt",
    "QualificationRevisionConflict",
    "QualificationRun",
    "QualificationRunDraft",
]

RUN_STATES: tuple[str, ...] = (
    "draft",
    "ready",
    "running",
    "pausing",
    "paused",
    "cancelled",
    "failed",
    "completed",
)
TERMINAL_RUN_STATES: tuple[str, ...] = ("cancelled", "failed", "completed")

ATTEMPT_STATES: tuple[str, ...] = (
    "pending",
    "reserved",
    "running",
    "completed",
    "failed",
    "cancelled",
    "ambiguous",
)
TERMINAL_ATTEMPT_STATES: tuple[str, ...] = (
    "completed",
    "failed",
    "cancelled",
    "ambiguous",
)

GRANT_TRANSITION_TYPES: tuple[str, ...] = (
    "activated",
    "suspended",
    "resumed",
    "revoked",
)


class QualificationRevisionConflict(RuntimeError):
    def __init__(self, resource: str, resource_id: str, current_revision: int) -> None:
        self.resource = resource
        self.resource_id = resource_id
        self.current_revision = current_revision
        super().__init__(f"{resource}_revision_conflict:{resource_id}:{current_revision}")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _require_money(value: MoneyMicros, name: str) -> None:
    if not isinstance(value, MoneyMicros):
        raise ValueError(f"{name} must be a MoneyMicros value")


def _require_payload(value: Mapping[str, Any], name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping payload")


@dataclass(frozen=True)
class QualificationRunDraft:
    """Owner-bound, digest-bound qualification run specification."""

    run_id: str
    owner_principal: str
    scope: QualificationScope
    corpus: CorpusManifest
    thresholds: QualificationThresholds
    target_snapshot: Mapping[str, Any]
    price_snapshot: Mapping[str, Any]
    policy_payload: Mapping[str, Any]
    learned_payload: Mapping[str, Any]
    project_authority: Mapping[str, Any]
    build: Mapping[str, Any]
    max_spend: MoneyMicros
    effective_stop_cap: MoneyMicros
    attempt_ceiling: MoneyMicros

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        _require_text(self.owner_principal, "owner_principal")
        if not isinstance(self.scope, QualificationScope):
            raise ValueError("scope must be a QualificationScope")
        if not isinstance(self.corpus, CorpusManifest):
            raise ValueError("corpus must be a CorpusManifest")
        if not isinstance(self.thresholds, QualificationThresholds):
            raise ValueError("thresholds must be a QualificationThresholds")
        for name in (
            "target_snapshot",
            "price_snapshot",
            "policy_payload",
            "learned_payload",
            "project_authority",
            "build",
        ):
            _require_payload(getattr(self, name), name)
        _require_money(self.max_spend, "max_spend")
        _require_money(self.effective_stop_cap, "effective_stop_cap")
        _require_money(self.attempt_ceiling, "attempt_ceiling")
        if self.effective_stop_cap.micros > self.max_spend.micros:
            raise ValueError("effective stop cap cannot exceed the immutable max spend")
        if self.attempt_ceiling.micros > self.max_spend.micros:
            raise ValueError("per-attempt ceiling cannot exceed the immutable max spend")


@dataclass(frozen=True)
class QualificationRun:
    run_id: str
    status: str
    revision: int
    owner_principal: str
    scope_json: str
    scope_digest: str
    corpus_json: str
    corpus_digest: str
    target_json: str
    target_digest: str
    price_json: str
    price_digest: str
    policy_json: str
    policy_digest: str
    learned_json: str
    learned_digest: str
    project_authority_json: str
    project_authority_digest: str
    build_json: str
    build_digest: str
    thresholds_json: str
    thresholds_digest: str
    max_spend: MoneyMicros
    effective_stop_cap: MoneyMicros
    actual_spend: MoneyMicros
    unresolved_reserve: MoneyMicros
    inflight_reserve: MoneyMicros
    attempt_ceiling: MoneyMicros
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    terminal_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATES


@dataclass(frozen=True)
class QualificationCaseDraft:
    case_id: str
    run_id: str
    item: CorpusItem
    repository_digest: str
    privacy_eligible: bool

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.run_id, "run_id")
        if not isinstance(self.item, CorpusItem):
            raise ValueError("item must be a CorpusItem")
        _require_text(self.repository_digest, "repository_digest")
        if not isinstance(self.privacy_eligible, bool):
            raise ValueError("privacy_eligible must be a boolean")


@dataclass(frozen=True)
class QualificationCase:
    case_id: str
    run_id: str
    item_id: str
    task_family: str
    risk: str
    task_contract_digest: str
    acceptance_plan_digest: str
    repository_digest: str
    privacy_eligible: bool
    scope_digest: str
    created_at: str


@dataclass(frozen=True)
class QualificationAttemptDraft:
    attempt_id: str
    case_id: str
    attempt_number: int
    target_id: str
    target_digest: str
    reservation: MoneyMicros
    routing_decision_id: str | None = None
    routing_lease_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.case_id, "case_id")
        if isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise ValueError("attempt_number must be a positive integer")
        _require_text(self.target_id, "target_id")
        _require_text(self.target_digest, "target_digest")
        _require_money(self.reservation, "reservation")


@dataclass(frozen=True)
class QualificationAttempt:
    attempt_id: str
    case_id: str
    run_id: str
    attempt_number: int
    status: str
    revision: int
    target_id: str
    target_digest: str
    routing_decision_id: str | None
    routing_lease_id: str | None
    provider_receipts: tuple[Mapping[str, Any], ...]
    usage: Mapping[str, Any] | None
    reservation: MoneyMicros
    actual_cost: MoneyMicros | None
    unresolved_cost: MoneyMicros
    validation_passed: bool | None
    validation_codes: tuple[str, ...]
    failure_category: str | None
    guardrail_state: str
    evidence_refs: tuple[str, ...]
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ATTEMPT_STATES


@dataclass(frozen=True)
class QualificationEvent:
    run_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True)
class QualificationReceipt:
    receipt_id: str
    run_id: str
    attempt_id: str | None
    receipt_type: str
    payload: Mapping[str, Any]
    payload_digest: str
    created_at: str


@dataclass(frozen=True)
class ActivationGrantDraft:
    grant_id: str
    run_id: str
    target_id: str
    qualification_receipt_id: str
    created_by: str

    def __post_init__(self) -> None:
        _require_text(self.grant_id, "grant_id")
        _require_text(self.run_id, "run_id")
        _require_text(self.target_id, "target_id")
        _require_text(self.qualification_receipt_id, "qualification_receipt_id")
        _require_text(self.created_by, "created_by")


@dataclass(frozen=True)
class ActivationGrant:
    grant_id: str
    run_id: str
    target_id: str
    scope_json: str
    scope_digest: str
    policy_id: str
    policy_revision: int
    qualification_receipt_id: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class ActivationTransition:
    transition_id: str
    grant_id: str
    sequence: int
    transition_type: str
    reason: str
    receipt_id: str | None
    created_at: str
