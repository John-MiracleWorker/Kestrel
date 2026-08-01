from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import ANY

import pytest

import nested_memvid_agent.lan_mdns as lan_mdns
from nested_memvid_agent.lan_discovery_models import MAX_ACTIVE_HOSTS, NetworkInterface
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_mdns import (
    ALLOWED_MODEL_SERVICE_TYPES,
    MAX_MDNS_METADATA_BYTES,
    LanCandidate,
    MdnsBinding,
    MdnsRecord,
    collect_mdns_candidates,
)


class ManualClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeSession:
    def __init__(
        self,
        callback: Callable[[MdnsRecord], None],
        records: Sequence[MdnsRecord],
        clock: ManualClock,
    ) -> None:
        self._callback = callback
        self._records = records
        self._clock = clock
        self.wait_seconds: list[float] = []
        self.close_calls = 0

    def wait(self, seconds: float) -> None:
        self.wait_seconds.append(seconds)
        for record in self._records:
            self._callback(record)
        self._clock.advance(seconds)

    def close(self) -> None:
        self.close_calls += 1


class FakeAdapterFactory:
    def __init__(self, records: Sequence[MdnsRecord], clock: ManualClock) -> None:
        self.records = records
        self.clock = clock
        self.calls = 0
        self.binding: MdnsBinding | None = None
        self.service_types: tuple[str, ...] | None = None
        self.session: FakeSession | None = None

    def __call__(
        self,
        binding: MdnsBinding,
        service_types: tuple[str, ...],
        callback: Callable[[MdnsRecord], None],
    ) -> FakeSession:
        self.calls += 1
        self.binding = binding
        self.service_types = service_types
        self.session = FakeSession(callback, self.records, self.clock)
        return self.session


def interface_fixture(
    *addresses: str,
    os_identity: str = "darwin:en0",
) -> NetworkInterface:
    return NetworkInterface.from_addresses(
        os_identity=os_identity,
        display_name="Wi-Fi",
        addresses=addresses or ("192.168.50.7/24",),
    )


def scope_fixture(
    *addresses: str,
    network: str = "192.168.50.0/24",
    os_identity: str = "darwin:en0",
) -> PrivateScanScope:
    return PrivateScanScope.from_request(
        interface_fixture(*addresses, os_identity=os_identity),
        network,
    )


def record(
    *,
    service_type: str = "_kestrel-model._tcp.local.",
    instance: str = "Fixture",
    addresses: tuple[object, ...] = ("192.168.50.20",),
    port: object = 11434,
    properties: Mapping[object, object] | None = None,
    hostname: object | None = None,
) -> MdnsRecord:
    return MdnsRecord(
        service_type=service_type,
        instance_name=f"{instance}.{service_type}",
        addresses=addresses,
        port=port,
        properties={} if properties is None else properties,
        hostname=hostname,
    )


def collect_fake(
    records: Sequence[MdnsRecord],
    *,
    scope: PrivateScanScope | None = None,
    interface_index: int = 7,
) -> tuple[tuple[LanCandidate, ...], FakeAdapterFactory]:
    selected_scope = scope or scope_fixture()
    clock = ManualClock()
    factory = FakeAdapterFactory(records, clock)
    candidates = collect_mdns_candidates(
        selected_scope,
        adapter_factory=factory,
        clock=clock,
        interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
            interface_index=interface_index,
            addresses=selected_scope.interface.addresses,
        ),
    )
    return candidates, factory


def test_collector_uses_exact_service_allowlist_and_known_ports_only() -> None:
    valid = [
        record(service_type=service_type, port=port, instance=f"Fixture-{index}")
        for index, (service_type, port) in enumerate(
            zip(ALLOWED_MODEL_SERVICE_TYPES, (1234, 8000, 8080, 11434), strict=True)
        )
    ]
    hostile = [
        record(service_type="_http._tcp.local."),
        record(service_type="_OLLAMA._tcp.local."),
        record(service_type="_ollama._tcp.local.evil."),
        record(port=22),
        record(port=True),
        record(port="11434"),
    ]

    candidates, factory = collect_fake([*hostile, *valid])

    assert factory.service_types == (
        "_ollama._tcp.local.",
        "_lmstudio._tcp.local.",
        "_openai._tcp.local.",
        "_kestrel-model._tcp.local.",
    )
    assert {(candidate.service_type, candidate.port) for candidate in candidates} == set(
        zip(ALLOWED_MODEL_SERVICE_TYPES, (1234, 8000, 8080, 11434), strict=True)
    )


