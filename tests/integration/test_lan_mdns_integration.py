from __future__ import annotations

import os

import pytest


def test_controlled_same_host_mdns_adapter_mechanics() -> None:
    """This is same-host mechanics evidence, never two-machine LAN qualification."""

    if os.getenv("RUN_LAN_DISCOVERY_INTEGRATION") != "1":
        pytest.skip(
            "Set RUN_LAN_DISCOVERY_INTEGRATION=1 to run controlled same-host mDNS mechanics."
        )

    # Every optional/live import remains below the explicit gate. The module can
    # therefore collect a truthful skip without importing zeroconf, enumerating
    # interfaces, or opening sockets.
    import importlib.util

    if importlib.util.find_spec("zeroconf") is None:
        pytest.skip(
            "RUN_LAN_DISCOVERY_INTEGRATION=1 requires the optional zeroconf package."
        )

    import ipaddress
    import socket
    import uuid

    from zeroconf import IPVersion, ServiceInfo, Zeroconf

    from nested_memvid_agent.lan_discovery_models import NetworkInterface
    from nested_memvid_agent.lan_discovery_scope import (
        PrivateScanScope,
        enumerate_private_interfaces,
    )
    from nested_memvid_agent.lan_mdns import collect_mdns_candidates

    selected_interface: NetworkInterface | None = None
    selected_address: str | None = None
    for discovered in enumerate_private_interfaces():
        for attached_value in discovered.addresses:
            attached = ipaddress.ip_interface(attached_value)
            if not isinstance(attached, ipaddress.IPv4Interface):
                continue
            address = attached.ip
            if address.is_loopback or address.is_multicast or address.is_reserved:
                continue
            # Preserve the complete current OS-owned address tuple. The /32 scope
            # below remains the exact candidate authority for this fixture.
            selected_interface = discovered
            selected_address = str(address)
            break
        if selected_interface is not None:
            break
    if selected_interface is None or selected_address is None:
        pytest.skip("No eligible real private IPv4 interface is available for the /32 fixture.")

    interface_name = selected_interface.os_identity.partition(":")[2]
    try:
        interface_index = socket.if_nametoindex(interface_name)
        current_interface_name = socket.if_indextoname(interface_index)
    except OSError:
        pytest.skip("The selected private IPv4 interface has no verifiable current OS index.")
    if interface_index <= 0 or current_interface_name != interface_name:
        pytest.skip("The selected private IPv4 interface index cannot be authenticated.")

    scope = PrivateScanScope.from_request(selected_interface, f"{selected_address}/32")
    service_type = "_kestrel-model._tcp.local."
    instance_name = f"Kestrel-Fixture-{uuid.uuid4().hex}.{service_type}"
    info = ServiceInfo(
        service_type,
        instance_name,
        addresses=[socket.inet_aton(selected_address)],
        port=11434,
        properties={b"display_name": b"Kestrel controlled same-host fixture"},
    )
    advertiser: Zeroconf | None = None
    registered = False
    try:
        try:
            advertiser = Zeroconf(interfaces=[selected_address], ip_version=IPVersion.V4Only)
            advertiser.register_service(info, allow_name_change=False)
            registered = True
        except OSError as exc:
            pytest.skip(f"Controlled same-host mDNS advertiser is unavailable: {exc}")

        candidates = collect_mdns_candidates(scope)

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.interface_id == selected_interface.interface_id
        assert candidate.address == selected_address
        assert candidate.port == 11434
        assert candidate.service_type == service_type
        assert candidate.instance_name == instance_name[: -len(f".{service_type}")]
        assert candidate.provider_hint is None
        assert dict(candidate.metadata) == {
            "display_name": "Kestrel controlled same-host fixture"
        }
        assert len(candidate.metadata_json.encode("utf-8")) <= 4096
    finally:
        if advertiser is not None:
            try:
                if registered:
                    advertiser.unregister_service(info)
            finally:
                advertiser.close()
