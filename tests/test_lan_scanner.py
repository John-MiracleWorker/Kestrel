from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import unicodedata
from dataclasses import FrozenInstanceError, replace
from importlib import import_module

import pytest

import nested_memvid_agent.lan_scanner as lan_scanner_module
from nested_memvid_agent.lan_discovery_models import (
    LanScanLimits,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_http_transport import (
    AuthenticatedLanSource,
    CurrentLanInterfaceInventory,
    CurrentLanInterfaceState,
    LanHttpResponse,
    LanRequestProgress,
    LanRequestRoute,
    LanTransportError,
    LanTransportFailure,
)
from nested_memvid_agent.lan_mdns import LanCandidate
from nested_memvid_agent.lan_scanner import (
    ApiShape,
    CapabilityName,
    CapabilityObservationStatus,
    CapabilityProvenance,
    LanFailureCategory,
    LanScanProgress,
    Reachability,
    ScanCancellation,
    TransportSecurity,
    probe_lan_endpoint,
    scan_lan_scope,
)


def interface_fixture(*addresses: str) -> NetworkInterface:
    return NetworkInterface.from_addresses(
        os_identity="darwin:en7",
        display_name="Private adapter",
        addresses=addresses or ("192.168.60.1/30",),
    )


def scope_fixture(
    address: str = "192.168.60.1/30",
    network: str = "192.168.60.0/30",
) -> PrivateScanScope:
    return PrivateScanScope.from_request(interface_fixture(address), network)


def current_inventory(scope: PrivateScanScope) -> CurrentLanInterfaceInventory:
    return CurrentLanInterfaceInventory(
        (
            CurrentLanInterfaceState(
                scope.interface.os_identity,
                7,
                scope.interface.addresses,
            ),
        )
    )


def manual_endpoint_type():
    return import_module("nested_memvid_agent.lan_discovery_models").ManualLanEndpoint


def probe_manual_lan_endpoint(*args, **kwargs):
    return lan_scanner_module.probe_manual_lan_endpoint(*args, **kwargs)


class RecordingTcpProbe:
    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.destinations: list[tuple[str, int]] = []
        self.sources: list[AuthenticatedLanSource] = []

    def tcp_reachable(
        self,
        scope: PrivateScanScope,
        endpoint: ResolvedLanEndpoint,
        source: AuthenticatedLanSource,
        *,
        deadline: float,
        cancellation: ScanCancellation,
    ) -> bool:
        del scope, deadline, cancellation
        self.destinations.append((endpoint.address, endpoint.port))
        self.sources.append(source)
        return self.reachable


class RecordingHttpTransport:
    def __init__(self, responses: dict[LanRequestRoute, LanHttpResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, int, LanRequestRoute, str | None]] = []
        self.deadlines: list[float] = []

    def request(
        self,
        scope: PrivateScanScope,
        endpoint: ResolvedLanEndpoint,
        source: AuthenticatedLanSource,
        route: LanRequestRoute,
        *,
        deadline: float,
        cancellation: ScanCancellation,
        model: object | None = None,
    ) -> LanHttpResponse:
        del scope, source, cancellation
        model_id = getattr(model, "model_id", None)
        self.requests.append((endpoint.address, endpoint.port, route, model_id))
        self.deadlines.append(deadline)
        response = self.responses[route]
        if isinstance(response, Exception):
            raise response
        return response


def request_progress(value: str) -> LanRequestProgress:
    return LanRequestProgress(value)


def candidate(scope: PrivateScanScope, address: str, port: int) -> LanCandidate:
    return LanCandidate._from_normalized(
        interface_id=scope.interface.interface_id,
        address=address,
        port=port,
        service_type="_ollama._tcp.local.",
        instance_name="Display only",
        metadata={"display_name": "Display only"},
    )


def scan(
    scope: PrivateScanScope,
    tcp: RecordingTcpProbe,
    http: RecordingHttpTransport,
    *,
    candidates: tuple[LanCandidate, ...] = (),
    cancellation: ScanCancellation | None = None,
    clock=lambda: 100.0,
):
    return scan_lan_scope(
        scope,
        LanScanLimits(),
        candidates=candidates,
        cancellation=cancellation,
        clock=clock,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )


def test_scanner_derives_exact_sorted_matrix_and_ignores_hostile_candidates() -> None:
    scope = scope_fixture()
    tcp = RecordingTcpProbe(reachable=False)
    http = RecordingHttpTransport({})
    hostile = object.__new__(LanCandidate)
    object.__setattr__(hostile, "interface_id", scope.interface.interface_id)
    object.__setattr__(hostile, "address", "8.8.8.8")
    object.__setattr__(hostile, "port", 22)
    object.__setattr__(hostile, "service_type", "_ollama._tcp.local.")
    object.__setattr__(hostile, "instance_name", "ignored")
    object.__setattr__(hostile, "_metadata_items", ())
    object.__setattr__(hostile, "metadata_json", "{}")

    with pytest.raises(ValueError, match="passive LAN candidate"):
        scan(scope, tcp, http, candidates=(hostile,))
    assert tcp.destinations == []

    observations = scan(scope, tcp, http)
    expected = [
        (host, port)
        for host in ("192.168.60.1", "192.168.60.2")
        for port in (1234, 8000, 8080, 11434)
    ]
    assert [(item.endpoint.address, item.endpoint.port) for item in observations] == expected
    assert sorted(tcp.destinations, key=lambda item: (item[0], item[1])) == sorted(expected)


def test_passive_ipv6_candidate_contributes_only_an_exact_known_port_endpoint() -> None:
    scope = scope_fixture("fd00::7/64", "fd00::/64")
    tcp = RecordingTcpProbe(reachable=False)
    http = RecordingHttpTransport({})

    scan(scope, tcp, http, candidates=(candidate(scope, "fd00::8", 11434),))

    assert tcp.destinations == [("fd00::8", 11434)]


@pytest.mark.parametrize(
    ("interface_address", "network", "address", "port", "source_address"),
    [
        ("192.168.60.7/24", "192.168.60.8/32", "192.168.60.8", 5001, "192.168.60.7"),
        (
            "192.168.60.7/24",
            "192.168.60.8/32",
            "192.168.60.8",
            11434,
            "192.168.60.7",
        ),
        ("fe80::7/64", "fe80::8/128", "fe80::8", 5001, "fe80::7"),
    ],
)
def test_manual_scanner_probes_one_exact_endpoint_without_expanding_a_port_matrix(
    interface_address: str,
    network: str,
    address: str,
    port: int,
    source_address: str,
) -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture(interface_address, network)
    endpoint = manual_type.from_exact_scope(scope, address, port)
    tcp = RecordingTcpProbe()
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200,
                b'{"models":[{"name":"safe-model"}]}',
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                200,
                b'{"response":"OK","done":true}',
            ),
        }
    )

    observation = probe_manual_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=ScanCancellation(),
        clock=lambda: 100.0,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert type(observation.endpoint) is manual_type
    assert observation.endpoint.kind == "manual"
    assert tcp.destinations == [(address, port)]
    assert [item.source_address for item in tcp.sources] == [source_address]
    assert [
        (address, request_port, route) for address, request_port, route, _model in http.requests
    ] == [
        (address, port, LanRequestRoute.OLLAMA_CATALOG),
        (address, port, LanRequestRoute.OLLAMA_GENERATION),
    ]
    assert observation.reachability is Reachability.REACHABLE
    assert observation.failure_category is None


def test_manual_scanner_rejects_automatic_endpoint_authority_before_transport() -> None:
    scope = scope_fixture("192.168.60.7/24", "192.168.60.8/32")
    automatic = ResolvedLanEndpoint.from_scope(scope, "192.168.60.8", 11434)
    tcp = RecordingTcpProbe()
    http = RecordingHttpTransport({})

    with pytest.raises((TypeError, ValueError), match="manual"):
        probe_manual_lan_endpoint(
            scope,
            automatic,
            scan_deadline=145.0,
            cancellation=ScanCancellation(),
            clock=lambda: 100.0,
            tcp_probe=tcp,
            http_transport=http,
            interface_inventory_resolver=lambda: current_inventory(scope),
        )

    assert tcp.destinations == []
    assert http.requests == []


