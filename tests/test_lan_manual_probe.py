from __future__ import annotations

import concurrent.futures
import re
import socket
from importlib import import_module
from typing import Any

import pytest

import nested_memvid_agent.lan_scanner as lan_scanner_module
from nested_memvid_agent.lan_discovery_models import NetworkInterface


def interface_fixture(*addresses: str) -> NetworkInterface:
    return NetworkInterface.from_addresses(
        os_identity="darwin:en7",
        display_name="Private adapter",
        addresses=addresses or ("192.168.50.7/24",),
    )


def preview_manual_host(
    interface_id: str,
    host: str,
    port: int,
    *,
    interfaces: tuple[NetworkInterface, ...],
    resolver: Any,
) -> Any:
    """Call the wished-for pure Task 7B preview helper after test collection."""

    module = import_module("nested_memvid_agent.lan_manual_probe")
    return module.preview_manual_host(
        interface_id,
        host,
        port,
        interfaces=interfaces,
        resolver=resolver,
    )


class RecordingResolver:
    def __init__(self, answers: tuple[str, ...]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, host: str) -> tuple[str, ...]:
        self.calls.append(host)
        return self.answers


class HostilePort(int):
    def __format__(self, format_spec: str) -> str:
        del format_spec
        raise AssertionError("hostile port formatting crossed validation")