@pytest.mark.parametrize(
    "hostile_record",
    [
        record(addresses=("8.8.8.8",)),
        record(addresses=("192.168.51.20",)),
        record(addresses=("127.0.0.1",)),
        record(addresses=("224.0.0.1",)),
        record(addresses=("192.0.2.10",)),
        record(addresses=("not-an-address",)),
        record(addresses=(b"192.168.50.20",)),
        record(addresses=(), hostname="model-host.local."),
    ],
)
def test_collector_rejects_nonliteral_ineligible_and_out_of_scope_addresses(
    hostile_record: MdnsRecord,
) -> None:
    candidates, _factory = collect_fake([hostile_record])

    assert candidates == ()


def test_link_local_candidates_require_the_exact_selected_numeric_zone() -> None:
    scope = scope_fixture("fe80::2/64", network="fe80::/64")

    candidates, factory = collect_fake(
        [
            record(addresses=("fe80::20%7",), instance="Accepted"),
            record(addresses=("fe80::21%8",), instance="Wrong-zone"),
            record(addresses=("fe80::22",), instance="Missing-zone"),
            record(addresses=("fe80::23%en0",), instance="Mutable-zone-name"),
        ],
        scope=scope,
        interface_index=7,
    )

    assert factory.binding == MdnsBinding(ipv4_addresses=(), ipv6_interface_index=7)
    assert [(candidate.interface_id, candidate.address) for candidate in candidates] == [
        (scope.interface.interface_id, "fe80::20")
    ]


def test_adapter_is_bound_only_to_confirmed_ipv4_literals_and_verified_ipv6_index() -> None:
    scope = scope_fixture(
        "192.168.50.7/24",
        "10.8.0.7/24",
        "fd00::7/64",
        network="192.168.50.0/24",
        os_identity="darwin:en7",
    )

    _candidates, factory = collect_fake([], scope=scope, interface_index=41)

    assert factory.binding == MdnsBinding(
        ipv4_addresses=("10.8.0.7", "192.168.50.7"),
        ipv6_interface_index=41,
    )
    assert factory.calls == 1
    assert factory.session is not None
    assert factory.session.wait_seconds == [2.5]
    assert factory.session.close_calls == 1


def test_ipv4_only_scope_still_authenticates_selected_os_interface_identity() -> None:
    scope = scope_fixture(os_identity="darwin:en9")
    clock = ManualClock()
    factory = FakeAdapterFactory([], clock)
    resolved_identities: list[str] = []

    collect_mdns_candidates(
        scope,
        adapter_factory=factory,
        clock=clock,
        interface_state_resolver=lambda identity: (
            resolved_identities.append(identity)
            or lan_mdns.CurrentInterfaceState(
                interface_index=19,
                addresses=scope.interface.addresses,
            )
        ),
    )

    assert resolved_identities == ["darwin:en9"]
    assert factory.binding == MdnsBinding(
        ipv4_addresses=("192.168.50.7",),
        ipv6_interface_index=None,
    )


def test_default_interface_resolver_rejects_a_failed_os_identity_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = scope_fixture(os_identity="darwin:en9")
    clock = ManualClock()
    factory = FakeAdapterFactory([], clock)
    monkeypatch.setattr(lan_mdns.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lan_mdns, "enumerate_private_interfaces", lambda: (scope.interface,))
    monkeypatch.setattr(lan_mdns.socket, "if_nametoindex", lambda _name: 19)
    monkeypatch.setattr(lan_mdns.socket, "if_indextoname", lambda _index: "en8")

    with pytest.raises(ValueError, match="verified numeric interface index"):
        collect_mdns_candidates(scope, adapter_factory=factory, clock=clock)

    assert factory.calls == 0


@pytest.mark.parametrize(
    ("scope_addresses", "current_addresses"),
    [
        (("192.168.50.7/24",), ("192.168.50.8/24",)),
        (("192.168.50.7/24",), ("192.168.50.7/24", "10.8.0.7/24")),
        (("192.168.50.7/24", "10.8.0.7/24"), ("192.168.50.7/24",)),
    ],
)
def test_current_interface_address_move_addition_or_removal_fails_before_factory(
    scope_addresses: tuple[str, ...],
    current_addresses: tuple[str, ...],
) -> None:
    scope = scope_fixture(*scope_addresses)
    clock = ManualClock()
    factory = FakeAdapterFactory([], clock)

    with pytest.raises(ValueError, match="current selected interface addresses changed"):
        collect_mdns_candidates(
            scope,
            adapter_factory=factory,
            clock=clock,
            interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
                interface_index=7,
                addresses=current_addresses,
            ),
        )

    assert factory.calls == 0


