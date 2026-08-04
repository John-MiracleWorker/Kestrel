from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from importlib import import_module

import pytest

from nested_memvid_agent.lan_discovery_models import (
    KNOWN_MODEL_SERVICE_PORTS,
    LanScanLimits,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import (
    PrivateScanScope,
    enumerate_private_interfaces,
    preview_private_scope,
)


def interface_fixture(*addresses: str, os_identity: str = "darwin:en0") -> NetworkInterface:
    return NetworkInterface.from_addresses(
        os_identity=os_identity,
        display_name="Wi-Fi",
        addresses=addresses or ("192.168.1.7/24",),
    )


def manual_endpoint_type():
    """Resolve the wished-for Task 7B type without breaking control-test collection."""

    return import_module("nested_memvid_agent.lan_discovery_models").ManualLanEndpoint


class HostilePort(int):
    def __format__(self, format_spec: str) -> str:
        del format_spec
        raise AssertionError("hostile port formatting crossed validation")


@pytest.mark.parametrize(
    "network",
    [
        "8.8.8.0/24",
        "0.0.0.0/24",
        "127.0.0.0/24",
        "224.0.0.0/24",
        "192.0.2.0/24",
        "2001:db8::/64",
    ],
)
def test_scan_scope_rejects_non_private_network(network: str) -> None:
    """Removing explicit RFC-range checks would admit unsafe public/documentation scopes."""
    with pytest.raises(ValueError, match="private interface scope"):
        PrivateScanScope.from_request(interface_fixture(), network)


def test_scan_scope_requires_a_subnet_attached_to_the_selected_interface() -> None:
    """Dropping interface containment would let a renderer scan a sibling subnet."""
    with pytest.raises(ValueError, match="attached to the selected interface"):
        PrivateScanScope.from_request(interface_fixture("192.168.1.7/24"), "192.168.2.0/24")


def test_scan_scope_caps_active_ipv4_hosts() -> None:
    """Removing the active-host limit would turn an owner preview into a broad scan."""
    with pytest.raises(ValueError, match="at most 256 hosts"):
        PrivateScanScope.from_request(interface_fixture("10.0.0.2/16"), "10.0.0.0/16")


@pytest.mark.parametrize(
    ("address", "network"),
    [
        ("10.10.0.2/16", "10.10.5.0/24"),
        ("172.16.8.2/20", "172.16.8.0/24"),
        ("192.168.1.7/24", "192.168.1.0/24"),
        ("169.254.12.7/16", "169.254.12.0/24"),
    ],
)
def test_scan_scope_accepts_only_explicit_private_or_ipv4_link_local_ranges(
    address: str, network: str
) -> None:
    scope = PrivateScanScope.from_request(interface_fixture(address), network)

    assert scope.network == network
    assert scope.active_hosts
    assert scope.passive_or_manual_only is False


def test_ipv6_scope_never_produces_an_active_host_enumeration() -> None:
    scope = PrivateScanScope.from_request(
        interface_fixture("fd00::2/64"),
        "fd00::/64",
    )

    assert scope.active_hosts == ()
    assert scope.passive_or_manual_only is True


def test_interface_bound_ipv6_link_local_scope_is_passive_only() -> None:
    scope = PrivateScanScope.from_request(
        interface_fixture("fe80::2/64"),
        "fe80::/64",
    )

    assert scope.active_hosts == ()
    assert scope.passive_or_manual_only is True


def test_interface_id_is_deterministic_opaque_and_ignores_display_name() -> None:
    """Using a display name as identity would let a renderer select the wrong adapter."""
    first = NetworkInterface.from_addresses(
        os_identity="windows:{4C3D}",
        display_name="Ethernet",
        addresses=("192.168.10.7/24", "fd00::7/64"),
    )
    renamed = NetworkInterface.from_addresses(
        os_identity="windows:{4C3D}",
        display_name="Untrusted renderer label",
        addresses=("fd00::7/64", "192.168.10.7/24"),
    )
    changed_address = NetworkInterface.from_addresses(
        os_identity="windows:{4C3D}",
        display_name="Ethernet",
        addresses=("192.168.10.8/24", "fd00::7/64"),
    )

    assert first.interface_id == renamed.interface_id
    assert first.interface_id != changed_address.interface_id
    assert first.interface_id.startswith("sha256:")
    assert "Ethernet" not in first.interface_id


@pytest.mark.parametrize("address", ["8.8.8.8", "192.0.2.10", "2001:db8::10"])
def test_endpoint_primitive_rejects_non_private_addresses(address: str) -> None:
    """Removing private-range admission would permit later probes to bypass scope validation."""
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.0/24",
    )
    with pytest.raises(ValueError, match="private LAN address"):
        ResolvedLanEndpoint.from_scope(scope, address, 11434)


