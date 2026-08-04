from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

from .state_store import SCHEMA_VERSION

_RECOVERY_SCHEMA = "kestrel.desktop.recovery.v1"
_SUPPORT_PREVIEW_SCHEMA = "kestrel.desktop.recovery-support-preview.v1"
_MAX_RECOVERY_ITEMS = 1_000
_ACTIONS = (
    "inspect",
    "export_support_bundle",
    "retry_readiness",
)
_BLOCKING_REASONS = frozenset(
    {
        "payload_verification_failed",
        "profile_conflict",
        "state_incompatible",
        "state_corrupt",
        "memvid_reopen_failed",
        "sidecar_crash_loop",
        "pending_high_risk_approval",
        "ambiguous_provider_attempt",
        "recovery_inspection_unavailable",
        "routing_integrity_key_missing_or_mismatched",
    }
)


class _RecoveryState(Protocol):
    def health_snapshot(self) -> Mapping[str, object]: ...

    def count_pending_high_risk_approvals(
        self,
        *,
        limit: int,
    ) -> int: ...


class _RecoveryRouting(Protocol):
    def count_running_decisions(
        self,
        *,
        limit: int,
    ) -> int: ...


@dataclass(frozen=True)
class DesktopRecoveryReport:
    can_auto_resume: bool
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    actions: tuple[str, ...]
    state_integrity: str
    state_schema_version: int | None
    state_writable: bool
    memory_ready: bool
    pending_high_risk_approvals: int
    ambiguous_provider_attempts: int
    credential_storage_state: str
    schema: str = _RECOVERY_SCHEMA

    def to_public_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "can_auto_resume": self.can_auto_resume,
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "actions": list(self.actions),
            "state": {
                "integrity": self.state_integrity,
                "schema_version": self.state_schema_version,
                "writable": self.state_writable,
            },
            "memory": {"ready": self.memory_ready},
            "approvals": {"pending_high_risk": (self.pending_high_risk_approvals)},
            "routing": {"ambiguous_provider_attempts": (self.ambiguous_provider_attempts)},
            "credential_storage": {"state": self.credential_storage_state},
        }


class DesktopRecoveryService:
    """Read-only Desktop recovery projection over existing authorities."""

    def __init__(
        self,
        *,
        state: _RecoveryState,
        routing: _RecoveryRouting,
        credential_readiness: Callable[[], Mapping[str, object]],
        memory_ready: Callable[[], bool],
        routing_integrity: Callable[[], str] | None = None,
    ) -> None:
        self._state = state
        self._routing = routing
        self._credential_readiness = credential_readiness
        self._memory_ready = memory_ready
        self._routing_integrity = routing_integrity
        self._inspection_lock = Lock()

    def inspect(self) -> DesktopRecoveryReport:
        if not self._inspection_lock.acquire(blocking=False):
            return self.inspection_unavailable_report()
        try:
            return self._inspect_once()
        finally:
            self._inspection_lock.release()

    def _inspect_once(self) -> DesktopRecoveryReport:
        reasons: list[str] = []
        inspection_failed = False

        try:
            health = dict(self._state.health_snapshot())
        except Exception:
            health = {}
            inspection_failed = True
        integrity = _state_integrity(health)
        schema_version = _state_schema_version(health)
        writable = health.get("writable") is True
        if integrity != "ok":
            reasons.append("state_corrupt")
        elif schema_version != SCHEMA_VERSION:
            reasons.append("state_incompatible")

        try:
            memory_ready = self._memory_ready() is True
        except Exception:
            memory_ready = False
            inspection_failed = True
        if not memory_ready:
            reasons.append("memvid_reopen_failed")

        try:
            pending_high_risk = _bounded_count(
                self._state.count_pending_high_risk_approvals(limit=_MAX_RECOVERY_ITEMS)
            )
        except Exception:
            pending_high_risk = 0
            inspection_failed = True
        if pending_high_risk:
            reasons.append("pending_high_risk_approval")

        try:
            ambiguous_provider_attempts = _bounded_count(
                self._routing.count_running_decisions(limit=_MAX_RECOVERY_ITEMS)
            )
        except Exception:
            ambiguous_provider_attempts = 0
            inspection_failed = True
        if ambiguous_provider_attempts:
            reasons.append("ambiguous_provider_attempt")

        try:
            credential_state = _credential_state(self._credential_readiness())
        except Exception:
            credential_state = "unavailable"
            inspection_failed = True
        if credential_state in {
            "locked_vault_required",
            "unavailable",
        }:
            reasons.append("credential_backend_unavailable")

        # Read-only probe: recovery reports key problems but never generates
        # new key material over existing signed receipts.
        if self._routing_integrity is not None:
            try:
                routing_integrity_state = str(self._routing_integrity())
            except Exception:
                routing_integrity_state = "unavailable"
                inspection_failed = True
            if routing_integrity_state != "ok":
                reasons.append("routing_integrity_key_missing_or_mismatched")

        if inspection_failed:
            reasons.append("recovery_inspection_unavailable")
        ordered_reasons = _unique(reasons)
        blockers = tuple(reason for reason in ordered_reasons if reason in _BLOCKING_REASONS)
        return DesktopRecoveryReport(
            can_auto_resume=not blockers,
            reasons=ordered_reasons,
            blockers=blockers,
            actions=_ACTIONS,
            state_integrity=integrity,
            state_schema_version=schema_version,
            state_writable=writable,
            memory_ready=memory_ready,
            pending_high_risk_approvals=pending_high_risk,
            ambiguous_provider_attempts=ambiguous_provider_attempts,
            credential_storage_state=credential_state,
        )

    def retry_readiness(self) -> DesktopRecoveryReport:
        """Repeat only the same read-only inspection."""

        return self.inspect()

    def inspection_unavailable_report(self) -> DesktopRecoveryReport:
        """Return a fixed fail-closed report without touching authorities."""

        return DesktopRecoveryReport(
            can_auto_resume=False,
            reasons=("recovery_inspection_unavailable",),
            blockers=("recovery_inspection_unavailable",),
            actions=_ACTIONS,
            state_integrity="error",
            state_schema_version=None,
            state_writable=False,
            memory_ready=False,
            pending_high_risk_approvals=0,
            ambiguous_provider_attempts=0,
            credential_storage_state="unavailable",
        )

    def support_bundle_preview(
        self,
        report: DesktopRecoveryReport | None = None,
    ) -> dict[str, Any]:
        active_report = report or self.inspect()
        return {
            "schema": _SUPPORT_PREVIEW_SCHEMA,
            "redacted": True,
            "entries": [
                "recovery.json",
                "state-summary.json",
                "routing-summary.json",
                "credential-storage.json",
            ],
            "excluded": [
                "raw_secrets",
                "request_payloads",
                "provider_content",
                "approval_arguments",
                "filesystem_paths",
            ],
            "recovery": active_report.to_public_payload(),
        }


def _state_integrity(health: Mapping[str, object]) -> str:
    value = health.get("integrity")
    return value if value in {"ok", "error"} else "error"


def _state_schema_version(
    health: Mapping[str, object],
) -> int | None:
    value = health.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _credential_state(
    readiness: Mapping[str, object],
) -> str:
    value = readiness.get("state")
    if value in {
        "available",
        "session_only",
        "locked_vault_required",
        "unavailable",
    }:
        return str(value)
    return "unavailable"


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _bounded_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("recovery count must be a non-negative integer")
    return min(value, _MAX_RECOVERY_ITEMS)
