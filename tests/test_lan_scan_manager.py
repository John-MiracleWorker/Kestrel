from __future__ import annotations

import ast
import hashlib
import inspect
import json
import logging
import socket
import time
import urllib.request
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from threading import Barrier, Event, Lock, Thread, current_thread
from typing import Any

import pytest

from nested_memvid_agent.lan_discovery_models import NetworkInterface, ResolvedLanEndpoint
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_http_transport import (
    CurrentLanInterfaceInventory,
    CurrentLanInterfaceState,
    LanTransportError,
    LanTransportFailure,
)
from nested_memvid_agent.lan_scanner import (
    LanFailureCategory,
    LanScanProgress,
    Reachability,
    _make_observation,
    scan_lan_scope,
)
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.routing.lan_records import (
    LanObservationDraft,
    LanScanRecord,
    LanScanRevisionConflict,
)
from nested_memvid_agent.state_store import AgentStateStore

FIXED_OWNER = "owner:local-runtime:v1"
PREVIEW_DIGEST = "sha256:" + "b" * 64


def _task6() -> Any:
    return import_module("nested_memvid_agent.lan_scan_manager")


def _manual_endpoint_type() -> type[Any]:
    return import_module("nested_memvid_agent.lan_discovery_models").ManualLanEndpoint


def _manual_conflict_type() -> type[Exception]:
    return _task6().LanManualPreviewConflict


def _manual_limits(port: int) -> dict[str, object]:
    return {
        "mode": "manual",
        "exact_port": port,
        "max_active_hosts": 1,
        "max_scan_concurrency": 1,
        "tcp_connect_timeout_seconds": 0.75,
        "http_probe_timeout_seconds": 2.0,
        "total_scan_deadline_seconds": 45.0,
        "max_probe_response_bytes": 256 * 1024,
        "max_discovered_models": 8,
        "mdns_enabled": False,
    }


def _manual_preview_event(
    interface: NetworkInterface,
    *,
    address: str,
    port: int,
    preview_digest: str = PREVIEW_DIGEST,
    expires_at: str = "2099-08-01T12:00:30Z",
) -> dict[str, object]:
    suffix = 32 if ":" not in address else 128
    return {
        "schema": "kestrel.lan.scan-preview.manual.v1",
        "mode": "manual",
        "endpoint_kind": "manual",
        "observation_source": "manual",
        "owner_principal": FIXED_OWNER,
        "interface_id": interface.interface_id,
        "network": f"{address}/{suffix}",
        "limits": _manual_limits(port),
        "active_host_count": 1,
        "passive_or_manual_only": True,
        "port_count": 1,
        "exact_port": port,
        "mdns_status": "unavailable",
        "server_version": _task6().LAN_SERVER_VERSION,
        "contract_version": _task6().LAN_MANUAL_PREVIEW_CONTRACT_VERSION,
        "preview_digest": preview_digest,
        "expires_at": expires_at,
        "confirmed": True,
        "privacy_acknowledged": True,
    }


def _manual_probe_result(
    _scope: object,
    endpoint: object,
    *,
    scan_deadline: float,
    cancellation: object,
    clock: object,
) -> object:
    del scan_deadline, cancellation, clock
    return _make_observation(
        endpoint,
        reachability=Reachability.UNREACHABLE,
        failure_category=LanFailureCategory.TCP_REFUSED,
    )


class MutableUtcClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class MutableMonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class AdvancingMonotonicClock:
    def __init__(self, *, start: float = 100.0, step: float = 0.25) -> None:
        self.value = start
        self.step = step
        self.calls: list[float] = []

    def __call__(self) -> float:
        current = self.value
        self.calls.append(current)
        self.value += self.step
        return current


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _interface(*, address: str = "192.168.90.1/30") -> NetworkInterface:
    return NetworkInterface.from_addresses(
        os_identity="darwin:en90",
        display_name="Task 6 fixture",
        addresses=(address,),
    )


def _wait_for_terminal(manager: Any, scan_id: str, *, timeout: float = 3.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = manager.get(scan_id)
        if current is not None and current.is_terminal:
            return current
        time.sleep(0.005)
    raise AssertionError(f"scan did not terminalize: {scan_id}")


def _manager(
    tmp_path: Path,
    *,
    interface_enumerator: Any | None = None,
    mdns_availability: Any | None = None,
    mdns_collector: Any | None = None,
    scanner: Any | None = None,
    manual_resolver: Any | None = None,
    manual_scanner: Any | None = None,
    utc_clock: MutableUtcClock | None = None,
    monotonic_clock: Any | None = None,
    precommit_hook: Any | None = None,
    scan_id: str = "lan_task6",
    scan_id_factory: Any | None = None,
) -> tuple[Any, AgentStateStore, LanDiscoveryLedger, NetworkInterface]:
    task6 = _task6()
    interface = _interface()
    state = AgentStateStore(tmp_path / "state.db")
    utc = utc_clock or MutableUtcClock()
    ledger = LanDiscoveryLedger(
        state,
        utc_clock=utc,
        precommit_hook=precommit_hook,
    )
    monotonic = monotonic_clock or MutableMonotonicClock()
    manual_options: dict[str, object] = {}
    if manual_resolver is not None:
        manual_options["manual_resolver"] = manual_resolver
    if manual_scanner is not None:
        manual_options["manual_scanner"] = manual_scanner
    manager = task6.LanScanManager(
        ledger=ledger,
        interface_enumerator=(interface_enumerator or (lambda: (interface,))),
        mdns_availability=(mdns_availability or (lambda: task6.MdnsAvailability.AVAILABLE)),
        mdns_collector=mdns_collector,
        scanner=scanner,
        utc_clock=utc,
        monotonic_clock=monotonic,
        scan_id_factory=(scan_id_factory or (lambda: scan_id)),
        **manual_options,
    )
    return manager, state, ledger, interface


def _start_lifecycle(manager: Any) -> ThreadPoolExecutor:
    executor = ThreadPoolExecutor(max_workers=17, thread_name_prefix="task6-test-lan")
    manager.start_lifecycle(executor)
    return executor


def test_construction_is_inert_and_recovery_runs_only_after_lifecycle_start(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    task6 = _task6()
    interface = _interface()
    state = AgentStateStore(tmp_path / "state.db")
    ledger = LanDiscoveryLedger(state)
    draft = ledger.create_scan(
        scan_id="lan_interrupted",
        owner_principal=FIXED_OWNER,
        confirmed_interface_id=interface.interface_id,
        network="192.168.90.0/30",
        limits=task6.canonical_scan_limits(),
        preview_digest="sha256:" + "1" * 64,
        expected_revision=0,
    )
    ledger.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )

    manager = task6.LanScanManager(
        ledger=ledger,
        interface_enumerator=lambda: calls.append("enumerate") or (interface,),
        mdns_availability=lambda: calls.append("availability") or task6.MdnsAvailability.AVAILABLE,
        mdns_collector=lambda *_args, **_kwargs: calls.append("mdns"),
        scanner=lambda *_args, **_kwargs: calls.append("scanner"),
        scan_id_factory=lambda: "unused",
    )

    assert calls == []
    assert ledger.get_scan(draft.scan_id).status == "running"  # type: ignore[union-attr]
    executor = ThreadPoolExecutor(max_workers=17, thread_name_prefix="task6-inert")
    try:
        interrupted = manager.start_lifecycle(executor)
        assert [item.scan_id for item in interrupted] == [draft.scan_id]
        recovered = manager.get(draft.scan_id)
        assert recovered is not None
        assert recovered.status == "interrupted"
        assert recovered.terminal_receipt is not None
        assert recovered.terminal_receipt["evidence_complete"] is False
        assert recovered.terminal_receipt["unknown_inflight_count"] is None
        assert calls == []
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_shutdown_before_lifecycle_start_permanently_fences_recovery_and_executor(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    interface = _interface()
    state = AgentStateStore(tmp_path / "state.db")
    ledger = LanDiscoveryLedger(state)
    draft = ledger.create_scan(
        scan_id="lan_must_not_recover",
        owner_principal=FIXED_OWNER,
        confirmed_interface_id=interface.interface_id,
        network="192.168.90.0/30",
        limits=task6.canonical_scan_limits(),
        preview_digest="sha256:" + "2" * 64,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )
    manager = task6.LanScanManager(
        ledger=ledger,
        interface_enumerator=lambda: (interface,),
    )

    class SpyExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=17, thread_name_prefix="task6-rejected-start")
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            self.shutdown_calls.append((wait, cancel_futures))
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    assert manager.shutdown(timeout_seconds=0.0) is True
    executor = SpyExecutor()
    with pytest.raises(RuntimeError, match="^LAN lifecycle is shut down$"):
        manager.start_lifecycle(executor)

    assert executor.shutdown_calls == [(True, False)]
    with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
        executor.submit(lambda: None)
    assert ledger.get_scan(running.scan_id) == running
    assert manager.is_quiescent() is True
    with pytest.raises(RuntimeError, match="has not started"):
        manager.preview(interface.interface_id, "192.168.90.0/30")


def test_started_idle_lifecycle_shutdown_with_zero_timeout_remains_immediate(
    tmp_path: Path,
) -> None:
    manager, _state, _ledger, _interface = _manager(tmp_path)

    class SpyExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=17, thread_name_prefix="task6-idle-zero-shutdown")
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            self.shutdown_calls.append((wait, cancel_futures))
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    executor = SpyExecutor()
    manager.start_lifecycle(executor)
    try:
        assert manager.shutdown(timeout_seconds=0.0) is True
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True
    assert executor.shutdown_calls == [(True, False)]


def test_recovery_failure_retry_owns_and_shuts_down_the_replacement_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task6 = _task6()
    worker_entered = Event()

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        worker_entered.set()
        return ()

    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
        scanner=scanner,
    )
    recovery_calls = 0
    recover = ledger.interrupt_active_scans

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 1:
            raise RuntimeError("injected recovery failure")
        return recover(*args, **kwargs)

    monkeypatch.setattr(ledger, "interrupt_active_scans", fail_once)

    class SpyExecutor(ThreadPoolExecutor):
        def __init__(self, name: str) -> None:
            super().__init__(max_workers=17, thread_name_prefix=name)
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            self.shutdown_calls.append((wait, cancel_futures))
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    rejected = SpyExecutor("task6-recovery-rejected")
    with pytest.raises(RuntimeError, match="^injected recovery failure$"):
        manager.start_lifecycle(rejected)
    assert rejected.shutdown_calls == [(True, False)]

    replacement = SpyExecutor("task6-recovery-replacement")
    assert manager.start_lifecycle(replacement) == []
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    assert worker_entered.wait(timeout=2.0)
    assert _wait_for_terminal(manager, draft.scan_id).status == "completed"

    assert manager.shutdown(timeout_seconds=1.0) is True
    assert replacement.shutdown_calls == [(True, False)]
    with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
        replacement.submit(lambda: None)


