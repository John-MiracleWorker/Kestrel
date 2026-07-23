from __future__ import annotations

from dataclasses import replace
from inspect import getsource
from threading import Lock
from types import SimpleNamespace
from typing import Any

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.run_manager import AdaptiveFlockRunManager
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.state_store import (
    RunRecord,
    SubagentRunRecord,
    TaskNodeRecord,
)


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    def publish(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.items.append((run_id, event_type, payload))


class _State:
    def __init__(self) -> None:
        self.task = TaskNodeRecord(
            task_id="task-scheduler-route",
            run_id="run-scheduler-route",
            title="Inspect repository",
            goal="Gather repository context.",
            profile="worker",
            status="running",
            approved=True,
            required_tools=("repo.search",),
            risk="low",
            acceptance_criteria=(),
        )
        self.subagent = SubagentRunRecord(
            subagent_id="subagent-scheduler-route",
            run_id=self.task.run_id,
            task_id=self.task.task_id,
            profile="worker",
            goal=self.task.goal,
            status="running",
        )

    def get_task_node(self, task_id: str) -> TaskNodeRecord:
        assert task_id == self.task.task_id
        return self.task

    def get_subagent_run(self, subagent_id: str) -> SubagentRunRecord:
        assert subagent_id == self.subagent.subagent_id
        return self.subagent


class _Coordinator:
    mode = "constrained"

    def __init__(self) -> None:
        self.outcomes: list[dict[str, Any]] = []

    def assign(
        self,
        config: AgentConfig,
        task: TaskNodeRecord,
        *,
        subagent_id: str | None,
        attempt: int,
    ) -> Any:
        record = SimpleNamespace(
            decision_id="route-scheduler",
            task_id=task.task_id,
            subagent_id=subagent_id,
            attempt=attempt,
            mode=self.mode,
            policy_id="balanced",
            selected_target_id="local-worker",
            selected_provider="openai-compatible",
            selected_model="qwen-coder",
            selection_kind="deterministic_router",
            score=0.9,
            reason_codes=("highest_admissible_score",),
            actionable=True,
        )
        return SimpleNamespace(
            assignment=SimpleNamespace(
                config=replace(
                    config,
                    provider="openai-compatible",
                    model="qwen-coder",
                ),
                executes_selected_target=True,
            ),
            record=record,
            reused=False,
        )

    def mark_started(self, _durable: Any) -> None:
        return

    def record_outcome(self, _durable: Any, **kwargs: Any) -> Any:
        self.outcomes.append(dict(kwargs))
        return SimpleNamespace(to_payload=lambda: dict(kwargs))


def _manager(
    state: _State,
    events: _Events,
    coordinator: _Coordinator,
) -> AdaptiveFlockRunManager:
    manager = object.__new__(AdaptiveFlockRunManager)
    manager.state = state  # type: ignore[assignment]
    manager.events = events  # type: ignore[assignment]
    manager.routing_coordinator = coordinator  # type: ignore[assignment]
    manager._scheduler_routing_lock = Lock()
    manager._scheduler_routing_attempts = {}
    return manager


def test_base_scheduler_executor_calls_provider_config_hook() -> None:
    source = getsource(RunManager._execute_ready_task)
    assert "self._prepare_scheduler_task_config(" in source


def test_scheduler_route_is_applied_and_terminal_outcome_is_recorded(
    monkeypatch: Any,
) -> None:
    state = _State()
    events = _Events()
    coordinator = _Coordinator()
    manager = _manager(state, events, coordinator)
    run = RunRecord(
        run_id=state.task.run_id,
        status="running",
        message="inspect repository",
        session_id="session-scheduler-route",
        workspace=".",
        provider="mock",
        model="mock",
    )
    routed = manager._prepare_scheduler_task_config(
        AgentConfig(provider="mock", model="baseline"),
        run=run,
        task=state.task,
        subagent=state.subagent,
    )
    assert routed.provider == "openai-compatible"
    assert routed.model == "qwen-coder"

    state.task = replace(
        state.task,
        status="completed",
        result={
            "acceptance_validation": {
                "passed": True,
                "failure_codes": [],
            }
        },
    )
    state.subagent = replace(state.subagent, status="completed")

    def parent_execute(
        _self: RunManager,
        _run: RunRecord,
        _task: TaskNodeRecord,
    ) -> dict[str, Any]:
        return {"status": "completed"}

    monkeypatch.setattr(RunManager, "_execute_ready_task", parent_execute)
    result = manager._execute_ready_task(run, state.task)

    assert result == {"status": "completed"}
    assert coordinator.outcomes
    assert coordinator.outcomes[0]["validation_passed"] is True
    event_types = {event_type for _, event_type, _ in events.items}
    assert {"routing.selected", "routing.attempt_started", "routing.outcome_recorded"} <= event_types
