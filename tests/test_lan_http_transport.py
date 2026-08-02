from __future__ import annotations

import copy
import json
import socket
import unicodedata
import urllib.parse
import urllib.request
from importlib import import_module

import pytest

from nested_memvid_agent.lan_discovery_models import (
    MAX_PROBE_RESPONSE_BYTES,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_http_transport import (
    CurrentLanInterfaceInventory,
    CurrentLanInterfaceState,
    DirectLanHttpTransport,
    LanProbeModel,
    LanRequestProgress,
    LanRequestRoute,
    LanTransportError,
    LanTransportFailure,
    authenticate_lan_source,
)
from nested_memvid_agent.llm import provider_urls
from nested_memvid_agent.llm.provider_urls import format_numeric_http_authority


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


class HostilePort(int):
    def __format__(self, format_spec: str) -> str:
        del format_spec
        raise AssertionError("hostile port formatting crossed validation")


class FakeSocket:
    def __init__(self, response: bytes = b"") -> None:
        self._response = bytearray(response)
        self.timeouts: list[float] = []
        self.socket_options: list[tuple[int, int, object]] = []
        self.bound: object | None = None
        self.connected: object | None = None
        self.sent = b""
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def setsockopt(self, level: int, option: int, value: object) -> None:
        self.socket_options.append((level, option, value))

    def bind(self, address: object) -> None:
        self.bound = address

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
        self.calls: list[tuple[int, int]] = []
        self.sockets: list[FakeSocket] = []

    def __call__(self, family: int, kind: int) -> FakeSocket:
        self.calls.append((family, kind))
        response = self._responses.pop(0) if self._responses else b""
        result = FakeSocket(response)
        self.sockets.append(result)
        return result


def interface_fixture(*addresses: str) -> NetworkInterface:
    return NetworkInterface.from_addresses(
        os_identity="darwin:en7",
        display_name="Private adapter",
        addresses=addresses,
    )


def scope_fixture(
    address: str = "192.168.50.7/24", network: str = "192.168.50.0/24"
) -> PrivateScanScope:
    return PrivateScanScope.from_request(interface_fixture(address), network)


def current_inventory(
    scope: PrivateScanScope,
    *,
    index: int = 7,
    extra: tuple[CurrentLanInterfaceState, ...] = (),
) -> CurrentLanInterfaceInventory:
    selected = CurrentLanInterfaceState(
        scope.interface.os_identity,
        index,
        scope.interface.addresses,
    )
    return CurrentLanInterfaceInventory((selected, *extra))


def manual_endpoint_type():
    return import_module("nested_memvid_agent.lan_discovery_models").ManualLanEndpoint


def direct_transport(
    scope: PrivateScanScope,
    sockets: SocketFactory,
    *,
    clock=lambda: 0.0,
    index: int = 7,
    platform_name: str = "Darwin",
) -> DirectLanHttpTransport:
    return DirectLanHttpTransport(
        socket_factory=sockets,
        clock=clock,
        inventory_resolver=lambda: current_inventory(scope, index=index),
        platform_name=platform_name,
    )


def http_response(status: int, body: bytes, *, headers: tuple[tuple[str, str], ...] = ()) -> bytes:
    reason = {200: "OK", 302: "Found", 404: "Not Found"}.get(status, "Status")
    lines = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}", "Connection: close"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def test_numeric_http_authority_accepts_only_an_authenticated_endpoint_value() -> None:
    ipv4_scope = scope_fixture()
    ipv6_scope = scope_fixture("fd00::7/64", "fd00::/64")
    ipv4 = ResolvedLanEndpoint.from_scope(ipv4_scope, "192.168.50.8", 11434)
    ipv6 = ResolvedLanEndpoint.from_scope(ipv6_scope, "fd00::8", 8000)

    assert format_numeric_http_authority(ipv4) == "192.168.50.8:11434"
    assert format_numeric_http_authority(ipv6) == "[fd00::8]:8000"

    for hostile in ("model-box.local", "user@192.168.50.8", "http://192.168.50.8"):
        with pytest.raises((TypeError, ValueError), match="endpoint"):
            format_numeric_http_authority(hostile)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "boundary",
    ("transport-authority", "socket-authority", "request-authority"),
)
def test_every_transport_authority_rejects_int_subclass_port_before_use(
    boundary: str,
) -> None:
    module = import_module("nested_memvid_agent.lan_http_transport")
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.50.7/24", "192.168.50.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.50.8", 5001)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    forged = object.__new__(manual_type)
    object.__setattr__(forged, "kind", "manual")
    object.__setattr__(forged, "interface_id", endpoint.interface_id)
    object.__setattr__(forged, "address", endpoint.address)
    object.__setattr__(forged, "port", HostilePort(5001))

    with pytest.raises(ValueError, match="port"):
        if boundary == "transport-authority":
            module._format_numeric_http_authority(forged)
        elif boundary == "socket-authority":
            module._socket_authority(forged, source)
        else:
            module._request_bytes(forged, LanRequestRoute.OLLAMA_CATALOG, None)