@pytest.mark.parametrize(
    "forgery",
    ["interface_id", "addresses", "active_hosts", "network", "passive_or_manual_only"],
)
def test_forged_scope_is_rejected_before_adapter_creation(forgery: str) -> None:
    scope = scope_fixture()
    if forgery == "interface_id":
        scope = replace(
            scope,
            interface=replace(scope.interface, interface_id="sha256:" + "0" * 64),
        )
    elif forgery == "addresses":
        scope = replace(
            scope,
            interface=replace(scope.interface, addresses=("192.168.50.99/24",)),
        )
    elif forgery == "active_hosts":
        scope = replace(scope, active_hosts=("8.8.8.8",))
    elif forgery == "network":
        scope = replace(scope, network="192.168.50.0/25")
    else:
        scope = replace(scope, passive_or_manual_only=True)
    clock = ManualClock()
    factory = FakeAdapterFactory([], clock)

    with pytest.raises(ValueError, match="canonical confirmed scope"):
        collect_mdns_candidates(
            scope,
            adapter_factory=factory,
            clock=clock,
            interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
                interface_index=7,
                addresses=scope.interface.addresses,
            ),
        )

    assert factory.calls == 0


@pytest.mark.parametrize(
    "extra_address",
    [
        "8.8.8.8/32",
        "127.0.0.1/8",
        "224.0.0.1/4",
        "2001:4860:4860::8888/128",
        "::1/128",
        "ff02::1/16",
    ],
)
def test_canonical_interface_with_an_extra_ineligible_address_fails_before_factory(
    extra_address: str,
) -> None:
    lookalike = NetworkInterface.from_addresses(
        os_identity="darwin:en0",
        display_name="Wi-Fi",
        addresses=("192.168.50.7/24", extra_address),
    )
    scope = PrivateScanScope.from_request(lookalike, "192.168.50.0/24")
    clock = ManualClock()
    factory = FakeAdapterFactory([], clock)

    with pytest.raises(ValueError, match="canonical confirmed scope"):
        collect_mdns_candidates(
            scope,
            adapter_factory=factory,
            clock=clock,
            interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
                interface_index=7,
                addresses=scope.interface.addresses,
            ),
        )

    assert factory.calls == 0


@pytest.mark.parametrize("forged_address", ["192.168.50.7/0", "fd00::7/1"])
def test_interface_address_with_a_broadened_nonprivate_prefix_fails_before_factory(
    forged_address: str,
) -> None:
    lookalike = NetworkInterface.from_addresses(
        os_identity="darwin:en0",
        display_name="Wi-Fi",
        addresses=(forged_address,),
    )
    network = "192.168.50.0/24" if "." in forged_address else "fd00::/64"
    scope = PrivateScanScope.from_request(lookalike, network)
    clock = ManualClock()
    factory = FakeAdapterFactory([], clock)

    with pytest.raises(ValueError, match="canonical confirmed scope"):
        collect_mdns_candidates(
            scope,
            adapter_factory=factory,
            clock=clock,
            interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
                interface_index=7,
                addresses=scope.interface.addresses,
            ),
        )

    assert factory.calls == 0


@pytest.mark.parametrize("interface_index", [0, -1, True, "7"])
def test_invalid_ipv6_interface_index_is_rejected_before_adapter_creation(
    interface_index: object,
) -> None:
    scope = scope_fixture("fd00::2/64", network="fd00::/64")
    clock = ManualClock()
    factory = FakeAdapterFactory([], clock)

    with pytest.raises(ValueError, match="verified numeric interface index"):
        collect_mdns_candidates(
            scope,
            adapter_factory=factory,
            clock=clock,
            interface_state_resolver=lambda _identity: SimpleNamespace(
                interface_index=interface_index,
                addresses=scope.interface.addresses,
            ),
        )

    assert factory.calls == 0


@pytest.mark.parametrize("interface_index", [0, -1, True, "7"])
def test_invalid_ipv4_interface_index_is_rejected_before_adapter_creation(
    interface_index: object,
) -> None:
    scope = scope_fixture()
    clock = ManualClock()
    factory = FakeAdapterFactory([], clock)

    with pytest.raises(ValueError, match="verified numeric interface index"):
        collect_mdns_candidates(
            scope,
            adapter_factory=factory,
            clock=clock,
            interface_state_resolver=lambda _identity: SimpleNamespace(
                interface_index=interface_index,
                addresses=scope.interface.addresses,
            ),
        )

    assert factory.calls == 0


