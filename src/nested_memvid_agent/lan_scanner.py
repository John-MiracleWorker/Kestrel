"""Bounded active discovery under one owner-confirmed private-LAN scope."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from nested_memvid_agent.lan_discovery_models import (
    HTTP_PROBE_TIMEOUT_SECONDS,
    KNOWN_MODEL_SERVICE_PORTS,
    MAX_ACTIVE_HOSTS,
    MAX_DISCOVERED_MODELS,
    MAX_SCAN_CONCURRENCY,
    TCP_CONNECT_TIMEOUT_SECONDS,
    TOTAL_SCAN_DEADLINE_SECONDS,
    LanScanLimits,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_http_transport import (
    AuthenticatedLanSource,
    CancellationToken,
    DirectLanHttpTransport,
    InterfaceInventoryResolver,
    LanHttpResponse,
    LanProbeModel,
    LanRequestProgress,
    LanRequestRoute,
    LanTransportError,
    LanTransportFailure,
    authenticate_lan_source,
    authenticate_private_scan_scope,
)
from nested_memvid_agent.lan_mdns import (
    ALLOWED_MODEL_SERVICE_TYPES,
    LanCandidate,
    _validate_display_text,
)
from nested_memvid_agent.security_boundary import redact_text

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DISPLAY_METADATA_FIELDS = frozenset(
    {"display_name", "description", "vendor", "product", "version"}
)


class Reachability(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    UNREACHABLE = "unreachable"
    REACHABLE = "reachable"


class ApiShape(StrEnum):
    OLLAMA_COMPATIBLE = "ollama_compatible"
    OPENAI_COMPATIBLE = "openai_compatible"


class TransportSecurity(StrEnum):
    PLAIN_HTTP = "plain_http"


class LanFailureCategory(StrEnum):
    CANCELLED = "cancelled"
    SCAN_DEADLINE_EXCEEDED = "scan_deadline_exceeded"
    INTERFACE_DRIFT = "interface_drift"
    INTERFACE_PINNING_UNAVAILABLE = "interface_pinning_unavailable"
    TCP_TIMEOUT = "tcp_timeout"
    TCP_REFUSED = "tcp_refused"
    TCP_UNREACHABLE = "tcp_unreachable"
    TCP_ERROR = "tcp_error"
    HTTP_TIMEOUT = "http_timeout"
    HTTP_PROTOCOL_REJECTED = "http_protocol_rejected"
    REDIRECT_REJECTED = "redirect_rejected"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"
    HTTP_STATUS_REJECTED = "http_status_rejected"
    CATALOG_NOT_FOUND = "catalog_not_found"
    CATALOG_INVALID = "catalog_invalid"
    CATALOG_EMPTY = "catalog_empty"
    GENERATION_REQUEST_FAILED = "generation_request_failed"
    GENERATION_RESPONSE_INVALID = "generation_response_invalid"


class CapabilityName(StrEnum):
    GENERATION = "generation"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    TOOLS = "tools"
    VISION = "vision"


class CapabilityObservationStatus(StrEnum):
    OBSERVED_PASS = "observed_pass"
    OBSERVED_FAILURE = "observed_failure"
    NOT_RUN = "not_run"


class CapabilityProvenance(StrEnum):
    OBSERVED = "observed"
    NOT_RUN = "not_run"


_PUBLIC_FAILURE_TEXT = {
    None: None,
    LanFailureCategory.CANCELLED: "LAN scan was cancelled",
    LanFailureCategory.SCAN_DEADLINE_EXCEEDED: "LAN scan deadline expired",
    LanFailureCategory.INTERFACE_DRIFT: "selected LAN interface changed",
    LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE: "selected interface cannot be pinned",
    LanFailureCategory.TCP_TIMEOUT: "LAN TCP check timed out",
    LanFailureCategory.TCP_REFUSED: "LAN TCP connection was refused",
    LanFailureCategory.TCP_UNREACHABLE: "LAN TCP destination was unreachable",
    LanFailureCategory.TCP_ERROR: "LAN TCP check failed",
    LanFailureCategory.HTTP_TIMEOUT: "LAN HTTP probe timed out",
    LanFailureCategory.HTTP_PROTOCOL_REJECTED: "LAN HTTP response was rejected",
    LanFailureCategory.REDIRECT_REJECTED: "LAN HTTP redirect was rejected",
    LanFailureCategory.RESPONSE_TOO_LARGE: "LAN HTTP response exceeded the byte limit",
    LanFailureCategory.UNSUPPORTED_CONTENT_ENCODING: "LAN HTTP encoding was rejected",
    LanFailureCategory.HTTP_STATUS_REJECTED: "LAN HTTP status was rejected",
    LanFailureCategory.CATALOG_NOT_FOUND: "LAN model catalog was not found",
    LanFailureCategory.CATALOG_INVALID: "LAN model catalog was invalid",
    LanFailureCategory.CATALOG_EMPTY: "LAN model catalog was empty",
    LanFailureCategory.GENERATION_REQUEST_FAILED: "LAN generation probe request failed",
    LanFailureCategory.GENERATION_RESPONSE_INVALID: "LAN generation response was invalid",
}


@dataclass(frozen=True)
class LanCapabilityEvidence:
    capability: CapabilityName
    supported: bool | None
    provenance: CapabilityProvenance
    status: CapabilityObservationStatus

    def __post_init__(self) -> None:
        if type(self.capability) is not CapabilityName:
            raise ValueError("LAN capability must use the closed capability enum")
        if self.capability is CapabilityName.GENERATION:
            valid = (
                (
                    self.status is CapabilityObservationStatus.OBSERVED_PASS
                    and self.provenance is CapabilityProvenance.OBSERVED
                    and self.supported is True
                )
                or (
                    self.status is CapabilityObservationStatus.OBSERVED_FAILURE
                    and self.provenance is CapabilityProvenance.OBSERVED
                    and self.supported is None
                )
                or (
                    self.status is CapabilityObservationStatus.NOT_RUN
                    and self.provenance is CapabilityProvenance.NOT_RUN
                    and self.supported is None
                )
            )
        else:
            valid = (
                self.status is CapabilityObservationStatus.NOT_RUN
                and self.provenance is CapabilityProvenance.NOT_RUN
                and self.supported is None
            )
        if not valid:
            raise ValueError("LAN capability evidence overclaims observed authority")

    @classmethod
    def observed_pass(cls) -> LanCapabilityEvidence:
        return cls(
            CapabilityName.GENERATION,
            True,
            CapabilityProvenance.OBSERVED,
            CapabilityObservationStatus.OBSERVED_PASS,
        )

    @classmethod
    def observed_failure(cls) -> LanCapabilityEvidence:
        return cls(
            CapabilityName.GENERATION,
            None,
            CapabilityProvenance.OBSERVED,
            CapabilityObservationStatus.OBSERVED_FAILURE,
        )

    @classmethod
    def not_run(cls, capability: CapabilityName) -> LanCapabilityEvidence:
        return cls(
            capability,
            None,
            CapabilityProvenance.NOT_RUN,
            CapabilityObservationStatus.NOT_RUN,
        )

    def to_digest_payload(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "provenance": self.provenance.value,
            "status": self.status.value,
            "supported": self.supported,
        }


@dataclass(frozen=True)
class LanEndpointObservation:
    endpoint: ResolvedLanEndpoint
    endpoint_binding_digest: str
    reachability: Reachability
    transport_security: TransportSecurity | None
    api_shape: ApiShape | None
    catalog: tuple[str, ...]
    catalog_complete: bool
    catalog_truncated: bool
    catalog_digest: str
    capabilities: tuple[LanCapabilityEvidence, ...]
    capability_route: str | None
    selected_model_id: str | None
    capability_digest: str
    failure_category: LanFailureCategory | None
    public_error: str | None
    observation_digest: str

    def __post_init__(self) -> None:
        if type(self.endpoint) is not ResolvedLanEndpoint:
            raise ValueError("LAN observation requires a typed endpoint")
        if type(self.reachability) is not Reachability:
            raise ValueError("LAN observation reachability is invalid")
        if (
            self.transport_security is not None
            and type(self.transport_security) is not TransportSecurity
        ):
            raise ValueError("LAN observation transport security is invalid")
        if self.api_shape is not None and type(self.api_shape) is not ApiShape:
            raise ValueError("LAN observation API shape is invalid")
        if (
            self.failure_category is not None
            and type(self.failure_category) is not LanFailureCategory
        ):
            raise ValueError("LAN observation failure category is invalid")
        if type(self.catalog) is not tuple or len(self.catalog) > MAX_DISCOVERED_MODELS:
            raise ValueError("LAN observation catalog is not a bounded tuple")
        if self.catalog != tuple(sorted(set(self.catalog))):
            raise ValueError("LAN observation catalog is not canonical")
        for model_id in self.catalog:
            if LanProbeModel.from_catalog(model_id).model_id != model_id:
                raise ValueError("LAN observation catalog contains an invalid model")
        if type(self.catalog_complete) is not bool or type(self.catalog_truncated) is not bool:
            raise ValueError("LAN catalog completeness evidence must be boolean")
        if self.catalog_complete and self.catalog_truncated:
            raise ValueError("LAN catalog cannot be complete and truncated")
        if self.catalog_truncated and len(self.catalog) != MAX_DISCOVERED_MODELS:
            raise ValueError("truncated LAN catalog must retain the fixed maximum")
        if (
            type(self.capabilities) is not tuple
            or not all(type(item) is LanCapabilityEvidence for item in self.capabilities)
            or tuple(item.capability for item in self.capabilities) != tuple(CapabilityName)
        ):
            raise ValueError("LAN capability evidence must be complete and ordered")
        if self.selected_model_id is not None:
            if (
                LanProbeModel.from_catalog(self.selected_model_id).model_id
                != self.selected_model_id
            ):
                raise ValueError("LAN selected model is invalid")
        expected_route = _capability_route(self.api_shape)
        if self.capability_route not in {None, expected_route}:
            raise ValueError("LAN capability route does not match its API shape")
        if (self.capability_route is None) != (self.selected_model_id is None):
            raise ValueError("LAN capability route and selected model must be paired")
        if self.selected_model_id is not None and (
            not self.catalog or self.selected_model_id != self.catalog[0]
        ):
            raise ValueError("LAN capability model must be the deterministic catalog model")
        if self.reachability is not Reachability.REACHABLE and (
            self.transport_security is not None or self.api_shape is not None or self.catalog
        ):
            raise ValueError("unreached LAN endpoint cannot carry HTTP evidence")
        if self.api_shape is None and self.catalog:
            raise ValueError("LAN catalog requires an established API shape")
        if self.api_shape is None and (self.catalog_complete or self.catalog_truncated):
            raise ValueError("LAN catalog flags require an established API shape")
        if self.api_shape is not None and (
            self.reachability is not Reachability.REACHABLE
            or self.transport_security is not TransportSecurity.PLAIN_HTTP
        ):
            raise ValueError("LAN API shape requires reachable plain HTTP evidence")
        if (
            self.transport_security is not None
            and self.transport_security is not TransportSecurity.PLAIN_HTTP
        ):
            raise ValueError("automatic LAN discovery can only use plain HTTP")
        if self.transport_security is not None and self.reachability is not Reachability.REACHABLE:
            raise ValueError("LAN transport evidence requires a reachable endpoint")

        tcp_failures = {
            LanFailureCategory.TCP_TIMEOUT,
            LanFailureCategory.TCP_REFUSED,
            LanFailureCategory.TCP_UNREACHABLE,
            LanFailureCategory.TCP_ERROR,
        }
        not_attempted_failures = {
            LanFailureCategory.CANCELLED,
            LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
            LanFailureCategory.INTERFACE_DRIFT,
            LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
        }
        if (
            self.reachability is Reachability.NOT_ATTEMPTED
            and self.failure_category not in not_attempted_failures
        ):
            raise ValueError("not-attempted LAN evidence has an impossible failure")
        if (
            self.reachability is Reachability.UNREACHABLE
            and self.failure_category not in tcp_failures
        ):
            raise ValueError("unreachable LAN evidence must have a TCP failure")
        if self.reachability is Reachability.REACHABLE and self.failure_category in tcp_failures:
            raise ValueError("reachable LAN evidence cannot have a TCP failure")

        generation = self.capabilities[0]
        generation_observed = generation.status in {
            CapabilityObservationStatus.OBSERVED_PASS,
            CapabilityObservationStatus.OBSERVED_FAILURE,
        }
        if generation_observed and (
            self.api_shape is None
            or not self.catalog
            or self.capability_route is None
            or self.selected_model_id is None
        ):
            raise ValueError("observed LAN generation requires catalog-bound authority")
        if not generation_observed and (
            self.capability_route is not None or self.selected_model_id is not None
        ):
            raise ValueError("unrun LAN generation cannot carry request authority")
        if (
            generation.status is CapabilityObservationStatus.OBSERVED_PASS
            and self.failure_category is not None
        ):
            raise ValueError("passing LAN generation cannot carry a failure")
        if (
            generation.status is CapabilityObservationStatus.OBSERVED_FAILURE
            and self.failure_category is None
        ):
            raise ValueError("failed LAN generation requires a closed failure")

        pre_api_failures = {
            LanFailureCategory.CANCELLED,
            LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
            LanFailureCategory.INTERFACE_DRIFT,
            LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
            LanFailureCategory.HTTP_TIMEOUT,
            LanFailureCategory.HTTP_PROTOCOL_REJECTED,
            LanFailureCategory.REDIRECT_REJECTED,
            LanFailureCategory.RESPONSE_TOO_LARGE,
            LanFailureCategory.UNSUPPORTED_CONTENT_ENCODING,
            LanFailureCategory.HTTP_STATUS_REJECTED,
            LanFailureCategory.CATALOG_NOT_FOUND,
            LanFailureCategory.CATALOG_INVALID,
        }
        pre_generation_failures = {
            LanFailureCategory.CANCELLED,
            LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
            LanFailureCategory.INTERFACE_DRIFT,
            LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
            LanFailureCategory.HTTP_TIMEOUT,
        }
        generation_failures = {
            LanFailureCategory.CANCELLED,
            LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
            LanFailureCategory.HTTP_TIMEOUT,
            LanFailureCategory.HTTP_PROTOCOL_REJECTED,
            LanFailureCategory.REDIRECT_REJECTED,
            LanFailureCategory.RESPONSE_TOO_LARGE,
            LanFailureCategory.UNSUPPORTED_CONTENT_ENCODING,
            LanFailureCategory.GENERATION_REQUEST_FAILED,
            LanFailureCategory.GENERATION_RESPONSE_INVALID,
        }
        response_proven_failures = {
            LanFailureCategory.HTTP_PROTOCOL_REJECTED,
            LanFailureCategory.REDIRECT_REJECTED,
            LanFailureCategory.RESPONSE_TOO_LARGE,
            LanFailureCategory.UNSUPPORTED_CONTENT_ENCODING,
            LanFailureCategory.HTTP_STATUS_REJECTED,
            LanFailureCategory.CATALOG_NOT_FOUND,
            LanFailureCategory.CATALOG_INVALID,
        }
        interface_failures = {
            LanFailureCategory.INTERFACE_DRIFT,
            LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
        }
        if self.reachability is Reachability.REACHABLE:
            if self.api_shape is None and self.failure_category in response_proven_failures:
                if self.transport_security is not TransportSecurity.PLAIN_HTTP:
                    raise ValueError("LAN response failure requires plain HTTP transport evidence")
            if self.api_shape is None and self.failure_category in interface_failures:
                if self.transport_security is not None:
                    raise ValueError("LAN interface failure cannot claim HTTP transport evidence")
            if self.api_shape is None and self.failure_category not in pre_api_failures:
                raise ValueError("LAN failure does not match the pre-catalog phase")
            if self.api_shape is not None and not self.catalog:
                if self.failure_category not in {
                    LanFailureCategory.CATALOG_EMPTY,
                    LanFailureCategory.CATALOG_INVALID,
                }:
                    raise ValueError("LAN empty catalog failure does not match its phase")
                if (
                    self.failure_category is LanFailureCategory.CATALOG_EMPTY
                    and not self.catalog_complete
                ) or (
                    self.failure_category is LanFailureCategory.CATALOG_INVALID
                    and self.catalog_complete
                ):
                    raise ValueError("LAN empty catalog completeness is inconsistent")
            if self.catalog and generation.status is CapabilityObservationStatus.NOT_RUN:
                if self.failure_category not in pre_generation_failures:
                    raise ValueError("LAN failure does not match the pre-generation phase")
            if generation.status is CapabilityObservationStatus.OBSERVED_FAILURE:
                if self.failure_category not in generation_failures:
                    raise ValueError("LAN failure does not match observed generation")
        if self.failure_category is None and not (
            self.reachability is Reachability.REACHABLE
            and self.transport_security is TransportSecurity.PLAIN_HTTP
            and self.api_shape is not None
            and bool(self.catalog)
            and generation.status is CapabilityObservationStatus.OBSERVED_PASS
        ):
            raise ValueError("failure-free LAN observation requires validated generation success")
        if self.public_error != _PUBLIC_FAILURE_TEXT[self.failure_category]:
            raise ValueError("LAN public error must be the closed canonical text")
        if self.public_error is not None and (
            len(self.public_error) > 1024
            or unicodedata.normalize("NFC", self.public_error) != self.public_error
            or any(
                unicodedata.category(character).startswith("C") for character in self.public_error
            )
        ):
            raise ValueError("LAN public error is not safe bounded text")
        expected_endpoint = _endpoint_binding_digest(self.endpoint)
        expected_catalog = _catalog_digest(
            self.endpoint,
            expected_endpoint,
            self.api_shape,
            self.catalog_complete,
            self.catalog_truncated,
            self.catalog,
        )
        expected_capability = _capability_digest(
            expected_endpoint,
            expected_catalog,
            self.api_shape,
            self.capability_route,
            self.selected_model_id,
            self.capabilities,
        )
        expected_observation = _observation_digest(
            expected_endpoint,
            self.reachability,
            self.transport_security,
            self.api_shape,
            expected_catalog,
            expected_capability,
            self.failure_category,
        )
        if (
            self.endpoint_binding_digest != expected_endpoint
            or self.catalog_digest != expected_catalog
            or self.capability_digest != expected_capability
            or self.observation_digest != expected_observation
        ):
            raise ValueError("LAN evidence digest does not match its typed preimage")
        if not all(
            _DIGEST_RE.fullmatch(value)
            for value in (
                self.endpoint_binding_digest,
                self.catalog_digest,
                self.capability_digest,
                self.observation_digest,
            )
        ):
            raise ValueError("LAN evidence digest is malformed")


class LanTcpProbe(Protocol):
    def tcp_reachable(
        self,
        scope: PrivateScanScope,
        endpoint: ResolvedLanEndpoint,
        source: AuthenticatedLanSource,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> bool: ...


class LanHttpTransport(Protocol):
    def request(
        self,
        scope: PrivateScanScope,
        endpoint: ResolvedLanEndpoint,
        source: AuthenticatedLanSource,
        route: LanRequestRoute,
        *,
        deadline: float,
        cancellation: CancellationToken,
        model: LanProbeModel | None = None,
    ) -> LanHttpResponse: ...


class ScanCancellation:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._admission_lock = threading.Lock()

    def cancel(self) -> None:
        with self._admission_lock:
            self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def _admit(
        self,
        *,
        deadline: float,
        clock: Callable[[], float],
        submit: Callable[[], Future[LanEndpointObservation]],
    ) -> tuple[Future[LanEndpointObservation] | None, LanFailureCategory | None]:
        """Linearize cancellation, deadline sampling, and executor admission."""

        with self._admission_lock:
            if self._event.is_set():
                return None, LanFailureCategory.CANCELLED
            if clock() >= deadline:
                return None, LanFailureCategory.SCAN_DEADLINE_EXCEEDED
            return submit(), None


def scan_lan_scope(
    scope: PrivateScanScope,
    limits: LanScanLimits,
    *,
    candidates: tuple[LanCandidate, ...] = (),
    cancellation: CancellationToken | None = None,
    clock: Callable[[], float] = time.monotonic,
    tcp_probe: LanTcpProbe | None = None,
    http_transport: LanHttpTransport | None = None,
    interface_inventory_resolver: InterfaceInventoryResolver | None = None,
) -> tuple[LanEndpointObservation, ...]:
    """Probe a deterministic matrix with a sliding at-most-sixteen-task window."""

    canonical_scope = authenticate_private_scan_scope(scope)
    if type(limits) is not LanScanLimits or limits != LanScanLimits():
        raise ValueError("LAN scan limits must be the fixed canonical limits")
    endpoints = _derive_endpoints(canonical_scope, candidates)
    if not endpoints:
        return ()
    if cancellation is not None and type(cancellation) is not ScanCancellation:
        raise ValueError("LAN scan cancellation must use the atomic scan token")
    token = cancellation if cancellation is not None else ScanCancellation()
    direct = DirectLanHttpTransport(
        clock=clock,
        inventory_resolver=interface_inventory_resolver,
    )
    tcp = tcp_probe or direct
    http = http_transport or direct
    started_at = clock()
    scan_deadline = started_at + TOTAL_SCAN_DEADLINE_SECONDS
    results: list[LanEndpointObservation | None] = [None] * len(endpoints)
    next_index = 0
    closure_failure: LanFailureCategory | None = None
    pending: dict[Future[LanEndpointObservation], int] = {}

    with ThreadPoolExecutor(
        max_workers=MAX_SCAN_CONCURRENCY,
        thread_name_prefix="kestrel-lan-probe",
    ) as executor:
        while pending or next_index < len(endpoints):
            while (
                closure_failure is None
                and len(pending) < MAX_SCAN_CONCURRENCY
                and next_index < len(endpoints)
            ):
                index = next_index

                def submit_endpoint(index: int = index) -> Future[LanEndpointObservation]:
                    return executor.submit(
                        probe_lan_endpoint,
                        canonical_scope,
                        endpoints[index],
                        scan_deadline=scan_deadline,
                        cancellation=token,
                        clock=clock,
                        tcp_probe=tcp,
                        http_transport=http,
                        interface_inventory_resolver=interface_inventory_resolver,
                    )

                future, admission_failure = token._admit(
                    deadline=scan_deadline,
                    clock=clock,
                    submit=submit_endpoint,
                )
                if admission_failure is not None:
                    closure_failure = admission_failure
                    break
                assert future is not None
                next_index += 1
                pending[future] = index

            if not pending:
                break
            if closure_failure is None:
                if token.is_cancelled():
                    closure_failure = LanFailureCategory.CANCELLED
                elif clock() >= scan_deadline:
                    closure_failure = LanFailureCategory.SCAN_DEADLINE_EXCEEDED
            timeout = (
                0.05
                if closure_failure is not None
                else max(0.0, min(0.05, scan_deadline - clock()))
            )
            done, _not_done = wait(tuple(pending), timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except Exception:
                    results[index] = _make_observation(
                        endpoints[index],
                        reachability=Reachability.UNREACHABLE,
                        failure_category=LanFailureCategory.TCP_ERROR,
                    )

    if closure_failure is None:
        closure_failure = (
            LanFailureCategory.CANCELLED
            if token.is_cancelled()
            else LanFailureCategory.SCAN_DEADLINE_EXCEEDED
        )
    for index, result in enumerate(results):
        if result is None:
            results[index] = _make_observation(
                endpoints[index],
                reachability=Reachability.NOT_ATTEMPTED,
                failure_category=closure_failure,
            )
    return tuple(result for result in results if result is not None)


def probe_lan_endpoint(
    scope: PrivateScanScope,
    endpoint: ResolvedLanEndpoint,
    *,
    scan_deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
    tcp_probe: LanTcpProbe,
    http_transport: LanHttpTransport,
    interface_inventory_resolver: InterfaceInventoryResolver | None = None,
) -> LanEndpointObservation:
    """Probe one canonical endpoint while sharing one absolute HTTP phase deadline."""

    canonical_scope = authenticate_private_scan_scope(scope)
    canonical_endpoint = ResolvedLanEndpoint.from_scope(
        canonical_scope,
        endpoint.address,
        endpoint.port,
    )
    if endpoint != canonical_endpoint:
        raise ValueError("LAN endpoint does not match its confirmed scope")
    if cancellation.is_cancelled():
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.NOT_ATTEMPTED,
            failure_category=LanFailureCategory.CANCELLED,
        )
    if clock() >= scan_deadline:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.NOT_ATTEMPTED,
            failure_category=LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
        )
    try:
        source = authenticate_lan_source(
            canonical_scope,
            canonical_endpoint,
            interface_inventory_resolver,
        )
    except ValueError:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.NOT_ATTEMPTED,
            failure_category=LanFailureCategory.INTERFACE_DRIFT,
        )
    tcp_deadline = min(scan_deadline, clock() + TCP_CONNECT_TIMEOUT_SECONDS)
    try:
        reachable = tcp_probe.tcp_reachable(
            canonical_scope,
            canonical_endpoint,
            source,
            deadline=tcp_deadline,
            cancellation=cancellation,
        )
    except LanTransportError as exc:
        tcp_failure = _map_tcp_failure(exc.failure)
        reachability = (
            Reachability.NOT_ATTEMPTED
            if exc.failure
            in {
                LanTransportFailure.CANCELLED,
                LanTransportFailure.INTERFACE_CHANGED,
                LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE,
            }
            else Reachability.UNREACHABLE
        )
        if exc.failure is LanTransportFailure.DEADLINE_EXCEEDED:
            if clock() >= scan_deadline:
                tcp_failure = LanFailureCategory.SCAN_DEADLINE_EXCEEDED
                reachability = Reachability.NOT_ATTEMPTED
            else:
                tcp_failure = LanFailureCategory.TCP_TIMEOUT
                reachability = Reachability.UNREACHABLE
        return _make_observation(
            canonical_endpoint,
            reachability=reachability,
            failure_category=tcp_failure,
        )
    except Exception:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.UNREACHABLE,
            failure_category=LanFailureCategory.TCP_ERROR,
        )
    if reachable is not True:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.UNREACHABLE,
            failure_category=LanFailureCategory.TCP_UNREACHABLE,
        )

    if cancellation.is_cancelled():
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.REACHABLE,
            failure_category=LanFailureCategory.CANCELLED,
        )
    if clock() >= scan_deadline:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.REACHABLE,
            failure_category=LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
        )
    http_deadline = min(scan_deadline, clock() + HTTP_PROBE_TIMEOUT_SECONDS)

    first = _request_phase(
        canonical_scope,
        canonical_endpoint,
        LanRequestRoute.OLLAMA_CATALOG,
        deadline=http_deadline,
        scan_deadline=scan_deadline,
        cancellation=cancellation,
        clock=clock,
        http_transport=http_transport,
        inventory_resolver=interface_inventory_resolver,
    )
    if isinstance(first, LanEndpointObservation):
        return first
    response, transport_security = first
    catalog_route = LanRequestRoute.OLLAMA_CATALOG
    api_shape = ApiShape.OLLAMA_COMPATIBLE
    if response.status_code == 404:
        second = _request_phase(
            canonical_scope,
            canonical_endpoint,
            LanRequestRoute.OPENAI_CATALOG,
            deadline=http_deadline,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
            http_transport=http_transport,
            inventory_resolver=interface_inventory_resolver,
        )
        if isinstance(second, LanEndpointObservation):
            return second
        response, transport_security = second
        catalog_route = LanRequestRoute.OPENAI_CATALOG
        api_shape = ApiShape.OPENAI_COMPATIBLE
        if response.status_code == 404:
            return _make_observation(
                canonical_endpoint,
                reachability=Reachability.REACHABLE,
                transport_security=transport_security,
                failure_category=LanFailureCategory.CATALOG_NOT_FOUND,
            )
    if response.status_code != 200:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.REACHABLE,
            transport_security=transport_security,
            failure_category=(
                LanFailureCategory.REDIRECT_REJECTED
                if 300 <= response.status_code <= 399
                else LanFailureCategory.HTTP_STATUS_REJECTED
            ),
        )

    try:
        models, complete, truncated, invalid_entries, schema_established = _parse_catalog(
            response.body,
            api_shape,
        )
    except ValueError:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.REACHABLE,
            transport_security=transport_security,
            failure_category=LanFailureCategory.CATALOG_INVALID,
        )
    if not schema_established:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.REACHABLE,
            transport_security=transport_security,
            failure_category=LanFailureCategory.CATALOG_INVALID,
        )
    if not models:
        return _make_observation(
            canonical_endpoint,
            reachability=Reachability.REACHABLE,
            transport_security=transport_security,
            api_shape=api_shape,
            catalog=(),
            catalog_complete=complete,
            catalog_truncated=truncated,
            failure_category=(
                LanFailureCategory.CATALOG_INVALID
                if invalid_entries
                else LanFailureCategory.CATALOG_EMPTY
            ),
        )

    selected_model = models[0]
    generation_route = (
        LanRequestRoute.OLLAMA_GENERATION
        if catalog_route is LanRequestRoute.OLLAMA_CATALOG
        else LanRequestRoute.OPENAI_GENERATION
    )
    generation = _request_phase(
        canonical_scope,
        canonical_endpoint,
        generation_route,
        deadline=http_deadline,
        scan_deadline=scan_deadline,
        cancellation=cancellation,
        clock=clock,
        http_transport=http_transport,
        inventory_resolver=interface_inventory_resolver,
        model=LanProbeModel.from_catalog(selected_model),
        established=(api_shape, models, complete, truncated),
    )
    if isinstance(generation, LanEndpointObservation):
        return generation
    generation_response, transport_security = generation
    capabilities = _capabilities_with_generation(
        passed=(
            generation_response.status_code == 200
            and _generation_response_passes(generation_response.body, api_shape)
        )
    )
    if generation_response.status_code != 200:
        failure = (
            LanFailureCategory.REDIRECT_REJECTED
            if 300 <= generation_response.status_code <= 399
            else LanFailureCategory.GENERATION_REQUEST_FAILED
        )
    elif capabilities[0].status is not CapabilityObservationStatus.OBSERVED_PASS:
        failure = LanFailureCategory.GENERATION_RESPONSE_INVALID
    else:
        failure = None
    return _make_observation(
        canonical_endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=transport_security,
        api_shape=api_shape,
        catalog=models,
        catalog_complete=complete,
        catalog_truncated=truncated,
        capabilities=capabilities,
        capability_route=generation_route.path,
        selected_model_id=selected_model,
        failure_category=failure,
    )


def _request_phase(
    scope: PrivateScanScope,
    endpoint: ResolvedLanEndpoint,
    route: LanRequestRoute,
    *,
    deadline: float,
    scan_deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
    http_transport: LanHttpTransport,
    inventory_resolver: InterfaceInventoryResolver | None,
    model: LanProbeModel | None = None,
    established: tuple[ApiShape, tuple[str, ...], bool, bool] | None = None,
) -> tuple[LanHttpResponse, TransportSecurity] | LanEndpointObservation:
    if cancellation.is_cancelled():
        return _phase_failure_observation(
            endpoint,
            LanFailureCategory.CANCELLED,
            established,
            transport_observed=False,
            generation_observed=False,
        )
    now = clock()
    if now >= deadline:
        return _phase_failure_observation(
            endpoint,
            (
                LanFailureCategory.SCAN_DEADLINE_EXCEEDED
                if now >= scan_deadline
                else LanFailureCategory.HTTP_TIMEOUT
            ),
            established,
            transport_observed=False,
            generation_observed=False,
        )
    try:
        source = authenticate_lan_source(scope, endpoint, inventory_resolver)
    except ValueError:
        return _phase_failure_observation(
            endpoint,
            LanFailureCategory.INTERFACE_DRIFT,
            established,
            transport_observed=False,
            generation_observed=False,
        )
    try:
        response = http_transport.request(
            scope,
            endpoint,
            source,
            route,
            deadline=deadline,
            cancellation=cancellation,
            model=model,
        )
    except LanTransportError as exc:
        failure = _map_http_failure(exc.failure, generation=model is not None)
        if failure is LanFailureCategory.HTTP_TIMEOUT and clock() >= scan_deadline:
            failure = LanFailureCategory.SCAN_DEADLINE_EXCEEDED
        interface_failure = failure in {
            LanFailureCategory.INTERFACE_DRIFT,
            LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
        }
        transport_observed = (
            exc.request_progress is not LanRequestProgress.NOT_STARTED and not interface_failure
        )
        generation_observed = (
            model is not None
            and exc.request_progress is LanRequestProgress.REQUEST_SENT
            and not interface_failure
        )
        return _phase_failure_observation(
            endpoint,
            failure,
            established,
            transport_observed=transport_observed,
            generation_observed=generation_observed,
        )
    except Exception:
        return _phase_failure_observation(
            endpoint,
            (
                LanFailureCategory.GENERATION_REQUEST_FAILED
                if model is not None
                else LanFailureCategory.HTTP_PROTOCOL_REJECTED
            ),
            established,
            transport_observed=True,
            generation_observed=False,
        )
    if type(response) is not LanHttpResponse:
        return _phase_failure_observation(
            endpoint,
            (
                LanFailureCategory.GENERATION_REQUEST_FAILED
                if model is not None
                else LanFailureCategory.HTTP_PROTOCOL_REJECTED
            ),
            established,
            transport_observed=True,
            generation_observed=model is not None,
        )
    try:
        canonical_response = LanHttpResponse(response.status_code, response.body)
    except (AttributeError, TypeError, ValueError):
        return _phase_failure_observation(
            endpoint,
            (
                LanFailureCategory.GENERATION_REQUEST_FAILED
                if model is not None
                else LanFailureCategory.HTTP_PROTOCOL_REJECTED
            ),
            established,
            transport_observed=True,
            generation_observed=model is not None,
        )
    if response != canonical_response:
        return _phase_failure_observation(
            endpoint,
            (
                LanFailureCategory.GENERATION_REQUEST_FAILED
                if model is not None
                else LanFailureCategory.HTTP_PROTOCOL_REJECTED
            ),
            established,
            transport_observed=True,
            generation_observed=model is not None,
        )
    return canonical_response, TransportSecurity.PLAIN_HTTP


def _phase_failure_observation(
    endpoint: ResolvedLanEndpoint,
    failure: LanFailureCategory,
    established: tuple[ApiShape, tuple[str, ...], bool, bool] | None,
    *,
    transport_observed: bool,
    generation_observed: bool,
) -> LanEndpointObservation:
    if established is None:
        return _make_observation(
            endpoint,
            reachability=Reachability.REACHABLE,
            transport_security=TransportSecurity.PLAIN_HTTP if transport_observed else None,
            failure_category=failure,
        )
    api_shape, catalog, complete, truncated = established
    capabilities = (
        _capabilities_with_generation(passed=False)
        if generation_observed
        else _not_run_capabilities()
    )
    return _make_observation(
        endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=TransportSecurity.PLAIN_HTTP,
        api_shape=api_shape,
        catalog=catalog,
        catalog_complete=complete,
        catalog_truncated=truncated,
        capabilities=capabilities,
        capability_route=(
            _capability_route(api_shape)
            if capabilities[0].status is CapabilityObservationStatus.OBSERVED_FAILURE
            else None
        ),
        selected_model_id=(
            catalog[0]
            if capabilities[0].status is CapabilityObservationStatus.OBSERVED_FAILURE
            else None
        ),
        failure_category=failure,
    )


def _derive_endpoints(
    scope: PrivateScanScope,
    candidates: tuple[LanCandidate, ...],
) -> tuple[ResolvedLanEndpoint, ...]:
    if type(candidates) is not tuple:
        raise ValueError("passive LAN candidates must be an exact tuple")
    if len(candidates) > MAX_ACTIVE_HOSTS:
        raise ValueError("passive LAN candidates may contain at most 256 entries")
    endpoints: dict[tuple[str, int], ResolvedLanEndpoint] = {
        (host, port): ResolvedLanEndpoint.from_scope(scope, host, port)
        for host in scope.active_hosts
        for port in KNOWN_MODEL_SERVICE_PORTS
    }
    for candidate in candidates:
        endpoint = _validate_candidate(scope, candidate)
        endpoints[(endpoint.address, endpoint.port)] = endpoint
    return tuple(
        endpoints[key]
        for key in sorted(
            endpoints,
            key=lambda item: (
                ipaddress.ip_address(item[0]).version,
                int(ipaddress.ip_address(item[0])),
                item[1],
            ),
        )
    )


def _validate_candidate(
    scope: PrivateScanScope,
    candidate: LanCandidate,
) -> ResolvedLanEndpoint:
    error = ValueError("passive LAN candidate failed server-side revalidation")
    if type(candidate) is not LanCandidate:
        raise error
    try:
        if (
            candidate.interface_id != scope.interface.interface_id
            or candidate.service_type not in ALLOWED_MODEL_SERVICE_TYPES
            or candidate.provider_hint is not None
            or type(candidate.address) is not str
            or "%" in candidate.address
            or candidate.port not in KNOWN_MODEL_SERVICE_PORTS
            or type(candidate.instance_name) is not str
            or type(candidate._metadata_items) is not tuple
            or type(candidate.metadata_json) is not str
        ):
            raise error
        metadata: dict[str, str] = {}
        for item in candidate._metadata_items:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                or item[0] not in _DISPLAY_METADATA_FIELDS
                or item[0] in metadata
                or not _safe_candidate_display_text(item[1], field=item[0])
            ):
                raise error
            metadata[item[0]] = item[1]
        if not _safe_candidate_display_text(candidate.instance_name, field=None):
            raise error
        rebuilt = LanCandidate._from_normalized(
            interface_id=candidate.interface_id,
            address=candidate.address,
            port=candidate.port,
            service_type=candidate.service_type,
            instance_name=candidate.instance_name,
            metadata=MappingProxyType(metadata),
        )
        if candidate != rebuilt:
            raise error
        return ResolvedLanEndpoint.from_scope(scope, candidate.address, candidate.port)
    except (AttributeError, TypeError, ValueError):
        raise error from None


def _safe_candidate_display_text(value: str, *, field: str | None) -> bool:
    try:
        _validate_display_text(
            value,
            max_bytes=255 if field is None else 300,
            allowed_dotted_value="llama.cpp" if field == "product" else None,
            allow_numeric_version=field == "version",
        )
    except (TypeError, ValueError):
        return False
    return redact_text(value) == value


def _parse_catalog(
    body: bytes,
    api_shape: ApiShape,
) -> tuple[tuple[str, ...], bool, bool, bool, bool]:
    payload = _strict_json_object(body)
    key = "models" if api_shape is ApiShape.OLLAMA_COMPATIBLE else "data"
    model_key = "name" if api_shape is ApiShape.OLLAMA_COMPATIBLE else "id"
    raw_models = payload.get(key)
    if type(raw_models) is not list:
        raise ValueError("catalog shape is invalid")
    valid: set[str] = set()
    invalid_entries = False
    typed_entries = False
    for item in raw_models:
        if type(item) is not dict:
            invalid_entries = True
            continue
        raw_model = item.get(model_key)
        if type(raw_model) is not str:
            invalid_entries = True
            continue
        typed_entries = True
        try:
            model = LanProbeModel.from_catalog(raw_model).model_id
        except (TypeError, ValueError):
            invalid_entries = True
            continue
        if redact_text(model) != model:
            invalid_entries = True
            continue
        valid.add(model)
    ordered = tuple(sorted(valid))
    truncated = len(ordered) > MAX_DISCOVERED_MODELS
    retained = ordered[:MAX_DISCOVERED_MODELS]
    complete = not invalid_entries and not truncated
    schema_established = not raw_models or typed_entries
    return retained, complete, truncated, invalid_entries, schema_established


def _strict_json_object(body: bytes) -> dict[str, object]:
    try:
        text = body.decode("utf-8", errors="strict")

        def object_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON member")
                result[key] = value
            return result

        payload = json.loads(
            text,
            object_pairs_hook=object_hook,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("LAN response must be strict JSON") from exc
    if type(payload) is not dict:
        raise ValueError("LAN response must be a JSON object")
    return payload


def _generation_response_passes(body: bytes, api_shape: ApiShape) -> bool:
    try:
        payload = _strict_json_object(body)
    except ValueError:
        return False
    if api_shape is ApiShape.OLLAMA_COMPATIBLE:
        response = payload.get("response")
        return type(response) is str and response.strip() == "OK" and payload.get("done") is True
    choices = payload.get("choices")
    if type(choices) is not list or not choices or type(choices[0]) is not dict:
        return False
    message = choices[0].get("message")
    if type(message) is not dict:
        return False
    content = message.get("content")
    return type(content) is str and content.strip() == "OK"


def _not_run_capabilities() -> tuple[LanCapabilityEvidence, ...]:
    return tuple(LanCapabilityEvidence.not_run(capability) for capability in CapabilityName)


def _capabilities_with_generation(*, passed: bool) -> tuple[LanCapabilityEvidence, ...]:
    generation = (
        LanCapabilityEvidence.observed_pass()
        if passed
        else LanCapabilityEvidence.observed_failure()
    )
    return (
        generation,
        *tuple(
            LanCapabilityEvidence.not_run(capability) for capability in tuple(CapabilityName)[1:]
        ),
    )


def _map_tcp_failure(failure: LanTransportFailure) -> LanFailureCategory:
    return {
        LanTransportFailure.CANCELLED: LanFailureCategory.CANCELLED,
        LanTransportFailure.DEADLINE_EXCEEDED: LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
        LanTransportFailure.INTERFACE_CHANGED: LanFailureCategory.INTERFACE_DRIFT,
        LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE: LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
        LanTransportFailure.TCP_TIMEOUT: LanFailureCategory.TCP_TIMEOUT,
        LanTransportFailure.TCP_REFUSED: LanFailureCategory.TCP_REFUSED,
        LanTransportFailure.TCP_UNREACHABLE: LanFailureCategory.TCP_UNREACHABLE,
        LanTransportFailure.TCP_ERROR: LanFailureCategory.TCP_ERROR,
    }.get(failure, LanFailureCategory.TCP_ERROR)


def _map_http_failure(
    failure: LanTransportFailure,
    *,
    generation: bool,
) -> LanFailureCategory:
    direct = {
        LanTransportFailure.CANCELLED: LanFailureCategory.CANCELLED,
        LanTransportFailure.DEADLINE_EXCEEDED: LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
        LanTransportFailure.INTERFACE_CHANGED: LanFailureCategory.INTERFACE_DRIFT,
        LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE: LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
        LanTransportFailure.HTTP_TIMEOUT: LanFailureCategory.HTTP_TIMEOUT,
        LanTransportFailure.HTTP_PROTOCOL_REJECTED: LanFailureCategory.HTTP_PROTOCOL_REJECTED,
        LanTransportFailure.REDIRECT_REJECTED: LanFailureCategory.REDIRECT_REJECTED,
        LanTransportFailure.RESPONSE_TOO_LARGE: LanFailureCategory.RESPONSE_TOO_LARGE,
        LanTransportFailure.UNSUPPORTED_CONTENT_ENCODING: LanFailureCategory.UNSUPPORTED_CONTENT_ENCODING,
    }
    if failure in direct:
        return direct[failure]
    return (
        LanFailureCategory.GENERATION_REQUEST_FAILED
        if generation
        else LanFailureCategory.HTTP_PROTOCOL_REJECTED
    )


def _make_observation(
    endpoint: ResolvedLanEndpoint,
    *,
    reachability: Reachability,
    transport_security: TransportSecurity | None = None,
    api_shape: ApiShape | None = None,
    catalog: tuple[str, ...] = (),
    catalog_complete: bool = False,
    catalog_truncated: bool = False,
    capabilities: tuple[LanCapabilityEvidence, ...] | None = None,
    capability_route: str | None = None,
    selected_model_id: str | None = None,
    failure_category: LanFailureCategory | None,
) -> LanEndpointObservation:
    capability_evidence = capabilities or _not_run_capabilities()
    endpoint_digest = _endpoint_binding_digest(endpoint)
    catalog_evidence_digest = _catalog_digest(
        endpoint,
        endpoint_digest,
        api_shape,
        catalog_complete,
        catalog_truncated,
        catalog,
    )
    capability_evidence_digest = _capability_digest(
        endpoint_digest,
        catalog_evidence_digest,
        api_shape,
        capability_route,
        selected_model_id,
        capability_evidence,
    )
    observation_evidence_digest = _observation_digest(
        endpoint_digest,
        reachability,
        transport_security,
        api_shape,
        catalog_evidence_digest,
        capability_evidence_digest,
        failure_category,
    )
    return LanEndpointObservation(
        endpoint=endpoint,
        endpoint_binding_digest=endpoint_digest,
        reachability=reachability,
        transport_security=transport_security,
        api_shape=api_shape,
        catalog=catalog,
        catalog_complete=catalog_complete,
        catalog_truncated=catalog_truncated,
        catalog_digest=catalog_evidence_digest,
        capabilities=capability_evidence,
        capability_route=capability_route,
        selected_model_id=selected_model_id,
        capability_digest=capability_evidence_digest,
        failure_category=failure_category,
        public_error=_PUBLIC_FAILURE_TEXT[failure_category],
        observation_digest=observation_evidence_digest,
    )


def _sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _endpoint_binding_digest(endpoint: ResolvedLanEndpoint) -> str:
    return _sha256(
        {
            "address": endpoint.address,
            "interface_id": endpoint.interface_id,
            "port": endpoint.port,
            "schema": "kestrel.lan.endpoint-binding.v1",
        }
    )


def _catalog_digest(
    endpoint: ResolvedLanEndpoint,
    endpoint_binding_digest: str,
    api_shape: ApiShape | None,
    complete: bool,
    truncated: bool,
    catalog: tuple[str, ...],
) -> str:
    return _sha256(
        {
            "address": endpoint.address,
            "api_shape": api_shape.value if api_shape is not None else None,
            "complete": complete,
            "endpoint_binding_digest": endpoint_binding_digest,
            "interface_id": endpoint.interface_id,
            "model_ids": list(catalog),
            "port": endpoint.port,
            "schema": "kestrel.lan.catalog.v1",
            "truncated": truncated,
        }
    )


def _capability_digest(
    endpoint_binding_digest: str,
    catalog_digest: str,
    api_shape: ApiShape | None,
    route: str | None,
    selected_model_id: str | None,
    capabilities: tuple[LanCapabilityEvidence, ...],
) -> str:
    return _sha256(
        {
            "api_shape": api_shape.value if api_shape is not None else None,
            "capabilities": [item.to_digest_payload() for item in capabilities],
            "catalog_digest": catalog_digest,
            "endpoint_binding_digest": endpoint_binding_digest,
            "model_id": selected_model_id,
            "route": route,
            "schema": "kestrel.lan.capability.v1",
        }
    )


def _observation_digest(
    endpoint_binding_digest: str,
    reachability: Reachability,
    transport_security: TransportSecurity | None,
    api_shape: ApiShape | None,
    catalog_digest: str,
    capability_digest: str,
    failure_category: LanFailureCategory | None,
) -> str:
    return _sha256(
        {
            "api_shape": api_shape.value if api_shape is not None else None,
            "capability_digest": capability_digest,
            "catalog_digest": catalog_digest,
            "endpoint_binding_digest": endpoint_binding_digest,
            "failure_category": (failure_category.value if failure_category is not None else None),
            "reachability": reachability.value,
            "schema": "kestrel.lan.observation.v1",
            "transport_security": (
                transport_security.value if transport_security is not None else None
            ),
        }
    )


def _capability_route(api_shape: ApiShape | None) -> str | None:
    if api_shape is ApiShape.OLLAMA_COMPATIBLE:
        return LanRequestRoute.OLLAMA_GENERATION.path
    if api_shape is ApiShape.OPENAI_COMPATIBLE:
        return LanRequestRoute.OPENAI_GENERATION.path
    return None