@pytest.mark.parametrize(
    "boundary",
    ("transport-authority", "socket-authority", "request-authority"),
)
def test_every_automatic_transport_authority_rejects_int_subclass_port_before_use(
    boundary: str,
) -> None:
    module = import_module("nested_memvid_agent.lan_http_transport")
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    forged = object.__new__(ResolvedLanEndpoint)
    object.__setattr__(forged, "interface_id", endpoint.interface_id)
    object.__setattr__(forged, "address", endpoint.address)
    object.__setattr__(forged, "port", HostilePort(11434))

    with pytest.raises(ValueError, match="port"):
        if boundary == "transport-authority":
            module._format_numeric_http_authority(forged)
        elif boundary == "socket-authority":
            module._socket_authority(forged, source)
        else:
            module._request_bytes(forged, LanRequestRoute.OLLAMA_CATALOG, None)


def test_source_authentication_rebuilds_endpoint_and_rejects_interface_drift() -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)

    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    assert source.source_address == "192.168.50.7"
    assert source.interface_index == 7

    drifted = CurrentLanInterfaceInventory(
        (CurrentLanInterfaceState(scope.interface.os_identity, 7, ("192.168.50.9/24",)),)
    )
    with pytest.raises(ValueError, match="changed"):
        authenticate_lan_source(scope, endpoint, lambda: drifted)

    forged = object.__new__(ResolvedLanEndpoint)
    object.__setattr__(forged, "interface_id", endpoint.interface_id)
    object.__setattr__(forged, "address", "8.8.8.8")
    object.__setattr__(forged, "port", endpoint.port)
    with pytest.raises(ValueError, match="confirmed scope|private LAN"):
        authenticate_lan_source(scope, forged, lambda: current_inventory(scope))


def test_manual_source_authentication_accepts_only_the_exact_manual_host_authority() -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.50.7/24", "192.168.50.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.50.8", 5001)

    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    assert (source.interface_id, source.source_address, source.interface_index) == (
        scope.interface.interface_id,
        "192.168.50.7",
        7,
    )


def test_source_selection_uses_unique_longest_prefix_and_preserves_os_provenance() -> None:
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.50.7/16", "192.168.50.9/24"),
        "192.168.50.0/24",
    )
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)

    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    assert source.source_address == "192.168.50.9"
    assert source.os_identity == "darwin:en7"


def test_source_selection_rejects_ties_and_duplicate_assignment_on_other_interface() -> None:
    tied_scope = PrivateScanScope.from_request(
        interface_fixture("192.168.50.7/24", "192.168.50.9/24"),
        "192.168.50.0/24",
    )
    tied_endpoint = ResolvedLanEndpoint.from_scope(tied_scope, "192.168.50.8", 11434)
    with pytest.raises(ValueError, match="changed"):
        authenticate_lan_source(
            tied_scope,
            tied_endpoint,
            lambda: current_inventory(tied_scope),
        )

    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    duplicate = CurrentLanInterfaceState("darwin:en8", 8, ("192.168.50.7/24",))
    with pytest.raises(ValueError, match="changed"):
        authenticate_lan_source(
            scope,
            endpoint,
            lambda: current_inventory(scope, extra=(duplicate,)),
        )

    duplicated_literal_scope = PrivateScanScope.from_request(
        interface_fixture("192.168.50.7/16", "192.168.50.7/24"),
        "192.168.50.0/24",
    )
    duplicated_endpoint = ResolvedLanEndpoint.from_scope(
        duplicated_literal_scope, "192.168.50.8", 11434
    )
    with pytest.raises(ValueError, match="changed"):
        authenticate_lan_source(
            duplicated_literal_scope,
            duplicated_endpoint,
            lambda: current_inventory(duplicated_literal_scope),
        )


def test_direct_transport_rebuilds_forged_endpoint_and_binding_before_socket() -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    sockets = SocketFactory()
    transport = direct_transport(scope, sockets)

    forged_endpoint = object.__new__(ResolvedLanEndpoint)
    object.__setattr__(forged_endpoint, "interface_id", endpoint.interface_id)
    object.__setattr__(forged_endpoint, "address", "8.8.8.8")
    object.__setattr__(forged_endpoint, "port", endpoint.port)
    with pytest.raises(ValueError, match="confirmed scope"):
        transport.tcp_reachable(
            scope,
            forged_endpoint,
            source,
            deadline=0.75,
            cancellation=NeverCancelled(),
        )

    forged_source = object.__new__(type(source))
    for name, value in vars(source).items():
        object.__setattr__(forged_source, name, value)
    object.__setattr__(forged_source, "source_address", "192.168.50.9")
    with pytest.raises(LanTransportError) as captured:
        transport.tcp_reachable(
            scope,
            endpoint,
            forged_source,
            deadline=0.75,
            cancellation=NeverCancelled(),
        )
    assert captured.value.failure is LanTransportFailure.INTERFACE_CHANGED
    assert sockets.sockets == []


