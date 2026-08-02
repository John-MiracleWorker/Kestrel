"""Adversarial qualification for bounded LAN discovery (LAN plan Task 10).

Every case in tests/evals/lan_discovery/hostile_responses.json is executed
against the real owning boundary (scope validation, manual-host resolution,
HTTP transport, scanner, mDNS collector, discovery service, or route model).
The uniform invariant for every case is:

  - the hostile input NEVER expands probe destinations beyond the confirmed
    fixture scope (2 hosts x 4 known model ports);
  - the hostile input NEVER produces an enabled routing target;
  - the corpus secret sentinel NEVER appears in serialized evidence.

Disposition assertions (expected failure category, conflict, bounding) live in
the per-case ``test_hostile_case_disposition`` run so an acceptance change is
as visible as an invariant break.
"""

from __future__ import annotations

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import nested_memvid_agent.lan_mdns as lan_mdns
from nested_memvid_agent.lan_discovery_models import (
    KNOWN_MODEL_SERVICE_PORTS,
    ManualLanEndpoint,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import (
    PrivateScanScope,
    preview_private_scope,
)
from nested_memvid_agent.lan_discovery_service import (
    LanDiscoveryConflict,
    LanDiscoveryService,
    LanExpectedRevision,
    LanImportRequest,
    LanReviewRequest,
)
from nested_memvid_agent.lan_http_transport import (
    CurrentLanInterfaceInventory,
    CurrentLanInterfaceState,
    DirectLanHttpTransport,
    LanHttpResponse,
    LanRequestProgress,
    LanRequestRoute,
    LanTransportError,
    LanTransportFailure,
    authenticate_lan_source,
)
from nested_memvid_agent.lan_manual_probe import preview_manual_host
from nested_memvid_agent.lan_mdns import MdnsRecord, collect_mdns_candidates
from nested_memvid_agent.lan_scanner import (
    ApiShape,
    CapabilityName,
    LanCapabilityEvidence,
    LanFailureCategory,
    LanScanLimits,
    Reachability,
    ScanCancellation,
    TransportSecurity,
    _make_observation,
    probe_manual_lan_endpoint,
    scan_lan_scope,
)
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.routing.lan_serialization import lan_observation_to_draft
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.server_routing_routes import (
    LanReviewConfirmationRouteRequest,
)
from nested_memvid_agent.state_store import AgentStateStore

CORPUS_PATH = Path("tests/evals/lan_discovery/hostile_responses.json")

REQUIRED_CATEGORIES = (
    "public_range_rejection",
    "dns_rebinding_simulation",
    "redirect",
    "oversize_response",
    "slowloris_deadline",
    "malformed_json",
    "duplicate_mdns",
    "interface_change",
    "cancellation",
    "stale_results",
    "secret_reflection",
    "revision_races",
    "target_enablement_bypass",
)

CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
SENTINEL = CORPUS["secret_sentinel"]
FIXTURE = CORPUS["fixture"]
ALLOWED_DESTINATIONS = frozenset(
    (host, port)
    for host in FIXTURE["allowed_hosts"]
    for port in FIXTURE["known_ports"]
)

OWNER = "owner:local-runtime:v1"
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
PREVIEW_DIGEST = "sha256:" + "7" * 64


@dataclass(frozen=True)
class HostileCase:
    raw: dict[str, Any]

    @property
    def case_id(self) -> str:
        return str(self.raw["case_id"])

    @property
    def category(self) -> str:
        return str(self.raw["category"])

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.raw["params"])

    @property
    def expected(self) -> dict[str, Any]:
        return dict(self.raw["expected"])

    def body(self, key: str) -> bytes:
        """Decode a base64 body parameter with the sentinel substituted."""

        encoded = str(self.params[key])
        decoded = base64.b64decode(encoded, validate=True)
        return decoded.replace(b"${SENTINEL}", SENTINEL.encode("ascii"))

    def text(self, key: str) -> str:
        return str(self.params[key]).replace("${SENTINEL}", SENTINEL)


def load_hostile_cases() -> tuple[HostileCase, ...]:
    return tuple(HostileCase(raw) for raw in CORPUS["cases"])


CASES = load_hostile_cases()
CASE_IDS = [case.case_id for case in CASES]


@dataclass(frozen=True)
class HostileResult:
    destinations: tuple[tuple[str, int], ...]
    enabled_targets: tuple[str, ...]
    serialized_evidence: str
    disposition: dict[str, Any]


# ---------------------------------------------------------------------------
# Shared fixtures and test doubles (mirrors of the committed LAN test fakes)
# ---------------------------------------------------------------------------


def fixture_interface(address: str | None = None) -> NetworkInterface:
    return NetworkInterface.from_addresses(
        os_identity=FIXTURE["interface_os_identity"],
        display_name=FIXTURE["interface_display_name"],
        addresses=(address or FIXTURE["interface_address"],),
    )


def fixture_scope(
    *,
    address: str | None = None,
    network: str | None = None,
) -> PrivateScanScope:
    return PrivateScanScope.from_request(
        fixture_interface(address),
        network or FIXTURE["network"],
    )


def fixture_inventory(
    scope: PrivateScanScope,
    *,
    index: int | None = None,
    addresses: tuple[str, ...] | None = None,
) -> CurrentLanInterfaceInventory:
    return CurrentLanInterfaceInventory(
        (
            CurrentLanInterfaceState(
                scope.interface.os_identity,
                FIXTURE["interface_index"] if index is None else index,
                scope.interface.addresses if addresses is None else addresses,
            ),
        )
    )


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


class RecordingTcpProbe:
    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.destinations: list[tuple[str, int]] = []

    def tcp_reachable(self, scope, endpoint, source, *, deadline, cancellation) -> bool:
        del scope, source, deadline, cancellation
        self.destinations.append((endpoint.address, endpoint.port))
        return self.reachable


class RecordingHttpTransport:
    def __init__(self, responses: dict[LanRequestRoute, LanHttpResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, int, LanRequestRoute]] = []

    def request(
        self,
        scope,
        endpoint,
        source,
        route,
        *,
        deadline,
        cancellation,
        model=None,
    ) -> LanHttpResponse:
        del scope, source, deadline, cancellation, model
        self.requests.append((endpoint.address, endpoint.port, route))
        response = self.responses[route]
        if isinstance(response, Exception):
            raise response
        return response


