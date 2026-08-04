from __future__ import annotations

import json
import socket
import subprocess
import sys
import textwrap
import traceback
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path

import pytest

from nested_memvid_agent.lan_discovery_models import (
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_http_transport import (
    MAX_HTTP_CHUNK_LINE_BYTES,
    MAX_HTTP_HEADER_BYTES,
    MAX_HTTP_HEADER_LINE_BYTES,
    MAX_HTTP_HEADER_LINES,
    MAX_HTTP_STATUS_LINE_BYTES,
    CurrentLanInterfaceInventory,
    CurrentLanInterfaceState,
)
from nested_memvid_agent.lan_runtime_authority import (
    LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    LanRuntimeAuthority,
    derive_lan_runtime_endpoint_binding_digest,
    derive_lan_runtime_provider_profile_id,
    derive_lan_runtime_target_id,
)
from nested_memvid_agent.llm.lan_runtime_transport import (
    MAX_LAN_RUNTIME_REQUEST_BYTES,
    MAX_LAN_RUNTIME_RESPONSE_BYTES,
    MAX_LAN_RUNTIME_TIMEOUT_SECONDS,
    DirectLanRuntimeTransport,
    LanRuntimeChatRequest,
    LanRuntimeTransportError,
    LanRuntimeTransportFailure,
)
from nested_memvid_agent.runtime_models import ChatMessage, ToolCall

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class Cancellation:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


class FakeSocket:
    def __init__(
        self,
        response: bytes = b"",
        *,
        connect_hook=None,
        send_hook=None,
        recv_hook=None,
        events: list[str] | None = None,
    ) -> None:
        self._response = bytearray(response)
        self._connect_hook = connect_hook
        self._send_hook = send_hook
        self._recv_hook = recv_hook
        self._events = events
        self.timeouts: list[float] = []
        self.socket_options: list[tuple[int, int, object]] = []
        self.bound: object | None = None
        self.connected: object | None = None
        self.sent = b""
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def setsockopt(self, level: int, option: int, value: object) -> None:
        if self._events is not None:
            self._events.append("pin")
        self.socket_options.append((level, option, value))

    def bind(self, address: object) -> None:
        if self._events is not None:
            self._events.append("bind")
        self.bound = address

    def connect(self, address: object) -> None:
        if self._events is not None:
            self._events.append("connect")
        self.connected = address
        if self._connect_hook is not None:
            self._connect_hook()

    def sendall(self, payload: bytes) -> None:
        if self._events is not None:
            self._events.append("sendall")
        if self._send_hook is not None:
            self._send_hook()
        self.sent += payload

    def recv(self, size: int) -> bytes:
        if self._recv_hook is not None:
            self._recv_hook()
        if not self._response:
            return b""
        payload = bytes(self._response[:size])
        del self._response[:size]
        return payload

    def close(self) -> None:
        self.closed = True


class SocketFactory:
    def __init__(self, *sockets: FakeSocket, events: list[str] | None = None) -> None:
        self.pending = list(sockets)
        self.events = events
        self.calls: list[tuple[int, int]] = []
        self.sockets: list[FakeSocket] = []

    def __call__(self, family: int, kind: int) -> FakeSocket:
        if self.events is not None:
            self.events.append("socket")
        self.calls.append((family, kind))
        result = self.pending.pop(0) if self.pending else FakeSocket()
        self.sockets.append(result)
        return result


class AuthorityResolver:
    def __init__(
        self,
        *values: LanRuntimeAuthority | BaseException,
        events: list[str] | None = None,
    ) -> None:
        self.values = list(values)
        self.events = events
        self.calls: list[str] = []

    def __call__(self, target_id: str) -> LanRuntimeAuthority:
        self.calls.append(target_id)
        if self.events is not None:
            self.events.append(f"resolve{len(self.calls)}")
        if not self.values:
            raise AssertionError("unexpected authority resolution")
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _scope(
    *,
    address: str = "192.168.50.7/24",
    network: str = "192.168.50.0/24",
    os_identity: str = "darwin:en7",
) -> PrivateScanScope:
    interface = NetworkInterface.from_addresses(
        os_identity=os_identity,
        display_name=os_identity,
        addresses=(address,),
    )
    return PrivateScanScope.from_request(interface, network)


def _authority(
    *,
    scope: PrivateScanScope | None = None,
    address: str = "192.168.50.8",
    port: int = 1234,
    source_address: str = "192.168.50.7",
    interface_index: int = 7,
    material_digest: str = "sha256:" + "4" * 64,
    review_digest: str = "sha256:" + "5" * 64,
    fresh_until: str = "2026-08-01T12:05:00Z",
) -> LanRuntimeAuthority:
    active_scope = scope or _scope()
    endpoint = ResolvedLanEndpoint.from_scope(active_scope, address, port)
    endpoint_binding_digest = derive_lan_runtime_endpoint_binding_digest(endpoint)
    provider_profile_id = derive_lan_runtime_provider_profile_id(endpoint_binding_digest)
    reviewed_target_id = derive_lan_runtime_target_id(provider_profile_id, "alpha")
    return LanRuntimeAuthority(
        scope=active_scope,
        endpoint=endpoint,
        source_address=source_address,
        os_interface_identity=active_scope.interface.os_identity,
        interface_index=interface_index,
        provider_profile_id=provider_profile_id,
        reviewed_target_id=reviewed_target_id,
        model_id="alpha",
        api_shape="openai_compatible",
        runtime_adapter="lan-openai-compatible",
        runtime_hardening_version=LAN_OPENAI_RUNTIME_HARDENING_VERSION,
        endpoint_binding_digest=endpoint_binding_digest,
        endpoint_fingerprint="sha256:" + "3" * 64,
        reviewed_material_binding_digest=material_digest,
        review_digest=review_digest,
        fresh_until=fresh_until,
    )


def _manual_endpoint_type():
    """Resolve the Task 7A type lazily so frozen-base collection remains useful."""

    return import_module("nested_memvid_agent.lan_discovery_models").ManualLanEndpoint


def _manual_authority(
    *,
    scope: PrivateScanScope | None = None,
    address: str = "192.168.50.8",
    port: int = 5001,
    source_address: str = "192.168.50.7",
    interface_index: int = 7,
    material_digest: str = "sha256:" + "4" * 64,
    review_digest: str = "sha256:" + "5" * 64,
    fresh_until: str = "2026-08-01T12:05:00Z",
) -> LanRuntimeAuthority:
    active_scope = scope or _scope(network="192.168.50.8/32")
    endpoint = _manual_endpoint_type().from_exact_scope(active_scope, address, port)
    endpoint_binding_digest = derive_lan_runtime_endpoint_binding_digest(endpoint)
    provider_profile_id = derive_lan_runtime_provider_profile_id(endpoint_binding_digest)
    reviewed_target_id = derive_lan_runtime_target_id(provider_profile_id, "alpha")
    return LanRuntimeAuthority(
        scope=active_scope,
        endpoint=endpoint,
        source_address=source_address,
        os_interface_identity=active_scope.interface.os_identity,
        interface_index=interface_index,
        provider_profile_id=provider_profile_id,
        reviewed_target_id=reviewed_target_id,
        model_id="alpha",
        api_shape="openai_compatible",
        runtime_adapter="lan-openai-compatible",
        runtime_hardening_version=LAN_OPENAI_RUNTIME_HARDENING_VERSION,
        endpoint_binding_digest=endpoint_binding_digest,
        endpoint_fingerprint="sha256:" + "3" * 64,
        reviewed_material_binding_digest=material_digest,
        review_digest=review_digest,
        fresh_until=fresh_until,
    )


def _inventory(authority: LanRuntimeAuthority) -> CurrentLanInterfaceInventory:
    return CurrentLanInterfaceInventory(
        (
            CurrentLanInterfaceState(
                os_identity=authority.os_interface_identity,
                interface_index=authority.interface_index,
                addresses=authority.scope.interface.addresses,
            ),
        )
    )


def _unchecked_authority(
    authority: LanRuntimeAuthority,
    **changes: object,
) -> LanRuntimeAuthority:
    forged = object.__new__(LanRuntimeAuthority)
    for field_name in authority.__dataclass_fields__:
        object.__setattr__(
            forged,
            field_name,
            changes.get(field_name, getattr(authority, field_name)),
        )
    return forged


def _unchecked_request(
    request: LanRuntimeChatRequest,
    **changes: object,
) -> LanRuntimeChatRequest:
    forged = object.__new__(LanRuntimeChatRequest)
    for field_name in request.__dataclass_fields__:
        object.__setattr__(
            forged,
            field_name,
            changes.get(field_name, getattr(request, field_name)),
        )
    return forged


def _unchecked_message(message: ChatMessage, **changes: object) -> ChatMessage:
    forged = object.__new__(ChatMessage)
    for field_name in message.__dataclass_fields__:
        object.__setattr__(
            forged,
            field_name,
            changes.get(field_name, getattr(message, field_name)),
        )
    return forged


def _unchecked_endpoint(
    endpoint: ResolvedLanEndpoint,
    **changes: object,
) -> ResolvedLanEndpoint:
    forged = object.__new__(ResolvedLanEndpoint)
    for field_name in endpoint.__dataclass_fields__:
        object.__setattr__(
            forged,
            field_name,
            changes.get(field_name, getattr(endpoint, field_name)),
        )
    return forged


def _request(*, content: str = "hello") -> LanRuntimeChatRequest:
    return LanRuntimeChatRequest(
        model_id="alpha",
        messages=(
            ChatMessage(role="system", content="Follow the system policy."),
            ChatMessage(role="user", content=content),
            ChatMessage(role="assistant", content="Prior ordinary response."),
        ),
        temperature=0.25,
    )


def _response(
    status: int = 200,
    body: bytes = b'{"choices":[{"message":{"content":"ok"}}]}',
    *,
    headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    reason = {200: "OK", 302: "Found", 307: "Redirect", 500: "Error"}.get(
        status,
        "Status",
    )
    lines = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _transport(
    authority: LanRuntimeAuthority,
    sockets: SocketFactory,
    resolver: AuthorityResolver,
    *,
    inventory_resolver=None,
    utc_clock=lambda: NOW,
    monotonic_clock=lambda: 0.0,
    platform_name: str = "Darwin",
) -> DirectLanRuntimeTransport:
    return DirectLanRuntimeTransport(
        authority_resolver=resolver,
        socket_factory=sockets,
        inventory_resolver=inventory_resolver or (lambda: _inventory(authority)),
        utc_clock=utc_clock,
        monotonic_clock=monotonic_clock,
        platform_name=platform_name,
    )


@pytest.mark.parametrize(
    ("boundary", "expected_failure"),
    (
        ("parser", None),
        ("cancellation", LanRuntimeTransportFailure.CANCELLED),
        ("utc_clock", LanRuntimeTransportFailure.AUTHORITY_EXPIRED),
        ("monotonic_clock", LanRuntimeTransportFailure.DEADLINE_EXCEEDED),
        ("authority_resolver", LanRuntimeTransportFailure.AUTHORITY_CHANGED),
        ("interface_inventory", LanRuntimeTransportFailure.INTERFACE_CHANGED),
    ),
)
def test_hostile_boundary_exceptions_have_no_cause_or_traceback_leakage(
    boundary: str,
    expected_failure: LanRuntimeTransportFailure | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.llm.lan_runtime_transport as runtime_transport

    token = f"hostile-{boundary}-token"
    authority = _authority()
    resolver = AuthorityResolver(
        RuntimeError(token) if boundary == "authority_resolver" else authority
    )

    def fail() -> object:
        raise RuntimeError(token)

    class HostileCancellation:
        def is_cancelled(self) -> bool:
            if boundary == "cancellation":
                raise RuntimeError(token)
            return False

    if boundary == "parser":

        def fail_parser(*_args, **_kwargs) -> object:
            raise ValueError(token)

        monkeypatch.setattr(runtime_transport.json, "dumps", fail_parser)
        with pytest.raises(ValueError) as raised:
            runtime_transport._canonical_request_body(authority, _request())
    else:
        transport = _transport(
            authority,
            SocketFactory(FakeSocket(_response())),
            resolver,
            inventory_resolver=(
                fail if boundary == "interface_inventory" else lambda: _inventory(authority)
            ),
            utc_clock=fail if boundary == "utc_clock" else lambda: NOW,
            monotonic_clock=fail if boundary == "monotonic_clock" else lambda: 0.0,
        )
        with pytest.raises((ValueError, LanRuntimeTransportError)) as raised:
            transport.request(
                authority,
                _request(),
                timeout_seconds=60,
                cancellation=HostileCancellation(),
            )

    if expected_failure is not None:
        assert isinstance(raised.value, LanRuntimeTransportError)
        assert raised.value.failure is expected_failure
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    rendered = "".join(
        traceback.format_exception(
            type(raised.value),
            raised.value,
            raised.value.__traceback__,
        )
    )
    assert token not in rendered


def test_runtime_transport_sends_one_canonical_numeric_request_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority()
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9999")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9999")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9999")
    monkeypatch.setenv("all_proxy", "socks5://127.0.0.1:9999")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DNS must never be used")),
    )

    body = _transport(authority, sockets, resolver).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert body == b'{"choices":[{"message":{"content":"ok"}}]}'
    assert sockets.calls == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert resolver.calls == [authority.reviewed_target_id] * 3
    assert socket_value.bound == ("192.168.50.7", 0)
    assert socket_value.connected == ("192.168.50.8", 1234)
    assert socket_value.socket_options == [(socket.IPPROTO_IP, 25, 7)]
    head, encoded = socket_value.sent.split(b"\r\n\r\n", 1)
    lowered = head.lower()
    assert head.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
    assert b"Host: 192.168.50.8:1234" in head
    assert b"Accept-Encoding: identity" in head
    assert b"Connection: close" in head
    assert b"authorization" not in lowered
    assert b"cookie" not in lowered
    assert b"proxy-" not in lowered
    assert b"secret" not in socket_value.sent
    assert json.loads(encoded) == {
        "messages": [
            {"content": "Follow the system policy.", "role": "system"},
            {"content": "hello", "role": "user"},
            {"content": "Prior ordinary response.", "role": "assistant"},
        ],
        "model": "alpha",
        "stream": False,
        "temperature": 0.25,
    }
    expected_body = (
        b'{"messages":[{"content":"Follow the system policy.","role":"system"},'
        b'{"content":"hello","role":"user"},{"content":"Prior ordinary response.",'
        b'"role":"assistant"}],"model":"alpha","stream":false,"temperature":0.25}'
    )
    expected_request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 192.168.50.8:1234\r\n"
        b"Accept: application/json\r\n"
        b"Accept-Encoding: identity\r\n"
        b"Connection: close\r\n"
        b"User-Agent: Kestrel-LAN-Runtime/1\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(expected_body)}\r\n\r\n".encode("ascii")
        + expected_body
    )
    assert socket_value.sent == expected_request
    assert socket_value.closed is True