def test_automatic_scanner_rejects_manual_endpoint_authority_before_transport() -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.60.7/24", "192.168.60.8/32")
    manual = manual_type.from_exact_scope(scope, "192.168.60.8", 11434)
    tcp = RecordingTcpProbe()
    http = RecordingHttpTransport({})

    with pytest.raises((TypeError, ValueError), match="endpoint|known model-service ports"):
        probe_lan_endpoint(
            scope,
            manual,
            scan_deadline=145.0,
            cancellation=ScanCancellation(),
            clock=lambda: 100.0,
            tcp_probe=tcp,
            http_transport=http,
            interface_inventory_resolver=lambda: current_inventory(scope),
        )

    assert tcp.destinations == []
    assert http.requests == []


def test_cancelled_manual_endpoint_retains_manual_provenance_without_transport() -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("fd00::7/64", "fd00::8/128")
    endpoint = manual_type.from_exact_scope(scope, "fd00::8", 5001)
    cancellation = ScanCancellation()
    cancellation.cancel()
    tcp = RecordingTcpProbe()
    http = RecordingHttpTransport({})

    observation = probe_manual_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=cancellation,
        clock=lambda: 100.0,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert type(observation.endpoint) is manual_type
    assert observation.endpoint.kind == "manual"
    assert observation.reachability is Reachability.NOT_ATTEMPTED
    assert observation.failure_category is LanFailureCategory.CANCELLED
    assert tcp.destinations == []
    assert http.requests == []


def test_expired_manual_endpoint_admits_no_transport() -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.60.7/24", "192.168.60.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.60.8", 5001)
    tcp = RecordingTcpProbe()
    http = RecordingHttpTransport({})

    observation = probe_manual_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=100.0,
        cancellation=ScanCancellation(),
        clock=lambda: 100.0,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert observation.endpoint.kind == "manual"
    assert observation.reachability is Reachability.NOT_ATTEMPTED
    assert observation.failure_category is LanFailureCategory.SCAN_DEADLINE_EXCEEDED
    assert tcp.destinations == []
    assert http.requests == []


def test_manual_endpoint_shares_the_capped_absolute_deadline_across_all_phases() -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.60.7/24", "192.168.60.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.60.8", 5001)

    class MutableClock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = MutableClock()

    class AdvancingTcpProbe(RecordingTcpProbe):
        def __init__(self) -> None:
            super().__init__()
            self.deadlines: list[float] = []

        def tcp_reachable(self, *args, deadline: float, **kwargs):
            self.deadlines.append(deadline)
            clock.now = 100.1
            return super().tcp_reachable(*args, deadline=deadline, **kwargs)

    class AdvancingHttpTransport(RecordingHttpTransport):
        def request(self, *args, deadline: float, **kwargs):
            response = super().request(*args, deadline=deadline, **kwargs)
            clock.now += 0.1
            return response

    tcp = AdvancingTcpProbe()
    http = AdvancingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200,
                b'{"models":[{"name":"safe-model"}]}',
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                200,
                b'{"response":"OK","done":true}',
            ),
        }
    )

    observation = probe_manual_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=100.5,
        cancellation=ScanCancellation(),
        clock=clock,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert tcp.deadlines == [100.5]
    assert http.deadlines == [100.5, 100.5]
    assert [request[2] for request in http.requests] == [
        LanRequestRoute.OLLAMA_CATALOG,
        LanRequestRoute.OLLAMA_GENERATION,
    ]
    assert observation.reachability is Reachability.REACHABLE
    assert observation.failure_category is None


def test_manual_endpoint_reuses_one_roomy_http_phase_deadline_after_tcp() -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.60.7/24", "192.168.60.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.60.8", 5001)

    class MutableClock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = MutableClock()

    class AdvancingTcpProbe(RecordingTcpProbe):
        def __init__(self) -> None:
            super().__init__()
            self.deadlines: list[float] = []

        def tcp_reachable(self, *args, deadline: float, **kwargs):
            self.deadlines.append(deadline)
            clock.now = 100.1
            return super().tcp_reachable(*args, deadline=deadline, **kwargs)

    class AdvancingHttpTransport(RecordingHttpTransport):
        def request(self, *args, deadline: float, **kwargs):
            response = super().request(*args, deadline=deadline, **kwargs)
            clock.now += 0.1
            return response

    tcp = AdvancingTcpProbe()
    http = AdvancingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200,
                b'{"models":[{"name":"safe-model"}]}',
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                200,
                b'{"response":"OK","done":true}',
            ),
        }
    )

    observation = probe_manual_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=ScanCancellation(),
        clock=clock,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert tcp.deadlines == [100.75]
    assert http.deadlines == [102.1, 102.1]
    assert [request[2] for request in http.requests] == [
        LanRequestRoute.OLLAMA_CATALOG,
        LanRequestRoute.OLLAMA_GENERATION,
    ]
    assert observation.reachability is Reachability.REACHABLE
    assert observation.failure_category is None


def test_manual_unusual_port_openai_fallback_uses_one_exact_request_sequence() -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.60.7/24", "192.168.60.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.60.8", 5001)

    class MutableClock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = MutableClock()

    class AdvancingTcpProbe(RecordingTcpProbe):
        def __init__(self) -> None:
            super().__init__()
            self.deadlines: list[float] = []

        def tcp_reachable(self, *args, deadline: float, **kwargs):
            self.deadlines.append(deadline)
            clock.now = 100.1
            return super().tcp_reachable(*args, deadline=deadline, **kwargs)

    class AdvancingHttpTransport(RecordingHttpTransport):
        def request(self, *args, deadline: float, **kwargs):
            response = super().request(*args, deadline=deadline, **kwargs)
            clock.now += 0.1
            return response

    tcp = AdvancingTcpProbe()
    http = AdvancingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(404, b"missing"),
            LanRequestRoute.OPENAI_CATALOG: LanHttpResponse(
                200,
                b'{"data":[{"id":"safe-model"}]}',
            ),
            LanRequestRoute.OPENAI_GENERATION: LanHttpResponse(
                200,
                b'{"choices":[{"message":{"content":"OK"}}]}',
            ),
        }
    )

    observation = probe_manual_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=ScanCancellation(),
        clock=clock,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert tcp.destinations == [("192.168.60.8", 5001)]
    assert tcp.deadlines == [100.75]
    assert http.requests == [
        ("192.168.60.8", 5001, LanRequestRoute.OLLAMA_CATALOG, None),
        ("192.168.60.8", 5001, LanRequestRoute.OPENAI_CATALOG, None),
        ("192.168.60.8", 5001, LanRequestRoute.OPENAI_GENERATION, "safe-model"),
    ]
    assert http.deadlines == [102.1, 102.1, 102.1]
    assert observation.endpoint == endpoint
    assert observation.api_shape is ApiShape.OPENAI_COMPATIBLE
    assert observation.catalog == ("safe-model",)
    assert observation.selected_model_id == "safe-model"
    generation, *untested = observation.capabilities
    assert generation.capability is CapabilityName.GENERATION
    assert generation.status is CapabilityObservationStatus.OBSERVED_PASS
    assert generation.provenance is CapabilityProvenance.OBSERVED
    assert generation.supported is True
    assert all(item.status is CapabilityObservationStatus.NOT_RUN for item in untested)
    assert all(item.provenance is CapabilityProvenance.NOT_RUN for item in untested)
    assert all(item.supported is None for item in untested)
    assert observation.failure_category is None


@pytest.mark.parametrize(
    "mutator",
    [
        lambda scope: replace(scope, active_hosts=("8.8.8.8",)),
        lambda scope: replace(scope, network="192.168.61.0/30"),
        lambda scope: replace(
            scope,
            interface=replace(scope.interface, interface_id="sha256:" + "0" * 64),
        ),
    ],
)
def test_forged_or_stale_scope_is_rejected_before_transport(mutator) -> None:
    original = scope_fixture()
    forged = mutator(original)
    tcp = RecordingTcpProbe()

    with pytest.raises(ValueError, match="canonical confirmed scope"):
        scan(forged, tcp, RecordingHttpTransport({}))
    assert tcp.destinations == []