class FakeSocket:
    def __init__(self, response: bytes = b"") -> None:
        self._response = bytearray(response)
        self.timeouts: list[float] = []
        self.connected: object | None = None
        self.sent = b""
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def setsockopt(self, level: int, option: int, value: object) -> None:
        del level, option, value

    def bind(self, address: object) -> None:
        del address

    def connect(self, address: object) -> None:
        self.connected = address

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def recv(self, size: int) -> bytes:
        if not self._response:
            return b""
        payload = bytes(self._response[:size])
        del self._response[:size]
        return payload

    def close(self) -> None:
        self.closed = True


class SocketFactory:
    def __init__(self, *responses: bytes) -> None:
        self._responses = list(responses)
        self.sockets: list[FakeSocket] = []

    def __call__(self, family: int, kind: int) -> FakeSocket:
        del family, kind
        response = self._responses.pop(0) if self._responses else b""
        socket = FakeSocket(response)
        self.sockets.append(socket)
        return socket


def http_wire(status: int, body: bytes, *, headers: tuple[tuple[str, str], ...] = ()) -> bytes:
    lines = [
        f"HTTP/1.1 {status} Status",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *(f"{name}: {value}" for name, value in headers),
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def direct_transport(
    scope: PrivateScanScope,
    sockets: SocketFactory,
    *,
    clock=lambda: 0.0,
) -> DirectLanHttpTransport:
    return DirectLanHttpTransport(
        socket_factory=sockets,
        clock=clock,
        inventory_resolver=lambda: fixture_inventory(scope),
        platform_name="Darwin",
    )


def fixture_source(scope: PrivateScanScope, endpoint: ResolvedLanEndpoint):
    return authenticate_lan_source(
        scope,
        endpoint,
        lambda: fixture_inventory(scope),
    )


def fixture_endpoint(scope: PrivateScanScope) -> ResolvedLanEndpoint:
    return ResolvedLanEndpoint.from_scope(
        scope,
        FIXTURE["probe_host"],
        FIXTURE["probe_port"],
    )


def run_scan(
    scope: PrivateScanScope,
    tcp: RecordingTcpProbe,
    http: RecordingHttpTransport,
    *,
    cancellation: ScanCancellation | None = None,
    executor=None,
    progress=None,
    inventory_resolver=None,
):
    return scan_lan_scope(
        scope,
        LanScanLimits(),
        cancellation=cancellation,
        clock=lambda: 100.0,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=(
            inventory_resolver
            if inventory_resolver is not None
            else lambda: fixture_inventory(scope)
        ),
        executor=executor,
        progress=progress,
    )


class ManualClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeMdnsSession:
    def __init__(self, callback, records, clock: ManualClock) -> None:
        self._callback = callback
        self._records = records
        self._clock = clock

    def wait(self, seconds: float) -> None:
        for record in self._records:
            self._callback(record)
        self._clock.advance(seconds)

    def close(self) -> None:
        pass


class FakeMdnsAdapterFactory:
    def __init__(self, records, clock: ManualClock) -> None:
        self._records = records
        self._clock = clock

    def __call__(self, binding, service_types, callback) -> FakeMdnsSession:
        del binding, service_types
        return FakeMdnsSession(callback, self._records, self._clock)


def mdns_record(
    *,
    instance: str,
    address: str,
    port: int,
    properties: dict[str, str] | None = None,
) -> MdnsRecord:
    service_type = "_ollama._tcp.local."
    return MdnsRecord(
        service_type=service_type,
        instance_name=f"{instance}.{service_type}",
        addresses=(address,),
        port=port,
        properties=(
            {}
            if properties is None
            else {
                str(key).encode("utf-8"): str(value).encode("utf-8")
                for key, value in properties.items()
            }
        ),
        hostname=None,
    )


def collect_fake(records, scope: PrivateScanScope):
    clock = ManualClock()
    factory = FakeMdnsAdapterFactory(records, clock)
    candidates = collect_mdns_candidates(
        scope,
        adapter_factory=factory,
        clock=clock,
        interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
            interface_index=FIXTURE["interface_index"],
            addresses=scope.interface.addresses,
        ),
    )
    return candidates


# ---------------------------------------------------------------------------
# Service-boundary helpers (mirrors of tests/test_lan_discovery_service.py)
# ---------------------------------------------------------------------------


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _capabilities(*, generation_passed: bool = True) -> tuple[LanCapabilityEvidence, ...]:
    generation = (
        LanCapabilityEvidence.observed_pass()
        if generation_passed
        else LanCapabilityEvidence.observed_failure()
    )
    return (
        generation,
        *(LanCapabilityEvidence.not_run(item) for item in tuple(CapabilityName)[1:]),
    )


def _positive_observation(scope: PrivateScanScope):
    endpoint = ResolvedLanEndpoint.from_scope(scope, FIXTURE["probe_host"], 1234)
    return _make_observation(
        endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=TransportSecurity.PLAIN_HTTP,
        api_shape=ApiShape.OPENAI_COMPATIBLE,
        catalog=("alpha",),
        catalog_complete=True,
        catalog_truncated=False,
        capabilities=_capabilities(),
        capability_route=LanRequestRoute.OPENAI_GENERATION.path,
        selected_model_id="alpha",
        failure_category=None,
    )


def _outage_observation(scope: PrivateScanScope):
    endpoint = ResolvedLanEndpoint.from_scope(scope, FIXTURE["probe_host"], 1234)
    return _make_observation(
        endpoint,
        reachability=Reachability.UNREACHABLE,
        capabilities=tuple(LanCapabilityEvidence.not_run(item) for item in CapabilityName),
        failure_category=LanFailureCategory.TCP_TIMEOUT,
    )


def _persist_completed_scan(
    state: AgentStateStore,
    *,
    scan_id: str,
    observation,
    scope: PrivateScanScope,
    observed_at: datetime,
):
    ledger = LanDiscoveryLedger(state)
    draft = ledger.create_scan(
        scan_id=scan_id,
        owner_principal=OWNER,
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits={
            "known_model_service_ports": [1234, 8000, 8080, 11434],
            "max_active_hosts": 256,
            "max_scan_concurrency": 16,
            "tcp_connect_timeout_seconds": 0.75,
            "http_probe_timeout_seconds": 2.0,
            "total_scan_deadline_seconds": 45.0,
            "max_probe_response_bytes": 262144,
            "max_discovered_models": 8,
            "mdns_window_seconds": 2.5,
        },
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        scan_id,
        "running",
        expected_revision=draft.revision,
    )
    persisted = ledger.append_observation(
        scan_id,
        lan_observation_to_draft(
            observation,
            scope=scope,
            freshness_timestamp=observed_at.isoformat().replace("+00:00", "Z"),
            source="active",
        ),
        expected_revision=running.revision,
    )
    current = ledger.get_scan(scan_id)
    assert current is not None
    completed = ledger.transition_scan(
        scan_id,
        "completed",
        expected_revision=current.revision,
        terminal_reason="completed",
        candidate_count=1,
        error_count=0,
        timeout_count=0,
    )
    assert completed.terminal_receipt_digest is not None
    return persisted, completed