def test_runtime_transport_imports_without_sdk_http_or_url_stacks() -> None:
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        import nested_memvid_agent
        import nested_memvid_agent.llm

        sys.modules.pop("nested_memvid_agent.llm.lan_runtime_transport", None)
        blocked = {"httpx", "openai", "requests", "urllib"}
        for name in tuple(sys.modules):
            if name.split(".", 1)[0] in blocked:
                del sys.modules[name]

        class Deny(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".", 1)[0] in blocked:
                    raise AssertionError(f"forbidden runtime dependency: {fullname}")
                return None

        sys.meta_path.insert(0, Deny())
        import nested_memvid_agent.llm.lan_runtime_transport  # noqa: F401
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(__import__("pathlib").Path(__file__).parents[1]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_resolver_and_socket_authority_order_has_no_unchecked_send_gap() -> None:
    events: list[str] = []
    authority = _authority()
    socket_value = FakeSocket(_response(), events=events)
    sockets = SocketFactory(socket_value, events=events)
    resolver = AuthorityResolver(authority, authority, authority, events=events)

    def inventory() -> CurrentLanInterfaceInventory:
        events.append("inventory")
        return _inventory(authority)

    _transport(
        authority,
        sockets,
        resolver,
        inventory_resolver=inventory,
    ).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert events == [
        "resolve1",
        "inventory",
        "socket",
        "pin",
        "bind",
        "connect",
        "resolve2",
        "resolve3",
        "sendall",
    ]


@pytest.mark.parametrize(
    "location",
    (
        "/v1/chat/completions",
        "http://192.168.50.8:1234/v1/chat/completions",
        "http://192.168.50.9:1234/v1/chat/completions",
        "https://example.com/v1/chat/completions",
    ),
)
def test_redirect_is_terminal_and_never_opens_a_second_socket(location: str) -> None:
    authority = _authority()
    sockets = SocketFactory(
        FakeSocket(_response(302, b"redirect", headers=(("Location", location),))),
        FakeSocket(_response()),
    )
    resolver = AuthorityResolver(authority, authority, authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.REDIRECT_REJECTED
    assert len(sockets.calls) == 1
    assert sockets.sockets[0].closed is True


@pytest.mark.parametrize(
    ("values", "expected_resolutions"),
    (
        ((ValueError("disabled"),), 1),
        ((None, ValueError("stale after connect")), 2),
        ((None, None, ValueError("re-reviewed before send")), 3),
    ),
)
def test_three_stage_current_binding_revalidation_blocks_bytes(
    values: tuple[LanRuntimeAuthority | BaseException | None, ...],
    expected_resolutions: int,
) -> None:
    authority = _authority()
    resolved = tuple(authority if value is None else value for value in values)
    resolver = AuthorityResolver(*resolved)
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_CHANGED
    assert len(resolver.calls) == expected_resolutions
    assert socket_value.sent == b""
    if sockets.sockets:
        assert sockets.sockets[0].closed is True


@pytest.mark.parametrize(
    "changed",
    (
        {"provider_profile_id": "lan-provider-" + "a" * 64},
        {"reviewed_target_id": "lan-target-" + "b" * 64},
        {"reviewed_material_binding_digest": "sha256:" + "8" * 64},
        {"review_digest": "sha256:" + "9" * 64},
        {"endpoint_binding_digest": "sha256:" + "c" * 64},
        {"endpoint_fingerprint": "sha256:" + "a" * 64},
        {"model_id": "beta"},
        {"api_shape": "ollama_compatible"},
        {"runtime_adapter": "openai-compatible"},
        {"runtime_hardening_version": "kestrel.lan.runtime.openai.v0"},
        {"source_address": "192.168.50.9"},
        {"os_interface_identity": "darwin:en8"},
        {"interface_index": 8},
    ),
)
@pytest.mark.parametrize("stage", (2, 3), ids=("post_connect", "pre_send"))
def test_changed_authority_after_connect_is_rejected_before_send(
    changed: dict[str, object],
    stage: int,
) -> None:
    authority = _authority()
    forged = _unchecked_authority(authority, **changed)
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(
        authority,
        *(() if stage == 2 else (authority,)),
        forged,
    )

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_CHANGED
    assert len(resolver.calls) == stage
    assert socket_value.sent == b""
    assert socket_value.closed is True


@pytest.mark.parametrize(
    "drift",
    ("scope_addresses", "confirmed_network", "endpoint_address", "endpoint_port"),
)
@pytest.mark.parametrize("stage", (2, 3), ids=("post_connect", "pre_send"))
def test_object_bypassed_scope_network_or_endpoint_drift_blocks_all_request_bytes(
    drift: str,
    stage: int,
) -> None:
    authority = _authority()
    if drift == "scope_addresses":
        changed_scope = _scope(address="192.168.50.9/24")
        forged = _unchecked_authority(authority, scope=changed_scope)
    elif drift == "confirmed_network":
        changed_scope = _scope(network="192.168.50.0/25")
        forged = _unchecked_authority(authority, scope=changed_scope)
    elif drift == "endpoint_address":
        forged = _unchecked_authority(
            authority,
            endpoint=_unchecked_endpoint(authority.endpoint, address="192.168.50.9"),
        )
    else:
        forged = _unchecked_authority(
            authority,
            endpoint=_unchecked_endpoint(authority.endpoint, port=8000),
        )
    assert forged.endpoint_binding_digest == authority.endpoint_binding_digest
    assert forged.endpoint_fingerprint == authority.endpoint_fingerprint
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(
        authority,
        *(() if stage == 2 else (authority,)),
        forged,
    )

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_CHANGED
    assert len(resolver.calls) == stage
    assert socket_value.sent == b""
    assert socket_value.closed is True


def test_only_newer_fresh_until_on_identical_authority_is_accepted() -> None:
    authority = _authority()
    refreshed = replace(authority, fresh_until="2026-08-01T12:06:00Z")
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, refreshed, refreshed)

    result = _transport(authority, sockets, resolver).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert result.startswith(b'{"choices"')
    assert socket_value.sent
    assert socket_value.closed is True


def test_older_but_still_fresh_authority_is_rejected_before_send() -> None:
    authority = _authority(fresh_until="2026-08-01T12:05:00Z")
    older = replace(authority, fresh_until="2026-08-01T12:04:59Z")
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, older)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_CHANGED
    assert socket_value.connected == ("192.168.50.8", 1234)
    assert socket_value.sent == b""
    assert socket_value.closed is True


@pytest.mark.parametrize(
    "inventory",
    (
        CurrentLanInterfaceInventory(
            (CurrentLanInterfaceState("darwin:en7", 7, ("192.168.50.9/24",)),)
        ),
        CurrentLanInterfaceInventory(
            (CurrentLanInterfaceState("darwin:en7", 8, ("192.168.50.7/24",)),)
        ),
        CurrentLanInterfaceInventory(
            (
                CurrentLanInterfaceState("darwin:en7", 7, ("192.168.50.7/24",)),
                CurrentLanInterfaceState("darwin:en8", 8, ("192.168.50.7/24",)),
            )
        ),
    ),
)
def test_interface_source_or_ifindex_drift_fails_before_socket(
    inventory: CurrentLanInterfaceInventory,
) -> None:
    authority = _authority()
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(
            authority,
            sockets,
            resolver,
            inventory_resolver=lambda: inventory,
        ).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.INTERFACE_CHANGED
    assert sockets.calls == []


@pytest.mark.parametrize(
    ("scope", "destination", "source", "family", "source_sockaddr", "destination_sockaddr"),
    (
        (
            _scope(),
            "192.168.50.8",
            "192.168.50.7",
            socket.AF_INET,
            ("192.168.50.7", 0),
            ("192.168.50.8", 1234),
        ),
        (
            _scope(address="fd00::7/64", network="fd00::/64"),
            "fd00::8",
            "fd00::7",
            socket.AF_INET6,
            ("fd00::7", 0, 0, 0),
            ("fd00::8", 1234, 0, 0),
        ),
        (
            _scope(address="fe80::7/64", network="fe80::/64"),
            "fe80::8",
            "fe80::7",
            socket.AF_INET6,
            ("fe80::7", 0, 0, 7),
            ("fe80::8", 1234, 0, 7),
        ),
    ),
)
def test_numeric_sockaddr_rules_and_source_binding_are_exact(
    scope: PrivateScanScope,
    destination: str,
    source: str,
    family: int,
    source_sockaddr: object,
    destination_sockaddr: object,
) -> None:
    authority = _authority(scope=scope, address=destination, source_address=source)
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    _transport(authority, sockets, resolver).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert sockets.calls == [(family, socket.SOCK_STREAM)]
    assert socket_value.bound == source_sockaddr
    assert socket_value.connected == destination_sockaddr
    expected_level = socket.IPPROTO_IP if family == socket.AF_INET else socket.IPPROTO_IPV6
    expected_option = 25 if family == socket.AF_INET else 125
    assert socket_value.socket_options == [(expected_level, expected_option, 7)]


