"""Pinned, credential-free HTTP for explicitly confirmed private-LAN scopes."""

from __future__ import annotations

import errno
import ipaddress
import json
import math
import platform
import re
import socket
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from nested_memvid_agent.lan_discovery_models import (
    HTTP_PROBE_TIMEOUT_SECONDS,
    MAX_ACTIVE_HOSTS,
    MAX_PROBE_RESPONSE_BYTES,
    TCP_CONNECT_TIMEOUT_SECONDS,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import (
    PrivateScanScope,
    enumerate_private_interfaces,
)
from nested_memvid_agent.lan_mdns import _contains_transport_material
from nested_memvid_agent.security_boundary import redact_text

MAX_HTTP_HEADER_BYTES = 32 * 1024
MAX_HTTP_HEADER_LINES = 64
MAX_HTTP_HEADER_LINE_BYTES = 8 * 1024
MAX_HTTP_STATUS_LINE_BYTES = 4 * 1024
MAX_HTTP_CHUNK_LINE_BYTES = 1024
MAX_PROBE_MODEL_BYTES = 512
MAX_PROBE_REQUEST_BODY_BYTES = 4 * 1024

_DARWIN_IP_BOUND_IF = 25
_DARWIN_IPV6_BOUND_IF = 125
_ELIGIBLE_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_ELIGIBLE_IPV6_NETWORKS = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)
_CREDENTIAL_RE = re.compile(
    r"(?:\bauthorization\b|\bbearer\s|\bbasic\s|\bapi[ _-]?key\b|"
    r"\bpassword\b|\bsecret\b|\btoken\s*[=:]|\bsk-[a-z0-9_-]{8,})",
    re.IGNORECASE,
)
_CHUNK_SIZE_RE = re.compile(rb"[0-9A-Fa-f]+\Z")
_LOCALHOST_RE = re.compile(r"(?<![A-Za-z0-9-])localhost(?![A-Za-z0-9-])", re.IGNORECASE)


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...


class SocketLike(Protocol):
    def settimeout(self, value: float) -> None: ...

    def setsockopt(self, level: int, option: int, value: object) -> None: ...

    def bind(self, address: object) -> None: ...

    def connect(self, address: object) -> None: ...

    def sendall(self, payload: bytes) -> None: ...

    def recv(self, size: int) -> bytes: ...

    def close(self) -> None: ...


SocketFactory = Callable[[int, int], SocketLike]


class LanRequestRoute(StrEnum):
    OLLAMA_CATALOG = "ollama_catalog"
    OPENAI_CATALOG = "openai_catalog"
    OLLAMA_GENERATION = "ollama_generation"
    OPENAI_GENERATION = "openai_generation"

    @property
    def path(self) -> str:
        return {
            LanRequestRoute.OLLAMA_CATALOG: "/api/tags",
            LanRequestRoute.OPENAI_CATALOG: "/v1/models",
            LanRequestRoute.OLLAMA_GENERATION: "/api/generate",
            LanRequestRoute.OPENAI_GENERATION: "/v1/chat/completions",
        }[self]


class LanRequestProgress(StrEnum):
    NOT_STARTED = "not_started"
    CONNECTION_ATTEMPTED = "connection_attempted"
    REQUEST_SENT = "request_sent"


class LanTransportFailure(StrEnum):
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INTERFACE_CHANGED = "interface_changed"
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
    HTTP_CONNECT_FAILED = "http_connect_failed"


_PUBLIC_FAILURE_MESSAGES: dict[LanTransportFailure, str] = {
    LanTransportFailure.CANCELLED: "LAN probe was cancelled",
    LanTransportFailure.DEADLINE_EXCEEDED: "LAN scan deadline expired",
    LanTransportFailure.INTERFACE_CHANGED: "selected LAN interface changed",
    LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE: "selected interface cannot be pinned",
    LanTransportFailure.TCP_TIMEOUT: "LAN TCP check timed out",
    LanTransportFailure.TCP_REFUSED: "LAN TCP connection was refused",
    LanTransportFailure.TCP_UNREACHABLE: "LAN TCP destination was unreachable",
    LanTransportFailure.TCP_ERROR: "LAN TCP check failed",
    LanTransportFailure.HTTP_TIMEOUT: "LAN HTTP probe timed out",
    LanTransportFailure.HTTP_PROTOCOL_REJECTED: "LAN HTTP response framing was rejected",
    LanTransportFailure.REDIRECT_REJECTED: "LAN HTTP redirect was rejected",
    LanTransportFailure.RESPONSE_TOO_LARGE: "LAN HTTP response exceeded the byte limit",
    LanTransportFailure.UNSUPPORTED_CONTENT_ENCODING: "LAN HTTP content encoding was rejected",
    LanTransportFailure.HTTP_CONNECT_FAILED: "LAN HTTP connection failed",
}