@pytest.mark.parametrize(
    ("address", "port"),
    [
        ("8.8.8.8", 11434),
        ("127.0.0.1", 11434),
        ("224.0.0.1", 11434),
        ("192.0.2.1", 11434),
        ("192.168.51.8", 11434),
        ("server.local", 11434),
        ("user@192.168.50.8", 11434),
        ("192.168.50.8", 22),
        ("fe80::8%99", 11434),
    ],
)
def test_direct_transport_rejects_every_forged_destination_class_before_socket(
    address: str,
    port: int,
) -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    forged = object.__new__(ResolvedLanEndpoint)
    object.__setattr__(forged, "interface_id", endpoint.interface_id)
    object.__setattr__(forged, "address", address)
    object.__setattr__(forged, "port", port)
    sockets = SocketFactory()

    with pytest.raises(ValueError, match="confirmed scope"):
        direct_transport(scope, sockets).tcp_reachable(
            scope,
            forged,
            source,
            deadline=0.75,
            cancellation=NeverCancelled(),
        )

    assert sockets.sockets == []


def test_direct_tcp_rejects_advertised_ipv6_zone_even_inside_confirmed_scope() -> None:
    scope = scope_fixture("fe80::7/64", "fe80::/64")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "fe80::8", 8000)
    zoned = ResolvedLanEndpoint.from_scope(scope, "fe80::8%en0", 8000)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    sockets = SocketFactory()

    with pytest.raises(ValueError, match="zone|confirmed scope"):
        direct_transport(scope, sockets).tcp_reachable(
            scope,
            zoned,
            source,
            deadline=0.75,
            cancellation=NeverCancelled(),
        )

    assert sockets.sockets == []


def test_direct_transport_maps_fresh_inventory_drift_before_reconnect() -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    drifted = CurrentLanInterfaceInventory(
        (CurrentLanInterfaceState(scope.interface.os_identity, 7, ("192.168.50.9/24",)),)
    )
    sockets = SocketFactory()
    transport = DirectLanHttpTransport(
        socket_factory=sockets,
        clock=lambda: 0.0,
        inventory_resolver=lambda: drifted,
        platform_name="Darwin",
    )

    with pytest.raises(LanTransportError) as captured:
        transport.request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.INTERFACE_CHANGED
    assert sockets.sockets == []


@pytest.mark.parametrize(
    ("port", "expected_request"),
    [
        (
            5001,
            b"GET /api/tags HTTP/1.1\r\n"
            b"Host: 192.168.50.8:5001\r\n"
            b"Accept: application/json\r\n"
            b"Accept-Encoding: identity\r\n"
            b"Connection: close\r\n"
            b"User-Agent: Kestrel-LAN-Discovery/1\r\n\r\n",
        ),
        (
            11434,
            b"GET /api/tags HTTP/1.1\r\n"
            b"Host: 192.168.50.8:11434\r\n"
            b"Accept: application/json\r\n"
            b"Accept-Encoding: identity\r\n"
            b"Connection: close\r\n"
            b"User-Agent: Kestrel-LAN-Discovery/1\r\n\r\n",
        ),
    ],
)
def test_direct_manual_transport_uses_one_literal_port_without_dns(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    expected_request: bytes,
) -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.50.7/24", "192.168.50.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.50.8", port)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    sockets = SocketFactory(http_response(200, b"{}"))

    def forbidden_dns(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("manual transport must never resolve a hostname")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    monkeypatch.setattr(socket, "gethostbyname", forbidden_dns)

    response = direct_transport(scope, sockets).request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_CATALOG,
        deadline=2.0,
        cancellation=NeverCancelled(),
    )

    assert (response.status_code, response.body) == (200, b"{}")
    assert sockets.sockets[0].bound == ("192.168.50.7", 0)
    assert sockets.sockets[0].connected == ("192.168.50.8", port)
    assert sockets.sockets[0].sent == expected_request


def test_direct_manual_ipv6_link_local_transport_is_bracketed_source_bound_and_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("fe80::7/64", "fe80::8/128")
    endpoint = manual_type.from_exact_scope(scope, "fe80::8", 5001)
    source = authenticate_lan_source(
        scope,
        endpoint,
        lambda: current_inventory(scope, index=23),
    )
    sockets = SocketFactory(http_response(200, b"{}"))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("manual transport crossed the literal direct-socket boundary")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "gethostbyname", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    response = direct_transport(scope, sockets, index=23).request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_CATALOG,
        deadline=2.0,
        cancellation=NeverCancelled(),
    )

    assert (response.status_code, response.body) == (200, b"{}")
    assert sockets.calls == [(socket.AF_INET6, socket.SOCK_STREAM)]
    assert sockets.sockets[0].bound == ("fe80::7", 0, 0, 23)
    assert sockets.sockets[0].connected == ("fe80::8", 5001, 0, 23)
    assert sockets.sockets[0].socket_options == [(socket.IPPROTO_IPV6, 125, 23)]
    assert sockets.sockets[0].sent == (
        b"GET /api/tags HTTP/1.1\r\n"
        b"Host: [fe80::8]:5001\r\n"
        b"Accept: application/json\r\n"
        b"Accept-Encoding: identity\r\n"
        b"Connection: close\r\n"
        b"User-Agent: Kestrel-LAN-Discovery/1\r\n\r\n"
    )