def test_linux_runtime_pins_the_exact_trusted_interface_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "SO_BINDTODEVICE", 25, raising=False)
    scope = _scope(os_identity="linux:eth7")
    authority = _authority(scope=scope)
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    _transport(
        authority,
        sockets,
        resolver,
        platform_name="Linux",
    ).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert socket_value.socket_options == [(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"eth7\0")]
    assert socket_value.bound == ("192.168.50.7", 0)
    assert socket_value.connected == ("192.168.50.8", 1234)
    assert socket_value.closed is True


def test_unsupported_runtime_interface_pinning_fails_closed_before_bind_or_send() -> None:
    authority = _authority()
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(
            authority,
            sockets,
            resolver,
            platform_name="UnsupportedOS",
        ).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.INTERFACE_PINNING_UNAVAILABLE
    assert socket_value.bound is None
    assert socket_value.connected is None
    assert socket_value.sent == b""
    assert socket_value.closed is True


@pytest.mark.parametrize(
    ("phase", "expected_failure"),
    (
        ("pin", "INTERFACE_PINNING_UNAVAILABLE"),
        ("bind", "HTTP_CONNECT_FAILED"),
        ("connect", "HTTP_CONNECT_FAILED"),
        ("connect_timeout", "HTTP_TIMEOUT"),
        ("send", "HTTP_CONNECT_FAILED"),
        ("read_timeout", "HTTP_TIMEOUT"),
    ),
)
def test_socket_phase_failures_have_closed_codes_zero_leakage_and_cleanup(
    phase: str,
    expected_failure: str,
) -> None:
    authority = _authority()

    class PhaseFailureSocket(FakeSocket):
        def setsockopt(self, level: int, option: int, value: object) -> None:
            if phase == "pin":
                raise OSError("pin-secret")
            super().setsockopt(level, option, value)

        def bind(self, address: object) -> None:
            if phase == "bind":
                raise OSError("bind-secret")
            super().bind(address)

        def connect(self, address: object) -> None:
            if phase == "connect":
                raise OSError("connect-secret")
            if phase == "connect_timeout":
                raise TimeoutError("connect-timeout-secret")
            super().connect(address)

        def sendall(self, payload: bytes) -> None:
            if phase == "send":
                raise OSError("send-secret")
            super().sendall(payload)

        def recv(self, size: int) -> bytes:
            if phase == "read_timeout":
                raise TimeoutError("read-timeout-secret")
            return super().recv(size)

    socket_value = PhaseFailureSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is getattr(LanRuntimeTransportFailure, expected_failure)
    assert "secret" not in str(raised.value).lower()
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert (
        "secret"
        not in "".join(
            traceback.format_exception(
                type(raised.value),
                raised.value,
                raised.value.__traceback__,
            )
        ).lower()
    )
    if phase in {"pin", "bind", "connect", "connect_timeout"}:
        assert socket_value.sent == b""
    assert socket_value.closed is True


@pytest.mark.parametrize(
    ("address", "port"),
    (
        ("127.0.0.1", 1234),
        ("8.8.8.8", 1234),
        ("224.0.0.1", 1234),
        ("192.0.2.8", 1234),
        ("server.local", 1234),
        ("192.168.50.8", 22),
        ("fe80::8%en7", 1234),
    ),
)
def test_forged_destination_classes_fail_before_socket(address: str, port: int) -> None:
    authority = _authority()
    forged_endpoint = object.__new__(ResolvedLanEndpoint)
    object.__setattr__(forged_endpoint, "interface_id", authority.endpoint.interface_id)
    object.__setattr__(forged_endpoint, "address", address)
    object.__setattr__(forged_endpoint, "port", port)
    forged = _unchecked_authority(authority, endpoint=forged_endpoint)
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(forged)

    with pytest.raises((ValueError, LanRuntimeTransportError)):
        _transport(forged, sockets, resolver).request(
            forged,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert sockets.calls == []


def test_request_bound_is_checked_before_socket() -> None:
    authority = _authority()
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(content="x" * (MAX_LAN_RUNTIME_REQUEST_BYTES + 1)),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.REQUEST_TOO_LARGE
    assert sockets.calls == []


def test_exact_one_mib_request_succeeds_and_one_byte_more_fails_closed() -> None:
    authority = _authority()
    prefix = b'{"messages":[{"content":"'
    suffix = b'","role":"user"}],"model":"alpha","stream":false,"temperature":null}'
    exact_content = "x" * (MAX_LAN_RUNTIME_REQUEST_BYTES - len(prefix) - len(suffix))
    exact = LanRuntimeChatRequest(
        model_id="alpha",
        messages=(ChatMessage(role="user", content=exact_content),),
        temperature=None,
    )
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    _transport(authority, sockets, resolver).request(
        authority,
        exact,
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert len(socket_value.sent.split(b"\r\n\r\n", 1)[1]) == MAX_LAN_RUNTIME_REQUEST_BYTES
    too_large = replace(
        exact,
        messages=(ChatMessage(role="user", content=exact_content + "x"),),
    )
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)
    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            too_large,
            timeout_seconds=60,
            cancellation=Cancellation(),
        )
    assert raised.value.failure is LanRuntimeTransportFailure.REQUEST_TOO_LARGE
    assert sockets.calls == []


def test_oversized_declared_content_length_is_rejected_and_closes_socket() -> None:
    authority = _authority()
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(MAX_LAN_RUNTIME_RESPONSE_BYTES + 1).encode("ascii")
        + b"\r\n\r\n"
    )
    socket_value = FakeSocket(response)
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.RESPONSE_TOO_LARGE
    assert socket_value.closed is True


def test_runtime_parser_limits_are_separate_from_immutable_discovery_limits() -> None:
    assert (
        MAX_HTTP_STATUS_LINE_BYTES,
        MAX_HTTP_HEADER_BYTES,
        MAX_HTTP_HEADER_LINES,
        MAX_HTTP_HEADER_LINE_BYTES,
        MAX_HTTP_CHUNK_LINE_BYTES,
    ) == (4 * 1024, 32 * 1024, 64, 8 * 1024, 1024)
    assert MAX_LAN_RUNTIME_REQUEST_BYTES == 1024 * 1024
    assert MAX_LAN_RUNTIME_RESPONSE_BYTES == 16 * 1024 * 1024
    assert MAX_LAN_RUNTIME_TIMEOUT_SECONDS == 120


@pytest.mark.parametrize("framing", ("content_length", "chunked", "eof"))
@pytest.mark.parametrize("extra", (0, 1), ids=("exact_limit", "one_byte_over"))
def test_sixteen_mib_response_bound_is_enforced_on_actual_bytes(
    framing: str,
    extra: int,
) -> None:
    authority = _authority()
    body = b"x" * (MAX_LAN_RUNTIME_RESPONSE_BYTES + extra)
    if framing == "content_length":
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\n\r\n"
            + body
        )
    elif framing == "chunked":
        response = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + f"{len(body):x}\r\n".encode("ascii")
            + body
            + b"\r\n0\r\n\r\n"
        )
    else:
        response = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body
    socket_value = FakeSocket(response)
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)
    transport = _transport(authority, sockets, resolver)

    if extra:
        with pytest.raises(LanRuntimeTransportError) as raised:
            transport.request(
                authority,
                _request(),
                timeout_seconds=60,
                cancellation=Cancellation(),
            )
        assert raised.value.failure is LanRuntimeTransportFailure.RESPONSE_TOO_LARGE
    else:
        assert (
            transport.request(
                authority,
                _request(),
                timeout_seconds=60,
                cancellation=Cancellation(),
            )
            == body
        )
    assert socket_value.closed is True


@pytest.mark.parametrize(
    "response",
    (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2;ext=x\r\n{}\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\nX-Trailer: no\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nsmuggled",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n+2\r\n{}\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-2\r\n{}\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n 2\r\n{}\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0x2\r\n{}\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\n{}\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}extra",
        b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n{}",
        b"HTTP/1.1 100 Continue\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 101 Switching Protocols\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\nContent-Length: 2\n\n{}",
        b"HTTP/1.1 200 OK\r\nX-Test: safe\x00unsafe\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nX\x01-Test: value\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\n\r\n{}",
        b"HTTP/1.1 200 "
        + (b"x" * (MAX_HTTP_STATUS_LINE_BYTES + 1 - len(b"HTTP/1.1 200 ")))
        + b"\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n"
        + b"\r\n".join(f"X-{index}: v".encode() for index in range(MAX_HTTP_HEADER_LINES))
        + b"\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX:"
        + (b"a" * (MAX_HTTP_HEADER_LINE_BYTES - 1))
        + b"\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX: " + (b"a" * MAX_HTTP_HEADER_BYTES) + b"\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + (b"0" * MAX_HTTP_CHUNK_LINE_BYTES)
        + b"1\r\nx\r\n0\r\n\r\n",
    ),
)
def test_hostile_http_framing_is_rejected_and_socket_is_closed(response: bytes) -> None:
    authority = _authority()
    socket_value = FakeSocket(response)
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure in {
        LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED,
        LanRuntimeTransportFailure.UNSUPPORTED_CONTENT_ENCODING,
    }
    assert socket_value.closed is True


@pytest.mark.parametrize(
    "response",
    (
        b"HTTP/1.1 200 "
        + (b"x" * (MAX_HTTP_STATUS_LINE_BYTES - len(b"HTTP/1.1 200 ")))
        + b"\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n"
        + b"\r\n".join(f"X-{index}: v".encode() for index in range(MAX_HTTP_HEADER_LINES - 1))
        + b"\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX:"
        + (b"a" * (MAX_HTTP_HEADER_LINE_BYTES - len(b"X:")))
        + b"\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + (b"0" * (MAX_HTTP_CHUNK_LINE_BYTES - 1))
        + b"1\r\nx\r\n0\r\n\r\n",
    ),
)
def test_exact_runtime_http_parser_boundaries_are_accepted(response: bytes) -> None:
    authority = _authority()
    socket_value = FakeSocket(response)
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    _transport(authority, sockets, resolver).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert socket_value.closed is True


@pytest.mark.parametrize("status", (201, 204, 400, 500))
def test_only_exact_http_200_is_accepted(status: int) -> None:
    authority = _authority()
    socket_value = FakeSocket(_response(status, b"{}"))
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.HTTP_STATUS_REJECTED
    assert socket_value.closed is True


def test_absolute_deadline_is_minimum_of_requested_cap_and_freshness() -> None:
    authority = _authority(fresh_until="2026-08-01T12:00:30Z")
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    _transport(
        authority,
        sockets,
        resolver,
        monotonic_clock=lambda: 100.0,
    ).request(
        authority,
        _request(),
        timeout_seconds=MAX_LAN_RUNTIME_TIMEOUT_SECONDS + 999,
        cancellation=Cancellation(),
    )

    assert socket_value.timeouts
    assert max(socket_value.timeouts) <= 30.0


@pytest.mark.parametrize(
    "timeout_seconds",
    (False, 0, -1, float("nan"), float("inf")),
)
def test_invalid_or_expired_deadline_fails_before_socket(timeout_seconds: object) -> None:
    authority = _authority()
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.DEADLINE_EXCEEDED
    assert sockets.calls == []


def test_expired_authority_fails_before_socket() -> None:
    authority = _authority(fresh_until="2026-08-01T11:59:59Z")
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_EXPIRED
    assert sockets.calls == []


def test_transport_token_cancellation_before_socket_and_during_read_closes_resources() -> None:
    authority = _authority()
    cancelled = Cancellation(True)
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)

    with pytest.raises(LanRuntimeTransportError) as before:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=cancelled,
        )
    assert before.value.failure is LanRuntimeTransportFailure.CANCELLED
    assert sockets.calls == []

    during = Cancellation()
    socket_value = FakeSocket(_response(), recv_hook=lambda: setattr(during, "cancelled", True))
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)
    with pytest.raises(LanRuntimeTransportError) as read:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=during,
        )
    assert read.value.failure is LanRuntimeTransportFailure.CANCELLED
    assert socket_value.closed is True


