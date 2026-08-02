"""Owner-controlled lifecycle orchestration for explicit private-LAN scans.

The manager is deliberately inert at construction time.  The primary runtime
starts it only after acquiring profile ownership and gives it the single
lifespan-owned executor shared by the controller and its bounded probe work.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import ipaddress
import json
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Generator, Iterable
from concurrent.futures import Executor, Future
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import islice
from math import isfinite
from uuid import uuid4

from nested_memvid_agent.lan_discovery_models import (
    TOTAL_SCAN_DEADLINE_SECONDS,
    LanScanLimits,
    LanScanPreview,
    NetworkInterface,
)
from nested_memvid_agent.lan_discovery_scope import (
    PrivateScanScope,
    enumerate_private_interfaces,
    preview_private_scope,
)
from nested_memvid_agent.lan_mdns import (
    MdnsAvailability,
    MdnsCollection,
    collect_mdns_candidates,
)
from nested_memvid_agent.lan_scanner import (
    LanEndpointObservation,
    LanFailureCategory,
    LanScanProgress,
    ScanCancellation,
    scan_lan_scope,
)
from nested_memvid_agent.routing.lan_ledger import (
    LanDiscoveryLedger,
    LanScanObservationPage,
)
from nested_memvid_agent.routing.lan_records import (
    LanObservationDraft,
    LanScanEvent,
    LanScanRecord,
)
from nested_memvid_agent.routing.lan_serialization import (
    LAN_SCAN_PREVIEW_EVENT_SCHEMA,
    lan_observation_to_draft,
)

LAN_OWNER_PRINCIPAL = "owner:local-runtime:v1"
LAN_PREVIEW_TTL_SECONDS = 30.0
LAN_PREVIEW_CONTRACT_VERSION = "kestrel.lan.preview-authorization.v1"
LAN_SERVER_VERSION = "kestrel-local-runtime-v1"
_PREVIEW_DIGEST_SCHEMA = "kestrel.lan.preview-authorization.v1"
_MAX_INTERFACE_COUNT = 64
_MAX_INTERFACE_ADDRESS_COUNT = 64
_MAX_INTERFACE_DISPLAY_NAME_BYTES = 256
_PRIVATE_IPV4_INTERFACE_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_PRIVATE_IPV6_INTERFACE_NETWORKS = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)
_TIMEOUT_FAILURES = frozenset(
    {
        LanFailureCategory.TCP_TIMEOUT,
        LanFailureCategory.HTTP_TIMEOUT,
        LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
    }
)


class LanPreviewAuthorizationError(ValueError):
    """A preview token is stale, foreign to this process, or no longer exact."""


class LanScanAdmissionConflict(RuntimeError):
    """Another scan already owns the one active slot for the local owner."""


@dataclass(frozen=True)
class LanPreviewAuthorization:
    owner_principal: str
    preview: LanScanPreview
    preview_digest: str
    server_version: str
    contract_version: str
    mdns_availability: MdnsAvailability
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class _AuthorizationContext:
    authorization: LanPreviewAuthorization
    interface: NetworkInterface
    inventory_authority: tuple[tuple[str, str, tuple[str, ...]], ...]
    bound_scan_id: str | None = None


@dataclass
class _ActiveScan:
    scan_id: str
    scope: PrivateScanScope
    authorization: LanPreviewAuthorization
    cancellation: ScanCancellation
    revision: int
    absolute_deadline: float
    future: Future[None] | None = None
    cleanup_handles: list[object] = field(default_factory=list)
    observations: dict[str, LanObservationDraft] = field(default_factory=dict)
    observation_failures: dict[str, LanFailureCategory | None] = field(default_factory=dict)
    planned_count: int = 0
    admitted_count: int = 0
    completed_count: int = 0
    progress_seen: bool = False
    mdns_status: MdnsAvailability = MdnsAvailability.UNAVAILABLE
    controller_finished: threading.Event = field(default_factory=threading.Event)
    pending_failure: str | None = None
    progress_persistence_failed: bool = False


def canonical_scan_limits() -> dict[str, object]:
    """Return a fresh canonical ledger projection of the fixed LAN limits."""

    return asdict(LanScanLimits())


def preview_authorization_digest(
    *,
    owner_principal: str,
    interface: NetworkInterface,
    preview: LanScanPreview,
    mdns_availability: MdnsAvailability,
    server_version: str,
    contract_version: str,
    expires_at: datetime,
) -> str:
    """Bind every server-owned field that grants one short-lived scan preview."""

    if type(interface) is not NetworkInterface:
        raise ValueError("LAN preview requires an exact network interface")
    if type(preview) is not LanScanPreview:
        raise ValueError("LAN preview requires an exact typed preview")
    if type(mdns_availability) is not MdnsAvailability:
        raise ValueError("LAN preview mDNS status is invalid")
    payload = {
        "schema": _PREVIEW_DIGEST_SCHEMA,
        "owner_principal": owner_principal,
        "interface": {
            "interface_id": interface.interface_id,
            "os_identity": interface.os_identity,
            "addresses": list(interface.addresses),
        },
        "network": preview.network,
        "limits": asdict(preview.limits),
        "active_host_count": preview.active_host_count,
        "passive_or_manual_only": preview.passive_or_manual_only,
        "port_count": len(preview.port_matrix),
        "mdns_status": mdns_availability.value,
        "server_version": server_version,
        "contract_version": contract_version,
        "expires_at": _utc_text(expires_at),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class LanScanManager:
    """Coordinate durable LAN scan authority without importing discovered targets."""

    def __init__(
        self,
        *,
        ledger: LanDiscoveryLedger,
        interface_enumerator: Callable[[], Iterable[NetworkInterface]] | None = None,
        mdns_availability: Callable[[], MdnsAvailability] | None = None,
        mdns_collector: Callable[..., MdnsCollection] | None = None,
        scanner: Callable[..., tuple[LanEndpointObservation, ...]] | None = None,
        utc_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        scan_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if type(ledger) is not LanDiscoveryLedger:
            raise TypeError("LAN manager requires an exact discovery ledger")
        self._ledger = ledger
        self._interface_enumerator = interface_enumerator or enumerate_private_interfaces
        self._mdns_availability = mdns_availability or _default_mdns_availability
        self._mdns_collector = mdns_collector or collect_mdns_candidates
        self._scanner = scanner or scan_lan_scope
        self._utc_clock = utc_clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._scan_id_factory = scan_id_factory or (lambda: f"lan_{uuid4().hex}")
        self._lock = threading.RLock()
        self._authorizations: dict[int, _AuthorizationContext] = {}
        self._active_scans: dict[str, _ActiveScan] = {}
        self._executor: Executor | None = None
        self._executor_shutdown = False
        self._lifecycle_started = False
        self._admission_open = False
        self._shutdown_requested = False

    def start_lifecycle(self, executor: Executor) -> list[LanScanRecord]:
        """Recover prior-process scans, then open admission on the supplied pool."""

        if not callable(getattr(executor, "submit", None)) or not callable(
            getattr(executor, "shutdown", None)
        ):
            raise TypeError("LAN lifecycle requires an executor")
        with self._lock:
            if self._shutdown_requested:
                executor.shutdown(wait=True, cancel_futures=False)
                raise RuntimeError("LAN lifecycle is shut down")
            if self._lifecycle_started or self._executor is not None:
                raise RuntimeError("LAN lifecycle already started")
            self._executor = executor
            self._executor_shutdown = False
            try:
                interrupted = self._ledger.interrupt_active_scans(
                    owner_principal=LAN_OWNER_PRINCIPAL
                )
            except BaseException:
                self._executor = None
                executor.shutdown(wait=True, cancel_futures=False)
                self._executor_shutdown = True
                raise
            self._lifecycle_started = True
            self._admission_open = True
            return interrupted

    def interfaces(self) -> tuple[NetworkInterface, ...]:
        """Return one bounded, canonical inventory under the operation lock."""

        with self._lock:
            self._require_admission()
            return self._canonical_inventory()

    def preview(self, interface_id: str, network: str) -> LanPreviewAuthorization:
        with self._lock:
            self._require_admission()
            inventory = self._canonical_inventory()
            preview = preview_private_scope(interface_id, network, interfaces=inventory)
            selected = next(item for item in inventory if item.interface_id == preview.interface_id)
            mdns_status = self._mdns_availability()
            if (
                mdns_status
                not in {
                    MdnsAvailability.AVAILABLE,
                    MdnsAvailability.UNAVAILABLE,
                }
                or type(mdns_status) is not MdnsAvailability
            ):
                raise ValueError("LAN preview mDNS availability must be available or unavailable")
            issued_at = self._now_utc()
            expires_at = issued_at + timedelta(seconds=LAN_PREVIEW_TTL_SECONDS)
            digest = preview_authorization_digest(
                owner_principal=LAN_OWNER_PRINCIPAL,
                interface=selected,
                preview=preview,
                mdns_availability=mdns_status,
                server_version=LAN_SERVER_VERSION,
                contract_version=LAN_PREVIEW_CONTRACT_VERSION,
                expires_at=expires_at,
            )
            authorization = LanPreviewAuthorization(
                owner_principal=LAN_OWNER_PRINCIPAL,
                preview=preview,
                preview_digest=digest,
                server_version=LAN_SERVER_VERSION,
                contract_version=LAN_PREVIEW_CONTRACT_VERSION,
                mdns_availability=mdns_status,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            self._prune_authorizations_locked(issued_at)
            self._authorizations.clear()
            self._authorizations[id(authorization)] = _AuthorizationContext(
                authorization=authorization,
                interface=selected,
                inventory_authority=_inventory_authority(inventory),
            )
            return authorization

    def create_draft(self, authorization: LanPreviewAuthorization) -> LanScanRecord:
        with self._lock:
            self._require_admission()
            context = self._validate_authorization(authorization)
            return self._ledger.create_scan(
                scan_id=self._scan_id_factory(),
                owner_principal=LAN_OWNER_PRINCIPAL,
                confirmed_interface_id=context.interface.interface_id,
                network=authorization.preview.network,
                limits=canonical_scan_limits(),
                preview_digest=authorization.preview_digest,
                expected_revision=0,
            )

    def create_draft_for_preview(
        self,
        preview_digest: str,
        *,
        expected_revision: int,
    ) -> LanScanRecord:
        """Create the single draft bound to the current retained preview digest."""

        with self._lock:
            self._require_admission()
            context = self._authorization_for_digest_locked(preview_digest)
            if context.bound_scan_id is not None:
                raise LanPreviewAuthorizationError("LAN preview already created a draft")
            authorization = context.authorization
            draft = self._ledger.create_scan(
                scan_id=self._scan_id_factory(),
                owner_principal=LAN_OWNER_PRINCIPAL,
                confirmed_interface_id=context.interface.interface_id,
                network=authorization.preview.network,
                limits=canonical_scan_limits(),
                preview_digest=authorization.preview_digest,
                expected_revision=expected_revision,
            )
            self._authorizations[id(authorization)] = replace(
                context,
                bound_scan_id=draft.scan_id,
            )
            return draft

    def start(
        self,
        scan_id: str,
        *,
        expected_revision: int,
        authorization: LanPreviewAuthorization,
        preview_digest: str,
    ) -> LanScanRecord:
        with self._lock:
            return self._start_locked(
                scan_id,
                expected_revision=expected_revision,
                authorization=authorization,
                preview_digest=preview_digest,
                consume_authorization=False,
            )

    def start_for_preview(
        self,
        scan_id: str,
        *,
        expected_revision: int,
        preview_digest: str,
    ) -> LanScanRecord:
        """Start only the draft bound to the exact live route preview authority."""

        with self._lock:
            self._require_admission()
            context = self._authorization_for_digest_locked(preview_digest)
            if context.bound_scan_id != scan_id:
                raise LanPreviewAuthorizationError("LAN preview is bound to another draft")
            return self._start_locked(
                scan_id,
                expected_revision=expected_revision,
                authorization=context.authorization,
                preview_digest=preview_digest,
                consume_authorization=True,
            )

    def cancel(
        self,
        scan_id: str,
        *,
        expected_revision: int,
    ) -> LanScanRecord:
        with self._lock:
            self._require_started()
            cancelled = self._ledger.request_scan_cancel(
                scan_id,
                owner_principal=LAN_OWNER_PRINCIPAL,
                expected_revision=expected_revision,
                cancel_reason="owner_cancelled",
            )
            handle = self._active_scans.get(scan_id)
            if handle is not None:
                handle.revision = cancelled.revision
                handle.cancellation.cancel()
            for key, context in tuple(self._authorizations.items()):
                if context.bound_scan_id == scan_id:
                    self._authorizations.pop(key, None)
            return cancelled

    def get(self, scan_id: str) -> LanScanRecord | None:
        record = self._ledger.get_scan(scan_id)
        if record is None or record.owner_principal != LAN_OWNER_PRINCIPAL:
            return None
        return record

    def list(self, *, status: str | None = None, limit: int = 200) -> list[LanScanRecord]:
        return self._ledger.list_scans(
            status=status,
            owner_principal=LAN_OWNER_PRINCIPAL,
            limit=limit,
        )

    def observation_page(
        self,
        scan_id: str,
        *,
        limit: int,
    ) -> LanScanObservationPage | None:
        return self._ledger.read_scan_observation_page(
            scan_id,
            owner_principal=LAN_OWNER_PRINCIPAL,
            limit=limit,
        )

    def events(
        self,
        scan_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> builtins.list[LanScanEvent]:
        if self.get(scan_id) is None:
            return []
        return self._ledger.list_events(
            scan_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def subscribe_events(
        self,
        scan_id: str,
        *,
        after_sequence: int = 0,
    ) -> Generator[LanScanEvent, None, None]:
        """Observe durable events; disconnecting never changes scan authority."""

        sequence = after_sequence
        while True:
            events = self.events(scan_id, after_sequence=sequence)
            if events:
                for event in events:
                    sequence = event.sequence
                    yield event
                continue
            current = self.get(scan_id)
            if current is None:
                return
            if current.is_terminal:
                # A terminal commit can linearize after the first empty event
                # read. Query once more after observing terminal state, and
                # continue page-by-page until every durable event is drained.
                terminal_events = self.events(scan_id, after_sequence=sequence)
                if terminal_events:
                    for event in terminal_events:
                        sequence = event.sequence
                        yield event
                    continue
                return
            time.sleep(0.05)

    def shutdown(self, *, timeout_seconds: float) -> bool:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not isfinite(float(timeout_seconds))
            or timeout_seconds < 0
        ):
            raise ValueError("LAN shutdown timeout must be finite and non-negative")
        shutdown_deadline = time.monotonic() + float(timeout_seconds)
        with self._lock:
            self._shutdown_requested = True
            self._admission_open = False
            self._authorizations.clear()
            for handle in tuple(self._active_scans.values()):
                current = self._ledger.get_scan(handle.scan_id)
                if current is not None and current.owner_principal == LAN_OWNER_PRINCIPAL:
                    if current.status == "running":
                        current = self._ledger.request_scan_cancel(
                            handle.scan_id,
                            owner_principal=LAN_OWNER_PRINCIPAL,
                            expected_revision=current.revision,
                            cancel_reason="shutdown_cancelled",
                        )
                    handle.revision = current.revision
                handle.cancellation.cancel()

        while True:
            with self._lock:
                self._reconcile_finished_locked()
                if not self._active_scans:
                    executor = self._executor
                    should_shutdown = executor is not None and not self._executor_shutdown
                    break
            remaining = shutdown_deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.005, remaining))

        if should_shutdown:
            assert executor is not None
            executor.shutdown(wait=True, cancel_futures=False)
            with self._lock:
                self._executor_shutdown = True
        return True

    def is_quiescent(self) -> bool:
        with self._lock:
            self._reconcile_finished_locked()
            return not self._active_scans

    def retained_controller_ids(self) -> tuple[str, ...]:
        with self._lock:
            self._reconcile_finished_locked()
            return tuple(sorted(self._active_scans))

    @property
    def retained_cleanup_count(self) -> int:
        with self._lock:
            return sum(
                1
                for handle in self._active_scans.values()
                for cleanup in handle.cleanup_handles
                if not _cleanup_is_quiescent(cleanup)
            )

    @property
    def controller_count(self) -> int:
        with self._lock:
            self._reconcile_finished_locked()
            return len(self._active_scans)

    def _start_locked(
        self,
        scan_id: str,
        *,
        expected_revision: int,
        authorization: LanPreviewAuthorization,
        preview_digest: str,
        consume_authorization: bool,
    ) -> LanScanRecord:
        self._require_admission()
        context = self._validate_authorization(authorization)
        if preview_digest != authorization.preview_digest:
            raise LanPreviewAuthorizationError("LAN preview digest does not match authority")
        inventory = self._canonical_inventory()
        if _inventory_authority(inventory) != context.inventory_authority:
            raise LanPreviewAuthorizationError("LAN interface inventory changed")
        selected = next(
            (item for item in inventory if item.interface_id == context.interface.interface_id),
            None,
        )
        if selected is None or _interface_authority(selected) != _interface_authority(
            context.interface
        ):
            raise LanPreviewAuthorizationError("LAN interface changed")
        current_preview = preview_private_scope(
            selected.interface_id,
            authorization.preview.network,
            interfaces=inventory,
        )
        if current_preview != authorization.preview:
            raise LanPreviewAuthorizationError("LAN interface preview changed")
        self._validate_authorization(authorization)
        preview_event = self._preview_event(authorization)
        try:
            claimed = self._ledger.claim_scan_start(
                scan_id,
                owner_principal=LAN_OWNER_PRINCIPAL,
                expected_revision=expected_revision,
                preview_digest=preview_digest,
                authorized_preview_digest=authorization.preview_digest,
                preview_event=preview_event,
            )
        except RuntimeError as exc:
            if str(exc) == "lan_scan_owner_already_active":
                raise LanScanAdmissionConflict("LAN owner already has an active scan") from None
            raise
        if consume_authorization:
            self._authorizations.pop(id(authorization), None)

        started = self._monotonic_clock()
        if (
            isinstance(started, bool)
            or not isinstance(started, (int, float))
            or not isfinite(float(started))
        ):
            terminal = self._ledger.commit_scan_terminal(
                scan_id,
                owner_principal=LAN_OWNER_PRINCIPAL,
                expected_revision=claimed.revision,
                status="failed",
                terminal_reason="worker_error",
                cancel_reason=None,
                observations=(),
                mdns_status=authorization.mdns_availability.value,
                planned_count=0,
                admitted_count=0,
                completed_count=0,
                error_category_counts={},
                timeout_count=0,
                evidence_complete=True,
                unknown_inflight_count=0,
            )
            return terminal
        scope = PrivateScanScope.from_request(selected, current_preview.network)
        handle = _ActiveScan(
            scan_id=scan_id,
            scope=scope,
            authorization=authorization,
            cancellation=ScanCancellation(),
            revision=claimed.revision,
            absolute_deadline=float(started) + TOTAL_SCAN_DEADLINE_SECONDS,
            mdns_status=authorization.mdns_availability,
        )
        self._active_scans[scan_id] = handle
        executor = self._executor
        if executor is None:
            self._active_scans.pop(scan_id, None)
            raise RuntimeError("LAN lifecycle executor is unavailable")
        try:
            handle.future = executor.submit(self._run_controller, handle)
        except BaseException:
            self._finalize_with_retry_locked(handle, failure="worker_error")
            durable = self._ledger.get_scan(scan_id)
            if durable is None or not durable.is_terminal:
                raise RuntimeError("LAN scan worker submission failed") from None
            return durable
        return claimed

    def _run_controller(self, handle: _ActiveScan) -> None:
        failure: str | None = None
        observations: tuple[LanEndpointObservation, ...] = ()
        try:
            try:
                if handle.authorization.mdns_availability is MdnsAvailability.AVAILABLE:
                    collection = self._mdns_collector(
                        handle.scope,
                        clock=self._monotonic_clock,
                        absolute_deadline=handle.absolute_deadline,
                        cleanup_handle_sink=lambda cleanup: self._register_cleanup(handle, cleanup),
                    )
                    if type(collection) is not MdnsCollection:
                        raise ValueError("LAN mDNS collector returned an invalid result")
                    handle.mdns_status = collection.availability
                else:
                    collection = MdnsCollection(MdnsAvailability.UNAVAILABLE, ())
                    handle.mdns_status = MdnsAvailability.UNAVAILABLE
            except Exception as exc:
                # Passive discovery is optional.  A capability, binding, or adapter
                # failure must not suppress the separately authorized active scan.
                # Any registered cleanup authority is still settled before probes.
                handle.mdns_status = (
                    MdnsAvailability.TIMED_OUT
                    if isinstance(exc, TimeoutError)
                    else MdnsAvailability.UNAVAILABLE
                )
                collection = MdnsCollection(handle.mdns_status, ())

            if not self._wait_for_cleanup(handle):
                failure = failure or handle.pending_failure or "worker_error"
                handle.pending_failure = failure
                self._await_late_cleanup(handle)
                return

            if failure is None and not self._durably_cancelling(handle):
                if self._deadline_expired(handle):
                    failure = "deadline_expired"
                else:
                    try:
                        observations = self._scanner(
                            handle.scope,
                            handle.authorization.preview.limits,
                            candidates=collection.candidates,
                            cancellation=handle.cancellation,
                            clock=self._monotonic_clock,
                            executor=self._executor,
                            absolute_deadline=handle.absolute_deadline,
                            progress=lambda progress: self._record_progress(handle, progress),
                        )
                        if type(observations) is not tuple or any(
                            type(item) is not LanEndpointObservation for item in observations
                        ):
                            raise ValueError("LAN scanner returned invalid observations")
                        self._ingest_terminal_observations(handle, observations)
                        if handle.progress_persistence_failed:
                            failure = "worker_error"
                    except Exception:
                        failure = "worker_error"
        except Exception:
            failure = failure or "worker_error"
        finally:
            with self._lock:
                failure = failure or handle.pending_failure
                handle.pending_failure = failure
                try:
                    if self._cleanup_settled(handle, wait=False):
                        self._finalize_with_retry_locked(handle, failure=failure)
                finally:
                    handle.controller_finished.set()

    def _record_progress(self, handle: _ActiveScan, progress: LanScanProgress) -> None:
        if type(progress) is not LanScanProgress:
            raise ValueError("LAN scanner progress must be exactly typed")
        observation: LanObservationDraft | None = None
        failure: LanFailureCategory | None = None
        if progress.observation is not None:
            observation = lan_observation_to_draft(
                progress.observation,
                scope=handle.scope,
                freshness_timestamp=_utc_text(self._now_utc()),
            )
            failure = progress.observation.failure_category
        with self._lock:
            current = self._ledger.get_scan(handle.scan_id)
            if current is None or current.status not in {"running", "cancelling"}:
                raise RuntimeError("lan_scan_progress_not_running")
            handle.revision = current.revision
            handle.progress_seen = True
            handle.planned_count = progress.planned_count
            handle.admitted_count = progress.admitted_count
            handle.completed_count = progress.completed_count
            if observation is not None:
                handle.observations[observation.endpoint_id] = observation
                handle.observation_failures[observation.endpoint_id] = failure
            if current.status == "cancelling":
                # The durable cancel event already closed progress mutation, but
                # the real scanner must still drain and account for every task
                # admitted before the token was signalled.
                return
            if handle.progress_persistence_failed:
                return
            errors, timeout_count = self._error_counts(handle)
            try:
                updated = self._ledger.record_scan_progress(
                    handle.scan_id,
                    owner_principal=LAN_OWNER_PRINCIPAL,
                    expected_revision=handle.revision,
                    planned_count=handle.planned_count,
                    admitted_count=handle.admitted_count,
                    completed_count=handle.completed_count,
                    persisted_observation_count=len(handle.observations),
                    error_category_counts=errors,
                    timeout_count=timeout_count,
                    mdns_status=handle.mdns_status.value,
                    observations=(() if observation is None else (observation,)),
                    absolute_deadline=handle.absolute_deadline,
                    monotonic_clock=self._monotonic_clock,
                )
            except Exception:
                # Preserve only a fixed failure code, close new admissions, and
                # keep accepting completion callbacks in memory while the real
                # scanner drains its already-bounded futures.
                handle.progress_persistence_failed = True
                handle.pending_failure = "worker_error"
                handle.cancellation.cancel()
                return
            handle.revision = updated.revision

    def _ingest_terminal_observations(
        self,
        handle: _ActiveScan,
        observations: tuple[LanEndpointObservation, ...],
    ) -> None:
        if handle.progress_seen:
            return
        drafts = [
            (
                lan_observation_to_draft(
                    item,
                    scope=handle.scope,
                    freshness_timestamp=_utc_text(self._now_utc()),
                ),
                item.failure_category,
            )
            for item in observations
        ]
        with self._lock:
            for draft, failure in drafts:
                handle.observations[draft.endpoint_id] = draft
                handle.observation_failures[draft.endpoint_id] = failure
            count = len(handle.observations)
            handle.planned_count = count
            handle.admitted_count = count
            handle.completed_count = count

    def _finalize_handle_locked(self, handle: _ActiveScan, *, failure: str | None) -> None:
        current = self._ledger.get_scan(handle.scan_id)
        if current is None or current.owner_principal != LAN_OWNER_PRINCIPAL:
            return
        if current.is_terminal:
            self._active_scans.pop(handle.scan_id, None)
            return
        if not self._cleanup_settled(handle, wait=False):
            handle.pending_failure = failure
            return
        handle.revision = current.revision
        expired = self._deadline_expired(handle)
        if current.status == "cancelling":
            status = "cancelled"
            terminal_reason = current.cancel_reason or "shutdown_cancelled"
            cancel_reason = terminal_reason
        elif current.status == "running":
            if failure == "deadline_expired" or expired:
                status = "failed"
                terminal_reason = "deadline_expired"
            elif failure is not None:
                status = "failed"
                terminal_reason = "worker_error"
            else:
                status = "completed"
                terminal_reason = "scan_complete"
            cancel_reason = None
        else:
            return
        errors, timeout_count = self._error_counts(handle)
        terminal = self._ledger.commit_scan_terminal(
            handle.scan_id,
            owner_principal=LAN_OWNER_PRINCIPAL,
            expected_revision=handle.revision,
            status=status,
            terminal_reason=terminal_reason,
            cancel_reason=cancel_reason,
            observations=tuple(handle.observations[key] for key in sorted(handle.observations)),
            mdns_status=handle.mdns_status.value,
            planned_count=handle.planned_count,
            admitted_count=handle.admitted_count,
            completed_count=handle.completed_count,
            error_category_counts=errors,
            timeout_count=timeout_count,
            evidence_complete=True,
            unknown_inflight_count=0,
            absolute_deadline=(handle.absolute_deadline if not expired else None),
            monotonic_clock=self._monotonic_clock,
        )
        handle.revision = terminal.revision
        self._active_scans.pop(handle.scan_id, None)

    def _finalize_with_retry_locked(
        self,
        handle: _ActiveScan,
        *,
        failure: str | None,
    ) -> None:
        terminal_failure = failure
        for attempt in range(2):
            try:
                self._finalize_handle_locked(handle, failure=terminal_failure)
                return
            except Exception:
                # A deadline may cross during the ledger's precommit check.
                # Retry once while authority is still retained so the new
                # expired decision is durable without incidental introspection.
                terminal_failure = failure or "worker_error"
                handle.pending_failure = terminal_failure
                if attempt == 1:
                    return

    def _reconcile_finished_locked(self) -> None:
        for handle in tuple(self._active_scans.values()):
            future = handle.future
            if future is not None and not future.done():
                continue
            if not handle.controller_finished.is_set() and future is not None:
                continue
            if not self._cleanup_settled(handle, wait=False):
                continue
            self._finalize_handle_locked(handle, failure=handle.pending_failure)

    def _wait_for_cleanup(self, handle: _ActiveScan) -> bool:
        return self._cleanup_settled(handle, wait=True)

    def _await_late_cleanup(self, handle: _ActiveScan) -> bool:
        """Wait in bounded slices while cleanup can still succeed autonomously."""

        while True:
            if self._cleanup_settled(handle, wait=False):
                return True
            if any(
                _cleanup_is_finished(item) and not _cleanup_is_quiescent(item)
                for item in handle.cleanup_handles
            ):
                return False
            with self._lock:
                if self._shutdown_requested:
                    return False
            time.sleep(0.01)

    def _cleanup_settled(self, handle: _ActiveScan, *, wait: bool) -> bool:
        for cleanup in tuple(handle.cleanup_handles):
            if _cleanup_is_quiescent(cleanup):
                continue
            if not wait:
                return False
            remaining = max(0.0, handle.absolute_deadline - self._numeric_monotonic())
            waiter = getattr(cleanup, "wait_quiescent", None)
            if not callable(waiter):
                return False
            try:
                settled = waiter(timeout_seconds=remaining)
            except Exception:
                return False
            if settled is not True or not _cleanup_is_quiescent(cleanup):
                return False
        return True

    def _register_cleanup(self, handle: _ActiveScan, cleanup: object) -> None:
        with self._lock:
            if all(existing is not cleanup for existing in handle.cleanup_handles):
                handle.cleanup_handles.append(cleanup)

    def _durably_cancelling(self, handle: _ActiveScan) -> bool:
        with self._lock:
            current = self._ledger.get_scan(handle.scan_id)
            if current is None:
                return True
            handle.revision = current.revision
            return current.status == "cancelling"

    def _deadline_expired(self, handle: _ActiveScan) -> bool:
        return self._numeric_monotonic() >= handle.absolute_deadline

    def _numeric_monotonic(self) -> float:
        value = self._monotonic_clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
        ):
            raise ValueError("LAN monotonic clock must return finite numeric time")
        return float(value)

    def _error_counts(self, handle: _ActiveScan) -> tuple[dict[str, int], int]:
        failures = [item for item in handle.observation_failures.values() if item is not None]
        counts = Counter(item.value for item in failures)
        timeout_count = sum(
            count for category, count in Counter(failures).items() if category in _TIMEOUT_FAILURES
        )
        return dict(sorted(counts.items())), timeout_count

    def _validate_authorization(
        self,
        authorization: LanPreviewAuthorization,
    ) -> _AuthorizationContext:
        if type(authorization) is not LanPreviewAuthorization:
            raise LanPreviewAuthorizationError("LAN preview authorization must be exact")
        context = self._authorizations.get(id(authorization))
        now = self._now_utc()
        if context is None or context.authorization is not authorization:
            self._prune_authorizations_locked(now)
            raise LanPreviewAuthorizationError("LAN preview authorization is not live")
        if now >= authorization.expires_at:
            self._authorizations.pop(id(authorization), None)
            self._prune_authorizations_locked(now)
            raise LanPreviewAuthorizationError("LAN preview authorization expired")
        self._prune_authorizations_locked(now)
        if (
            authorization.owner_principal != LAN_OWNER_PRINCIPAL
            or authorization.server_version != LAN_SERVER_VERSION
            or authorization.contract_version != LAN_PREVIEW_CONTRACT_VERSION
        ):
            raise LanPreviewAuthorizationError("LAN preview authorization changed")
        expected = preview_authorization_digest(
            owner_principal=authorization.owner_principal,
            interface=context.interface,
            preview=authorization.preview,
            mdns_availability=authorization.mdns_availability,
            server_version=authorization.server_version,
            contract_version=authorization.contract_version,
            expires_at=authorization.expires_at,
        )
        if expected != authorization.preview_digest:
            raise LanPreviewAuthorizationError("LAN preview authorization digest changed")
        return context

    def _authorization_for_digest_locked(
        self,
        preview_digest: object,
    ) -> _AuthorizationContext:
        if type(preview_digest) is not str:
            raise LanPreviewAuthorizationError("LAN preview digest must be exact")
        contexts = tuple(
            context
            for context in self._authorizations.values()
            if context.authorization.preview_digest == preview_digest
        )
        if len(contexts) != 1:
            self._prune_authorizations_locked(self._now_utc())
            raise LanPreviewAuthorizationError("LAN preview digest is not live")
        return self._validate_authorization(contexts[0].authorization)

    def _prune_authorizations_locked(self, now: datetime) -> None:
        for key, context in tuple(self._authorizations.items()):
            if now >= context.authorization.expires_at:
                self._authorizations.pop(key, None)

    def _preview_event(self, authorization: LanPreviewAuthorization) -> dict[str, object]:
        preview = authorization.preview
        return {
            "schema": LAN_SCAN_PREVIEW_EVENT_SCHEMA,
            "owner_principal": LAN_OWNER_PRINCIPAL,
            "interface_id": preview.interface_id,
            "network": preview.network,
            "limits": asdict(preview.limits),
            "active_host_count": preview.active_host_count,
            "passive_or_manual_only": preview.passive_or_manual_only,
            "port_count": len(preview.port_matrix),
            "mdns_status": authorization.mdns_availability.value,
            "server_version": authorization.server_version,
            "contract_version": authorization.contract_version,
            "preview_digest": authorization.preview_digest,
            "expires_at": _utc_text(authorization.expires_at),
        }

    def _canonical_inventory(self) -> tuple[NetworkInterface, ...]:
        raw = tuple(islice(iter(self._interface_enumerator()), _MAX_INTERFACE_COUNT + 1))
        if len(raw) > _MAX_INTERFACE_COUNT:
            raise ValueError("LAN interface inventory exceeds its fixed limit")
        canonical: list[NetworkInterface] = []
        for interface in raw:
            if type(interface) is not NetworkInterface:
                raise ValueError("LAN interface inventory must be exactly typed")
            if type(interface.os_identity) is not str or type(interface.display_name) is not str:
                raise ValueError("LAN interface inventory contains invalid text")
            if type(interface.addresses) is not tuple:
                raise ValueError("LAN interface inventory addresses must be exact")
            if len(interface.addresses) > _MAX_INTERFACE_ADDRESS_COUNT:
                raise ValueError("LAN interface address inventory exceeds its fixed limit")
            try:
                display_bytes = interface.display_name.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("LAN interface display name is invalid") from None
            if (
                not interface.display_name
                or len(display_bytes) > _MAX_INTERFACE_DISPLAY_NAME_BYTES
                or unicodedata.normalize("NFC", interface.display_name) != interface.display_name
                or any(
                    unicodedata.category(character).startswith("C")
                    for character in interface.display_name
                )
            ):
                raise ValueError("LAN interface display name is invalid")
            if any(
                type(address) is not str or not _is_private_interface_address(address)
                for address in interface.addresses
            ):
                raise ValueError("LAN interface address is invalid")
            try:
                rebuilt = NetworkInterface.from_addresses(
                    os_identity=interface.os_identity,
                    display_name=interface.display_name,
                    addresses=interface.addresses,
                )
            except (TypeError, ValueError):
                raise ValueError("LAN interface inventory is invalid") from None
            if rebuilt != interface:
                raise ValueError("LAN interface inventory is not canonical")
            canonical.append(rebuilt)
        canonical.sort(key=lambda item: item.interface_id)
        if len({item.interface_id for item in canonical}) != len(canonical) or len(
            {item.os_identity for item in canonical}
        ) != len(canonical):
            raise ValueError("LAN interface inventory contains duplicates")
        return tuple(canonical)

    def _require_started(self) -> None:
        if not self._lifecycle_started:
            raise RuntimeError("LAN lifecycle has not started")

    def _require_admission(self) -> None:
        self._require_started()
        if not self._admission_open:
            raise RuntimeError("LAN scan admission is closed")

    def _now_utc(self) -> datetime:
        value = self._utc_clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("LAN UTC clock must return an aware datetime")
        return value.astimezone(UTC)


def _default_mdns_availability() -> MdnsAvailability:
    """Report package capability without creating a socket or browser."""

    return (
        MdnsAvailability.AVAILABLE
        if importlib.util.find_spec("zeroconf") is not None
        else MdnsAvailability.UNAVAILABLE
    )


def _interface_authority(interface: NetworkInterface) -> tuple[str, str, tuple[str, ...]]:
    return interface.interface_id, interface.os_identity, interface.addresses


def _inventory_authority(
    inventory: tuple[NetworkInterface, ...],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    return tuple(_interface_authority(interface) for interface in inventory)


def _is_private_interface_address(value: str) -> bool:
    try:
        attached = ipaddress.ip_interface(value)
    except ValueError:
        return False
    if isinstance(attached, ipaddress.IPv4Interface):
        return any(
            attached.network.subnet_of(network) for network in _PRIVATE_IPV4_INTERFACE_NETWORKS
        )
    return any(attached.network.subnet_of(network) for network in _PRIVATE_IPV6_INTERFACE_NETWORKS)


def _cleanup_is_quiescent(cleanup: object) -> bool:
    predicate = getattr(cleanup, "is_quiescent", None)
    if not callable(predicate):
        return False
    try:
        return predicate() is True
    except Exception:
        return False


def _cleanup_is_finished(cleanup: object) -> bool:
    predicate = getattr(cleanup, "is_finished", None)
    if not callable(predicate):
        return False
    try:
        return predicate() is True
    except Exception:
        return False


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("LAN timestamp must be timezone aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