class LanTransportError(RuntimeError):
    """A closed failure that deliberately discards hostile exception details."""

    def __init__(
        self,
        failure: LanTransportFailure,
        _untrusted_detail: str | None = None,
        *,
        request_progress: LanRequestProgress = LanRequestProgress.NOT_STARTED,
    ) -> None:
        if not isinstance(failure, LanTransportFailure):
            raise TypeError("LAN transport failure must use the closed enum")
        if type(request_progress) is not LanRequestProgress:
            raise TypeError("LAN request progress must use the closed enum")
        self.failure = failure
        self.request_progress = request_progress
        super().__init__(_PUBLIC_FAILURE_MESSAGES[failure])


@dataclass(frozen=True)
class LanHttpResponse:
    status_code: int
    body: bytes

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("LAN HTTP status must be a three-digit status code")
        if type(self.body) is not bytes or len(self.body) > MAX_PROBE_RESPONSE_BYTES:
            raise ValueError("LAN HTTP body is not bounded immutable bytes")


@dataclass(frozen=True)
class CurrentLanInterfaceState:
    os_identity: str
    interface_index: int
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class CurrentLanInterfaceInventory:
    interfaces: tuple[CurrentLanInterfaceState, ...]


@dataclass(frozen=True, init=False)
class AuthenticatedLanSource:
    interface_id: str
    os_identity: str
    interface_index: int
    source_address: str

    @classmethod
    def _from_authenticated(
        cls,
        *,
        interface_id: str,
        os_identity: str,
        interface_index: int,
        source_address: str,
    ) -> AuthenticatedLanSource:
        value = object.__new__(cls)
        object.__setattr__(value, "interface_id", interface_id)
        object.__setattr__(value, "os_identity", os_identity)
        object.__setattr__(value, "interface_index", interface_index)
        object.__setattr__(value, "source_address", source_address)
        return value


@dataclass(frozen=True, init=False)
class LanProbeModel:
    model_id: str

    @classmethod
    def from_catalog(cls, model_id: str) -> LanProbeModel:
        normalized = _validate_probe_model_id(model_id)
        value = object.__new__(cls)
        object.__setattr__(value, "model_id", normalized)
        return value


InterfaceInventoryResolver = Callable[[], CurrentLanInterfaceInventory]


def _format_numeric_http_authority(endpoint: ResolvedLanEndpoint) -> str:
    """Format only the authenticated literal endpoint used by this transport."""

    if type(endpoint) is not ResolvedLanEndpoint:
        raise TypeError("HTTP authority requires an authenticated LAN endpoint")
    address = endpoint.address
    port = endpoint.port
    if type(address) is not str or "%" in address:
        raise ValueError("LAN endpoint requires an unzoned literal IP address")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError("LAN endpoint requires a literal IP address") from None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("LAN endpoint requires a valid numeric port")
    literal = str(parsed)
    if isinstance(parsed, ipaddress.IPv6Address):
        return f"[{literal}]:{port}"
    return f"{literal}:{port}"


def _open_socket(family: int, kind: int) -> SocketLike:
    return cast(SocketLike, socket.socket(family, kind))


def authenticate_private_scan_scope(scope: PrivateScanScope) -> PrivateScanScope:
    """Rebuild a complete scope so forged dataclass fields carry no authority."""

    error = ValueError("LAN transport requires a canonical confirmed scope")
    if type(scope) is not PrivateScanScope:
        raise error
    try:
        interface = scope.interface
        canonical_interface = NetworkInterface.from_addresses(
            os_identity=interface.os_identity,
            display_name=interface.display_name,
            addresses=interface.addresses,
        )
        if canonical_interface != interface:
            raise error
        if not all(
            "%" not in item and _is_eligible_interface_address(item) for item in interface.addresses
        ):
            raise error
        canonical_scope = PrivateScanScope.from_request(canonical_interface, scope.network)
    except (AttributeError, TypeError, ValueError):
        raise error from None
    if scope != canonical_scope:
        raise error
    return canonical_scope


