from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

import nested_memvid_agent.run_manager as run_manager_module
from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.layers import MemoryCleanupIncompleteError
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.run_manager import RunCapacityError, RunManager
from nested_memvid_agent.runtime_models import AgentTurnResult
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


def test_primary_run_queue_applies_backpressure_and_drains_in_order(tmp_path) -> None:
    config = AgentConfig(
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        max_concurrent_runs=1,
        max_queued_runs=1,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
    )
    release = Event()
    lock = Lock()
    started: list[str] = []

    def slow_turn(run_id, _config, _message, _session_id):
        with lock:
            started.append(run_id)
        state.transition_run(run_id, "running")
        release.wait(timeout=2)
        state.transition_run(run_id, "completed", stop_reason="done")

    manager._run_agent_turn = slow_turn

    first = manager.create_run(message="first", autonomy_mode="manual")
    second = manager.create_run(message="second", autonomy_mode="manual")
    with pytest.raises(RunCapacityError):
        manager.create_run(message="overflow", autonomy_mode="manual")

    assert _wait_until(lambda: started == [first.run_id])
    assert state.get_run(second.run_id).status == "queued"
    manager.cancel_run(second.run_id)
    assert state.get_run(second.run_id).status == "cancelled"
    assert manager.capacity_snapshot()["queued"] == 0
    replacement = manager.create_run(message="replacement", autonomy_mode="manual")
    release.set()
    assert _wait_until(lambda: state.get_run(first.run_id).status == "completed")
    assert _wait_until(lambda: state.get_run(replacement.run_id).status == "completed")
    assert started == [first.run_id, replacement.run_id]


def test_primary_queue_continues_when_start_and_first_reconciliation_fail(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path, max_concurrent_runs=1, max_queued_runs=2)
    first_started = Event()
    release_first = Event()
    started: list[str] = []
    started_lock = Lock()

    def controlled_turn(run_id, _config, _message, _session_id):
        manager.state.transition_run(run_id, "running")
        with started_lock:
            started.append(run_id)
            is_first = len(started) == 1
        if is_first:
            first_started.set()
            assert release_first.wait(timeout=3)
        manager.state.transition_run(run_id, "completed", stop_reason="done")

    manager._run_agent_turn = controlled_turn  # type: ignore[method-assign]
    first = manager.create_run(message="active", autonomy_mode="manual")
    assert first_started.wait(timeout=3)
    queued_first = manager.create_run(message="queued first", autonomy_mode="manual")
    queued_second = manager.create_run(message="queued second", autonomy_mode="manual")

    start_attempts: list[str] = []
    reconciliation_started = Event()
    allow_reconciliation = Event()
    original_abort = manager._abort_primary_admission

    def gated_abort(run_id, error, *, publication=None) -> None:
        reconciliation_started.set()
        assert allow_reconciliation.wait(timeout=3)
        original_abort(run_id, error, publication=publication)

    class FailExactlyOneDrainStart(Thread):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._run_id = str(kwargs["args"][0])

        def start(self) -> None:
            start_attempts.append(self._run_id)
            if len(start_attempts) == 1:
                raise RuntimeError("injected queued Thread.start failure")
            super().start()

    monkeypatch.setattr(run_manager_module, "Thread", FailExactlyOneDrainStart)
    monkeypatch.setattr(manager, "_abort_primary_admission", gated_abort)
    original_transition = manager.state.transition_run
    transition_failures: list[str] = []

    def fail_first_admission_transition(run_id, status, **fields):
        if run_id == queued_first.run_id and status == "failed" and not transition_failures:
            transition_failures.append(run_id)
            raise sqlite3.OperationalError("injected admission reconciliation failure")
        return original_transition(run_id, status, **fields)

    monkeypatch.setattr(manager.state, "transition_run", fail_first_admission_transition)
    release_first.set()

    assert reconciliation_started.wait(timeout=3)
    newcomer = manager.create_run(message="newcomer", autonomy_mode="manual")
    assert manager.state.get_run(newcomer.run_id).status == "queued"
    allow_reconciliation.set()

    assert _wait_until(lambda: manager.state.get_run(queued_first.run_id).status == "failed")
    assert _wait_until(lambda: manager.state.get_run(queued_second.run_id).status == "completed")
    assert _wait_until(lambda: manager.state.get_run(newcomer.run_id).status == "completed")
    assert start_attempts == [queued_first.run_id, queued_second.run_id, newcomer.run_id]
    assert transition_failures == [queued_first.run_id]
    assert started == [first.run_id, queued_second.run_id, newcomer.run_id]
    failed = manager.state.get_run(queued_first.run_id)
    assert failed.stop_reason == "admission_setup_failed"
    assert failed.recovery_reason == "admission_setup_failed"
    assert manager.capacity_snapshot() == {
        "active": 0,
        "queued": 0,
        "reserved": 0,
        "max_active": 1,
        "max_queued": 2,
    }
    assert manager._publication_events == {}
    assert manager._publication_counts == {}
    counters = manager.operational_counters()
    assert counters["admission_reconciliation_failures"] == 1
    assert counters["admission_reconciliations_pending"] == 0