def _provider_id(endpoint_binding_digest: str) -> str:
    digest = _digest(
        {
            "schema": "kestrel.lan.provider-binding.v1",
            "endpoint_binding_digest": endpoint_binding_digest,
        }
    )
    return "lan-provider-" + digest.removeprefix("sha256:")


def _target_id(provider_profile_id: str, model_id: str) -> str:
    digest = _digest(
        {
            "schema": "kestrel.lan.model-target.v1",
            "provider_profile_id": provider_profile_id,
            "model_id": model_id,
        }
    )
    return "lan-target-" + digest.removeprefix("sha256:")


def _import_request(
    observation,
    completed,
    *,
    profile_revision: int,
    target_revisions: tuple[tuple[str, int], ...],
) -> LanImportRequest:
    assert completed.terminal_receipt_digest is not None
    return LanImportRequest(
        scan_id=completed.scan_id,
        endpoint_binding_digest=observation.endpoint_binding_digest,
        expected_terminal_receipt_digest=completed.terminal_receipt_digest,
        expected_observation_digest=observation.observation_digest,
        expected_profile_revision=profile_revision,
        expected_target_revisions=tuple(
            LanExpectedRevision(resource_id, revision)
            for resource_id, revision in target_revisions
        ),
    )


def _review_material_digest(
    protected: dict[str, object],
    *,
    trust_class: str,
    privacy_acknowledgement_digest: str | None,
    intended_roles: tuple[str, ...],
    task_family_affinities: tuple[str, ...],
) -> str:
    return _digest(
        {
            "schema": "kestrel.lan.material-binding.v1",
            "provider_profile_id": protected["provider_profile_id"],
            "target_id": _target_id(
                str(protected["provider_profile_id"]),
                str(protected["model_id"]),
            ),
            "endpoint_fingerprint": protected["endpoint_fingerprint"],
            "endpoint_binding_digest": protected["endpoint_binding_digest"],
            "interface_id": protected["interface_id"],
            "confirmed_network": protected["confirmed_network"],
            "address": protected["address"],
            "port": protected["port"],
            "transport_security": protected["transport_security"],
            "certificate_sha256": protected["certificate_sha256"],
            "api_shape": protected["api_shape"],
            "model_id": protected["model_id"],
            "catalog_digest": protected["catalog_digest"],
            "capability_digest": protected["capability_digest"],
            "capability_claims": protected["capability_claims"],
            "trust_class": trust_class,
            "privacy_acknowledgement_digest": privacy_acknowledgement_digest,
            "intended_roles": list(intended_roles),
            "task_family_affinities": list(task_family_affinities),
        }
    )


def _exact_review_request(
    *,
    owner: str,
    profile_revision: int,
    target_revision: int,
    target_id: str,
    protected: dict[str, object],
    enabled: bool = False,
) -> LanReviewRequest:
    roles = ("reviewer", "worker")
    families = ("code-repair",)
    stale_reasons = tuple(protected["stale_reasons"])
    transition_receipt = protected["stale_transition_terminal_receipt_digest"]
    privacy_digest = _digest(
        {
            "schema": "kestrel.lan.privacy-acknowledgement.v1",
            "owner_principal": owner,
            "provider_profile_id": protected["provider_profile_id"],
            "target_id": target_id,
            "observation_digest": protected["observation_digest"],
            "endpoint_fingerprint": protected["endpoint_fingerprint"],
            "expected_profile_revision": profile_revision,
            "expected_target_revision": target_revision,
            "trust_class": "operator_confirmed",
            "intended_roles": list(roles),
            "task_family_affinities": list(families),
            "enabled": enabled,
            "privacy_acknowledged": True,
            "expected_stale_reasons": list(stale_reasons),
            "stale_transition_terminal_receipt_digest": transition_receipt,
        }
    )
    reviewed_material = _review_material_digest(
        protected,
        trust_class="operator_confirmed",
        privacy_acknowledgement_digest=privacy_digest,
        intended_roles=roles,
        task_family_affinities=families,
    )
    review_digest = _digest(
        {
            "schema": "kestrel.lan.review.v1",
            "privacy_acknowledgement_digest": privacy_digest,
            "expected_terminal_receipt_digest": protected["terminal_receipt_digest"],
            "expected_observation_digest": protected["observation_digest"],
            "pre_review_material_binding_digest": protected["material_binding_digest"],
            "reviewed_material_binding_digest": reviewed_material,
            "expected_stale_reasons": list(stale_reasons),
            "stale_transition_terminal_receipt_digest": transition_receipt,
        }
    )
    return LanReviewRequest(
        target_id=target_id,
        expected_profile_revision=profile_revision,
        expected_target_revision=target_revision,
        expected_terminal_receipt_digest=str(protected["terminal_receipt_digest"]),
        expected_observation_digest=str(protected["observation_digest"]),
        expected_endpoint_fingerprint=str(protected["endpoint_fingerprint"]),
        expected_material_binding_digest=str(protected["material_binding_digest"]),
        expected_review_digest=review_digest,
        expected_stale_reasons=stale_reasons,
        trust_class="operator_confirmed",
        intended_roles=roles,
        task_family_affinities=families,
        privacy_acknowledged=True,
        enabled=enabled,
    )


@dataclass
class ServiceFixture:
    state: AgentStateStore
    registry: RoutingLedger
    service: LanDiscoveryService
    observation: Any
    provider_id: str
    target_id: str
    clock_now: list[datetime]


def _import_first_positive(tmp_path: Path) -> ServiceFixture:
    scope = fixture_scope()
    clock_now = [NOW]
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation(scope)
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-hostile-initial",
        observation=observation,
        scope=scope,
        observed_at=NOW,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: clock_now[0])
    service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    return ServiceFixture(
        state=state,
        registry=registry,
        service=service,
        observation=observation,
        provider_id=provider_id,
        target_id=target_id,
        clock_now=clock_now,
    )