def authenticate_lan_source(
    scope: PrivateScanScope,
    endpoint: ResolvedLanEndpoint,
    inventory_resolver: InterfaceInventoryResolver | None = None,
) -> AuthenticatedLanSource:
    """Choose one unique longest-prefix source from a fresh full inventory."""

    canonical_scope = authenticate_private_scan_scope(scope)
    canonical_endpoint = _rebuild_endpoint(canonical_scope, endpoint)
    resolver = inventory_resolver or _resolve_current_interface_inventory
    try:
        inventory = resolver()
    except Exception:
        raise ValueError("selected LAN interface changed") from None
    if type(inventory) is not CurrentLanInterfaceInventory:
        raise ValueError("selected LAN interface changed")
    states = inventory.interfaces
    if type(states) is not tuple or not states or len(states) > MAX_ACTIVE_HOSTS:
        raise ValueError("selected LAN interface changed")

    identities: set[str] = set()
    indices: set[int] = set()
    selected: CurrentLanInterfaceState | None = None
    canonical_states: list[
        tuple[
            CurrentLanInterfaceState, tuple[ipaddress.IPv4Interface | ipaddress.IPv6Interface, ...]
        ]
    ] = []
    for state in states:
        if type(state) is not CurrentLanInterfaceState:
            raise ValueError("selected LAN interface changed")
        if (
            type(state.os_identity) is not str
            or not state.os_identity
            or state.os_identity in identities
            or isinstance(state.interface_index, bool)
            or not isinstance(state.interface_index, int)
            or state.interface_index <= 0
            or state.interface_index in indices
            or type(state.addresses) is not tuple
            or not state.addresses
            or len(state.addresses) > MAX_ACTIVE_HOSTS
        ):
            raise ValueError("selected LAN interface changed")
        identities.add(state.os_identity)
        indices.add(state.interface_index)
        try:
            attached_inventory = tuple(ipaddress.ip_interface(value) for value in state.addresses)
        except (TypeError, ValueError):
            raise ValueError("selected LAN interface changed") from None
        if len({str(item.ip) for item in attached_inventory}) != len(attached_inventory):
            raise ValueError("selected LAN interface changed")
        canonical_states.append((state, attached_inventory))
        if state.os_identity == canonical_scope.interface.os_identity:
            selected = state
    if selected is None:
        raise ValueError("selected LAN interface changed")
    try:
        current_selected = NetworkInterface.from_addresses(
            os_identity=selected.os_identity,
            display_name=canonical_scope.interface.display_name,
            addresses=selected.addresses,
        )
    except (TypeError, ValueError):
        raise ValueError("selected LAN interface changed") from None
    if current_selected.addresses != canonical_scope.interface.addresses or not all(
        _is_eligible_interface_address(item) for item in selected.addresses
    ):
        raise ValueError("selected LAN interface changed")

    destination = ipaddress.ip_address(canonical_endpoint.address)
    confirmed_network = ipaddress.ip_network(canonical_scope.network, strict=True)
    candidates: list[tuple[int, str]] = []
    for value in selected.addresses:
        selected_attached = ipaddress.ip_interface(value)
        if (
            isinstance(destination, ipaddress.IPv4Address)
            and isinstance(selected_attached, ipaddress.IPv4Interface)
            and isinstance(confirmed_network, ipaddress.IPv4Network)
            and confirmed_network.subnet_of(selected_attached.network)
            and destination in selected_attached.network
        ) or (
            isinstance(destination, ipaddress.IPv6Address)
            and isinstance(selected_attached, ipaddress.IPv6Interface)
            and isinstance(confirmed_network, ipaddress.IPv6Network)
            and confirmed_network.subnet_of(selected_attached.network)
            and destination in selected_attached.network
        ):
            candidates.append((selected_attached.network.prefixlen, str(selected_attached.ip)))
    if not candidates:
        raise ValueError("selected LAN interface changed")
    longest_prefix = max(item[0] for item in candidates)
    selected_literals = {literal for prefix, literal in candidates if prefix == longest_prefix}
    if len(selected_literals) != 1:
        raise ValueError("selected LAN interface changed")
    source_address = next(iter(selected_literals))

    for state, attached_values in canonical_states:
        if state.os_identity == selected.os_identity:
            continue
        if any(str(attached.ip) == source_address for attached in attached_values):
            raise ValueError("selected LAN interface changed")
    return AuthenticatedLanSource._from_authenticated(
        interface_id=canonical_scope.interface.interface_id,
        os_identity=selected.os_identity,
        interface_index=selected.interface_index,
        source_address=source_address,
    )