@pytest.mark.parametrize("authority_kind", ["automatic", "manual"])
def test_direct_transport_reauthenticates_fresh_inventory_before_every_connect(
    authority_kind: str,
) -> None:
    if authority_kind == "automatic":
        scope = scope_fixture("192.168.50.7/24", "192.168.50.0/24")
        endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    else:
        scope = scope_fixture("192.168.50.7/24", "192.168.50.8/32")
        endpoint = manual_endpoint_type().from_exact_scope(scope, "192.168.50.8", 5001)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    drifted = CurrentLanInterfaceInventory(
        (CurrentLanInterfaceState(scope.interface.os_identity, 7, ("192.168.50.9/24",)),)
    )
    inventories = iter((current_inventory(scope), drifted))
    resolver_calls = 0

    def resolve_inventory() -> CurrentLanInterfaceInventory:
        nonlocal resolver_calls
        resolver_calls += 1
        return next(inventories)

    sockets = SocketFactory()
    transport = DirectLanHttpTransport(
        socket_factory=sockets,
        clock=lambda: 0.0,
        inventory_resolver=resolve_inventory,
        platform_name="Darwin",
    )

    assert transport.tcp_reachable(
        scope,
        endpoint,
        source,
        deadline=0.75,
        cancellation=NeverCancelled(),
    )
    with pytest.raises(LanTransportError) as captured:
        transport.request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.INTERFACE_CHANGED
    assert resolver_calls == 2
    assert len(sockets.sockets) == 1
    assert sockets.sockets[0].sent == b""


def test_direct_transport_rebuilds_forged_manual_authority_before_socket() -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.50.7/24", "192.168.50.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.50.8", 5001)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    forged = object.__new__(manual_type)
    object.__setattr__(forged, "kind", "manual")
    object.__setattr__(forged, "interface_id", endpoint.interface_id)
    object.__setattr__(forged, "address", "8.8.8.8")
    object.__setattr__(forged, "port", endpoint.port)
    sockets = SocketFactory()

    with pytest.raises((LanTransportError, TypeError, ValueError)):
        direct_transport(scope, sockets).tcp_reachable(
            scope,
            forged,
            source,
            deadline=0.75,
            cancellation=NeverCancelled(),
        )

    assert sockets.sockets == []


@pytest.mark.parametrize("request_kind", ["tcp", "http"])
def test_direct_transport_rejects_forged_manual_kind_before_each_connect(
    request_kind: str,
) -> None:
    manual_type = manual_endpoint_type()
    scope = scope_fixture("192.168.50.7/24", "192.168.50.8/32")
    endpoint = manual_type.from_exact_scope(scope, "192.168.50.8", 5001)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    forged = copy.copy(endpoint)
    object.__setattr__(forged, "kind", "automatic")
    sockets = SocketFactory()
    transport = direct_transport(scope, sockets)

    with pytest.raises((LanTransportError, TypeError, ValueError)):
        if request_kind == "tcp":
            transport.tcp_reachable(
                scope,
                forged,
                source,
                deadline=0.75,
                cancellation=NeverCancelled(),
            )
        else:
            transport.request(
                scope,
                forged,
                source,
                LanRequestRoute.OLLAMA_CATALOG,
                deadline=2.0,
                cancellation=NeverCancelled(),
            )

    assert sockets.sockets == []


def test_unsupported_interface_pinning_fails_closed() -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    sockets = SocketFactory()

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets, platform_name="UnsupportedOS").tcp_reachable(
            scope,
            endpoint,
            source,
            deadline=0.75,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE
    assert sockets.sockets[0].closed is True


def test_linux_transport_pins_exact_interface_name_and_pin_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "SO_BINDTODEVICE", 25, raising=False)
    interface = NetworkInterface.from_addresses(
        os_identity="linux:eth7",
        display_name="Private adapter",
        addresses=("192.168.50.7/24",),
    )
    scope = PrivateScanScope.from_request(interface, "192.168.50.0/24")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    sockets = SocketFactory()
    transport = direct_transport(scope, sockets, platform_name="Linux")

    assert transport.tcp_reachable(
        scope,
        endpoint,
        source,
        deadline=0.75,
        cancellation=NeverCancelled(),
    )
    assert sockets.sockets[0].socket_options == [
        (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"eth7\0")
    ]

    class FailingPinSocket(FakeSocket):
        def setsockopt(self, level: int, option: int, value: object) -> None:
            del level, option, value
            raise OSError("pin denied")

    class FailingPinFactory(SocketFactory):
        def __call__(self, family: int, kind: int) -> FakeSocket:
            self.calls.append((family, kind))
            result = FailingPinSocket()
            self.sockets.append(result)
            return result

    failing = FailingPinFactory()
    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, failing, platform_name="Linux").tcp_reachable(
            scope,
            endpoint,
            source,
            deadline=0.75,
            cancellation=NeverCancelled(),
        )
    assert captured.value.failure is LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE
    assert failing.sockets[0].closed is True


def test_successful_tcp_connect_is_reachable_even_at_the_timeout_boundary() -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    values = iter((0.0, 0.0, 0.75))
    sockets = SocketFactory()
    transport = direct_transport(
        scope,
        sockets,
        clock=lambda: next(values, 0.8),
    )

    assert transport.tcp_reachable(
        scope,
        endpoint,
        source,
        deadline=0.75,
        cancellation=NeverCancelled(),
    )
    assert sockets.sockets[0].closed is True


