"""Server-side canonicalization for explicit, bounded private-LAN scopes."""

from __future__ import annotations

import ipaddress
import platform
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from nested_memvid_agent.lan_discovery_models import (
    KNOWN_MODEL_SERVICE_PORTS,
    MAX_ACTIVE_HOSTS,
    LanScanLimits,
    LanScanPreview,
    NetworkInterface,
    ResolvedLanEndpoint,
)

_PRIVATE_IPV4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_IPV4_LINK_LOCAL_NETWORK = ipaddress.IPv4Network("169.254.0.0/16")
_IPV6_ULA_NETWORK = ipaddress.IPv6Network("fc00::/7")
_IPV6_LINK_LOCAL_NETWORK = ipaddress.IPv6Network("fe80::/10")


@dataclass(frozen=True)
class PrivateScanScope:
    """A selected interface plus a server-validated, non-broadenable network."""

    interface: NetworkInterface
    network: str
    active_hosts: tuple[str, ...]
    passive_or_manual_only: bool

    @classmethod
    def from_request(cls, interface: NetworkInterface, network: str) -> PrivateScanScope:
        requested = _parse_network(network)
        if not _is_private_lan_network(requested):
            raise ValueError("network must be a private interface scope")
        if not _network_is_attached_to_interface(requested, interface):
            raise ValueError("network must be attached to the selected interface")

        if isinstance(requested, ipaddress.IPv6Network):
            return cls(
                interface=interface,
                network=str(requested),
                active_hosts=(),
                passive_or_manual_only=True,
            )

        host_count = _ipv4_host_count(requested)
        if host_count > MAX_ACTIVE_HOSTS:
            raise ValueError(f"private interface scope may contain at most {MAX_ACTIVE_HOSTS} hosts")
        return cls(
            interface=interface,
            network=str(requested),
            active_hosts=tuple(str(host) for host in requested.hosts()),
            passive_or_manual_only=False,
        )


def enumerate_private_interfaces(
    *,
    snapshots: Mapping[str, tuple[str, Iterable[str]]] | None = None,
) -> tuple[NetworkInterface, ...]:
    """Return canonical private interfaces from injected snapshots or optional psutil.

    Tests inject snapshots so deterministic scope validation never observes the
    host's ambient network.  A minimal install without the optional dependency
    truthfully reports no passive-discovery-capable interfaces rather than
    silently falling back to host probing.
    """

    source = snapshots if snapshots is not None else _live_interface_snapshots()
    interfaces: list[NetworkInterface] = []
    for os_identity in sorted(source):
        display_name, addresses = source[os_identity]
        private_addresses = tuple(
            address for address in addresses if _is_private_interface_address(address)
        )
        if private_addresses:
            interfaces.append(
                NetworkInterface.from_addresses(
                    os_identity=os_identity,
                    display_name=display_name,
                    addresses=private_addresses,
                )
            )
    return tuple(interfaces)


def preview_private_scope(
    interface_id: str,
    network: str,
    *,
    interfaces: Iterable[NetworkInterface] | None = None,
) -> LanScanPreview:
    """Recalculate a preview from server-owned interface data, never renderer data."""

    candidates = tuple(enumerate_private_interfaces() if interfaces is None else interfaces)
    selected = next((item for item in candidates if item.interface_id == interface_id), None)
    if selected is None:
        raise ValueError("unknown private network interface")
    scope = PrivateScanScope.from_request(selected, network)
    limits = LanScanLimits()
    matrix = tuple(
        ResolvedLanEndpoint(interface_id=selected.interface_id, address=host, port=port)
        for host in scope.active_hosts
        for port in KNOWN_MODEL_SERVICE_PORTS
    )
    return LanScanPreview(
        interface_id=selected.interface_id,
        network=scope.network,
        active_hosts=scope.active_hosts,
        passive_or_manual_only=scope.passive_or_manual_only,
        limits=limits,
        port_matrix=matrix,
    )


def _parse_network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    try:
        parsed = ipaddress.ip_network(value.strip(), strict=True)
    except ValueError as exc:
        raise ValueError("network must be a private interface scope") from exc
    if not isinstance(parsed, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        raise ValueError("network must be a private interface scope")
    return parsed


def _is_private_lan_network(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    if isinstance(network, ipaddress.IPv4Network):
        return network.subnet_of(_IPV4_LINK_LOCAL_NETWORK) or any(
            network.subnet_of(private_network) for private_network in _PRIVATE_IPV4_NETWORKS
        )
    return network.subnet_of(_IPV6_ULA_NETWORK) or network.subnet_of(_IPV6_LINK_LOCAL_NETWORK)


def _network_is_attached_to_interface(
    requested: ipaddress.IPv4Network | ipaddress.IPv6Network,
    interface: NetworkInterface,
) -> bool:
    for value in interface.addresses:
        attached = ipaddress.ip_interface(value)
        if isinstance(requested, ipaddress.IPv4Network) and isinstance(
            attached, ipaddress.IPv4Interface
        ) and requested.subnet_of(attached.network):
            return True
        if isinstance(requested, ipaddress.IPv6Network) and isinstance(
            attached, ipaddress.IPv6Interface
        ) and requested.subnet_of(attached.network):
            return True
    return False


def _ipv4_host_count(network: ipaddress.IPv4Network) -> int:
    if network.prefixlen == 32:
        return 1
    if network.prefixlen == 31:
        return 2
    return network.num_addresses - 2


def _is_private_interface_address(value: str) -> bool:
    try:
        attached = ipaddress.ip_interface(value.partition("%")[0])
    except ValueError:
        return False
    return _is_private_lan_network(attached.network)


def _live_interface_snapshots() -> Mapping[str, tuple[str, Iterable[str]]]:
    """Adapt psutil lazily so importing scope primitives needs no optional extra."""

    try:
        import psutil  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        return {}

    snapshots: dict[str, tuple[str, tuple[str, ...]]] = {}
    for name, entries in psutil.net_if_addrs().items():
        addresses: list[str] = []
        for entry in entries:
            if entry.family not in {socket.AF_INET, socket.AF_INET6} or not entry.netmask:
                continue
            literal = str(entry.address).partition("%")[0]
            try:
                addresses.append(str(ipaddress.ip_interface(f"{literal}/{entry.netmask}")))
            except ValueError:
                continue
        if addresses:
            os_identity = f"{platform.system().lower()}:{name}"
            snapshots[os_identity] = (name, tuple(addresses))
    return snapshots