def test_duplicate_reconciliation_and_top_k_are_independent_of_callback_order() -> None:
    duplicate_a = record(
        service_type="_ollama._tcp.local.",
        instance="Zulu",
        properties={b"display_name": b"Alpha", b"version": b"2"},
    )
    duplicate_b = record(
        service_type="_kestrel-model._tcp.local.",
        instance="Able",
        properties={b"display_name": b"Beta", b"product": b"Local inference server"},
    )
    flood = [
        record(
            addresses=(f"192.168.50.{host}",),
            port=port,
            instance=f"Node-{host}-{port}",
        )
        for host in range(1, 151)
        for port in (8000, 11434)
    ]

    first, _factory = collect_fake([duplicate_a, duplicate_b, *flood])
    second, _factory = collect_fake(list(reversed([duplicate_a, duplicate_b, *flood])))

    assert first == second
    assert len(first) == MAX_ACTIVE_HOSTS
    assert [(candidate.address, candidate.port) for candidate in first] == sorted(
        ((candidate.address, candidate.port) for candidate in first),
        key=lambda item: (tuple(int(part) for part in item[0].split(".")), item[1]),
    )
    assert (first[-1].address, first[-1].port) == ("192.168.50.128", 11434)
    reconciled = next(
        candidate
        for candidate in first
        if candidate.address == "192.168.50.20" and candidate.port == 11434
    )
    assert reconciled.service_type == "_kestrel-model._tcp.local."
    assert reconciled.instance_name == "Able"
    assert dict(reconciled.metadata) == {
        "display_name": "Alpha",
        "product": "Local inference server",
        "version": "2",
    }


@pytest.mark.parametrize(
    "hostile_record",
    [
        record(properties={b"display_name": b"bad\xff"}),
        record(properties={b"display_name": b"line\nbreak"}),
        record(properties={b"token": b"secret"}),
        record(properties={b"display_name": b"Bearer secret"}),
        record(properties={b"display_name": b"https://host.local/api"}),
        record(properties={b"display_name": b"model.example"}),
        record(properties={b"display_name": b"model.dev"}),
        record(properties={b"display_name": b"host.corp"}),
        record(properties={b"display_name": b"192.168.50.20"}),
        record(properties={b"display_name": b"fd00::20"}),
        record(properties={b"hostname": b"model.local"}),
        record(properties={"display_name": b"text"}),
        record(properties={b"display_name": "text"}),
        record(properties={b"description": b"x" * MAX_MDNS_METADATA_BYTES}),
        # Character-count bounds would admit this; canonical UTF-8 bytes exceed 4096.
        record(properties={b"description": ("é" * 2030).encode("utf-8")}),
        # Model/catalog identity is not public display metadata authority.
        record(properties={b"model": b"llama3.2:latest"}),
        record(instance="https://host.local/api"),
        record(instance="Cafe\u0301"),
        record(properties={b"display_name": "Cafe\u0301".encode("utf-8")}),
    ],
)
def test_untrusted_txt_and_instance_metadata_fail_closed(hostile_record: MdnsRecord) -> None:
    candidates, _factory = collect_fake([hostile_record])

    assert candidates == ()