def test_endpoint_requires_an_address_in_its_confirmed_scope() -> None:
    """Dropping scope membership would let a later probe leave the confirmed subnet."""
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.0/24",
    )

    with pytest.raises(ValueError, match="confirmed scope"):
        ResolvedLanEndpoint.from_scope(scope, "10.9.8.7", 11434)


def test_endpoint_rejects_ports_outside_the_known_active_matrix() -> None:
    """Allowing arbitrary ports would turn active discovery into a port scanner."""
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.0/24",
    )

    with pytest.raises(ValueError, match="known model-service ports"):
        ResolvedLanEndpoint.from_scope(scope, "192.168.1.8", 22)


def test_automatic_endpoint_factory_rejects_int_subclass_port() -> None:
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.0/24",
    )

    with pytest.raises(ValueError, match="port"):
        ResolvedLanEndpoint.from_scope(scope, "192.168.1.8", HostilePort(11434))


@pytest.mark.parametrize(
    ("interface_address", "network", "address", "port"),
    (
        ("192.168.1.7/24", "192.168.1.8/32", "192.168.1.8", 1),
        ("192.168.1.7/24", "192.168.1.8/32", "192.168.1.8", 5001),
        ("192.168.1.7/24", "192.168.1.8/32", "192.168.1.8", 11434),
        ("192.168.1.7/24", "192.168.1.8/32", "192.168.1.8", 65535),
        ("169.254.10.7/16", "169.254.10.8/32", "169.254.10.8", 5001),
        ("fd00::7/64", "fd00::8/128", "fd00::8", 5001),
        ("fe80::7/64", "fe80::8/128", "fe80::8", 5001),
    ),
)
def test_manual_endpoint_is_a_separate_exact_host_authority(
    interface_address: str,
    network: str,
    address: str,
    port: int,
) -> None:
    """Broadening the automatic endpoint factory would turn one consent into a port scan."""

    manual_type = manual_endpoint_type()
    scope = PrivateScanScope.from_request(interface_fixture(interface_address), network)

    endpoint = manual_type.from_exact_scope(scope, address, port)

    assert type(endpoint) is manual_type
    assert (endpoint.kind, endpoint.interface_id, endpoint.address, endpoint.port) == (
        "manual",
        scope.interface.interface_id,
        address,
        port,
    )
    if port not in KNOWN_MODEL_SERVICE_PORTS:
        with pytest.raises(ValueError, match="known model-service ports"):
            ResolvedLanEndpoint.from_scope(scope, address, port)


def test_manual_endpoint_authority_is_factory_only_and_frozen() -> None:
    manual_type = manual_endpoint_type()
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.8/32",
    )
    endpoint = manual_type.from_exact_scope(scope, "192.168.1.8", 5001)
    construction_attempts = (
        ((scope.interface.interface_id, "192.168.1.8", 5001), {}),
        (("manual", scope.interface.interface_id, "192.168.1.8", 5001), {}),
        (
            (),
            {
                "interface_id": scope.interface.interface_id,
                "address": "192.168.1.8",
                "port": 5001,
            },
        ),
        (
            (),
            {
                "kind": "manual",
                "interface_id": scope.interface.interface_id,
                "address": "192.168.1.8",
                "port": 5001,
            },
        ),
    )

    for args, kwargs in construction_attempts:
        with pytest.raises(TypeError):
            manual_type(*args, **kwargs)

    with pytest.raises((FrozenInstanceError, AttributeError)):
        endpoint.port = 5002
    assert (endpoint.kind, endpoint.address, endpoint.port) == (
        "manual",
        "192.168.1.8",
        5001,
    )