class DirectLanHttpTransport:
    """Direct sockets with endpoint reconstruction and fresh binding verification."""

    def __init__(
        self,
        *,
        socket_factory: SocketFactory = _open_socket,
        clock: Callable[[], float] = time.monotonic,
        inventory_resolver: InterfaceInventoryResolver | None = None,
        platform_name: str | None = None,
    ) -> None:
        self._socket_factory = socket_factory
        self._clock = clock
        self._inventory_resolver = inventory_resolver or _resolve_current_interface_inventory
        self._platform_name = platform_name or platform.system()

    def tcp_reachable(
        self,
        scope: PrivateScanScope,
        endpoint: ResolvedLanEndpoint,
        source: AuthenticatedLanSource,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> bool:
        canonical_endpoint, fresh_source = self._authority(scope, endpoint, source)
        family, source_sockaddr, destination_sockaddr = _socket_authority(
            canonical_endpoint, fresh_source
        )
        connection: SocketLike | None = None
        try:
            _remaining(
                deadline,
                cancellation,
                self._clock,
                deadline_failure=LanTransportFailure.DEADLINE_EXCEEDED,
            )
            connection = self._socket_factory(family, socket.SOCK_STREAM)
            _pin_socket(connection, family, fresh_source, self._platform_name)
            connection.settimeout(
                min(
                    TCP_CONNECT_TIMEOUT_SECONDS,
                    _remaining(
                        deadline,
                        cancellation,
                        self._clock,
                        deadline_failure=LanTransportFailure.DEADLINE_EXCEEDED,
                    ),
                )
            )
            connection.bind(source_sockaddr)
            connection.connect(destination_sockaddr)
            _check_completed_deadline(
                deadline,
                cancellation,
                self._clock,
                deadline_failure=LanTransportFailure.DEADLINE_EXCEEDED,
            )
            return True
        except LanTransportError:
            raise
        except TimeoutError:
            raise LanTransportError(LanTransportFailure.TCP_TIMEOUT) from None
        except ConnectionRefusedError:
            raise LanTransportError(LanTransportFailure.TCP_REFUSED) from None
        except OSError as exc:
            failure = (
                LanTransportFailure.TCP_UNREACHABLE
                if exc.errno in {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL}
                else LanTransportFailure.TCP_ERROR
            )
            raise LanTransportError(failure) from None
        finally:
            if connection is not None:
                connection.close()

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
    ) -> LanHttpResponse:
        if isinstance(deadline, bool) or not math.isfinite(deadline):
            raise LanTransportError(LanTransportFailure.HTTP_TIMEOUT)
        effective_deadline = min(deadline, self._clock() + HTTP_PROBE_TIMEOUT_SECONDS)
        canonical_endpoint, fresh_source = self._authority(scope, endpoint, source)
        if type(route) is not LanRequestRoute:
            raise ValueError("LAN HTTP route must use the closed allowlist")
        generation = route in {
            LanRequestRoute.OLLAMA_GENERATION,
            LanRequestRoute.OPENAI_GENERATION,
        }
        if generation != (type(model) is LanProbeModel):
            raise ValueError("LAN generation routes require one typed catalog model")
        if model is not None:
            try:
                canonical_model = LanProbeModel.from_catalog(model.model_id)
            except (AttributeError, TypeError, ValueError):
                raise ValueError("LAN generation model failed canonical validation") from None
            if model != canonical_model:
                raise ValueError("LAN generation model failed canonical validation")
            model = canonical_model
        family, source_sockaddr, destination_sockaddr = _socket_authority(
            canonical_endpoint, fresh_source
        )
        request = _request_bytes(canonical_endpoint, route, model)
        connection: SocketLike | None = None
        request_progress = LanRequestProgress.NOT_STARTED
        try:
            _remaining(
                effective_deadline,
                cancellation,
                self._clock,
                deadline_failure=LanTransportFailure.HTTP_TIMEOUT,
            )
            request_progress = LanRequestProgress.CONNECTION_ATTEMPTED
            connection = self._socket_factory(family, socket.SOCK_STREAM)
            _pin_socket(connection, family, fresh_source, self._platform_name)
            connection.settimeout(
                min(
                    TCP_CONNECT_TIMEOUT_SECONDS,
                    _remaining(
                        effective_deadline,
                        cancellation,
                        self._clock,
                        deadline_failure=LanTransportFailure.HTTP_TIMEOUT,
                    ),
                )
            )
            connection.bind(source_sockaddr)
            connection.connect(destination_sockaddr)
            _set_http_deadline(connection, effective_deadline, cancellation, self._clock)
            connection.sendall(request)
            request_progress = LanRequestProgress.REQUEST_SENT
            status, headers, initial_body = _read_response_head(
                connection, effective_deadline, cancellation, self._clock
            )
            if 100 <= status <= 199:
                raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
            if 300 <= status <= 399:
                raise LanTransportError(LanTransportFailure.REDIRECT_REJECTED)
            body = _read_response_body(
                connection,
                headers,
                initial_body,
                effective_deadline,
                cancellation,
                self._clock,
            )
            _check_completed_deadline(
                effective_deadline,
                cancellation,
                self._clock,
                deadline_failure=LanTransportFailure.HTTP_TIMEOUT,
            )
            return LanHttpResponse(status, body)
        except LanTransportError as exc:
            raise LanTransportError(
                exc.failure,
                request_progress=request_progress,
            ) from None
        except TimeoutError:
            raise LanTransportError(
                LanTransportFailure.HTTP_TIMEOUT,
                request_progress=request_progress,
            ) from None
        except OSError:
            raise LanTransportError(
                LanTransportFailure.HTTP_CONNECT_FAILED,
                request_progress=request_progress,
            ) from None
        finally:
            if connection is not None:
                connection.close()

    def _authority(
        self,
        scope: PrivateScanScope,
        endpoint: ResolvedLanEndpoint,
        source: AuthenticatedLanSource,
    ) -> tuple[ResolvedLanEndpoint, AuthenticatedLanSource]:
        canonical_scope = authenticate_private_scan_scope(scope)
        canonical_endpoint = _rebuild_endpoint(canonical_scope, endpoint)
        try:
            fresh_source = authenticate_lan_source(
                canonical_scope,
                canonical_endpoint,
                self._inventory_resolver,
            )
        except ValueError:
            raise LanTransportError(LanTransportFailure.INTERFACE_CHANGED) from None
        if type(source) is not AuthenticatedLanSource or source != fresh_source:
            raise LanTransportError(LanTransportFailure.INTERFACE_CHANGED)
        return canonical_endpoint, fresh_source