def test_fixed_owner_preview_is_short_lived_restart_local_and_has_no_principal_input(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    utc = MutableUtcClock()
    manager, _state, _ledger, interface = _manager(tmp_path, utc_clock=utc)
    executor = _start_lifecycle(manager)
    del executor
    try:
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        assert authorization.owner_principal == FIXED_OWNER
        assert authorization.preview.interface_id == interface.interface_id
        assert authorization.preview_digest.startswith("sha256:")
        assert len(authorization.preview_digest) == 71
        assert authorization.mdns_availability is task6.MdnsAvailability.AVAILABLE
        assert "owner_principal" not in inspect.signature(manager.preview).parameters
        assert "owner_principal" not in inspect.signature(manager.create_draft).parameters
        assert "owner_principal" not in inspect.signature(manager.start).parameters

        draft = manager.create_draft(authorization)
        utc.advance(task6.LAN_PREVIEW_TTL_SECONDS + 0.001)
        with pytest.raises(task6.LanPreviewAuthorizationError, match="expired"):
            manager.start(
                draft.scan_id,
                expected_revision=draft.revision,
                authorization=authorization,
                preview_digest=authorization.preview_digest,
            )

        restarted, *_ = _manager(
            tmp_path / "restarted",
            utc_clock=utc,
            scan_id="lan_restarted",
        )
        restarted_executor = _start_lifecycle(restarted)
        del restarted_executor
        try:
            with pytest.raises(task6.LanPreviewAuthorizationError, match="live"):
                restarted.create_draft(authorization)
        finally:
            assert restarted.shutdown(timeout_seconds=1.0) is True
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_preview_digest_is_independently_derived_and_every_authority_field_is_bound(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    utc = MutableUtcClock()
    interface = _interface()
    mdns_status = [task6.MdnsAvailability.AVAILABLE]
    manager, _state, _ledger, _ = _manager(
        tmp_path,
        interface_enumerator=lambda: (interface,),
        mdns_availability=lambda: mdns_status[0],
        utc_clock=utc,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        interface_payload = {
            "interface_id": interface.interface_id,
            "os_identity": interface.os_identity,
            "addresses": list(interface.addresses),
        }
        payload = {
            "schema": "kestrel.lan.preview-authorization.v1",
            "owner_principal": FIXED_OWNER,
            "interface": interface_payload,
            "network": authorization.preview.network,
            "limits": asdict(authorization.preview.limits),
            "active_host_count": authorization.preview.active_host_count,
            "passive_or_manual_only": authorization.preview.passive_or_manual_only,
            "port_count": len(authorization.preview.port_matrix),
            "mdns_status": authorization.mdns_availability.value,
            "server_version": authorization.server_version,
            "contract_version": authorization.contract_version,
            "expires_at": _utc_text(authorization.expires_at),
        }
        expected = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
        )
        assert authorization.preview_digest == expected
        assert (
            task6.preview_authorization_digest(
                owner_principal=FIXED_OWNER,
                interface=interface,
                preview=authorization.preview,
                mdns_availability=authorization.mdns_availability,
                server_version=authorization.server_version,
                contract_version=authorization.contract_version,
                expires_at=authorization.expires_at,
            )
            == expected
        )

        for field, value in (
            ("owner_principal", "owner:lookalike"),
            ("mdns_status", "unavailable"),
            ("server_version", "kestrel-mutated"),
            ("contract_version", "kestrel.lan.preview-authorization.v999"),
            (
                "expires_at",
                _utc_text(authorization.expires_at + timedelta(microseconds=1)),
            ),
            ("network", "192.168.90.0/31"),
            ("active_host_count", authorization.preview.active_host_count + 1),
            ("passive_or_manual_only", not authorization.preview.passive_or_manual_only),
            ("port_count", len(authorization.preview.port_matrix) + 1),
            ("limits", {**asdict(authorization.preview.limits), "max_active_hosts": 255}),
            (
                "interface",
                {
                    **interface_payload,
                    "os_identity": "darwin:en-mutated",
                    "addresses": ["192.168.90.2/30"],
                },
            ),
        ):
            mutated = {**payload, field: value}
            mutated_digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        mutated,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            )
            assert mutated_digest != expected, field

        mdns_status[0] = task6.MdnsAvailability.UNAVAILABLE
        unavailable = manager.preview(interface.interface_id, "192.168.90.0/30")
        assert unavailable.preview_digest != authorization.preview_digest
        utc.advance(0.001)
        later = manager.preview(interface.interface_id, "192.168.90.0/30")
        assert later.preview_digest != unavailable.preview_digest
        narrower = manager.preview(interface.interface_id, "192.168.90.0/31")
        assert narrower.preview_digest != later.preview_digest
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_preview_expiry_equality_and_authorization_lookalikes_are_zero_write(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    utc = MutableUtcClock()
    manager, _state, ledger, interface = _manager(tmp_path, utc_clock=utc)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")

    with pytest.raises((TypeError, ValueError, task6.LanPreviewAuthorizationError)):
        manager.create_draft(asdict(authorization))

    draft = manager.create_draft(authorization)
    utc.value = authorization.expires_at
    before = ledger.get_scan(draft.scan_id)
    before_events = ledger.list_events(draft.scan_id)
    try:
        with pytest.raises(task6.LanPreviewAuthorizationError, match="expired"):
            manager.start(
                draft.scan_id,
                expected_revision=draft.revision,
                authorization=authorization,
                preview_digest=authorization.preview_digest,
            )
        assert ledger.get_scan(draft.scan_id) == before
        assert ledger.list_events(draft.scan_id) == before_events

        restarted, *_ = _manager(
            tmp_path / "other-process",
            utc_clock=utc,
            scan_id="lan_other_process",
        )
        _start_lifecycle(restarted)
        try:
            with pytest.raises(task6.LanPreviewAuthorizationError, match="live"):
                restarted.create_draft(authorization)
        finally:
            assert restarted.shutdown(timeout_seconds=1.0) is True
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_preview_authority_is_one_live_object_pruned_at_expiry_and_cleared_on_shutdown(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    utc = MutableUtcClock()
    interface = _interface(address="192.168.90.1/24")
    manager, _state, _ledger, _ = _manager(
        tmp_path,
        interface_enumerator=lambda: (interface,),
        utc_clock=utc,
    )
    _start_lifecycle(manager)

    first = manager.preview(interface.interface_id, "192.168.90.0/24")
    latest = first
    for _ in range(32):
        utc.advance(0.001)
        latest = manager.preview(interface.interface_id, "192.168.90.0/24")
        assert len(manager._authorizations) == 1  # noqa: SLF001
        assert next(iter(manager._authorizations.values())).authorization is latest  # noqa: SLF001

    with pytest.raises(task6.LanPreviewAuthorizationError, match="not live"):
        manager.create_draft(first)

    utc.value = latest.expires_at
    with pytest.raises(task6.LanPreviewAuthorizationError, match="expired"):
        manager.create_draft(latest)
    assert manager._authorizations == {}  # noqa: SLF001

    current = manager.preview(interface.interface_id, "192.168.90.0/24")
    assert manager._authorizations[id(current)].authorization is current  # noqa: SLF001
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert manager._authorizations == {}  # noqa: SLF001


def test_start_revalidates_expiry_after_inventory_before_any_durable_claim(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    utc = MutableUtcClock()
    interface = _interface()
    inventory_calls = 0
    expires_at: list[datetime] = []
    scanner_calls = 0

    def inventory() -> tuple[NetworkInterface, ...]:
        nonlocal inventory_calls
        inventory_calls += 1
        if inventory_calls == 2:
            utc.value = expires_at[0] + timedelta(microseconds=1)
        return (interface,)

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        nonlocal scanner_calls
        scanner_calls += 1
        return ()

    manager, _state, ledger, _ = _manager(
        tmp_path,
        interface_enumerator=inventory,
        scanner=scanner,
        utc_clock=utc,
    )
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    expires_at.append(authorization.expires_at)
    draft = manager.create_draft(authorization)
    utc.value = authorization.expires_at - timedelta(microseconds=1)

    with pytest.raises(task6.LanPreviewAuthorizationError, match="expired"):
        manager.start(
            draft.scan_id,
            expected_revision=draft.revision,
            authorization=authorization,
            preview_digest=authorization.preview_digest,
        )

    assert inventory_calls == 2
    assert scanner_calls == 0
    assert ledger.get_scan(draft.scan_id) == draft
    assert ledger.list_events(draft.scan_id) == []
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_start_rejects_digest_substitution_and_full_interface_inventory_drift(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    first = _interface()
    second = _interface(address="192.168.90.2/30")
    inventory = [first]
    manager, _state, _ledger, _interface_value = _manager(
        tmp_path,
        interface_enumerator=lambda: tuple(inventory),
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(first.interface_id, "192.168.90.0/30")
        draft = manager.create_draft(authorization)
        with pytest.raises(task6.LanPreviewAuthorizationError, match="digest"):
            manager.start(
                draft.scan_id,
                expected_revision=draft.revision,
                authorization=authorization,
                preview_digest="sha256:" + "9" * 64,
            )
        inventory[:] = [second]
        with pytest.raises(task6.LanPreviewAuthorizationError, match="interface"):
            manager.start(
                draft.scan_id,
                expected_revision=draft.revision,
                authorization=authorization,
                preview_digest=authorization.preview_digest,
            )
        assert manager.get(draft.scan_id) == draft
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_rejected_controller_submission_returns_the_durable_failed_record(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    manager, _state, _ledger, interface = _manager(
        tmp_path,
        mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
    )

    class RejectingExecutor:
        def __init__(self) -> None:
            self.submit_calls = 0
            self.shutdown_calls = 0

        def submit(self, *_args: Any, **_kwargs: Any) -> None:
            self.submit_calls += 1
            raise RuntimeError("injected executor rejection")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True
            assert cancel_futures is False
            self.shutdown_calls += 1

    executor = RejectingExecutor()
    manager.start_lifecycle(executor)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)

    terminal = manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )

    assert executor.submit_calls == 1
    assert terminal == manager.get(draft.scan_id)
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "worker_error"
    assert terminal.terminal_receipt is not None
    assert terminal.terminal_receipt["terminal_reason"] == "worker_error"
    assert manager.controller_count == 0
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert executor.shutdown_calls == 1


@pytest.mark.parametrize("mode", ("automatic", "manual"))
def test_submit_boundary_reraises_base_exception_after_durable_terminalization(
    tmp_path: Path,
    mode: str,
) -> None:
    class BaseRejectingExecutor:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def submit(self, *_args: Any, **_kwargs: Any) -> None:
            raise SystemExit("raw-secret-submit-system-exit")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True
            assert cancel_futures is False
            self.shutdown_calls += 1

    scan_id = "lan_" + "2" * 32
    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        scan_id=scan_id,
    )
    executor = BaseRejectingExecutor()
    manager.start_lifecycle(executor)
    if mode == "automatic":
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        draft = manager.create_draft(authorization)
        scan_id = draft.scan_id

        def submit_scan() -> object:
            return manager.start(
                draft.scan_id,
                expected_revision=draft.revision,
                authorization=authorization,
                preview_digest=authorization.preview_digest,
            )

    else:
        manual_authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )

        def submit_scan() -> object:
            return manager.confirm_manual(
                manual_authorization.preview_digest,
                "192.168.90.2",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )

    with pytest.raises(SystemExit, match="raw-secret-submit-system-exit"):
        submit_scan()

    terminal = ledger.get_scan(scan_id)
    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "worker_error"
    assert terminal.terminal_receipt is not None
    assert "raw-secret-submit-system-exit" not in json.dumps(
        terminal.terminal_receipt,
        sort_keys=True,
    )
    assert ledger.list_scans(status="running", owner_principal=FIXED_OWNER) == []
    assert manager.controller_count == 0
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert executor.shutdown_calls == 1


def test_rejected_controller_submission_crossing_deadline_retries_before_return(
    tmp_path: Path,
) -> None:
    task6 = _task6()

    class BoundaryClock:
        def __init__(self) -> None:
            self.values = iter((100.0, 144.999, 145.001, 145.001))
            self.calls: list[float] = []

        def __call__(self) -> float:
            value = next(self.values)
            self.calls.append(value)
            return value

    class RejectingExecutor:
        def submit(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("injected controller rejection")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True
            assert cancel_futures is False

    clock = BoundaryClock()
    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
        monotonic_clock=clock,
    )
    manager.start_lifecycle(RejectingExecutor())
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)

    terminal = manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )

    assert ledger.get_scan(draft.scan_id) == terminal
    assert manager.get(draft.scan_id) == terminal
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "deadline_expired"
    assert [event.event_type for event in ledger.list_events(draft.scan_id)] == [
        "scan_started",
        "scan_failed",
    ]
    assert clock.calls == [100.0, 144.999, 145.001, 145.001]
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_terminal_deadline_crossing_rolls_back_then_retries_as_expired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task6 = _task6()

    class BoundaryClock:
        def __init__(self) -> None:
            self.values = iter((100.0, 100.0, 144.999, 145.001, 145.001))
            self.calls: list[float] = []

        def __call__(self) -> float:
            value = next(self.values)
            self.calls.append(value)
            return value

    clock = BoundaryClock()
    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
        scanner=lambda *_args, **_kwargs: (),
        monotonic_clock=clock,
    )
    terminal_attempts: list[tuple[str, str, float | None]] = []
    commit_terminal = ledger.commit_scan_terminal

    def capture_terminal_attempt(*args: Any, **kwargs: Any) -> Any:
        terminal_attempts.append(
            (
                kwargs["status"],
                kwargs["terminal_reason"],
                kwargs["absolute_deadline"],
            )
        )
        return commit_terminal(*args, **kwargs)

    monkeypatch.setattr(ledger, "commit_scan_terminal", capture_terminal_attempt)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    handle = manager._active_scans[draft.scan_id]  # noqa: SLF001
    assert handle.controller_finished.wait(timeout=2.0)

    terminal = ledger.get_scan(draft.scan_id)
    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "deadline_expired"
    assert manager.get(draft.scan_id) == terminal
    assert terminal_attempts == [
        ("completed", "scan_complete", 145.0),
        ("failed", "deadline_expired", None),
    ]
    assert [event.event_type for event in ledger.list_events(draft.scan_id)] == [
        "scan_started",
        "scan_failed",
    ]
    assert clock.calls == [100.0, 100.0, 144.999, 145.001, 145.001]
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_terminal_precommit_hook_deadline_crossing_retries_as_expired(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    clock = MutableMonotonicClock()
    terminal_hook_calls = 0

    def cross_deadline(operation: str) -> None:
        nonlocal terminal_hook_calls
        if operation != "commit_scan_terminal":
            return
        terminal_hook_calls += 1
        if terminal_hook_calls == 1:
            clock.value = 145.0

    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
        scanner=lambda *_args, **_kwargs: (),
        monotonic_clock=clock,
        precommit_hook=cross_deadline,
    )
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )

    terminal = _wait_for_terminal(manager, draft.scan_id)
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "deadline_expired"
    assert terminal_hook_calls == 2
    assert [event.event_type for event in ledger.list_events(draft.scan_id)] == [
        "scan_started",
        "scan_failed",
    ]
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_two_managers_same_owner_have_one_durable_start_winner_and_one_submission(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    interface = _interface()
    state = AgentStateStore(tmp_path / "state.db")
    ledger_a = LanDiscoveryLedger(state)
    ledger_b = LanDiscoveryLedger(state)
    barrier = Barrier(2)
    submitted: list[str] = []
    submitted_event = Event()
    release = Event()

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        submitted.append("scan")
        submitted_event.set()
        release.wait(timeout=3)
        return ()

    managers = [
        task6.LanScanManager(
            ledger=ledger,
            interface_enumerator=lambda: (interface,),
            mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
            scanner=scanner,
            scan_id_factory=lambda value=value: value,
        )
        for ledger, value in ((ledger_a, "lan_a"), (ledger_b, "lan_b"))
    ]
    for manager in managers:
        _start_lifecycle(manager)
    authorizations = [
        manager.preview(interface.interface_id, "192.168.90.0/30") for manager in managers
    ]
    drafts = [
        manager.create_draft(authorization)
        for manager, authorization in zip(managers, authorizations, strict=True)
    ]
    outcomes: list[str] = []

    def start(index: int) -> None:
        barrier.wait()
        try:
            managers[index].start(
                drafts[index].scan_id,
                expected_revision=drafts[index].revision,
                authorization=authorizations[index],
                preview_digest=authorizations[index].preview_digest,
            )
        except task6.LanScanAdmissionConflict:
            outcomes.append("lost")
        else:
            outcomes.append("won")

    threads = [Thread(target=start, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    try:
        assert sorted(outcomes) == ["lost", "won"]
        assert submitted_event.wait(timeout=2.0)
        assert submitted == ["scan"]
        scans = ledger_a.list_scans(owner_principal=FIXED_OWNER)
        assert len(scans) == 2
        winners = [item for item in scans if item.status == "running"]
        losers = [item for item in scans if item.status == "draft"]
        assert len(winners) == 1
        assert len(losers) == 1
        assert winners[0].revision == 2
        assert losers[0].revision == 1
        assert [event.event_type for event in ledger_a.list_events(winners[0].scan_id)] == [
            "scan_started"
        ]
        assert ledger_a.list_events(losers[0].scan_id) == []
        assert ledger_a.list_observations(winners[0].scan_id) == []
        assert ledger_a.list_observations(losers[0].scan_id) == []
    finally:
        release.set()
        for manager in managers:
            assert manager.shutdown(timeout_seconds=5.0) is True


def test_stale_cancel_has_no_token_side_effect_and_committed_cancel_signals_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()

    def scanner(*_args: Any, cancellation: Any, **_kwargs: Any) -> tuple[()]:
        entered.set()
        while not cancellation.is_cancelled() and not release.wait(timeout=0.005):
            pass
        return ()

    manager, _state, ledger, interface = _manager(tmp_path, scanner=scanner)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    running = manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    assert entered.wait(timeout=2)
    token = manager._active_scans[draft.scan_id].cancellation  # noqa: SLF001
    durable_at_signal: list[tuple[str, tuple[str, ...]]] = []
    original_cancel = type(token).cancel

    def inspect_commit_before_signal(current_token: Any) -> None:
        if current_token is token:
            current = ledger.get_scan(draft.scan_id)
            assert current is not None
            durable_at_signal.append(
                (
                    current.status,
                    tuple(event.event_type for event in ledger.list_events(draft.scan_id)),
                )
            )
        original_cancel(current_token)

    monkeypatch.setattr(type(token), "cancel", inspect_commit_before_signal)

    try:
        stale_row = ledger.get_scan(draft.scan_id)
        stale_events = ledger.list_events(draft.scan_id)
        with pytest.raises(LanScanRevisionConflict):
            manager.cancel(draft.scan_id, expected_revision=draft.revision)
        assert token.is_cancelled() is False
        assert ledger.get_scan(draft.scan_id) == stale_row
        assert ledger.list_events(draft.scan_id) == stale_events
        cancelling = manager.cancel(draft.scan_id, expected_revision=running.revision)
        assert cancelling.status == "cancelling"
        assert durable_at_signal == [("cancelling", ("scan_started", "scan_cancel_requested"))]
        assert token.is_cancelled() is True
        terminal = _wait_for_terminal(manager, draft.scan_id)
        assert terminal.status == "cancelled"
    finally:
        release.set()
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_real_scanner_cancel_drains_admitted_work_before_terminal_receipt(
    tmp_path: Path,
) -> None:
    entered = Event()

    class BlockingTcp:
        def tcp_reachable(
            self,
            _scope: Any,
            _endpoint: Any,
            _source: Any,
            *,
            deadline: float,
            cancellation: Any,
        ) -> bool:
            del deadline
            entered.set()
            while not cancellation.is_cancelled():
                time.sleep(0.001)
            raise LanTransportError(LanTransportFailure.CANCELLED)

    def scanner(scope: Any, limits: Any, **kwargs: Any) -> tuple[Any, ...]:
        return scan_lan_scope(
            scope,
            limits,
            tcp_probe=BlockingTcp(),
            interface_inventory_resolver=lambda: CurrentLanInterfaceInventory(
                (
                    CurrentLanInterfaceState(
                        os_identity=scope.interface.os_identity,
                        interface_index=90,
                        addresses=scope.interface.addresses,
                    ),
                )
            ),
            **kwargs,
        )

    manager, _state, _ledger, interface = _manager(tmp_path, scanner=scanner)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    assert entered.wait(timeout=2)

    expected_admitted = len(authorization.preview.port_matrix)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        progress_events = [
            event for event in manager.events(draft.scan_id) if event.event_type == "scan_progress"
        ]
        if progress_events and progress_events[-1].payload["admitted_count"] == (expected_admitted):
            break
        time.sleep(0.005)
    else:
        raise AssertionError("real scanner did not durably admit its bounded work")

    current = manager.get(draft.scan_id)
    assert current is not None
    manager.cancel(draft.scan_id, expected_revision=current.revision)
    terminal = _wait_for_terminal(manager, draft.scan_id)
    assert terminal.status == "cancelled"
    assert terminal.terminal_receipt is not None
    assert terminal.terminal_receipt["admitted_count"] == expected_admitted
    assert terminal.terminal_receipt["completed_count"] == expected_admitted
    assert terminal.terminal_receipt["unknown_inflight_count"] == 0
    assert manager.shutdown(timeout_seconds=2.0) is True


def test_real_scanner_pre_admission_cancel_receipt_has_zero_admitted_work(
    tmp_path: Path,
) -> None:
    scanner_entered = Event()
    release_scanner = Event()

    class UnexpectedTcp:
        def tcp_reachable(self, *_args: Any, **_kwargs: Any) -> bool:
            raise AssertionError("pre-cancelled scan must not submit endpoint work")

    def scanner(scope: Any, limits: Any, **kwargs: Any) -> tuple[Any, ...]:
        scanner_entered.set()
        assert release_scanner.wait(timeout=2.0)
        return scan_lan_scope(
            scope,
            limits,
            tcp_probe=UnexpectedTcp(),
            interface_inventory_resolver=lambda: CurrentLanInterfaceInventory(
                (
                    CurrentLanInterfaceState(
                        os_identity=scope.interface.os_identity,
                        interface_index=90,
                        addresses=scope.interface.addresses,
                    ),
                )
            ),
            **kwargs,
        )

    manager, _state, ledger, interface = _manager(tmp_path, scanner=scanner)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    assert scanner_entered.wait(timeout=2.0)
    running = manager.get(draft.scan_id)
    assert running is not None
    manager.cancel(draft.scan_id, expected_revision=running.revision)
    release_scanner.set()

    terminal = _wait_for_terminal(manager, draft.scan_id)
    assert terminal.status == "cancelled"
    assert terminal.terminal_receipt is not None
    receipt = terminal.terminal_receipt
    assert receipt["planned_count"] == 8
    assert receipt["admitted_count"] == 0
    assert receipt["completed_count"] == 0
    assert receipt["observation_count"] == 0
    assert receipt["persisted_observation_count"] == 0
    assert ledger.list_observations(draft.scan_id) == []
    assert manager.shutdown(timeout_seconds=2.0) is True


def test_real_scanner_pre_admission_deadline_receipt_has_zero_admitted_work(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    clock = MutableMonotonicClock()

    class UnexpectedTcp:
        def tcp_reachable(self, *_args: Any, **_kwargs: Any) -> bool:
            raise AssertionError("expired scan must not submit endpoint work")

    def scanner(scope: Any, limits: Any, **kwargs: Any) -> tuple[Any, ...]:
        clock.value = 145.0
        return scan_lan_scope(
            scope,
            limits,
            tcp_probe=UnexpectedTcp(),
            interface_inventory_resolver=lambda: CurrentLanInterfaceInventory(
                (
                    CurrentLanInterfaceState(
                        os_identity=scope.interface.os_identity,
                        interface_index=90,
                        addresses=scope.interface.addresses,
                    ),
                )
            ),
            **kwargs,
        )

    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
        scanner=scanner,
        monotonic_clock=clock,
    )
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )

    terminal = _wait_for_terminal(manager, draft.scan_id)
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "deadline_expired"
    assert terminal.terminal_receipt is not None
    receipt = terminal.terminal_receipt
    assert receipt["planned_count"] == 8
    assert receipt["admitted_count"] == 0
    assert receipt["completed_count"] == 0
    assert receipt["observation_count"] == 0
    assert receipt["persisted_observation_count"] == 0
    assert ledger.list_observations(draft.scan_id) == []
    assert manager.shutdown(timeout_seconds=2.0) is True


def test_progress_persistence_fault_cancels_and_drains_without_invented_counts(
    tmp_path: Path,
) -> None:
    interface = _interface()
    entered = Event()
    faulted: list[str] = []
    progress_commits = 0

    def fail_first_progress(operation: str) -> None:
        nonlocal progress_commits
        if operation != "record_scan_progress":
            return
        progress_commits += 1
        if progress_commits == 2 and not faulted:
            faulted.append(operation)
            raise RuntimeError("hostile persistence detail")

    class BlockingTcp:
        def tcp_reachable(
            self,
            _scope: Any,
            _endpoint: Any,
            _source: Any,
            *,
            deadline: float,
            cancellation: Any,
        ) -> bool:
            del deadline
            entered.set()
            while not cancellation.is_cancelled():
                time.sleep(0.001)
            raise LanTransportError(LanTransportFailure.CANCELLED)

    def scanner(scope: Any, limits: Any, **kwargs: Any) -> tuple[Any, ...]:
        return scan_lan_scope(
            scope,
            limits,
            tcp_probe=BlockingTcp(),
            interface_inventory_resolver=lambda: CurrentLanInterfaceInventory(
                (
                    CurrentLanInterfaceState(
                        os_identity=scope.interface.os_identity,
                        interface_index=90,
                        addresses=scope.interface.addresses,
                    ),
                )
            ),
            **kwargs,
        )

    state = AgentStateStore(tmp_path / "state.db")
    ledger = LanDiscoveryLedger(state, precommit_hook=fail_first_progress)
    task6 = _task6()
    manager = task6.LanScanManager(
        ledger=ledger,
        interface_enumerator=lambda: (interface,),
        mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
        scanner=scanner,
        scan_id_factory=lambda: "lan_progress_fault",
    )
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    assert entered.wait(timeout=2)

    terminal = _wait_for_terminal(manager, draft.scan_id)
    assert faulted == ["record_scan_progress"]
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "worker_error"
    assert terminal.terminal_receipt is not None
    receipt = terminal.terminal_receipt
    assert receipt["admitted_count"] == 1
    assert receipt["completed_count"] == 1
    assert receipt["observation_count"] == 1
    assert receipt["unknown_inflight_count"] == 0
    assert manager.controller_count == 0
    assert manager.shutdown(timeout_seconds=2.0) is True


def test_partial_executor_rejection_drains_admitted_probe_before_failed_receipt(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    interface = _interface()
    probe_entered = Event()
    active_probes = 0

    class CancellingTcp:
        def tcp_reachable(
            self,
            _scope: Any,
            _endpoint: Any,
            _source: Any,
            *,
            deadline: float,
            cancellation: Any,
        ) -> bool:
            nonlocal active_probes
            del deadline
            active_probes += 1
            probe_entered.set()
            try:
                while not cancellation.is_cancelled():
                    time.sleep(0.001)
            finally:
                active_probes -= 1
            raise LanTransportError(LanTransportFailure.CANCELLED)

    def scanner(scope: Any, limits: Any, **kwargs: Any) -> tuple[Any, ...]:
        return scan_lan_scope(
            scope,
            limits,
            tcp_probe=CancellingTcp(),
            interface_inventory_resolver=lambda: CurrentLanInterfaceInventory(
                (
                    CurrentLanInterfaceState(
                        os_identity=scope.interface.os_identity,
                        interface_index=90,
                        addresses=scope.interface.addresses,
                    ),
                )
            ),
            **kwargs,
        )

    class RejectSecondProbeExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=17, thread_name_prefix="task6-partial-reject")
            self.submit_calls = 0

        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            self.submit_calls += 1
            # One submits the controller, two the first probe, and three
            # rejects the next probe after durable admission of the first.
            if self.submit_calls == 3:
                raise RuntimeError("injected partial executor rejection")
            return super().submit(fn, *args, **kwargs)

    state = AgentStateStore(tmp_path / "state.db")
    utc = MutableUtcClock()
    ledger = LanDiscoveryLedger(state, utc_clock=utc)
    manager = task6.LanScanManager(
        ledger=ledger,
        interface_enumerator=lambda: (interface,),
        mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
        scanner=scanner,
        utc_clock=utc,
        monotonic_clock=MutableMonotonicClock(),
        scan_id_factory=lambda: "lan_partial_executor_reject",
    )
    executor = RejectSecondProbeExecutor()
    manager.start_lifecycle(executor)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )

    assert probe_entered.wait(timeout=2.0)
    terminal = _wait_for_terminal(manager, draft.scan_id)
    receipt = terminal.terminal_receipt
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "worker_error"
    assert receipt is not None
    assert receipt["admitted_count"] == 1
    assert receipt["completed_count"] == 1
    assert receipt["persisted_observation_count"] == 1
    assert receipt["unknown_inflight_count"] == 0
    assert active_probes == 0
    assert executor.submit_calls == 3
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_manager_passes_one_executor_and_one_absolute_deadline_through_all_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task6 = _task6()
    monotonic = AdvancingMonotonicClock(step=0.25)
    seen: dict[str, object] = {}

    class CleanupHandle:
        def __init__(self) -> None:
            self.closed = False
            self.timeouts: list[float] = []

        def is_quiescent(self) -> bool:
            return self.closed

        def wait_quiescent(self, *, timeout_seconds: float) -> bool:
            self.timeouts.append(timeout_seconds)
            self.closed = True
            return True

    cleanup = CleanupHandle()

    def collect(_scope: Any, **kwargs: Any) -> Any:
        assert seen["durable_claim"] is True
        seen["mdns_deadline"] = kwargs["absolute_deadline"]
        seen["mdns_remaining"] = kwargs["absolute_deadline"] - monotonic()
        seen["cleanup_sink"] = kwargs["cleanup_handle_sink"]
        kwargs["cleanup_handle_sink"](cleanup)
        return task6.MdnsCollection(
            availability=task6.MdnsAvailability.AVAILABLE,
            candidates=(),
        )

    def scanner(_scope: Any, _limits: Any, **kwargs: Any) -> tuple[()]:
        assert seen["durable_claim"] is True
        seen["scan_deadline"] = kwargs["absolute_deadline"]
        seen["probe_remaining"] = kwargs["absolute_deadline"] - monotonic()
        seen["executor"] = kwargs["executor"]
        kwargs["progress"](
            task6.LanScanProgress(
                phase="admitted",
                planned_count=0,
                admitted_count=0,
                completed_count=0,
                observation=None,
            )
        )
        return ()

    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_collector=collect,
        scanner=scanner,
        monotonic_clock=monotonic,
    )
    original_claim = ledger.claim_scan_start
    original_progress = ledger.record_scan_progress
    original_terminal = ledger.commit_scan_terminal

    def claim(*args: Any, **kwargs: Any) -> Any:
        assert monotonic.calls == [100.0]
        claimed = original_claim(*args, **kwargs)
        seen["durable_claim"] = True
        return claimed

    def progress(*args: Any, **kwargs: Any) -> Any:
        seen["progress_deadline"] = kwargs["absolute_deadline"]
        assert kwargs["monotonic_clock"] is monotonic
        seen["progress_remaining"] = kwargs["absolute_deadline"] - monotonic()
        return original_progress(*args, **kwargs)

    def terminal(*args: Any, **kwargs: Any) -> Any:
        seen["terminal_deadline"] = kwargs["absolute_deadline"]
        assert kwargs["monotonic_clock"] is monotonic
        seen["terminal_remaining"] = kwargs["absolute_deadline"] - monotonic()
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(ledger, "claim_scan_start", claim)
    monkeypatch.setattr(ledger, "record_scan_progress", progress)
    monkeypatch.setattr(ledger, "commit_scan_terminal", terminal)
    executor = _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    try:
        terminal = _wait_for_terminal(manager, draft.scan_id)
        assert terminal.status == "completed"
        assert seen["executor"] is executor
        assert seen["mdns_deadline"] == 145.0
        assert seen["scan_deadline"] == 145.0
        assert seen["progress_deadline"] == 145.0
        assert seen["terminal_deadline"] == 145.0
        assert callable(seen["cleanup_sink"])
        assert cleanup.timeouts
        assert all(0.0 < timeout < 45.0 for timeout in cleanup.timeouts)
        remaining = [
            seen["mdns_remaining"],
            seen["probe_remaining"],
            seen["progress_remaining"],
            seen["terminal_remaining"],
        ]
        assert all(isinstance(value, float) and 0.0 < value < 45.0 for value in remaining)
        assert remaining == sorted(remaining, reverse=True)
        assert monotonic.calls[0] == 100.0
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_get_list_and_events_never_project_foreign_owner_rows(tmp_path: Path) -> None:
    task6 = _task6()
    manager, _state, ledger, interface = _manager(tmp_path)
    _start_lifecycle(manager)
    foreign = ledger.create_scan(
        scan_id="lan_foreign",
        owner_principal="owner:foreign",
        confirmed_interface_id=interface.interface_id,
        network="192.168.90.0/30",
        limits=task6.canonical_scan_limits(),
        preview_digest="sha256:" + "4" * 64,
        expected_revision=0,
    )
    ledger.append_event(
        foreign.scan_id,
        "foreign_event",
        {},
        expected_revision=foreign.revision,
    )
    try:
        assert manager.get(foreign.scan_id) is None
        assert manager.list() == []
        assert manager.events(foreign.scan_id) == []
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_observer_disconnect_does_not_cancel_or_change_durable_state(tmp_path: Path) -> None:
    entered = Event()
    release = Event()

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        entered.set()
        release.wait(timeout=3)
        return ()

    manager, _state, _ledger, interface = _manager(tmp_path, scanner=scanner)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    running = manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    assert entered.wait(timeout=2)
    observer = manager.subscribe_events(draft.scan_id)
    next(observer, None)
    observer.close()
    current = manager.get(draft.scan_id)
    token = manager._active_scans[draft.scan_id].cancellation  # noqa: SLF001
    try:
        assert current == running
        assert token.is_cancelled() is False
    finally:
        release.set()
        _wait_for_terminal(manager, draft.scan_id)
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_event_subscription_rechecks_after_terminal_observation_and_yields_final_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task6 = _task6()
    manager, _state, ledger, interface = _manager(tmp_path)
    _start_lifecycle(manager)
    draft = ledger.create_scan(
        scan_id="lan_subscription_terminal_race",
        owner_principal=FIXED_OWNER,
        confirmed_interface_id=interface.interface_id,
        network="192.168.90.0/30",
        limits=task6.canonical_scan_limits(),
        preview_digest="sha256:" + "c" * 64,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )
    events = manager.events
    first_read = True

    def terminal_between_event_and_status_reads(
        scan_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[Any]:
        nonlocal first_read
        page = events(scan_id, after_sequence=after_sequence, limit=limit)
        if first_read:
            first_read = False
            assert page == []
            ledger.commit_scan_terminal(
                running.scan_id,
                owner_principal=FIXED_OWNER,
                expected_revision=running.revision,
                status="completed",
                terminal_reason="scan_complete",
                cancel_reason=None,
                observations=(),
                mdns_status="unavailable",
                planned_count=0,
                admitted_count=0,
                completed_count=0,
                error_category_counts={},
                timeout_count=0,
                evidence_complete=True,
                unknown_inflight_count=0,
            )
        return page

    monkeypatch.setattr(manager, "events", terminal_between_event_and_status_reads)
    observer = manager.subscribe_events(running.scan_id)

    yielded = list(observer)

    assert [event.event_type for event in yielded] == ["scan_completed"]
    terminal = ledger.get_scan(running.scan_id)
    assert terminal is not None and terminal.status == "completed"
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_worker_exception_is_code_only_and_scan_never_imports_provider_or_target(
    tmp_path: Path,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> tuple[()]:
        raise RuntimeError("secret-token host.internal 192.168.90.2")

    manager, state, _ledger, interface = _manager(tmp_path, scanner=fail)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    try:
        terminal = _wait_for_terminal(manager, draft.scan_id)
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        receipt_text = str(terminal.terminal_receipt)
        assert "secret-token" not in receipt_text
        assert "host.internal" not in receipt_text
        assert "192.168.90.2" not in receipt_text
        with state._connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM routing_provider_profiles").fetchone()[0]
                == 0
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM routing_model_targets").fetchone()[0] == 0
            )
            durable_text = " ".join(
                str(value)
                for row in connection.execute(
                    """
                    SELECT terminal_receipt_json FROM routing_lan_scans
                    WHERE scan_id = ?
                    UNION ALL
                    SELECT payload_json FROM routing_lan_scan_events
                    WHERE scan_id = ?
                    """,
                    (draft.scan_id, draft.scan_id),
                ).fetchall()
                for value in row
                if value is not None
            )
        assert "secret-token" not in durable_text
        assert "host.internal" not in durable_text
        assert "192.168.90.2" not in durable_text
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_automatic_worker_base_exception_terminalizes_failed_without_false_completion(
    tmp_path: Path,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> tuple[()]:
        raise SystemExit("raw-secret-automatic-system-exit")

    manager, _state, ledger, interface = _manager(tmp_path, scanner=fail)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    try:
        terminal = _wait_for_terminal(manager, draft.scan_id)

        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["evidence_complete"] is True
        assert terminal.terminal_receipt["unknown_inflight_count"] == 0
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (0, 0, 0)
        assert [event.event_type for event in ledger.list_events(draft.scan_id)] == [
            "scan_started",
            "scan_failed",
        ]
        assert "raw-secret-automatic-system-exit" not in json.dumps(
            terminal.terminal_receipt,
            sort_keys=True,
        )
        assert manager.controller_count == 0
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
    assert ledger.list_scans(status="running", owner_principal=FIXED_OWNER) == []


def test_automatic_worker_gap_interrupts_and_fences_until_shared_executor_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_entered = Event()
    release_child = Event()
    shutdown_helper_waiting_to_return = Event()
    release_shutdown_helper = Event()
    captured_cancellation: list[Any] = []

    class SpyThreadPoolExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=17, thread_name_prefix="task6-worker-gap")
            self.shutdown_calls: list[tuple[bool, bool]] = []
            self.shutdown_entered = Event()
            self.shutdown_finished = Event()

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            self.shutdown_calls.append((wait, cancel_futures))
            self.shutdown_entered.set()
            try:
                super().shutdown(wait=wait, cancel_futures=cancel_futures)
            finally:
                self.shutdown_finished.set()

    def child_work() -> None:
        child_entered.set()
        release_child.wait()

    def fail_after_admission(*_args: Any, **kwargs: Any) -> tuple[()]:
        progress = kwargs["progress"]
        executor = kwargs["executor"]
        cancellation = kwargs["cancellation"]
        progress(
            LanScanProgress(
                phase="planned",
                planned_count=1,
                admitted_count=0,
                completed_count=0,
                observation=None,
            )
        )
        executor.submit(child_work)
        assert child_entered.wait(timeout=2.0)
        progress(
            LanScanProgress(
                phase="admitted",
                planned_count=1,
                admitted_count=1,
                completed_count=0,
                observation=None,
            )
        )
        captured_cancellation.append(cancellation)
        raise SystemExit("raw-secret-automatic-gap-exit")

    manager, _state, ledger, interface = _manager(
        tmp_path,
        scanner=fail_after_admission,
        scan_id="lan_" + "e" * 32,
    )
    executor = SpyThreadPoolExecutor()
    manager.start_lifecycle(executor)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    try:
        manager.start(
            draft.scan_id,
            expected_revision=draft.revision,
            authorization=authorization,
            preview_digest=authorization.preview_digest,
        )
        terminal = _wait_for_terminal(manager, draft.scan_id, timeout=1.0)

        assert terminal.status == "interrupted"
        assert terminal.terminal_reason == "worker_interrupted"
        assert terminal.cancel_reason is None
        assert terminal.terminal_receipt is not None
        receipt = terminal.terminal_receipt
        assert receipt["evidence_complete"] is False
        assert receipt["unknown_inflight_count"] == 1
        assert (
            receipt["planned_count"],
            receipt["admitted_count"],
            receipt["completed_count"],
            receipt["persisted_observation_count"],
        ) == (1, 1, 0, 0)
        assert ledger.list_observations(draft.scan_id) == []
        assert [event.event_type for event in ledger.list_events(draft.scan_id)] == [
            "scan_started",
            "scan_progress",
            "scan_progress",
            "scan_interrupted",
        ]
        assert captured_cancellation and captured_cancellation[0].is_cancelled() is True
        assert "raw-secret-automatic-gap-exit" not in json.dumps(receipt, sort_keys=True)
        with pytest.raises(RuntimeError, match="admission.*closed"):
            manager.preview(interface.interface_id, "192.168.90.0/30")
        assert manager.retained_controller_ids() == (draft.scan_id,)
        assert manager.is_quiescent() is False

        shutdown_executor = manager._shutdown_executor

        def pause_shutdown_helper_before_return(executor_to_shutdown: object) -> None:
            shutdown_executor(executor_to_shutdown)
            shutdown_helper_waiting_to_return.set()
            release_shutdown_helper.wait()

        monkeypatch.setattr(manager, "_shutdown_executor", pause_shutdown_helper_before_return)

        shutdown_results: list[bool | None] = [None, None]
        shutdown_returned = (Event(), Event())
        shutdown_callers_ready = Barrier(3)

        def bounded_shutdown(index: int) -> None:
            try:
                shutdown_callers_ready.wait(timeout=1.0)
                shutdown_results[index] = manager.shutdown(timeout_seconds=0.01)
            finally:
                shutdown_returned[index].set()

        shutdown_threads = tuple(
            Thread(
                target=bounded_shutdown,
                args=(index,),
                name=f"task6-bounded-shutdown-{index}",
            )
            for index in range(2)
        )
        for shutdown_thread in shutdown_threads:
            shutdown_thread.start()
        try:
            shutdown_callers_ready.wait(timeout=1.0)
            assert executor.shutdown_entered.wait(timeout=1.0)
            assert all(item.wait(timeout=0.25) for item in shutdown_returned)
            assert shutdown_results == [False, False]
            assert executor.shutdown_calls == [(True, False)]
            assert executor.shutdown_finished.is_set() is False
            assert manager.get(draft.scan_id) == terminal
            with pytest.raises(RuntimeError, match="admission.*closed"):
                manager.preview(interface.interface_id, "192.168.90.0/30")
            assert manager.retained_controller_ids() == (draft.scan_id,)
            assert manager.controller_count == 1
            assert manager.is_quiescent() is False
        finally:
            release_child.set()
            for shutdown_thread in shutdown_threads:
                shutdown_thread.join(timeout=2.0)
        assert all(not item.is_alive() for item in shutdown_threads)
        assert executor.shutdown_finished.wait(timeout=2.0)
        assert shutdown_helper_waiting_to_return.wait(timeout=2.0)
        assert manager.is_quiescent() is False
        assert manager.shutdown(timeout_seconds=0.01) is False
        release_shutdown_helper.set()
        assert manager.shutdown(timeout_seconds=1.0) is True
        assert executor.shutdown_calls == [(True, False)]
        assert manager.retained_controller_ids() == ()
        assert manager.controller_count == 0
        assert manager.is_quiescent() is True
    finally:
        release_child.set()
        release_shutdown_helper.set()
        assert manager.shutdown(timeout_seconds=2.0) is True
    assert manager.is_quiescent() is True


def test_worker_gap_executor_shutdown_failure_is_exactly_once_and_retains_fence(
    tmp_path: Path,
) -> None:
    child_entered = Event()
    release_child = Event()

    class FailingShutdownExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=17, thread_name_prefix="task6-shutdown-failure")
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            self.shutdown_calls.append((wait, cancel_futures))
            raise RuntimeError("injected LAN executor shutdown failure")

        def close_for_test(self) -> None:
            super().shutdown(wait=True, cancel_futures=False)

    def child_work() -> None:
        child_entered.set()
        release_child.wait()

    def fail_after_admission(*_args: Any, **kwargs: Any) -> tuple[()]:
        progress = kwargs["progress"]
        executor = kwargs["executor"]
        progress(
            LanScanProgress(
                phase="planned",
                planned_count=1,
                admitted_count=0,
                completed_count=0,
                observation=None,
            )
        )
        executor.submit(child_work)
        assert child_entered.wait(timeout=2.0)
        progress(
            LanScanProgress(
                phase="admitted",
                planned_count=1,
                admitted_count=1,
                completed_count=0,
                observation=None,
            )
        )
        raise SystemExit("raw-secret-shutdown-failure-gap")

    manager, _state, _ledger, interface = _manager(
        tmp_path,
        scanner=fail_after_admission,
        scan_id="lan_" + "d" * 32,
    )
    executor = FailingShutdownExecutor()
    manager.start_lifecycle(executor)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    try:
        manager.start(
            draft.scan_id,
            expected_revision=draft.revision,
            authorization=authorization,
            preview_digest=authorization.preview_digest,
        )
        terminal = _wait_for_terminal(manager, draft.scan_id, timeout=1.0)
        assert terminal.status == "interrupted"
        assert terminal.terminal_reason == "worker_interrupted"

        with pytest.raises(RuntimeError, match="^injected LAN executor shutdown failure$"):
            manager.shutdown(timeout_seconds=1.0)
        assert executor.shutdown_calls == [(True, False)]
        assert manager.get(draft.scan_id) == terminal
        assert manager.retained_controller_ids() == (draft.scan_id,)
        assert manager.controller_count == 1
        assert manager.is_quiescent() is False

        with pytest.raises(RuntimeError, match="^injected LAN executor shutdown failure$"):
            manager.shutdown(timeout_seconds=1.0)
        assert executor.shutdown_calls == [(True, False)]
        assert manager.retained_controller_ids() == (draft.scan_id,)
        assert manager.is_quiescent() is False
    finally:
        release_child.set()
        executor.close_for_test()


