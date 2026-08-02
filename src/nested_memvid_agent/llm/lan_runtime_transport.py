"""Direct, credential-free runtime transport for reviewed LAN model targets."""

from __future__ import annotations

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
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from nested_memvid_agent.lan_http_transport import (
    MAX_HTTP_CHUNK_LINE_BYTES,
    MAX_HTTP_HEADER_BYTES,
    MAX_HTTP_HEADER_LINE_BYTES,
    MAX_HTTP_HEADER_LINES,
    MAX_HTTP_STATUS_LINE_BYTES,
    AuthenticatedLanSource,
    InterfaceInventoryResolver,
    SocketFactory,
    SocketLike,
    authenticate_lan_source,
)
from nested_memvid_agent.lan_runtime_authority import (
    LanRuntimeAuthority,
    LanRuntimeAuthorityResolver,
    authenticate_lan_runtime_authority,
)
from nested_memvid_agent.runtime_models import ChatMessage

MAX_LAN_RUNTIME_REQUEST_BYTES = 1024 * 1024
MAX_LAN_RUNTIME_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_LAN_RUNTIME_TIMEOUT_SECONDS = 120

_DARWIN_IP_BOUND_IF = 25
_DARWIN_IPV6_BOUND_IF = 125
_CHUNK_SIZE_RE = re.compile(rb"[0-9A-Fa-f]+\Z")
_ORDINARY_ROLES = frozenset({"system", "user", "assistant"})


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...


class LanRuntimeTransportFailure(StrEnum):
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    AUTHORITY_EXPIRED = "authority_expired"
    AUTHORITY_CHANGED = "authority_changed"
    INTERFACE_CHANGED = "interface_changed"
    INTERFACE_PINNING_UNAVAILABLE = "interface_pinning_unavailable"
    REQUEST_TOO_LARGE = "request_too_large"
    RESPONSE_TOO_LARGE = "response_too_large"
    HTTP_CONNECT_FAILED = "http_connect_failed"
    HTTP_TIMEOUT = "http_timeout"
    HTTP_PROTOCOL_REJECTED = "http_protocol_rejected"
    HTTP_STATUS_REJECTED = "http_status_rejected"
    REDIRECT_REJECTED = "redirect_rejected"
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"


_PUBLIC_FAILURE_MESSAGES: dict[LanRuntimeTransportFailure, str] = {
    LanRuntimeTransportFailure.CANCELLED: "LAN runtime request was cancelled",
    LanRuntimeTransportFailure.DEADLINE_EXCEEDED: "LAN runtime deadline expired",
    LanRuntimeTransportFailure.AUTHORITY_EXPIRED: "LAN runtime authority expired",
    LanRuntimeTransportFailure.AUTHORITY_CHANGED: "LAN runtime authority changed",
    LanRuntimeTransportFailure.INTERFACE_CHANGED: "selected LAN interface changed",
    LanRuntimeTransportFailure.INTERFACE_PINNING_UNAVAILABLE: (
        "selected interface cannot be pinned"
    ),
    LanRuntimeTransportFailure.REQUEST_TOO_LARGE: ("LAN runtime request exceeded the byte limit"),
    LanRuntimeTransportFailure.RESPONSE_TOO_LARGE: ("LAN runtime response exceeded the byte limit"),
    LanRuntimeTransportFailure.HTTP_CONNECT_FAILED: "LAN runtime connection failed",
    LanRuntimeTransportFailure.HTTP_TIMEOUT: "LAN runtime request timed out",
    LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED: ("LAN HTTP response framing was rejected"),
    LanRuntimeTransportFailure.HTTP_STATUS_REJECTED: "LAN HTTP status was rejected",
    LanRuntimeTransportFailure.REDIRECT_REJECTED: "LAN HTTP redirect was rejected",
    LanRuntimeTransportFailure.UNSUPPORTED_CONTENT_ENCODING: (
        "LAN HTTP content encoding was rejected"
    ),
}


class LanRuntimeTransportError(RuntimeError):
    """Closed runtime failure that never reflects hostile exception detail."""

    def __init__(
        self,
        failure: LanRuntimeTransportFailure,
        _untrusted_detail: str | None = None,
    ) -> None:
        if type(failure) is not LanRuntimeTransportFailure:
            raise TypeError("LAN runtime failure must use the closed enum")
        self.failure = failure
        super().__init__(_PUBLIC_FAILURE_MESSAGES[failure])


