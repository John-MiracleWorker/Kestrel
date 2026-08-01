"""Immutable, bounded values used by explicit private-LAN discovery."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass

KNOWN_MODEL_SERVICE_PORTS = (1234, 8000, 8080, 11434)
MAX_ACTIVE_HOSTS = 256
MAX_SCAN_CONCURRENCY = 16
TCP_CONNECT_TIMEOUT_SECONDS = 0.75
HTTP_PROBE_TIMEOUT_SECONDS = 2.0
TOTAL_SCAN_DEADLINE_SECONDS = 45.0
MAX_PROBE_RESPONSE_BYTES = 256 * 1024
MAX_DISCOVERED_MODELS = 8
MDNS_WINDOW_SECONDS = 2.5

_PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_PRIVATE_IPV6_NETWORKS = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)


def _sha256_identifier(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_interface_address(value: str) -> str:
    """Normalize an attached address while retaining its CIDR prefix."""

    address, separator, prefix = value.strip().partition("/")
    if not separator or not address or not prefix:
        raise ValueError("interface addresses must include an address and prefix")
    # Link-local zone identifiers are OS routing hints, not part of the IP literal.
    literal = address.partition("%")[0]
    try:
        return str(ipaddress.ip_interface(f"{literal}/{prefix}"))
    except ValueError as exc:
        raise ValueError(f"invalid interface address: {value}") from exc


@dataclass(frozen=True)
class NetworkInterface:
    """A server-derived network interface; display_name is never an authority key."""

    interface_id: str
    os_identity: str
    display_name: str
    addresses: tuple[str, ...]

    @classmethod
    def from_addresses(
        cls,
        *,
        os_identity: str,
        display_name: str,
        addresses: tuple[str, ...],
    ) -> NetworkInterface:
        normalized_identity = os_identity.strip()
        if not normalized_identity:
            raise ValueError("OS interface identity is required")
        canonical_addresses = tuple(sorted({_canonical_interface_address(value) for value in addresses}))
        if not canonical_addresses:
            raise ValueError("an interface must have at least one address")
        return cls(
            interface_id=_sha256_identifier(
                {"os_identity": normalized_identity, "addresses": canonical_addresses}
            ),
            os_identity=normalized_identity,
            display_name=display_name.strip() or normalized_identity,
            addresses=canonical_addresses,
        )


@dataclass(frozen=True)
class ResolvedLanEndpoint:
    """A literal endpoint derived only from a confirmed private scope."""

    interface_id: str
    address: str
    port: int

    def __post_init__(self) -> None:
        if not self.interface_id.startswith("sha256:"):
            raise ValueError("endpoint requires a canonical interface ID")
        try:
            parsed = ipaddress.ip_address(self.address)
        except ValueError as exc:
            raise ValueError("endpoint requires a literal IP address") from exc
        if parsed.is_unspecified or parsed.is_loopback or parsed.is_multicast or parsed.is_reserved:
            raise ValueError("endpoint address is not eligible for LAN discovery")
        if not _is_private_lan_address(parsed):
            raise ValueError("endpoint requires a private LAN address")
        if not 1 <= self.port <= 65535:
            raise ValueError("endpoint port must be between 1 and 65535")

    @property
    def endpoint_id(self) -> str:
        return _sha256_identifier(
            {"interface_id": self.interface_id, "address": self.address, "port": self.port}
        )


def _is_private_lan_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _PRIVATE_IPV4_NETWORKS)
    return any(address in network for network in _PRIVATE_IPV6_NETWORKS)


@dataclass(frozen=True)
class LanScanLimits:
    """Non-negotiable resource limits for one explicitly confirmed scan."""

    known_model_service_ports: tuple[int, ...] = KNOWN_MODEL_SERVICE_PORTS
    max_active_hosts: int = MAX_ACTIVE_HOSTS
    max_scan_concurrency: int = MAX_SCAN_CONCURRENCY
    tcp_connect_timeout_seconds: float = TCP_CONNECT_TIMEOUT_SECONDS
    http_probe_timeout_seconds: float = HTTP_PROBE_TIMEOUT_SECONDS
    total_scan_deadline_seconds: float = TOTAL_SCAN_DEADLINE_SECONDS
    max_probe_response_bytes: int = MAX_PROBE_RESPONSE_BYTES
    max_discovered_models: int = MAX_DISCOVERED_MODELS
    mdns_window_seconds: float = MDNS_WINDOW_SECONDS

    def __post_init__(self) -> None:
        if self.known_model_service_ports != KNOWN_MODEL_SERVICE_PORTS:
            raise ValueError("LAN discovery uses only known model-service ports")
        if self.max_active_hosts != MAX_ACTIVE_HOSTS:
            raise ValueError("LAN discovery maximum active hosts is fixed")
        if self.max_scan_concurrency != MAX_SCAN_CONCURRENCY:
            raise ValueError("LAN discovery maximum concurrency is fixed")
        if self.tcp_connect_timeout_seconds != TCP_CONNECT_TIMEOUT_SECONDS:
            raise ValueError("LAN discovery TCP timeout is fixed")
        if self.http_probe_timeout_seconds != HTTP_PROBE_TIMEOUT_SECONDS:
            raise ValueError("LAN discovery HTTP timeout is fixed")
        if self.total_scan_deadline_seconds != TOTAL_SCAN_DEADLINE_SECONDS:
            raise ValueError("LAN discovery total deadline is fixed")
        if self.max_probe_response_bytes != MAX_PROBE_RESPONSE_BYTES:
            raise ValueError("LAN discovery response limit is fixed")
        if self.max_discovered_models != MAX_DISCOVERED_MODELS:
            raise ValueError("LAN discovery model limit is fixed")
        if self.mdns_window_seconds != MDNS_WINDOW_SECONDS:
            raise ValueError("LAN discovery mDNS window is fixed")


@dataclass(frozen=True)
class LanScanPreview:
    """The canonical, read-only scope a later scan-start request must bind."""

    interface_id: str
    network: str
    active_hosts: tuple[str, ...]
    passive_or_manual_only: bool
    limits: LanScanLimits
    port_matrix: tuple[ResolvedLanEndpoint, ...]

    @property
    def active_host_count(self) -> int:
        return len(self.active_hosts)