def test_passive_candidates_require_an_exact_bounded_tuple() -> None:
    scope = scope_fixture()
    tcp = RecordingTcpProbe(reachable=False)
    valid = candidate(scope, "192.168.60.2", 11434)

    for malformed in ([valid], tuple(valid for _ in range(257))):
        with pytest.raises(ValueError, match="tuple|at most 256"):
            scan_lan_scope(
                scope,
                LanScanLimits(),
                candidates=malformed,  # type: ignore[arg-type]
                tcp_probe=tcp,
                http_transport=RecordingHttpTransport({}),
                interface_inventory_resolver=lambda: current_inventory(scope),
            )
    assert tcp.destinations == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interface_id", "sha256:" + "0" * 64),
        ("service_type", "_ssh._tcp.local."),
        ("port", 22),
        ("address", "192.168.61.8"),
        ("_metadata_items", (("url", "http://evil.invalid"),)),
        ("_metadata_items", (("display_name", "server.local"),)),
        ("_metadata_items", (("display_name", "Use 'model.dev' locally."),)),
        ("_metadata_items", (("display_name", "server.local,"),)),
        ("_metadata_items", (("display_name", "Try 192.168.60.8"),)),
    ],
)
def test_passive_candidate_authority_fields_are_revalidated_before_transport(
    field: str,
    value: object,
) -> None:
    scope = scope_fixture()
    forged = candidate(scope, "192.168.60.2", 11434)
    object.__setattr__(forged, field, value)
    tcp = RecordingTcpProbe()

    with pytest.raises(ValueError, match="passive LAN candidate"):
        scan(scope, tcp, RecordingHttpTransport({}), candidates=(forged,))
    assert tcp.destinations == []


def test_passive_candidate_subclasses_cannot_supply_provider_identity() -> None:
    scope = scope_fixture()

    class ProviderCandidate(LanCandidate):
        @property
        def provider_hint(self) -> str:
            return "ollama"

    forged = object.__new__(ProviderCandidate)
    for name, value in vars(candidate(scope, "192.168.60.2", 11434)).items():
        object.__setattr__(forged, name, value)
    tcp = RecordingTcpProbe()

    with pytest.raises(ValueError, match="passive LAN candidate"):
        scan(scope, tcp, RecordingHttpTransport({}), candidates=(forged,))
    assert tcp.destinations == []


def test_revalidated_display_only_candidate_accepts_task3_llamacpp_product() -> None:
    scope = scope_fixture()
    display_candidate = LanCandidate._from_normalized(
        interface_id=scope.interface.interface_id,
        address="192.168.60.2",
        port=11434,
        service_type="_ollama._tcp.local.",
        instance_name="Display only",
        metadata={"product": "llama.cpp", "version": "1.2.3"},
    )
    tcp = RecordingTcpProbe(reachable=False)

    scan(scope, tcp, RecordingHttpTransport({}), candidates=(display_candidate,))

    assert ("192.168.60.2", 11434) in tcp.destinations


def test_pre_cancelled_scan_admits_no_endpoint_work() -> None:
    scope = scope_fixture()
    cancellation = ScanCancellation()
    cancellation.cancel()
    tcp = RecordingTcpProbe()

    result = scan(
        scope,
        tcp,
        RecordingHttpTransport({}),
        cancellation=cancellation,
    )
    assert len(result) == 8
    assert all(item.reachability is Reachability.NOT_ATTEMPTED for item in result)
    assert all(item.failure_category is LanFailureCategory.CANCELLED for item in result)
    assert tcp.destinations == []


def test_expired_total_deadline_admits_no_endpoint_work() -> None:
    scope = scope_fixture()
    values = iter((10.0, 55.0, 55.0))
    tcp = RecordingTcpProbe()

    result = scan(
        scope,
        tcp,
        RecordingHttpTransport({}),
        clock=lambda: next(values, 55.0),
    )
    assert len(result) == 8
    assert all(item.reachability is Reachability.NOT_ATTEMPTED for item in result)
    assert all(
        item.failure_category is LanFailureCategory.SCAN_DEADLINE_EXCEEDED for item in result
    )
    assert tcp.destinations == []


def test_scanner_never_exceeds_sixteen_concurrent_endpoint_tasks() -> None:
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.70.1/24"),
        "192.168.70.0/24",
    )
    lock = threading.Lock()
    release = threading.Event()
    counts = {"active": 0, "maximum": 0}

    class BlockingTcp(RecordingTcpProbe):
        def tcp_reachable(self, scope, endpoint, source, *, deadline, cancellation):
            del scope, source, deadline, cancellation
            with lock:
                self.destinations.append((endpoint.address, endpoint.port))
                counts["active"] += 1
                counts["maximum"] = max(counts["maximum"], counts["active"])
                if counts["active"] == 16:
                    release.set()
            assert release.wait(1.0)
            with lock:
                counts["active"] -= 1
            return False

    tcp = BlockingTcp(reachable=False)
    scan(scope, tcp, RecordingHttpTransport({}))

    assert counts["maximum"] == 16
    assert len(tcp.destinations) == 254 * 4


def test_sliding_window_keeps_submitted_plus_running_work_at_or_below_sixteen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.71.1/24"),
        "192.168.71.0/24",
    )
    release = threading.Event()
    lock = threading.Lock()
    counts = {"outstanding": 0, "maximum": 0}
    real_executor = concurrent.futures.ThreadPoolExecutor

    class TrackingExecutor:
        def __init__(self, *args, **kwargs) -> None:
            self._executor = real_executor(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._executor.__exit__(exc_type, exc, traceback)

        def submit(self, function, /, *args, **kwargs):
            with lock:
                counts["outstanding"] += 1
                counts["maximum"] = max(counts["maximum"], counts["outstanding"])
            future = self._executor.submit(function, *args, **kwargs)

            def settled(_future) -> None:
                with lock:
                    counts["outstanding"] -= 1

            future.add_done_callback(settled)
            return future

    class HoldingTcp(RecordingTcpProbe):
        def tcp_reachable(self, scope, endpoint, source, *, deadline, cancellation):
            del scope, endpoint, source, deadline, cancellation
            assert release.wait(1.0)
            return False

    monkeypatch.setattr(lan_scanner_module, "ThreadPoolExecutor", TrackingExecutor)
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        scan(scope, HoldingTcp(reachable=False), RecordingHttpTransport({}))
    finally:
        release.set()
        timer.join(timeout=1.0)

    assert counts["maximum"] <= 16
    assert counts["outstanding"] == 0


def test_cancellation_closes_admission_and_marks_remaining_endpoints_not_attempted() -> None:
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.72.1/24"),
        "192.168.72.0/24",
    )
    cancellation = ScanCancellation()

    class CancellingTcp(RecordingTcpProbe):
        def tcp_reachable(self, scope, endpoint, source, *, deadline, cancellation):
            result = super().tcp_reachable(
                scope,
                endpoint,
                source,
                deadline=deadline,
                cancellation=cancellation,
            )
            cancellation.cancel()
            return result

    tcp = CancellingTcp(reachable=False)
    result = scan(
        scope,
        tcp,
        RecordingHttpTransport({}),
        cancellation=cancellation,
    )

    assert len(result) == 254 * 4
    assert 1 <= len(tcp.destinations) <= 16
    assert (
        sum(item.reachability is Reachability.NOT_ATTEMPTED for item in result) >= len(result) - 16
    )
    assert all(
        item.failure_category is LanFailureCategory.CANCELLED
        for item in result
        if item.reachability is Reachability.NOT_ATTEMPTED
    )


