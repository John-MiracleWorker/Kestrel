from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.run_manager import AdaptiveFlockRunManager
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.state_store import RunRecord, SubagentRunRecord, TaskNodeRecord


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    def publish(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.items.append((run_id, event_type, payload))


class _State:
    def __init__(self) -> None:
        self.run = RunRecord(
            run_id="run-route-start",
            status="running",
            message="route a worker",
            session_id="session-route-start",
            workspace=".",
            provider="mock",
            model="mock",
        )
        self.task = TaskNodeRecord(
            task_id="task-route-start",
            run_id=self.run.run_id,
            title="Bounded worker",
            goal="Apply the assigned bounded change.",
            profile="worker",
            status="running",
            approved=True,
            required_tools=("repo.search",),
            risk="low",
            acceptance_criteria=("The result is validated.",),
        )
        self.subagent = SubagentRunRecord(
            subagent_id="subagent-route-start",
            run_id=self.run.run_id,
            task_id=self.task.task_id,
            profile="worker",
            goal=self.task.goal,
            status="queued",
        )

    def get_run(self, run_id: str) -> RunRecord:
        assert run_id == self.run.run_id
        return self.run

    def get_task_node(self, task_id: str) -> TaskNodeRecord:
        assert task_id == self.task.task_id
        return self.task

    def get_subagent_run(self, subagent_id: str) -> SubagentRunRecord:
        assert subagent_id == self.subagent.subagent_id
        return self.subagent

    def transition_scheduler_task_and_subagent(
        self,
        task_id: str,
        status: str,
        **kwargs: Any,
    ) -> tuple[TaskNodeRecord, SubagentRunRecord, bool]:
        assert task_id == self.task.task_id
        task_fields = dict(kwargs.get("task_fields", {}))
        self.task = replace(
            self.task,
            status=status,
            attempt_count=self.task.attempt_count
            + (1 if kwargs.get("increment_attempt") else 0),
            failure_reason=str(task_fields.get("failure_reason", self.task.failure_reason)),
            diagnosis=task_fields.get("diagnosis", self.task.diagnosis),
            retry_strategy=task_fields.get("retry_strategy", self.task.retry_strategy),
            result=task_fields.get("result", self.task.result),
        )
        self.subagent = replace(
            self.subagent,
            status=status,
            error=kwargs.get("subagent_error", self.subagent.error),
        )
        return self.task, self.subagent, True


class _Coordinator:
    def __init__(self, *, mode: str) -> None:
        self.mode = mode

    def assign(
        self,
        config: AgentConfig,
        task: TaskNodeRecord,
        *,
        subagent_id: str | None,
        attempt: int,
    ) -> Any:
        record = SimpleNamespace(
            decision_id="route-start-failure",
            task_id=task.task_id,
            subagent_id=subagent_id,
            attempt=attempt,
            mode=self.mode,
            policy_id="balanced",
            selected_target_id="local-worker",
            selected_provider="openai-compatible",
            selected_model="local-worker",
            selection_kind="deterministic_router",
            score=0.9,
            reason_codes=("highest_admissible_score",),
            actionable=self.mode != "shadow",
        )
        return SimpleNamespace(
            assignment=SimpleNamespace(
                config=replace(config, provider="openai-compatible", model="local-worker"),
                executes_selected_target=record.actionable,
            ),
            record=record,
            reused=False,
        )

    def mark_started(self, _durable: Any) -> None:
        raise RuntimeError("database unavailable")


def _manager(state: _State, events: _Events, coordinator: _Coordinator) -> AdaptiveFlockRunManager:
    manager = object.__new__(AdaptiveFlockRunManager)
    manager.state = state  # type: ignore[assignment]
    manager.events = events  # type: ignore[assignment]
    manager.routing_coordinator = coordinator  # type: ignore[assignment]
    manager._lease_owner = "manager-test"
    manager._cancelled = set()

    def complete_root(_run_id: str) -> None:
        return

    manager._maybe_complete_root_task = complete_root  # type: ignore[method-assign]
    return manager


def test_shadow_start_failure_executes_baseline_provider(monkeypatch: Any) -> None:
    state = _State()
    events = _Events()
    manager = _manager(state, events, _Coordinator(mode="shadow"))
    observed: list[AgentConfig] = []

    def parent_run(
        _self: RunManager,
        _thread_key: str,
        config: AgentConfig,
        _subagent_id: str,
        _run_id: str,
        _session_id: str,
    ) -> None:
        observed.append(config)

    monkeypatch.setattr(RunManager, "_run_subagent", parent_run)
    monkeypatch.setattr(AdaptiveFlockRunManager, "_is_cancelled", lambda _self, _run_id: False)
    baseline = AgentConfig(provider="mock", model="baseline")

    manager._run_subagent(
        "thread",
        baseline,
        state.subagent.subagent_id,
        state.run.run_id,
        state.run.session_id,
    )

    assert observed == [baseline]
    assert state.task.status == "running"
    assert "routing.start_failed" in {event_type for _, event_type, _ in events.items}


def test_constrained_start_failure_blocks_before_provider_execution(monkeypatch: Any) -> None:
    state = _State()
    events = _Events()
    manager = _manager(state, events, _Coordinator(mode="constrained"))
    called = False

    def parent_run(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(RunManager, "_run_subagent", parent_run)
    monkeypatch.setattr(AdaptiveFlockRunManager, "_is_cancelled", lambda _self, _run_id: False)

    manager._run_subagent(
        "thread",
        AgentConfig(provider="mock", model="baseline"),
        state.subagent.subagent_id,
        state.run.run_id,
        state.run.session_id,
    )

    assert called is False
    assert state.task.status == "failed"
    assert state.subagent.status == "failed"
    assert state.task.diagnosis == {
        "category": "routing_persistence_failed",
        "reason_codes": ["routing_start_failed"],
    }
    assert "routing.start_failed" in {event_type for _, event_type, _ in events.items}