def test_primary_queue_continues_after_one_dequeued_state_read_fails(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path, max_concurrent_runs=1, max_queued_runs=2)
    first_started = Event()
    release_first = Event()
    started: list[str] = []

    def controlled_turn(run_id, _config, _message, _session_id):
        manager.state.transition_run(run_id, "running")
        started.append(run_id)
        if len(started) == 1:
            first_started.set()
            assert release_first.wait(timeout=3)
        manager.state.transition_run(run_id, "completed", stop_reason="done")

    manager._run_agent_turn = controlled_turn  # type: ignore[method-assign]
    first = manager.create_run(message="active", autonomy_mode="manual")
    assert first_started.wait(timeout=3)
    queued_first = manager.create_run(message="queued first", autonomy_mode="manual")
    queued_second = manager.create_run(message="queued second", autonomy_mode="manual")

    original_get_run = manager.state.get_run
    failed_reads: list[str] = []
    failed_read_triggered = Event()

    def fail_first_queued_read(run_id):
        if run_id == queued_first.run_id and not failed_reads:
            failed_reads.append(run_id)
            failed_read_triggered.set()
            raise sqlite3.OperationalError("injected queued state read failure")
        return original_get_run(run_id)

    monkeypatch.setattr(manager.state, "get_run", fail_first_queued_read)
    release_first.set()

    assert failed_read_triggered.wait(timeout=3)
    assert _wait_until(lambda: manager.state.get_run(queued_first.run_id).status == "failed")
    assert _wait_until(lambda: manager.state.get_run(queued_second.run_id).status == "completed")
    assert failed_reads == [queued_first.run_id]
    assert started == [first.run_id, queued_second.run_id]
    failed = manager.state.get_run(queued_first.run_id)
    assert failed.stop_reason == "admission_setup_failed"
    assert failed.recovery_reason == "admission_setup_failed"
    assert manager.capacity_snapshot() == {
        "active": 0,
        "queued": 0,
        "reserved": 0,
        "max_active": 1,
        "max_queued": 2,
    }
    assert manager._publication_events == {}
    assert manager._publication_counts == {}