@pytest.mark.parametrize("stage", (2, 3), ids=("post_connect", "pre_send"))
def test_cancellation_after_connect_or_immediately_before_send_emits_zero_bytes(
    stage: int,
) -> None:
    authority = _authority()
    cancellation = Cancellation()
    calls = 0

    def resolve(target_id: str) -> LanRuntimeAuthority:
        nonlocal calls
        assert target_id == authority.reviewed_target_id
        calls += 1
        if calls == stage:
            cancellation.cancelled = True
        return authority

    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    transport = DirectLanRuntimeTransport(
        authority_resolver=resolve,
        socket_factory=sockets,
        inventory_resolver=lambda: _inventory(authority),
        utc_clock=lambda: NOW,
        monotonic_clock=lambda: 0.0,
        platform_name="Darwin",
    )

    with pytest.raises(LanRuntimeTransportError) as raised:
        transport.request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=cancellation,
        )

    assert raised.value.failure is LanRuntimeTransportFailure.CANCELLED
    assert calls == stage
    assert socket_value.connected == ("192.168.50.8", 1234)
    assert socket_value.sent == b""
    assert socket_value.closed is True


@pytest.mark.parametrize("expiry_stage", (2, 3), ids=("post_connect", "pre_send"))
def test_authority_expiry_after_connect_or_before_send_emits_zero_bytes(
    expiry_stage: int,
) -> None:
    authority = _authority(fresh_until="2026-08-01T12:00:10Z")
    utc_values = [NOW]
    utc_values.extend(
        NOW if stage < expiry_stage else NOW + timedelta(seconds=11) for stage in range(1, 4)
    )

    def utc_clock() -> datetime:
        return utc_values.pop(0) if utc_values else NOW + timedelta(seconds=11)

    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)
    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(
            authority,
            sockets,
            resolver,
            utc_clock=utc_clock,
        ).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_EXPIRED
    assert socket_value.connected == ("192.168.50.8", 1234)
    assert socket_value.sent == b""
    assert socket_value.closed is True


@pytest.mark.parametrize(
    ("requested", "fresh_until", "maximum_budget"),
    (
        (10, "2026-08-01T12:05:00Z", 10.0),
        (999, "2026-08-01T12:05:00Z", 120.0),
        (999, "2026-08-01T12:00:30Z", 30.0),
    ),
)
def test_one_absolute_deadline_uses_requested_cap_or_original_freshness(
    requested: float,
    fresh_until: str,
    maximum_budget: float,
) -> None:
    authority = _authority(fresh_until=fresh_until)
    refreshed = replace(authority, fresh_until="2026-08-01T12:10:00Z")
    ticks = iter((100.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0))
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, refreshed, refreshed)

    _transport(
        authority,
        sockets,
        resolver,
        monotonic_clock=lambda: next(ticks, 106.0),
    ).request(
        authority,
        _request(),
        timeout_seconds=requested,
        cancellation=Cancellation(),
    )

    assert socket_value.timeouts
    assert max(socket_value.timeouts) <= maximum_budget
    assert socket_value.timeouts[-1] < maximum_budget


def test_connect_send_and_partial_read_errors_are_closed_and_cleanup_socket() -> None:
    authority = _authority()

    class BrokenConnectSocket(FakeSocket):
        def connect(self, address: object) -> None:
            self.connected = address
            raise OSError("secret-token-from-kernel")

    fixtures = (
        BrokenConnectSocket(),
        FakeSocket(_response(), send_hook=lambda: (_ for _ in ()).throw(OSError("secret-send"))),
        FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\npartial"),
    )
    for socket_value in fixtures:
        sockets = SocketFactory(socket_value)
        resolver = AuthorityResolver(authority, authority, authority)
        with pytest.raises(LanRuntimeTransportError) as raised:
            _transport(authority, sockets, resolver).request(
                authority,
                _request(),
                timeout_seconds=60,
                cancellation=Cancellation(),
            )
        assert "secret" not in str(raised.value).lower()
        assert "partial" not in str(raised.value).lower()
        assert socket_value.closed is True


def test_transport_never_logs_response_identity_body_kernel_or_resolver_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    authority = _authority()
    identity = "HOSTILE_TRANSPORT_IDENTITY_2F10C4"
    body_token = "HOSTILE_TRANSPORT_BODY_A18E73"
    response = json.dumps(
        {"id": identity, "choices": [{"message": {"content": body_token}}]},
        separators=(",", ":"),
    ).encode("utf-8")
    socket_value = FakeSocket(_response(body=response))
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)
    assert (
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )
        == response
    )

    kernel_token = "HOSTILE_KERNEL_EXCEPTION_D3A965"

    class KernelFailureSocket(FakeSocket):
        def connect(self, address: object) -> None:
            self.connected = address
            raise OSError(kernel_token)

    sockets = SocketFactory(KernelFailureSocket())
    resolver = AuthorityResolver(authority)
    with pytest.raises(LanRuntimeTransportError):
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    resolver_token = "HOSTILE_RESOLVER_EXCEPTION_6B82F1"
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(ValueError(resolver_token))
    with pytest.raises(LanRuntimeTransportError):
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    rendered = caplog.text
    for token in (identity, body_token, kernel_token, resolver_token):
        assert token not in rendered


def test_request_value_is_exact_and_rejects_tool_metadata_before_socket() -> None:
    authority = _authority()
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)
    forged_message = ChatMessage(role="assistant", content="", tool_calls=())
    object.__setattr__(forged_message, "tool_call_id", "tool-credential")
    hostile = LanRuntimeChatRequest(
        model_id="alpha",
        messages=(forged_message,),
        temperature=None,
    )

    with pytest.raises((TypeError, ValueError, LanRuntimeTransportError)):
        _transport(authority, sockets, resolver).request(
            authority,
            hostile,
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert sockets.calls == []


@pytest.mark.parametrize(
    "case",
    (
        "model_mismatch",
        "model_bool",
        "messages_list",
        "messages_tuple_subclass",
        "message_mapping",
        "message_subclass",
        "tool_role",
        "invalid_role",
        "role_control",
        "tool_calls",
        "name",
        "tool_call_id",
        "content_control",
        "content_surrogate",
        "content_non_string",
        "temperature_bool",
        "temperature_nan",
        "temperature_inf",
    ),
)
def test_runtime_request_boundary_rejects_bypassed_values_before_socket(case: str) -> None:
    authority = _authority()
    request = _request()
    valid_message = ChatMessage(role="user", content="hello")
    if case == "model_mismatch":
        hostile = replace(request, model_id="beta")
    elif case == "model_bool":
        hostile = _unchecked_request(request, model_id=True)
    elif case == "messages_list":
        hostile = _unchecked_request(request, messages=[valid_message])
    elif case == "messages_tuple_subclass":

        class MessageTuple(tuple):
            pass

        hostile = _unchecked_request(request, messages=MessageTuple((valid_message,)))
    elif case == "message_mapping":
        hostile = _unchecked_request(request, messages=({"role": "user", "content": "x"},))
    elif case == "message_subclass":

        class MessageSubclass(ChatMessage):
            pass

        forged_message = object.__new__(MessageSubclass)
        for field_name in valid_message.__dataclass_fields__:
            object.__setattr__(
                forged_message,
                field_name,
                getattr(valid_message, field_name),
            )
        hostile = _unchecked_request(request, messages=(forged_message,))
    elif case == "tool_role":
        hostile = _unchecked_request(
            request,
            messages=(_unchecked_message(valid_message, role="tool"),),
        )
    elif case == "invalid_role":
        hostile = _unchecked_request(
            request,
            messages=(_unchecked_message(valid_message, role="developer"),),
        )
    elif case == "role_control":
        hostile = _unchecked_request(
            request,
            messages=(_unchecked_message(valid_message, role="user\x00"),),
        )
    elif case == "tool_calls":
        hostile = _unchecked_request(
            request,
            messages=(
                _unchecked_message(
                    valid_message,
                    role="assistant",
                    tool_calls=(ToolCall(name="memory.search", arguments={}),),
                ),
            ),
        )
    elif case == "name":
        hostile = _unchecked_request(
            request,
            messages=(_unchecked_message(valid_message, name="caller-name"),),
        )
    elif case == "tool_call_id":
        hostile = _unchecked_request(
            request,
            messages=(_unchecked_message(valid_message, tool_call_id="call-secret"),),
        )
    elif case == "content_control":
        hostile = _unchecked_request(
            request,
            messages=(_unchecked_message(valid_message, content="bad\x00control"),),
        )
    elif case == "content_surrogate":
        hostile = _unchecked_request(
            request,
            messages=(_unchecked_message(valid_message, content="bad\ud800surrogate"),),
        )
    elif case == "content_non_string":
        hostile = _unchecked_request(
            request,
            messages=(_unchecked_message(valid_message, content=7),),
        )
    elif case == "temperature_bool":
        hostile = _unchecked_request(request, temperature=False)
    elif case == "temperature_nan":
        hostile = _unchecked_request(request, temperature=float("nan"))
    else:
        hostile = _unchecked_request(request, temperature=float("inf"))
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)

    with pytest.raises((TypeError, ValueError, LanRuntimeTransportError)):
        _transport(authority, sockets, resolver).request(
            authority,
            hostile,
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert sockets.calls == []


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("source_address", "192.168.50.9"),
        ("source_address", "server.local"),
        ("os_interface_identity", "darwin:en8"),
        ("interface_index", False),
        ("endpoint_binding_digest", "sha256:" + "f" * 64),
        ("fresh_until", "2026-08-01 12:05:00"),
    ),
)
def test_transport_independently_reconstructs_object_bypass_authority(
    field_name: str,
    value: object,
) -> None:
    valid = _authority()
    forged = _unchecked_authority(valid, **{field_name: value})
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(forged)

    with pytest.raises((ValueError, LanRuntimeTransportError)):
        _transport(forged, sockets, resolver).request(
            forged,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert sockets.calls == []


def test_transport_rejects_runtime_request_subclass_before_socket() -> None:
    authority = _authority()

    class RequestSubclass(LanRuntimeChatRequest):
        pass

    valid = _request()
    forged = object.__new__(RequestSubclass)
    for field_name in valid.__dataclass_fields__:
        object.__setattr__(forged, field_name, getattr(valid, field_name))
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)

    with pytest.raises((TypeError, ValueError, LanRuntimeTransportError)):
        _transport(authority, sockets, resolver).request(
            authority,
            forged,
            timeout_seconds=60,
            cancellation=Cancellation(),
        )
    assert sockets.calls == []


def test_zero_freshness_budget_and_one_microsecond_late_are_closed_before_socket() -> None:
    boundary = _authority(fresh_until="2026-08-01T12:00:00Z")
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(boundary)
    with pytest.raises(LanRuntimeTransportError) as at_boundary:
        _transport(boundary, sockets, resolver).request(
            boundary,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )
    assert at_boundary.value.failure is LanRuntimeTransportFailure.AUTHORITY_EXPIRED
    assert sockets.calls == []

    late = _authority(fresh_until="2026-08-01T12:00:00Z")
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(late)
    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(
            late,
            sockets,
            resolver,
            utc_clock=lambda: NOW + timedelta(microseconds=1),
        ).request(
            late,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )
    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_EXPIRED
    assert sockets.calls == []