@pytest.mark.parametrize("port", [False, True, 0, -1, 65536, 1.0, "5001"])
def test_manual_endpoint_rejects_non_integer_or_out_of_range_ports(port: object) -> None:
    manual_type = manual_endpoint_type()
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.8/32",
    )

    with pytest.raises((TypeError, ValueError), match="port"):
        manual_type.from_exact_scope(scope, "192.168.1.8", port)


def test_manual_endpoint_factory_rejects_int_subclass_port() -> None:
    manual_type = manual_endpoint_type()
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.8/32",
    )

    with pytest.raises(ValueError, match="port"):
        manual_type.from_exact_scope(scope, "192.168.1.8", HostilePort(5001))


@pytest.mark.parametrize(
    "address",
    [3232235784, b"\xc0\xa8\x01\x08", True, object()],
)
def test_manual_endpoint_rejects_non_string_address_lookalikes(address: object) -> None:
    manual_type = manual_endpoint_type()
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.8/32",
    )

    with pytest.raises((TypeError, ValueError), match="manual|literal|address"):
        manual_type.from_exact_scope(scope, address, 5001)


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "127.0.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "192.0.2.1",
        "model-box.local",
        "http://192.168.1.8",
        "user@192.168.1.8",
        "fe80::8%en0",
        "192.168.001.008",
        "192.168.1.9",
    ],
)
def test_manual_endpoint_rejects_nonliteral_ineligible_or_different_exact_host(
    address: str,
) -> None:
    manual_type = manual_endpoint_type()
    scope = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.8/32",
    )

    with pytest.raises(ValueError, match="manual|literal|eligible|scope|address"):
        manual_type.from_exact_scope(scope, address, 5001)


def test_manual_endpoint_rejects_broad_or_forged_scope_objects() -> None:
    manual_type = manual_endpoint_type()
    broad = PrivateScanScope.from_request(
        interface_fixture("192.168.1.7/24"),
        "192.168.1.0/24",
    )
    exact = PrivateScanScope.from_request(
        broad.interface,
        "192.168.1.8/32",
    )
    forged = replace(exact, network="192.168.1.9/32")

    for scope in (broad, forged, object()):
        with pytest.raises((TypeError, ValueError), match="manual|exact|scope"):
            manual_type.from_exact_scope(scope, "192.168.1.8", 5001)


@pytest.mark.parametrize("interface_id", ["sha256:abc", "sha256:" + "A" * 64])
def test_endpoint_rejects_noncanonical_interface_digests(interface_id: str) -> None:
    """A prefix-only digest check would permit a forged interface identity."""
    interface = replace(interface_fixture("192.168.1.7/24"), interface_id=interface_id)
    scope = PrivateScanScope.from_request(interface, "192.168.1.0/24")

    with pytest.raises(ValueError, match="canonical interface ID"):
        ResolvedLanEndpoint.from_scope(scope, "192.168.1.8", 11434)


def test_preview_binds_the_exact_active_host_count_and_port_matrix() -> None:
    interface = interface_fixture("192.168.10.2/30")
    preview = preview_private_scope(
        interface.interface_id,
        "192.168.10.0/30",
        interfaces=(interface,),
    )

    assert preview.active_host_count == 2
    assert tuple((endpoint.address, endpoint.port) for endpoint in preview.port_matrix) == (
        ("192.168.10.1", 1234),
        ("192.168.10.1", 8000),
        ("192.168.10.1", 8080),
        ("192.168.10.1", 11434),
        ("192.168.10.2", 1234),
        ("192.168.10.2", 8000),
        ("192.168.10.2", 8080),
        ("192.168.10.2", 11434),
    )
    assert preview.limits == LanScanLimits()
    assert KNOWN_MODEL_SERVICE_PORTS == (1234, 8000, 8080, 11434)


def test_enumeration_uses_injected_platform_snapshots_without_touching_host_network() -> None:
    interfaces = enumerate_private_interfaces(
        snapshots={
            "darwin:en0": ("Wi-Fi", ("192.168.40.4/24", "2001:db8::4/64")),
            "linux:lo": ("Loopback", ("127.0.0.1/8",)),
        }
    )

    assert [(interface.os_identity, interface.addresses) for interface in interfaces] == [
        ("darwin:en0", ("192.168.40.4/24",)),
    ]