def test_candidate_metadata_is_canonical_bounded_and_immutable() -> None:
    candidates, _factory = collect_fake(
        [
            record(
                instance="Studio One",
                properties={
                    b"description": "Local inference — owner display".encode(),
                    b"version": b"1.2.3",
                    b"product": b"Desktop inference service",
                    b"display_name": b"Studio One",
                },
            )
        ]
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.provider_hint is None
    assert len(candidate.metadata_json.encode("utf-8")) <= MAX_MDNS_METADATA_BYTES
    assert json.loads(candidate.metadata_json) == {
        "instance_name": "Studio One",
        "service_type": "_kestrel-model._tcp.local.",
        "txt": {
            "description": "Local inference — owner display",
            "display_name": "Studio One",
            "product": "Desktop inference service",
            "version": "1.2.3",
        },
    }
    with pytest.raises(TypeError):
        candidate.metadata["display_name"] = "mutated"  # type: ignore[index]
    with pytest.raises((AttributeError, FrozenInstanceError)):
        candidate.provider_hint = "ollama"  # type: ignore[misc]
    with pytest.raises((AttributeError, FrozenInstanceError)):
        candidate.address = "8.8.8.8"  # type: ignore[misc]


def test_display_value_limit_counts_multibyte_utf8_bytes() -> None:
    accepted, _factory = collect_fake(
        [record(properties={b"description": ("é" * 150).encode("utf-8")})]
    )
    rejected, _factory = collect_fake(
        [record(properties={b"description": ("é" * 151).encode("utf-8")})]
    )

    assert len(accepted) == 1
    assert rejected == ()


class CloseBoundarySession:
    def __init__(
        self,
        callback: Callable[[MdnsRecord], None],
        clock: ManualClock,
    ) -> None:
        self._callback = callback
        self._clock = clock
        self._release_callback = threading.Event()
        self._callback_started = threading.Event()
        self._worker: threading.Thread | None = None
        self.close_calls = 0

    def wait(self, seconds: float) -> None:
        def late_callback() -> None:
            self._callback_started.set()
            assert self._release_callback.wait(timeout=1.0)
            self._callback(record(instance="Late"))

        self._worker = threading.Thread(target=late_callback)
        self._worker.start()
        assert self._callback_started.wait(timeout=1.0)
        self._clock.advance(seconds)

    def close(self) -> None:
        self.close_calls += 1
        self._release_callback.set()
        assert self._worker is not None
        self._worker.join(timeout=1.0)
        assert not self._worker.is_alive()


def test_deadline_closes_admissions_before_a_concurrent_callback_is_joined() -> None:
    clock = ManualClock()
    session: CloseBoundarySession | None = None

    def factory(
        _binding: MdnsBinding,
        _service_types: tuple[str, ...],
        callback: Callable[[MdnsRecord], None],
    ) -> CloseBoundarySession:
        nonlocal session
        session = CloseBoundarySession(callback, clock)
        return session

    candidates = collect_mdns_candidates(
        (scope := scope_fixture()),
        adapter_factory=factory,
        clock=clock,
        interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
            interface_index=7,
            addresses=scope.interface.addresses,
        ),
    )

    assert candidates == ()
    assert session is not None
    assert session.close_calls == 1


def test_callback_failures_are_contained_and_later_records_are_admitted() -> None:
    candidates, _factory = collect_fake(
        [
            record(properties={b"display_name": b"bad\xff"}),
            record(instance="Healthy", properties={b"display_name": b"Healthy"}),
        ]
    )

    assert [(candidate.instance_name, dict(candidate.metadata)) for candidate in candidates] == [
        ("Healthy", {"display_name": "Healthy"})
    ]


def test_one_callback_cannot_supply_an_unbounded_address_tuple() -> None:
    hostile = record(addresses=("192.168.50.20",) * (MAX_ACTIVE_HOSTS + 1))

    candidates, _factory = collect_fake([hostile])

    assert candidates == ()


def test_oversized_txt_mapping_is_rejected_before_iteration() -> None:
    class OversizedTxt(Mapping[object, object]):
        iterated = False

        def __len__(self) -> int:
            return 6

        def __iter__(self):  # type: ignore[no-untyped-def]
            self.iterated = True
            raise AssertionError("oversized TXT mapping must not be iterated")

        def __getitem__(self, _key: object) -> object:
            raise KeyError

    properties = OversizedTxt()

    candidates, _factory = collect_fake([record(properties=properties)])

    assert candidates == ()
    assert properties.iterated is False


def test_session_is_closed_when_bounded_wait_raises() -> None:
    clock = ManualClock()

    class RaisingSession(FakeSession):
        def wait(self, seconds: float) -> None:
            self.wait_seconds.append(seconds)
            raise RuntimeError("adapter wait failed")

    session: RaisingSession | None = None

    def factory(
        _binding: MdnsBinding,
        _service_types: tuple[str, ...],
        callback: Callable[[MdnsRecord], None],
    ) -> RaisingSession:
        nonlocal session
        session = RaisingSession(callback, (), clock)
        return session

    with pytest.raises(RuntimeError, match="adapter wait failed"):
        collect_mdns_candidates(
            scope_fixture(),
            adapter_factory=factory,
            clock=clock,
            interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
                interface_index=7,
                addresses=("192.168.50.7/24",),
            ),
        )

    assert session is not None
    assert session.close_calls == 1