def test_tcp_connect_returning_after_deadline_does_not_prove_reachability() -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    values = iter((0.0, 0.0, 0.751))
    sockets = SocketFactory()
    transport = direct_transport(scope, sockets, clock=lambda: next(values, 0.751))

    with pytest.raises(LanTransportError) as captured:
        transport.tcp_reachable(
            scope,
            endpoint,
            source,
            deadline=0.75,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.DEADLINE_EXCEEDED
    assert sockets.sockets[0].closed is True


def test_tcp_probe_uses_exact_ipv4_source_and_literal_destination_without_resolvers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    sockets = SocketFactory()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("DNS used"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("opener used"))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")

    transport = direct_transport(scope, sockets, clock=lambda: 10.0)
    assert transport.tcp_reachable(
        scope, endpoint, source, deadline=10.75, cancellation=NeverCancelled()
    )

    assert sockets.calls == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert sockets.sockets[0].bound == ("192.168.50.7", 0)
    assert sockets.sockets[0].connected == ("192.168.50.8", 11434)
    assert sockets.sockets[0].socket_options == [(socket.IPPROTO_IP, 25, 7)]
    assert sockets.sockets[0].timeouts == [0.75]
    assert sockets.sockets[0].closed is True


def test_tcp_probe_uses_selected_numeric_ifindex_for_ipv6_link_local() -> None:
    scope = scope_fixture("fe80::7/64", "fe80::/64")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "fe80::8", 8000)
    source = authenticate_lan_source(
        scope,
        endpoint,
        lambda: current_inventory(scope, index=23),
    )
    sockets = SocketFactory()
    transport = direct_transport(scope, sockets, clock=lambda: 5.0, index=23)

    assert transport.tcp_reachable(
        scope, endpoint, source, deadline=5.75, cancellation=NeverCancelled()
    )
    assert sockets.calls == [(socket.AF_INET6, socket.SOCK_STREAM)]
    assert sockets.sockets[0].bound == ("fe80::7", 0, 0, 23)
    assert sockets.sockets[0].connected == ("fe80::8", 8000, 0, 23)
    assert sockets.sockets[0].socket_options == [(socket.IPPROTO_IPV6, 125, 23)]


def test_http_transport_builds_one_fixed_credential_free_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"models":[{"name":"llama3.2"}]}'
    sockets = SocketFactory(http_response(200, body))
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    transport = direct_transport(scope, sockets, clock=lambda: 20.0)
    monkeypatch.setattr(
        provider_urls,
        "validate_provider_http_url",
        lambda *_a, **_k: pytest.fail("generic URL validator used"),
    )
    monkeypatch.setattr(
        urllib.parse,
        "urljoin",
        lambda *_a, **_k: pytest.fail("generic URL join used"),
    )

    response = transport.request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_CATALOG,
        deadline=22.0,
        cancellation=NeverCancelled(),
    )

    assert response.status_code == 200
    assert response.body == body
    request = sockets.sockets[0].sent
    assert request.startswith(b"GET /api/tags HTTP/1.1\r\n")
    assert b"Host: 192.168.50.8:11434\r\n" in request
    assert b"Authorization:" not in request
    assert b"Cookie:" not in request
    assert b"Proxy-" not in request
    assert b"Accept-Encoding: identity\r\n" in request
    assert sockets.sockets[0].timeouts[0] == 0.75
    assert sockets.sockets[0].closed is True


def test_generation_request_has_only_the_fixed_route_and_bounded_payload() -> None:
    sockets = SocketFactory(http_response(200, b'{"response":"OK"}'))
    scope = scope_fixture("fd00::7/64", "fd00::/64")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "fd00::8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    transport = direct_transport(scope, sockets, clock=lambda: 1.0)

    transport.request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_GENERATION,
        deadline=3.0,
        cancellation=NeverCancelled(),
        model=LanProbeModel.from_catalog("llama3.2"),
    )

    raw = sockets.sockets[0].sent
    head, payload = raw.split(b"\r\n\r\n", 1)
    assert head.startswith(b"POST /api/generate HTTP/1.1\r\n")
    assert b"Host: [fd00::8]:11434\r\n" in head
    assert sockets.sockets[0].bound == ("fd00::7", 0, 0, 0)
    assert sockets.sockets[0].connected == ("fd00::8", 11434, 0, 0)
    assert json.loads(payload) == {
        "model": "llama3.2",
        "options": {"num_predict": 8, "temperature": 0},
        "prompt": "Reply with OK only.",
        "stream": False,
    }


@pytest.mark.parametrize(
    "model_id",
    (
        "llama3:8b",
        "deepseek-r1:8b",
        "mistral:7b-instruct",
        "phi4:14b",
        "gpt-oss:20b",
        "qwen2.5-coder:7b",
    ),
)
def test_canonical_name_tag_model_reaches_the_exact_generation_body(model_id: str) -> None:
    sockets = SocketFactory(http_response(200, b'{"response":"OK"}'))
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    direct_transport(scope, sockets).request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_GENERATION,
        deadline=2.0,
        cancellation=NeverCancelled(),
        model=LanProbeModel.from_catalog(model_id),
    )

    _head, payload = sockets.sockets[0].sent.split(b"\r\n\r\n", 1)
    assert json.loads(payload)["model"] == model_id