def test_cancellation_is_serialized_with_executor_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = scope_fixture()
    cancellation = ScanCancellation()
    submit_entered = threading.Event()
    release_submit = threading.Event()
    cancel_returned = threading.Event()
    post_close_submissions: list[bool] = []
    real_executor = concurrent.futures.ThreadPoolExecutor

    class PausingExecutor:
        def __init__(self, *args, **kwargs) -> None:
            self._executor = real_executor(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._executor.__exit__(exc_type, exc, traceback)

        def submit(self, function, /, *args, **kwargs):
            if not submit_entered.is_set():
                submit_entered.set()
                assert release_submit.wait(1.0)
            post_close_submissions.append(cancellation.is_cancelled())
            return self._executor.submit(function, *args, **kwargs)

    monkeypatch.setattr(lan_scanner_module, "ThreadPoolExecutor", PausingExecutor)
    result: list[object] = []
    scan_thread = threading.Thread(
        target=lambda: result.extend(
            scan(
                scope,
                RecordingTcpProbe(reachable=False),
                RecordingHttpTransport({}),
                cancellation=cancellation,
            )
        )
    )
    scan_thread.start()
    assert submit_entered.wait(1.0)
    cancel_thread = threading.Thread(target=lambda: (cancellation.cancel(), cancel_returned.set()))
    cancel_thread.start()
    assert not cancel_returned.wait(0.05)
    release_submit.set()
    cancel_thread.join(timeout=1.0)
    scan_thread.join(timeout=2.0)

    assert cancel_returned.is_set()
    assert not scan_thread.is_alive()
    assert 1 <= len(post_close_submissions) <= 16
    assert not any(post_close_submissions)
    assert len(result) == 8


def test_tcp_failure_records_closed_typed_evidence_without_http() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    result = scan(scope, RecordingTcpProbe(reachable=False), RecordingHttpTransport({}))

    assert len(result) == 4
    assert all(item.reachability is Reachability.UNREACHABLE for item in result)
    assert all(item.api_shape is None for item in result)
    assert all(item.transport_security is None for item in result)
    assert all(item.failure_category is LanFailureCategory.TCP_UNREACHABLE for item in result)


def test_only_an_ordinary_ollama_404_falls_through_to_openai_catalog() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(404, b"missing"),
            LanRequestRoute.OPENAI_CATALOG: LanHttpResponse(200, b'{"data":[{"id":"model-a"}]}'),
            LanRequestRoute.OPENAI_GENERATION: LanHttpResponse(
                200,
                b'{"choices":[{"message":{"content":"OK"}}]}',
            ),
        }
    )

    result = scan(scope, RecordingTcpProbe(), http)

    assert result[0].api_shape is ApiShape.OPENAI_COMPATIBLE
    assert result[0].catalog == ("model-a",)
    generation = result[0].capabilities[0]
    assert generation.status is CapabilityObservationStatus.OBSERVED_PASS
    assert [request[2] for request in http.requests[:3]] == [
        LanRequestRoute.OLLAMA_CATALOG,
        LanRequestRoute.OPENAI_CATALOG,
        LanRequestRoute.OPENAI_GENERATION,
    ]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_terminal_and_never_triggers_fallthrough(status: int) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(status, b"private redirect")}
    )

    result = scan(scope, RecordingTcpProbe(), http)

    assert result[0].failure_category is LanFailureCategory.REDIRECT_REJECTED
    assert len(http.requests) == 4
    assert all(request[2] is LanRequestRoute.OLLAMA_CATALOG for request in http.requests)


@pytest.mark.parametrize("body", [b"\xff", b"not-json", b"[]", b'{"models":"wrong"}'])
def test_malformed_ollama_catalog_is_terminal_and_does_not_fall_through(body: bytes) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport({LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, body)})

    result = scan(scope, RecordingTcpProbe(), http)

    assert result[0].failure_category is LanFailureCategory.CATALOG_INVALID
    assert all(request[2] is LanRequestRoute.OLLAMA_CATALOG for request in http.requests)


@pytest.mark.parametrize(
    ("api", "body"),
    (
        ("ollama", b'{"models":[7]}'),
        ("openai", b'{"data":[7]}'),
    ),
)
def test_nonempty_untyped_catalog_list_does_not_establish_an_api_shape(
    api: str,
    body: bytes,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    responses: dict[LanRequestRoute, LanHttpResponse | Exception]
    if api == "ollama":
        responses = {LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, body)}
    else:
        responses = {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(404, b"missing"),
            LanRequestRoute.OPENAI_CATALOG: LanHttpResponse(200, body),
        }

    observation = scan(
        scope,
        RecordingTcpProbe(),
        RecordingHttpTransport(responses),
    )[0]

    assert observation.transport_security is TransportSecurity.PLAIN_HTTP
    assert observation.api_shape is None
    assert observation.catalog == ()
    assert observation.catalog_complete is False
    assert observation.failure_category is LanFailureCategory.CATALOG_INVALID


@pytest.mark.parametrize(
    ("body", "expected_shape", "expected_catalog", "complete", "failure"),
    (
        (
            b'{"models":[]}',
            ApiShape.OLLAMA_COMPATIBLE,
            (),
            True,
            LanFailureCategory.CATALOG_EMPTY,
        ),
        (
            b'{"models":[{"name":"safe-model"},7]}',
            ApiShape.OLLAMA_COMPATIBLE,
            ("safe-model",),
            False,
            None,
        ),
        (
            b'{"models":[{"name":"https://evil.invalid/model"}]}',
            ApiShape.OLLAMA_COMPATIBLE,
            (),
            False,
            LanFailureCategory.CATALOG_INVALID,
        ),
    ),
)
def test_api_shape_uses_typed_schema_evidence_independently_from_retained_models(
    body: bytes,
    expected_shape: ApiShape,
    expected_catalog: tuple[str, ...],
    complete: bool,
    failure: LanFailureCategory | None,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    responses: dict[LanRequestRoute, LanHttpResponse | Exception] = {
        LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, body),
    }
    if expected_catalog:
        responses[LanRequestRoute.OLLAMA_GENERATION] = LanHttpResponse(
            200, b'{"done":true,"response":"OK"}'
        )

    observation = scan(
        scope,
        RecordingTcpProbe(),
        RecordingHttpTransport(responses),
    )[0]

    assert observation.api_shape is expected_shape
    assert observation.catalog == expected_catalog
    assert observation.catalog_complete is complete
    assert observation.failure_category is failure


def test_deeply_nested_bounded_json_is_a_catalog_failure_not_a_scanner_bug() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    body = b'{"models":' + (b"[" * 10_000) + b"0" + (b"]" * 10_000) + b"}"
    http = RecordingHttpTransport({LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, body)})

    result = scan(scope, RecordingTcpProbe(), http)

    assert result[0].reachability is Reachability.REACHABLE
    assert result[0].failure_category is LanFailureCategory.CATALOG_INVALID


def test_untyped_http_adapter_response_is_a_reachable_protocol_failure() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")

    class UntypedHttpTransport:
        def request(self, *args, **kwargs):
            del args, kwargs
            return object()

    observations = scan_lan_scope(
        scope,
        LanScanLimits(),
        clock=lambda: 100.0,
        tcp_probe=RecordingTcpProbe(),
        http_transport=UntypedHttpTransport(),
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert all(item.reachability is Reachability.REACHABLE for item in observations)
    assert all(
        item.failure_category is LanFailureCategory.HTTP_PROTOCOL_REJECTED for item in observations
    )


def test_injected_executor_uses_one_absolute_deadline_and_emits_typed_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    progress: list[LanScanProgress] = []
    external = concurrent.futures.ThreadPoolExecutor(
        max_workers=16,
        thread_name_prefix="task6-injected-probe",
    )

    class CapturingTcpProbe(RecordingTcpProbe):
        def __init__(self) -> None:
            super().__init__(reachable=False)
            self.deadlines: list[float] = []

        def tcp_reachable(self, *args, deadline: float, **kwargs):
            self.deadlines.append(deadline)
            return super().tcp_reachable(*args, deadline=deadline, **kwargs)

    tcp = CapturingTcpProbe()
    monkeypatch.setattr(
        lan_scanner_module,
        "ThreadPoolExecutor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("production manager path must not create a nested executor")
        ),
    )
    try:
        observations = scan_lan_scope(
            scope,
            LanScanLimits(),
            clock=lambda: 100.0,
            tcp_probe=tcp,
            http_transport=RecordingHttpTransport({}),
            interface_inventory_resolver=lambda: current_inventory(scope),
            executor=external,
            absolute_deadline=120.0,
            progress=progress.append,
        )
    finally:
        external.shutdown(wait=True)

    assert len(observations) == 4
    assert tcp.deadlines == [100.75] * 4
    assert progress[0] == LanScanProgress(
        phase="planned",
        planned_count=4,
        admitted_count=0,
        completed_count=0,
        observation=None,
    )
    assert [item.phase for item in progress].count("planned") == 1
    assert [item.phase for item in progress].count("admitted") == 4
    assert [item.phase for item in progress].count("completed") == 4
    assert [item.admitted_count for item in progress if item.phase == "admitted"] == [1, 2, 3, 4]
    assert [item.completed_count for item in progress if item.phase == "completed"] == [
        1,
        2,
        3,
        4,
    ]
    assert all(item.planned_count == 4 for item in progress)