def _stale_via_outage(fixture: ServiceFixture, *, age_seconds: int) -> None:
    scope = fixture_scope()
    fixture.clock_now[0] = NOW + timedelta(seconds=age_seconds)
    outage = _outage_observation(scope)
    _row, outage_scan = _persist_completed_scan(
        fixture.state,
        scan_id=f"scan-hostile-outage-{age_seconds}",
        observation=outage,
        scope=scope,
        observed_at=fixture.clock_now[0],
    )
    profile = fixture.registry.get_provider_profile(fixture.provider_id)
    target = fixture.registry.get_model_target(fixture.target_id)
    assert profile is not None and target is not None
    if age_seconds <= 300:
        # Within freshness an outage is a no-op that must not cite any target.
        target_revisions: tuple[tuple[str, int], ...] = ()
    else:
        target_revisions = ((fixture.target_id, target.revision),)
    fixture.service.import_observation(
        _import_request(
            outage,
            outage_scan,
            profile_revision=profile.revision,
            target_revisions=target_revisions,
        ),
        authenticated_owner_principal=OWNER,
    )


def _target_protected(fixture: ServiceFixture) -> dict[str, object]:
    target = fixture.registry.get_model_target(fixture.target_id)
    assert target is not None
    return dict(target.target.metadata["lan_discovery"])


def _serialize_public(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Hostile case runners (one per boundary)
# ---------------------------------------------------------------------------


def _run_scope_case(case: HostileCase) -> HostileResult:
    address = case.params.get("interface_address")
    network = str(case.params["network"])
    error: str | None = None
    try:
        PrivateScanScope.from_request(fixture_interface(address), network)
    except ValueError as exc:
        error = str(exc)
    preview_error: str | None = None
    try:
        interface = fixture_interface(address)
        preview_private_scope(interface.interface_id, network, interfaces=(interface,))
    except ValueError as exc:
        preview_error = str(exc)
    disposition = {
        "error": error,
        "preview_error": preview_error,
        "rejected": error is not None and preview_error is not None,
    }
    return HostileResult((), (), _serialize_public(disposition), disposition)


def _run_manual_probe_case(case: HostileCase, tmp_path: Path) -> HostileResult:
    del tmp_path
    address = case.params.get("interface_address")
    interface = fixture_interface(address)
    answers = tuple(str(answer) for answer in case.params.get("answers", ()))
    error: str | None = None
    preview_payload: object = None
    try:
        preview = preview_manual_host(
            interface.interface_id,
            case.text("host"),
            FIXTURE["probe_port"],
            interfaces=(interface,),
                resolver=lambda _host: answers,
        )
        preview_payload = {
            "interface_id": preview.interface_id,
            "port": preview.port,
            "resolved_addresses": list(preview.resolved_addresses),
            "host_input_digest": preview.host_input_digest,
        }
    except ValueError as exc:
        error = str(exc)
    disposition = {"error": error, "preview": preview_payload, "rejected": error is not None}
    return HostileResult((), (), _serialize_public(disposition), disposition)


def _run_redirect_case(case: HostileCase) -> HostileResult:
    scope = fixture_scope()
    endpoint = fixture_endpoint(scope)
    status = int(case.params["status_code"])
    location = case.text("location")
    sockets = SocketFactory(
        http_wire(status, b"", headers=(("Location", location),)),
    )
    transport_failure: str | None = None
    transport_error_text: str | None = None
    try:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            fixture_source(scope, endpoint),
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )
    except LanTransportError as exc:
        transport_failure = exc.failure.value
        transport_error_text = str(exc)

    tcp = RecordingTcpProbe(reachable=True)
    http = RecordingHttpTransport(
        {LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(status, b"")}
    )
    observations = run_scan(scope, tcp, http)
    scanner_failures = sorted(
        {str(item.failure_category) for item in observations}
    )
    connected = [
        tuple(socket.connected[:2])  # type: ignore[index]
        for socket in sockets.sockets
        if isinstance(socket.connected, tuple)
    ]
    disposition = {
        "transport_failure": transport_failure,
        "sockets_opened": len(sockets.sockets),
        "scanner_failures": scanner_failures,
        "http_requests": len(http.requests),
    }
    evidence = _serialize_public(
        {
            "disposition": disposition,
            "transport_error": transport_error_text,
            "observations": [item.public_error for item in observations],
        }
    )
    destinations = tuple(tcp.destinations) + tuple(connected)  # type: ignore[misc]
    return HostileResult(destinations, (), evidence, disposition)


def _wire_for_case(case: HostileCase) -> bytes:
    params = case.params
    wire = str(params["wire"])
    if wire == "content_length":
        declared = int(params["declared_length"])
        body = b"x" * int(params["body_bytes"])
        lines = [
            "HTTP/1.1 200 OK",
            f"Content-Length: {declared}",
            "Connection: close",
        ]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
    if wire == "chunked":
        parts = [b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"]
        for size in params["chunk_sizes"]:
            parts.append(f"{int(size):x}\r\n".encode("ascii"))
            parts.append(b"x" * int(size))
            parts.append(b"\r\n")
        parts.append(b"0\r\n\r\n")
        return b"".join(parts)
    if wire == "chunked_extension":
        return (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"2;evil=payload\r\n{}\r\n0\r\n\r\n"
        )
    if wire == "chunked_trailer":
        return (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"0\r\nX-Evil: 1\r\n\r\n"
        )
    if wire == "content_encoding":
        body = base64.b64decode(str(params["body_b64"]), validate=True)
        return http_wire(200, body, headers=(("Content-Encoding", str(params["encoding"])),))
    raise AssertionError(f"unknown wire shape {wire}")


def _run_transport_rejection_case(case: HostileCase) -> HostileResult:
    scope = fixture_scope()
    endpoint = fixture_endpoint(scope)
    sockets = SocketFactory(_wire_for_case(case))
    failure: str | None = None
    error_text: str | None = None
    try:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            fixture_source(scope, endpoint),
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=20.0,
            cancellation=NeverCancelled(),
        )
    except LanTransportError as exc:
        failure = exc.failure.value
        error_text = str(exc)
    connected = [
        tuple(socket.connected[:2])  # type: ignore[index]
        for socket in sockets.sockets
        if isinstance(socket.connected, tuple)
    ]
    disposition = {
        "transport_failure": failure,
        "sockets_opened": len(sockets.sockets),
        "all_closed": all(socket.closed for socket in sockets.sockets),
    }
    evidence = _serialize_public({"disposition": disposition, "error": error_text})
    return HostileResult(tuple(connected), (), evidence, disposition)  # type: ignore[arg-type]