def _enabled_lan_registry(
    tmp_path: Path,
    *,
    review_target: bool = True,
    interface_index: int = 7,
):
    import test_lan_discovery_service as lan_cases

    from nested_memvid_agent.routing.ledger import RoutingLedger
    from nested_memvid_agent.state_store import AgentStateStore

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scope = lan_cases._scope(
        network="192.168.50.0/29",
        os_identity="darwin:en7",
        display_name="Ambient adapter label before rename",
        addresses=("192.168.50.1/29",),
    )
    observation = lan_cases._positive_observation(scope=scope)
    _row, completed = lan_cases._persist_completed_scan(
        state,
        scan_id="scan-runtime-authority",
        observation=observation,
        scope=scope,
    )
    registry = RoutingLedger(state)
    inventory = CurrentLanInterfaceInventory(
        (
            CurrentLanInterfaceState(
                os_identity="darwin:en7",
                interface_index=interface_index,
                addresses=("192.168.50.1/29",),
            ),
        )
    )
    service = lan_cases._task5b_service(
        registry,
        interface_inventory_resolver=lambda: inventory,
    )
    provider_id = lan_cases._provider_id(observation.endpoint_binding_digest)
    target_id = lan_cases._target_id(provider_id, "alpha")
    service.import_observation(
        lan_cases._import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=lan_cases.OWNER,
    )
    if review_target:
        profile = registry.get_provider_profile(provider_id)
        target = registry.get_model_target(target_id)
        assert profile is not None and target is not None
        review, _privacy, _material = lan_cases._exact_review_request(
            owner=lan_cases.OWNER,
            profile_revision=profile.revision,
            target_revision=target.revision,
            target_id=target_id,
            protected=target.target.metadata["lan_discovery"],
            enabled=True,
        )
        service.review_lan_target(review, authenticated_owner_principal=lan_cases.OWNER)
    return state, registry, service, scope, observation, provider_id, target_id, inventory


def test_registry_resolver_begins_before_select_and_uses_one_coherent_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        state,
        registry,
        _service,
        _scope_value,
        _observation,
        provider_id,
        target_id,
        inventory,
    ) = _enabled_lan_registry(tmp_path)
    events: list[str] = []
    connections = 0
    original_connect = state._connect

    class AuditedConnection:
        def __init__(self, connection) -> None:
            self._connection = connection

        def execute(self, sql: str, parameters=()):
            token = sql.strip().split(None, 1)[0].upper()
            events.append(token)
            return self._connection.execute(sql, parameters)

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    @contextmanager
    def audited_connect():
        nonlocal connections
        connections += 1
        with original_connect() as connection:
            yield AuditedConnection(connection)

    monkeypatch.setattr(state, "_connect", audited_connect)
    monkeypatch.setattr(
        registry,
        "get_provider_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("resolver must not compose public getters")
        ),
    )
    monkeypatch.setattr(
        registry,
        "get_model_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("resolver must not compose public getters")
        ),
    )

    authority = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: NOW,
        interface_inventory_resolver=lambda: inventory,
    )

    assert connections == 1
    assert events[0] == "BEGIN"
    assert "SELECT" in events
    assert events.index("BEGIN") < events.index("SELECT")
    assert authority.provider_profile_id == provider_id
    assert authority.reviewed_target_id == target_id
    assert authority.model_id == "alpha"
    assert authority.scope.interface.display_name == authority.os_interface_identity
    assert authority.source_address == "192.168.50.1"
    assert authority.interface_index == 7


def test_registry_resolver_authenticates_different_profile_and_target_evidence_pointers(
    tmp_path: Path,
) -> None:
    import test_lan_discovery_service as lan_cases

    (
        state,
        registry,
        service,
        scope,
        _initial,
        provider_id,
        target_id,
        inventory,
    ) = _enabled_lan_registry(tmp_path)
    before_profile = registry.get_provider_profile(provider_id)
    before_target = registry.get_model_target(target_id)
    assert before_profile is not None and before_target is not None
    incomplete = lan_cases._positive_observation(
        scope=scope,
        models=("alpha", "beta"),
        catalog_complete=False,
        catalog_truncated=False,
    )
    beta_id = lan_cases._target_id(provider_id, "beta")
    _row, completed = lan_cases._persist_completed_scan(
        state,
        scan_id="scan-profile-newer-than-target",
        observation=incomplete,
        scope=scope,
        observed_at=NOW + timedelta(seconds=1),
    )
    service = lan_cases._task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    service.import_observation(
        lan_cases._import_request(
            incomplete,
            completed,
            profile_revision=before_profile.revision,
            target_revisions=((beta_id, 0),),
        ),
        authenticated_owner_principal=lan_cases.OWNER,
    )
    profile = registry.get_provider_profile(provider_id)
    alpha = registry.get_model_target(target_id)
    assert profile is not None and alpha is not None
    profile_lan = profile.profile.metadata["lan_discovery"]
    target_lan = alpha.target.metadata["lan_discovery"]
    assert profile_lan["terminal_receipt_digest"] != target_lan["terminal_receipt_digest"]
    assert profile_lan["observation_digest"] != target_lan["observation_digest"]

    authority = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: NOW + timedelta(seconds=1),
        interface_inventory_resolver=lambda: inventory,
    )

    assert authority.provider_profile_id == provider_id
    assert authority.reviewed_target_id == target_id
    assert (
        authority.reviewed_material_binding_digest == target_lan["reviewed_material_binding_digest"]
    )
    assert authority.review_digest == target_lan["review_digest"]


@pytest.mark.parametrize("pointer", ("profile", "target"))
def test_registry_resolver_rejects_tamper_in_either_independent_evidence_pointer(
    tmp_path: Path,
    pointer: str,
) -> None:
    import test_lan_discovery_service as lan_cases

    (
        state,
        registry,
        service,
        scope,
        _initial,
        provider_id,
        target_id,
        inventory,
    ) = _enabled_lan_registry(tmp_path)
    before_profile = registry.get_provider_profile(provider_id)
    before_target = registry.get_model_target(target_id)
    assert before_profile is not None and before_target is not None
    incomplete = lan_cases._positive_observation(
        scope=scope,
        models=("alpha", "beta"),
        catalog_complete=False,
        catalog_truncated=False,
    )
    beta_id = lan_cases._target_id(provider_id, "beta")
    _row, completed = lan_cases._persist_completed_scan(
        state,
        scan_id="scan-independent-evidence-tamper",
        observation=incomplete,
        scope=scope,
        observed_at=NOW + timedelta(seconds=1),
    )
    service = lan_cases._task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    service.import_observation(
        lan_cases._import_request(
            incomplete,
            completed,
            profile_revision=before_profile.revision,
            target_revisions=((beta_id, 0),),
        ),
        authenticated_owner_principal=lan_cases.OWNER,
    )
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    profile_lan = profile.profile.metadata["lan_discovery"]
    target_lan = target.target.metadata["lan_discovery"]
    scan_id = str(profile_lan["scan_id"] if pointer == "profile" else target_lan["scan_id"])
    lan_cases._rewrite_terminal_receipt(
        state,
        scan_id,
        mutate=lambda receipt: receipt.__setitem__("terminal_reason", "tampered"),
        recompute_digest=False,
    )

    with pytest.raises((ValueError, RuntimeError), match="receipt|evidence|authority|digest"):
        registry.resolve_lan_runtime_authority(
            target_id,
            clock=lambda: NOW + timedelta(seconds=1),
            interface_inventory_resolver=lambda: inventory,
        )