def test_progress_callback_failure_closes_admission_cancels_and_drains_injected_work() -> None:
    scope = scope_fixture()
    cancellation = ScanCancellation()
    probe_entered = threading.Event()
    probe_release = threading.Event()
    callback_called = threading.Event()
    controller_returned = threading.Event()
    external = concurrent.futures.ThreadPoolExecutor(
        max_workers=16,
        thread_name_prefix="task6-progress-failure",
    )
    callbacks = 0
    probe_calls = 0

    class BlockedTcp(RecordingTcpProbe):
        def tcp_reachable(self, *args, **kwargs):
            nonlocal probe_calls
            probe_calls += 1
            probe_entered.set()
            assert probe_release.wait(timeout=3)
            return super().tcp_reachable(*args, **kwargs)

    tcp = BlockedTcp(reachable=False)

    def fail_progress(progress: LanScanProgress) -> None:
        nonlocal callbacks
        callbacks += 1
        if progress.phase == "planned":
            return
        callback_called.set()
        raise RuntimeError("secret progress callback body")

    failures: list[BaseException] = []

    def execute() -> None:
        try:
            scan_lan_scope(
                scope,
                LanScanLimits(),
                cancellation=cancellation,
                clock=lambda: 100.0,
                tcp_probe=tcp,
                http_transport=RecordingHttpTransport({}),
                interface_inventory_resolver=lambda: current_inventory(scope),
                executor=external,
                absolute_deadline=120.0,
                progress=fail_progress,
            )
        except BaseException as exc:  # noqa: BLE001 - assert fixed public failure below
            failures.append(exc)
        finally:
            controller_returned.set()

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert callback_called.wait(timeout=1.0)
        assert probe_entered.wait(timeout=1.0)
        assert worker.is_alive()
        assert controller_returned.is_set() is False
        assert cancellation.is_cancelled() is True
        assert probe_calls == 1
        assert callbacks == 2
    finally:
        probe_release.set()
        worker.join(timeout=2)
    try:
        sentinel = external.submit(lambda: "drained")
        assert sentinel.result(timeout=1.0) == "drained"
    finally:
        external.shutdown(wait=True)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert type(failures[0]) is RuntimeError
    assert str(failures[0]) == "lan_scan_progress_failed"
    assert callbacks == 2
    assert probe_calls == 1
    assert cancellation.is_cancelled() is True
    assert len(tcp.destinations) == 1


def test_catalog_is_sanitized_deduplicated_top_eight_and_digest_is_order_stable() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    model_ids = [f"model-{index:02d}" for index in range(10)]
    hostile = [
        "https://evil.invalid/model",
        "user:pass@server.local",
        "server.local",
        "server.local,",
        "Use model.dev locally.",
        "192.168.60.9",
        "fd00::9",
        "Try 192.168.60.9",
        "localhost:11434",
        "modelbox:11434",
        "[fd00::9]:11434",
        "Try fd00::9 locally",
        "token=sk-abcdefghijk",
        unicodedata.normalize("NFD", "modèle"),
        "bad\nmodel",
        7,
    ]

    def run_for(items: list[object]):
        body = repr({"models": [{"name": item} for item in items]}).replace("'", '"')
        # JSON booleans/null are absent from this fixture, so the hand-built encoding is exact.
        http = RecordingHttpTransport(
            {
                LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, body.encode()),
                LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                    200, b'{"done":true,"response":"OK"}'
                ),
            }
        )
        return scan(scope, RecordingTcpProbe(), http)[0]

    first = run_for(model_ids + hostile + ["model-00"])
    second = run_for(list(reversed(model_ids)) + ["model-00"])

    assert first.catalog == tuple(model_ids[:8])
    assert second.catalog == tuple(model_ids[:8])
    assert first.catalog_truncated is True
    assert first.catalog_complete is False
    assert first.catalog_digest == second.catalog_digest


def test_capability_probe_runs_once_only_after_nonempty_catalog_for_deterministic_model() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200,
                b'{"models":[{"name":"zeta"},{"name":"alpha"}]}',
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                200, b'{"done":true,"response":"OK"}'
            ),
        }
    )

    result = scan(scope, RecordingTcpProbe(), http)

    assert tuple(item.capability for item in result[0].capabilities) == tuple(CapabilityName)
    generation, *untested = result[0].capabilities
    assert generation.capability is CapabilityName.GENERATION
    assert generation.status is CapabilityObservationStatus.OBSERVED_PASS
    assert generation.provenance is CapabilityProvenance.OBSERVED
    assert generation.supported is True
    assert all(item.status is CapabilityObservationStatus.NOT_RUN for item in untested)
    assert all(item.provenance is CapabilityProvenance.NOT_RUN for item in untested)
    assert all(item.supported is None for item in untested)
    generation_requests = [
        request for request in http.requests if request[2] is LanRequestRoute.OLLAMA_GENERATION
    ]
    assert len(generation_requests) == 4
    assert all(request[3] == "alpha" for request in generation_requests)


def test_empty_catalog_never_runs_capability_or_claims_capability_support() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(200, b'{"models":[]}')}
    )

    result = scan(scope, RecordingTcpProbe(), http)

    assert all(
        item.status is CapabilityObservationStatus.NOT_RUN for item in result[0].capabilities
    )
    assert all(item.supported is None for item in result[0].capabilities)
    assert all(request[2] is LanRequestRoute.OLLAMA_CATALOG for request in http.requests)


@pytest.mark.parametrize(
    ("transport_failure", "expected"),
    [
        (LanTransportFailure.HTTP_TIMEOUT, LanFailureCategory.HTTP_TIMEOUT),
        (LanTransportFailure.RESPONSE_TOO_LARGE, LanFailureCategory.RESPONSE_TOO_LARGE),
        (
            LanTransportFailure.HTTP_PROTOCOL_REJECTED,
            LanFailureCategory.HTTP_PROTOCOL_REJECTED,
        ),
        (LanTransportFailure.REDIRECT_REJECTED, LanFailureCategory.REDIRECT_REJECTED),
        (
            LanTransportFailure.UNSUPPORTED_CONTENT_ENCODING,
            LanFailureCategory.UNSUPPORTED_CONTENT_ENCODING,
        ),
        (
            LanTransportFailure.INTERFACE_CHANGED,
            LanFailureCategory.INTERFACE_DRIFT,
        ),
        (
            LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE,
            LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
        ),
        (
            LanTransportFailure.HTTP_CONNECT_FAILED,
            LanFailureCategory.HTTP_PROTOCOL_REJECTED,
        ),
        (
            LanTransportFailure.DEADLINE_EXCEEDED,
            LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
        ),
        (LanTransportFailure.CANCELLED, LanFailureCategory.CANCELLED),
    ],
)
def test_transport_failures_map_to_closed_secret_free_categories(
    transport_failure: LanTransportFailure,
    expected: LanFailureCategory,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    progress = (
        "not_started"
        if transport_failure
        in {
            LanTransportFailure.INTERFACE_CHANGED,
            LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE,
        }
        else (
            "connection_attempted"
            if transport_failure is LanTransportFailure.HTTP_CONNECT_FAILED
            else "request_sent"
        )
    )
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanTransportError(
                transport_failure,
                "secret sk-abcdefghijk at evil.invalid",
                request_progress=request_progress(progress),
            )
        }
    )

    result = scan(scope, RecordingTcpProbe(), http)

    assert result[0].failure_category is expected
    assert result[0].reachability is Reachability.REACHABLE
    assert "sk-" not in (result[0].public_error or "")
    assert "evil.invalid" not in (result[0].public_error or "")
    assert len(result[0].public_error or "") <= 1024


