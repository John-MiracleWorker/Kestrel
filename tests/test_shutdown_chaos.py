from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import nested_memvid_agent.lan_mdns as lan_mdns
from nested_memvid_agent.lan_discovery_models import NetworkInterface
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.state_store import AgentStateStore


def _task6() -> Any:
    return import_module("nested_memvid_agent.lan_scan_manager")


def _interface() -> NetworkInterface:
    return NetworkInterface.from_addresses(
        os_identity="darwin:en92",
        display_name="Shutdown fixture",
        addresses=("192.168.92.1/30",),
    )


def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def _started_manager(tmp_path: Path, **kwargs: Any) -> tuple[Any, NetworkInterface]:
    task6 = _task6()
    interface = _interface()
    manager = task6.LanScanManager(
        ledger=LanDiscoveryLedger(AgentStateStore(tmp_path / "state.db")),
        interface_enumerator=lambda: (interface,),
        mdns_availability=lambda: task6.MdnsAvailability.AVAILABLE,
        scan_id_factory=lambda: "lan_shutdown",
        **kwargs,
    )
    manager.start_lifecycle(ThreadPoolExecutor(max_workers=17, thread_name_prefix="task6-shutdown"))
    return manager, interface


def _start(manager: Any, interface: NetworkInterface) -> Any:
    authorization = manager.preview(interface.interface_id, "192.168.92.0/30")
    draft = manager.create_draft(authorization)
    return manager.start(
        draft.scan_id,
        expected_revision=draft.revision,
        authorization=authorization,
        preview_digest=authorization.preview_digest,
    )


