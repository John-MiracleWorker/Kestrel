from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .state_store import SCHEMA_VERSION

_RECOVERY_SCHEMA = "kestrel.desktop.recovery.v1"
_SUPPORT_PREVIEW_SCHEMA = (
    "kestrel.desktop.recovery-support-preview.v1"
)
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
    }
)


class _RecoveryState(Protocol):
    def health_snapshot(self) -> Mapping[str, object]: ...

    def list_approvals(
        self,
        status: str | None = None,
        *,
        expire: bool = True,
    ) -> Sequence[Mapping[str, object]]: ...


class _RecoveryRouting(Protocol):
    def list_unsettled_decisions(
        self,
        *,
        limit: int,
    ) -> Sequence[Any]: ...


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
            "approvals": {
                "pending_high_risk": (
                    self.pending_high_risk_approvals
                )
            },
            "routing": {
                "ambiguous_provider_attempts": (
                    self.ambiguous_provider_attempts
                )
            },
            "credential_storage": {
                "state": self.credential_storage_state
            },
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
    ) -> None:
        self._state = state
        self._routing = routing
        self._credential_readiness = credential_readiness
        self._memory_ready = memory_ready

    def inspect(self) -> DesktopRecoveryReport:
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
            approvals = self._state.list_approvals(
                status="pending",
                expire=False,
            )
            pending_high_risk = min(
                _MAX_RECOVERY_ITEMS,
                sum(
                    1
                    for approval in approvals
                    if str(approval.get("status", "")) == "pending"
                    and str(approval.get("risk", "")).lower()
                    in {"high", "critical"}
                ),
            )
        except Exception:
            pending_high_risk = 0
            inspection_failed = True
        if pending_high_risk:
            reasons.append("pending_high_risk_approval")

        try:
            unsettled = self._routing.list_unsettled_decisions(
                limit=_MAX_RECOVERY_ITEMS
            )
            ambiguous_provider_attempts = min(
                _MAX_RECOVERY_ITEMS,
                sum(
                    1
                    for decision in unsettled
                    if str(getattr(decision, "status", "")) == "running"
                ),
            )
        except Exception:
            ambiguous_provider_attempts = 0
            inspection_failed = True
        if ambiguous_provider_attempts:
            reasons.append("ambiguous_provider_attempt")

        try:
            credential_state = _credential_state(
                self._credential_readiness()
            )
        except Exception:
            credential_state = "unavailable"
            inspection_failed = True
        if credential_state in {
            "locked_vault_required",
            "unavailable",
        }:
            reasons.append("credential_backend_unavailable")

        if inspection_failed:
            reasons.append("recovery_inspection_unavailable")
        ordered_reasons = _unique(reasons)
        blockers = tuple(
            reason
            for reason in ordered_reasons
            if reason in _BLOCKING_REASONS
        )
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

    def support_bundle_preview(self) -> dict[str, Any]:
        report = self.inspect()
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
            "recovery": report.to_public_payload(),
        }


def _state_integrity(health: Mapping[str, object]) -> str:
    value = health.get("integrity")
    return value if value in {"ok", "error"} else "error"


def _state_schema_version(
    health: Mapping[str, object],
) -> int | None:
    value = health.get("schema_version")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
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
