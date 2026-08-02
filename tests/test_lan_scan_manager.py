from __future__ import annotations

import ast
import hashlib
import inspect
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import Any

import pytest

from nested_memvid_agent.lan_discovery_models import NetworkInterface, ResolvedLanEndpoint
from nested_memvid_agent.lan_http_transport import (
    CurrentLanInterfaceInventory,
    CurrentLanInterfaceState,
    LanTransportError,
    LanTransportFailure,
)
from nested_memvid_agent.lan_scanner import (
    LanFailureCategory,
    Reachability,
    _make_observation,
    scan_lan_scope,
)
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.routing.lan_records import LanScanRevisionConflict
from nested_memvid_agent.state_store import AgentStateStore

FIXED_OWNER = "owner:local-runtime:v1"


def _task6() -> Any:
    return import_module("nested_memvid_agent.lan_scan_manager")


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
    utc_clock: MutableUtcClock | None = None,
    monotonic_clock: Any | None = None,
    precommit_hook: Any | None = None,
    scan_id: str = "lan_task6",
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
    manager = task6.LanScanManager(
        ledger=ledger,
        interface_enumerator=(interface_enumerator or (lambda: (interface,))),
        mdns_availability=(mdns_availability or (lambda: task6.MdnsAvailability.AVAILABLE)),
        mdns_collector=mdns_collector,
        scanner=scanner,
        utc_clock=utc,
        monotonic_clock=monotonic,
        scan_id_factory=lambda: scan_id,
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
            assert manager.shutdown(timeout_seconds=2.0) is True


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
        assert monotonic.calls == []
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