def test_successful_worker_persists_evidence_only_and_never_imports_inventory(
    tmp_path: Path,
) -> None:
    def observe(scope: Any, *_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.90.2", 11434)
        return (
            _make_observation(
                endpoint,
                reachability=Reachability.UNREACHABLE,
                failure_category=LanFailureCategory.TCP_REFUSED,
            ),
        )

    manager, state, ledger, interface = _manager(tmp_path, scanner=observe)
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )
    try:
        terminal = _wait_for_terminal(manager, draft.scan_id)
        assert terminal.status == "completed"
        observations = ledger.list_observations(draft.scan_id)
        assert len(observations) == 1
        assert observations[0].endpoint_id.startswith("sha256:")
        with state._connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM routing_provider_profiles").fetchone()[0]
                == 0
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM routing_model_targets").fetchone()[0] == 0
            )
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_no_tool_routine_scheduler_startup_or_flock_module_can_start_a_scan(
    tmp_path: Path,
) -> None:
    manager, _state, _ledger, _interface_value = _manager(tmp_path)
    public_methods = {
        name
        for name, value in inspect.getmembers(type(manager), predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert "start" in public_methods
    assert not ({"invoke", "run_tool", "run_routine", "schedule", "startup_scan"} & public_methods)

    source_root = Path(__file__).parents[1] / "src" / "nested_memvid_agent"
    prohibited_roots = (
        source_root / "agent.py",
        source_root / "run_manager.py",
        source_root / "routines.py",
        source_root / "routine_loop.py",
        source_root / "tools",
        source_root / "routing",
    )
    violations: list[str] = []
    for root in prohibited_roots:
        paths = (root,) if root.is_file() else tuple(root.rglob("*.py"))
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == (
                    "nested_memvid_agent.lan_scan_manager"
                ):
                    violations.append(f"{path.name}:{node.lineno}:import")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "nested_memvid_agent.lan_scan_manager":
                            violations.append(f"{path.name}:{node.lineno}:import")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "start"
                    and isinstance(node.func.value, ast.Name)
                    and "lan" in node.func.value.id.lower()
                ):
                    violations.append(f"{path.name}:{node.lineno}:start")
    assert violations == []


def test_interfaces_is_lifecycle_gated_and_uses_one_canonical_injected_inventory(
    tmp_path: Path,
) -> None:
    first = NetworkInterface.from_addresses(
        os_identity="darwin:en91",
        display_name="Secondary fixture",
        addresses=("192.168.91.1/30",),
    )
    second = NetworkInterface.from_addresses(
        os_identity="darwin:en90",
        display_name="Primary fixture",
        addresses=("192.168.90.1/30",),
    )
    calls = 0

    def enumerate_fixture() -> tuple[NetworkInterface, ...]:
        nonlocal calls
        calls += 1
        return first, second

    manager, _state, _ledger, _interface_value = _manager(
        tmp_path,
        interface_enumerator=enumerate_fixture,
    )
    with pytest.raises(RuntimeError, match="has not started"):
        manager.interfaces()
    assert calls == 0

    _start_lifecycle(manager)
    try:
        interfaces = manager.interfaces()
        assert interfaces == tuple(sorted((first, second), key=lambda item: item.interface_id))
        assert calls == 1
        assert "owner_principal" not in inspect.signature(manager.interfaces).parameters
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_interfaces_fails_closed_above_its_fixed_bound(tmp_path: Path) -> None:
    consumed = 0

    def enumerate_fixture() -> Iterator[NetworkInterface]:
        nonlocal consumed
        for index in range(65):
            consumed += 1
            yield NetworkInterface.from_addresses(
                os_identity=f"darwin:fixture-{index}",
                display_name=f"Fixture {index}",
                addresses=("192.168.90.1/30",),
            )
        raise AssertionError("interface enumeration consumed beyond the fixed limit sentinel")

    manager, _state, _ledger, _interface_value = _manager(
        tmp_path,
        interface_enumerator=enumerate_fixture,
    )
    _start_lifecycle(manager)
    try:
        with pytest.raises(ValueError, match="interface.*limit"):
            manager.interfaces()
        assert consumed == 65
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_interfaces_rejects_unbounded_or_nonprivate_public_projection(
    tmp_path: Path,
) -> None:
    canonical = _interface()
    multibyte_display = "é" * 129
    assert len(multibyte_display) <= 256
    assert len(multibyte_display.encode("utf-8")) > 256
    excessive_addresses = tuple(
        f"10.{(index // (254 * 254)) % 254}.{(index // 254) % 254}.{index % 254 + 1}/8"
        for index in range(65)
    )
    cases = (
        replace(
            canonical,
            display_name="x" * 257,
        ),
        replace(canonical, display_name=multibyte_display),
        replace(canonical, display_name="Control\ncharacter"),
        replace(canonical, display_name="Cafe\u0301"),
        replace(canonical, interface_id="sha256:" + "0" * 64),
        NetworkInterface.from_addresses(
            os_identity="darwin:too-many-addresses",
            display_name="Too many addresses",
            addresses=excessive_addresses,
        ),
        NetworkInterface.from_addresses(
            os_identity="darwin:public-address",
            display_name="Public address",
            addresses=("8.8.8.8/32",),
        ),
        replace(canonical, addresses=(" 192.168.90.1/30",)),
    )

    for index, interface in enumerate(cases):
        case_root = tmp_path / f"case-{index}"
        case_root.mkdir()
        manager, _state, _ledger, _interface_value = _manager(
            case_root,
            interface_enumerator=lambda interface=interface: (interface,),
        )
        _start_lifecycle(manager)
        try:
            with pytest.raises(ValueError, match="interface"):
                manager.interfaces()
        finally:
            assert manager.shutdown(timeout_seconds=1.0) is True


@pytest.mark.parametrize("duplicate_kind", ("canonical_id", "os_identity"))
def test_interfaces_reject_duplicate_canonical_id_or_os_identity(
    tmp_path: Path,
    duplicate_kind: str,
) -> None:
    first = _interface()
    second = (
        first
        if duplicate_kind == "canonical_id"
        else NetworkInterface.from_addresses(
            os_identity=first.os_identity,
            display_name="Same OS interface with drifted addresses",
            addresses=("192.168.91.1/30",),
        )
    )
    assert second.interface_id == first.interface_id or second.os_identity == first.os_identity
    manager, _state, _ledger, _interface_value = _manager(
        tmp_path,
        interface_enumerator=lambda: (first, second),
    )
    _start_lifecycle(manager)
    try:
        with pytest.raises(ValueError, match="interface.*duplicate"):
            manager.interfaces()
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_interface_enumeration_and_canonicalization_hold_manager_operation_lock(
    tmp_path: Path,
) -> None:
    first = NetworkInterface.from_addresses(
        os_identity="darwin:lock-first",
        display_name="Lock first",
        addresses=("192.168.90.1/30",),
    )
    second = NetworkInterface.from_addresses(
        os_identity="darwin:lock-second",
        display_name="Lock second",
        addresses=("192.168.91.1/30",),
    )
    block_enumerator = False
    enumeration_entered = Event()
    enumeration_release = Event()

    def enumerate_fixture() -> tuple[NetworkInterface, ...]:
        if block_enumerator:
            enumeration_entered.set()
            if not enumeration_release.wait(timeout=2.0):
                raise AssertionError("interface enumeration release timed out")
        return second, first

    manager, _state, _ledger, _interface_value = _manager(
        tmp_path,
        interface_enumerator=enumerate_fixture,
        scan_id="lan_" + "0" * 32,
    )
    _start_lifecycle(manager)
    authorization = manager.preview(first.interface_id, "192.168.90.0/30")
    block_enumerator = True
    mutation_attempted = Event()
    mutation_finished = Event()
    completions: list[str] = []
    failures: list[BaseException] = []
    completion_lock = Lock()

    def read_interfaces() -> None:
        try:
            result = manager.interfaces()
            assert result == tuple(sorted((first, second), key=lambda item: item.interface_id))
            with completion_lock:
                completions.append("interfaces")
        except BaseException as exc:  # noqa: BLE001 - surfaced in controller thread
            failures.append(exc)

    def create_draft() -> None:
        try:
            mutation_attempted.set()
            manager.create_draft_for_preview(
                authorization.preview_digest,
                expected_revision=0,
            )
            with completion_lock:
                completions.append("draft")
        except BaseException as exc:  # noqa: BLE001 - surfaced in controller thread
            failures.append(exc)
        finally:
            mutation_finished.set()

    reader = Thread(target=read_interfaces, name="task7-interface-lock-reader", daemon=True)
    mutation = Thread(target=create_draft, name="task7-interface-lock-mutation", daemon=True)
    try:
        reader.start()
        if not enumeration_entered.wait(timeout=1.0):
            reader.join(timeout=1.0)
            if failures:
                raise failures[0]
            raise AssertionError("manager never entered the blocking interface enumerator")
        mutation.start()
        assert mutation_attempted.wait(timeout=1.0)
        assert mutation_finished.wait(timeout=0.1) is False
        assert completions == []
        enumeration_release.set()
        reader.join(timeout=2.0)
        mutation.join(timeout=2.0)
        assert not reader.is_alive() and not mutation.is_alive()
        assert failures == []
        assert sorted(completions) == ["draft", "interfaces"]
    finally:
        enumeration_release.set()
        reader.join(timeout=1.0)
        if mutation.ident is not None:
            mutation.join(timeout=1.0)
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_digest_route_seam_binds_one_draft_and_consumes_authority_after_start(
    tmp_path: Path,
) -> None:
    task7 = _task6()
    manager, _state, ledger, interface = _manager(
        tmp_path,
        scanner=lambda *_args, **_kwargs: (),
        scan_id="lan_" + "a" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        draft = manager.create_draft_for_preview(
            authorization.preview_digest,
            expected_revision=0,
        )
        assert draft.revision == 1
        assert draft.preview_digest == authorization.preview_digest

        before = ledger.get_scan(draft.scan_id)
        with pytest.raises(task7.LanPreviewAuthorizationError):
            manager.create_draft_for_preview(
                authorization.preview_digest,
                expected_revision=0,
            )
        assert ledger.get_scan(draft.scan_id) == before

        started = manager.start_for_preview(
            draft.scan_id,
            expected_revision=draft.revision,
            preview_digest=authorization.preview_digest,
        )
        assert started.status == "running"
        _wait_for_terminal(manager, draft.scan_id)

        with pytest.raises(task7.LanPreviewAuthorizationError):
            manager.start_for_preview(
                draft.scan_id,
                expected_revision=started.revision,
                preview_digest=authorization.preview_digest,
            )
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_digest_route_seam_rejects_creation_aliases_and_replaced_preview_without_writes(
    tmp_path: Path,
) -> None:
    task7 = _task6()
    manager, _state, _ledger, interface = _manager(
        tmp_path,
        scan_id="lan_" + "b" * 32,
    )
    _start_lifecycle(manager)
    try:
        replaced = manager.preview(interface.interface_id, "192.168.90.0/30")
        current = manager.preview(interface.interface_id, "192.168.90.0/31")
        assert manager.list() == []

        for digest in (replaced.preview_digest, "sha256:" + "f" * 64):
            with pytest.raises(task7.LanPreviewAuthorizationError):
                manager.create_draft_for_preview(
                    digest,
                    expected_revision=0,
                )
            assert manager.list() == []

        with pytest.raises(LanScanRevisionConflict):
            manager.create_draft_for_preview(
                current.preview_digest,
                expected_revision=True,
            )
        assert manager.list() == []

        live_draft = manager.create_draft_for_preview(
            current.preview_digest,
            expected_revision=0,
        )
        assert live_draft.preview_digest == current.preview_digest
        assert manager.get(live_draft.scan_id) == live_draft
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_digest_route_seam_allows_exactly_one_draft_creation_winner(
    tmp_path: Path,
) -> None:
    task7 = _task6()
    manager, _state, _ledger, interface = _manager(
        tmp_path,
        scan_id="lan_" + "e" * 32,
    )
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    start_gate = Barrier(3, timeout=2.0)
    records: list[object] = []
    failures: list[BaseException] = []
    outcome_lock = Lock()

    def create() -> None:
        try:
            start_gate.wait()
            result = manager.create_draft_for_preview(
                authorization.preview_digest,
                expected_revision=0,
            )
            with outcome_lock:
                records.append(result)
        except BaseException as exc:  # noqa: BLE001 - exact losing type asserted below
            with outcome_lock:
                failures.append(exc)

    threads = tuple(
        Thread(
            target=create,
            name=f"task7-create-{index}",
            daemon=True,
        )
        for index in range(2)
    )
    try:
        for thread in threads:
            thread.start()
        start_gate.wait()
        for thread in threads:
            thread.join(timeout=2.0)
        assert all(not thread.is_alive() for thread in threads)
        assert len(records) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], task7.LanPreviewAuthorizationError)
        assert len(manager.list()) == 1
    finally:
        start_gate.abort()
        for thread in threads:
            thread.join(timeout=1.0)
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_digest_route_seam_consumes_start_authority_only_after_committed_claim(
    tmp_path: Path,
) -> None:
    manager, _state, _ledger, interface = _manager(
        tmp_path,
        scanner=lambda *_args, **_kwargs: (),
        scan_id="lan_" + "f" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        draft = manager.create_draft_for_preview(
            authorization.preview_digest,
            expected_revision=0,
        )
        with pytest.raises(LanScanRevisionConflict):
            manager.start_for_preview(
                draft.scan_id,
                expected_revision=draft.revision + 1,
                preview_digest=authorization.preview_digest,
            )

        started = manager.start_for_preview(
            draft.scan_id,
            expected_revision=draft.revision,
            preview_digest=authorization.preview_digest,
        )
        assert started.status == "running"
        assert _wait_for_terminal(manager, draft.scan_id).status == "completed"
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_digest_start_rejects_wrong_scan_substitution_and_keeps_original_usable(
    tmp_path: Path,
) -> None:
    task7 = _task6()
    manager, _state, ledger, interface = _manager(
        tmp_path,
        scanner=lambda *_args, **_kwargs: (),
        scan_id="lan_" + "1" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        original = manager.create_draft_for_preview(
            authorization.preview_digest,
            expected_revision=0,
        )
        substituted = ledger.create_scan(
            scan_id="lan_" + "2" * 32,
            owner_principal=FIXED_OWNER,
            confirmed_interface_id=interface.interface_id,
            network=authorization.preview.network,
            limits=task7.canonical_scan_limits(),
            preview_digest=authorization.preview_digest,
            expected_revision=0,
        )
        original_before = ledger.get_scan(original.scan_id)
        substituted_before = ledger.get_scan(substituted.scan_id)

        with pytest.raises(task7.LanPreviewAuthorizationError):
            manager.start_for_preview(
                substituted.scan_id,
                expected_revision=substituted.revision,
                preview_digest=authorization.preview_digest,
            )
        assert ledger.get_scan(original.scan_id) == original_before
        assert ledger.get_scan(substituted.scan_id) == substituted_before
        assert ledger.list_events(substituted.scan_id) == []

        started = manager.start_for_preview(
            original.scan_id,
            expected_revision=original.revision,
            preview_digest=authorization.preview_digest,
        )
        assert started.status == "running"
        assert _wait_for_terminal(manager, original.scan_id).status == "completed"
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_replacement_preview_after_draft_invalidates_bound_start_without_writes(
    tmp_path: Path,
) -> None:
    task7 = _task6()
    scan_ids = iter(("lan_" + "3" * 32, "lan_" + "4" * 32))
    manager, _state, ledger, interface = _manager(
        tmp_path,
        scanner=lambda *_args, **_kwargs: (),
        scan_id_factory=lambda: next(scan_ids),
    )
    _start_lifecycle(manager)
    try:
        original = manager.preview(interface.interface_id, "192.168.90.0/30")
        draft = manager.create_draft_for_preview(
            original.preview_digest,
            expected_revision=0,
        )
        replacement = manager.preview(interface.interface_id, "192.168.90.0/31")
        assert replacement.preview_digest != original.preview_digest
        before = ledger.get_scan(draft.scan_id)

        with pytest.raises(task7.LanPreviewAuthorizationError):
            manager.start_for_preview(
                draft.scan_id,
                expected_revision=draft.revision,
                preview_digest=original.preview_digest,
            )
        assert ledger.get_scan(draft.scan_id) == before
        assert ledger.list_events(draft.scan_id) == []

        replacement_draft = manager.create_draft_for_preview(
            replacement.preview_digest,
            expected_revision=0,
        )
        started = manager.start_for_preview(
            replacement_draft.scan_id,
            expected_revision=replacement_draft.revision,
            preview_digest=replacement.preview_digest,
        )
        assert started.status == "running"
        assert _wait_for_terminal(manager, replacement_draft.scan_id).status == "completed"
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_digest_start_expiry_equality_is_zero_write(tmp_path: Path) -> None:
    task7 = _task6()
    utc = MutableUtcClock()
    manager, _state, ledger, interface = _manager(
        tmp_path,
        utc_clock=utc,
        scan_id="lan_" + "4" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        draft = manager.create_draft_for_preview(
            authorization.preview_digest,
            expected_revision=0,
        )
        before = ledger.get_scan(draft.scan_id)
        utc.value = authorization.expires_at

        with pytest.raises(task7.LanPreviewAuthorizationError):
            manager.start_for_preview(
                draft.scan_id,
                expected_revision=draft.revision,
                preview_digest=authorization.preview_digest,
            )
        assert ledger.get_scan(draft.scan_id) == before
        assert ledger.list_events(draft.scan_id) == []
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_digest_start_interface_inventory_drift_is_zero_write(tmp_path: Path) -> None:
    task7 = _task6()
    original = _interface()
    changed = NetworkInterface.from_addresses(
        os_identity=original.os_identity,
        display_name=original.display_name,
        addresses=("192.168.90.1/31",),
    )
    inventory: list[tuple[NetworkInterface, ...]] = [(original,)]
    manager, _state, ledger, _interface_value = _manager(
        tmp_path,
        interface_enumerator=lambda: inventory[0],
        scan_id="lan_" + "5" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(original.interface_id, "192.168.90.0/30")
        draft = manager.create_draft_for_preview(
            authorization.preview_digest,
            expected_revision=0,
        )
        before = ledger.get_scan(draft.scan_id)
        inventory[0] = (changed,)

        with pytest.raises(task7.LanPreviewAuthorizationError):
            manager.start_for_preview(
                draft.scan_id,
                expected_revision=draft.revision,
                preview_digest=authorization.preview_digest,
            )
        assert ledger.get_scan(draft.scan_id) == before
        assert ledger.list_events(draft.scan_id) == []
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_concurrent_digest_starts_have_one_claim_event_and_one_executor_submission(
    tmp_path: Path,
) -> None:
    task7 = _task6()
    scanner_entered = Event()
    scanner_release = Event()

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        scanner_entered.set()
        if not scanner_release.wait(timeout=2.0):
            raise AssertionError("digest start scanner release timed out")
        return ()

    class CountingExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=17, thread_name_prefix="task7-start-race")
            self.submit_calls = 0
            self._submit_lock = Lock()

        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            with self._submit_lock:
                self.submit_calls += 1
            return super().submit(fn, *args, **kwargs)

    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_availability=lambda: task7.MdnsAvailability.UNAVAILABLE,
        scanner=scanner,
        scan_id="lan_" + "6" * 32,
    )
    executor = CountingExecutor()
    manager.start_lifecycle(executor)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft_for_preview(
        authorization.preview_digest,
        expected_revision=0,
    )
    start_gate = Barrier(3, timeout=2.0)
    outcomes: list[LanScanRecord] = []
    failures: list[BaseException] = []
    outcome_lock = Lock()

    def start() -> None:
        try:
            start_gate.wait()
            result = manager.start_for_preview(
                draft.scan_id,
                expected_revision=draft.revision,
                preview_digest=authorization.preview_digest,
            )
            with outcome_lock:
                outcomes.append(result)
        except BaseException as exc:  # noqa: BLE001 - exact losing type asserted below
            with outcome_lock:
                failures.append(exc)

    threads = tuple(
        Thread(target=start, name=f"task7-start-{index}", daemon=True) for index in range(2)
    )
    try:
        for thread in threads:
            thread.start()
        start_gate.wait()
        for thread in threads:
            thread.join(timeout=2.0)
        assert all(not thread.is_alive() for thread in threads)
        assert len(outcomes) == 1 and outcomes[0].status == "running"
        assert len(failures) == 1
        assert isinstance(failures[0], task7.LanPreviewAuthorizationError)
        assert scanner_entered.wait(timeout=1.0)
        assert executor.submit_calls == 1
        assert [event.event_type for event in ledger.list_events(draft.scan_id)] == ["scan_started"]
    finally:
        start_gate.abort()
        scanner_release.set()
        for thread in threads:
            thread.join(timeout=1.0)
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_active_slot_preclaim_failure_retains_digest_authority_for_retry(
    tmp_path: Path,
) -> None:
    task7 = _task6()
    manager, _state, ledger, interface = _manager(
        tmp_path,
        scanner=lambda *_args, **_kwargs: (),
        scan_id="lan_" + "7" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        draft = manager.create_draft_for_preview(
            authorization.preview_digest,
            expected_revision=0,
        )
        blocker = ledger.create_scan(
            scan_id="lan_" + "8" * 32,
            owner_principal=FIXED_OWNER,
            confirmed_interface_id=interface.interface_id,
            network="192.168.90.0/30",
            limits=task7.canonical_scan_limits(),
            preview_digest=authorization.preview_digest,
            expected_revision=0,
        )
        preview = authorization.preview
        preview_event = {
            "schema": "kestrel.lan.scan-preview.v1",
            "owner_principal": FIXED_OWNER,
            "interface_id": preview.interface_id,
            "network": preview.network,
            "limits": asdict(preview.limits),
            "active_host_count": preview.active_host_count,
            "passive_or_manual_only": preview.passive_or_manual_only,
            "port_count": len(preview.port_matrix),
            "mdns_status": authorization.mdns_availability.value,
            "server_version": authorization.server_version,
            "contract_version": authorization.contract_version,
            "preview_digest": authorization.preview_digest,
            "expires_at": _utc_text(authorization.expires_at),
        }
        blocker = ledger.claim_scan_start(
            blocker.scan_id,
            owner_principal=FIXED_OWNER,
            expected_revision=blocker.revision,
            preview_digest=authorization.preview_digest,
            authorized_preview_digest=authorization.preview_digest,
            preview_event=preview_event,
        )
        assert [event.event_type for event in ledger.list_events(blocker.scan_id)] == [
            "scan_started"
        ]

        with pytest.raises(task7.LanScanAdmissionConflict):
            manager.start_for_preview(
                draft.scan_id,
                expected_revision=draft.revision,
                preview_digest=authorization.preview_digest,
            )
        assert ledger.get_scan(draft.scan_id) == draft
        assert ledger.list_events(draft.scan_id) == []

        ledger.commit_scan_terminal(
            blocker.scan_id,
            owner_principal=FIXED_OWNER,
            expected_revision=blocker.revision,
            status="completed",
            terminal_reason="scan_complete",
            cancel_reason=None,
            observations=(),
            mdns_status=authorization.mdns_availability.value,
            planned_count=0,
            admitted_count=0,
            completed_count=0,
            error_category_counts={},
            timeout_count=0,
            evidence_complete=True,
            unknown_inflight_count=0,
        )
        assert [event.event_type for event in ledger.list_events(blocker.scan_id)] == [
            "scan_started",
            "scan_completed",
        ]
        started = manager.start_for_preview(
            draft.scan_id,
            expected_revision=draft.revision,
            preview_digest=authorization.preview_digest,
        )
        assert started.status == "running"
        assert _wait_for_terminal(manager, draft.scan_id).status == "completed"
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_executor_rejection_after_claim_consumes_digest_authority(
    tmp_path: Path,
) -> None:
    task7 = _task6()

    class RejectingExecutor:
        def __init__(self) -> None:
            self.submit_calls = 0
            self.shutdown_calls = 0

        def submit(self, *_args: Any, **_kwargs: Any) -> None:
            self.submit_calls += 1
            raise RuntimeError("injected executor rejection")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True
            assert cancel_futures is False
            self.shutdown_calls += 1

    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_availability=lambda: task7.MdnsAvailability.UNAVAILABLE,
        scan_id="lan_" + "9" * 32,
    )
    executor = RejectingExecutor()
    manager.start_lifecycle(executor)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft_for_preview(
        authorization.preview_digest,
        expected_revision=0,
    )

    terminal = manager.start_for_preview(
        draft.scan_id,
        expected_revision=draft.revision,
        preview_digest=authorization.preview_digest,
    )
    assert terminal.status == "failed"
    assert executor.submit_calls == 1
    assert [event.event_type for event in ledger.list_events(draft.scan_id)] == [
        "scan_started",
        "scan_failed",
    ]
    with pytest.raises(task7.LanPreviewAuthorizationError):
        manager.start_for_preview(
            draft.scan_id,
            expected_revision=terminal.revision,
            preview_digest=authorization.preview_digest,
        )
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert executor.shutdown_calls == 1


def test_draft_cancel_invalidates_digest_start_authority(tmp_path: Path) -> None:
    task7 = _task6()
    manager, _state, ledger, interface = _manager(
        tmp_path,
        scan_id="lan_" + "a" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
        draft = manager.create_draft_for_preview(
            authorization.preview_digest,
            expected_revision=0,
        )
        cancelled = manager.cancel(draft.scan_id, expected_revision=draft.revision)
        assert cancelled.status == "cancelled"

        with pytest.raises(task7.LanPreviewAuthorizationError):
            manager.start_for_preview(
                draft.scan_id,
                expected_revision=cancelled.revision,
                preview_digest=authorization.preview_digest,
            )
        assert ledger.get_scan(draft.scan_id) == cancelled
        assert [event.event_type for event in ledger.list_events(draft.scan_id)] == [
            "scan_cancelled"
        ]
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_observation_page_is_fixed_owner_deterministic_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task7 = _task6()
    manager, _state, ledger, interface = _manager(tmp_path)
    _start_lifecycle(manager)
    owned = ledger.create_scan(
        scan_id="lan_" + "c" * 32,
        owner_principal=FIXED_OWNER,
        confirmed_interface_id=interface.interface_id,
        network="192.168.90.0/30",
        limits=task7.canonical_scan_limits(),
        preview_digest="sha256:" + "c" * 64,
        expected_revision=0,
    )
    foreign = ledger.create_scan(
        scan_id="lan_" + "d" * 32,
        owner_principal="owner:foreign",
        confirmed_interface_id=interface.interface_id,
        network="192.168.90.0/30",
        limits=task7.canonical_scan_limits(),
        preview_digest="sha256:" + "d" * 64,
        expected_revision=0,
    )

    def observation(index: int) -> LanObservationDraft:
        return LanObservationDraft(
            endpoint_id="sha256:" + f"{index:064x}",
            source="active",
            interface_id=interface.interface_id,
            address="192.168.90.2",
            port=11434,
            api_shape=None,
            tls_enabled=False,
            certificate_sha256=None,
            catalog_digest=None,
            capability_digest=None,
            public_payload={"model_count": index},
            freshness_timestamp="2026-08-01T12:00:00Z",
            error_category="tcp_refused",
        )

    owned_revision = owned.revision
    for index in (3, 1, 2):
        ledger.append_observation(
            owned.scan_id,
            observation(index),
            expected_revision=owned_revision,
        )
        current = ledger.get_scan(owned.scan_id)
        assert current is not None
        owned_revision = current.revision
    ledger.append_observation(
        foreign.scan_id,
        observation(4),
        expected_revision=foreign.revision,
    )
    owned_snapshot = ledger.get_scan(owned.scan_id)
    assert owned_snapshot is not None

    def forbidden_split_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("manager detail must use the atomic ledger snapshot seam")

    monkeypatch.setattr(ledger, "get_scan", forbidden_split_read)
    monkeypatch.setattr(ledger, "list_observations", forbidden_split_read)

    try:
        page = manager.observation_page(owned.scan_id, limit=2)
        assert page is not None
        assert page.scan == owned_snapshot
        assert page.total_count == 3
        assert page.truncated is True
        assert tuple(item.endpoint_id for item in page.observations) == (
            "sha256:" + f"{1:064x}",
            "sha256:" + f"{2:064x}",
        )
        assert page.next_cursor is not None
        continuation = manager.observation_page(
            owned.scan_id,
            limit=2,
            cursor=page.next_cursor,
        )
        assert continuation is not None
        assert tuple(item.endpoint_id for item in continuation.observations) == (
            "sha256:" + f"{3:064x}",
        )
        assert continuation.next_cursor is None
        assert manager.observation_page(foreign.scan_id, limit=2) is None
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_manual_preview_digest_independently_binds_every_authority_field(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    clock = MutableUtcClock()
    selected = NetworkInterface.from_addresses(
        os_identity="darwin:en-manual-digest",
        display_name="Manual digest selected",
        addresses=("192.168.90.1/29", "fd00::1/64"),
    )
    secondary = NetworkInterface.from_addresses(
        os_identity="darwin:en-manual-secondary",
        display_name="Manual digest secondary",
        addresses=("10.90.0.1/24",),
    )
    canonical_inventory = tuple(sorted((selected, secondary), key=lambda item: item.interface_id))
    resolver_calls: list[str] = []
    scanned_endpoints: list[object] = []

    def manual_resolver(host: str) -> tuple[str, ...]:
        resolver_calls.append(host)
        return ("192.168.90.3", "192.168.90.2")

    def manual_scanner(scope: object, endpoint: object, **kwargs: object) -> object:
        scanned_endpoints.append(endpoint)
        return _manual_probe_result(scope, endpoint, **kwargs)

    manager, _state, _ledger, _interface_value = _manager(
        tmp_path,
        interface_enumerator=lambda: (secondary, selected),
        manual_resolver=manual_resolver,
        manual_scanner=manual_scanner,
        utc_clock=clock,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            selected.interface_id,
            "model-box.local",
            5001,
        )
        assert authorization.resolved_addresses == (
            "192.168.90.2",
            "192.168.90.3",
        )
        assert resolver_calls == ["model-box.local"]
        assert (
            task6.LAN_MANUAL_PREVIEW_CONTRACT_VERSION
            == "kestrel.lan.manual-preview-authorization.v1"
        )
        retained = asdict(authorization)
        assert not hasattr(authorization, "host")
        assert {key for key in retained if "host" in key} == {"host_input_digest"}
        assert retained["host_input_digest"] == authorization.host_input_digest
        assert "model-box.local" not in repr(authorization)
        assert "model-box.local" not in repr(retained)
        inventory_authority = [
            {
                "interface_id": item.interface_id,
                "os_identity": item.os_identity,
                "addresses": list(item.addresses),
            }
            for item in canonical_inventory
        ]
        arguments: dict[str, object] = {
            "owner_principal": FIXED_OWNER,
            "interface": selected,
            "inventory_authority": inventory_authority,
            "host_input_digest": authorization.host_input_digest,
            "port": 5001,
            "resolved_addresses": ("192.168.90.2", "192.168.90.3"),
            "issued_at": authorization.issued_at,
            "expires_at": authorization.expires_at,
            "server_version": task6.LAN_SERVER_VERSION,
            "contract_version": task6.LAN_MANUAL_PREVIEW_CONTRACT_VERSION,
            "limits": _manual_limits(5001),
        }

        def independent_payload(values: dict[str, object]) -> dict[str, object]:
            interface = values["interface"]
            assert type(interface) is NetworkInterface
            return {
                "schema": "kestrel.lan.manual-preview-authorization.v1",
                "owner_principal": values["owner_principal"],
                "interface": {
                    "interface_id": interface.interface_id,
                    "os_identity": interface.os_identity,
                    "addresses": list(interface.addresses),
                },
                "inventory_authority": values["inventory_authority"],
                "host_input_digest": values["host_input_digest"],
                "port": values["port"],
                "resolved_addresses": list(values["resolved_addresses"]),
                "issued_at": _utc_text(values["issued_at"]),
                "expires_at": _utc_text(values["expires_at"]),
                "server_version": values["server_version"],
                "contract_version": values["contract_version"],
                "limits": values["limits"],
            }

        payload = independent_payload(arguments)
        expected = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        assert authorization.preview_digest == expected
        assert task6.manual_preview_authorization_digest(**arguments) == expected

        mutated_interface = NetworkInterface.from_addresses(
            os_identity=selected.os_identity,
            display_name=selected.display_name,
            addresses=("192.168.90.1/30", "fd00::1/64"),
        )
        mutations = {
            "owner_principal": "owner:lookalike",
            "interface": mutated_interface,
            "inventory_authority": [
                *inventory_authority,
                {
                    "interface_id": "sha256:" + "9" * 64,
                    "os_identity": "darwin:en-injected",
                    "addresses": ["10.99.0.1/24"],
                },
            ],
            "host_input_digest": "sha256:" + "8" * 64,
            "port": 5002,
            "resolved_addresses": ("192.168.90.2", "192.168.90.4"),
            "issued_at": authorization.issued_at + timedelta(microseconds=1),
            "expires_at": authorization.expires_at + timedelta(microseconds=1),
            "server_version": "kestrel-mutated",
            "contract_version": "kestrel.lan.manual-preview-authorization.v999",
            "limits": {**_manual_limits(5001), "max_scan_concurrency": 2},
        }
        for field, value in mutations.items():
            mutated_arguments = {**arguments, field: value}
            mutated_payload = independent_payload(mutated_arguments)
            mutated_digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        mutated_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
            )
            assert mutated_digest != expected, field
            try:
                production_digest = task6.manual_preview_authorization_digest(**mutated_arguments)
            except (TypeError, ValueError):
                continue
            assert production_digest != expected, field

        with pytest.raises(_manual_conflict_type()):
            manager.confirm_manual(
                authorization.preview_digest,
                "192.168.90.4",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.3",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert started.network == "192.168.90.3/32"
        terminal = _wait_for_terminal(manager, started.scan_id)
        assert terminal.status == "completed"
        assert terminal.network == "192.168.90.3/32"
        assert resolver_calls == ["model-box.local"]
        assert len(scanned_endpoints) == 1
        endpoint = scanned_endpoints[0]
        assert type(endpoint) is _manual_endpoint_type()
        assert (endpoint.kind, endpoint.address, endpoint.port) == (
            "manual",
            "192.168.90.3",
            5001,
        )
        observations = _ledger.list_observations(started.scan_id)
        assert len(observations) == 1
        assert (
            observations[0].source,
            observations[0].address,
            observations[0].port,
        ) == ("manual", "192.168.90.3", 5001)
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


@pytest.mark.parametrize(
    "host",
    (
        "8.8.8.8",
        "127.0.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "192.0.2.1",
        "192.168.91.2",
    ),
    ids=(
        "public",
        "loopback",
        "multicast",
        "unspecified",
        "reserved-documentation",
        "private-out-of-interface",
    ),
)
def test_manual_preview_rejects_ineligible_literals_before_any_work_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    host: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    boundary_calls: list[str] = []

    def forbidden_resolver(_host: str) -> tuple[str, ...]:
        boundary_calls.append("resolver")
        raise AssertionError("invalid literal reached the resolver")

    def forbidden_scanner(*_args: object, **_kwargs: object) -> object:
        boundary_calls.append("scanner")
        raise AssertionError("invalid literal reached the scanner")

    def forbidden_boundary(*_args: object, **_kwargs: object) -> object:
        boundary_calls.append("direct-boundary")
        raise AssertionError("invalid literal crossed a direct network boundary")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_boundary)
    monkeypatch.setattr(socket, "socket", forbidden_boundary)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_boundary)
    monkeypatch.setattr(
        import_module("nested_memvid_agent.lan_scanner"),
        "probe_manual_lan_endpoint",
        forbidden_boundary,
        raising=False,
    )
    monkeypatch.setattr(
        import_module("nested_memvid_agent.lan_manual_probe"),
        "probe_manual_lan_endpoint",
        forbidden_boundary,
        raising=False,
    )
    monkeypatch.setattr(
        _task6(),
        "probe_manual_lan_endpoint",
        forbidden_boundary,
        raising=False,
    )

    manager, state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=forbidden_resolver,
        manual_scanner=forbidden_scanner,
    )
    executor = _start_lifecycle(manager)

    def forbidden_submit(*_args: object, **_kwargs: object) -> object:
        boundary_calls.append("executor")
        raise AssertionError("invalid literal reached executor submission")

    monkeypatch.setattr(executor, "submit", forbidden_submit)
    try:
        with pytest.raises((TypeError, ValueError)):
            manager.manual_preview(interface.interface_id, host, 5001)

        assert boundary_calls == []
        assert manager.list() == []
        assert ledger.list_scans(owner_principal=FIXED_OWNER) == []
        with state._connect() as connection:
            assert {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "routing_lan_scans",
                    "routing_lan_observations",
                    "routing_lan_scan_events",
                )
            } == {
                "routing_lan_scans": 0,
                "routing_lan_observations": 0,
                "routing_lan_scan_events": 0,
            }
        assert host not in caplog.text
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_sequential_manual_preview_replacement_leaves_only_one_live_authorization(
    tmp_path: Path,
) -> None:
    manager, _state, _ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=_manual_probe_result,
        scan_id="lan_" + "0" * 32,
    )
    _start_lifecycle(manager)
    try:
        replaced = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        current = manager.manual_preview(
            interface.interface_id,
            "192.168.90.2",
            5002,
        )
        assert current.preview_digest != replaced.preview_digest

        with pytest.raises(_manual_conflict_type()):
            manager.confirm_manual(
                replaced.preview_digest,
                "192.168.90.2",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )
        started = manager.confirm_manual(
            current.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert _wait_for_terminal(manager, started.scan_id).status == "completed"
        assert [item.scan_id for item in manager.list()] == [started.scan_id]
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_admission_fails_before_start_and_after_shutdown_without_side_effects(
    tmp_path: Path,
) -> None:
    boundary_calls: Counter[str] = Counter()

    def resolver(_host: str) -> tuple[str, ...]:
        boundary_calls["resolver"] += 1
        raise AssertionError("closed manual admission invoked the resolver")

    def scanner(*_args: object, **_kwargs: object) -> object:
        boundary_calls["scanner"] += 1
        raise AssertionError("closed manual admission invoked the scanner")

    class CountingExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> None:
            boundary_calls["executor_submit"] += 1
            raise AssertionError("closed manual admission submitted work")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True
            assert cancel_futures is False
            boundary_calls["executor_shutdown"] += 1

    manager, state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=resolver,
        manual_scanner=scanner,
    )

    def durable_row_counts() -> tuple[int, int, int]:
        with state._connect() as connection:
            return tuple(
                int(connection.execute(query).fetchone()[0])
                for query in (
                    "SELECT COUNT(*) FROM routing_lan_scans",
                    "SELECT COUNT(*) FROM routing_lan_observations",
                    "SELECT COUNT(*) FROM routing_lan_scan_events",
                )
            )  # type: ignore[return-value]

    before = durable_row_counts()
    with pytest.raises(RuntimeError, match="^LAN lifecycle has not started$"):
        manager.manual_preview(interface.interface_id, "model-box.local", 5001)
    with pytest.raises(RuntimeError, match="^LAN lifecycle has not started$"):
        manager.confirm_manual(
            PREVIEW_DIGEST,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )

    executor = CountingExecutor()
    assert manager.start_lifecycle(executor) == []
    assert manager.shutdown(timeout_seconds=1.0) is True

    with pytest.raises(RuntimeError, match="^LAN scan admission is closed$"):
        manager.manual_preview(interface.interface_id, "model-box.local", 5001)
    with pytest.raises(RuntimeError, match="^LAN scan admission is closed$"):
        manager.confirm_manual(
            PREVIEW_DIGEST,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )

    assert durable_row_counts() == before == (0, 0, 0)
    assert ledger.list_scans(owner_principal=FIXED_OWNER) == []
    assert boundary_calls == Counter({"executor_shutdown": 1})