def _run_slowloris_case(case: HostileCase) -> HostileResult:
    scope = fixture_scope()
    endpoint = fixture_endpoint(scope)
    mode = str(case.params["mode"])

    if mode == "drip":
        step = float(case.params["clock_step_seconds"])

        class AdvancingClock:
            def __init__(self) -> None:
                self.now = 0.0

            def __call__(self) -> float:
                result = self.now
                self.now += step
                return result

        class DripSocket(FakeSocket):
            def recv(self, size: int) -> bytes:
                return super().recv(min(size, 1))

        class DripFactory(SocketFactory):
            def __call__(self, family: int, kind: int) -> FakeSocket:
                response = self._responses.pop(0) if self._responses else b""
                socket = DripSocket(response)
                self.sockets.append(socket)
                return socket

        clock = AdvancingClock()
        sockets: SocketFactory = DripFactory(
            b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nshort"
        )
    else:
        assert mode == "late_bytes"

        class MutableClock:
            def __init__(self) -> None:
                self.now = 0.0

            def __call__(self) -> float:
                return self.now

        class LateSocket(FakeSocket):
            def __init__(self, parts: tuple[bytes, ...], clock_ref: MutableClock) -> None:
                super().__init__()
                self._parts = list(parts)
                self._clock_ref = clock_ref
                self._reads = 0

            def recv(self, size: int) -> bytes:
                del size
                self._reads += 1
                payload = self._parts.pop(0) if self._parts else b""
                if not self._parts:
                    self._clock_ref.now = 2.001
                return payload

        class LateFactory(SocketFactory):
            def __init__(self, parts: tuple[bytes, ...], clock_ref: MutableClock) -> None:
                super().__init__()
                self._parts = parts
                self._clock_ref = clock_ref

            def __call__(self, family: int, kind: int) -> FakeSocket:
                socket = LateSocket(self._parts, self._clock_ref)
                self.sockets.append(socket)
                return socket

        clock = MutableClock()
        sockets = LateFactory(
            (
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n",
                b"{}",
            ),
            clock,
        )

    failure: str | None = None
    error_text: str | None = None
    try:
        direct_transport(scope, sockets, clock=clock).request(
            scope,
            endpoint,
            fixture_source(scope, endpoint),
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=20.0,
            cancellation=NeverCancelled(),
        )
    except LanTransportError as exc:
        failure = exc.failure.value
        error_text = str(exc)
    connected = [
        tuple(socket.connected[:2])  # type: ignore[index]
        for socket in sockets.sockets
        if isinstance(socket.connected, tuple)
    ]
    disposition = {
        "transport_failure": failure,
        "sockets_opened": len(sockets.sockets),
        "all_closed": all(socket.closed for socket in sockets.sockets),
    }
    evidence = _serialize_public({"disposition": disposition, "error": error_text})
    return HostileResult(tuple(connected), (), evidence, disposition)  # type: ignore[arg-type]


def _catalog_scan_observations(case: HostileCase):
    scope = fixture_scope()
    catalog_body = case.body("catalog_body_b64")
    responses: dict[LanRequestRoute, LanHttpResponse | Exception] = {
        LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, catalog_body),
    }
    if "generation_body_b64" in case.params:
        responses[LanRequestRoute.OLLAMA_GENERATION] = LanHttpResponse(
            200,
            case.body("generation_body_b64"),
        )
    tcp = RecordingTcpProbe(reachable=True)
    http = RecordingHttpTransport(responses)
    observations = run_scan(scope, tcp, http)
    return observations, tcp, http


def _run_malformed_json_case(case: HostileCase) -> HostileResult:
    if "body_generator" in case.params:
        depth = int(case.params["depth"])
        case = HostileCase(
            {
                **case.raw,
                "params": {
                    "catalog_body_b64": base64.b64encode(
                        b"[" * depth + b"]" * depth
                    ).decode("ascii"),
                },
            }
        )
    observations, tcp, http = _catalog_scan_observations(case)
    failures = sorted({str(item.failure_category) for item in observations})
    catalogs = sorted({tuple(item.catalog) for item in observations})
    generation_passed = any(
        item.capabilities[0].status.value == "observed_pass" for item in observations
    )
    complete = sorted({item.catalog_complete for item in observations})
    disposition = {
        "failures": failures,
        "catalogs": [list(catalog) for catalog in catalogs],
        "generation_passed": generation_passed,
        "catalog_complete": complete,
    }
    evidence = _serialize_public(
        [
            {
                "failure": str(item.failure_category),
                "public_error": item.public_error,
                "catalog": list(item.catalog),
                "digests": [
                    item.endpoint_binding_digest,
                    item.catalog_digest,
                    item.capability_digest,
                    item.observation_digest,
                ],
            }
            for item in observations
        ]
        + [{"requests": [(a, p, r.value) for a, p, r in http.requests]}]
    )
    return HostileResult(tuple(tcp.destinations), (), evidence, disposition)


def _run_secret_scanner_case(case: HostileCase) -> HostileResult:
    if case.params.get("mode") == "transport_error_detail":
        scope = fixture_scope()
        tcp = RecordingTcpProbe(reachable=True)
        http = RecordingHttpTransport(
            {
                LanRequestRoute.OLLAMA_CATALOG: LanTransportError(
                    LanTransportFailure.HTTP_PROTOCOL_REJECTED,
                    f"upstream refused with credential {SENTINEL}",
                    request_progress=LanRequestProgress.REQUEST_SENT,
                )
            }
        )
        observations = run_scan(scope, tcp, http)
        transport_texts = [
            str(http.responses[LanRequestRoute.OLLAMA_CATALOG]),
        ]
    else:
        observations, tcp, http = _catalog_scan_observations(case)
        transport_texts = []
    failures = sorted({str(item.failure_category) for item in observations})
    catalogs = sorted({tuple(item.catalog) for item in observations})
    complete = sorted({item.catalog_complete for item in observations})
    generation_passed = any(
        item.capabilities[0].status.value == "observed_pass" for item in observations
    )
    disposition = {
        "failures": failures,
        "catalogs": [list(catalog) for catalog in catalogs],
        "catalog_complete": complete,
        "generation_passed": generation_passed,
    }
    evidence = _serialize_public(
        {
            "disposition": disposition,
            "transport_errors": transport_texts,
            "public_errors": [item.public_error for item in observations],
            "catalogs": [list(item.catalog) for item in observations],
        }
    )
    return HostileResult(tuple(tcp.destinations), (), evidence, disposition)