def test_memvid_primary_runs_share_one_cancellable_lifecycle_slot(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_fake_memvid_sdk(monkeypatch)
    manager = _manager(
        tmp_path,
        backend="memvid",
        max_concurrent_runs=4,
        max_queued_runs=2,
    )
    first_started = Event()
    release_first = Event()
    started: list[str] = []
    started_lock = Lock()

    def controlled_turn(run_id, config, _message, _session_id):
        agent = manager._build_agent(config)
        try:
            manager.state.transition_run(run_id, "running")
            with started_lock:
                started.append(run_id)
                position = len(started)
            if position == 1:
                first_started.set()
                assert release_first.wait(timeout=3)
            manager.state.transition_run(run_id, "completed", stop_reason="done")
        finally:
            agent.close()

    manager._run_agent_turn = controlled_turn  # type: ignore[method-assign]

    first = manager.create_run(message="first", autonomy_mode="manual")
    assert first_started.wait(timeout=3)
    second = manager.create_run(message="second", autonomy_mode="manual")

    assert manager.capacity_snapshot() == {
        "active": 1,
        "queued": 1,
        "reserved": 0,
        "max_active": 1,
        "max_queued": 2,
    }
    assert manager.state.get_run(second.run_id).status == "queued"
    release_first.set()
    assert _wait_until(lambda: manager.state.get_run(first.run_id).status == "completed")
    assert _wait_until(lambda: manager.state.get_run(second.run_id).status == "completed")
    assert started == [first.run_id, second.run_id]
    assert manager.shutdown(timeout_seconds=2.0) is True

    created = [path for action, path in calls if action == "create"]
    reopened = [path for action, path in calls if action == "use"]
    assert len(created) == 6
    assert len(set(created)) == 6
    assert sorted(reopened) == sorted(created)


def test_memvid_shutdown_cancels_active_and_queued_runs_and_releases_locks(
    tmp_path,
    monkeypatch,
) -> None:
    _install_fake_memvid_sdk(monkeypatch)
    manager = _manager(
        tmp_path,
        backend="memvid",
        max_concurrent_runs=4,
        max_queued_runs=2,
    )
    first_started = Event()

    def cancellation_aware_turn(run_id, config, _message, _session_id):
        agent = manager._build_agent(config)
        try:
            manager.state.transition_run(run_id, "running")
            first_started.set()
            assert _wait_until(lambda: manager._is_cancelled(run_id), timeout=2.0)
        finally:
            agent.close()

    manager._run_agent_turn = cancellation_aware_turn  # type: ignore[method-assign]

    first = manager.create_run(message="active", autonomy_mode="manual")
    assert first_started.wait(timeout=3)
    second = manager.create_run(message="queued", autonomy_mode="manual")
    assert manager.state.get_run(second.run_id).status == "queued"

    assert manager.shutdown(timeout_seconds=2.0) is True
    assert manager.state.get_run(first.run_id).status == "cancelled"
    assert manager.state.get_run(second.run_id).status == "cancelled"
    assert manager.capacity_snapshot()["active"] == 0
    assert manager.capacity_snapshot()["queued"] == 0

    backend = MemvidBackend(
        path=tmp_path / "memory" / "working.mv2",
        layer=MemoryLayer.WORKING,
        path_lock_blocking=False,
    )
    backend.open()
    backend.close()


def test_direct_cancel_records_active_run_close_failure_without_rewriting_status(tmp_path) -> None:
    manager = _manager(tmp_path)
    started, close_calls = _install_cancellation_agent(
        manager,
        close_error="injected direct-cancel force-seal failure",
    )

    run = manager.create_run(message="cancel with a failing close", autonomy_mode="manual")
    assert started.wait(timeout=3)

    cancelled = manager.cancel_run(run.run_id)

    assert cancelled["status"] == "cancelled"
    assert _wait_until(lambda: run.run_id not in manager._threads)
    final = manager.state.get_run(run.run_id)
    assert final.status == "cancelled"
    assert final.stop_reason == "cancelled"
    assert final.recovery_reason == "cancelled_memory_close_failed"
    assert final.error is not None
    assert "injected direct-cancel force-seal failure" in final.error
    assert close_calls == ["close"]
    event_types = [event["type"] for event in manager.state.list_run_steps(run.run_id)]
    assert "run.cancellation_durability_failed" in event_types
    assert manager.operational_counters()["cancelled_run_durability_failures"] == 1


def test_shutdown_fails_when_cancelled_active_run_close_fails(tmp_path) -> None:
    manager = _manager(tmp_path)
    started, close_calls = _install_cancellation_agent(
        manager,
        close_error="injected shutdown force-seal failure",
    )
    run = manager.create_run(message="shutdown with a failing close", autonomy_mode="manual")
    assert started.wait(timeout=3)

    assert manager.shutdown(timeout_seconds=2.0) is False

    final = manager.state.get_run(run.run_id)
    assert final.status == "cancelled"
    assert final.recovery_reason == "cancelled_memory_close_failed"
    assert final.error is not None
    assert "injected shutdown force-seal failure" in final.error
    assert close_calls == ["close", "close"]
    assert manager.capacity_snapshot()["active"] == 0
    assert manager.operational_counters()["cancelled_run_durability_failures"] == 1


def test_shutdown_succeeds_when_cancelled_active_run_closes_cleanly(tmp_path) -> None:
    manager = _manager(tmp_path)
    started, close_calls = _install_cancellation_agent(manager, close_error=None)
    run = manager.create_run(message="shutdown with a clean close", autonomy_mode="manual")
    assert started.wait(timeout=3)

    assert manager.shutdown(timeout_seconds=2.0) is True

    final = manager.state.get_run(run.run_id)
    assert final.status == "cancelled"
    assert final.recovery_reason == ""
    assert final.error is None
    assert close_calls == ["close"]
    assert manager.operational_counters()["cancelled_run_durability_failures"] == 0
    assert manager._publication_events == {}
    assert manager._publication_counts == {}


@pytest.mark.parametrize("operation", ["step", "until_idle", "approve_task"])
def test_shutdown_drains_synchronous_scheduler_operations(
    tmp_path,
    operation: str,
) -> None:
    manager = _manager(
        tmp_path,
        enable_autonomous_scheduler=operation == "approve_task",
    )
    run = manager.state.create_run(
        run_id=f"sync_scheduler_{operation}",
        message="drain synchronous scheduler",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = manager.state.create_task_node(
        task_id=f"sync_task_{operation}",
        run_id=run.run_id,
        title="drain me",
        goal="drain me",
        profile="worker",
        status="queued",
        approved=operation != "approve_task",
    )
    started, _release, close_calls = _install_scheduler_shutdown_agent(
        manager,
        wait_for_explicit_release=False,
    )

    def execute() -> object:
        if operation == "step":
            return manager.run_scheduler_step(run.run_id, max_tasks=1)
        if operation == "until_idle":
            return manager.run_scheduler_until_idle(
                run.run_id,
                max_tasks=1,
                max_cycles=1,
            )
        return manager.approve_task(run.run_id, task.task_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        assert started.wait(timeout=3)
        assert manager.shutdown(timeout_seconds=2.0) is True
        future.result(timeout=1)

    assert manager.state.get_run(run.run_id).status == "cancelled"
    assert manager.state.get_task_node(task.task_id).status == "cancelled"
    assert close_calls == ["close"]
    assert manager._active_run_operations == {}


def test_shutdown_times_out_for_blocked_synchronous_scheduler_and_retries_cleanly(
    tmp_path,
) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="sync_scheduler_timeout",
        message="retain ownership until scheduler drains",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    manager.state.create_task_node(
        task_id="sync_task_timeout",
        run_id=run.run_id,
        title="blocked scheduler",
        goal="blocked scheduler",
        profile="worker",
        status="queued",
        approved=True,
    )
    started, release, close_calls = _install_scheduler_shutdown_agent(
        manager,
        wait_for_explicit_release=True,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.run_scheduler_step, run.run_id, max_tasks=1)
        assert started.wait(timeout=3)

        assert manager.shutdown(timeout_seconds=0.05) is False
        assert not future.done()
        assert manager._active_run_operations == {run.run_id: 1}
        with pytest.raises(RuntimeError, match="run_manager_shutting_down"):
            manager.run_scheduler_step(run.run_id, max_tasks=1)

        release.set()
        future.result(timeout=2)

    assert close_calls == ["close"]
    assert manager._active_run_operations == {}
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_cancelled_synchronous_scheduler_is_not_public_until_operation_closes(
    tmp_path,
) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="sync_scheduler_publication",
        message="keep cancellation private until scheduler memory closes",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    manager.state.create_task_node(
        task_id="sync_task_publication",
        run_id=run.run_id,
        title="blocked scheduler publication",
        goal="blocked scheduler publication",
        profile="worker",
        status="queued",
        approved=True,
    )
    started, release, close_calls = _install_scheduler_shutdown_agent(
        manager,
        wait_for_explicit_release=True,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        scheduler = pool.submit(manager.run_scheduler_step, run.run_id, max_tasks=1)
        assert started.wait(timeout=3)

        cancelled = manager.cancel_run(run.run_id)
        assert cancelled["status"] == "cancelled"
        public = next(item for item in manager.list_runs() if item["run_id"] == run.run_id)
        assert public["status"] == "running"
        assert public["stop_reason"] == "publication_pending"
        assert public["publication_pending"] is True
        assert manager._publication_counts == {run.run_id: 1}

        blocking_read = pool.submit(manager.get_run, run.run_id)
        sleep(0.05)
        assert not blocking_read.done()

        release.set()
        scheduler.result(timeout=2)
        final = blocking_read.result(timeout=2)

    assert close_calls == ["close"]
    assert final["status"] == "cancelled"
    assert "publication_pending" not in final
    assert manager._active_run_operations == {}
    assert manager._publication_events == {}
    assert manager._publication_counts == {}


def test_cancelled_run_owned_by_different_thread_key_waits_for_thread_close(
    tmp_path,
) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="subagent_owner_publication",
        message="keep cancellation private until subagent closes",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    started = Event()
    release = Event()
    closed: list[str] = []

    def blocking_subagent(thread_key: str) -> None:
        assert thread_key == "subagent-thread-key"
        started.set()
        assert release.wait(timeout=3)
        closed.append("closed")

    manager._start_thread(
        "subagent-thread-key",
        blocking_subagent,
        owner_run_id=run.run_id,
    )
    assert started.wait(timeout=3)

    cancelled = manager.cancel_run(run.run_id)
    assert cancelled["status"] == "cancelled"
    public = next(item for item in manager.list_runs() if item["run_id"] == run.run_id)
    assert public["status"] == "running"
    assert public["stop_reason"] == "publication_pending"
    assert manager._publication_counts == {run.run_id: 1}

    with ThreadPoolExecutor(max_workers=1) as pool:
        blocking_read = pool.submit(manager.get_run, run.run_id)
        sleep(0.05)
        assert not blocking_read.done()
        release.set()
        final = blocking_read.result(timeout=2)

    assert closed == ["closed"]
    assert final["status"] == "cancelled"
    assert "publication_pending" not in final
    assert _wait_until(lambda: "subagent-thread-key" not in manager._threads)
    assert manager._publication_events == {}
    assert manager._publication_counts == {}


def test_run_lease_acquisition_failure_does_not_leak_operation_registration(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="lease_acquisition_failure",
        message="fail before acquiring a lease",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )

    def fail_acquire(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected lease acquisition failure")

    monkeypatch.setattr(manager.state, "acquire_run_lease", fail_acquire)

    with pytest.raises(sqlite3.OperationalError, match="injected lease acquisition failure"):
        manager.run_scheduler_step(run.run_id, max_tasks=1)

    assert manager._active_run_operations == {}
    assert manager._publication_events == {}
    assert manager._publication_counts == {}
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_memvid_shutdown_interrupts_agent_waiting_for_lifecycle_slot(
    tmp_path,
    monkeypatch,
) -> None:
    _install_fake_memvid_sdk(monkeypatch)
    manager = _manager(tmp_path, backend="memvid")
    active_agent = manager.build_runtime_agent()

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(manager.build_runtime_agent)
        sleep(0.05)
        assert not waiting.done()
        assert manager.shutdown(timeout_seconds=0.0) is False
        with pytest.raises(RuntimeError, match="run_manager_shutting_down"):
            waiting.result(timeout=1)

    active_agent.close()
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_memvid_agent_build_failure_closes_layers_and_releases_lifecycle_hook(
    tmp_path,
    monkeypatch,
) -> None:
    _install_fake_memvid_sdk(monkeypatch)
    config = AgentConfig(
        backend="memvid",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
    )
    released: list[str] = []

    def fail_provider(*_args, **_kwargs):
        raise RuntimeError("injected provider construction failure")

    monkeypatch.setattr(
        "nested_memvid_agent.app_factory.build_llm_provider",
        fail_provider,
    )
    with pytest.raises(RuntimeError, match="injected provider construction failure"):
        build_agent(config, close_handler=lambda: released.append("released"))

    assert released == ["released"]
    backend = MemvidBackend(
        path=config.memory_dir / "working.mv2",
        layer=MemoryLayer.WORKING,
        path_lock_blocking=False,
    )
    backend.open()
    backend.close()


def test_memvid_agent_close_failure_keeps_slot_until_verified_retry(
    tmp_path,
    monkeypatch,
) -> None:
    _install_fake_memvid_sdk(monkeypatch)
    manager = _manager(tmp_path, backend="memvid")
    agent = manager._build_agent(manager.config)
    original_close_all = agent.memory.close_all
    allow_close = False

    def close_all() -> None:
        if not allow_close:
            raise RuntimeError("injected memory close failure")
        original_close_all()

    agent.memory.close_all = close_all  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="injected memory close failure"):
        agent.close()
    assert agent._closed is False
    assert manager._memvid_agent_active is True

    waiting = ThreadPoolExecutor(max_workers=1)
    future = waiting.submit(manager._build_agent, manager.config)
    sleep(0.05)
    assert not future.done()

    allow_close = True
    agent.close()
    replacement = future.result(timeout=2.0)
    replacement.close()
    waiting.shutdown()
    assert manager._memvid_agent_active is False
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_run_manager_shutdown_retries_quarantined_agent_close(
    tmp_path,
    monkeypatch,
) -> None:
    _install_fake_memvid_sdk(monkeypatch)
    manager = _manager(tmp_path, backend="memvid")
    agent = manager._build_agent(manager.config)
    original_close_all = agent.memory.close_all
    close_calls = 0

    def close_all() -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("injected one-shot memory close failure")
        original_close_all()

    agent.memory.close_all = close_all  # type: ignore[method-assign]
    run = manager.state.create_run(
        run_id="cancelled_close_retry",
        message="close must be retried",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    manager.state.transition_run(run.run_id, "cancelled", stop_reason="cancelled")

    with pytest.raises(RuntimeError, match="one-shot memory close failure"):
        manager._close_agent_for_run(run.run_id, agent)
    assert close_calls == 1
    assert manager._memvid_agent_active is True
    assert manager._failed_agent_closures[id(agent)] == (run.run_id, agent)

    assert manager.shutdown(timeout_seconds=1.0) is True
    assert close_calls == 2
    assert manager._memvid_agent_active is False
    assert not manager._failed_agent_closures
    assert not manager._cancelled_run_durability_failures
    assert manager.operational_counters()["cancelled_run_durability_failures"] == 1


def test_manager_retains_runless_runtime_agent_for_shutdown_retry(
    tmp_path,
    monkeypatch,
) -> None:
    _install_fake_memvid_sdk(monkeypatch)
    manager = _manager(tmp_path, backend="memvid")
    agent = manager.build_runtime_agent()
    original_close_all = agent.memory.close_all
    allow_close = False
    close_calls = 0

    def close_all() -> None:
        nonlocal close_calls
        close_calls += 1
        if not allow_close:
            raise RuntimeError("injected runless close failure")
        original_close_all()

    agent.memory.close_all = close_all  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="runless close failure"):
        manager.close_runtime_agent(agent)
    assert manager._failed_agent_closures[id(agent)] == (None, agent)
    assert manager._memvid_agent_active is True
    with pytest.raises(RuntimeError, match="memory_cleanup_incomplete"):
        manager.build_runtime_agent()

    allow_close = True
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert close_calls == 3
    assert not manager._failed_agent_closures
    assert manager._memvid_agent_active is False


def test_memvid_agent_build_cleanup_failure_is_quarantined_until_shutdown_retry(
    tmp_path,
    monkeypatch,
) -> None:
    allow_close = False
    close_attempts = 0

    class FakeMemvid:
        def close(self) -> None:
            nonlocal close_attempts
            close_attempts += 1
            if not allow_close:
                raise RuntimeError("injected construction cleanup failure")

    def create(filename: str, **_kwargs) -> FakeMemvid:
        Path(filename).write_bytes(b"fake mv2")
        return FakeMemvid()

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda _name: SimpleNamespace(create=create, use=lambda *_args, **_kwargs: FakeMemvid()),
    )
    monkeypatch.setattr(
        "nested_memvid_agent.app_factory.build_llm_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected provider construction failure")
        ),
    )
    manager = _manager(tmp_path, backend="memvid")

    with pytest.raises(MemoryCleanupIncompleteError) as raised:
        manager._build_agent(manager.config)
    assert raised.value.phase == "agent_construction"
    assert manager._memvid_agent_active is True
    assert len(manager._quarantined_memory_cleanups) == 1
    failed_close_attempts = close_attempts
    assert failed_close_attempts == len(MemoryLayer)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(manager._build_agent, manager.config)
    with pytest.raises(RuntimeError, match="memory_cleanup_incomplete"):
        future.result(timeout=1.0)
    attempts_before_successful_retry = close_attempts

    allow_close = True
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert close_attempts == attempts_before_successful_retry + len(MemoryLayer)
    executor.shutdown()
    assert manager._memvid_agent_active is False
    assert not manager._quarantined_memory_cleanups


def test_admission_setup_failure_releases_capacity_and_terminally_reconciles_run(
    tmp_path,
    monkeypatch,
) -> None:
    config = AgentConfig(
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        max_concurrent_runs=1,
        max_queued_runs=0,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
    )

    def fail_task_graph_creation(**_fields):
        raise RuntimeError("injected setup failure")

    monkeypatch.setattr(state, "create_task_graph_once", fail_task_graph_creation)
    with pytest.raises(RuntimeError, match="injected setup failure"):
        manager.create_run(message="must fail closed", autonomy_mode="manual")

    assert manager.capacity_snapshot() == {
        "active": 0,
        "queued": 0,
        "reserved": 0,
        "max_active": 1,
        "max_queued": 0,
    }
    failed = state.list_runs(limit=1)[0]
    assert failed.status == "failed"
    assert failed.stop_reason == "admission_setup_failed"
    assert failed.recovery_reason == "admission_setup_failed"


def test_cancelled_run_cancels_descendants_and_rejects_new_work(tmp_path) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="run_cancel_descendants",
        message="cancel descendants",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = manager.state.create_task_node(
        task_id="task_cancel_descendants",
        run_id=run.run_id,
        title="cancel me",
        goal="cancel me",
        profile="worker",
        status="queued",
        approved=False,
    )
    subagent = manager.state.create_subagent_run(
        subagent_id="subagent_cancel_descendants",
        run_id=run.run_id,
        task_id=task.task_id,
        profile="worker",
        goal=task.goal,
        status="queued",
    )

    cancelled = manager.cancel_run(run.run_id)

    assert cancelled["status"] == "cancelled"
    assert manager.state.get_task_node(task.task_id).status == "cancelled"
    assert manager.state.get_subagent_run(subagent.subagent_id).status == "cancelled"
    with pytest.raises(ValueError, match="scheduler_not_allowed_for_terminal_run:cancelled"):
        manager.run_scheduler_step(run.run_id)
    with pytest.raises(ValueError, match="task_approval_not_allowed_for_terminal_run:cancelled"):
        manager.approve_task(run.run_id, task.task_id)
    with pytest.raises(
        ValueError, match="subagent_creation_not_allowed_for_terminal_run:cancelled"
    ):
        manager.create_subagent(run_id=run.run_id, profile="worker", goal="too late")


def test_concurrent_scheduler_steps_claim_each_task_exactly_once(tmp_path) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="run_scheduler_claim_race",
        message="race schedulers",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = manager.state.create_task_node(
        task_id="task_scheduler_claim_race",
        run_id=run.run_id,
        title="single execution",
        goal="execute once",
        profile="worker",
        status="queued",
        approved=True,
    )
    chat_calls: list[str] = []
    chat_lock = Lock()
    chat_started = Event()
    release_chat = Event()

    class FakeAgent:
        def chat(self, message, **_kwargs):
            with chat_lock:
                chat_calls.append(message)
            chat_started.set()
            assert release_chat.wait(timeout=3)
            return AgentTurnResult(
                session_id="session",
                user_message=message,
                assistant_message="executed exactly once",
                tool_executions=(),
                context_chars=0,
                memory_writes=(),
                stop_reason="complete",
            )

        def close(self):
            return None

    manager._build_agent = lambda _config: FakeAgent()  # type: ignore[method-assign]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager.run_scheduler_step, run.run_id, max_tasks=1)
        assert chat_started.wait(timeout=3)
        second = pool.submit(manager.run_scheduler_step, run.run_id, max_tasks=1)
        sleep(0.05)
        assert not second.done()
        assert manager.state.get_task_node(task.task_id).status == "running"
        release_chat.set()
        futures = [first, second]
        results = [future.result(timeout=5) for future in futures]

    assert len(chat_calls) == 1
    assert sum(len(result["executed"]) for result in results) == 1
    assert manager.state.get_task_node(task.task_id).status == "completed"
    assert len(manager.state.list_subagent_runs(run.run_id)) == 1


def test_scheduler_task_close_failure_fails_worker_before_terminal_publication(
    tmp_path,
) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="run_scheduler_close_failure",
        message="force-seal scheduler memory",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = manager.state.create_task_node(
        task_id="task_scheduler_close_failure",
        run_id=run.run_id,
        title="Durably complete scheduler work",
        goal="Durably complete scheduler work",
        profile="worker",
        status="queued",
        approved=True,
    )

    class CloseFailingAgent:
        def chat(self, message, **_kwargs):
            return AgentTurnResult(
                session_id="session",
                user_message=message,
                assistant_message="Durably completed scheduler work.",
                tool_executions=(),
                context_chars=0,
                memory_writes=("working:record",),
                stop_reason="complete",
            )

        def close(self):
            raise RuntimeError("injected scheduler memory force-seal failure")

    manager._build_agent = lambda _config: CloseFailingAgent()  # type: ignore[method-assign]

    step = manager.run_scheduler_step(run.run_id, max_tasks=1)

    assert step["executed"][0]["status"] == "failed"
    assert manager.state.get_task_node(task.task_id).status == "failed"
    assert manager.state.list_subagent_runs(run.run_id)[0].status == "failed"
    event_types = {event["type"] for event in manager.state.list_run_steps(run.run_id)}
    assert "task.completed" not in event_types
    assert "subagent.completed" not in event_types


def test_public_subagent_close_failure_fails_worker_before_terminal_publication(
    tmp_path,
) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="run_subagent_close_failure",
        message="force-seal public subagent memory",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )

    class CloseFailingAgent:
        def chat(self, message, **_kwargs):
            return AgentTurnResult(
                session_id="session",
                user_message=message,
                assistant_message="Durably completed public subagent work.",
                tool_executions=(),
                context_chars=0,
                memory_writes=("working:record",),
                stop_reason="complete",
            )

        def close(self):
            raise RuntimeError("injected subagent memory force-seal failure")

    manager._build_agent = lambda _config: CloseFailingAgent()  # type: ignore[method-assign]

    created = manager.create_subagent(
        run_id=run.run_id,
        profile="worker",
        goal="Durably complete public subagent work",
    )

    assert _wait_until(
        lambda: manager.state.get_subagent_run(str(created["subagent_id"])).status == "failed"
    )
    assert manager.state.get_task_node(str(created["task_id"])).status == "failed"
    event_types = {event["type"] for event in manager.state.list_run_steps(run.run_id)}
    assert "task.completed" not in event_types
    assert "subagent.completed" not in event_types


