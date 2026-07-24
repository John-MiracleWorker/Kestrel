from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.router import RoutingUnavailableError
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
            run_id="run-route",
            status="running",
            message="route a worker",
            session_id="session-route",
            workspace=".",
            provider="mock",
            model="mock",
        )
        self.task = TaskNodeRecord(
            task_id="task-route",
            run_id="run-route",
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
            subagent_id="subagent-route",
            run_id="run-route",
            task_id="task-route",
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
        attempt_count = self.task.attempt_count + (1 if kwargs.get("increment_attempt") else 0)
        self.task = replace(
            self.task,
            status=status,
            attempt_count=attempt_count,
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
    def __init__(self, *, mode: str = "constrained", unavailable: bool = False) -> None:
        self.mode = mode
        self.unavailable = unavailable
        self.started: list[str] = []
        self.outcomes: list[dict[str, Any]] = []

    def assign(
        self,
        config: AgentConfig,
        task: TaskNodeRecord,
        *,
        subagent_id: str | None,
        attempt: int,
    ) -> Any:
        if self.unavailable:
            raise RoutingUnavailableError(
                "no eligible target",
                reason_codes=("no_eligible_target",),
            )
        routed = replace(
            config,
            provider="openai-compatible",
            model="local-worker",
            base_url="http://127.0.0.1:1234/v1",
            api_key_env="secret://local-worker",
        )
        record = SimpleNamespace(
            decision_id="route-decision",
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
        assignment = SimpleNamespace(
            config=routed if record.actionable else config,
            executes_selected_target=record.actionable,
        )
        return SimpleNamespace(assignment=assignment, record=record, reused=False)

    def mark_started(self, durable: Any) -> Any:
        self.started.append(str(durable.record.decision_id))
        return durable.record

    def record_outcome(self, durable: Any, **kwargs: Any) -> Any:
        self.outcomes.append(dict(kwargs))
        return SimpleNamespace(
            to_payload=lambda: {
                "decision_id": durable.record.decision_id,
                **kwargs,
            }
        )


def _manager(state: _State, events: _Events, coordinator: _Coordinator) -> AdaptiveFlockRunManager:
    manager = object.__new__(AdaptiveFlockRunManager)
    manager.state = state  # type: ignore[assignment]
    manager.events = events  # type: ignore[assignment]
    manager.routing_coordinator = coordinator  # type: ignore[assignment]
    manager._lease_owner = "manager-test"
    manager._cancelled = set()
    manager._maybe_complete_root_task = lambda _run_id: None  # type: ignore[method-assign]
    return manager


def test_actionable_route_changes_worker_provider_and_records_validated_outcome(
    monkeypatch: Any,
) -> None:
    state = _State()
    events = _Events()
    coordinator = _Coordinator()
    manager = _manager(state, events, coordinator)
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
        state.task = replace(
            state.task,
            status="completed",
            result={
                "acceptance_validation": {
                    "passed": True,
                    "criteria": [
                        {
                            "evidence_refs": ["tool:test-run"],
                        }
                    ],
                },
                "tool_count": 2,
            },
        )
        state.subagent = replace(state.subagent, status="completed")

    monkeypatch.setattr(RunManager, "_run_subagent", parent_run)
    monkeypatch.setattr(AdaptiveFlockRunManager, "_is_cancelled", lambda _self, _run_id: False)

    base = AgentConfig(provider="mock", model="orchestrator", allow_file_write=False)
    manager._run_subagent("thread", base, "subagent-route", "run-route", "session-route")

    assert observed[0].provider == "openai-compatible"
    assert observed[0].model == "local-worker"
    assert observed[0].allow_file_write is False
    assert coordinator.started == ["route-decision"]
    assert coordinator.outcomes[0]["validation_passed"] is True
    assert coordinator.outcomes[0]["tool_count"] == 2
    assert coordinator.outcomes[0]["evidence_refs"] == ("tool:test-run",)
    assert "routing.selected" in {event_type for _, event_type, _ in events.items}
    assert "routing.outcome_recorded" in {event_type for _, event_type, _ in events.items}


def test_shadow_unavailable_router_preserves_baseline_execution(monkeypatch: Any) -> None:
    state = _State()
    events = _Events()
    coordinator = _Coordinator(mode="shadow", unavailable=True)
    manager = _manager(state, events, coordinator)
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
    base = AgentConfig(provider="mock", model="baseline")

    manager._run_subagent("thread", base, "subagent-route", "run-route", "session-route")

    assert observed == [base]
    assert "routing.shadow_unavailable" in {
        event_type for _, event_type, _ in events.items
    }
    assert state.task.status == "running"


def test_constrained_unavailable_router_fails_before_provider_execution(monkeypatch: Any) -> None:
    state = _State()
    events = _Events()
    coordinator = _Coordinator(mode="constrained", unavailable=True)
    manager = _manager(state, events, coordinator)
    called = False

    def parent_run(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(RunManager, "_run_subagent", parent_run)
    monkeypatch.setattr(AdaptiveFlockRunManager, "_is_cancelled", lambda _self, _run_id: False)

    manager._run_subagent(
        "thread",
        AgentConfig(provider="mock", model="baseline"),
        "subagent-route",
        "run-route",
        "session-route",
    )

    assert called is False
    assert state.task.status == "failed"
    assert state.subagent.status == "failed"
    assert state.task.diagnosis == {
        "category": "routing_unavailable",
        "reason_codes": ["no_eligible_target"],
    }
    assert "routing.guardrail_blocked" in {
        event_type for _, event_type, _ in events.items
    }


def test_approval_block_does_not_record_terminal_route_outcome(monkeypatch: Any) -> None:
    state = _State()
    events = _Events()
    coordinator = _Coordinator()
    manager = _manager(state, events, coordinator)

    def parent_run(*_args: Any, **_kwargs: Any) -> None:
        state.task = replace(
            state.task,
            status="blocked",
            result={
                "acceptance_validation": {"passed": False},
                "stop_reason": "approval_required",
            },
        )
        state.subagent = replace(state.subagent, status="blocked")

    monkeypatch.setattr(RunManager, "_run_subagent", parent_run)
    monkeypatch.setattr(AdaptiveFlockRunManager, "_is_cancelled", lambda _self, _run_id: False)

    manager._run_subagent(
        "thread",
        AgentConfig(provider="mock", model="baseline"),
        "subagent-route",
        "run-route",
        "session-route",
    )

    assert coordinator.started == ["route-decision"]
    assert coordinator.outcomes == []
    assert "routing.outcome_recorded" not in {
        event_type for _, event_type, _ in events.items
    }