def test_literal_manual_preview_never_invokes_dns_and_returns_one_canonical_option() -> None:
    interface = interface_fixture()

    def resolver(_host: str) -> tuple[str, ...]:
        raise AssertionError("literal manual preview must never invoke DNS")

    preview = preview_manual_host(
        interface.interface_id,
        "192.168.50.8",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert (
        preview.interface_id,
        preview.port,
        preview.resolved_addresses,
        preview.requires_confirmation,
    ) == (
        interface.interface_id,
        5001,
        ("192.168.50.8",),
        True,
    )


@pytest.mark.parametrize(
    ("interface_address", "literal"),
    [
        ("169.254.10.7/16", "169.254.10.8"),
        ("fd00::7/64", "fd00::8"),
        ("fe80::7/64", "fe80::8"),
    ],
)
def test_link_local_and_ipv6_literal_previews_also_skip_dns(
    interface_address: str,
    literal: str,
) -> None:
    interface = interface_fixture(interface_address)

    def resolver(_host: str) -> tuple[str, ...]:
        raise AssertionError("literal manual preview must never invoke DNS")

    preview = preview_manual_host(
        interface.interface_id,
        literal,
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert preview.resolved_addresses == (literal,)


def test_named_manual_preview_resolves_once_sorts_answers_and_retains_no_raw_hostname() -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.9", "192.168.50.8"))

    preview = preview_manual_host(
        interface.interface_id,
        "model-box.local",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert resolver.calls == ["model-box.local"]
    assert preview.resolved_addresses == ("192.168.50.8", "192.168.50.9")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", preview.host_input_digest)
    assert not hasattr(preview, "host")
    assert "model-box.local" not in repr(preview)


def test_named_manual_preview_maps_runtime_failure_to_sanitized_resolver_unavailability() -> None:
    interface = interface_fixture()
    module = import_module("nested_memvid_agent.lan_manual_probe")
    unavailable = module.ManualHostResolverUnavailable

    def resolver(host: str) -> tuple[str, ...]:
        raise RuntimeError(f"resolver unavailable for {host}")

    with pytest.raises(unavailable) as captured:
        module.preview_manual_host(
            interface.interface_id,
            "model-box.local",
            5001,
            interfaces=(interface,),
            resolver=resolver,
        )

    assert not issubclass(unavailable, ValueError)
    assert "model-box.local" not in str(captured.value)


def test_default_manual_resolver_maps_os_failure_to_sanitized_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = import_module("nested_memvid_agent.lan_manual_probe")
    unavailable = getattr(module, "ManualHostResolverUnavailable", None)
    resolver = getattr(module, "default_manual_host_resolver", None)

    assert isinstance(unavailable, type)
    assert issubclass(unavailable, RuntimeError)
    assert not issubclass(unavailable, ValueError)
    assert callable(resolver)

    def failed_getaddrinfo(*_args: object, **_kwargs: object) -> object:
        raise OSError("resolver failed for model-box.local")

    monkeypatch.setattr(socket, "getaddrinfo", failed_getaddrinfo)
    with pytest.raises(unavailable) as captured:
        resolver("model-box.local")

    assert "model-box.local" not in str(captured.value)


@pytest.mark.parametrize(
    "host",
    [
        "model-box",
        "model-box.local",
        "rack.model-box.local",
        "model-box.lan",
        "model-box.internal",
        "model-box.home.arpa",
    ],
)
def test_manual_preview_accepts_only_canonical_local_name_forms(host: str) -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.8",))

    preview = preview_manual_host(
        interface.interface_id,
        host,
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert preview.resolved_addresses == ("192.168.50.8",)
    assert resolver.calls == [host]


def test_ipv6_only_local_name_resolves_once_to_sorted_attached_ipv6_literals() -> None:
    interface = interface_fixture("fd00::7/64", "fe80::7/64")
    resolver = RecordingResolver(("fe80::9", "fd00::8"))

    preview = preview_manual_host(
        interface.interface_id,
        "model-box.local",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert resolver.calls == ["model-box.local"]
    assert preview.resolved_addresses == ("fd00::8", "fe80::9")


@pytest.mark.parametrize(
    ("interface_address", "literal"),
    (
        ("192.168.50.7/24", "8.8.8.8"),
        ("192.168.50.7/24", "127.0.0.1"),
        ("192.168.50.7/24", "224.0.0.1"),
        ("192.168.50.7/24", "0.0.0.0"),
        ("192.168.50.7/24", "192.0.2.1"),
        ("192.168.50.7/24", "192.168.51.8"),
        ("fd00::7/64", "2001:4860::8888"),
        ("fd00::7/64", "::1"),
        ("fd00::7/64", "ff02::1"),
        ("fd00::7/64", "::"),
        ("fd00::7/64", "2001:db8::1"),
        ("fd00::7/64", "fd01::8"),
    ),
)
def test_ineligible_or_out_of_interface_literals_fail_before_every_boundary(
    interface_address: str,
    literal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interface = interface_fixture(interface_address)
    resolver = RecordingResolver(("192.168.50.8",))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("rejected literal crossed the preview-only boundary")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, "submit", forbidden)
    monkeypatch.setattr(lan_scanner_module, "probe_lan_endpoint", forbidden)
    monkeypatch.setattr(
        lan_scanner_module,
        "probe_manual_lan_endpoint",
        forbidden,
        raising=False,
    )
    manual_probe_module = import_module("nested_memvid_agent.lan_manual_probe")
    monkeypatch.setattr(
        manual_probe_module,
        "probe_manual_lan_endpoint",
        forbidden,
        raising=False,
    )

    with pytest.raises((TypeError, ValueError)) as captured:
        manual_probe_module.preview_manual_host(
            interface.interface_id,
            literal,
            5001,
            interfaces=(interface,),
            resolver=resolver,
        )

    assert resolver.calls == []
    assert literal not in str(captured.value)


@pytest.mark.parametrize(
    "host",
    [
        "https://model-box.local",
        "user@model-box.local",
        "token=secret.local",
        "model-box.local/path",
        "model-box.local:5001",
        "model-box.local.",
        "Model-Box.local",
        "localhost",
        "localhost.local",
        "example.com",
        "-model.local",
        "model-.local",
        "model..local",
        "m" * 64 + ".local",
        "m" * 250 + ".local",
        " model-box.local",
        "model-box.local ",
        "",
        "m\x00odel.local",
        "mödél.local",
        "fe80::8%en7",
    ],
)
def test_hostile_or_public_name_shapes_fail_before_dns(host: str) -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.8",))

    with pytest.raises((TypeError, ValueError)) as captured:
        preview_manual_host(
            interface.interface_id,
            host,
            5001,
            interfaces=(interface,),
            resolver=resolver,
        )

    assert resolver.calls == []
    if host:
        assert host not in str(captured.value)


@pytest.mark.parametrize(
    "host",
    [
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456.local",
    ],
)
def test_credential_shaped_names_fail_without_reaching_dns_or_error_text(host: str) -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.8",))

    with pytest.raises(ValueError) as captured:
        preview_manual_host(
            interface.interface_id,
            host,
            5001,
            interfaces=(interface,),
            resolver=resolver,
        )

    assert resolver.calls == []
    assert host not in str(captured.value)


@pytest.mark.parametrize(
    "answers",
    [
        ["192.168.50.8"],
        (),
        (3232248328,),
        (b"\xc0\xa8\x32\x08",),
        (True,),
        (object(),),
        ("192.168.50.8", "192.168.50.8"),
        tuple(f"192.168.50.{index}" for index in range(8, 25)),
        ("192.168.50.8", "8.8.8.8"),
        ("192.168.50.8", "127.0.0.1"),
        ("192.168.50.8", "224.0.0.1"),
        ("192.168.50.8", "0.0.0.0"),
        ("192.168.50.8", "192.0.2.1"),
        ("192.168.50.8", "192.168.51.8"),
        ("192.168.50.8", "model-box.local"),
        ("192.168.50.8", "fe80::8%en7"),
        ("192.168.50.8", "fd00::8"),
    ],
)
def test_one_hostile_or_noncanonical_dns_answer_rejects_the_complete_preview(
    answers: object,
) -> None:
    interface = interface_fixture()
    calls: list[str] = []

    def resolver(host: str) -> object:
        calls.append(host)
        return answers

    with pytest.raises(ValueError) as captured:
        preview_manual_host(
            interface.interface_id,
            "model-box.local",
            5001,
            interfaces=(interface,),
            resolver=resolver,
        )

    assert calls == ["model-box.local"]
    assert "model-box.local" not in str(captured.value)


def test_mixed_family_answers_are_retained_when_both_families_are_attached() -> None:
    interface = interface_fixture("192.168.50.7/24", "fd00::7/64")
    resolver = RecordingResolver(("192.168.50.8", "fd00::8"))

    preview = preview_manual_host(
        interface.interface_id,
        "model-box.local",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert resolver.calls == ["model-box.local"]
    assert preview.resolved_addresses == ("192.168.50.8", "fd00::8")


def test_sixteen_unique_attached_answers_are_accepted_at_the_exact_cap() -> None:
    interface = interface_fixture()
    answers = tuple(f"192.168.50.{index}" for index in range(8, 24))
    resolver = RecordingResolver(answers)

    preview = preview_manual_host(
        interface.interface_id,
        "model-box.local",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert resolver.calls == ["model-box.local"]
    assert len(preview.resolved_addresses) == 16
    assert preview.resolved_addresses == tuple(sorted(answers))


def test_host_input_digest_is_deterministic_and_binds_the_accepted_host() -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.8",))

    local_preview = preview_manual_host(
        interface.interface_id,
        "model-box.local",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )
    lan_preview = preview_manual_host(
        interface.interface_id,
        "model-box.lan",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )
    repeated_local_preview = preview_manual_host(
        interface.interface_id,
        "model-box.local",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert resolver.calls == ["model-box.local", "model-box.lan", "model-box.local"]
    assert local_preview.resolved_addresses == lan_preview.resolved_addresses
    assert local_preview.host_input_digest == repeated_local_preview.host_input_digest
    assert local_preview.host_input_digest != lan_preview.host_input_digest


@pytest.mark.parametrize("port", [False, True, 0, -1, 65536, 1.0, "5001"])
def test_invalid_manual_preview_port_fails_before_dns(port: object) -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.8",))

    with pytest.raises((TypeError, ValueError), match="port"):
        preview_manual_host(
            interface.interface_id,
            "model-box.local",
            port,
            interfaces=(interface,),
            resolver=resolver,
        )

    assert resolver.calls == []


def test_manual_preview_rejects_int_subclass_port_before_dns_or_formatting() -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.8",))

    with pytest.raises(ValueError, match="port"):
        preview_manual_host(
            interface.interface_id,
            "model-box.local",
            HostilePort(5001),
            interfaces=(interface,),
            resolver=resolver,
        )

    assert resolver.calls == []


def test_manual_preview_performs_no_probe_socket_or_executor_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.8",))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("manual preview crossed the no-probe boundary")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, "submit", forbidden)
    monkeypatch.setattr(lan_scanner_module, "probe_lan_endpoint", forbidden)
    monkeypatch.setattr(
        lan_scanner_module,
        "probe_manual_lan_endpoint",
        forbidden,
        raising=False,
    )
    manual_probe_module = import_module("nested_memvid_agent.lan_manual_probe")
    monkeypatch.setattr(
        manual_probe_module,
        "probe_manual_lan_endpoint",
        forbidden,
        raising=False,
    )

    preview = manual_probe_module.preview_manual_host(
        interface.interface_id,
        "model-box.local",
        5001,
        interfaces=(interface,),
        resolver=resolver,
    )

    assert preview.resolved_addresses == ("192.168.50.8",)
    assert resolver.calls == ["model-box.local"]


def test_manual_preview_rejects_unknown_interface_without_resolving() -> None:
    interface = interface_fixture()
    resolver = RecordingResolver(("192.168.50.8",))

    with pytest.raises(ValueError, match="interface"):
        preview_manual_host(
            "sha256:" + "0" * 64,
            "model-box.local",
            5001,
            interfaces=(interface,),
            resolver=resolver,
        )

    assert resolver.calls == []
