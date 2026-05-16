from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

from .agent import NestedMV2Agent
from .app_factory import build_agent
from .config import AgentConfig
from .diagnosis import classify_failure
from .event_bus import RunEventBus
from .mcp_manager import MCPManager
from .models import MemoryLayer
from .nested_learning import NestedLearningKernel
from .runtime_models import AgentTurnResult, LLMStreamEvent, ToolCall, ToolExecution, ToolSpec
from .skill_manager import SkillManager
from .state_store import AgentStateStore, RunRecord, TaskNodeRecord
from .task_capsule import summarize_run_capsule, write_turn_capsule
from .tools.base import ToolContext
from .tools.builtin import build_default_tools
from .tools.registry import ToolRegistry


class RunManager:
    """Background run orchestration for the web UI and API."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        state: AgentStateStore,
        events: RunEventBus,
        mcp: MCPManager,
        skills: SkillManager,
    ) -> None:
        self.config = config
        self.state = state
        self.events = events
        self.mcp = mcp
        self.skills = skills
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}
        self._cancelled: set[str] = set()

    def create_run(
        self,
        *,
        message: str,
        session_id: str | None = None,
        workspace: Path | None = None,
        model: str | None = None,
    ) -> RunRecord:
        run_id = f"run_{uuid4().hex}"
        run_config = replace(
            self.config,
            workspace=(workspace or self.config.workspace),
            model=model or self.config.model,
        )
        run = self.state.create_run(
            run_id=run_id,
            message=message,
            session_id=session_id or run_id,
            workspace=str(run_config.workspace),
            model=run_config.model,
        )
        root = self.state.create_task_node(
            task_id=f"task_{uuid4().hex}",
            run_id=run_id,
            title="Root objective",
            goal=message,
            profile="planner",
            status="queued",
            approved=True,
            plan={"autonomy_mode": "background", "decomposition": "initial"},
            acceptance_criteria=["User objective is addressed or explicitly blocked with next steps."],
        )
        for planned in _initial_task_plan(message):
            dependencies = [root.task_id if dependency == "root" else dependency for dependency in planned["dependencies"]]
            self.state.create_task_node(
                task_id=str(planned["task_id"]),
                run_id=run_id,
                parent_id=root.task_id,
                title=str(planned["title"]),
                goal=str(planned["goal"]),
                profile=str(planned["profile"]),
                status="queued",
                approved=planned["risk"] == "low",
                dependencies=dependencies,
                required_tools=planned["required_tools"],
                risk=str(planned["risk"]),
                acceptance_criteria=planned["acceptance_criteria"],
            )
        self.events.publish(run_id, "run.queued", {"message": message, "session_id": run.session_id})
        self._start_thread(run_id, self._run_agent_turn, run_config, message, run.session_id)
        return run

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.state.get_run(run_id)
        approvals = [approval for approval in self.state.list_approvals() if approval["run_id"] == run_id]
        return {**asdict(run), "approvals": approvals}

    def list_runs(self) -> list[dict[str, Any]]:
        return [asdict(run) for run in self.state.list_runs()]

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.state.list_sessions()

    def run_trace(self, run_id: str, *, limit: int = 1000) -> dict[str, Any]:
        run = self.get_run(run_id)
        timeline = self.state.list_run_steps(run_id, limit=limit)
        traces: dict[str, list[dict[str, Any]]] = {
            "tool": [],
            "memory": [],
            "context": [],
            "provider": [],
            "approval": [],
            "error": [],
            "lifecycle": [],
        }
        for event in timeline:
            traces[_trace_category(event)].append(event)
        first = timeline[0]["created_at"] if timeline else None
        last = timeline[-1]["created_at"] if timeline else None
        return {
            "run": run,
            "summary": {
                "event_count": len(timeline),
                "first_event_at": first,
                "last_event_at": last,
                "trace_counts": {name: len(events) for name, events in traces.items()},
            },
            "timeline": timeline,
            "traces": traces,
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._cancelled.add(run_id)
        run = self.state.transition_run(run_id, "cancelled", stop_reason="cancelled")
        self.events.publish(run_id, "run.cancelled", {})
        return asdict(run)

    def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        approval = self.state.get_approval(approval_id)
        status = "approved" if approved else "denied"
        approved_arguments = arguments or dict(approval["arguments"])
        decision = {"approved": approved, "arguments": approved_arguments}
        updated = self.state.decide_approval(approval_id, status=status, decision=decision)
        self.events.publish(updated["run_id"], f"approval.{status}", updated)
        if approved:
            self._resume_after_approval(updated, approved_arguments)
        else:
            self.state.transition_run(updated["run_id"], "failed", error="Approval denied", stop_reason="approval_denied")
            self.events.publish(updated["run_id"], "run.failed", {"error": "Approval denied"})
        return updated

    def invoke_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str = "manual",
        run_id: str | None = None,
    ) -> ToolExecution:
        registry = self.build_registry()
        agent = build_agent(self.config, tools=registry)
        try:
            call = ToolCall(name=tool_name, arguments=arguments)
            if run_id:
                self.events.publish(run_id, "tool.started", {"tool": tool_name, "tool_call_id": call.id})
            execution = registry.execute(
                call,
                ToolContext(
                    memory=agent.memory,
                    config=agent.config,
                    workspace=agent.config.workspace,
                    event_log=agent.event_log,
                    session_id=session_id,
                    run_id=run_id,
                    approval_handler=self._approval_handler if run_id else None,
                ),
            )
            if run_id:
                self.events.publish(run_id, "tool.executed", _execution_payload(execution))
                self.events.publish(
                    run_id,
                    "tool.completed" if execution.success else "tool.failed",
                    _execution_payload(execution),
                )
            return execution
        finally:
            agent.close()

    def task_graph(self, run_id: str) -> dict[str, Any]:
        self.state.get_run(run_id)
        return {
            "tasks": [_task_payload(task) for task in self.state.list_task_nodes(run_id)],
            "ready_tasks": self.ready_tasks(run_id),
            "approval_blocked_tasks": self.approval_blocked_tasks(run_id),
            "subagents": [asdict(subagent) for subagent in self.state.list_subagent_runs(run_id)],
        }

    def ready_tasks(self, run_id: str) -> list[dict[str, Any]]:
        self.state.get_run(run_id)
        tasks = self.state.list_task_nodes(run_id)
        by_id = {task.task_id: task for task in tasks}
        ready: list[dict[str, Any]] = []
        for task in tasks:
            reason = _task_scheduler_reason(task, by_id)
            if reason is None:
                continue
            payload = _task_payload(task)
            payload["scheduler_reason"] = reason
            ready.append(payload)
        return ready

    def approval_blocked_tasks(self, run_id: str) -> list[dict[str, Any]]:
        self.state.get_run(run_id)
        tasks = self.state.list_task_nodes(run_id)
        by_id = {task.task_id: task for task in tasks}
        blocked: list[dict[str, Any]] = []
        for task in tasks:
            if task.approved or task.status not in {"queued", "approved"}:
                continue
            if not _dependencies_completed(task, by_id):
                continue
            payload = _task_payload(task)
            payload["scheduler_reason"] = "task_approval_required"
            blocked.append(payload)
        return blocked

    def run_scheduler_step(self, run_id: str, *, max_tasks: int | None = None) -> dict[str, Any]:
        """Execute currently ready approved task nodes through normal agent gates."""
        run = self.state.get_run(run_id)
        limit = max(1, max_tasks or self.config.max_scheduler_tasks)
        executed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        while len(executed) < limit:
            executable: TaskNodeRecord | None = None
            for task_payload in self.ready_tasks(run_id):
                task = self.state.get_task_node(str(task_payload["task_id"]))
                if _is_root_objective_task(task):
                    if not any(item["task_id"] == task.task_id for item in skipped):
                        skipped.append({"task_id": task.task_id, "reason": "root_objective_tracking_node"})
                    continue
                executable = task
                break
            if executable is None:
                break
            result = self._execute_ready_task(run, executable)
            executed.append(result)
            if result["status"] == "blocked":
                blocked.append(result)
                break

        self._maybe_complete_root_task(run_id)
        payload = {
            "run_id": run_id,
            "executed": executed,
            "blocked": blocked,
            "skipped": skipped,
            "remaining_ready_tasks": self.ready_tasks(run_id),
            "approval_blocked_tasks": self.approval_blocked_tasks(run_id),
        }
        self.events.publish(run_id, "scheduler.step", payload)
        return payload

    def run_scheduler_until_idle(
        self,
        run_id: str,
        *,
        max_tasks: int | None = None,
        max_cycles: int | None = None,
    ) -> dict[str, Any]:
        """Drain scheduler-selected tasks until idle, blocked, failed, or bounded."""
        self.state.get_run(run_id)
        cycle_limit = max(1, max_cycles or self.config.max_scheduler_cycles)
        task_limit = max(1, max_tasks or self.config.max_scheduler_tasks)
        steps: list[dict[str, Any]] = []
        stop_reason = "idle"

        for _ in range(cycle_limit):
            if self.approval_blocked_tasks(run_id) and not self._executable_ready_tasks(run_id):
                stop_reason = "task_approval_required"
                break
            if not self._executable_ready_tasks(run_id):
                stop_reason = "idle"
                break

            step = self.run_scheduler_step(run_id, max_tasks=task_limit)
            steps.append(step)
            executed_statuses = {str(item.get("status")) for item in step["executed"]}
            if "failed" in executed_statuses:
                stop_reason = "task_failed"
                break
            if step["blocked"]:
                stop_reason = "tool_approval_required"
                break
            if step["approval_blocked_tasks"] and not self._executable_ready_tasks(run_id):
                stop_reason = "task_approval_required"
                break
            if not step["executed"]:
                stop_reason = "idle"
                break
        else:
            stop_reason = "cycle_limit_reached"

        payload = {
            "run_id": run_id,
            "cycles": len(steps),
            "max_cycles": cycle_limit,
            "max_tasks_per_cycle": task_limit,
            "stop_reason": stop_reason,
            "steps": steps,
            "executed": [item for step in steps for item in step["executed"]],
            "blocked": [item for step in steps for item in step["blocked"]],
            "remaining_ready_tasks": self.ready_tasks(run_id),
            "approval_blocked_tasks": self.approval_blocked_tasks(run_id),
        }
        self.events.publish(run_id, "scheduler.run", payload)
        return payload

    def _executable_ready_tasks(self, run_id: str) -> list[dict[str, Any]]:
        executable: list[dict[str, Any]] = []
        for task_payload in self.ready_tasks(run_id):
            task = self.state.get_task_node(str(task_payload["task_id"]))
            if not _is_root_objective_task(task):
                executable.append(task_payload)
        return executable

    def approve_task(self, run_id: str, task_id: str) -> dict[str, Any]:
        self.state.get_run(run_id)
        task = self.state.update_task_node(task_id, approved=True, status="approved")
        self.events.publish(run_id, "task.approved", asdict(task))
        payload = asdict(task)
        if self.config.enable_autonomous_scheduler:
            self.state.transition_run(run_id, "running", stop_reason="task_approved")
            scheduler = self.run_scheduler_until_idle(run_id)
            final_status, stop_reason = _scheduler_run_outcome(scheduler)
            self.state.transition_run(run_id, final_status, stop_reason=stop_reason)
            self.events.publish(run_id, f"run.{final_status}", {"scheduler": scheduler})
            payload["scheduler"] = scheduler
        return payload

    def create_subagent(self, *, run_id: str, profile: str, goal: str, task_id: str | None = None) -> dict[str, Any]:
        run = self.state.get_run(run_id)
        profile = profile if profile in {"planner", "worker", "reviewer"} else "worker"
        if task_id is None:
            task = self.state.create_task_node(
                task_id=f"task_{uuid4().hex}",
                run_id=run_id,
                title=f"{profile.title()} subtask",
                goal=goal,
                profile=profile,
                status="queued",
                approved=True,
            )
            task_id = task.task_id
        subagent = self.state.create_subagent_run(
            subagent_id=f"subagent_{uuid4().hex}",
            run_id=run_id,
            task_id=task_id,
            profile=profile,
            goal=goal,
            status="queued",
        )
        config = replace(self.config, workspace=Path(run.workspace), model=run.model)
        self.events.publish(run_id, "subagent.queued", asdict(subagent))
        self._start_thread(subagent.subagent_id, self._run_subagent, config, subagent.subagent_id, run_id, run.session_id)
        return asdict(subagent)

    def _run_agent_turn(self, run_id: str, config: AgentConfig, message: str, session_id: str) -> None:
        if self._is_cancelled(run_id):
            return
        self.state.transition_run(run_id, "running")
        self.events.publish(run_id, "run.started", {"session_id": session_id})
        agent = self._build_agent(config)
        try:
            result = agent.chat(
                message,
                session_id=session_id,
                run_id=run_id,
                approval_handler=self._approval_handler,
                stream_handler=self._stream_handler(run_id),
            )
            if self._is_cancelled(run_id):
                return
            self._publish_turn_observability(run_id, result)
            for execution in result.tool_executions:
                self.events.publish(run_id, "tool.executed", _execution_payload(execution))
                self.events.publish(
                    run_id,
                    "tool.completed" if execution.success else "tool.failed",
                    _execution_payload(execution),
                )
            self._finish_agent_turn(run_id, config, agent, result)
        except Exception as exc:  # noqa: BLE001
            if self._is_cancelled(run_id):
                return
            self.state.transition_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}", stop_reason="error")
            self.events.publish(run_id, "run.failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            agent.close()

    def _resume_after_approval(self, approval: dict[str, Any], arguments: dict[str, Any]) -> None:
        run_id = str(approval["run_id"])
        if self._is_cancelled(run_id):
            return
        run = self.state.get_run(run_id)
        config = replace(self.config, workspace=Path(run.workspace), model=run.model)
        self.state.transition_run(run_id, "running", stop_reason="resuming_after_approval")
        self._start_thread(run_id, self._run_approved_tool_then_continue, config, approval, arguments, run.session_id)

    def _run_approved_tool_then_continue(
        self,
        run_id: str,
        config: AgentConfig,
        approval: dict[str, Any],
        arguments: dict[str, Any],
        session_id: str,
    ) -> None:
        agent = self._build_agent(config)
        try:
            if self._is_cancelled(run_id):
                return
            call = ToolCall(name=str(approval["tool_name"]), arguments=arguments, id=str(approval["tool_call_id"]))
            execution = agent.tools.execute(
                call,
                ToolContext(
                    memory=agent.memory,
                    config=agent.config,
                    workspace=agent.config.workspace,
                    event_log=agent.event_log,
                    session_id=session_id,
                    run_id=run_id,
                    approved_tool_call_ids=frozenset({call.id}),
                    approved_tool_call_arguments={call.id: arguments},
                ),
            )
            self.state.record_approval_result(str(approval["approval_id"]), _execution_payload(execution))
            self.events.publish(run_id, "tool.executed", _execution_payload(execution))
            self.events.publish(run_id, "tool.completed" if execution.success else "tool.failed", _execution_payload(execution))
            continuation = (
                f"Continue the previous run after approved tool `{call.name}`.\n\n"
                f"Tool success: {execution.success}\n"
                f"Tool result:\n{execution.content[:4000]}"
            )
            result = agent.chat(
                continuation,
                session_id=session_id,
                run_id=run_id,
                approval_handler=self._approval_handler,
                stream_handler=self._stream_handler(run_id),
            )
            if self._is_cancelled(run_id):
                return
            self._publish_turn_observability(run_id, result)
            self._finish_agent_turn(run_id, config, agent, result, tool_count_offset=1)
        except Exception as exc:  # noqa: BLE001
            if self._is_cancelled(run_id):
                return
            self.state.transition_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}", stop_reason="error")
            self.events.publish(run_id, "run.failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            agent.close()

    def _finish_agent_turn(
        self,
        run_id: str,
        config: AgentConfig,
        agent: NestedMV2Agent,
        result: AgentTurnResult,
        *,
        tool_count_offset: int = 0,
    ) -> None:
        status = "blocked" if result.stop_reason == "approval_required" else "completed"
        run_status = "running" if status == "completed" and config.enable_autonomous_scheduler else status
        stop_reason = "scheduler_running" if run_status == "running" and status == "completed" else result.stop_reason
        self.state.transition_run(
            run_id,
            run_status,
            assistant_message=result.assistant_message,
            context_chars=result.context_chars,
            tool_count=len(result.tool_executions) + tool_count_offset,
            stop_reason=stop_reason,
        )
        if status == "completed":
            self._complete_capsule(run_id, config, agent, result)
        event_type = "run.blocked" if status == "blocked" else "run.turn_completed" if config.enable_autonomous_scheduler else "run.completed"
        self.events.publish(run_id, event_type, _turn_payload(result))
        if status == "completed" and config.enable_autonomous_scheduler:
            scheduler = self.run_scheduler_until_idle(
                run_id,
                max_tasks=config.max_scheduler_tasks,
                max_cycles=config.max_scheduler_cycles,
            )
            final_status, scheduler_stop_reason = _scheduler_run_outcome(scheduler)
            self.state.transition_run(run_id, final_status, stop_reason=scheduler_stop_reason)
            self.events.publish(run_id, f"run.{final_status}", {"scheduler": scheduler, "turn": _turn_payload(result)})

    def _run_subagent(
        self,
        thread_key: str,
        config: AgentConfig,
        subagent_id: str,
        run_id: str,
        session_id: str,
    ) -> None:
        del thread_key
        subagent = self.state.get_subagent_run(subagent_id)
        running = self.state.update_subagent_run(subagent_id, status="running")
        if subagent.task_id:
            self.state.update_task_node(subagent.task_id, status="running")
        self.events.publish(run_id, "subagent.started", asdict(running))
        agent = self._build_agent(config)
        try:
            prompt = _subagent_prompt(subagent.profile, subagent.goal)
            result = agent.chat(
                prompt,
                session_id=session_id,
                run_id=run_id,
                approval_handler=self._approval_handler,
                stream_handler=self._stream_handler(run_id),
            )
            self._publish_turn_observability(run_id, result)
            updated = self.state.update_subagent_run(subagent_id, status="completed", result=result.assistant_message)
            if subagent.task_id:
                self.state.update_task_node(
                    subagent.task_id,
                    status="completed",
                    result={"assistant_message": result.assistant_message, "stop_reason": result.stop_reason},
                )
            self.events.publish(run_id, "subagent.completed", asdict(updated))
        except Exception as exc:  # noqa: BLE001
            error_text = f"{type(exc).__name__}: {exc}"
            updated = self.state.update_subagent_run(subagent_id, status="failed", error=error_text)
            if subagent.task_id:
                diagnosis = classify_failure(error_text, source="subagent")
                diagnosis_payload = diagnosis.to_payload()
                failed_task = self.state.record_task_failure(
                    subagent.task_id,
                    failure_reason=error_text,
                    diagnosis=diagnosis_payload,
                    retry_strategy={
                        "requires_changed_strategy": True,
                        "retry_allowed": False,
                        "reason": "subagent failure must be diagnosed and strategy must change before retry",
                    },
                    result={"error": updated.error},
                )
                self.events.publish(run_id, "task.failed", _task_payload(failed_task))
                self.events.publish(
                    run_id,
                    "diagnosis.classified",
                    {"task_id": subagent.task_id, "source": "subagent", **diagnosis_payload},
                )
            self.events.publish(run_id, "subagent.failed", asdict(updated))
        finally:
            agent.close()

    def _execute_ready_task(self, run: RunRecord, task: TaskNodeRecord) -> dict[str, Any]:
        running = self.state.update_task_node(task.task_id, status="running")
        subagent = self.state.create_subagent_run(
            subagent_id=f"subagent_{uuid4().hex}",
            run_id=run.run_id,
            task_id=task.task_id,
            profile=task.profile,
            goal=task.goal,
            status="running",
        )
        self.events.publish(run.run_id, "task.started", _task_payload(running))
        self.events.publish(run.run_id, "subagent.started", asdict(subagent))
        config = replace(self.config, workspace=Path(run.workspace), model=run.model)
        agent = self._build_agent(config)
        try:
            result = agent.chat(
                _task_execution_prompt(task),
                session_id=run.session_id,
                run_id=run.run_id,
                approval_handler=self._approval_handler,
                stream_handler=self._stream_handler(run.run_id),
            )
            self._publish_turn_observability(run.run_id, result)
            status = "blocked" if result.stop_reason == "approval_required" else "completed"
            task_result = {
                "assistant_message": result.assistant_message,
                "stop_reason": result.stop_reason,
                "context_chars": result.context_chars,
                "tool_count": len(result.tool_executions),
                "memory_writes": list(result.memory_writes),
            }
            updated_task = self.state.update_task_node(task.task_id, status=status, result=task_result)
            updated_subagent = self.state.update_subagent_run(
                subagent.subagent_id,
                status=status,
                result=result.assistant_message,
            )
            for execution in result.tool_executions:
                self.events.publish(run.run_id, "tool.executed", _execution_payload(execution))
                self.events.publish(
                    run.run_id,
                    "tool.completed" if execution.success else "tool.failed",
                    _execution_payload(execution),
                )
            event_type = "task.blocked" if status == "blocked" else "task.completed"
            self.events.publish(run.run_id, event_type, _task_payload(updated_task))
            self.events.publish(
                run.run_id,
                "subagent.blocked" if status == "blocked" else "subagent.completed",
                asdict(updated_subagent),
            )
            return {"task_id": task.task_id, "subagent_id": subagent.subagent_id, "status": status, "result": task_result}
        except Exception as exc:  # noqa: BLE001
            error_text = f"{type(exc).__name__}: {exc}"
            diagnosis = classify_failure(error_text, source="scheduler").to_payload()
            failed_task = self.state.record_task_failure(
                task.task_id,
                failure_reason=error_text,
                diagnosis=diagnosis,
                retry_strategy={
                    "requires_changed_strategy": True,
                    "retry_allowed": False,
                    "reason": "scheduler task failed; inspect diagnosis before retry",
                },
                result={"error": error_text},
            )
            failed_subagent = self.state.update_subagent_run(subagent.subagent_id, status="failed", error=error_text)
            self.events.publish(run.run_id, "task.failed", _task_payload(failed_task))
            self.events.publish(run.run_id, "subagent.failed", asdict(failed_subagent))
            self.events.publish(run.run_id, "diagnosis.classified", {"task_id": task.task_id, "source": "scheduler", **diagnosis})
            return {"task_id": task.task_id, "subagent_id": subagent.subagent_id, "status": "failed", "error": error_text}
        finally:
            agent.close()

    def _maybe_complete_root_task(self, run_id: str) -> None:
        tasks = self.state.list_task_nodes(run_id)
        roots = [task for task in tasks if _is_root_objective_task(task)]
        if not roots:
            return
        root = roots[0]
        children = [task for task in tasks if task.parent_id == root.task_id]
        if not children:
            return
        child_statuses = {task.status for task in children}
        if any(status == "failed" for status in child_statuses):
            updated = self.state.update_task_node(root.task_id, status="failed", result={"child_statuses": sorted(child_statuses)})
            self.events.publish(run_id, "task.failed", _task_payload(updated))
        elif any(status == "blocked" for status in child_statuses):
            updated = self.state.update_task_node(root.task_id, status="blocked", result={"child_statuses": sorted(child_statuses)})
            self.events.publish(run_id, "task.blocked", _task_payload(updated))
        elif all(status == "completed" for status in child_statuses):
            updated = self.state.update_task_node(root.task_id, status="completed", result={"child_statuses": sorted(child_statuses)})
            self.events.publish(run_id, "task.completed", _task_payload(updated))

    def _approval_handler(self, call: ToolCall, spec: ToolSpec, context: ToolContext) -> ToolExecution:
        run_id = context.run_id or f"manual_{uuid4().hex}"
        approval_id = f"approval_{uuid4().hex}"
        approval = self.state.create_approval(
            approval_id=approval_id,
            run_id=run_id,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments=call.arguments,
            risk=spec.risk,
        )
        self.events.publish(run_id, "approval.requested", approval)
        return ToolExecution(
            call=call,
            success=False,
            content=f"Approval required for {call.name}.",
            data={"approval_id": approval_id, "status": "pending"},
            error="approval_pending",
        )

    def _build_agent(self, config: AgentConfig) -> NestedMV2Agent:
        return build_agent(config, tools=self.build_registry())

    def _stream_handler(self, run_id: str) -> Callable[[LLMStreamEvent], None]:
        def handle(event: LLMStreamEvent) -> None:
            if event.type == "token":
                self.events.publish(run_id, "assistant.token", {"content": event.content})
            elif event.type == "tool_call" and event.tool_call is not None:
                self.events.publish(
                    run_id,
                    "assistant.tool_call",
                    {"tool": event.tool_call.name, "tool_call_id": event.tool_call.id, "arguments": event.tool_call.arguments},
                )
            elif event.type == "usage":
                self.events.publish(run_id, "assistant.usage", event.data)
            elif event.type == "provider_error":
                self.events.publish(run_id, "assistant.provider_error", {"content": event.content, **event.data})

        return handle

    def build_registry(self) -> ToolRegistry:
        registry = build_default_tools()
        self.skills.discover()
        for adapter in [*self.mcp.tool_adapters(), *self.skills.tool_adapters()]:
            registry.register(adapter)
        return registry

    def _complete_capsule(
        self,
        run_id: str,
        config: AgentConfig,
        agent: NestedMV2Agent,
        result: AgentTurnResult,
    ) -> None:
        if not config.enable_task_capsules:
            return
        runs_dir = config.memory_dir.parent / "runs"
        try:
            capsule_path = write_turn_capsule(
                runs_dir=runs_dir,
                run_id=run_id,
                result=result,
                backend=config.backend,
                selected_context=result.context_prompt,
            )
            summary = summarize_run_capsule(runs_dir=runs_dir, run_id=run_id, backend=config.backend)
            decisions = _capsule_decisions(
                summary,
                agent=agent,
                dry_run=config.auto_consolidation_dry_run or not config.enable_auto_consolidation,
            )
            self.events.publish(
                run_id,
                "capsule.completed",
                {
                    "capsule_path": str(capsule_path),
                    "summary": summary.to_payload(),
                    "auto_consolidation_enabled": config.enable_auto_consolidation,
                    "dry_run": config.auto_consolidation_dry_run or not config.enable_auto_consolidation,
                    "decisions": decisions,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self.events.publish(run_id, "capsule.failed", {"error": f"{type(exc).__name__}: {exc}"})

    def _publish_turn_observability(self, run_id: str, result: AgentTurnResult) -> None:
        self.events.publish(
            run_id,
            "context.compile",
            {
                "session_id": result.session_id,
                "context_chars": result.context_chars,
                "stop_reason": result.stop_reason,
            },
        )
        for index, record_id in enumerate(result.memory_writes, start=1):
            self.events.publish(
                run_id,
                "memory.write",
                {
                    "session_id": result.session_id,
                    "record_id": record_id,
                    "index": index,
                    "total": len(result.memory_writes),
                },
            )
        if result.error:
            self.events.publish(run_id, "runtime.error", result.error)

    def _start_thread(self, run_id: str, target: Any, *args: Any) -> None:
        thread = Thread(target=target, args=(run_id, *args), daemon=True)
        with self._lock:
            self._threads[run_id] = thread
        thread.start()

    def _is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled


def _execution_payload(execution: ToolExecution) -> dict[str, Any]:
    return {
        "tool": execution.call.name,
        "tool_call_id": execution.call.id,
        "arguments": execution.call.arguments,
        "success": execution.success,
        "content": execution.content,
        "data": execution.data,
        "error": execution.error,
    }


def _turn_payload(result: AgentTurnResult) -> dict[str, Any]:
    return {
        "session_id": result.session_id,
        "user_message": result.user_message,
        "assistant_message": result.assistant_message,
        "tool_executions": [_execution_payload(execution) for execution in result.tool_executions],
        "context_chars": result.context_chars,
        "memory_writes": list(result.memory_writes),
        "stop_reason": result.stop_reason,
    }


def _is_root_objective_task(task: TaskNodeRecord) -> bool:
    plan = task.plan or {}
    return task.parent_id is None and task.profile == "planner" and plan.get("autonomy_mode") == "background"


def _scheduler_run_outcome(scheduler: dict[str, Any]) -> tuple[str, str]:
    stop_reason = str(scheduler.get("stop_reason") or "idle")
    executed = scheduler.get("executed", [])
    statuses = {str(item.get("status")) for item in executed if isinstance(item, dict)}
    if "failed" in statuses or stop_reason == "task_failed":
        return "failed", stop_reason
    if stop_reason in {"tool_approval_required", "task_approval_required", "cycle_limit_reached"}:
        return "blocked", stop_reason
    return "completed", "scheduler_idle"


def _task_execution_prompt(task: TaskNodeRecord) -> str:
    dependencies = "\n".join(f"- {dependency}" for dependency in task.dependencies) or "- none"
    tools = "\n".join(f"- {tool}" for tool in task.required_tools) or "- none"
    criteria = "\n".join(f"- {criterion}" for criterion in task.acceptance_criteria) or "- Report concrete outcome and remaining risk."
    retry = task.retry_strategy or {}
    retry_note = ""
    if retry:
        retry_note = f"\nRetry strategy metadata:\n{retry}"
    return (
        f"Autonomous task profile: {task.profile}\n"
        f"Task title: {task.title}\n"
        f"Goal:\n{task.goal}\n\n"
        f"Dependencies:\n{dependencies}\n\n"
        f"Expected tools:\n{tools}\n\n"
        f"Acceptance criteria:\n{criteria}\n"
        f"{retry_note}\n\n"
        "Execute only the approved task scope. Use available tools when needed, respect high-risk approval gates, "
        "and finish with a concise result plus any blocker."
    )


def _task_payload(task: TaskNodeRecord) -> dict[str, Any]:
    payload = asdict(task)
    payload["dependencies"] = list(task.dependencies)
    payload["required_tools"] = list(task.required_tools)
    payload["acceptance_criteria"] = list(task.acceptance_criteria)
    return payload


def _task_scheduler_reason(task: TaskNodeRecord, by_id: dict[str, TaskNodeRecord]) -> str | None:
    if not task.approved:
        return None
    if task.status not in {"queued", "approved"}:
        return None
    if not _dependencies_completed(task, by_id):
        return None
    retry = task.retry_strategy or {}
    if retry.get("requires_changed_strategy"):
        if retry.get("retry_allowed") is not True or not str(retry.get("changed_strategy") or "").strip():
            return None
        return "retry_strategy_changed"
    if task.attempt_count > 0:
        return "retry_ready"
    return "dependencies_satisfied"


def _dependencies_completed(task: TaskNodeRecord, by_id: dict[str, TaskNodeRecord]) -> bool:
    return all(by_id.get(dependency) and by_id[dependency].status == "completed" for dependency in task.dependencies)


def _initial_task_plan(message: str) -> list[dict[str, Any]]:
    """Create a conservative persisted starter plan for new background runs.

    The live agent still does the real work. These deterministic nodes give the
    control plane a durable DAG skeleton for tracking, resume, and review instead
    of leaving every run as one opaque root task.
    """
    objective = message.strip() or "User objective"
    inspect_id = f"task_{uuid4().hex}"
    validate_id = f"task_{uuid4().hex}"
    if _looks_like_repair_commit_request(objective):
        prepare_id = f"task_{uuid4().hex}"
        patch_id = f"task_{uuid4().hex}"
        review_id = f"task_{uuid4().hex}"
        commit_id = f"task_{uuid4().hex}"
        return [
            {
                "task_id": inspect_id,
                "title": "Inspect repair context",
                "goal": f"Gather repository context and failure evidence for: {objective}",
                "profile": "worker",
                "dependencies": [],
                "required_tools": ["repo.search", "repo.map", "memory.search", "context.pack"],
                "risk": "low",
                "acceptance_criteria": ["Relevant code, tests, and prior repair lessons are identified before mutation."],
            },
            {
                "task_id": prepare_id,
                "title": "Prepare repair isolation",
                "goal": f"Create or confirm an isolated repair branch/worktree before changing files for: {objective}",
                "profile": "worker",
                "dependencies": [inspect_id],
                "required_tools": ["repair.prepare", "repair.status"],
                "risk": "high",
                "acceptance_criteria": ["Mutation happens only on an approved repair branch/worktree."],
            },
            {
                "task_id": patch_id,
                "title": "Apply repair patch",
                "goal": f"Apply the smallest repair patch for: {objective}",
                "profile": "worker",
                "dependencies": [prepare_id],
                "required_tools": ["repair.apply_patch", "patch.apply"],
                "risk": "high",
                "acceptance_criteria": ["Patch is scoped to the diagnosed repair and path-safe."],
            },
            {
                "task_id": validate_id,
                "title": "Validate repair",
                "goal": f"Run targeted validation and classify failures for: {objective}",
                "profile": "worker",
                "dependencies": [patch_id],
                "required_tools": ["repair.orchestrate_validate", "repair.validate", "test.run", "lint.run"],
                "risk": "high",
                "acceptance_criteria": ["Targeted validation passes, or retry guidance records a changed strategy."],
            },
            {
                "task_id": review_id,
                "title": "Review repair before commit",
                "goal": f"Create the durable repair.review artifact after successful validation for: {objective}",
                "profile": "reviewer",
                "dependencies": [validate_id],
                "required_tools": ["repair.review", "git.diff", "repair.status"],
                "risk": "medium",
                "acceptance_criteria": ["repair.review records successful validation, current branch, changed files, and current diff hash."],
            },
            {
                "task_id": commit_id,
                "title": "Commit reviewed repair",
                "goal": f"Commit only after repair.review created a current reviewer gate for: {objective}",
                "profile": "worker",
                "dependencies": [review_id],
                "required_tools": ["git.commit"],
                "risk": "high",
                "acceptance_criteria": ["git.commit includes the current repair.review id and still requires exact-call approval."],
            },
        ]
    return [
        {
            "task_id": inspect_id,
            "title": "Inspect context",
            "goal": f"Gather relevant context for: {objective}",
            "profile": "worker",
            "dependencies": [],
            "required_tools": ["memory.search", "context.pack"],
            "risk": "low",
            "acceptance_criteria": ["Relevant memory/context is considered before acting."],
        },
        {
            "task_id": validate_id,
            "title": "Execute and validate",
            "goal": f"Execute the approved low-risk path and validate progress for: {objective}",
            "profile": "worker",
            "dependencies": [inspect_id],
            "required_tools": ["tool.registry"],
            "risk": "low",
            "acceptance_criteria": ["Result is checked against the objective and failures are recorded."],
        },
        {
            "task_id": f"task_{uuid4().hex}",
            "title": "Review outcome",
            "goal": f"Review whether the result satisfies: {objective}",
            "profile": "reviewer",
            "dependencies": [validate_id],
            "required_tools": [],
            "risk": "low",
            "acceptance_criteria": ["Remaining risks or next steps are explicit."],
        },
    ]


def _looks_like_repair_commit_request(message: str) -> bool:
    normalized = message.lower()
    repair_terms = ("repair", "fix", "patch", "failing", "failure", "bug")
    commit_terms = ("commit", "merge", "pr", "pull request")
    validation_terms = ("validate", "test", "lint", "check")
    return (
        any(term in normalized for term in repair_terms)
        and any(term in normalized for term in commit_terms)
        and any(term in normalized for term in validation_terms)
    )


def _trace_category(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", ""))
    payload = event.get("payload", {})
    if event_type.startswith("tool.") or event_type == "assistant.tool_call":
        return "tool"
    if event_type.startswith("memory.") or _payload_has_key(payload, "memory_writes"):
        return "memory"
    if event_type.startswith("context."):
        return "context"
    if event_type.startswith("assistant.") or event_type.startswith("llm.") or event_type.startswith("provider."):
        return "provider"
    if event_type.startswith("approval."):
        return "approval"
    if event_type.endswith(".failed") or event_type.endswith(".error") or _payload_has_key(payload, "error"):
        return "error"
    return "lifecycle"


def _payload_has_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_payload_has_key(item, key) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_payload_has_key(item, key) for item in value)
    return False


def _capsule_decisions(
    summary: Any,
    *,
    agent: NestedMV2Agent,
    dry_run: bool,
) -> list[dict[str, object]]:
    kernel = NestedLearningKernel()
    decisions: list[dict[str, object]] = []
    wrote = False
    for signal in summary.learning_signals:
        decision = kernel.decide(signal)
        payload = decision.to_payload()
        payload["dry_run"] = dry_run
        payload["signal_title"] = signal.title
        if decision.accepted and decision.target_layer is not None:
            if decision.target_layer == MemoryLayer.POLICY and not (
                agent.config.allow_policy_writes and signal.explicit_instruction
            ):
                payload["accepted"] = False
                payload["blocked"] = "policy_write_requires_explicit_config_and_instruction"
            elif not dry_run:
                record = kernel.to_memory_record(signal, decision)
                payload["record_id"] = agent.memory.put(record)
                wrote = True
        decisions.append(payload)
    if wrote:
        agent.memory.seal_all()
    return decisions


def _subagent_prompt(profile: str, goal: str) -> str:
    role = {
        "planner": "Break the goal into a concise execution plan with dependencies and checks.",
        "worker": "Execute the bounded subtask using available low-risk tools and report concrete results.",
        "reviewer": "Review the proposed or completed work for risks, missing tests, and next checks.",
    }.get(profile, "Execute the bounded subtask and report concrete results.")
    return f"Subagent profile: {profile}\nRole: {role}\nGoal:\n{goal}"