def test_live_wrapper_uses_exact_binding_cache_only_reads_and_idempotent_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    service_type = "_kestrel-model._tcp.local."
    instance_name = f"Cached.{service_type}"
    server = "must-not-be-authority.local."
    txt_entry = b"display_name=Cached fixture"

    class FakeIPVersion:
        All = "all"
        V4Only = "v4"
        V6Only = "v6"

    class FakeLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback: Callable[[], None]) -> None:
            events.append("loop.call_soon_threadsafe")
            callback()

    class FakeTxtRecord:
        type = 16
        class_ = 1
        text = bytes([len(txt_entry)]) + txt_entry

        @property
        def properties(self) -> object:
            raise AssertionError("raw TXT must be validated before properties are exposed")

    class FakeZeroconf:
        def __init__(self, *, interfaces: list[str | int], ip_version: object) -> None:
            events.append(("zeroconf", tuple(interfaces), ip_version))
            self.loop = FakeLoop()
            self.cache = SimpleNamespace(
                cache={
                    instance_name.lower(): {
                        "srv": SimpleNamespace(
                            type=33,
                            class_=1,
                            port=11434,
                            server=server,
                        ),
                        "txt": FakeTxtRecord(),
                    },
                    server.lower(): {
                        "a": SimpleNamespace(
                            type=1,
                            class_=1,
                            address=bytes((192, 168, 50, 20)),
                            scope_id=None,
                        )
                    },
                }
            )

        def close(self) -> None:
            events.append("zeroconf.close")

    class FakeBrowser:
        def __init__(
            self,
            zeroconf: FakeZeroconf,
            service_types: list[str],
            listener: object,
        ) -> None:
            events.append(("browser", zeroconf, tuple(service_types)))
            self.zeroconf = zeroconf
            self.zc = zeroconf
            self.listener = listener
            self.queue = SimpleNamespace(put=self._queue_put)
            self._alive = True
            self.cancel_called = False
            fake_module.browser = self

        def cancel(self) -> None:
            self.cancel_called = True
            threading.Event().wait(timeout=5.0)

        def _queue_put(self, value: object) -> None:
            events.append(("browser.queue.put", value))
            self._alive = False

        def _async_cancel(self) -> None:
            events.append("browser._async_cancel")

        def join(self, *, timeout: float) -> None:
            events.append(("browser.join", timeout))

        def is_alive(self) -> bool:
            return self._alive

    fake_module = SimpleNamespace(
        IPVersion=FakeIPVersion,
        Zeroconf=FakeZeroconf,
        ServiceBrowser=FakeBrowser,
        browser=None,
    )
    monkeypatch.setattr(
        lan_mdns.importlib,
        "import_module",
        lambda name: fake_module if name == "zeroconf" else None,
    )
    observed: list[MdnsRecord] = []
    binding = MdnsBinding(
        ipv4_addresses=("10.8.0.7", "192.168.50.7"),
        ipv6_interface_index=41,
    )

    session = lan_mdns._live_adapter_factory(  # noqa: SLF001
        binding,
        ALLOWED_MODEL_SERVICE_TYPES,
        observed.append,
    )
    assert events[:2] == [
        ("zeroconf", ("10.8.0.7", "192.168.50.7", 41), "all"),
        ("browser", ANY, ALLOWED_MODEL_SERVICE_TYPES),
    ]
    assert fake_module.browser is not None
    fake_module.browser.listener.add_service(
        fake_module.browser.zeroconf,
        service_type,
        instance_name,
    )
    assert observed == [
        MdnsRecord(
            service_type=service_type,
            instance_name=instance_name,
            addresses=("192.168.50.20",),
            port=11434,
            properties={b"display_name": b"Cached fixture"},
            hostname=server,
        )
    ]

    session.close()
    session.close()

    assert fake_module.browser.cancel_called is False
    assert events[-5:] == [
        ("browser.queue.put", None),
        "loop.call_soon_threadsafe",
        "browser._async_cancel",
        ("browser.join", 0.25),
        "zeroconf.close",
    ]


def test_collection_never_calls_pinned_blocking_browser_cancel() -> None:
    events: list[object] = []
    cancel_release = threading.Event()

    class FakeLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback: Callable[[], None]) -> None:
            callback()

    class FakeZeroconf:
        loop = FakeLoop()

        def close(self) -> None:
            events.append("zeroconf.close")

    class FakeBrowser:
        def __init__(self) -> None:
            self.zc = FakeZeroconf()
            self.queue = SimpleNamespace(put=self._queue_put)
            self.alive = True
            self.cancel_called = False

        def _queue_put(self, value: object) -> None:
            assert value is None
            self.alive = False

        def _async_cancel(self) -> None:
            events.append("browser._async_cancel")

        def join(self, *, timeout: float) -> None:
            assert timeout == 0.25

        def is_alive(self) -> bool:
            return self.alive

        def cancel(self) -> None:
            self.cancel_called = True
            cancel_release.wait(timeout=5.0)

    browser = FakeBrowser()
    scope = scope_fixture()

    class ExpiredClock:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            return 100.0 if self.calls == 1 else 103.0

    started = time.monotonic()
    candidates = collect_mdns_candidates(
        scope,
        adapter_factory=lambda _binding, _types, _callback: lan_mdns._LiveMdnsSession(  # noqa: SLF001
            browser.zc,
            browser,
        ),
        clock=ExpiredClock(),
        interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
            interface_index=7,
            addresses=scope.interface.addresses,
        ),
    )
    elapsed = time.monotonic() - started

    assert candidates == ()
    assert browser.cancel_called is False
    assert elapsed < 1.0
    assert events == ["browser._async_cancel", "zeroconf.close"]