def _rebuild_endpoint(
    scope: PrivateScanScope,
    endpoint: ResolvedLanEndpoint,
) -> ResolvedLanEndpoint:
    error = ValueError("LAN transport requires an endpoint in the confirmed scope")
    if type(endpoint) is not ResolvedLanEndpoint:
        raise error
    try:
        if type(endpoint.address) is not str or "%" in endpoint.address:
            raise error
        rebuilt = ResolvedLanEndpoint.from_scope(scope, endpoint.address, endpoint.port)
    except (AttributeError, TypeError, ValueError):
        raise error from None
    if endpoint != rebuilt:
        raise error
    return rebuilt


def _request_bytes(
    endpoint: ResolvedLanEndpoint,
    route: LanRequestRoute,
    model: LanProbeModel | None,
) -> bytes:
    body = b""
    if route is LanRequestRoute.OLLAMA_CATALOG:
        method = "GET"
    elif route is LanRequestRoute.OPENAI_CATALOG:
        method = "GET"
    elif route is LanRequestRoute.OLLAMA_GENERATION:
        assert model is not None
        method = "POST"
        body = json.dumps(
            {
                "model": model.model_id,
                "options": {"num_predict": 8, "temperature": 0},
                "prompt": "Reply with OK only.",
                "stream": False,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        assert route is LanRequestRoute.OPENAI_GENERATION and model is not None
        method = "POST"
        body = json.dumps(
            {
                "max_tokens": 8,
                "messages": [{"content": "Reply with OK only.", "role": "user"}],
                "model": model.model_id,
                "stream": False,
                "temperature": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    if len(body) > MAX_PROBE_REQUEST_BODY_BYTES:
        raise ValueError("LAN probe request body exceeds its byte limit")
    headers = [
        f"{method} {route.path} HTTP/1.1",
        f"Host: {_format_numeric_http_authority(endpoint)}",
        "Accept: application/json",
        "Accept-Encoding: identity",
        "Connection: close",
        "User-Agent: Kestrel-LAN-Discovery/1",
    ]
    if body:
        headers.extend(("Content-Type: application/json", f"Content-Length: {len(body)}"))
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def _socket_authority(
    endpoint: ResolvedLanEndpoint,
    source: AuthenticatedLanSource,
) -> tuple[int, object, object]:
    destination = ipaddress.ip_address(endpoint.address)
    source_address = ipaddress.ip_address(source.source_address)
    if type(destination) is not type(source_address):
        raise ValueError("LAN source and destination address families differ")
    if isinstance(destination, ipaddress.IPv4Address):
        return socket.AF_INET, (str(source_address), 0), (str(destination), endpoint.port)
    zone = (
        source.interface_index if destination.is_link_local or source_address.is_link_local else 0
    )
    return (
        socket.AF_INET6,
        (str(source_address), 0, 0, zone),
        (str(destination), endpoint.port, 0, zone),
    )


def _pin_socket(
    connection: SocketLike,
    family: int,
    source: AuthenticatedLanSource,
    platform_name: str,
) -> None:
    try:
        if platform_name == "Darwin":
            level = socket.IPPROTO_IP if family == socket.AF_INET else socket.IPPROTO_IPV6
            option = _DARWIN_IP_BOUND_IF if family == socket.AF_INET else _DARWIN_IPV6_BOUND_IF
            connection.setsockopt(level, option, source.interface_index)
            return
        if platform_name == "Linux" and hasattr(socket, "SO_BINDTODEVICE"):
            interface_name = _interface_name(source.os_identity, platform_name)
            connection.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BINDTODEVICE,
                interface_name.encode("utf-8") + b"\0",
            )
            return
    except OSError:
        raise LanTransportError(LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE) from None
    raise LanTransportError(LanTransportFailure.INTERFACE_PINNING_UNAVAILABLE)


def _read_response_head(
    connection: SocketLike,
    deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
) -> tuple[int, dict[str, str], bytes]:
    buffer = bytearray()
    delimiter = b"\r\n\r\n"
    while delimiter not in buffer:
        if len(buffer) > MAX_HTTP_HEADER_BYTES:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        _set_http_deadline(connection, deadline, cancellation, clock)
        chunk = connection.recv(min(4096, MAX_HTTP_HEADER_BYTES + len(delimiter) - len(buffer)))
        _check_completed_deadline(
            deadline,
            cancellation,
            clock,
            deadline_failure=LanTransportFailure.HTTP_TIMEOUT,
        )
        if not chunk:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        buffer.extend(chunk)
    head, initial_body = bytes(buffer).split(delimiter, 1)
    if len(head) > MAX_HTTP_HEADER_BYTES:
        raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
    lines = head.split(b"\r\n")
    if (
        not lines
        or len(lines) - 1 > MAX_HTTP_HEADER_LINES
        or not lines[0]
        or len(lines[0]) > MAX_HTTP_STATUS_LINE_BYTES
        or any(not line or len(line) > MAX_HTTP_HEADER_LINE_BYTES for line in lines[1:])
    ):
        raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
    status_parts = lines[0].split(b" ", 2)
    if (
        len(status_parts) < 2
        or status_parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"}
        or len(status_parts[1]) != 3
        or not status_parts[1].isdigit()
        or any(byte < 0x20 or byte == 0x7F for byte in lines[0])
    ):
        raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
    status = int(status_parts[1])
    if not 100 <= status <= 599:
        raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
    headers: dict[str, str] = {}
    for raw_line in lines[1:]:
        if raw_line[:1] in {b" ", b"\t"} or b":" not in raw_line:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        raw_name, raw_value = raw_line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("ascii").strip()
        except UnicodeDecodeError:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED) from None
        if (
            not name
            or not all(character.isalnum() or character == "-" for character in name)
            or name in headers
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        headers[name] = value
    return status, headers, initial_body


def _read_response_body(
    connection: SocketLike,
    headers: dict[str, str],
    initial: bytes,
    deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
) -> bytes:
    transfer_encoding = headers.get("transfer-encoding")
    content_length = headers.get("content-length")
    content_encoding = headers.get("content-encoding")
    if content_encoding is not None and content_encoding.lower() != "identity":
        raise LanTransportError(LanTransportFailure.UNSUPPORTED_CONTENT_ENCODING)
    if transfer_encoding is not None and content_length is not None:
        raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
    reader = _BoundedSocketReader(connection, initial, deadline, cancellation, clock)
    if transfer_encoding is not None:
        if transfer_encoding.lower() != "chunked":
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        return _read_chunked_body(reader)
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        canonical_length = content_length.lstrip("0") or "0"
        if len(canonical_length) > len(str(MAX_PROBE_RESPONSE_BYTES)):
            raise LanTransportError(LanTransportFailure.RESPONSE_TOO_LARGE)
        try:
            length = int(canonical_length)
        except ValueError:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED) from None
        if length > MAX_PROBE_RESPONSE_BYTES:
            raise LanTransportError(LanTransportFailure.RESPONSE_TOO_LARGE)
        body = reader.read_exact(length)
        if reader.buffered_bytes:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        return body
    return reader.read_until_close(MAX_PROBE_RESPONSE_BYTES)


class _BoundedSocketReader:
    def __init__(
        self,
        connection: SocketLike,
        initial: bytes,
        deadline: float,
        cancellation: CancellationToken,
        clock: Callable[[], float],
    ) -> None:
        self._connection = connection
        self._buffer = bytearray(initial)
        self._deadline = deadline
        self._cancellation = cancellation
        self._clock = clock

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def read_exact(self, length: int) -> bytes:
        while len(self._buffer) < length:
            self._receive(min(65536, length - len(self._buffer)))
        payload = bytes(self._buffer[:length])
        del self._buffer[:length]
        return payload

    def read_line(self, max_bytes: int) -> bytes:
        while b"\r\n" not in self._buffer:
            if len(self._buffer) > max_bytes:
                raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
            self._receive(min(4096, max_bytes + 2 - len(self._buffer)))
        line, remainder = bytes(self._buffer).split(b"\r\n", 1)
        self._buffer = bytearray(remainder)
        if len(line) > max_bytes:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        return line

    def read_until_close(self, max_bytes: int) -> bytes:
        while True:
            if len(self._buffer) > max_bytes:
                raise LanTransportError(LanTransportFailure.RESPONSE_TOO_LARGE)
            chunk = self._receive(min(65536, max_bytes + 1 - len(self._buffer)), eof_ok=True)
            if not chunk:
                return bytes(self._buffer)

    def _receive(self, size: int, *, eof_ok: bool = False) -> bytes:
        _set_http_deadline(self._connection, self._deadline, self._cancellation, self._clock)
        chunk = self._connection.recv(max(1, size))
        _check_completed_deadline(
            self._deadline,
            self._cancellation,
            self._clock,
            deadline_failure=LanTransportFailure.HTTP_TIMEOUT,
        )
        if not chunk and not eof_ok:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        self._buffer.extend(chunk)
        return chunk


def _read_chunked_body(reader: _BoundedSocketReader) -> bytes:
    result = bytearray()
    while True:
        raw_size = reader.read_line(MAX_HTTP_CHUNK_LINE_BYTES)
        if _CHUNK_SIZE_RE.fullmatch(raw_size) is None:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
        try:
            size = int(raw_size, 16)
        except ValueError:
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED) from None
        if size < 0 or len(result) + size > MAX_PROBE_RESPONSE_BYTES:
            raise LanTransportError(LanTransportFailure.RESPONSE_TOO_LARGE)
        if size == 0:
            if reader.read_line(MAX_HTTP_HEADER_LINE_BYTES) != b"":
                raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
            if reader.buffered_bytes:
                raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)
            return bytes(result)
        result.extend(reader.read_exact(size))
        if reader.read_exact(2) != b"\r\n":
            raise LanTransportError(LanTransportFailure.HTTP_PROTOCOL_REJECTED)