def test_registry_resolver_rejects_authenticated_current_target_projection_mismatch(
    tmp_path: Path,
) -> None:
    import test_lan_discovery_service as lan_cases

    (
        state,
        registry,
        _service,
        scope,
        _initial,
        _provider_id,
        target_id,
        inventory,
    ) = _enabled_lan_registry(tmp_path)
    target = registry.get_model_target(target_id)
    assert target is not None
    newer = lan_cases._positive_observation(scope=scope, models=("beta",))
    _row, completed = lan_cases._persist_completed_scan(
        state,
        scan_id="scan-current-target-projection-mismatch",
        observation=newer,
        scope=scope,
        observed_at=NOW + timedelta(seconds=1),
    )
    assert completed.terminal_receipt_digest is not None

    protected = json.loads(json.dumps(target.target.metadata["lan_discovery"]))
    protected.update(
        {
            "scan_id": "scan-current-target-projection-mismatch",
            "observation_digest": newer.observation_digest,
            "terminal_receipt_digest": completed.terminal_receipt_digest,
            "catalog_digest": newer.catalog_digest,
            "capability_digest": newer.capability_digest,
            "observed_at": "2026-08-01T12:00:01Z",
            "fresh_until": "2026-08-01T12:05:01Z",
        }
    )
    reviewed_material = lan_cases._review_material_digest(
        protected,
        trust_class="operator_confirmed",
        privacy_acknowledgement_digest=protected["privacy_acknowledgement_digest"],
        intended_roles=target.target.role_affinities,
        task_family_affinities=target.target.task_family_affinities,
    )
    protected["material_binding_digest"] = reviewed_material
    protected["reviewed_material_binding_digest"] = reviewed_material
    review_digest = lan_cases._digest(
        {
            "schema": "kestrel.lan.review.v1",
            "privacy_acknowledgement_digest": protected["privacy_acknowledgement_digest"],
            "expected_terminal_receipt_digest": protected[
                "review_evidence_terminal_receipt_digest"
            ],
            "expected_observation_digest": protected["review_evidence_observation_digest"],
            "pre_review_material_binding_digest": protected[
                "reviewed_from_material_binding_digest"
            ],
            "reviewed_material_binding_digest": reviewed_material,
            "expected_stale_reasons": protected["review_acknowledged_stale_reasons"],
            "stale_transition_terminal_receipt_digest": protected[
                "review_acknowledged_stale_transition_terminal_receipt_digest"
            ],
        }
    )
    protected["review_digest"] = review_digest
    protected["reviewed_runtime_interface_binding_digest"] = lan_cases._digest(
        {
            "schema": "kestrel.lan.reviewed-runtime-interface-binding.v1",
            "os_interface_identity": "darwin:en7",
            "source_address": "192.168.50.1",
            "interface_index": 7,
            "interface_id": protected["interface_id"],
            "confirmed_network": protected["confirmed_network"],
            "endpoint_binding_digest": protected["endpoint_binding_digest"],
            "endpoint_fingerprint": protected["endpoint_fingerprint"],
            "reviewed_material_binding_digest": reviewed_material,
            "review_digest": review_digest,
        }
    )
    metadata_json = json.dumps(
        {"lan_discovery": protected},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    with state._connect() as connection:
        connection.execute(
            "UPDATE routing_model_targets SET metadata_json = ? WHERE target_id = ?",
            (metadata_json, target_id),
        )
    before = _lan_authority_table_snapshot(state)
    inventory_calls = 0

    def inventory_resolver() -> CurrentLanInterfaceInventory:
        nonlocal inventory_calls
        inventory_calls += 1
        return inventory

    with pytest.raises(ValueError, match="target evidence projection"):
        registry.resolve_lan_runtime_authority(
            target_id,
            clock=lambda: NOW + timedelta(seconds=1),
            interface_inventory_resolver=inventory_resolver,
        )

    assert inventory_calls == 0
    assert _lan_authority_table_snapshot(state) == before


def _lan_authority_table_snapshot(state) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    tables = (
        "routing_provider_profiles",
        "routing_model_targets",
        "routing_lan_scans",
        "routing_lan_observations",
        "routing_lan_scan_events",
    )
    snapshot: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
    with state._connect() as connection:
        for table in tables:
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            snapshot.append((table, tuple(tuple(row) for row in rows)))
    return tuple(snapshot)


@pytest.mark.parametrize(
    ("case", "expected_inventory_calls"),
    (
        ("stale", 0),
        ("disabled", 0),
        ("unreviewed", 0),
        ("profile_tamper", 0),
        ("target_tamper", 0),
        ("expired", 0),
        ("scan_network_mismatch", 0),
        ("missing_interface", 1),
        ("duplicate_interface", 1),
        ("same_id_recreation", 1),
        ("changed_address", 1),
        ("changed_network", 1),
        ("changed_source", 1),
        ("changed_ifindex", 1),
    ),
)
def test_registry_resolver_hostile_durable_and_live_matrix_is_read_only(
    tmp_path: Path,
    case: str,
    expected_inventory_calls: int,
) -> None:
    import test_lan_discovery_service as lan_cases

    (
        state,
        registry,
        _service,
        _scope_value,
        _observation,
        provider_id,
        target_id,
        valid_inventory,
    ) = _enabled_lan_registry(tmp_path, review_target=case != "unreviewed")
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    clock_value = NOW

    def clock() -> datetime:
        return clock_value

    inventory = valid_inventory

    def rewrite_metadata(table: str, id_column: str, identity: str, mutate) -> None:
        with state._connect() as connection:
            row = connection.execute(
                f"SELECT metadata_json FROM {table} WHERE {id_column} = ?",
                (identity,),
            ).fetchone()
            assert row is not None
            metadata = json.loads(str(row[0]))
            mutate(metadata["lan_discovery"])
            connection.execute(
                f"UPDATE {table} SET metadata_json = ? WHERE {id_column} = ?",
                (
                    json.dumps(
                        metadata,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    identity,
                ),
            )

    if case == "stale":
        drift_scope = lan_cases._scope(
            network="192.168.50.0/30",
            os_identity="darwin:en7",
            display_name="Ambient adapter label after network drift",
            addresses=("192.168.50.1/29",),
        )
        drift = lan_cases._positive_observation(scope=drift_scope)
        _row, completed = lan_cases._persist_completed_scan(
            state,
            scan_id="scan-runtime-authority-stale",
            observation=drift,
            scope=drift_scope,
            observed_at=NOW + timedelta(seconds=1),
        )
        lan_cases._task5b_service(
            registry,
            clock=lambda: NOW + timedelta(seconds=1),
        ).import_observation(
            lan_cases._import_request(
                drift,
                completed,
                profile_revision=profile.revision,
                target_revisions=((target_id, target.revision),),
            ),
            authenticated_owner_principal=lan_cases.OWNER,
        )
        stale_profile = registry.get_provider_profile(provider_id)
        stale_target = registry.get_model_target(target_id)
        assert stale_profile is not None and stale_target is not None
        assert stale_profile.profile.enabled is False
        assert stale_target.target.enabled is False
        assert stale_profile.profile.metadata["lan_discovery"]["stale_reasons"] == [
            "network_changed"
        ]
        assert stale_target.target.metadata["lan_discovery"]["stale_reasons"] == ["network_changed"]
        clock_value = NOW + timedelta(seconds=1)
    elif case == "disabled":
        disable, _privacy, _material = lan_cases._exact_review_request(
            owner=lan_cases.OWNER,
            profile_revision=profile.revision,
            target_revision=target.revision,
            target_id=target_id,
            protected=target.target.metadata["lan_discovery"],
            enabled=False,
        )
        disabled = lan_cases._task5b_service(registry).review_lan_target(
            disable,
            authenticated_owner_principal=lan_cases.OWNER,
        )
        assert disabled.profile.profile.enabled is False
        assert disabled.target.target.enabled is False
    elif case == "unreviewed":
        assert profile.profile.enabled is False
        assert target.target.enabled is False
        assert target.target.metadata["lan_discovery"]["runtime_hardening"] is not None
        assert target.target.metadata["lan_discovery"]["reviewed"] is False
    elif case == "profile_tamper":
        rewrite_metadata(
            "routing_provider_profiles",
            "profile_id",
            provider_id,
            lambda protected: protected.__setitem__(
                "endpoint_fingerprint",
                "sha256:" + "a" * 64,
            ),
        )
    elif case == "target_tamper":
        rewrite_metadata(
            "routing_model_targets",
            "target_id",
            target_id,
            lambda protected: protected.__setitem__("review_digest", "sha256:" + "b" * 64),
        )
    elif case == "expired":
        clock_value = NOW + timedelta(minutes=6)
    elif case == "scan_network_mismatch":
        scan_id = str(target.target.metadata["lan_discovery"]["scan_id"])
        with state._connect() as connection:
            connection.execute("DROP TRIGGER trg_routing_lan_terminal_scan_update_immutable")
            connection.execute(
                "UPDATE routing_lan_scans SET network = ? WHERE scan_id = ?",
                ("192.168.50.0/30", scan_id),
            )
    elif case == "missing_interface":
        inventory = CurrentLanInterfaceInventory(())
    elif case == "duplicate_interface":
        inventory = CurrentLanInterfaceInventory(
            (
                CurrentLanInterfaceState("darwin:en7", 7, ("192.168.50.1/29",)),
                CurrentLanInterfaceState("darwin:en7", 8, ("192.168.50.1/29",)),
            )
        )
    elif case == "same_id_recreation":
        inventory = CurrentLanInterfaceInventory(
            (
                CurrentLanInterfaceState(
                    "darwin:en7",
                    7,
                    ("192.168.50.1/29", "10.0.0.7/24"),
                ),
            )
        )
    elif case == "changed_address":
        inventory = CurrentLanInterfaceInventory(
            (CurrentLanInterfaceState("darwin:en7", 7, ("192.168.50.6/29",)),)
        )
    elif case == "changed_network":
        inventory = CurrentLanInterfaceInventory(
            (CurrentLanInterfaceState("darwin:en7", 7, ("192.168.50.1/30",)),)
        )
    elif case == "changed_source":
        inventory = CurrentLanInterfaceInventory(
            (
                CurrentLanInterfaceState(
                    "darwin:en7",
                    7,
                    ("192.168.50.1/29", "192.168.50.3/30"),
                ),
            )
        )
    elif case == "changed_ifindex":
        inventory = CurrentLanInterfaceInventory(
            (CurrentLanInterfaceState("darwin:en7", 8, ("192.168.50.1/29",)),)
        )
    before = _lan_authority_table_snapshot(state)
    inventory_calls = 0

    def inventory_resolver() -> CurrentLanInterfaceInventory:
        nonlocal inventory_calls
        inventory_calls += 1
        return inventory

    with pytest.raises((ValueError, RuntimeError)):
        registry.resolve_lan_runtime_authority(
            target_id,
            clock=clock,
            interface_inventory_resolver=inventory_resolver,
        )

    assert inventory_calls == expected_inventory_calls
    assert _lan_authority_table_snapshot(state) == before


def test_enabled_review_binds_current_interface_without_serializing_raw_identity(
    tmp_path: Path,
) -> None:
    (
        state,
        registry,
        _service,
        _scope_value,
        _observation,
        _provider_id,
        target_id,
        inventory,
    ) = _enabled_lan_registry(tmp_path, interface_index=104729)
    target = registry.get_model_target(target_id)
    assert target is not None
    protected = target.target.metadata["lan_discovery"]
    serialized = json.dumps(target.to_public_payload(), sort_keys=True)
    durable_snapshot = repr(_lan_authority_table_snapshot(state))
    assert protected["reviewed_runtime_interface_binding_digest"] is not None
    for raw_value in (
        "darwin:en7",
        "192.168.50.1",
        "104729",
        "os_identity",
        "source_address",
        "interface_index",
    ):
        assert raw_value not in serialized
        assert raw_value not in durable_snapshot
    before = _lan_authority_table_snapshot(state)

    authority = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: NOW,
        interface_inventory_resolver=lambda: inventory,
    )

    assert authority.interface_index == 104729
    assert authority.os_interface_identity == "darwin:en7"
    assert _lan_authority_table_snapshot(state) == before


def test_registry_resolver_ignores_display_only_drift_and_enumerates_inventory_once(
    tmp_path: Path,
) -> None:
    (
        state,
        registry,
        _service,
        source_scope,
        _observation,
        _provider_id,
        target_id,
        inventory,
    ) = _enabled_lan_registry(tmp_path)
    assert source_scope.interface.display_name == "Ambient adapter label before rename"
    before = _lan_authority_table_snapshot(state)
    calls = 0

    def inventory_resolver() -> CurrentLanInterfaceInventory:
        nonlocal calls
        calls += 1
        return inventory

    authority = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: NOW,
        interface_inventory_resolver=inventory_resolver,
    )

    assert calls == 1
    assert authority.scope.interface.display_name == authority.os_interface_identity
    assert authority.scope.interface.display_name != source_scope.interface.display_name
    assert _lan_authority_table_snapshot(state) == before


def test_task7b_reviewed_manual_unusual_port_resolves_and_sends_directly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import test_lan_discovery_service as lan_cases

    from nested_memvid_agent.routing.ledger import RoutingLedger
    from nested_memvid_agent.state_store import AgentStateStore

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scope = lan_cases._manual_scope(
        network="192.168.50.8/32",
        os_identity="darwin:en7",
        addresses=("192.168.50.7/24",),
    )
    observation = lan_cases._manual_observation(
        scope=scope,
        address="192.168.50.8",
        port=5001,
    )
    _row, completed = lan_cases._persist_completed_manual_scan(
        state,
        scan_id="scan-task7b-runtime-manual",
        observation=observation,
        scope=scope,
    )
    registry = RoutingLedger(state)
    inventory = CurrentLanInterfaceInventory(
        (
            CurrentLanInterfaceState(
                os_identity="darwin:en7",
                interface_index=7,
                addresses=("192.168.50.7/24",),
            ),
        )
    )
    service = lan_cases._task5b_service(
        registry,
        interface_inventory_resolver=lambda: inventory,
    )
    provider_id = lan_cases._provider_id(observation.endpoint_binding_digest)
    target_id = lan_cases._target_id(provider_id, "alpha")
    service.import_observation(
        lan_cases._import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=lan_cases.OWNER,
    )
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    review, _privacy, _material = lan_cases._exact_review_request(
        owner=lan_cases.OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )
    service.review_lan_target(
        review,
        authenticated_owner_principal=lan_cases.OWNER,
    )

    authority = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: NOW,
        interface_inventory_resolver=lambda: inventory,
    )

    assert type(authority.endpoint) is _manual_endpoint_type()
    assert authority.endpoint.kind == "manual"
    assert authority.endpoint.port == 5001
    assert authority.scope.network == "192.168.50.8/32"
    assert authority.source_address == "192.168.50.7"
    assert authority.source_address not in authority.scope.active_hosts

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.setenv(name, "http://proxy.invalid:9999")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual runtime must not perform DNS")
        ),
    )
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    body = _transport(
        authority,
        sockets,
        resolver,
        inventory_resolver=lambda: inventory,
    ).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert body == b'{"choices":[{"message":{"content":"ok"}}]}'
    assert sockets.calls == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert socket_value.bound == ("192.168.50.7", 0)
    assert socket_value.connected == ("192.168.50.8", 5001)
    assert socket_value.socket_options == [(socket.IPPROTO_IP, 25, 7)]
    head = socket_value.sent.split(b"\r\n\r\n", 1)[0]
    lowered = head.lower()
    assert b"Host: 192.168.50.8:5001" in head
    assert b"authorization" not in lowered
    assert b"cookie" not in lowered
    assert b"proxy-" not in lowered
    assert b"secret" not in socket_value.sent
    assert len(sockets.calls) == 1


@pytest.mark.parametrize(
    (
        "interface_address",
        "network",
        "destination",
        "source",
        "source_sockaddr",
        "destination_sockaddr",
    ),
    (
        (
            "fd00::7/64",
            "fd00::8/128",
            "fd00::8",
            "fd00::7",
            ("fd00::7", 0, 0, 0),
            ("fd00::8", 5001, 0, 0),
        ),
        (
            "fe80::7/64",
            "fe80::8/128",
            "fe80::8",
            "fe80::7",
            ("fe80::7", 0, 0, 7),
            ("fe80::8", 5001, 0, 7),
        ),
    ),
)
def test_task7b_manual_ipv6_exact_scope_allows_attached_same_family_source(
    interface_address: str,
    network: str,
    destination: str,
    source: str,
    source_sockaddr: object,
    destination_sockaddr: object,
) -> None:
    scope = _scope(address=interface_address, network=network)
    authority = _manual_authority(
        scope=scope,
        address=destination,
        source_address=source,
    )
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    resolver = AuthorityResolver(authority, authority, authority)

    _transport(authority, sockets, resolver).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert authority.scope.network == network
    assert authority.source_address != authority.endpoint.address
    assert sockets.calls == [(socket.AF_INET6, socket.SOCK_STREAM)]
    assert socket_value.bound == source_sockaddr
    assert socket_value.connected == destination_sockaddr
    assert socket_value.socket_options == [(socket.IPPROTO_IPV6, 125, 7)]