def _mdns_records_for_case(case: HostileCase) -> tuple[list[MdnsRecord], PrivateScanScope]:
    if "flood_distinct_count" in case.params:
        scope = fixture_scope(
            address=str(case.params["interface_address"]),
            network="192.168.90.0/24",
        )
        count = int(case.params["flood_distinct_count"])
        ports = FIXTURE["known_ports"]
        records = [
            mdns_record(
                instance=f"Flood-{index}",
                address=f"192.168.90.{2 + (index % 250)}",
                port=ports[index % len(ports)],
            )
            for index in range(count)
        ]
        return records, scope
    scope = fixture_scope()
    records = [
        mdns_record(
            instance=str(item["instance"]).replace("${SENTINEL}", SENTINEL),
            address=str(item["address"]),
            port=int(item["port"]),
            properties=item.get("properties"),
        )
        for item in case.params["records"]
    ]
    return records, scope


def _run_mdns_case(case: HostileCase) -> HostileResult:
    records, scope = _mdns_records_for_case(case)
    candidates = collect_fake(records, scope)
    endpoints = [(candidate.address, candidate.port) for candidate in candidates]
    disposition = {
        "candidate_count": len(candidates),
        "unique_endpoints": len(set(endpoints)) == len(endpoints),
    }
    evidence = _serialize_public(
        [
            {
                "address": candidate.address,
                "port": candidate.port,
                "service_type": candidate.service_type,
                "instance_name": candidate.instance_name,
                "metadata": dict(candidate.metadata),
            }
            for candidate in candidates
        ]
    )
    # mDNS collection alone never probes; only endpoints inside the confirmed
    # scope may even be retained as candidates.
    in_scope = [
        item
        for item in endpoints
        if item[0] in scope.active_hosts and item[1] in KNOWN_MODEL_SERVICE_PORTS
    ]
    assert len(in_scope) == len(endpoints)
    return HostileResult((), (), evidence, disposition)


def _run_interface_change_case(case: HostileCase) -> HostileResult:
    scope = fixture_scope(network=f"{FIXTURE['probe_host']}/32")
    drift = str(case.params["drift"])
    calls = {"count": 0}

    def resolver() -> CurrentLanInterfaceInventory:
        calls["count"] += 1
        if calls["count"] == 1:
            return fixture_inventory(scope)
        if drift == "address_removed":
            return fixture_inventory(scope, addresses=("192.168.90.9/29",))
        if drift == "index_changed":
            return fixture_inventory(scope, index=FIXTURE["interface_index"] + 1)
        assert drift == "interface_removed"
        return CurrentLanInterfaceInventory(())

    endpoint = ManualLanEndpoint.from_exact_scope(
        scope,
        FIXTURE["probe_host"],
        FIXTURE["probe_port"],
    )
    tcp = RecordingTcpProbe(reachable=True)
    http = RecordingHttpTransport(
        {LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, b'{"models": []}')}
    )
    observation = probe_manual_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=ScanCancellation(),
        clock=lambda: 100.0,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=resolver,
    )
    disposition = {
        "failures": [str(observation.failure_category)],
        "http_requests": len(http.requests),
    }
    evidence = _serialize_public(
        {
            "disposition": disposition,
            "public_error": observation.public_error,
        }
    )
    return HostileResult(tuple(tcp.destinations), (), evidence, disposition)


def _run_cancellation_case(case: HostileCase) -> HostileResult:
    scope = fixture_scope()
    mode = str(case.params["mode"])
    token = ScanCancellation()
    tcp = RecordingTcpProbe(reachable=True)
    http = RecordingHttpTransport(
        {LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, b'{"models": []}')}
    )
    progress = None
    if mode == "pre_cancelled":
        token.cancel()
    elif mode == "cancel_on_first_tcp":
        class CancellingTcpProbe(RecordingTcpProbe):
            def tcp_reachable(self, *args, **kwargs) -> bool:
                token.cancel()
                return super().tcp_reachable(*args, **kwargs)

        tcp = CancellingTcpProbe(reachable=False)
    else:
        assert mode == "progress_cancels"

        def progress(event) -> None:
            if event.phase == "completed":
                token.cancel()

    with ThreadPoolExecutor(max_workers=1) as executor:
        observations = run_scan(
            scope,
            tcp,
            http,
            cancellation=token,
            executor=executor,
            progress=progress,
        )
    cancelled = sum(
        1
        for item in observations
        if item.failure_category is LanFailureCategory.CANCELLED
    )
    disposition = {
        "failures": sorted({str(item.failure_category) for item in observations}),
        "cancelled_observations": cancelled,
        "observation_count": len(observations),
        "destinations": len(tcp.destinations),
    }
    evidence = _serialize_public(
        {
            "disposition": disposition,
            "public_errors": [item.public_error for item in observations],
        }
    )
    return HostileResult(tuple(tcp.destinations), (), evidence, disposition)


def _service_public_evidence(fixture: ServiceFixture) -> str:
    profile = fixture.registry.get_provider_profile(fixture.provider_id)
    target = fixture.registry.get_model_target(fixture.target_id)
    payload = {
        "profile": None if profile is None else profile.profile.to_public_payload(),
        "target": None if target is None else target.target.to_public_payload(),
    }
    return _serialize_public(payload)


def _enabled_target_ids(fixture: ServiceFixture) -> tuple[str, ...]:
    target = fixture.registry.get_model_target(fixture.target_id)
    if target is not None and target.target.enabled is True:
        return (fixture.target_id,)
    return ()


def _run_stale_results_case(case: HostileCase, tmp_path: Path) -> HostileResult:
    fixture = _import_first_positive(tmp_path)
    mode = str(case.params["mode"])
    age = int(case.params["outage_age_seconds"])
    _stale_via_outage(fixture, age_seconds=age)

    protected = _target_protected(fixture)
    profile = fixture.registry.get_provider_profile(fixture.provider_id)
    target = fixture.registry.get_model_target(fixture.target_id)
    assert profile is not None and target is not None
    request = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=fixture.target_id,
        protected=protected,
    )
    if mode == "wrong_stale_expectation":
        request = replace(request, expected_stale_reasons=())

    review_blocked = False
    review_error: str | None = None
    try:
        fixture.service.review_lan_target(request, authenticated_owner_principal=OWNER)
    except LanDiscoveryConflict as exc:
        review_blocked = True
        review_error = str(exc)

    disposition = {
        "target_stale": bool(protected["stale_reasons"]),
        "review_blocked": review_blocked,
        "target_enabled": fixture.registry.get_model_target(fixture.target_id)
        .target.enabled,
        "review_error": review_error,
    }
    evidence = _serialize_public(
        {
            "disposition": disposition,
            "public": _service_public_evidence(fixture),
        }
    )
    return HostileResult((), _enabled_target_ids(fixture), evidence, disposition)