def test_manual_authorization_is_restart_local_and_shutdown_permanently_closes_admission(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=_manual_probe_result,
    )
    _start_lifecycle(manager)
    authorization = manager.manual_preview(
        interface.interface_id,
        "model-box.local",
        5001,
    )
    assert manager.shutdown(timeout_seconds=1.0) is True

    with pytest.raises((RuntimeError, _manual_conflict_type())):
        manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
    assert ledger.list_scans(owner_principal=FIXED_OWNER) == []

    restarted = task6.LanScanManager(
        ledger=ledger,
        interface_enumerator=lambda: (interface,),
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=_manual_probe_result,
        scan_id_factory=lambda: "lan_" + "1" * 32,
    )
    _start_lifecycle(restarted)
    try:
        with pytest.raises(_manual_conflict_type()):
            restarted.confirm_manual(
                authorization.preview_digest,
                "192.168.90.2",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )
        assert restarted.list() == []
        fresh = restarted.manual_preview(
            interface.interface_id,
            "192.168.90.2",
            5001,
        )
        started = restarted.confirm_manual(
            fresh.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert _wait_for_terminal(restarted, started.scan_id).status == "completed"
    finally:
        assert restarted.shutdown(timeout_seconds=2.0) is True


def test_manual_preview_rechecks_complete_inventory_after_blocked_dns_without_competing_preview(
    tmp_path: Path,
) -> None:
    selected = _interface()
    inventory = [selected]
    resolver_entered = Event()
    release_resolver = Event()
    enumeration_calls = 0

    def enumerate_inventory() -> tuple[NetworkInterface, ...]:
        nonlocal enumeration_calls
        enumeration_calls += 1
        return tuple(inventory)

    def resolver(host: str) -> tuple[str, ...]:
        assert host == "model-box.local"
        resolver_entered.set()
        assert release_resolver.wait(timeout=2.0)
        return ("192.168.90.2",)

    manager, _state, ledger, _interface_value = _manager(
        tmp_path,
        interface_enumerator=enumerate_inventory,
        manual_resolver=resolver,
        manual_scanner=_manual_probe_result,
    )
    _start_lifecycle(manager)
    results: list[object] = []
    failures: list[BaseException] = []

    def preview() -> None:
        try:
            results.append(
                manager.manual_preview(
                    selected.interface_id,
                    "model-box.local",
                    5001,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - exact conflict asserted
            failures.append(exc)

    thread = Thread(target=preview, name="task7b-dns-inventory-drift", daemon=True)
    try:
        thread.start()
        assert resolver_entered.wait(timeout=1.0)
        inventory.append(
            NetworkInterface.from_addresses(
                os_identity="darwin:en-unrelated-drift",
                display_name="Unrelated drift fixture",
                addresses=("10.92.0.1/24",),
            )
        )
        release_resolver.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert results == []
        assert len(failures) == 1
        assert isinstance(failures[0], _manual_conflict_type())
        assert "model-box.local" not in str(failures[0])
        assert enumeration_calls >= 2
        assert ledger.list_scans(owner_principal=FIXED_OWNER) == []
        with pytest.raises(_manual_conflict_type()):
            manager.confirm_manual(
                PREVIEW_DIGEST,
                "192.168.90.2",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )
        assert ledger.list_scans(owner_principal=FIXED_OWNER) == []
    finally:
        release_resolver.set()
        thread.join(timeout=1.0)
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_default_manual_resolver_uses_one_getaddrinfo_call_and_ignores_proxy_http_probe_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def getaddrinfo(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        calls.append((*args, kwargs))
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("192.168.90.2", 0),
            )
        ]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("manual preview crossed HTTP, socket, or probe boundary")

    monkeypatch.setenv("HTTP_PROXY", "http://raw-secret-proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://raw-secret-proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://raw-secret-proxy.invalid:1080")
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(
        import_module("nested_memvid_agent.lan_scanner"),
        "probe_manual_lan_endpoint",
        forbidden,
        raising=False,
    )
    manager, _state, ledger, interface = _manager(tmp_path)
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        assert authorization.resolved_addresses == ("192.168.90.2",)
        assert len(calls) == 1
        assert calls[0][0] == "model-box.local"
        assert ledger.list_scans(owner_principal=FIXED_OWNER) == []
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_default_manual_scanner_wires_exact_endpoint_probe_signature_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task6 = _task6()
    calls: list[dict[str, object]] = []

    def probe(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        calls.append(
            {
                "scope": scope,
                "endpoint": endpoint,
                "scan_deadline": scan_deadline,
                "cancellation": cancellation,
                "clock": clock,
            }
        )
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    monkeypatch.setattr(
        task6,
        "probe_manual_lan_endpoint",
        probe,
        raising=False,
    )
    manager, _state, _ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "2" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert _wait_for_terminal(manager, started.scan_id).status == "completed"
        assert len(calls) == 1
        assert type(calls[0]["endpoint"]) is _manual_endpoint_type()
        assert calls[0]["scan_deadline"] == 145.0
        assert callable(calls[0]["clock"])
        assert calls[0]["cancellation"] is not None
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_preview_resolves_outside_manager_lock_and_stale_generation_cannot_win(
    tmp_path: Path,
) -> None:
    resolver_entered = Event()
    release_resolver = Event()

    def resolver(host: str) -> tuple[str, ...]:
        assert host == "model-box.local"
        resolver_entered.set()
        assert release_resolver.wait(timeout=2.0)
        return ("192.168.90.2",)

    manager, _state, _ledger, interface = _manager(
        tmp_path,
        manual_resolver=resolver,
        manual_scanner=_manual_probe_result,
        scan_id="lan_" + "1" * 32,
    )
    _start_lifecycle(manager)
    first_results: list[object] = []
    first_failures: list[BaseException] = []

    def first_preview() -> None:
        try:
            first_results.append(
                manager.manual_preview(interface.interface_id, "model-box.local", 5001)
            )
        except BaseException as exc:  # noqa: BLE001 - losing type is asserted
            first_failures.append(exc)

    first = Thread(target=first_preview, name="task7b-manual-preview-first", daemon=True)
    lock_reader_done = Event()
    lock_reader_values: list[object] = []

    def read_interfaces() -> None:
        lock_reader_values.append(manager.interfaces())
        lock_reader_done.set()

    reader = Thread(target=read_interfaces, name="task7b-manual-preview-reader", daemon=True)
    try:
        first.start()
        assert resolver_entered.wait(timeout=1.0)

        # DNS may block, but it must not retain the operation lock or fence reads.
        reader.start()
        assert lock_reader_done.wait(timeout=0.5)
        assert lock_reader_values == [(interface,)]

        current = manager.manual_preview(interface.interface_id, "192.168.90.1", 5001)
        assert current.resolved_addresses == ("192.168.90.1",)
        release_resolver.set()
        first.join(timeout=2.0)
        assert not first.is_alive()
        assert first_results == []
        assert len(first_failures) == 1
        assert isinstance(first_failures[0], _manual_conflict_type())

        started = manager.confirm_manual(
            current.preview_digest,
            "192.168.90.1",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert started.status == "running"
        assert _wait_for_terminal(manager, started.scan_id).status == "completed"
    finally:
        release_resolver.set()
        first.join(timeout=1.0)
        if reader.ident is not None:
            reader.join(timeout=1.0)
        assert manager.shutdown(timeout_seconds=2.0) is True


@pytest.mark.parametrize(
    ("digest", "address", "revision", "confirmed", "privacy", "conflict"),
    (
        (PREVIEW_DIGEST, "192.168.90.2", True, True, True, False),
        (PREVIEW_DIGEST, "192.168.90.2", 1, True, True, False),
        (PREVIEW_DIGEST, "192.168.90.2", 0, False, True, False),
        (PREVIEW_DIGEST, "192.168.90.2", 0, 1, True, False),
        (PREVIEW_DIGEST, "192.168.90.2", 0, True, False, False),
        (PREVIEW_DIGEST, "192.168.90.2", 0, True, 1, False),
        ("sha256:" + "f" * 64, "192.168.90.2", 0, True, True, True),
        (PREVIEW_DIGEST, "192.168.90.1", 0, True, True, True),
        (PREVIEW_DIGEST, "192.168.090.002", 0, True, True, True),
    ),
    ids=(
        "bool-cas",
        "nonzero-cas",
        "unconfirmed",
        "coerced-confirmation",
        "privacy-not-acknowledged",
        "coerced-privacy",
        "digest-substitution",
        "address-substitution",
        "noncanonical-address",
    ),
)
def test_manual_confirm_requires_exact_consent_and_cached_authority_without_writes(
    tmp_path: Path,
    digest: str,
    address: str,
    revision: object,
    confirmed: object,
    privacy: object,
    conflict: bool,
) -> None:
    resolver_calls: list[str] = []

    def resolver(host: str) -> tuple[str, ...]:
        resolver_calls.append(host)
        return ("192.168.90.2",)

    manager, _state, _ledger, interface = _manager(
        tmp_path,
        manual_resolver=resolver,
        manual_scanner=_manual_probe_result,
        scan_id="lan_" + "2" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        requested_digest = authorization.preview_digest if digest == PREVIEW_DIGEST else digest
        expected_error = (
            _manual_conflict_type()
            if conflict
            else (TypeError, ValueError, LanScanRevisionConflict)
        )
        with pytest.raises(expected_error):
            manager.confirm_manual(
                requested_digest,
                address,
                expected_revision=revision,
                confirmed=confirmed,
                privacy_acknowledged=privacy,
            )

        assert resolver_calls == ["model-box.local"]
        assert manager.list() == []
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert _wait_for_terminal(manager, started.scan_id).status == "completed"
        assert resolver_calls == ["model-box.local"]
        assert [item.scan_id for item in manager.list()] == [started.scan_id]
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_manual_confirm_never_reresolves_and_consumes_authority_only_after_commit(
    tmp_path: Path,
) -> None:
    resolver_calls: list[str] = []

    def resolver(host: str) -> tuple[str, ...]:
        resolver_calls.append(host)
        return ("192.168.90.2",)

    manager, _state, _ledger, interface = _manager(
        tmp_path,
        manual_resolver=resolver,
        manual_scanner=_manual_probe_result,
        scan_id="lan_" + "3" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert resolver_calls == ["model-box.local"]
        assert _wait_for_terminal(manager, started.scan_id).status == "completed"

        with pytest.raises(_manual_conflict_type()):
            manager.confirm_manual(
                authorization.preview_digest,
                "192.168.90.2",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )
        assert resolver_calls == ["model-box.local"]
        assert len(manager.list()) == 1
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


@pytest.mark.parametrize("failure", ("expiry", "selected-interface", "complete-inventory"))
def test_manual_confirm_expiry_or_complete_inventory_drift_is_zero_write(
    tmp_path: Path,
    failure: str,
) -> None:
    clock = MutableUtcClock()
    selected = _interface()
    inventory = [selected]
    manager, _state, _ledger, interface = _manager(
        tmp_path,
        interface_enumerator=lambda: tuple(inventory),
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=_manual_probe_result,
        utc_clock=clock,
        scan_id="lan_" + "4" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        if failure == "expiry":
            clock.advance(float(_task6().LAN_PREVIEW_TTL_SECONDS))
        elif failure == "selected-interface":
            inventory[:] = [_interface(address="192.168.90.1/31")]
        else:
            inventory.append(
                NetworkInterface.from_addresses(
                    os_identity="darwin:en91",
                    display_name="Unexpected second interface",
                    addresses=("10.91.0.1/24",),
                )
            )

        with pytest.raises(_manual_conflict_type()):
            manager.confirm_manual(
                authorization.preview_digest,
                "192.168.90.2",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )
        assert manager.list() == []
    finally:
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_concurrent_manual_confirmation_has_one_claim_submission_and_cancel_lifecycle(
    tmp_path: Path,
) -> None:
    scanner_entered = Event()
    scanner_release = Event()

    def scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        scanner_entered.set()
        assert scanner_release.wait(timeout=2.0) or cancellation.is_cancelled()
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    manager, _state, _ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        scan_id="lan_" + "5" * 32,
    )

    class CountingExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=17, thread_name_prefix="task7b-counting")
            self.submit_calls = 0
            self._submit_lock = Lock()

        def submit(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def,override]
            with self._submit_lock:
                self.submit_calls += 1
            return super().submit(*args, **kwargs)

    executor = CountingExecutor()
    manager.start_lifecycle(executor)
    authorization = manager.manual_preview(interface.interface_id, "model-box.local", 5001)
    gate = Barrier(3, timeout=2.0)
    records: list[LanScanRecord] = []
    failures: list[BaseException] = []
    outcomes_lock = Lock()

    def confirm() -> None:
        try:
            gate.wait()
            record = manager.confirm_manual(
                authorization.preview_digest,
                "192.168.90.2",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )
            with outcomes_lock:
                records.append(record)
        except BaseException as exc:  # noqa: BLE001 - losing type is asserted
            with outcomes_lock:
                failures.append(exc)

    threads = tuple(
        Thread(target=confirm, name=f"task7b-confirm-{index}", daemon=True) for index in range(2)
    )
    try:
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(timeout=2.0)
        assert all(not thread.is_alive() for thread in threads)
        assert len(records) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], _manual_conflict_type())
        assert scanner_entered.wait(timeout=1.0)
        assert executor.submit_calls == 1
        assert len(manager.list()) == 1

        current = manager.get(records[0].scan_id)
        assert current is not None and current.status == "running"
        cancelling = manager.cancel(current.scan_id, expected_revision=current.revision)
        assert cancelling.status == "cancelling"
        scanner_release.set()
        terminal = _wait_for_terminal(manager, current.scan_id)
        assert terminal.status == "cancelled"
        assert manager.is_quiescent() is True
    finally:
        scanner_release.set()
        gate.abort()
        for thread in threads:
            thread.join(timeout=1.0)
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_active_slot_conflict_strands_no_draft_and_keeps_preview_retryable(
    tmp_path: Path,
) -> None:
    first_scanner_entered = Event()
    release_scanner = Event()
    calls = 0

    def scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_scanner_entered.set()
            assert release_scanner.wait(timeout=2.0)
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    first_scan_id = "lan_" + "6" * 32
    contested_scan_id = "lan_" + "7" * 32
    fallback_scan_id = "lan_" + "8" * 32
    identifiers = iter((first_scan_id, contested_scan_id, fallback_scan_id))
    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        scan_id_factory=lambda: next(identifiers),
    )
    _start_lifecycle(manager)
    try:
        first = manager.manual_preview(interface.interface_id, "192.168.90.2", 5001)
        running = manager.confirm_manual(
            first.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert first_scanner_entered.wait(timeout=1.0)

        second = manager.manual_preview(interface.interface_id, "192.168.90.1", 5002)
        with pytest.raises(_task6().LanScanAdmissionConflict):
            manager.confirm_manual(
                second.preview_digest,
                "192.168.90.1",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )
        assert [item.scan_id for item in manager.list()] == [running.scan_id]
        assert ledger.get_scan(contested_scan_id) is None
        assert ledger.list_events(contested_scan_id) == []

        current = manager.get(running.scan_id)
        assert current is not None
        manager.cancel(current.scan_id, expected_revision=current.revision)
        release_scanner.set()
        assert _wait_for_terminal(manager, current.scan_id).status == "cancelled"

        retried = manager.confirm_manual(
            second.preview_digest,
            "192.168.90.1",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert retried.scan_id in {contested_scan_id, fallback_scan_id}
        unused_scan_id = (
            fallback_scan_id if retried.scan_id == contested_scan_id else contested_scan_id
        )
        assert ledger.get_scan(unused_scan_id) is None
        assert ledger.list_events(unused_scan_id) == []
        assert _wait_for_terminal(manager, retried.scan_id).status == "completed"
        assert {item.scan_id for item in manager.list()} == {running.scan_id, retried.scan_id}
    finally:
        release_scanner.set()
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_scan_uses_shared_executor_deadline_skips_mdns_and_persists_manual_source(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    mdns_calls: list[str] = []
    scanner_calls = 0

    def scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        captured.update(
            {
                "scope": scope,
                "endpoint": endpoint,
                "scan_deadline": scan_deadline,
                "cancellation": cancellation,
                "clock": clock,
                "thread_name": current_thread().name,
            }
        )
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    manager, _state, ledger, interface = _manager(
        tmp_path,
        mdns_collector=lambda *_args, **_kwargs: mdns_calls.append("mdns"),
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "8" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)
        assert terminal.status == "completed"
        assert mdns_calls == []
        assert scanner_calls == 1
        assert captured["scan_deadline"] == 145.0
        assert str(captured["thread_name"]).startswith("task6-test-lan")
        assert type(captured["endpoint"]) is _manual_endpoint_type()
        assert captured["clock"] is not None
        assert captured["cancellation"] is not None

        observations = ledger.list_observations(started.scan_id)
        assert len(observations) == 1
        assert observations[0].source == "manual"
        assert (observations[0].address, observations[0].port) == (
            "192.168.90.2",
            5001,
        )
        event = manager.events(started.scan_id)[0]
        assert event.event_type == "scan_started"
        assert event.payload == {
            "schema": "kestrel.lan.scan-preview.manual.v1",
            "mode": "manual",
            "endpoint_kind": "manual",
            "observation_source": "manual",
            "owner_principal": FIXED_OWNER,
            "interface_id": interface.interface_id,
            "network": "192.168.90.2/32",
            "limits": _manual_limits(5001),
            "active_host_count": 1,
            "passive_or_manual_only": True,
            "port_count": 1,
            "exact_port": 5001,
            "mdns_status": "unavailable",
            "server_version": _task6().LAN_SERVER_VERSION,
            "contract_version": _task6().LAN_MANUAL_PREVIEW_CONTRACT_VERSION,
            "preview_digest": authorization.preview_digest,
            "expires_at": _utc_text(authorization.expires_at),
            "confirmed": True,
            "privacy_acknowledged": True,
        }
        assert "model-box.local" not in json.dumps(event.payload, sort_keys=True)
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["mdns_status"] == "unavailable"
        assert terminal.terminal_receipt["limits"] == _manual_limits(5001)
        assert terminal.terminal_receipt["planned_count"] == 1
        assert terminal.terminal_receipt["admitted_count"] == 1
        assert terminal.terminal_receipt["completed_count"] == 1
        progress = [
            item.payload
            for item in manager.events(started.scan_id)
            if item.event_type == "scan_progress"
        ]
        assert [
            (
                item["planned_count"],
                item["admitted_count"],
                item["completed_count"],
            )
            for item in progress
        ] == [(1, 0, 0), (1, 1, 0), (1, 1, 1)]
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True


def test_automatic_monotonic_exception_precedes_claim_and_preserves_authorization(
    tmp_path: Path,
) -> None:
    clock_calls = 0

    def monotonic_clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            raise RuntimeError("injected automatic monotonic outage")
        return 100.0

    manager, _state, ledger, interface = _manager(
        tmp_path,
        scanner=lambda *_args, **_kwargs: (),
        monotonic_clock=monotonic_clock,
        scan_id="lan_" + "3" * 32,
    )
    _start_lifecycle(manager)
    authorization = manager.preview(interface.interface_id, "192.168.90.0/30")
    draft = manager.create_draft(authorization)
    try:
        with pytest.raises(RuntimeError, match="injected automatic monotonic outage"):
            manager.start(
                draft.scan_id,
                expected_revision=draft.revision,
                authorization=authorization,
                preview_digest=authorization.preview_digest,
            )

        assert ledger.get_scan(draft.scan_id) == draft
        assert ledger.list_events(draft.scan_id) == []
        assert ledger.list_scans(status="running", owner_principal=FIXED_OWNER) == []
        assert manager.controller_count == 0

        started = manager.start(
            draft.scan_id,
            expected_revision=draft.revision,
            authorization=authorization,
            preview_digest=authorization.preview_digest,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)
        assert terminal.status == "completed"
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
    assert ledger.list_scans(status="running", owner_principal=FIXED_OWNER) == []


def test_manual_monotonic_exception_precedes_claim_and_preserves_authorization(
    tmp_path: Path,
) -> None:
    clock_calls = 0
    scanner_calls = 0
    scan_id = "lan_" + "4" * 32

    def monotonic_clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            raise RuntimeError("injected monotonic outage")
        return 100.0

    def scanner(scope: object, endpoint: object, **kwargs: object) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        return _manual_probe_result(scope, endpoint, **kwargs)

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=monotonic_clock,
        scan_id=scan_id,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )

        with pytest.raises(RuntimeError, match="injected monotonic outage"):
            manager.confirm_manual(
                authorization.preview_digest,
                "192.168.90.2",
                expected_revision=0,
                confirmed=True,
                privacy_acknowledged=True,
            )

        assert ledger.get_scan(scan_id) is None
        assert ledger.list_events(scan_id) == []
        assert ledger.list_scans(status="running", owner_principal=FIXED_OWNER) == []
        assert scanner_calls == 0

        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert _wait_for_terminal(manager, started.scan_id).status == "completed"
        assert scanner_calls == 1
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_probe_begins_only_after_durable_admission_progress(
    tmp_path: Path,
) -> None:
    scan_id = "lan_" + "5" * 32
    progress_visible_at_probe: list[tuple[int, int, int]] = []

    def scanner(scope: object, endpoint: object, **kwargs: object) -> object:
        progress_visible_at_probe.extend(
            (
                int(event.payload["planned_count"]),
                int(event.payload["admitted_count"]),
                int(event.payload["completed_count"]),
            )
            for event in ledger.list_events(scan_id)
            if event.event_type == "scan_progress"
        )
        return _manual_probe_result(scope, endpoint, **kwargs)

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id=scan_id,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert terminal.status == "completed"
        assert progress_visible_at_probe == [(1, 0, 0), (1, 1, 0)]
        assert [
            (
                event.payload["planned_count"],
                event.payload["admitted_count"],
                event.payload["completed_count"],
            )
            for event in ledger.list_events(scan_id)
            if event.event_type == "scan_progress"
        ] == [(1, 0, 0), (1, 1, 0), (1, 1, 1)]
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_admission_persistence_failure_prevents_probe_and_terminalizes(
    tmp_path: Path,
) -> None:
    progress_attempts = 0
    scanner_calls = 0

    def fail_admission_progress(operation: str) -> None:
        nonlocal progress_attempts
        if operation != "record_scan_progress":
            return
        progress_attempts += 1
        if progress_attempts == 2:
            raise RuntimeError("injected admission persistence failure")

    def scanner(scope: object, endpoint: object, **kwargs: object) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        return _manual_probe_result(scope, endpoint, **kwargs)

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        precommit_hook=fail_admission_progress,
        scan_id="lan_" + "6" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert scanner_calls == 0
        assert progress_attempts == 2
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        assert terminal.terminal_receipt is not None
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (1, 0, 0)
        assert [
            (
                event.payload["planned_count"],
                event.payload["admitted_count"],
                event.payload["completed_count"],
            )
            for event in ledger.list_events(started.scan_id)
            if event.event_type == "scan_progress"
        ] == [(1, 0, 0)]
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_planned_persistence_failure_restores_zero_counts_and_terminalizes(
    tmp_path: Path,
) -> None:
    progress_attempts = 0
    scanner_calls = 0

    def fail_planned_progress(operation: str) -> None:
        nonlocal progress_attempts
        if operation != "record_scan_progress":
            return
        progress_attempts += 1
        if progress_attempts == 1:
            raise RuntimeError("injected planned persistence failure")

    def scanner(scope: object, endpoint: object, **kwargs: object) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        return _manual_probe_result(scope, endpoint, **kwargs)

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        precommit_hook=fail_planned_progress,
        scan_id="lan_" + "c" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert scanner_calls == 0
        assert progress_attempts == 1
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        assert terminal.terminal_receipt is not None
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (0, 0, 0)
        assert [
            event
            for event in ledger.list_events(started.scan_id)
            if event.event_type == "scan_progress"
        ] == []
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True


def test_manual_scanner_exception_terminalizes_with_explicit_incomplete_evidence(
    tmp_path: Path,
) -> None:
    scanner_calls = 0

    def scanner(*_args: object, **_kwargs: object) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        raise RuntimeError("raw-secret-injected-scanner-failure")

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "7" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert scanner_calls == 1
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["evidence_complete"] is False
        assert terminal.terminal_receipt["unknown_inflight_count"] == 0
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (1, 1, 0)
        assert ledger.list_observations(started.scan_id) == []
        assert [
            (
                event.payload["planned_count"],
                event.payload["admitted_count"],
                event.payload["completed_count"],
            )
            for event in ledger.list_events(started.scan_id)
            if event.event_type == "scan_progress"
        ] == [(1, 0, 0), (1, 1, 0)]
        assert "raw-secret-injected-scanner-failure" not in json.dumps(
            terminal.terminal_receipt,
            sort_keys=True,
        )
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True


def test_manual_observation_conversion_failure_preserves_admitted_evidence_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner_calls = 0
    task6 = _task6()

    def scanner(scope: object, endpoint: object, **kwargs: object) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        return _manual_probe_result(scope, endpoint, **kwargs)

    def fail_conversion(*_args: object, **_kwargs: object) -> object:
        raise ValueError("raw-secret-observation-conversion-failure")

    monkeypatch.setattr(task6, "lan_observation_to_draft", fail_conversion)
    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "d" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert scanner_calls == 1
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["evidence_complete"] is False
        assert terminal.terminal_receipt["unknown_inflight_count"] == 0
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (1, 1, 0)
        assert ledger.list_observations(started.scan_id) == []
        assert "raw-secret-observation-conversion-failure" not in json.dumps(
            terminal.terminal_receipt,
            sort_keys=True,
        )
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True


def test_manual_worker_base_exception_after_admission_terminalizes_incomplete_failure(
    tmp_path: Path,
) -> None:
    scanner_calls = 0

    def scanner(*_args: object, **_kwargs: object) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        raise SystemExit("raw-secret-manual-system-exit")

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "a" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert scanner_calls == 1
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["evidence_complete"] is False
        assert terminal.terminal_receipt["unknown_inflight_count"] == 0
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (1, 1, 0)
        assert ledger.list_observations(started.scan_id) == []
        assert "raw-secret-manual-system-exit" not in json.dumps(
            terminal.terminal_receipt,
            sort_keys=True,
        )
        assert manager.controller_count == 0
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True
    assert ledger.list_scans(status="running", owner_principal=FIXED_OWNER) == []


def test_manual_completion_read_failure_retains_typed_evidence_and_terminalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_next_worker_read = False
    failed_reads = 0

    def scanner(scope: object, endpoint: object, **kwargs: object) -> object:
        nonlocal fail_next_worker_read
        observation = _manual_probe_result(scope, endpoint, **kwargs)
        fail_next_worker_read = True
        return observation

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "b" * 32,
    )
    durable_get = ledger.get_scan

    def transient_worker_read(scan_id: str) -> Any:
        nonlocal fail_next_worker_read, failed_reads
        if fail_next_worker_read and current_thread().name.startswith("task6-test-lan"):
            fail_next_worker_read = False
            failed_reads += 1
            raise RuntimeError("raw-secret-transient-ledger-read")
        return durable_get(scan_id)

    monkeypatch.setattr(ledger, "get_scan", transient_worker_read)
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert failed_reads == 1
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["evidence_complete"] is True
        assert terminal.terminal_receipt["unknown_inflight_count"] == 0
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (1, 1, 1)
        observations = ledger.list_observations(started.scan_id)
        assert len(observations) == 1
        assert observations[0].source == "manual"
        assert "raw-secret-transient-ledger-read" not in json.dumps(
            terminal.terminal_receipt,
            sort_keys=True,
        )
        assert manager.controller_count == 0
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True
    assert ledger.list_scans(status="running", owner_principal=FIXED_OWNER) == []


def test_manual_scanner_exception_after_cancel_fails_without_weakening_cancelled_receipts(
    tmp_path: Path,
) -> None:
    scanner_calls = 0

    def scanner(*_args: object, **_kwargs: object) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        current = ledger.get_scan("lan_" + "9" * 32)
        assert current is not None
        manager.cancel(current.scan_id, expected_revision=current.revision)
        raise RuntimeError("raw-secret-after-cancel")

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "9" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert scanner_calls == 1
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "worker_error"
        assert terminal.cancel_reason == "owner_cancelled"
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["evidence_complete"] is False
        assert terminal.terminal_receipt["unknown_inflight_count"] == 0
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (1, 1, 0)
        assert ledger.list_observations(started.scan_id) == []
        assert [event.event_type for event in ledger.list_events(started.scan_id)][-2:] == [
            "scan_cancel_requested",
            "scan_failed",
        ]
        assert "raw-secret-after-cancel" not in json.dumps(
            terminal.terminal_receipt,
            sort_keys=True,
        )
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True


def test_manual_cancel_before_admission_persistence_never_invokes_scanner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner_calls = 0

    def scanner(scope: object, endpoint: object, **kwargs: object) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        return _manual_probe_result(scope, endpoint, **kwargs)

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "8" * 32,
    )
    original_record_progress = manager._record_progress
    cancelled_before_admission = False

    def cancel_then_record(handle: Any, progress: Any) -> Any:
        nonlocal cancelled_before_admission
        if progress.phase == "admitted" and not cancelled_before_admission:
            current = ledger.get_scan(handle.scan_id)
            assert current is not None
            manager.cancel(handle.scan_id, expected_revision=current.revision)
            cancelled_before_admission = True
        return original_record_progress(handle, progress)

    monkeypatch.setattr(manager, "_record_progress", cancel_then_record)
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        started = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert cancelled_before_admission is True
        assert scanner_calls == 0
        assert terminal.status == "cancelled"
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["evidence_complete"] is True
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (1, 0, 0)
        assert [
            (
                event.payload["planned_count"],
                event.payload["admitted_count"],
                event.payload["completed_count"],
            )
            for event in ledger.list_events(started.scan_id)
            if event.event_type == "scan_progress"
        ] == [(1, 0, 0)]
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True


def test_manual_ipv6_preview_confirm_uses_exact_128_authority_and_durable_bindings(
    tmp_path: Path,
) -> None:
    selected = _interface(address="fe80::7/64")
    captured: dict[str, object] = {}

    def forbidden_resolver(_host: str) -> tuple[str, ...]:
        raise AssertionError("literal IPv6 preview must not invoke DNS")

    def scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        captured.update(
            {
                "scope": scope,
                "endpoint": endpoint,
                "scan_deadline": scan_deadline,
                "cancellation": cancellation,
                "clock": clock,
            }
        )
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    manager, _state, ledger, _default_interface = _manager(
        tmp_path,
        interface_enumerator=lambda: (selected,),
        manual_resolver=forbidden_resolver,
        manual_scanner=scanner,
        monotonic_clock=MutableMonotonicClock(),
        scan_id="lan_" + "d" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            selected.interface_id,
            "fe80::8",
            5001,
        )
        assert authorization.resolved_addresses == ("fe80::8",)
        started = manager.confirm_manual(
            authorization.preview_digest,
            "fe80::8",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert started.network == terminal.network == "fe80::8/128"
        assert started.limits == terminal.limits == _manual_limits(5001)
        scope = captured["scope"]
        endpoint = captured["endpoint"]
        assert type(scope) is PrivateScanScope
        assert scope.interface == selected
        assert scope.network == "fe80::8/128"
        assert type(endpoint) is _manual_endpoint_type()
        assert (endpoint.kind, endpoint.address, endpoint.port) == (
            "manual",
            "fe80::8",
            5001,
        )
        assert captured["scan_deadline"] == 145.0

        observations = ledger.list_observations(started.scan_id)
        assert len(observations) == 1
        assert (observations[0].source, observations[0].address, observations[0].port) == (
            "manual",
            "fe80::8",
            5001,
        )
        events = manager.events(started.scan_id)
        assert events[0].event_type == "scan_started"
        assert events[0].payload == _manual_preview_event(
            selected,
            address="fe80::8",
            port=5001,
            preview_digest=authorization.preview_digest,
            expires_at=_utc_text(authorization.expires_at),
        )
        progress = [item.payload for item in events if item.event_type == "scan_progress"]
        assert [
            (
                item["planned_count"],
                item["admitted_count"],
                item["completed_count"],
            )
            for item in progress
        ] == [(1, 0, 0), (1, 1, 0), (1, 1, 1)]
        assert terminal.status == "completed"
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["limits"] == _manual_limits(5001)
        assert terminal.terminal_receipt["mdns_status"] == "unavailable"
        assert (
            terminal.terminal_receipt["planned_count"],
            terminal.terminal_receipt["admitted_count"],
            terminal.terminal_receipt["completed_count"],
        ) == (1, 1, 1)
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True


def test_manual_mixed_family_preview_selects_one_ipv6_member_without_reresolution(
    tmp_path: Path,
) -> None:
    selected = NetworkInterface.from_addresses(
        os_identity="darwin:en-manual-dual-stack",
        display_name="Manual dual-stack selected",
        addresses=("192.168.90.1/29", "fd00::1/64"),
    )
    resolver_calls: list[str] = []
    scanner_calls: list[tuple[object, object]] = []

    def resolver(host: str) -> tuple[str, ...]:
        resolver_calls.append(host)
        return ("fd00::8", "192.168.90.2")

    def scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        scanner_calls.append((scope, endpoint))
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    manager, _state, ledger, _default_interface = _manager(
        tmp_path,
        interface_enumerator=lambda: (selected,),
        manual_resolver=resolver,
        manual_scanner=scanner,
        scan_id="lan_" + "e" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            selected.interface_id,
            "model-box.local",
            5001,
        )
        assert authorization.resolved_addresses == ("192.168.90.2", "fd00::8")
        assert resolver_calls == ["model-box.local"]

        started = manager.confirm_manual(
            authorization.preview_digest,
            "fd00::8",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, started.scan_id)

        assert started.network == terminal.network == "fd00::8/128"
        assert resolver_calls == ["model-box.local"]
        assert len(scanner_calls) == 1
        scope, endpoint = scanner_calls[0]
        assert type(scope) is PrivateScanScope
        assert scope.interface == selected
        assert scope.network == "fd00::8/128"
        assert type(endpoint) is _manual_endpoint_type()
        assert (endpoint.kind, endpoint.address, endpoint.port) == (
            "manual",
            "fd00::8",
            5001,
        )

        observations = ledger.list_observations(started.scan_id)
        assert len(observations) == 1
        assert (
            observations[0].source,
            observations[0].address,
            observations[0].port,
        ) == ("manual", "fd00::8", 5001)
        events = manager.events(started.scan_id)
        assert events[0].event_type == "scan_started"
        assert events[0].payload == _manual_preview_event(
            selected,
            address="fd00::8",
            port=5001,
            preview_digest=authorization.preview_digest,
            expires_at=_utc_text(authorization.expires_at),
        )
        assert terminal.status == "completed"
        assert [item.scan_id for item in manager.list()] == [started.scan_id]
        assert (
            ledger.list_scans(
                status="draft",
                owner_principal=FIXED_OWNER,
            )
            == []
        )
        assert {item.scan_id for item in events} == {started.scan_id}
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
        assert manager.is_quiescent() is True


def test_manual_worker_observes_shared_shutdown_cancellation_and_reaches_quiescence(
    tmp_path: Path,
) -> None:
    scanner_entered = Event()
    cancellation_observed = Event()
    emergency_release = Event()

    def scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        scanner_entered.set()
        while not cancellation.is_cancelled() and not emergency_release.wait(0.01):
            pass
        if cancellation.is_cancelled():
            cancellation_observed.set()
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        scan_id="lan_" + "9" * 32,
    )
    _start_lifecycle(manager)
    shutdown_complete = False
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        running = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        assert scanner_entered.wait(timeout=1.0)

        assert manager.shutdown(timeout_seconds=2.0) is True
        shutdown_complete = True
        assert cancellation_observed.is_set()
        terminal = ledger.get_scan(running.scan_id)
        assert terminal is not None
        assert terminal.status == "cancelled"
        assert terminal.cancel_reason == "shutdown_cancelled"
        assert manager.is_quiescent() is True
    finally:
        emergency_release.set()
        if not shutdown_complete:
            assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_terminal_precommit_deadline_crossing_rolls_back_and_retries_expired(
    tmp_path: Path,
) -> None:
    clock = MutableMonotonicClock()
    terminal_hook_calls = 0
    scanner_calls = 0

    def cross_deadline(operation: str) -> None:
        nonlocal terminal_hook_calls
        if operation != "commit_scan_terminal":
            return
        terminal_hook_calls += 1
        if terminal_hook_calls == 1:
            clock.value = 145.0

    def scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        monotonic_clock=clock,
        precommit_hook=cross_deadline,
        scan_id="lan_" + "b" * 32,
    )
    _start_lifecycle(manager)
    try:
        authorization = manager.manual_preview(
            interface.interface_id,
            "model-box.local",
            5001,
        )
        running = manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
        terminal = _wait_for_terminal(manager, running.scan_id)
        assert terminal.status == "failed"
        assert terminal.terminal_reason == "deadline_expired"
        assert terminal_hook_calls == 2
        assert scanner_calls == 1
        assert [item.event_type for item in ledger.list_events(running.scan_id)][-1] == (
            "scan_failed"
        )
        assert not any(
            item.event_type == "scan_completed" for item in ledger.list_events(running.scan_id)
        )
        assert terminal.terminal_receipt is not None
        assert terminal.terminal_receipt["limits"] == _manual_limits(5001)
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True


def test_manual_executor_rejection_terminalizes_committed_claim_and_consumes_authority(
    tmp_path: Path,
) -> None:
    scanner_calls = 0

    def scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        nonlocal scanner_calls
        scanner_calls += 1
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    class RejectingExecutor:
        def __init__(self) -> None:
            self.submit_calls = 0
            self.shutdown_calls = 0

        def submit(self, *_args: object, **_kwargs: object) -> None:
            self.submit_calls += 1
            raise RuntimeError("raw-secret-manual-executor-rejection")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True
            assert cancel_futures is False
            self.shutdown_calls += 1

    manager, _state, ledger, interface = _manager(
        tmp_path,
        manual_resolver=lambda _host: ("192.168.90.2",),
        manual_scanner=scanner,
        scan_id="lan_" + "c" * 32,
    )
    executor = RejectingExecutor()
    manager.start_lifecycle(executor)
    authorization = manager.manual_preview(
        interface.interface_id,
        "model-box.local",
        5001,
    )

    terminal = manager.confirm_manual(
        authorization.preview_digest,
        "192.168.90.2",
        expected_revision=0,
        confirmed=True,
        privacy_acknowledged=True,
    )

    assert terminal.status == "failed"
    assert terminal.terminal_reason == "worker_error"
    assert executor.submit_calls == 1
    assert scanner_calls == 0
    assert ledger.list_scans(status="draft") == []
    assert [item.event_type for item in ledger.list_events(terminal.scan_id)] == [
        "scan_started",
        "scan_failed",
    ]
    with pytest.raises(_manual_conflict_type()):
        manager.confirm_manual(
            authorization.preview_digest,
            "192.168.90.2",
            expected_revision=0,
            confirmed=True,
            privacy_acknowledged=True,
        )
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert executor.shutdown_calls == 1


def test_restart_recovery_interrupts_manual_claim_without_resolver_probe_or_submission(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    state = AgentStateStore(tmp_path / "manual-recovery" / "agent.db")
    ledger = LanDiscoveryLedger(state)
    interface = _interface()
    running = ledger.create_and_claim_manual_scan(
        scan_id="lan_" + "a" * 32,
        owner_principal=FIXED_OWNER,
        confirmed_interface_id=interface.interface_id,
        network="192.168.90.2/32",
        limits=_manual_limits(5001),
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event=_manual_preview_event(
            interface,
            address="192.168.90.2",
            port=5001,
        ),
        expected_revision=0,
    )
    calls: list[str] = []

    def manual_scanner(
        scope: object,
        endpoint: object,
        *,
        scan_deadline: float,
        cancellation: object,
        clock: object,
    ) -> object:
        calls.append("manual-scanner")
        return _manual_probe_result(
            scope,
            endpoint,
            scan_deadline=scan_deadline,
            cancellation=cancellation,
            clock=clock,
        )

    manager = task6.LanScanManager(
        ledger=ledger,
        interface_enumerator=lambda: calls.append("interfaces") or (interface,),
        mdns_availability=(
            lambda: calls.append("mdns-availability") or task6.MdnsAvailability.AVAILABLE
        ),
        mdns_collector=lambda *_args, **_kwargs: calls.append("mdns"),
        scanner=lambda *_args, **_kwargs: calls.append("automatic-scanner"),
        manual_resolver=lambda _host: calls.append("resolver") or ("192.168.90.2",),
        manual_scanner=manual_scanner,
        scan_id_factory=lambda: "unused",
    )
    executor = ThreadPoolExecutor(
        max_workers=17,
        thread_name_prefix="task7b-manual-recovery",
    )
    try:
        recovered = manager.start_lifecycle(executor)
        assert [item.scan_id for item in recovered] == [running.scan_id]
        interrupted = manager.get(running.scan_id)
        assert interrupted is not None
        assert interrupted.status == "interrupted"
        assert interrupted.terminal_receipt is not None
        assert interrupted.terminal_receipt["limits"] == _manual_limits(5001)
        assert interrupted.terminal_receipt["mdns_status"] == "unavailable"
        events = manager.events(running.scan_id)
        assert events[0].payload == _manual_preview_event(
            interface,
            address="192.168.90.2",
            port=5001,
        )
        assert events[-1].event_type == "scan_interrupted"
        assert calls == []
        assert manager.is_quiescent() is True
    finally:
        assert manager.shutdown(timeout_seconds=2.0) is True
