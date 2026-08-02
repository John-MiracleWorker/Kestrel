"""Pure validation and resolution for one owner-entered private-LAN host."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice

from nested_memvid_agent.lan_discovery_models import MAX_ACTIVE_HOSTS, NetworkInterface

MAX_MANUAL_RESOLVED_ADDRESSES = 16

_LOCAL_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LOCAL_SUFFIXES = (("local",), ("lan",), ("internal",), ("home", "arpa"))
_ELIGIBLE_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_ELIGIBLE_IPV6_NETWORKS = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)

ManualHostResolver = Callable[[str], tuple[str, ...]]


class ManualHostResolverUnavailable(RuntimeError):
    """The OS resolver could not service a manual-host lookup."""


def default_manual_host_resolver(host: str) -> tuple[str, ...]:
    """Resolve once with the OS resolver and return only bounded literal answers."""

    try:
        raw = socket.getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError:
        raise ManualHostResolverUnavailable("manual host resolver is unavailable") from None
    bounded = tuple(islice(iter(raw), MAX_MANUAL_RESOLVED_ADDRESSES + 1))
    if not bounded:
        raise ValueError("manual host resolution failed")
    answers: list[str] = []
    for item in bounded:
        if type(item) is not tuple or len(item) != 5:
            raise ValueError("manual host resolution failed")
        family, socket_type, protocol, _canonical_name, sockaddr = item
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise ValueError("manual host resolution failed")
        if socket_type != socket.SOCK_STREAM or protocol not in {0, socket.IPPROTO_TCP}:
            raise ValueError("manual host resolution failed")
        if type(sockaddr) is not tuple or not sockaddr or type(sockaddr[0]) is not str:
            raise ValueError("manual host resolution failed")
        answers.append(sockaddr[0])
    return tuple(answers)


@dataclass(frozen=True)
class ManualLanPreview:
    """Safe preview output that deliberately retains no raw host input."""

    interface_id: str
    port: int
    resolved_addresses: tuple[str, ...]
    host_input_digest: str
    requires_confirmation: bool = True


def preview_manual_host(
    interface_id: str,
    host: str,
    port: int,
    *,
    interfaces: tuple[NetworkInterface, ...],
    resolver: ManualHostResolver,
) -> ManualLanPreview:
    """Resolve and validate a host without probing, writing, or submitting work."""

    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("manual probe port must be an integer from 1 through 65535")
    selected = _select_interface(interface_id, interfaces)
    if not callable(resolver):
        raise TypeError("manual host resolver must be callable")
    if type(host) is not str:
        raise TypeError("manual host must be canonical text")

    literal = _parse_literal(host)
    addresses: tuple[str, ...]
    if literal is not None:
        addresses = (_validate_attached_address(str(literal), selected),)
    else:
        _validate_local_hostname(host)
        try:
            answers = resolver(host)
        except Exception:
            raise ManualHostResolverUnavailable("manual host resolver is unavailable") from None
        addresses = _validate_resolver_answers(answers, selected)

    return ManualLanPreview(
        interface_id=selected.interface_id,
        port=port,
        resolved_addresses=addresses,
        host_input_digest=_host_input_digest(host),
    )


def _select_interface(
    interface_id: str,
    interfaces: tuple[NetworkInterface, ...],
) -> NetworkInterface:
    if (
        type(interface_id) is not str
        or type(interfaces) is not tuple
        or not interfaces
        or len(interfaces) > MAX_ACTIVE_HOSTS
    ):
        raise ValueError("unknown private network interface")
    selected: NetworkInterface | None = None
    seen: set[str] = set()
    for interface in interfaces:
        if type(interface) is not NetworkInterface:
            raise ValueError("manual interface inventory is invalid")
        try:
            rebuilt = NetworkInterface.from_addresses(
                os_identity=interface.os_identity,
                display_name=interface.display_name,
                addresses=interface.addresses,
            )
        except (AttributeError, TypeError, ValueError):
            raise ValueError("manual interface inventory is invalid") from None
        if interface != rebuilt or interface.interface_id in seen:
            raise ValueError("manual interface inventory is invalid")
        seen.add(interface.interface_id)
        if interface.interface_id == interface_id:
            selected = interface
    if selected is None:
        raise ValueError("unknown private network interface")
    return selected


def _parse_literal(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value or value != value.strip() or "%" in value:
        return None
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    if value != str(parsed):
        raise ValueError("manual host literal is not canonical")
    return parsed


def _validate_local_hostname(host: str) -> None:
    error = ValueError("manual host must be a canonical local name")
    if not host or len(host.encode("ascii", errors="ignore")) != len(host) or len(host) > 253:
        raise error
    labels = host.split(".")
    if any(_LOCAL_LABEL_RE.fullmatch(label) is None for label in labels) or "localhost" in labels:
        raise error
    if len(labels) == 1:
        return
    if not any(tuple(labels[-len(suffix) :]) == suffix for suffix in _LOCAL_SUFFIXES):
        raise error
    # A suffix alone is not a host authority.
    if any(tuple(labels) == suffix for suffix in _LOCAL_SUFFIXES):
        raise error


def _validate_resolver_answers(
    answers: object,
    interface: NetworkInterface,
) -> tuple[str, ...]:
    error = ValueError("manual host resolution returned no eligible attached addresses")
    if type(answers) is not tuple or not 1 <= len(answers) <= MAX_MANUAL_RESOLVED_ADDRESSES:
        raise error
    canonical: list[str] = []
    seen: set[str] = set()
    for answer in answers:
        if type(answer) is not str:
            raise error
        validated = _validate_attached_address(answer, interface)
        if validated in seen:
            raise error
        seen.add(validated)
        canonical.append(validated)
    return tuple(sorted(canonical))


def _validate_attached_address(value: str, interface: NetworkInterface) -> str:
    error = ValueError("manual host address is not an eligible attached literal")
    if type(value) is not str or not value or value != value.strip() or "%" in value:
        raise error
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise error from None
    if value != str(address):
        raise error
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved:
        raise error
    if isinstance(address, ipaddress.IPv4Address):
        if not any(address in network for network in _ELIGIBLE_IPV4_NETWORKS):
            raise error
    elif not any(address in network for network in _ELIGIBLE_IPV6_NETWORKS):
        raise error
    try:
        attached = tuple(ipaddress.ip_interface(item) for item in interface.addresses)
    except (TypeError, ValueError):
        raise error from None
    if not any(type(item.ip) is type(address) and address in item.network for item in attached):
        raise error
    return str(address)


def _host_input_digest(host: str) -> str:
    encoded = json.dumps(
        {"host": host, "schema": "kestrel.lan.manual-host-input.v1"},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