@dataclass(frozen=True, slots=True)
class LanRuntimeChatRequest:
    """The only request shape accepted by the direct LAN runtime transport."""

    model_id: str
    messages: tuple[ChatMessage, ...]
    temperature: float | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "model": self.model_id,
            "messages": [
                {"role": message.role, "content": message.content} for message in self.messages
            ],
            "stream": False,
            "temperature": self.temperature,
        }


class DirectLanRuntimeTransport:
    """Own a direct raw-socket request path for one revalidated LAN authority."""

    def __init__(
        self,
        *,
        authority_resolver: LanRuntimeAuthorityResolver,
        socket_factory: SocketFactory | None = None,
        inventory_resolver: InterfaceInventoryResolver | None = None,
        utc_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        platform_name: str | None = None,
    ) -> None:
        if not callable(authority_resolver):
            raise TypeError("LAN runtime authority resolver must be callable")
        if socket_factory is not None and not callable(socket_factory):
            raise TypeError("LAN runtime socket factory must be callable")
        if inventory_resolver is not None and not callable(inventory_resolver):
            raise TypeError("LAN runtime inventory resolver must be callable")
        if utc_clock is not None and not callable(utc_clock):
            raise TypeError("LAN runtime UTC clock must be callable")
        if monotonic_clock is not None and not callable(monotonic_clock):
            raise TypeError("LAN runtime monotonic clock must be callable")
        active_platform = platform.system() if platform_name is None else platform_name
        if type(active_platform) is not str or not active_platform:
            raise ValueError("LAN runtime platform name is invalid")
        self._authority_resolver = authority_resolver
        self._socket_factory = socket_factory or _open_socket
        self._inventory_resolver = inventory_resolver
        self._utc_clock = utc_clock or _utc_now
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._platform_name = active_platform

    def request(
        self,
        authority: LanRuntimeAuthority,
        request: LanRuntimeChatRequest,
        *,
        timeout_seconds: float,
        cancellation: CancellationToken,
    ) -> bytes:
        try:
            assigned = authenticate_lan_runtime_authority(authority)
        except Exception:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_CHANGED) from None
        body = _canonical_request_body(assigned, request)
        request_bytes = _request_bytes(assigned, body)
        _require_not_cancelled(cancellation)
        requested_timeout = _validate_requested_timeout(timeout_seconds)

        initial_now = _read_utc_clock(self._utc_clock)
        original_freshness = (assigned.fresh_until_datetime - initial_now).total_seconds()
        if original_freshness <= 0:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_EXPIRED)
        start = _read_monotonic_clock(self._monotonic_clock)
        deadline = start + min(
            requested_timeout,
            float(MAX_LAN_RUNTIME_TIMEOUT_SECONDS),
            original_freshness,
        )

        current = self._resolve_current(assigned, cancellation, deadline)
        source = self._authenticate_interface(current)
        family, source_sockaddr, destination_sockaddr = _socket_authority(current)

        connection: SocketLike | None = None
        try:
            _remaining(deadline, cancellation, self._monotonic_clock)
            connection = self._socket_factory(family, socket.SOCK_STREAM)
            _pin_socket(connection, family, current, self._platform_name)
            _set_deadline(connection, deadline, cancellation, self._monotonic_clock)
            connection.bind(source_sockaddr)
            connection.connect(destination_sockaddr)

            current = self._resolve_current(current, cancellation, deadline)
            current = self._resolve_current(current, cancellation, deadline)
            if (
                source.source_address != current.source_address
                or source.os_identity != current.os_interface_identity
                or source.interface_index != current.interface_index
                or source.interface_id != current.scope.interface.interface_id
            ):
                raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_CHANGED)
            _set_deadline(connection, deadline, cancellation, self._monotonic_clock)
            connection.sendall(request_bytes)

            status, headers, initial_body = _read_response_head(
                connection,
                deadline,
                cancellation,
                self._monotonic_clock,
            )
            if 100 <= status <= 199:
                raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
            if 300 <= status <= 399:
                raise LanRuntimeTransportError(LanRuntimeTransportFailure.REDIRECT_REJECTED)
            if status != 200:
                raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_STATUS_REJECTED)
            response_body = _read_response_body(
                connection,
                headers,
                initial_body,
                deadline,
                cancellation,
                self._monotonic_clock,
            )
            _check_completed_deadline(deadline, cancellation, self._monotonic_clock)
            return response_body
        except LanRuntimeTransportError:
            raise
        except TimeoutError:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_TIMEOUT) from None
        except OSError:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_CONNECT_FAILED) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _resolve_current(
        self,
        previous: LanRuntimeAuthority,
        cancellation: CancellationToken,
        deadline: float,
    ) -> LanRuntimeAuthority:
        try:
            candidate = self._authority_resolver(previous.reviewed_target_id)
            current = authenticate_lan_runtime_authority(candidate)
        except Exception:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_CHANGED) from None
        if not _same_binding(previous, current):
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_CHANGED)
        if current.fresh_until_datetime < previous.fresh_until_datetime:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_CHANGED)
        _require_not_cancelled(cancellation)
        if current.fresh_until_datetime <= _read_utc_clock(self._utc_clock):
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_EXPIRED)
        _remaining(deadline, cancellation, self._monotonic_clock)
        return current

    def _authenticate_interface(
        self,
        authority: LanRuntimeAuthority,
    ) -> AuthenticatedLanSource:
        try:
            source = authenticate_lan_source(
                authority.scope,
                authority.endpoint,
                self._inventory_resolver,
            )
        except Exception:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.INTERFACE_CHANGED) from None
        if (
            source.source_address != authority.source_address
            or source.os_identity != authority.os_interface_identity
            or source.interface_index != authority.interface_index
            or source.interface_id != authority.scope.interface.interface_id
        ):
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.INTERFACE_CHANGED)
        return source


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _open_socket(family: int, kind: int) -> SocketLike:
    return cast(SocketLike, socket.socket(family, kind))