@pytest.mark.parametrize(
    "model_id",
    (
        "localhost",
        "localhost:11434",
        "localhost/model:8b",
        "modelbox:11434",
        "model:1234",
        "qwen2.5-coder:1234",
        "qwen2.5-coder:",
        "qwen2.5-coder:ß",
        "registry.local/model:8b",
        "192.168.50.8/model:8b",
        "[fd00::8]:11434",
        "https://evil.invalid/model:8b",
        "user:pass@modelbox:8b",
        "token=sk-abcdefghijk:8b",
        "bad\nmodel:8b",
        unicodedata.normalize("NFD", "modèle:8b"),
    ),
)
def test_name_tag_exception_never_admits_transport_credentials_or_noncanonical_text(
    model_id: str,
) -> None:
    with pytest.raises(ValueError, match="model"):
        LanProbeModel.from_catalog(model_id)


def test_openai_generation_request_has_the_exact_fixed_route_headers_and_body() -> None:
    sockets = SocketFactory(http_response(200, b'{"choices":[{"message":{"content":"OK"}}]}'))
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 8000)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    direct_transport(scope, sockets).request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OPENAI_GENERATION,
        deadline=2.0,
        cancellation=NeverCancelled(),
        model=LanProbeModel.from_catalog("safe-model"),
    )

    head, payload = sockets.sockets[0].sent.split(b"\r\n\r\n", 1)
    assert head.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
    assert b"Host: 192.168.50.8:8000\r\n" in head
    assert b"Accept-Encoding: identity\r\n" in head
    assert b"Authorization:" not in head
    assert json.loads(payload) == {
        "max_tokens": 8,
        "messages": [{"content": "Reply with OK only.", "role": "user"}],
        "model": "safe-model",
        "stream": False,
        "temperature": 0,
    }


@pytest.mark.parametrize(
    "location",
    ("http://8.8.8.8/models", "/v1/models", "http://192.168.50.9:11434/v1/models"),
)
def test_redirect_is_returned_without_following_or_opening_another_socket(
    location: str,
) -> None:
    sockets = SocketFactory(http_response(302, b"", headers=(("Location", location),)))
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.REDIRECT_REJECTED
    assert len(sockets.sockets) == 1
    assert sockets.sockets[0].closed is True


def test_model_and_fixed_generation_body_have_independent_byte_limits() -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    sockets = SocketFactory(http_response(200, b'{"response":"OK"}'))

    direct_transport(scope, sockets).request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_GENERATION,
        deadline=2.0,
        cancellation=NeverCancelled(),
        model=LanProbeModel.from_catalog("x" * 512),
    )

    _head, body = sockets.sockets[0].sent.split(b"\r\n\r\n", 1)
    assert len(body) <= 4096
    with pytest.raises(ValueError, match="byte limit"):
        LanProbeModel.from_catalog("x" * 513)


def test_generation_rebuilds_forged_typed_model_before_opening_socket() -> None:
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    forged = object.__new__(LanProbeModel)
    object.__setattr__(forged, "model_id", "https://evil.invalid/model")
    sockets = SocketFactory()

    with pytest.raises(ValueError, match="model"):
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_GENERATION,
            deadline=2.0,
            cancellation=NeverCancelled(),
            model=forged,
        )

    assert sockets.sockets == []


def test_typed_probe_model_rejects_text_changed_by_central_secret_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KESTREL_TEST_API_KEY", "opaque-model-value-8462740")

    with pytest.raises(ValueError, match="credential"):
        LanProbeModel.from_catalog("opaque-model-value-8462740")


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 100 Continue\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 101 Switching Protocols\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 " + (b"x" * 4090) + b"\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: " + (b"9" * 5000) + b"\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Encoding: gzip\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;ext=yes\r\nx\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\nX-Trailer: no\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX-Test: safe\x00unsafe\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX\x01-Test: value\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nsmuggled",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n+1\r\nx\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\nx\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n 1\r\nx\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0x1\r\nx\r\n0\r\n\r\n",
    ],
)
def test_http_framing_rejects_interim_upgrade_ambiguous_encoded_and_extended_responses(
    response: bytes,
) -> None:
    sockets = SocketFactory(response)
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure in {
        LanTransportFailure.HTTP_PROTOCOL_REJECTED,
        LanTransportFailure.RESPONSE_TOO_LARGE,
        LanTransportFailure.UNSUPPORTED_CONTENT_ENCODING,
    }
    assert sockets.sockets[0].closed is True


def test_single_exact_chunked_response_without_extensions_or_trailers_is_accepted() -> None:
    response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
    sockets = SocketFactory(response)
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    result = direct_transport(scope, sockets).request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_CATALOG,
        deadline=2.0,
        cancellation=NeverCancelled(),
    )

    assert result.body == b"{}"