@pytest.mark.parametrize(
    ("transport_failure", "expected", "reachability"),
    [
        (LanTransportFailure.CANCELLED, LanFailureCategory.CANCELLED, Reachability.NOT_ATTEMPTED),
        (
            LanTransportFailure.DEADLINE_EXCEEDED,
            LanFailureCategory.TCP_TIMEOUT,
            Reachability.UNREACHABLE,
        ),
        (
            LanTransportFailure.INTERFACE_CHANGED,
            LanFailureCategory.INTERFACE_DRIFT,
            Reachability.NOT_ATTEMPTED,
        ),
        (
            LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE,
            LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
            Reachability.NOT_ATTEMPTED,
        ),
        (LanTransportFailure.TCP_TIMEOUT, LanFailureCategory.TCP_TIMEOUT, Reachability.UNREACHABLE),
        (LanTransportFailure.TCP_REFUSED, LanFailureCategory.TCP_REFUSED, Reachability.UNREACHABLE),
        (
            LanTransportFailure.TCP_UNREACHABLE,
            LanFailureCategory.TCP_UNREACHABLE,
            Reachability.UNREACHABLE,
        ),
        (LanTransportFailure.TCP_ERROR, LanFailureCategory.TCP_ERROR, Reachability.UNREACHABLE),
    ],
)
def test_tcp_transport_failures_map_to_exact_closed_observation_categories(
    transport_failure: LanTransportFailure,
    expected: LanFailureCategory,
    reachability: Reachability,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")

    class RaisingTcp(RecordingTcpProbe):
        def tcp_reachable(self, scope, endpoint, source, *, deadline, cancellation):
            del scope, endpoint, source, deadline, cancellation
            raise LanTransportError(transport_failure, "hostile detail")

    observation = scan(scope, RaisingTcp(), RecordingHttpTransport({}))[0]

    assert observation.failure_category is expected
    assert observation.reachability is reachability
    assert observation.transport_security is None


def test_tcp_deadline_failure_uses_actual_scan_cap_not_the_local_tcp_cap() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.60.1", 11434)

    class MutableClock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    class DeadlineTcp(RecordingTcpProbe):
        def __init__(self, clock: MutableClock, completed_at: float) -> None:
            super().__init__()
            self._clock = clock
            self._completed_at = completed_at
            self.deadlines: list[float] = []

        def tcp_reachable(self, scope, endpoint, source, *, deadline, cancellation):
            del scope, endpoint, source, cancellation
            self.deadlines.append(deadline)
            self._clock.now = self._completed_at
            raise LanTransportError(LanTransportFailure.DEADLINE_EXCEEDED)

    local_clock = MutableClock()
    local_tcp = DeadlineTcp(local_clock, 100.751)
    local = probe_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=ScanCancellation(),
        clock=local_clock,
        tcp_probe=local_tcp,
        http_transport=RecordingHttpTransport({}),
        interface_inventory_resolver=lambda: current_inventory(scope),
    )
    assert local_tcp.deadlines == [100.75]
    assert local.failure_category is LanFailureCategory.TCP_TIMEOUT
    assert local.reachability is Reachability.UNREACHABLE

    scan_clock = MutableClock()
    scan_tcp = DeadlineTcp(scan_clock, 100.501)
    capped = probe_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=100.5,
        cancellation=ScanCancellation(),
        clock=scan_clock,
        tcp_probe=scan_tcp,
        http_transport=RecordingHttpTransport({}),
        interface_inventory_resolver=lambda: current_inventory(scope),
    )
    assert scan_tcp.deadlines == [100.5]
    assert capped.failure_category is LanFailureCategory.SCAN_DEADLINE_EXCEEDED
    assert capped.reachability is Reachability.NOT_ATTEMPTED


def test_output_is_frozen_and_contains_no_raw_network_metadata() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}],"secret":"raw-body"}'
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                200, b'{"response":"OK","hostname":"evil.invalid"}'
            ),
        }
    )

    observation = scan(scope, RecordingTcpProbe(), http)[0]

    with pytest.raises(FrozenInstanceError):
        observation.catalog = ()  # type: ignore[misc]
    rendered = repr(observation)
    assert "raw-body" not in rendered
    assert "evil.invalid" not in rendered
    assert observation.reachability is Reachability.REACHABLE


def test_catalog_text_changed_by_central_secret_redaction_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KESTREL_TEST_API_KEY", "registered-safe-model")
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"registered-safe-model"}]}'
            )
        }
    )

    observation = scan(scope, RecordingTcpProbe(), http)[0]

    assert observation.catalog == ()
    assert observation.failure_category is LanFailureCategory.CATALOG_INVALID


def test_failed_generation_is_observed_failure_but_not_proof_of_unsupported() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}]}'
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(500, b"failed"),
        }
    )

    observation = scan(scope, RecordingTcpProbe(), http)[0]
    generation = observation.capabilities[0]

    assert generation.status is CapabilityObservationStatus.OBSERVED_FAILURE
    assert generation.provenance is CapabilityProvenance.OBSERVED
    assert generation.supported is None
    assert all(
        item.status is CapabilityObservationStatus.NOT_RUN for item in observation.capabilities[1:]
    )


@pytest.mark.parametrize(
    ("transport_failure", "expected"),
    [
        (LanTransportFailure.HTTP_TIMEOUT, LanFailureCategory.HTTP_TIMEOUT),
        (
            LanTransportFailure.HTTP_CONNECT_FAILED,
            LanFailureCategory.GENERATION_REQUEST_FAILED,
        ),
        (LanTransportFailure.REDIRECT_REJECTED, LanFailureCategory.REDIRECT_REJECTED),
        (
            LanTransportFailure.RESPONSE_TOO_LARGE,
            LanFailureCategory.RESPONSE_TOO_LARGE,
        ),
    ],
)
def test_generation_transport_failure_preserves_catalog_and_is_observed_failure(
    transport_failure: LanTransportFailure,
    expected: LanFailureCategory,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}]}'
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanTransportError(
                transport_failure,
                request_progress=request_progress("request_sent"),
            ),
        }
    )

    observation = scan(scope, RecordingTcpProbe(), http)[0]

    assert observation.reachability is Reachability.REACHABLE
    assert observation.api_shape is ApiShape.OLLAMA_COMPATIBLE
    assert observation.catalog == ("safe-model",)
    assert observation.failure_category is expected
    assert observation.capabilities[0].status is CapabilityObservationStatus.OBSERVED_FAILURE
    assert observation.capabilities[0].supported is None


@pytest.mark.parametrize(
    ("transport_failure", "expected_failure"),
    (
        (LanTransportFailure.CANCELLED, LanFailureCategory.CANCELLED),
        (
            LanTransportFailure.DEADLINE_EXCEEDED,
            LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
        ),
        (LanTransportFailure.HTTP_TIMEOUT, LanFailureCategory.HTTP_TIMEOUT),
    ),
)
@pytest.mark.parametrize(
    ("progress", "expected_transport"),
    (
        ("not_started", None),
        ("connection_attempted", TransportSecurity.PLAIN_HTTP),
        ("request_sent", TransportSecurity.PLAIN_HTTP),
    ),
)
def test_catalog_transport_evidence_uses_closed_request_progress(
    transport_failure: LanTransportFailure,
    expected_failure: LanFailureCategory,
    progress: str,
    expected_transport: TransportSecurity | None,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanTransportError(
                transport_failure,
                request_progress=request_progress(progress),
            )
        }
    )

    observation = scan(scope, RecordingTcpProbe(), http)[0]

    assert observation.failure_category is expected_failure
    assert observation.transport_security is expected_transport
    assert observation.capabilities[0].status is CapabilityObservationStatus.NOT_RUN


