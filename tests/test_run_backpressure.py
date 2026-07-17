from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from time import monotonic, sleep

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
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

    def fail_task_creation(**_fields):
        raise RuntimeError("injected setup failure")

    monkeypatch.setattr(state, "create_task_node", fail_task_creation)
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
    with pytest.raises(ValueError, match="subagent_creation_not_allowed_for_terminal_run:cancelled"):
        manager.create_subagent(run_id=run.run_id, profile="worker", goal="too late")


def test_concurrent_scheduler_steps_claim_each_task_exactly_once(tmp_path, monkeypatch) -> None:
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
    original_claim = manager.state.claim_task_node
    claim_barrier = Barrier(2)

    def synchronized_claim(task_id, **kwargs):
        claim_barrier.wait(timeout=3)
        return original_claim(task_id, **kwargs)

    monkeypatch.setattr(manager.state, "claim_task_node", synchronized_claim)
    chat_calls: list[str] = []
    chat_lock = Lock()

    class FakeAgent:
        def chat(self, message, **_kwargs):
            with chat_lock:
                chat_calls.append(message)
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
        futures = [pool.submit(manager.run_scheduler_step, run.run_id, max_tasks=1) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]

    assert len(chat_calls) == 1
    assert sum(len(result["executed"]) for result in results) == 1
    assert manager.state.get_task_node(task.task_id).status == "completed"
    assert len(manager.state.list_subagent_runs(run.run_id)) == 1


def test_run_heartbeat_error_revokes_execution_and_terminalizes_run(tmp_path, monkeypatch) -> None:
    manager = _manager(
        tmp_path,
        run_heartbeat_interval_seconds=0.01,
        run_lease_ttl_seconds=0.2,
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

    failed = manager.state.get_run(run.run_id)
    assert failed.status == "failed"
    assert failed.stop_reason == "run_lease_lost"
    assert any(
        event["type"] == "run.lease_lost"
        for event in manager.state.list_run_steps(run.run_id)
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