def _run_revision_race_case(case: HostileCase, tmp_path: Path) -> HostileResult:
    mode = str(case.params["mode"])
    if mode == "forged_bool_revision":
        fixture = _import_first_positive(tmp_path)
        error: str | None = None
        try:
            _import_request(
                fixture.observation,
                replace(
                    _persist_completed_scan(
                        fixture.state,
                        scan_id="scan-hostile-bool-revision",
                        observation=fixture.observation,
                        scope=fixture_scope(),
                        observed_at=fixture.clock_now[0],
                    )[1],
                ),
                profile_revision=True,  # type: ignore[arg-type]
                target_revisions=((fixture.target_id, 0),),
            )
        except (TypeError, ValueError) as exc:
            error = str(exc)
        disposition = {"value_error": error is not None, "error": error, "mutation": False}
        evidence = _serialize_public(disposition)
        return HostileResult((), _enabled_target_ids(fixture), evidence, disposition)

    fixture = _import_first_positive(tmp_path)
    if mode == "import_future_profile_revision":
        before = _service_public_evidence(fixture)
        conflict: str | None = None
        scope = fixture_scope()
        observation = _positive_observation(scope)
        _row, completed = _persist_completed_scan(
            fixture.state,
            scan_id="scan-hostile-future-revision",
            observation=observation,
            scope=scope,
            observed_at=fixture.clock_now[0],
        )
        try:
            fixture.service.import_observation(
                _import_request(
                    observation,
                    completed,
                    profile_revision=int(case.params["claimed_profile_revision"]),
                    target_revisions=((fixture.target_id, 0),),
                ),
                authenticated_owner_principal=OWNER,
            )
        except LanDiscoveryConflict as exc:
            conflict = str(exc)
        after = _service_public_evidence(fixture)
        disposition = {
            "conflict": conflict is not None,
            "conflict_error": conflict,
            "mutation": before != after,
        }
    else:
        assert mode == "review_stale_target_revision"
        profile = fixture.registry.get_provider_profile(fixture.provider_id)
        target = fixture.registry.get_model_target(fixture.target_id)
        assert profile is not None and target is not None
        before = _service_public_evidence(fixture)
        request = _exact_review_request(
            owner=OWNER,
            profile_revision=profile.revision,
            target_revision=target.revision + int(case.params["revision_skew"]),
            target_id=fixture.target_id,
            protected=_target_protected(fixture),
        )
        conflict = None
        try:
            fixture.service.review_lan_target(request, authenticated_owner_principal=OWNER)
        except LanDiscoveryConflict as exc:
            conflict = str(exc)
        after = _service_public_evidence(fixture)
        disposition = {
            "conflict": conflict is not None,
            "conflict_error": conflict,
            "mutation": before != after,
        }
    evidence = _serialize_public(
        {"disposition": disposition, "public": _service_public_evidence(fixture)}
    )
    return HostileResult((), _enabled_target_ids(fixture), evidence, disposition)


def _run_enablement_bypass_case(case: HostileCase, tmp_path: Path) -> HostileResult:
    mode = str(case.params["mode"])
    fixture = _import_first_positive(tmp_path)
    profile = fixture.registry.get_provider_profile(fixture.provider_id)
    target = fixture.registry.get_model_target(fixture.target_id)
    assert profile is not None and target is not None

    if mode == "import_draft_posture":
        disposition = {
            "profile_enabled": profile.profile.enabled,
            "targets_enabled": 1 if target.target.enabled else 0,
            "trust_class": target.target.trust_class,
            "profile_trust_class": profile.profile.trust_class,
            "secret_ref": profile.profile.secret_ref,
        }
        evidence = _serialize_public(
            {"disposition": disposition, "public": _service_public_evidence(fixture)}
        )
        return HostileResult((), _enabled_target_ids(fixture), evidence, disposition)

    request = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=fixture.target_id,
        protected=_target_protected(fixture),
    )
    conflict: str | None = None
    value_error: str | None = None
    if mode == "enable_flip_on_review_digests":
        tampered = replace(request, enabled=True)
        try:
            fixture.service.review_lan_target(
                tampered,
                authenticated_owner_principal=OWNER,
            )
        except LanDiscoveryConflict as exc:
            conflict = str(exc)
    elif mode == "trust_class_forgery":
        try:
            replace(request, trust_class=str(case.params["forged_trust_class"]))
        except (TypeError, ValueError) as exc:
            value_error = str(exc)
    else:
        assert mode == "privacy_acknowledgement_missing"
        tampered = replace(request, privacy_acknowledged=False)
        try:
            fixture.service.review_lan_target(
                tampered,
                authenticated_owner_principal=OWNER,
            )
        except LanDiscoveryConflict as exc:
            conflict = str(exc)
    disposition = {
        "conflict": conflict is not None,
        "conflict_error": conflict,
        "value_error": value_error is not None,
        "value_error_text": value_error,
        "target_enabled": fixture.registry.get_model_target(fixture.target_id)
        .target.enabled,
    }
    evidence = _serialize_public(
        {"disposition": disposition, "public": _service_public_evidence(fixture)}
    )
    return HostileResult((), _enabled_target_ids(fixture), evidence, disposition)


def _run_route_model_case(case: HostileCase) -> HostileResult:
    del case
    errors: list[str] = []
    base = {
        "intended_roles": ["reviewer"],
        "task_family_affinities": ["code-repair"],
        "enabled": True,
        "preview_digest": PREVIEW_DIGEST,
        "privacy_acknowledged": True,
        "confirmed": True,
    }
    for field, value in (
        ("privacy_acknowledged", False),
        ("confirmed", False),
        ("enabled", "yes"),
    ):
        try:
            LanReviewConfirmationRouteRequest(**{**base, field: value})
        except ValueError as exc:
            errors.append(f"{field}:{type(exc).__name__}")
    disposition = {"validation_errors": errors, "rejected": len(errors) == 3}
    return HostileResult((), (), _serialize_public(disposition), disposition)