@pytest.mark.parametrize(
    "response",
    [
        (
            b"HTTP/1.1 200 "
            + (b"x" * (4096 - len(b"HTTP/1.1 200 ")))
            + b"\r\nContent-Length: 0\r\n\r\n"
        ),
        (
            b"HTTP/1.1 200 OK\r\n"
            + b"\r\n".join(f"X-{index}: v".encode() for index in range(63))
            + b"\r\nContent-Length: 0\r\n\r\n"
        ),
        b"HTTP/1.1 200 OK\r\nX:" + (b"a" * 8190) + b"\r\nContent-Length: 0\r\n\r\n",
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + (b"0" * 1023)
            + b"1\r\nx\r\n0\r\n\r\n"
        ),
        b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + (b"x" * MAX_PROBE_RESPONSE_BYTES),
    ],
)
def test_exact_http_framing_and_body_limits_are_accepted(response: bytes) -> None:
    sockets = SocketFactory(response)
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    result = direct_transport(scope, sockets).request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_CATALOG,
        deadline=2.0,
        cancellation=NeverCancelled(),
    )

    assert len(result.body) <= MAX_PROBE_RESPONSE_BYTES


@pytest.mark.parametrize(
    "response",
    [
        (
            b"HTTP/1.1 200 "
            + (b"x" * (4097 - len(b"HTTP/1.1 200 ")))
            + b"\r\nContent-Length: 0\r\n\r\n"
        ),
        (
            b"HTTP/1.1 200 OK\r\n"
            + b"\r\n".join(f"X-{index}: v".encode() for index in range(64))
            + b"\r\nContent-Length: 0\r\n\r\n"
        ),
        b"HTTP/1.1 200 OK\r\nX:" + (b"a" * 8191) + b"\r\nContent-Length: 0\r\n\r\n",
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + (b"0" * 1024)
            + b"1\r\nx\r\n0\r\n\r\n"
        ),
        b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + (b"x" * (MAX_PROBE_RESPONSE_BYTES + 1)),
    ],
)
def test_one_byte_over_http_framing_and_body_limits_is_rejected(response: bytes) -> None:
    sockets = SocketFactory(response)
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure in {
        LanTransportFailure.HTTP_PROTOCOL_REJECTED,
        LanTransportFailure.RESPONSE_TOO_LARGE,
    }


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 200 OK\r\nX: " + (b"a" * 33_000) + b"\r\n\r\n",
        http_response(200, b"x" * (256 * 1024 + 1)),
    ],
)
def test_http_transport_bounds_headers_and_body_and_always_closes(response: bytes) -> None:
    sockets = SocketFactory(response)
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure in {
        LanTransportFailure.HTTP_PROTOCOL_REJECTED,
        LanTransportFailure.RESPONSE_TOO_LARGE,
    }
    assert sockets.sockets[0].closed is True


@pytest.mark.parametrize("caller_deadline", [2.0, 100.0])
def test_one_absolute_http_deadline_is_recomputed_before_each_blocking_read(
    caller_deadline: float,
) -> None:
    class AdvancingClock:
        now = 0.0

        def __call__(self) -> float:
            result = self.now
            self.now += 0.3
            return result

    class DripSocket(FakeSocket):
        def recv(self, size: int) -> bytes:
            return super().recv(min(size, 1))

    class DripFactory(SocketFactory):
        def __call__(self, family: int, kind: int) -> FakeSocket:
            self.calls.append((family, kind))
            response = self._responses.pop(0) if self._responses else b""
            result = DripSocket(response)
            self.sockets.append(result)
            return result

    clock = AdvancingClock()
    sockets = DripFactory(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nshort")
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    with pytest.raises(LanTransportError) as captured:
        direct_transport(
            scope,
            sockets,
            clock=clock,
        ).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=caller_deadline,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.HTTP_TIMEOUT
    assert sockets.sockets[0].closed is True


def _deadline_response_parts(framing: str, *, split: bool) -> tuple[bytes, ...]:
    if framing == "content_length":
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n"
        body = b"{}"
    elif framing == "chunked":
        head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        body = b"2\r\n{}\r\n0\r\n\r\n"
    else:
        assert framing == "eof"
        head = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n"
        body = b"{}"
    return (head, body) if split else (head + body,)


@pytest.mark.parametrize("framing", ("content_length", "chunked", "eof"))
@pytest.mark.parametrize("split", (False, True), ids=("single_read", "split_read"))
def test_bytes_returned_after_the_absolute_http_deadline_are_never_consumed(
    framing: str,
    split: bool,
) -> None:
    class MutableClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    class LateSocket(FakeSocket):
        def __init__(self, parts: tuple[bytes, ...], clock: MutableClock) -> None:
            super().__init__()
            self._parts = list(parts)
            self._clock = clock
            self._expire_on_read = len(parts) + (1 if framing == "eof" else 0)
            self._reads = 0

        def recv(self, size: int) -> bytes:
            del size
            self._reads += 1
            payload = self._parts.pop(0) if self._parts else b""
            if self._reads == self._expire_on_read:
                self._clock.now = 2.001
            return payload

    class LateFactory(SocketFactory):
        def __init__(self, parts: tuple[bytes, ...], clock: MutableClock) -> None:
            super().__init__()
            self._parts = parts
            self._clock = clock

        def __call__(self, family: int, kind: int) -> FakeSocket:
            self.calls.append((family, kind))
            result = LateSocket(self._parts, self._clock)
            self.sockets.append(result)
            return result

    clock = MutableClock()
    sockets = LateFactory(_deadline_response_parts(framing, split=split), clock)
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets, clock=clock).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=20.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.HTTP_TIMEOUT
    assert sockets.sockets[0].closed is True


