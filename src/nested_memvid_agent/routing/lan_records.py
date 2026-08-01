"""Typed durable records for explicit private-LAN discovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

LanScanStatus = Literal[
    "draft",
    "running",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
    "interrupted",
]
LanObservationSource = Literal["mdns", "active", "manual"]

SCAN_STATES = frozenset(
    {"draft", "running", "cancelling", "cancelled", "completed", "failed", "interrupted"}
)
TERMINAL_SCAN_STATES = frozenset({"cancelled", "completed", "failed", "interrupted"})
ALLOWED_SCAN_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"running", "cancelled"}),
    "running": frozenset({"cancelling", "completed", "failed", "interrupted"}),
    "cancelling": frozenset({"cancelled", "failed", "interrupted"}),
    "cancelled": frozenset(),
    "completed": frozenset(),
    "failed": frozenset(),
    "interrupted": frozenset(),
}


class LanScanRevisionConflict(RuntimeError):
    def __init__(self, scan_id: str, current_revision: int) -> None:
        self.scan_id = scan_id
        self.current_revision = current_revision
        super().__init__(f"lan_scan_revision_conflict:{scan_id}:{current_revision}")


class LanScanTransitionError(ValueError):
    def __init__(self, scan_id: str, current_status: str, requested_status: str) -> None:
        self.scan_id = scan_id
        self.current_status = current_status
        self.requested_status = requested_status
        super().__init__(
            f"lan_scan_transition_not_allowed:{scan_id}:"
            f"{current_status}_to_{requested_status}"
        )


@dataclass(frozen=True)
class LanScanRecord:
    scan_id: str
    status: LanScanStatus
    revision: int
    owner_principal: str
    confirmed_interface_id: str
    network: str
    limits: dict[str, Any]
    limits_digest: str
    preview_digest: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    cancel_reason: str | None = None
    terminal_reason: str | None = None
    candidate_count: int | None = None
    error_count: int | None = None
    timeout_count: int | None = None
    terminal_receipt: dict[str, Any] | None = None
    terminal_receipt_digest: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SCAN_STATES

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LanObservationDraft:
    endpoint_id: str
    source: LanObservationSource
    interface_id: str
    address: str
    port: int
    api_shape: str | None
    tls_enabled: bool
    certificate_sha256: str | None
    catalog_digest: str | None
    capability_digest: str | None
    public_payload: dict[str, Any]
    freshness_timestamp: str
    error_category: str | None


@dataclass(frozen=True)
class LanObservationRecord:
    scan_id: str
    endpoint_id: str
    source: LanObservationSource
    interface_id: str
    address: str
    port: int
    api_shape: str | None
    tls_enabled: bool
    certificate_sha256: str | None
    catalog_digest: str | None
    capability_digest: str | None
    public_payload: dict[str, Any]
    freshness_timestamp: str
    error_category: str | None
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LanScanEvent:
    scan_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)