def test_run_heartbeat_error_revokes_execution_and_terminalizes_run(tmp_path, monkeypatch) -> None:
    manager = _manager(
        tmp_path,
        run_heartbeat_interval_seconds=0.01,
        # Keep lease expiry out of this heartbeat-error test. Hosted Windows
        # can spend more than 200 ms in SQLite while the full suite is busy;
        # expiry fencing has separate deterministic coverage.
        run_lease_ttl_seconds=5.0,
    )
    run = manager.state.create_run(
        run_id="run_heartbeat_failure",
        message="lose lease",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    heartbeat_failed = Event()

    def fail_renewal(*_args, **_kwargs):
        heartbeat_failed.set()
        raise sqlite3.OperationalError("injected heartbeat failure")

    monkeypatch.setattr(manager.state, "renew_run_lease", fail_renewal)

    with manager._run_lease(run.run_id, manager.config) as lease:
        assert lease is not None
        assert heartbeat_failed.wait(timeout=1)
        assert _wait_until(lambda: manager._is_cancelled(run.run_id))
        assert _wait_until(lambda: manager.state.get_run(run.run_id).status == "failed")

    failed = manager.state.get_run(run.run_id)
    assert failed.status == "failed"
    assert failed.stop_reason == "run_lease_lost"
    assert any(
        event["type"] == "run.lease_lost" for event in manager.state.list_run_steps(run.run_id)
    )


def test_worker_heartbeat_error_revokes_claim_before_execution(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="run_worker_heartbeat_failure",
        message="worker heartbeat",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = manager.state.create_task_node(
        task_id="task_worker_heartbeat_failure",
        run_id=run.run_id,
        title="worker heartbeat",
        goal="worker heartbeat",
        profile="worker",
        status="queued",
        approved=True,
    )
    claimed = manager.state.claim_task_node(
        task.task_id,
        run_id=run.run_id,
        worker_owner=manager._lease_owner,
        worker_claim_id="worker_claim",
    )
    assert claimed is not None

    def fail_heartbeat(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected worker heartbeat failure")

    monkeypatch.setattr(manager.state, "heartbeat_task_claim", fail_heartbeat)

    with manager._worker_heartbeat(
        task.task_id,
        manager.config,
        run_id=run.run_id,
        worker_owner=manager._lease_owner,
        worker_claim_id="worker_claim",
    ) as lost:
        assert lost.is_set()

    assert manager.state.get_task_node(task.task_id).status == "failed"
    assert any(
        event["type"] == "worker.heartbeat_lost"
        for event in manager.state.list_run_steps(run.run_id)
    )


def test_scheduler_reports_worker_heartbeat_fence_loss_as_failure(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="run_scheduler_heartbeat_failure",
        message="scheduler heartbeat",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = manager.state.create_task_node(
        task_id="task_scheduler_heartbeat_failure",
        run_id=run.run_id,
        title="scheduler heartbeat",
        goal="scheduler heartbeat",
        profile="worker",
        status="queued",
        approved=True,
    )

    def fail_heartbeat(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected scheduler heartbeat failure")

    monkeypatch.setattr(manager.state, "heartbeat_task_claim", fail_heartbeat)
    manager._build_agent = lambda _config: pytest.fail(  # type: ignore[method-assign]
        "agent construction must not happen after the initial execution fence fails"
    )

    step = manager.run_scheduler_step(run.run_id, max_tasks=1)

    assert step["executed"][0]["status"] == "failed"
    assert manager.state.get_task_node(task.task_id).status == "failed"
    assert manager.state.list_subagent_runs(run.run_id)[0].status == "failed"


def _manager(tmp_path, **config_overrides) -> RunManager:
    config = AgentConfig(
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        **config_overrides,
    )
    state = AgentStateStore(config.state_path)
    return RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
    )


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.01)
    return bool(predicate())


def _install_cancellation_agent(
    manager: RunManager,
    *,
    close_error: str | None,
) -> tuple[Event, list[str]]:
    started = Event()
    close_calls: list[str] = []

    class CancellationAgent:
        def chat(self, message: str, **kwargs: object) -> AgentTurnResult:
            started.set()
            run_id = str(kwargs["run_id"])
            assert _wait_until(lambda: manager._is_cancelled(run_id))
            return AgentTurnResult(
                session_id=str(kwargs["session_id"]),
                user_message=message,
                assistant_message="cancelled after active execution",
                tool_executions=(),
                context_chars=0,
                memory_writes=("dirty-before-cancel",),
                stop_reason="complete",
            )

        def close(self) -> None:
            close_calls.append("close")
            if close_error is not None:
                raise RuntimeError(close_error)

    manager._build_agent = lambda _config: CancellationAgent()  # type: ignore[method-assign]
    return started, close_calls


def _install_scheduler_shutdown_agent(
    manager: RunManager,
    *,
    wait_for_explicit_release: bool,
) -> tuple[Event, Event, list[str]]:
    started = Event()
    release = Event()
    close_calls: list[str] = []

    class SchedulerAgent:
        def chat(self, message: str, **kwargs: object) -> AgentTurnResult:
            started.set()
            run_id = str(kwargs["run_id"])
            execution_origin = str(kwargs["execution_origin"])
            assert execution_origin.startswith("subagent:")
            persisted_subagent = manager.state.get_subagent_run(
                execution_origin.removeprefix("subagent:")
            )
            assert persisted_subagent.run_id == run_id
            if wait_for_explicit_release:
                assert release.wait(timeout=3)
            else:
                assert _wait_until(lambda: manager._is_cancelled(run_id))
            return AgentTurnResult(
                session_id=str(kwargs["session_id"]),
                user_message=message,
                assistant_message="scheduler drained",
                tool_executions=(),
                context_chars=0,
                memory_writes=(),
                stop_reason="complete",
            )

        def close(self) -> None:
            close_calls.append("close")

    manager._build_agent = lambda _config: SchedulerAgent()  # type: ignore[method-assign]
    return started, release, close_calls


def _install_fake_memvid_sdk(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    class FakeMemvid:
        def close(self) -> None:
            return None

    def create(filename: str, **_kwargs) -> FakeMemvid:
        calls.append(("create", filename))
        Path(filename).write_bytes(b"fake mv2")
        return FakeMemvid()

    def use(_kind: str, filename: str, **_kwargs) -> FakeMemvid:
        calls.append(("use", filename))
        return FakeMemvid()

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda _name: SimpleNamespace(create=create, use=use),
    )
    return calls