def test_task7b_automatic_exact_host_scope_rejects_attached_source_outside_scope() -> None:
    scope = _scope(
        address="192.168.50.7/24",
        network="192.168.50.8/32",
    )
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 11434)
    sockets = SocketFactory(FakeSocket(_response()))

    assert type(endpoint) is ResolvedLanEndpoint
    assert endpoint.address == "192.168.50.8"
    assert endpoint.port == 11434
    with pytest.raises(ValueError, match="authority|canonical|network|source"):
        _authority(
            scope=scope,
            address=endpoint.address,
            port=endpoint.port,
            source_address="192.168.50.7",
        )

    assert sockets.calls == []


def test_task7b_manual_prefix_drift_rejects_when_source_remains_but_destination_detaches() -> None:
    authority = _manual_authority()
    drifted = CurrentLanInterfaceInventory(
        (
            CurrentLanInterfaceState(
                authority.os_interface_identity,
                authority.interface_index,
                ("192.168.50.7/32",),
            ),
        )
    )
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(
            authority,
            sockets,
            resolver,
            inventory_resolver=lambda: drifted,
        ).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.INTERFACE_CHANGED
    assert drifted.interfaces[0].addresses[0].split("/", 1)[0] == authority.source_address
    assert authority.endpoint.address == "192.168.50.8"
    assert sockets.calls == []


def test_task7b_manual_redirect_is_terminal_retry_free_and_closes_socket() -> None:
    authority = _manual_authority()
    sockets = SocketFactory(
        FakeSocket(
            _response(
                302,
                b"redirect",
                headers=(("Location", "https://example.com/v1/chat/completions"),),
            )
        ),
        FakeSocket(_response()),
    )
    resolver = AuthorityResolver(authority, authority, authority)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.REDIRECT_REJECTED
    assert len(sockets.calls) == 1
    assert sockets.sockets[0].closed is True


def test_task7b_automatic_kind_cannot_reuse_manual_unusual_port_authority() -> None:
    manual = _manual_authority()
    automatic_endpoint = object.__new__(ResolvedLanEndpoint)
    for field_name in ("interface_id", "address", "port"):
        object.__setattr__(
            automatic_endpoint,
            field_name,
            getattr(manual.endpoint, field_name),
        )
    forged = _unchecked_authority(manual, endpoint=automatic_endpoint)
    sockets = SocketFactory(FakeSocket(_response()))
    resolver = AuthorityResolver(forged)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(forged, sockets, resolver).request(
            forged,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_CHANGED
    assert sockets.calls == []


def _reviewed_manual_registry(
    tmp_path: Path,
    *,
    scan_id: str,
    network: str,
    interface_address: str,
    address: str,
    port: int,
):
    import test_lan_discovery_service as lan_cases

    from nested_memvid_agent.routing.lan_serialization import (
        load_authenticated_task4_observation,
    )
    from nested_memvid_agent.routing.ledger import RoutingLedger
    from nested_memvid_agent.state_store import AgentStateStore

    state = AgentStateStore(tmp_path / scan_id / "agent.db")
    scope = lan_cases._manual_scope(
        network=network,
        os_identity="darwin:en7",
        addresses=(interface_address,),
    )
    observation = lan_cases._manual_observation(
        scope=scope,
        address=address,
        port=port,
    )
    _row, completed = lan_cases._persist_completed_manual_scan(
        state,
        scan_id=scan_id,
        observation=observation,
        scope=scope,
    )
    assert completed.terminal_receipt_digest is not None
    with state._connect() as connection:
        authenticated = load_authenticated_task4_observation(
            connection,
            scan_id=completed.scan_id,
            endpoint_binding_digest=observation.endpoint_binding_digest,
            expected_terminal_receipt_digest=completed.terminal_receipt_digest,
            expected_observation_digest=observation.observation_digest,
            authenticated_owner_principal=lan_cases.OWNER,
        )

    registry = RoutingLedger(state)
    inventory = CurrentLanInterfaceInventory(
        (
            CurrentLanInterfaceState(
                os_identity="darwin:en7",
                interface_index=7,
                addresses=(interface_address,),
            ),
        )
    )
    service = lan_cases._task5b_service(
        registry,
        interface_inventory_resolver=lambda: inventory,
    )
    provider_id = lan_cases._provider_id(observation.endpoint_binding_digest)
    target_id = lan_cases._target_id(provider_id, "alpha")
    imported = service.import_observation(
        lan_cases._import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=lan_cases.OWNER,
    )
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    review, _privacy, _material = lan_cases._exact_review_request(
        owner=lan_cases.OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )
    reviewed = service.review_lan_target(
        review,
        authenticated_owner_principal=lan_cases.OWNER,
    )
    fresh_registry = RoutingLedger(state)
    authority = fresh_registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: NOW,
        interface_inventory_resolver=lambda: inventory,
    )
    return (
        state,
        fresh_registry,
        scope,
        observation,
        completed,
        authenticated,
        imported,
        reviewed,
        provider_id,
        target_id,
        inventory,
        authority,
    )


def _reviewed_known_port_manual_registry(tmp_path: Path):
    return _reviewed_manual_registry(
        tmp_path,
        scan_id="scan-task7b-known-port-manual",
        network="192.168.50.8/32",
        interface_address="192.168.50.8/24",
        address="192.168.50.8",
        port=11434,
    )


def test_task7b_manual_link_local_ipv6_receipt_review_and_fresh_registry_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _state,
        registry,
        scope,
        observation,
        completed,
        authenticated,
        imported,
        reviewed,
        provider_id,
        target_id,
        inventory,
        authority,
    ) = _reviewed_manual_registry(
        tmp_path,
        scan_id="scan-task7b-manual-ipv6-runtime",
        network="fe80::8/128",
        interface_address="fe80::7/64",
        address="fe80::8",
        port=5001,
    )

    assert completed.terminal_receipt is not None
    assert completed.terminal_receipt["limits"]["mode"] == "manual"
    assert completed.terminal_receipt["limits"]["exact_port"] == 5001
    assert authenticated.source == "manual"
    assert authenticated.confirmed_network == "fe80::8/128"
    assert type(authenticated.observation.endpoint) is _manual_endpoint_type()
    assert authenticated.observation.endpoint.kind == "manual"
    assert imported.profile is not None
    assert imported.profile.profile.enabled is False
    assert imported.profile.profile.secret_ref is None
    assert imported.profile.profile.base_url == "http://[fe80::8]:5001/v1"
    assert imported.targets[0].target.enabled is False
    assert reviewed.profile.profile.enabled is True
    assert reviewed.target.target.enabled is True
    assert type(authority.endpoint) is _manual_endpoint_type()
    assert authority.endpoint.kind == "manual"
    assert authority.scope == scope
    assert authority.scope.network == "fe80::8/128"
    assert authority.endpoint.address == "fe80::8"
    assert authority.source_address == "fe80::7"
    assert authority.source_address != authority.endpoint.address
    assert authority.os_interface_identity == "darwin:en7"
    assert authority.interface_index == 7
    assert authority.provider_profile_id == provider_id
    assert authority.reviewed_target_id == target_id
    assert observation.endpoint_binding_digest == authority.endpoint_binding_digest

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.setenv(name, "http://proxy.invalid:9999")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual IPv6 runtime must not perform DNS")
        ),
    )
    resolver_calls: list[str] = []

    def resolve_current(requested_target_id: str) -> LanRuntimeAuthority:
        resolver_calls.append(requested_target_id)
        return registry.resolve_lan_runtime_authority(
            requested_target_id,
            clock=lambda: NOW,
            interface_inventory_resolver=lambda: inventory,
        )

    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    body = _transport(
        authority,
        sockets,
        resolve_current,  # type: ignore[arg-type]
        inventory_resolver=lambda: inventory,
    ).request(
        authority,
        _request(),
        timeout_seconds=60,
        cancellation=Cancellation(),
    )

    assert body == b'{"choices":[{"message":{"content":"ok"}}]}'
    assert resolver_calls == [target_id] * 3
    assert sockets.calls == [(socket.AF_INET6, socket.SOCK_STREAM)]
    assert socket_value.bound == ("fe80::7", 0, 0, 7)
    assert socket_value.connected == ("fe80::8", 5001, 0, 7)
    assert socket_value.socket_options == [(socket.IPPROTO_IPV6, 125, 7)]
    head = socket_value.sent.split(b"\r\n\r\n", 1)[0]
    lowered = head.lower()
    assert head.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
    assert b"Host: [fe80::8]:5001" in head
    assert b"authorization" not in lowered
    assert b"cookie" not in lowered
    assert b"proxy-" not in lowered
    assert b"secret" not in socket_value.sent
    assert len(sockets.calls) == 1


def test_task7b_known_port_manual_receipt_review_registry_and_provider_stay_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nested_memvid_agent.llm.lan_openai_compatible_provider import (
        LanOpenAICompatibleProvider,
    )
    from nested_memvid_agent.runtime_models import LLMOptions

    (
        _state,
        registry,
        scope,
        observation,
        completed,
        authenticated,
        imported,
        reviewed,
        provider_id,
        target_id,
        inventory,
        authority,
    ) = _reviewed_known_port_manual_registry(tmp_path)

    assert completed.terminal_receipt is not None
    assert completed.terminal_receipt["limits"]["exact_port"] == 11434
    assert authenticated.source == "manual"
    assert type(authenticated.observation.endpoint) is _manual_endpoint_type()
    assert authenticated.observation.endpoint.kind == "manual"
    assert authenticated.observation.endpoint.port == 11434
    assert imported.profile is not None
    assert imported.profile.profile.enabled is False
    assert imported.targets[0].target.enabled is False
    imported_protected = imported.targets[0].target.metadata["lan_discovery"]
    assert imported_protected["observation_source"] == "manual"
    assert imported_protected["endpoint_kind"] == "manual"
    assert imported_protected["port"] == 11434
    assert reviewed.profile.profile.enabled is True
    assert reviewed.target.target.enabled is True
    assert type(authority.endpoint) is _manual_endpoint_type()
    assert authority.endpoint.kind == "manual"
    assert authority.endpoint.port == 11434
    assert authority.scope == scope
    assert authority.source_address == authority.endpoint.address
    assert authority.provider_profile_id == provider_id
    assert authority.reviewed_target_id == target_id
    assert observation.endpoint_binding_digest == authority.endpoint_binding_digest

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.setenv(name, "http://proxy.invalid:9999")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("known-port manual runtime must not perform DNS")
        ),
    )
    resolver_calls: list[str] = []

    def resolve_current(requested_target_id: str) -> LanRuntimeAuthority:
        resolver_calls.append(requested_target_id)
        return registry.resolve_lan_runtime_authority(
            requested_target_id,
            clock=lambda: NOW,
            interface_inventory_resolver=lambda: inventory,
        )

    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    direct = _transport(
        authority,
        sockets,
        resolve_current,  # type: ignore[arg-type]
        inventory_resolver=lambda: inventory,
    )
    provider = LanOpenAICompatibleProvider(
        model="alpha",
        base_url="http://192.168.50.8:11434/v1",
        authority=authority,
        authority_resolver=resolve_current,
        transport=direct,
        timeout_seconds=60,
        temperature=0.2,
        utc_clock=lambda: NOW,
    )

    result = provider.generate(
        [ChatMessage(role="user", content="hello")],
        [],
        LLMOptions(max_retries=99),
    )

    assert result.content == "ok"
    assert result.raw is None
    assert resolver_calls == [target_id] * 5
    assert sockets.calls == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert socket_value.bound == ("192.168.50.8", 0)
    assert socket_value.connected == ("192.168.50.8", 11434)
    head = socket_value.sent.split(b"\r\n\r\n", 1)[0]
    lowered = head.lower()
    assert b"Host: 192.168.50.8:11434" in head
    assert b"authorization" not in lowered
    assert b"cookie" not in lowered
    assert b"proxy-" not in lowered
    assert len(sockets.calls) == 1