@pytest.mark.parametrize(
    ("transport_failure", "expected_failure"),
    (
        (LanTransportFailure.CANCELLED, LanFailureCategory.CANCELLED),
        (
            LanTransportFailure.DEADLINE_EXCEEDED,
            LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
        ),
        (LanTransportFailure.HTTP_TIMEOUT, LanFailureCategory.HTTP_TIMEOUT),
    ),
)
@pytest.mark.parametrize(
    ("progress", "expected_status"),
    (
        ("not_started", CapabilityObservationStatus.NOT_RUN),
        ("connection_attempted", CapabilityObservationStatus.NOT_RUN),
        ("request_sent", CapabilityObservationStatus.OBSERVED_FAILURE),
    ),
)
def test_generation_observation_uses_send_completion_not_failure_enum_guessing(
    transport_failure: LanTransportFailure,
    expected_failure: LanFailureCategory,
    progress: str,
    expected_status: CapabilityObservationStatus,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}]}'
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanTransportError(
                transport_failure,
                request_progress=request_progress(progress),
            ),
        }
    )

    observation = scan(scope, RecordingTcpProbe(), http)[0]

    assert observation.failure_category is expected_failure
    assert observation.transport_security is TransportSecurity.PLAIN_HTTP
    assert observation.api_shape is ApiShape.OLLAMA_COMPATIBLE
    assert observation.catalog == ("safe-model",)
    assert observation.capabilities[0].status is expected_status
    if expected_status is CapabilityObservationStatus.OBSERVED_FAILURE:
        assert observation.capability_route == LanRequestRoute.OLLAMA_GENERATION.path
        assert observation.selected_model_id == "safe-model"
    else:
        assert observation.capability_route is None
        assert observation.selected_model_id is None


@pytest.mark.parametrize(
    ("api", "body"),
    [
        ("ollama", b'{"done":true,"response":"YES"}'),
        ("ollama", b'{"done":false,"response":"OK"}'),
        ("ollama", b'{"response":"OK"}'),
        ("openai", b'{"choices":[{"message":{"content":"YES"}}]}'),
    ],
)
def test_generation_requires_the_exact_compatible_ok_response(api: str, body: bytes) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    responses: dict[LanRequestRoute, LanHttpResponse | Exception]
    if api == "ollama":
        responses = {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}]}'
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(200, body),
        }
    else:
        responses = {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(404, b"missing"),
            LanRequestRoute.OPENAI_CATALOG: LanHttpResponse(200, b'{"data":[{"id":"safe-model"}]}'),
            LanRequestRoute.OPENAI_GENERATION: LanHttpResponse(200, body),
        }

    observation = scan(scope, RecordingTcpProbe(), RecordingHttpTransport(responses))[0]

    assert observation.capabilities[0].status is CapabilityObservationStatus.OBSERVED_FAILURE
    assert observation.capabilities[0].supported is None
    assert observation.failure_category is LanFailureCategory.GENERATION_RESPONSE_INVALID


def _expected_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def test_evidence_digests_use_exact_domains_and_material_fields() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}]}'
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                200, b'{"done":true,"response":"OK"}'
            ),
        }
    )
    observation = scan(scope, RecordingTcpProbe(), http)[0]
    endpoint = observation.endpoint
    expected_endpoint = _expected_digest(
        {
            "address": endpoint.address,
            "interface_id": endpoint.interface_id,
            "port": endpoint.port,
            "schema": "kestrel.lan.endpoint-binding.v1",
        }
    )
    expected_catalog = _expected_digest(
        {
            "address": endpoint.address,
            "api_shape": "ollama_compatible",
            "complete": True,
            "endpoint_binding_digest": expected_endpoint,
            "interface_id": endpoint.interface_id,
            "model_ids": ["safe-model"],
            "port": endpoint.port,
            "schema": "kestrel.lan.catalog.v1",
            "truncated": False,
        }
    )
    capability_payload = [
        {
            "capability": item.capability.value,
            "provenance": item.provenance.value,
            "status": item.status.value,
            "supported": item.supported,
        }
        for item in observation.capabilities
    ]
    expected_capability = _expected_digest(
        {
            "api_shape": "ollama_compatible",
            "capabilities": capability_payload,
            "catalog_digest": expected_catalog,
            "endpoint_binding_digest": expected_endpoint,
            "model_id": "safe-model",
            "route": "/api/generate",
            "schema": "kestrel.lan.capability.v1",
        }
    )
    expected_observation = _expected_digest(
        {
            "api_shape": "ollama_compatible",
            "capability_digest": expected_capability,
            "catalog_digest": expected_catalog,
            "endpoint_binding_digest": expected_endpoint,
            "failure_category": None,
            "reachability": "reachable",
            "schema": "kestrel.lan.observation.v1",
            "transport_security": "plain_http",
        }
    )

    assert observation.endpoint_binding_digest == expected_endpoint
    assert observation.catalog_digest == expected_catalog
    assert observation.capability_digest == expected_capability
    assert observation.observation_digest == expected_observation
    assert (
        len(
            {
                expected_endpoint,
                expected_catalog,
                expected_capability,
                expected_observation,
            }
        )
        == 4
    )


def test_endpoint_and_catalog_material_drift_changes_every_dependent_digest() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}]}'
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                200, b'{"done":true,"response":"OK"}'
            ),
        }
    )
    observations = scan(scope, RecordingTcpProbe(), http)

    assert len({item.endpoint_binding_digest for item in observations}) == 4
    assert len({item.catalog_digest for item in observations}) == 4
    assert len({item.capability_digest for item in observations}) == 4
    assert len({item.observation_digest for item in observations}) == 4


def test_catalog_model_drift_preserves_endpoint_digest_and_changes_dependents() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")

    def observation_for(model: str):
        http = RecordingHttpTransport(
            {
                LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                    200, json.dumps({"models": [{"name": model}]}).encode()
                ),
                LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                    200, b'{"done":true,"response":"OK"}'
                ),
            }
        )
        return scan(scope, RecordingTcpProbe(), http)[0]

    first = observation_for("model-a")
    second = observation_for("model-b")

    assert first.endpoint_binding_digest == second.endpoint_binding_digest
    assert first.catalog_digest != second.catalog_digest
    assert first.capability_digest != second.capability_digest
    assert first.observation_digest != second.observation_digest


def test_observation_consistency_rejects_impossible_reachability_and_capability_shapes() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}]}'
            ),
            LanRequestRoute.OLLAMA_GENERATION: LanHttpResponse(
                200, b'{"done":true,"response":"OK"}'
            ),
        }
    )
    observation = scan(scope, RecordingTcpProbe(), http)[0]

    with pytest.raises(ValueError, match="unreached|failure|consistency"):
        replace(
            observation,
            reachability=Reachability.UNREACHABLE,
            transport_security=None,
            failure_category=None,
        )
    with pytest.raises(ValueError, match="overclaims"):
        type(observation.capabilities[1])(
            CapabilityName.STREAMING,
            True,
            CapabilityProvenance.OBSERVED,
            CapabilityObservationStatus.OBSERVED_PASS,
        )

    impossible = (
        {"catalog_complete": True, "catalog_truncated": True},
        {"transport_security": None},
        {
            "catalog": (),
            "catalog_complete": False,
            "capability_route": None,
            "selected_model_id": None,
        },
        {
            "capabilities": tuple(
                type(item).not_run(item.capability) for item in observation.capabilities
            ),
            "capability_route": None,
            "selected_model_id": None,
            "failure_category": None,
        },
    )
    for changes in impossible:
        with pytest.raises(ValueError):
            replace(observation, **changes)

    with pytest.raises(ValueError):
        replace(observation, capabilities=(object(), *observation.capabilities[1:]))