def test_shutdown_retains_controller_and_probe_future_until_retry_quiesces(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()

    def stuck_scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        entered.set()
        release.wait(timeout=5)
        return ()

    manager, interface = _started_manager(tmp_path, scanner=stuck_scanner)
    running = _start(manager, interface)
    assert entered.wait(timeout=2)

    assert manager.shutdown(timeout_seconds=0.01) is False
    assert running.scan_id in manager.retained_controller_ids()
    assert manager.is_quiescent() is False
    cancelling = manager.get(running.scan_id)
    assert cancelling.status == "cancelling"
    assert cancelling.terminal_receipt is None
    token = manager._active_scans[running.scan_id].cancellation  # noqa: SLF001
    assert token.is_cancelled() is True
    assert [event.event_type for event in manager.events(running.scan_id)] == [
        "scan_started",
        "scan_cancel_requested",
    ]

    release.set()
    _wait_until(lambda: manager.get(running.scan_id).is_terminal)
    terminal = manager.get(running.scan_id)
    assert terminal.status == "cancelled"
    assert terminal.terminal_receipt is not None
    assert [event.event_type for event in manager.events(running.scan_id)] == [
        "scan_started",
        "scan_cancel_requested",
        "scan_cancelled",
    ]
    _wait_until(lambda: manager.controller_count == 0)
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert manager.is_quiescent() is True
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_shutdown_retains_mdns_cleanup_handle_until_idempotent_retry(
    tmp_path: Path,
) -> None:
    cleanup_release = Event()
    cleanup_registered = Event()
    scanner_calls = 0

    class RetainedCleanup:
        def is_quiescent(self) -> bool:
            return cleanup_release.is_set()

        def wait_quiescent(self, *, timeout_seconds: float) -> bool:
            return cleanup_release.wait(timeout=max(0.0, timeout_seconds))

    cleanup = RetainedCleanup()

    def collector(_scope: Any, **kwargs: Any) -> Any:
        kwargs["cleanup_handle_sink"](cleanup)
        cleanup_registered.set()
        raise TimeoutError("bounded mDNS cleanup timed out")

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        nonlocal scanner_calls
        scanner_calls += 1
        return ()

    manager, interface = _started_manager(
        tmp_path,
        mdns_collector=collector,
        scanner=scanner,
    )
    running = _start(manager, interface)
    assert cleanup_registered.wait(timeout=2)

    assert manager.shutdown(timeout_seconds=0.01) is False
    assert manager.retained_cleanup_count == 1
    assert manager.is_quiescent() is False
    assert scanner_calls == 0
    cancelling = manager.get(running.scan_id)
    assert cancelling.status == "cancelling"
    assert cancelling.terminal_receipt is None
    token = manager._active_scans[running.scan_id].cancellation  # noqa: SLF001
    assert token.is_cancelled() is True
    assert [event.event_type for event in manager.events(running.scan_id)] == [
        "scan_started",
        "scan_cancel_requested",
    ]

    cleanup_release.set()
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_finished_but_failed_live_mdns_cleanup_retains_manager_authority(
    tmp_path: Path,
) -> None:
    class FakeLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback: Any) -> None:
            callback()

    class FailingZeroconf:
        loop = FakeLoop()

        def close(self) -> None:
            raise RuntimeError("injected finished cleanup failure")

    class FakeBrowser:
        def __init__(self, zeroconf: FailingZeroconf) -> None:
            self.zc = zeroconf
            self.queue = SimpleNamespace(put=self._queue_put)
            self.alive = True

        def _queue_put(self, _value: object) -> None:
            self.alive = False

        def _async_cancel(self) -> None:
            self.alive = False

        def join(self, *, timeout: float) -> None:
            assert timeout == 0.25

        def is_alive(self) -> bool:
            return self.alive

    zeroconf = FailingZeroconf()
    session = lan_mdns._LiveMdnsSession(zeroconf, FakeBrowser(zeroconf))  # noqa: SLF001
    scanner_calls = 0

    def collector(_scope: Any, **kwargs: Any) -> Any:
        kwargs["cleanup_handle_sink"](session)
        session.close(timeout_seconds=1.0)
        raise AssertionError("cleanup failure must remain visible")

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        nonlocal scanner_calls
        scanner_calls += 1
        return ()

    manager, interface = _started_manager(
        tmp_path,
        mdns_collector=collector,
        scanner=scanner,
    )
    running = _start(manager, interface)
    assert session._cleanup_done.wait(timeout=2.0)  # noqa: SLF001
    _wait_until(
        lambda: manager._active_scans[running.scan_id].controller_finished.is_set()  # noqa: SLF001
    )

    assert session.is_quiescent() is False
    assert session.wait_quiescent(timeout_seconds=0.0) is False
    assert manager.shutdown(timeout_seconds=0.01) is False
    assert manager.retained_cleanup_count == 1
    assert manager.is_quiescent() is False
    assert scanner_calls == 0
    cancelling = manager.get(running.scan_id)
    assert cancelling.status == "cancelling"
    assert cancelling.terminal_receipt is None

    # Test-only fault release permits orderly executor teardown after proving
    # that production authority remains retained for the persistent error.
    session._cleanup_error = None  # noqa: SLF001
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_just_late_cleanup_autonomously_fails_without_status_side_effects(
    tmp_path: Path,
) -> None:
    task6 = _task6()
    cleanup_waited = Event()
    cleanup_release = Event()
    scanner_calls = 0

    class AlreadyFinishedCleanup:
        def is_quiescent(self) -> bool:
            return True

        def is_finished(self) -> bool:
            return True

        def wait_quiescent(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            return True

    class JustLateCleanup:
        def is_quiescent(self) -> bool:
            return cleanup_release.is_set()

        def is_finished(self) -> bool:
            return False

        def wait_quiescent(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            cleanup_waited.set()
            return False

    def collector(_scope: Any, **kwargs: Any) -> Any:
        kwargs["cleanup_handle_sink"](AlreadyFinishedCleanup())
        kwargs["cleanup_handle_sink"](JustLateCleanup())
        return task6.MdnsCollection(
            availability=task6.MdnsAvailability.AVAILABLE,
            candidates=(),
        )

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        nonlocal scanner_calls
        scanner_calls += 1
        return ()

    manager, interface = _started_manager(
        tmp_path,
        mdns_collector=collector,
        scanner=scanner,
    )
    running = _start(manager, interface)
    assert cleanup_waited.wait(timeout=2.0)
    ledger = manager._ledger  # noqa: SLF001
    assert ledger.get_scan(running.scan_id).status == "running"

    def release_after_old_grace() -> None:
        time.sleep(0.35)
        cleanup_release.set()

    release_thread = Thread(target=release_after_old_grace, name="late-cleanup-release")
    release_thread.start()
    _wait_until(lambda: ledger.get_scan(running.scan_id).is_terminal)
    release_thread.join(timeout=1.0)
    terminal = ledger.get_scan(running.scan_id)
    assert terminal.status == "failed"
    assert terminal.terminal_reason == "worker_error"
    assert scanner_calls == 0
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_expired_shared_deadline_reaches_cleanup_as_zero_and_retains_authority(
    tmp_path: Path,
) -> None:
    task6 = _task6()

    class Clock:
        value = 100.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    cleanup_release = Event()
    cleanup_registered = Event()
    cleanup_timeouts: list[float] = []
    scanner_calls = 0

    class Cleanup:
        def is_quiescent(self) -> bool:
            return cleanup_release.is_set()

        def wait_quiescent(self, *, timeout_seconds: float) -> bool:
            cleanup_timeouts.append(timeout_seconds)
            return cleanup_release.wait(timeout=max(0.0, timeout_seconds))

    cleanup = Cleanup()

    def collector(_scope: Any, **kwargs: Any) -> Any:
        kwargs["cleanup_handle_sink"](cleanup)
        cleanup_registered.set()
        clock.value = kwargs["absolute_deadline"] + 1.0
        return task6.MdnsCollection(
            availability=task6.MdnsAvailability.TIMED_OUT,
            candidates=(),
        )

    def scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        nonlocal scanner_calls
        scanner_calls += 1
        return ()

    manager, interface = _started_manager(
        tmp_path,
        mdns_collector=collector,
        scanner=scanner,
        monotonic_clock=clock,
    )
    running = _start(manager, interface)
    assert cleanup_registered.wait(timeout=2)

    assert manager.shutdown(timeout_seconds=0.01) is False
    assert cleanup_timeouts
    assert set(cleanup_timeouts) == {0.0}
    assert scanner_calls == 0
    cancelling = manager.get(running.scan_id)
    assert cancelling.status == "cancelling"
    assert cancelling.terminal_receipt is None

    cleanup_release.set()
    assert manager.shutdown(timeout_seconds=1.0) is True
    terminal = manager.get(running.scan_id)
    assert terminal.status == "cancelled"
    assert terminal.terminal_receipt is not None
    terminal = manager.get(running.scan_id)
    assert terminal.status == "cancelled"
    assert terminal.terminal_receipt is not None
    assert [event.event_type for event in manager.events(running.scan_id)] == [
        "scan_started",
        "scan_cancel_requested",
        "scan_cancelled",
    ]
    assert manager.retained_cleanup_count == 0
    assert manager.is_quiescent() is True
    assert manager.shutdown(timeout_seconds=1.0) is True