def test_task7b_known_port_manual_authority_rejects_automatic_type_swap(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.llm.base import ProviderError
    from nested_memvid_agent.llm.lan_openai_compatible_provider import (
        LanOpenAICompatibleProvider,
    )

    (
        _state,
        _registry,
        scope,
        _observation,
        _completed,
        _authenticated,
        _imported,
        _reviewed,
        _provider_id,
        _target_id,
        _inventory_value,
        authority,
    ) = _reviewed_known_port_manual_registry(tmp_path)
    automatic_endpoint = ResolvedLanEndpoint.from_scope(
        scope,
        authority.endpoint.address,
        11434,
    )
    assert automatic_endpoint.kind == "automatic"
    assert authority.endpoint.kind == "manual"
    assert (
        automatic_endpoint.interface_id,
        automatic_endpoint.address,
        automatic_endpoint.port,
    ) == (
        authority.endpoint.interface_id,
        authority.endpoint.address,
        authority.endpoint.port,
    )
    forged = _unchecked_authority(authority, endpoint=automatic_endpoint)
    provider_resolver = AuthorityResolver(authority)
    sockets = SocketFactory(FakeSocket(_response()))
    transport_resolver = AuthorityResolver(authority, authority, authority)
    direct = _transport(authority, sockets, transport_resolver)

    with pytest.raises((TypeError, ValueError, ProviderError)):
        LanOpenAICompatibleProvider(
            model="alpha",
            base_url="http://192.168.50.8:11434/v1",
            authority=forged,
            authority_resolver=provider_resolver,
            transport=direct,
            timeout_seconds=60,
            temperature=0.2,
            utc_clock=lambda: NOW,
        )

    with pytest.raises(LanRuntimeTransportError) as direct_failure:
        direct.request(
            forged,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert direct_failure.value.failure is LanRuntimeTransportFailure.AUTHORITY_CHANGED
    assert sockets.calls == []
    assert sockets.pending[0].sent == b""
    assert sockets.pending[0].closed is False


@pytest.mark.parametrize(
    "swap_stage",
    ("post_connect", "pre_send"),
)
def test_task7b_known_port_manual_resolver_rejects_automatic_type_swap_before_send(
    tmp_path: Path,
    swap_stage: str,
) -> None:
    (
        _state,
        _registry,
        scope,
        _observation,
        _completed,
        _authenticated,
        _imported,
        _reviewed,
        _provider_id,
        target_id,
        _inventory_value,
        authority,
    ) = _reviewed_known_port_manual_registry(tmp_path)
    automatic_endpoint = ResolvedLanEndpoint.from_scope(
        scope,
        authority.endpoint.address,
        11434,
    )
    forged = _unchecked_authority(authority, endpoint=automatic_endpoint)
    resolved = (
        (authority, forged) if swap_stage == "post_connect" else (authority, authority, forged)
    )
    resolver = AuthorityResolver(*resolved)
    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)

    with pytest.raises(LanRuntimeTransportError) as raised:
        _transport(authority, sockets, resolver).request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_CHANGED
    assert resolver.calls == [target_id] * len(resolved)
    assert sockets.calls == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert socket_value.bound == ("192.168.50.8", 0)
    assert socket_value.connected == ("192.168.50.8", 11434)
    assert socket_value.sent == b""
    assert socket_value.closed is True


@pytest.mark.parametrize(
    ("resource", "field_name", "forged_value"),
    (
        ("profile", "observation_source", "active"),
        ("profile", "endpoint_kind", "automatic"),
        ("target", "observation_source", "active"),
        ("target", "endpoint_kind", "automatic"),
    ),
)
def test_task7b_post_review_one_sided_source_or_kind_tamper_fails_before_inventory(
    tmp_path: Path,
    resource: str,
    field_name: str,
    forged_value: str,
) -> None:
    from nested_memvid_agent.llm.base import ProviderError
    from nested_memvid_agent.llm.lan_openai_compatible_provider import (
        LanOpenAICompatibleProvider,
    )

    (
        state,
        registry,
        _scope_value,
        _observation,
        _completed,
        _authenticated,
        _imported,
        reviewed,
        provider_id,
        target_id,
        _inventory_value,
        authority,
    ) = _reviewed_known_port_manual_registry(tmp_path)
    assert reviewed.profile.profile.enabled is True
    assert reviewed.target.target.enabled is True
    table, id_column, resource_id, other_table, other_id_column, other_id = {
        "profile": (
            "routing_provider_profiles",
            "profile_id",
            provider_id,
            "routing_model_targets",
            "target_id",
            target_id,
        ),
        "target": (
            "routing_model_targets",
            "target_id",
            target_id,
            "routing_provider_profiles",
            "profile_id",
            provider_id,
        ),
    }[resource]
    with state._connect() as connection:
        row = connection.execute(
            f"SELECT metadata_json FROM {table} WHERE {id_column} = ?",
            (resource_id,),
        ).fetchone()
        other_row = connection.execute(
            f"SELECT metadata_json FROM {other_table} WHERE {other_id_column} = ?",
            (other_id,),
        ).fetchone()
        assert row is not None and other_row is not None
        metadata = json.loads(str(row[0]))
        other_metadata = json.loads(str(other_row[0]))
        assert metadata["lan_discovery"][field_name] == "manual"
        assert other_metadata["lan_discovery"][field_name] == "manual"
        metadata["lan_discovery"][field_name] = forged_value
        connection.execute(
            f"UPDATE {table} SET metadata_json = ? WHERE {id_column} = ?",
            (
                json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                resource_id,
            ),
        )
    before_attempt = _lan_authority_table_snapshot(state)
    inventory_calls = 0
    resolver_calls = 0

    def forbidden_inventory() -> CurrentLanInterfaceInventory:
        nonlocal inventory_calls
        inventory_calls += 1
        raise AssertionError("one-sided durable tamper must fail before inventory")

    def resolve_current(requested_target_id: str) -> LanRuntimeAuthority:
        nonlocal resolver_calls
        resolver_calls += 1
        return registry.resolve_lan_runtime_authority(
            requested_target_id,
            clock=lambda: NOW,
            interface_inventory_resolver=forbidden_inventory,
        )

    sockets = SocketFactory(FakeSocket(_response()))
    direct = _transport(
        authority,
        sockets,
        resolve_current,  # type: ignore[arg-type]
        inventory_resolver=forbidden_inventory,
    )

    with pytest.raises((TypeError, ValueError, ProviderError)):
        LanOpenAICompatibleProvider(
            model="alpha",
            base_url="http://192.168.50.8:11434/v1",
            authority=authority,
            authority_resolver=resolve_current,
            transport=direct,
            timeout_seconds=60,
            temperature=0.2,
            utc_clock=lambda: NOW,
        )

    assert resolver_calls == 1
    assert inventory_calls == 0
    assert sockets.calls == []
    assert _lan_authority_table_snapshot(state) == before_attempt
    with state._connect() as connection:
        other_after = connection.execute(
            f"SELECT metadata_json FROM {other_table} WHERE {other_id_column} = ?",
            (other_id,),
        ).fetchone()
    assert other_after is not None
    assert json.loads(str(other_after[0])) == other_metadata


def test_task7b_post_review_two_sided_manual_to_automatic_flip_fails_on_reopen(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.routing.ledger import RoutingLedger

    (
        state,
        _registry,
        _scope_value,
        _observation,
        _completed,
        _authenticated,
        _imported,
        reviewed,
        provider_id,
        target_id,
        _inventory_value,
        authority,
    ) = _reviewed_known_port_manual_registry(tmp_path)
    assert reviewed.profile.profile.enabled is True
    assert reviewed.target.target.enabled is True
    assert authority.endpoint.kind == "manual"
    assert authority.scope.network == "192.168.50.8/32"
    assert authority.source_address == authority.endpoint.address == "192.168.50.8"
    structurally_valid_automatic = ResolvedLanEndpoint.from_scope(
        authority.scope,
        authority.endpoint.address,
        authority.endpoint.port,
    )
    assert structurally_valid_automatic.kind == "automatic"

    original_digests: dict[str, dict[str, object]] = {}
    with state._connect() as connection:
        for table, id_column, resource_id in (
            ("routing_provider_profiles", "profile_id", provider_id),
            ("routing_model_targets", "target_id", target_id),
        ):
            row = connection.execute(
                f"SELECT metadata_json FROM {table} WHERE {id_column} = ?",
                (resource_id,),
            ).fetchone()
            assert row is not None
            metadata = json.loads(str(row[0]))
            protected = metadata["lan_discovery"]
            assert protected["observation_source"] == "manual"
            assert protected["endpoint_kind"] == "manual"
            original_digests[table] = {
                field: protected[field]
                for field in (
                    "endpoint_binding_digest",
                    "endpoint_fingerprint",
                    "terminal_receipt_digest",
                    "observation_digest",
                    "material_binding_digest",
                    "review_evidence_terminal_receipt_digest",
                    "review_evidence_observation_digest",
                    "reviewed_material_binding_digest",
                    "review_digest",
                )
                if field in protected
            }
            protected["observation_source"] = "active"
            protected["endpoint_kind"] = "automatic"
            connection.execute(
                f"UPDATE {table} SET metadata_json = ? WHERE {id_column} = ?",
                (
                    json.dumps(
                        metadata,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    resource_id,
                ),
            )

    durable_protected: dict[str, dict[str, object]] = {}
    with state._connect() as connection:
        for table, id_column, resource_id in (
            ("routing_provider_profiles", "profile_id", provider_id),
            ("routing_model_targets", "target_id", target_id),
        ):
            row = connection.execute(
                f"SELECT metadata_json FROM {table} WHERE {id_column} = ?",
                (resource_id,),
            ).fetchone()
            assert row is not None
            protected = json.loads(str(row[0]))["lan_discovery"]
            durable_protected[table] = protected
            assert protected["observation_source"] == "active"
            assert protected["endpoint_kind"] == "automatic"
            assert {
                field: protected[field] for field in original_digests[table]
            } == original_digests[table]
    profile_protected = durable_protected["routing_provider_profiles"]
    target_protected = durable_protected["routing_model_targets"]
    for field in (
        "observation_source",
        "endpoint_kind",
        "endpoint_binding_digest",
        "endpoint_fingerprint",
    ):
        assert profile_protected[field] == target_protected[field]

    before_attempt = _lan_authority_table_snapshot(state)
    reopened_registry = RoutingLedger(state)
    resolver_calls = 0
    inventory_calls = 0

    def forbidden_inventory() -> CurrentLanInterfaceInventory:
        nonlocal inventory_calls
        inventory_calls += 1
        raise AssertionError("two-sided durable flip must fail before inventory")

    def resolve_current(requested_target_id: str) -> LanRuntimeAuthority:
        nonlocal resolver_calls
        resolver_calls += 1
        return reopened_registry.resolve_lan_runtime_authority(
            requested_target_id,
            clock=lambda: NOW,
            interface_inventory_resolver=forbidden_inventory,
        )

    socket_value = FakeSocket(_response())
    sockets = SocketFactory(socket_value)
    direct = _transport(
        authority,
        sockets,
        resolve_current,  # type: ignore[arg-type]
        inventory_resolver=forbidden_inventory,
    )

    with pytest.raises(LanRuntimeTransportError) as raised:
        direct.request(
            authority,
            _request(),
            timeout_seconds=60,
            cancellation=Cancellation(),
        )

    assert raised.value.failure is LanRuntimeTransportFailure.AUTHORITY_CHANGED
    assert resolver_calls == 1
    assert inventory_calls == 0
    assert sockets.calls == []
    assert socket_value.sent == b""
    assert socket_value.closed is False
    assert _lan_authority_table_snapshot(state) == before_attempt