_SERVICE_CATEGORIES = {"stale_results", "revision_races", "target_enablement_bypass"}


def run_hostile_case(case: HostileCase, tmp_path: Path) -> HostileResult:
    """Execute one corpus case against its owning boundary."""

    if case.category == "public_range_rejection":
        return _run_scope_case(case)
    if case.category == "dns_rebinding_simulation":
        return _run_manual_probe_case(case, tmp_path)
    if case.category == "redirect":
        return _run_redirect_case(case)
    if case.category == "oversize_response":
        return _run_transport_rejection_case(case)
    if case.category == "slowloris_deadline":
        return _run_slowloris_case(case)
    if case.category == "malformed_json":
        return _run_malformed_json_case(case)
    if case.category == "duplicate_mdns":
        return _run_mdns_case(case)
    if case.category == "interface_change":
        return _run_interface_change_case(case)
    if case.category == "cancellation":
        return _run_cancellation_case(case)
    if case.category == "stale_results":
        return _run_stale_results_case(case, tmp_path)
    if case.category == "secret_reflection":
        boundary = str(case.raw["boundary"])
        if boundary == "scanner":
            return _run_secret_scanner_case(case)
        if boundary == "mdns":
            return _run_mdns_case(case)
        assert boundary == "manual_probe"
        return _run_manual_probe_case(case, tmp_path)
    if case.category == "revision_races":
        return _run_revision_race_case(case, tmp_path)
    if case.category == "target_enablement_bypass":
        if case.raw["boundary"] == "route_model":
            return _run_route_model_case(case)
        return _run_enablement_bypass_case(case, tmp_path)
    raise AssertionError(f"uncovered hostile category {case.category}")


# ---------------------------------------------------------------------------
# Corpus structure and the uniform adversarial invariant
# ---------------------------------------------------------------------------


def test_hostile_corpus_is_structurally_closed() -> None:
    assert CORPUS["schema"] == "kestrel.lan-hostile-corpus.v1"
    assert isinstance(SENTINEL, str) and len(SENTINEL) >= 12
    assert len(CASE_IDS) == len(set(CASE_IDS))
    for case in CASES:
        assert set(case.raw) >= {"case_id", "category", "summary", "boundary", "params", "expected"}
        assert case.category in REQUIRED_CATEGORIES
        assert case.raw["summary"]


def test_hostile_corpus_covers_every_required_category() -> None:
    present = {case.category for case in CASES}
    assert present == set(REQUIRED_CATEGORIES)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_hostile_lan_response_never_expands_probe_or_authority(
    case: HostileCase,
    tmp_path: Path,
) -> None:
    result = run_hostile_case(case, tmp_path)
    assert set(result.destinations) <= ALLOWED_DESTINATIONS
    assert result.enabled_targets == ()
    assert SENTINEL not in result.serialized_evidence


# ---------------------------------------------------------------------------
# Per-case dispositions (what the boundary must do, not only what it must not)
# ---------------------------------------------------------------------------


def _assert_disposition(case: HostileCase, result: HostileResult) -> None:
    expected = case.expected
    observed = result.disposition
    if case.category == "public_range_rejection":
        assert observed["rejected"] is True
        assert expected["error_match"] in str(observed["error"])
        assert expected["error_match"] in str(observed["preview_error"])
        return
    if case.category in {"dns_rebinding_simulation", "secret_reflection"} and observed.get(
        "rejected"
    ) is not None:
        assert observed["rejected"] is True
        assert expected["error_match"] in str(observed["error"])
        return
    if case.category == "redirect":
        assert observed["transport_failure"] == expected["transport_failure"]
        assert observed["sockets_opened"] == expected["sockets_opened"]
        assert observed["scanner_failures"] == [expected["scanner_failure"]]
        return
    if case.category in {"oversize_response", "slowloris_deadline"}:
        assert observed["transport_failure"] == expected["transport_failure"]
        assert observed["all_closed"] is True
        return
    if case.category == "malformed_json":
        assert observed["failures"] == [str(expected["failure_category"])]
        assert observed["catalogs"] == [expected["catalog"]]
        assert observed["generation_passed"] is bool(expected.get("generation_passed", False))
        return
    if case.category == "duplicate_mdns":
        assert observed["candidate_count"] == expected["candidate_count"]
        assert observed["unique_endpoints"] is True
        return
    if case.category == "interface_change":
        assert observed["failures"] == [expected["failure_category"]]
        assert observed["http_requests"] == expected["http_requests"]
        return
    if case.category == "cancellation":
        assert observed["destinations"] <= expected["max_destinations"]
        if expected["max_destinations"] == 0:
            assert observed["cancelled_observations"] == observed["observation_count"]
        else:
            assert observed["cancelled_observations"] >= observed["observation_count"] - 1
        return
    if case.category == "stale_results":
        assert observed["target_stale"] is expected["target_stale"]
        assert observed["review_blocked"] is expected["review_blocked"]
        assert observed["target_enabled"] is expected["target_enabled"]
        return
    if case.category == "secret_reflection":
        if "failure_category" in expected:
            assert observed["failures"] == [str(expected["failure_category"])]
        if "catalog" in expected:
            assert observed["catalogs"] == [expected["catalog"]]
        if "catalog_complete" in expected:
            assert observed["catalog_complete"] == [expected["catalog_complete"]]
        if "candidate_count" in expected:
            assert observed["candidate_count"] == expected["candidate_count"]
        return
    if case.category == "revision_races":
        if expected.get("value_error"):
            assert observed["value_error"] is True
        else:
            assert observed["conflict"] is True
        assert observed["mutation"] is False
        return
    if case.category == "target_enablement_bypass":
        if expected.get("validation_error"):
            assert observed["rejected"] is True
            return
        if expected.get("value_error"):
            assert observed["value_error"] is True
        if expected.get("conflict"):
            assert observed["conflict"] is True
        if "profile_enabled" in expected:
            assert observed["profile_enabled"] is expected["profile_enabled"]
            assert observed["targets_enabled"] == expected["targets_enabled"]
            assert observed["trust_class"] == expected["trust_class"]
            assert observed["profile_trust_class"] == expected["trust_class"]
            assert observed["secret_ref"] is None
        if "target_enabled" in expected:
            assert observed["target_enabled"] is expected["target_enabled"]
        return
    raise AssertionError(f"no disposition contract for {case.category}")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_hostile_case_disposition(case: HostileCase, tmp_path: Path) -> None:
    result = run_hostile_case(case, tmp_path)
    _assert_disposition(case, result)