def test_cleanup_timeout_keeps_admissions_closed_and_does_not_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_join = threading.Event()
    join_started = threading.Event()
    close_calls = 0
    queue_put_calls = 0
    captured_callback: Callable[[MdnsRecord], None] | None = None
    session: lan_mdns._LiveMdnsSession | None = None  # noqa: SLF001

    class FakeLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback: Callable[[], None]) -> None:
            callback()

    class FakeZeroconf:
        loop = FakeLoop()

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    class StuckBrowser:
        def __init__(self, zeroconf: FakeZeroconf) -> None:
            self.zc = zeroconf
            self.queue = SimpleNamespace(put=self._queue_put)
            self.alive = True

        def _queue_put(self, value: object) -> None:
            nonlocal queue_put_calls
            assert value is None
            queue_put_calls += 1

        def _async_cancel(self) -> None:
            return

        def join(self, *, timeout: float) -> None:
            assert timeout == 0.25
            join_started.set()
            assert release_join.wait(timeout=5.0)
            self.alive = False

        def is_alive(self) -> bool:
            return self.alive

        def cancel(self) -> None:
            raise AssertionError("bounded cleanup must never call browser.cancel()")

    normalization_calls = 0
    real_normalize_record = lan_mdns._normalize_record  # noqa: SLF001

    def track_normalization(
        callback_record: MdnsRecord,
        callback_scope: PrivateScanScope,
        callback_binding: MdnsBinding,
    ) -> tuple[LanCandidate, ...]:
        nonlocal normalization_calls
        normalization_calls += 1
        return real_normalize_record(callback_record, callback_scope, callback_binding)

    monkeypatch.setattr(lan_mdns, "_normalize_record", track_normalization)
    scope = scope_fixture()

    class ExpiredClockForCleanup:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            return 100.0 if self.calls == 1 else 103.0

    def factory(
        _binding: MdnsBinding,
        _service_types: tuple[str, ...],
        callback: Callable[[MdnsRecord], None],
    ) -> lan_mdns._LiveMdnsSession:  # noqa: SLF001
        nonlocal captured_callback, session
        captured_callback = callback
        zeroconf = FakeZeroconf()
        session = lan_mdns._LiveMdnsSession(zeroconf, StuckBrowser(zeroconf))  # noqa: SLF001
        return session

    started = time.monotonic()
    try:
        with pytest.raises(
            TimeoutError,
            match="cleanup did not settle within its bounded window",
        ):
            collect_mdns_candidates(
                scope,
                adapter_factory=factory,
                clock=ExpiredClockForCleanup(),
                interface_state_resolver=lambda _identity: lan_mdns.CurrentInterfaceState(
                    interface_index=7,
                    addresses=scope.interface.addresses,
                ),
            )
        elapsed = time.monotonic() - started
        assert join_started.is_set()
        assert elapsed < 1.0
        assert session is not None
        assert session._cleanup_thread is not None  # noqa: SLF001
        assert session._cleanup_thread.daemon is True  # noqa: SLF001

        assert captured_callback is not None
        captured_callback(record(instance="Too late"))
        assert normalization_calls == 0
        assert close_calls == 0
    finally:
        release_join.set()

    assert session is not None
    assert session._cleanup_done.wait(timeout=1.0)  # noqa: SLF001
    session.close()
    session.close()
    assert queue_put_calls == 1
    assert close_calls == 1