def test_completed_http_framing_gets_one_final_absolute_deadline_check() -> None:
    class FinalExpiryClock:
        calls = 0

        def __call__(self) -> float:
            self.calls += 1
            return 2.001 if self.calls >= 7 else 0.0

    clock = FinalExpiryClock()
    sockets = SocketFactory(http_response(200, b"{}"))
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets, clock=clock).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=20.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.HTTP_TIMEOUT
    assert sockets.sockets[0].closed is True


def test_http_completion_exactly_at_the_absolute_deadline_is_accepted() -> None:
    class BoundaryClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    class BoundarySocket(FakeSocket):
        def __init__(self, response: bytes, clock: BoundaryClock) -> None:
            super().__init__(response)
            self._clock = clock

        def recv(self, size: int) -> bytes:
            payload = super().recv(size)
            self._clock.now = 2.0
            return payload

    class BoundaryFactory(SocketFactory):
        def __init__(self, response: bytes, clock: BoundaryClock) -> None:
            super().__init__()
            self._response = response
            self._clock = clock

        def __call__(self, family: int, kind: int) -> FakeSocket:
            self.calls.append((family, kind))
            result = BoundarySocket(self._response, self._clock)
            self.sockets.append(result)
            return result

    clock = BoundaryClock()
    sockets = BoundaryFactory(http_response(200, b"{}"), clock)
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    response = direct_transport(scope, sockets, clock=clock).request(
        scope,
        endpoint,
        source,
        LanRequestRoute.OLLAMA_CATALOG,
        deadline=20.0,
        cancellation=NeverCancelled(),
    )

    assert response.body == b"{}"
    assert sockets.sockets[0].closed is True


def test_cancellation_after_recv_wins_before_returned_bytes_are_consumed() -> None:
    class MutableCancellation:
        cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    class CancellingSocket(FakeSocket):
        def __init__(self, response: bytes, cancellation: MutableCancellation) -> None:
            super().__init__(response)
            self._cancellation = cancellation

        def recv(self, size: int) -> bytes:
            payload = super().recv(size)
            self._cancellation.cancelled = True
            return payload

    class CancellingFactory(SocketFactory):
        def __init__(self, response: bytes, cancellation: MutableCancellation) -> None:
            super().__init__()
            self._response = response
            self._cancellation = cancellation

        def __call__(self, family: int, kind: int) -> FakeSocket:
            self.calls.append((family, kind))
            result = CancellingSocket(self._response, self._cancellation)
            self.sockets.append(result)
            return result

    cancellation = MutableCancellation()
    sockets = CancellingFactory(http_response(200, b"{}"), cancellation)
    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=cancellation,
        )

    assert captured.value.failure is LanTransportFailure.CANCELLED
    assert captured.value.request_progress is LanRequestProgress.REQUEST_SENT
    assert sockets.sockets[0].closed is True


def test_request_failure_progress_is_closed_secret_free_and_tracks_request_boundary() -> None:
    assert tuple(item.value for item in LanRequestProgress) == (
        "not_started",
        "connection_attempted",
        "request_sent",
    )

    scope = scope_fixture()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    source = authenticate_lan_source(scope, endpoint, lambda: current_inventory(scope))
    sockets = SocketFactory()
    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=float("inf"),
            cancellation=NeverCancelled(),
        )
    assert captured.value.failure is LanTransportFailure.HTTP_TIMEOUT
    assert captured.value.request_progress is LanRequestProgress.NOT_STARTED
    assert sockets.sockets == []

    class FailingSendSocket(FakeSocket):
        def sendall(self, payload: bytes) -> None:
            del payload
            raise OSError("secret sk-abcdefghijk at evil.invalid")

    class FailingSendFactory(SocketFactory):
        def __call__(self, family: int, kind: int) -> FakeSocket:
            self.calls.append((family, kind))
            result = FailingSendSocket()
            self.sockets.append(result)
            return result

    sockets = FailingSendFactory()

    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )

    assert captured.value.failure is LanTransportFailure.HTTP_CONNECT_FAILED
    assert captured.value.request_progress is LanRequestProgress.CONNECTION_ATTEMPTED
    assert "sk-" not in str(captured.value)
    assert "evil.invalid" not in str(captured.value)
    assert sockets.sockets[0].closed is True

    sockets = SocketFactory(b"not-http")
    with pytest.raises(LanTransportError) as captured:
        direct_transport(scope, sockets).request(
            scope,
            endpoint,
            source,
            LanRequestRoute.OLLAMA_CATALOG,
            deadline=2.0,
            cancellation=NeverCancelled(),
        )
    assert captured.value.failure is LanTransportFailure.HTTP_PROTOCOL_REJECTED
    assert captured.value.request_progress is LanRequestProgress.REQUEST_SENT
    assert sockets.sockets[0].closed is True

    with pytest.raises(TypeError, match="progress"):
        LanTransportError(
            LanTransportFailure.HTTP_TIMEOUT,
            request_progress="request_sent",  # type: ignore[arg-type]
        )
