from __future__ import annotations

from pathlib import Path
from textwrap import dedent, indent


def replace_once(text: str, marker: str, replacement: str, *, name: str) -> str:
    count = text.count(marker)
    if count != 1:
        raise SystemExit(f"{name} marker count was {count}")
    return text.replace(marker, replacement, 1)


def main() -> None:
    parent = Path("src/nested_memvid_agent/run_manager.py")
    text = parent.read_text(encoding="utf-8")
    method_marker = (
        "    def _execute_ready_task(self, run: RunRecord, task: TaskNodeRecord) -> dict[str, Any]:\n"
    )
    base_hook = indent(
        dedent(
            '''\
            def _prepare_scheduler_task_config(
                self,
                config: AgentConfig,
                *,
                run: RunRecord,
                task: TaskNodeRecord,
                subagent: SubagentRunRecord,
            ) -> AgentConfig:
                """Return the provider configuration for one claimed scheduler task.

                The default implementation is deliberately neutral. Specialized
                runtimes may choose a provider here, after the task claim and
                subagent identity exist but before worker isolation and model
                construction.
                """

                del run, task, subagent
                return config

            def _execute_ready_task(self, run: RunRecord, task: TaskNodeRecord) -> dict[str, Any]:
            '''
        ),
        "    ",
    )
    text = replace_once(text, method_marker, base_hook, name="scheduler method")

    config_marker = (
        "                config, worker_isolation = self._worker_config(\n"
        "                    config,\n"
        "                    run_id=run.run_id,\n"
    )
    config_replacement = (
        "                config = self._prepare_scheduler_task_config(\n"
        "                    config,\n"
        "                    run=run,\n"
        "                    task=task,\n"
        "                    subagent=subagent,\n"
        "                )\n"
        + config_marker
    )
    text = replace_once(text, config_marker, config_replacement, name="scheduler config")
    parent.write_text(text, encoding="utf-8")

    routed = Path("src/nested_memvid_agent/routing/run_manager.py")
    routed_text = routed.read_text(encoding="utf-8")
    routed_text = replace_once(
        routed_text,
        "from dataclasses import asdict\nfrom time import monotonic\n",
        "from dataclasses import asdict\nfrom threading import Lock\nfrom time import monotonic\n",
        name="routing imports",
    )
    routed_text = replace_once(
        routed_text,
        "from ..state_store import AgentStateStore, TaskNodeRecord\n",
        "from ..state_store import (\n"
        "    AgentStateStore,\n"
        "    RunRecord,\n"
        "    SubagentRunRecord,\n"
        "    TaskNodeRecord,\n"
        ")\n",
        name="routing state imports",
    )
    routed_text = replace_once(
        routed_text,
        "        self.routing_coordinator = routing_coordinator\n        super().__init__(\n",
        "        self.routing_coordinator = routing_coordinator\n"
        "        self._scheduler_routing_lock = Lock()\n"
        "        self._scheduler_routing_attempts: dict[\n"
        "            tuple[str, str], tuple[DurableRoutingAssignment, float]\n"
        "        ] = {}\n"
        "        super().__init__(\n",
        name="routing initializer",
    )

    scheduler_methods = indent(
        dedent(
            '''\
            def _prepare_scheduler_task_config(
                self,
                config: AgentConfig,
                *,
                run: RunRecord,
                task: TaskNodeRecord,
                subagent: SubagentRunRecord,
            ) -> AgentConfig:
                attempt = max(1, task.attempt_count + 1)
                durable: DurableRoutingAssignment | None = None
                try:
                    durable = self.routing_coordinator.assign(
                        config,
                        task,
                        subagent_id=subagent.subagent_id,
                        attempt=attempt,
                    )
                    self.events.publish(
                        run.run_id,
                        "routing.selected",
                        _routing_decision_payload(durable),
                    )
                    self.routing_coordinator.mark_started(durable)
                except Exception as exc:  # noqa: BLE001 - mode selects fail-open or fail-closed
                    return self._handle_scheduler_routing_failure(
                        exc,
                        config=config,
                        run=run,
                        task=task,
                        subagent=subagent,
                        attempt=attempt,
                        durable=durable,
                    )
                self.events.publish(
                    run.run_id,
                    "routing.attempt_started",
                    {
                        "decision_id": durable.record.decision_id,
                        "task_id": task.task_id,
                        "subagent_id": subagent.subagent_id,
                        "attempt": attempt,
                        "selected_target_id": durable.record.selected_target_id,
                        "selected_provider": durable.record.selected_provider,
                        "selected_model": durable.record.selected_model,
                        "actionable": durable.record.actionable,
                        "scheduler": True,
                    },
                )
                with self._scheduler_routing_lock:
                    self._scheduler_routing_attempts[(run.run_id, task.task_id)] = (
                        durable,
                        monotonic(),
                    )
                return durable.assignment.config

            def _execute_ready_task(self, run: RunRecord, task: TaskNodeRecord) -> dict[str, Any]:
                try:
                    return super()._execute_ready_task(run, task)
                finally:
                    with self._scheduler_routing_lock:
                        context = self._scheduler_routing_attempts.pop(
                            (run.run_id, task.task_id),
                            None,
                        )
                    if context is not None:
                        durable, started_at = context
                        subagent_id = durable.record.subagent_id
                        if subagent_id is not None:
                            self._record_terminal_routing_outcome(
                                durable,
                                started_at=started_at,
                                task_id=task.task_id,
                                subagent_id=subagent_id,
                                run_id=run.run_id,
                            )

            def _handle_scheduler_routing_failure(
                self,
                exc: Exception,
                *,
                config: AgentConfig,
                run: RunRecord,
                task: TaskNodeRecord,
                subagent: SubagentRunRecord,
                attempt: int,
                durable: DurableRoutingAssignment | None,
            ) -> AgentConfig:
                phase = "assignment" if durable is None else "start"
                if isinstance(exc, RoutingUnavailableError):
                    unavailable = True
                    reason_codes = tuple(exc.reason_codes)
                else:
                    unavailable = False
                    reason_codes = (f"routing_{phase}_failed",)
                category = "routing_unavailable" if unavailable else "routing_persistence_failed"
                error = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
                payload: dict[str, Any] = {
                    "task_id": task.task_id,
                    "subagent_id": subagent.subagent_id,
                    "attempt": attempt,
                    "phase": phase,
                    "mode": self.routing_coordinator.mode,
                    "reason_codes": list(reason_codes),
                    "error": error,
                    "scheduler": True,
                }
                if durable is not None:
                    payload["decision_id"] = durable.record.decision_id
                    try:
                        outcome = self.routing_coordinator.record_outcome(
                            durable,
                            execution_status=f"routing_{phase}_failed",
                            validation_passed=False,
                            validation_codes=reason_codes,
                            failure_category=category,
                            reward_components={"completion": -1.0},
                            outcome_labels=(f"routing_{phase}_failed",),
                        )
                        self.events.publish(
                            run.run_id,
                            "routing.outcome_recorded",
                            outcome.to_payload(),
                        )
                    except Exception as outcome_exc:  # noqa: BLE001 - retain root failure
                        self.events.publish(
                            run.run_id,
                            "routing.outcome_failed",
                            {
                                "decision_id": durable.record.decision_id,
                                "task_id": task.task_id,
                                "subagent_id": subagent.subagent_id,
                                "error": str(
                                    redact_secrets(
                                        f"{type(outcome_exc).__name__}: {outcome_exc}"
                                    )
                                ),
                            },
                        )
                if self.routing_coordinator.mode == "shadow":
                    event_type = (
                        "routing.shadow_unavailable"
                        if unavailable and phase == "assignment"
                        else f"routing.{phase}_failed"
                    )
                    self.events.publish(run.run_id, event_type, payload)
                    return config
                event_type = (
                    "routing.guardrail_blocked"
                    if unavailable
                    else f"routing.{phase}_failed"
                )
                self.events.publish(run.run_id, event_type, payload)
                raise RuntimeError(
                    f"{category}:{','.join(reason_codes)}:{error}"
                ) from exc

            '''
        ),
        "    ",
    )
    routed_text = replace_once(
        routed_text,
        "    def _run_subagent(\n",
        scheduler_methods + "    def _run_subagent(\n",
        name="routing subagent",
    )
    routed.write_text(routed_text, encoding="utf-8")

    Path("tests/test_adaptive_flock_scheduler_hook.py").write_text(
        dedent(
            '''\
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
                assert "routing.attempt_started" in {
                    event_type for _, event_type, _ in events.items
                }
            '''
        ),
        encoding="utf-8",
    )

    Path(".github/workflows/adaptive-flock-scheduler-hook.yml").unlink()
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