def _validate_requested_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.DEADLINE_EXCEEDED)
    return float(value)


def _read_utc_clock(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_EXPIRED) from None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.AUTHORITY_EXPIRED)
    return value.astimezone(UTC)


def _read_monotonic_clock(clock: Callable[[], float]) -> float:
    try:
        value = clock()
    except Exception:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.DEADLINE_EXCEEDED) from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.DEADLINE_EXCEEDED)
    return float(value)


def _same_binding(left: LanRuntimeAuthority, right: LanRuntimeAuthority) -> bool:
    return (
        left.scope == right.scope
        and left.endpoint == right.endpoint
        and left.source_address == right.source_address
        and left.os_interface_identity == right.os_interface_identity
        and left.interface_index == right.interface_index
        and left.provider_profile_id == right.provider_profile_id
        and left.reviewed_target_id == right.reviewed_target_id
        and left.model_id == right.model_id
        and left.api_shape == right.api_shape
        and left.runtime_adapter == right.runtime_adapter
        and left.runtime_hardening_version == right.runtime_hardening_version
        and left.endpoint_binding_digest == right.endpoint_binding_digest
        and left.endpoint_fingerprint == right.endpoint_fingerprint
        and left.reviewed_material_binding_digest == right.reviewed_material_binding_digest
        and left.review_digest == right.review_digest
    )


def _canonical_request_body(
    authority: LanRuntimeAuthority,
    request: LanRuntimeChatRequest,
) -> bytes:
    if type(request) is not LanRuntimeChatRequest:
        raise TypeError("LAN runtime request must use the exact internal type")
    if type(request.model_id) is not str or request.model_id != authority.model_id:
        raise ValueError("LAN runtime request model does not match authority")
    if type(request.messages) is not tuple or not request.messages:
        raise ValueError("LAN runtime request requires ordinary messages")
    canonical_messages: list[dict[str, str]] = []
    for message in request.messages:
        if type(message) is not ChatMessage:
            raise TypeError("LAN runtime messages must use the exact internal type")
        if type(message.role) is not str or message.role not in _ORDINARY_ROLES:
            raise ValueError("LAN runtime message role is not allowed")
        if type(message.content) is not str or _contains_forbidden_text(message.content):
            raise ValueError("LAN runtime message content is not canonical")
        if (
            message.name is not None
            or message.tool_call_id is not None
            or type(message.tool_calls) is not tuple
            or message.tool_calls
        ):
            raise ValueError("LAN runtime messages cannot carry tool metadata")
        canonical_messages.append({"role": message.role, "content": message.content})
    temperature = request.temperature
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
    ):
        raise ValueError("LAN runtime temperature must be finite numeric data")
    try:
        encoded = json.dumps(
            {
                "model": request.model_id,
                "messages": canonical_messages,
                "stream": False,
                "temperature": temperature,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("LAN runtime request is not canonical JSON") from None
    if len(encoded) > MAX_LAN_RUNTIME_REQUEST_BYTES:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.REQUEST_TOO_LARGE)
    return encoded


def _contains_forbidden_text(value: str) -> bool:
    if unicodedata.normalize("NFC", value) != value:
        return True
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            return True
        if unicodedata.category(character).startswith("C") and character not in {
            "\t",
            "\n",
            "\r",
        }:
            return True
    return False


def _request_bytes(authority: LanRuntimeAuthority, body: bytes) -> bytes:
    host = _format_numeric_authority(authority.endpoint.address, authority.endpoint.port)
    head = (
        "POST /v1/chat/completions HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Accept: application/json\r\n"
        "Accept-Encoding: identity\r\n"
        "Connection: close\r\n"
        "User-Agent: Kestrel-LAN-Runtime/1\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii")
    return head + body


def _format_numeric_authority(address: str, port: int) -> str:
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address):
        return f"[{parsed}]:{port}"
    return f"{parsed}:{port}"