def _remaining(
    deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
    *,
    deadline_failure: LanTransportFailure,
) -> float:
    if cancellation.is_cancelled():
        raise LanTransportError(LanTransportFailure.CANCELLED)
    if isinstance(deadline, bool) or not math.isfinite(deadline):
        raise LanTransportError(deadline_failure)
    remaining = deadline - clock()
    if remaining <= 0:
        raise LanTransportError(deadline_failure)
    return min(remaining, HTTP_PROBE_TIMEOUT_SECONDS)


def _check_completed_deadline(
    deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
    *,
    deadline_failure: LanTransportFailure,
) -> None:
    if cancellation.is_cancelled():
        raise LanTransportError(LanTransportFailure.CANCELLED)
    if isinstance(deadline, bool) or not math.isfinite(deadline) or clock() > deadline:
        raise LanTransportError(deadline_failure)


def _set_http_deadline(
    connection: SocketLike,
    deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
) -> None:
    connection.settimeout(
        _remaining(
            deadline,
            cancellation,
            clock,
            deadline_failure=LanTransportFailure.HTTP_TIMEOUT,
        )
    )


def _validate_probe_model_id(value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("LAN probe model must be canonical text")
    if len(value.encode("utf-8")) > MAX_PROBE_MODEL_BYTES:
        raise ValueError("LAN probe model exceeds its byte limit")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("LAN probe model must use NFC normalization")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("LAN probe model contains control material")
    contains_transport = _contains_transport_material(
        value,
        allowed_dotted_value=None,
        allow_numeric_version=False,
    )
    canonical_name_tag = _is_canonical_model_name_tag(value)
    if (
        _LOCALHOST_RE.search(value)
        or ((":" in value or contains_transport) and not canonical_name_tag)
        or _CREDENTIAL_RE.search(value)
        or redact_text(value) != value
    ):
        raise ValueError("LAN probe model contains transport or credential material")
    return value


def _is_canonical_model_name_tag(value: str) -> bool:
    if value.count(":") != 1:
        return False
    name, tag = value.split(":", 1)
    if (
        not name
        or not tag
        or name.casefold() == "localhost"
        or tag.isdecimal()
        or re.search(r"[A-Za-z]", tag) is None
    ):
        return False
    return not _contains_transport_material(
        name,
        allowed_dotted_value=None,
        allow_numeric_version=False,
    ) and not _contains_transport_material(
        tag,
        allowed_dotted_value=None,
        allow_numeric_version=False,
    )


def _is_eligible_interface_address(value: str) -> bool:
    attached = ipaddress.ip_interface(value)
    address = attached.ip
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved:
        return False
    if isinstance(attached, ipaddress.IPv4Interface):
        return any(attached.network.subnet_of(network) for network in _ELIGIBLE_IPV4_NETWORKS)
    return any(attached.network.subnet_of(network) for network in _ELIGIBLE_IPV6_NETWORKS)


def _interface_name(os_identity: str, platform_name: str) -> str:
    prefix = f"{platform_name.lower()}:"
    if not os_identity.startswith(prefix) or not os_identity[len(prefix) :]:
        raise ValueError("selected LAN interface identity is invalid")
    return os_identity[len(prefix) :]


def _resolve_current_interface_inventory() -> CurrentLanInterfaceInventory:
    platform_name = platform.system()
    states: list[CurrentLanInterfaceState] = []
    for interface in enumerate_private_interfaces():
        name = _interface_name(interface.os_identity, platform_name)
        try:
            index = socket.if_nametoindex(name)
            if index <= 0 or socket.if_indextoname(index) != name:
                raise ValueError("selected LAN interface identity is invalid")
        except OSError:
            raise ValueError("selected LAN interface identity is invalid") from None
        states.append(
            CurrentLanInterfaceState(
                os_identity=interface.os_identity,
                interface_index=index,
                addresses=interface.addresses,
            )
        )
    return CurrentLanInterfaceInventory(tuple(states))