def test_pre_api_observation_matrix_requires_exact_transport_evidence() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.60.1", 11434)
    response_proven_failures = (
        LanFailureCategory.CATALOG_NOT_FOUND,
        LanFailureCategory.HTTP_STATUS_REJECTED,
        LanFailureCategory.HTTP_PROTOCOL_REJECTED,
        LanFailureCategory.REDIRECT_REJECTED,
        LanFailureCategory.RESPONSE_TOO_LARGE,
        LanFailureCategory.UNSUPPORTED_CONTENT_ENCODING,
        LanFailureCategory.CATALOG_INVALID,
    )
    for failure in response_proven_failures:
        with pytest.raises(ValueError, match="transport|HTTP|plain"):
            lan_scanner_module._make_observation(
                endpoint,
                reachability=Reachability.REACHABLE,
                failure_category=failure,
            )
        observation = lan_scanner_module._make_observation(
            endpoint,
            reachability=Reachability.REACHABLE,
            transport_security=TransportSecurity.PLAIN_HTTP,
            failure_category=failure,
        )
        assert observation.transport_security is TransportSecurity.PLAIN_HTTP

    for failure in (
        LanFailureCategory.INTERFACE_DRIFT,
        LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
    ):
        with pytest.raises(ValueError, match="transport|interface"):
            lan_scanner_module._make_observation(
                endpoint,
                reachability=Reachability.REACHABLE,
                transport_security=TransportSecurity.PLAIN_HTTP,
                failure_category=failure,
            )
        observation = lan_scanner_module._make_observation(
            endpoint,
            reachability=Reachability.REACHABLE,
            failure_category=failure,
        )
        assert observation.transport_security is None

    for failure in (
        LanFailureCategory.CANCELLED,
        LanFailureCategory.SCAN_DEADLINE_EXCEEDED,
        LanFailureCategory.HTTP_TIMEOUT,
    ):
        for transport in (None, TransportSecurity.PLAIN_HTTP):
            observation = lan_scanner_module._make_observation(
                endpoint,
                reachability=Reachability.REACHABLE,
                transport_security=transport,
                failure_category=failure,
            )
            assert observation.transport_security is transport


@pytest.mark.parametrize(
    "transport_failure",
    (
        LanTransportFailure.INTERFACE_CHANGED,
        LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE,
    ),
)
def test_pre_api_interface_failures_never_claim_plain_http_even_after_connection_progress(
    transport_failure: LanTransportFailure,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanTransportError(
                transport_failure,
                request_progress=request_progress("connection_attempted"),
            )
        }
    )

    observation = scan(scope, RecordingTcpProbe(), http)[0]

    assert observation.transport_security is None
    assert observation.failure_category in {
        LanFailureCategory.INTERFACE_DRIFT,
        LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
    }


@pytest.mark.parametrize(
    "failure",
    [
        LanFailureCategory.CATALOG_EMPTY,
        LanFailureCategory.CATALOG_NOT_FOUND,
        LanFailureCategory.HTTP_STATUS_REJECTED,
        LanFailureCategory.INTERFACE_PINNING_UNAVAILABLE,
    ],
)
def test_digest_valid_observation_rejects_phase_impossible_generation_failure(
    failure: LanFailureCategory,
) -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.60.1", 11434)

    with pytest.raises(ValueError, match="phase|generation"):
        lan_scanner_module._make_observation(
            endpoint,
            reachability=Reachability.REACHABLE,
            transport_security=TransportSecurity.PLAIN_HTTP,
            api_shape=ApiShape.OLLAMA_COMPATIBLE,
            catalog=("safe-model",),
            catalog_complete=True,
            capabilities=lan_scanner_module._capabilities_with_generation(passed=False),
            capability_route=LanRequestRoute.OLLAMA_GENERATION.path,
            selected_model_id="safe-model",
            failure_category=failure,
        )


def test_scan_cap_expiry_during_http_is_attributed_to_total_scan_deadline() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.60.1", 11434)
    values = iter((100.0, 100.0, 100.0, 100.0, 100.0, 101.0))

    class TimingOutHttp:
        deadlines: list[float] = []

        def request(self, *args, deadline, **kwargs):
            del args, kwargs
            self.deadlines.append(deadline)
            raise LanTransportError(LanTransportFailure.HTTP_TIMEOUT)

    http = TimingOutHttp()
    observation = probe_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=101.0,
        cancellation=ScanCancellation(),
        clock=lambda: next(values, 101.0),
        tcp_probe=RecordingTcpProbe(),
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert http.deadlines == [101.0]
    assert observation.reachability is Reachability.REACHABLE
    assert observation.failure_category is LanFailureCategory.SCAN_DEADLINE_EXCEEDED


def test_output_enums_are_the_exact_closed_durable_value_sets() -> None:
    assert tuple(item.value for item in Reachability) == (
        "not_attempted",
        "unreachable",
        "reachable",
    )
    assert tuple(item.value for item in ApiShape) == (
        "ollama_compatible",
        "openai_compatible",
    )
    assert tuple(item.value for item in TransportSecurity) == ("plain_http",)
    assert tuple(item.value for item in LanFailureCategory) == (
        "cancelled",
        "scan_deadline_exceeded",
        "interface_drift",
        "interface_pinning_unavailable",
        "tcp_timeout",
        "tcp_refused",
        "tcp_unreachable",
        "tcp_error",
        "http_timeout",
        "http_protocol_rejected",
        "redirect_rejected",
        "response_too_large",
        "unsupported_content_encoding",
        "http_status_rejected",
        "catalog_not_found",
        "catalog_invalid",
        "catalog_empty",
        "generation_request_failed",
        "generation_response_invalid",
    )


def test_all_http_reconnects_share_one_endpoint_phase_deadline() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.60.1", 11434)
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(404, b"missing"),
            LanRequestRoute.OPENAI_CATALOG: LanHttpResponse(200, b'{"data":[{"id":"safe-model"}]}'),
            LanRequestRoute.OPENAI_GENERATION: LanHttpResponse(
                200, b'{"choices":[{"message":{"content":"OK"}}]}'
            ),
        }
    )

    probe_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=ScanCancellation(),
        clock=lambda: 100.0,
        tcp_probe=RecordingTcpProbe(),
        http_transport=http,
        interface_inventory_resolver=lambda: current_inventory(scope),
    )

    assert http.deadlines == [102.0, 102.0, 102.0]


def test_interface_drift_between_tcp_and_catalog_sends_no_http_request() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.60.1", 11434)
    states = iter(
        (
            current_inventory(scope),
            CurrentLanInterfaceInventory(
                (
                    CurrentLanInterfaceState(
                        scope.interface.os_identity,
                        7,
                        ("192.168.60.2/32",),
                    ),
                )
            ),
        )
    )
    tcp = RecordingTcpProbe()
    http = RecordingHttpTransport({})

    result = probe_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=ScanCancellation(),
        clock=lambda: 100.0,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: next(states),
    )

    assert result.failure_category is LanFailureCategory.INTERFACE_DRIFT
    assert len(tcp.destinations) == 1
    assert http.requests == []


def test_interface_drift_between_catalog_and_capability_sends_no_generation() -> None:
    scope = scope_fixture("192.168.60.1/32", "192.168.60.1/32")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.60.1", 11434)
    states = iter(
        (
            current_inventory(scope),
            current_inventory(scope),
            CurrentLanInterfaceInventory(
                (
                    CurrentLanInterfaceState(
                        scope.interface.os_identity,
                        7,
                        ("192.168.60.2/32",),
                    ),
                )
            ),
        )
    )
    tcp = RecordingTcpProbe()
    http = RecordingHttpTransport(
        {
            LanRequestRoute.OLLAMA_CATALOG: LanHttpResponse(
                200, b'{"models":[{"name":"safe-model"}]}'
            )
        }
    )

    result = probe_lan_endpoint(
        scope,
        endpoint,
        scan_deadline=145.0,
        cancellation=ScanCancellation(),
        clock=lambda: 100.0,
        tcp_probe=tcp,
        http_transport=http,
        interface_inventory_resolver=lambda: next(states),
    )

    assert result.failure_category is LanFailureCategory.INTERFACE_DRIFT
    assert [item[2] for item in http.requests] == [LanRequestRoute.OLLAMA_CATALOG]
    assert result.capabilities[0].status is CapabilityObservationStatus.NOT_RUN