def _socket_authority(
    authority: LanRuntimeAuthority,
) -> tuple[int, object, object]:
    destination = ipaddress.ip_address(authority.endpoint.address)
    source = ipaddress.ip_address(authority.source_address)
    if type(destination) is not type(source):
        raise ValueError("LAN runtime source and destination families differ")
    if isinstance(destination, ipaddress.IPv4Address):
        return (
            socket.AF_INET,
            (str(source), 0),
            (str(destination), authority.endpoint.port),
        )
    zone = authority.interface_index if destination.is_link_local or source.is_link_local else 0
    return (
        socket.AF_INET6,
        (str(source), 0, 0, zone),
        (str(destination), authority.endpoint.port, 0, zone),
    )


def _pin_socket(
    connection: SocketLike,
    family: int,
    authority: LanRuntimeAuthority,
    platform_name: str,
) -> None:
    try:
        if platform_name == "Darwin":
            level = socket.IPPROTO_IP if family == socket.AF_INET else socket.IPPROTO_IPV6
            option = _DARWIN_IP_BOUND_IF if family == socket.AF_INET else _DARWIN_IPV6_BOUND_IF
            connection.setsockopt(level, option, authority.interface_index)
            return
        if platform_name == "Linux" and hasattr(socket, "SO_BINDTODEVICE"):
            name = _interface_name(authority.os_interface_identity, platform_name)
            connection.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BINDTODEVICE,
                name.encode("utf-8") + b"\0",
            )
            return
    except (OSError, TypeError, ValueError):
        raise LanRuntimeTransportError(
            LanRuntimeTransportFailure.INTERFACE_PINNING_UNAVAILABLE
        ) from None
    raise LanRuntimeTransportError(LanRuntimeTransportFailure.INTERFACE_PINNING_UNAVAILABLE)


def _interface_name(os_identity: str, platform_name: str) -> str:
    prefix = f"{platform_name.lower()}:"
    if not os_identity.startswith(prefix) or not os_identity[len(prefix) :]:
        raise ValueError("selected LAN interface identity is invalid")
    return os_identity[len(prefix) :]


def _remaining(
    deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
) -> float:
    _require_not_cancelled(cancellation)
    try:
        now = clock()
    except Exception:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_TIMEOUT) from None
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(now)
        or not math.isfinite(deadline)
    ):
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_TIMEOUT)
    remaining = deadline - float(now)
    if remaining <= 0:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_TIMEOUT)
    return remaining


def _check_completed_deadline(
    deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
) -> None:
    _require_not_cancelled(cancellation)
    try:
        now = clock()
    except Exception:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_TIMEOUT) from None
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(now)
        or not math.isfinite(deadline)
        or float(now) > deadline
    ):
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_TIMEOUT)


def _require_not_cancelled(cancellation: CancellationToken) -> None:
    try:
        cancelled = cancellation.is_cancelled()
    except Exception:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.CANCELLED) from None
    if type(cancelled) is not bool or cancelled:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.CANCELLED)