def test_pinned_browser_shape_drift_closes_zeroconf_and_fails_visible() -> None:
    close_calls = 0

    class FakeLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback: Callable[[], None]) -> None:
            callback()

    class FakeZeroconf:
        loop = FakeLoop()

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    class DriftedBrowser:
        def __init__(self, zeroconf: FakeZeroconf) -> None:
            self.zc = zeroconf

        def _async_cancel(self) -> None:
            return

        def join(self, *, timeout: float) -> None:
            assert timeout == 0.25

        def is_alive(self) -> bool:
            return False

    zeroconf = FakeZeroconf()
    session = lan_mdns._LiveMdnsSession(zeroconf, DriftedBrowser(zeroconf))  # noqa: SLF001

    with pytest.raises(AttributeError, match="queue"):
        session.close()
    with pytest.raises(AttributeError, match="queue"):
        session.close()

    assert close_calls == 1
    assert session._cleanup_thread is not None  # noqa: SLF001
    assert session._cleanup_thread.daemon is True  # noqa: SLF001


def test_live_listener_rejects_oversized_address_cache_before_iteration() -> None:
    class OversizedBucket(Mapping[object, object]):
        iterated = False

        def __len__(self) -> int:
            return MAX_ACTIVE_HOSTS + 1

        def __iter__(self):  # type: ignore[no-untyped-def]
            self.iterated = True
            raise AssertionError("oversized address cache must not be iterated")

        def __getitem__(self, _key: object) -> object:
            raise KeyError

    service_type = "_kestrel-model._tcp.local."
    instance_name = f"Cached.{service_type}"
    server = "cached-address.local."
    oversized = OversizedBucket()
    zeroconf = SimpleNamespace(
        cache=SimpleNamespace(
            cache={
                instance_name.lower(): {
                    "srv": SimpleNamespace(
                        type=33,
                        class_=1,
                        port=11434,
                        server=server,
                    )
                },
                server.lower(): oversized,
            }
        )
    )
    observed: list[MdnsRecord] = []
    listener = lan_mdns._LiveListener(  # noqa: SLF001
        observed.append,
        MdnsBinding(ipv4_addresses=("192.168.50.7",), ipv6_interface_index=None),
    )

    listener.add_service(zeroconf, service_type, instance_name)

    assert observed == []
    assert oversized.iterated is False


@pytest.mark.parametrize(
    "raw_txt",
    [
        b"x" * (MAX_MDNS_METADATA_BYTES + 1),
        b"".join(
            bytes([len(entry)]) + entry
            for entry in (
                b"display_name=a",
                b"description=b",
                b"vendor=c",
                b"product=d",
                b"version=e",
                b"sixth=f",
            )
        ),
        b"\x05abc",
    ],
    ids=("oversized", "too-many", "malformed"),
)
def test_live_listener_rejects_raw_txt_before_mapping_exposure(raw_txt: bytes) -> None:
    service_type = "_kestrel-model._tcp.local."
    instance_name = f"Cached.{service_type}"
    server = "cached-address.local."
    zeroconf = SimpleNamespace(
        cache=SimpleNamespace(
            cache={
                instance_name.lower(): {
                    "srv": SimpleNamespace(
                        type=33,
                        class_=1,
                        port=11434,
                        server=server,
                    ),
                    "txt": SimpleNamespace(type=16, class_=1, text=raw_txt),
                },
                server.lower(): {},
            }
        )
    )
    observed: list[MdnsRecord] = []
    listener = lan_mdns._LiveListener(  # noqa: SLF001
        observed.append,
        MdnsBinding(ipv4_addresses=("192.168.50.7",), ipv6_interface_index=None),
    )

    listener.add_service(zeroconf, service_type, instance_name)

    assert observed == []


def test_live_wrapper_closes_partial_zeroconf_when_browser_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeZeroconf:
        def __init__(self, **_kwargs: object) -> None:
            events.append("zeroconf.open")

        def close(self) -> None:
            events.append("zeroconf.close")

    class FailingBrowser:
        def __init__(self, *_args: object) -> None:
            events.append("browser.fail")
            raise RuntimeError("browser creation failed")

    fake_module = SimpleNamespace(
        IPVersion=SimpleNamespace(All="all", V4Only="v4", V6Only="v6"),
        Zeroconf=FakeZeroconf,
        ServiceBrowser=FailingBrowser,
    )
    monkeypatch.setattr(lan_mdns.importlib, "import_module", lambda _name: fake_module)

    with pytest.raises(RuntimeError, match="browser creation failed"):
        lan_mdns._live_adapter_factory(  # noqa: SLF001
            MdnsBinding(ipv4_addresses=("192.168.50.7",), ipv6_interface_index=None),
            ALLOWED_MODEL_SERVICE_TYPES,
            lambda _record: None,
        )

    assert events == ["zeroconf.open", "browser.fail", "zeroconf.close"]