def _set_deadline(
    connection: SocketLike,
    deadline: float,
    cancellation: CancellationToken,
    clock: Callable[[], float],
) -> None:
    connection.settimeout(_remaining(deadline, cancellation, clock))


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
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        _set_deadline(connection, deadline, cancellation, clock)
        chunk = connection.recv(min(4096, MAX_HTTP_HEADER_BYTES + len(delimiter) - len(buffer)))
        _check_completed_deadline(deadline, cancellation, clock)
        if not chunk:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        buffer.extend(chunk)
    head, initial_body = bytes(buffer).split(delimiter, 1)
    if len(head) > MAX_HTTP_HEADER_BYTES:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
    lines = head.split(b"\r\n")
    if (
        not lines
        or len(lines) - 1 > MAX_HTTP_HEADER_LINES
        or not lines[0]
        or len(lines[0]) > MAX_HTTP_STATUS_LINE_BYTES
        or any(not line or len(line) > MAX_HTTP_HEADER_LINE_BYTES for line in lines[1:])
    ):
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
    status_parts = lines[0].split(b" ", 2)
    if (
        len(status_parts) < 2
        or status_parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"}
        or len(status_parts[1]) != 3
        or not status_parts[1].isdigit()
        or any(byte < 0x20 or byte == 0x7F for byte in lines[0])
    ):
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
    status = int(status_parts[1])
    if not 100 <= status <= 599:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
    headers: dict[str, str] = {}
    for raw_line in lines[1:]:
        if raw_line[:1] in {b" ", b"\t"} or b":" not in raw_line:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        raw_name, raw_value = raw_line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("ascii").strip()
        except UnicodeDecodeError:
            raise LanRuntimeTransportError(
                LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED
            ) from None
        if (
            not name
            or not all(character.isalnum() or character == "-" for character in name)
            or name in headers
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
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
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.UNSUPPORTED_CONTENT_ENCODING)
    if transfer_encoding is not None and content_length is not None:
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
    reader = _BoundedSocketReader(connection, initial, deadline, cancellation, clock)
    if transfer_encoding is not None:
        if transfer_encoding.lower() != "chunked":
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        return _read_chunked_body(reader)
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        canonical_length = content_length.lstrip("0") or "0"
        if len(canonical_length) > len(str(MAX_LAN_RUNTIME_RESPONSE_BYTES)):
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.RESPONSE_TOO_LARGE)
        length = int(canonical_length)
        if length > MAX_LAN_RUNTIME_RESPONSE_BYTES:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.RESPONSE_TOO_LARGE)
        body = reader.read_exact(length)
        if reader.buffered_bytes:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        return body
    if headers.get("connection", "").lower() != "close":
        raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
    return reader.read_until_close(MAX_LAN_RUNTIME_RESPONSE_BYTES)


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
                raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
            self._receive(min(4096, max_bytes + 2 - len(self._buffer)))
        line, remainder = bytes(self._buffer).split(b"\r\n", 1)
        self._buffer = bytearray(remainder)
        if len(line) > max_bytes:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        return line

    def read_until_close(self, max_bytes: int) -> bytes:
        while True:
            if len(self._buffer) > max_bytes:
                raise LanRuntimeTransportError(LanRuntimeTransportFailure.RESPONSE_TOO_LARGE)
            chunk = self._receive(
                min(65536, max_bytes + 1 - len(self._buffer)),
                eof_ok=True,
            )
            if not chunk:
                return bytes(self._buffer)

    def _receive(self, size: int, *, eof_ok: bool = False) -> bytes:
        _set_deadline(
            self._connection,
            self._deadline,
            self._cancellation,
            self._clock,
        )
        chunk = self._connection.recv(max(1, size))
        _check_completed_deadline(
            self._deadline,
            self._cancellation,
            self._clock,
        )
        if not chunk and not eof_ok:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        self._buffer.extend(chunk)
        return chunk


def _read_chunked_body(reader: _BoundedSocketReader) -> bytes:
    result = bytearray()
    while True:
        raw_size = reader.read_line(MAX_HTTP_CHUNK_LINE_BYTES)
        if _CHUNK_SIZE_RE.fullmatch(raw_size) is None:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
        canonical_size = raw_size.lstrip(b"0") or b"0"
        if len(canonical_size) > len(f"{MAX_LAN_RUNTIME_RESPONSE_BYTES:x}"):
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.RESPONSE_TOO_LARGE)
        size = int(canonical_size, 16)
        if len(result) + size > MAX_LAN_RUNTIME_RESPONSE_BYTES:
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.RESPONSE_TOO_LARGE)
        if size == 0:
            if reader.read_line(MAX_HTTP_HEADER_LINE_BYTES) != b"":
                raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
            if reader.buffered_bytes:
                raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
            return bytes(result)
        result.extend(reader.read_exact(size))
        if reader.read_exact(2) != b"\r\n":
            raise LanRuntimeTransportError(LanRuntimeTransportFailure.HTTP_PROTOCOL_REJECTED)
