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
from .event_bus import RunEventBus
from .mcp_manager import MCPManager
from .runtime_models import AgentTurnResult, LLMStreamEvent, ToolCall, ToolExecution, ToolSpec
from .skill_manager import SkillManager
from .state_store import AgentStateStore, RunRecord
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
        self.state.create_task_node(
            task_id=f"task_{uuid4().hex}",
            run_id=run_id,
            title="Root objective",
            goal=message,
            profile="planner",
            status="queued",
            approved=True,
            plan={"autonomy_mode": "background"},
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

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._cancelled.add(run_id)
        run = self.state.update_run(run_id, status="cancelled", stop_reason="cancelled")
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
            self.state.update_run(updated["run_id"], status="failed", error="Approval denied", stop_reason="approval_denied")
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
            "tasks": [asdict(task) for task in self.state.list_task_nodes(run_id)],
            "subagents": [asdict(subagent) for subagent in self.state.list_subagent_runs(run_id)],
        }

    def approve_task(self, run_id: str, task_id: str) -> dict[str, Any]:
        self.state.get_run(run_id)
        task = self.state.update_task_node(task_id, approved=True, status="approved")
        self.events.publish(run_id, "task.approved", asdict(task))
        return asdict(task)

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
        self.state.update_run(run_id, status="running")
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
            for execution in result.tool_executions:
                self.events.publish(run_id, "tool.executed", _execution_payload(execution))
                self.events.publish(
                    run_id,
                    "tool.completed" if execution.success else "tool.failed",
                    _execution_payload(execution),
                )
            status = "blocked" if result.stop_reason == "approval_required" else "completed"
            self.state.update_run(
                run_id,
                status=status,
                assistant_message=result.assistant_message,
                context_chars=result.context_chars,
                tool_count=len(result.tool_executions),
                stop_reason=result.stop_reason,
            )
            self.events.publish(run_id, "run.blocked" if status == "blocked" else "run.completed", _turn_payload(result))
        except Exception as exc:  # noqa: BLE001
            self.state.update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}", stop_reason="error")
            self.events.publish(run_id, "run.failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            agent.close()

    def _resume_after_approval(self, approval: dict[str, Any], arguments: dict[str, Any]) -> None:
        run_id = str(approval["run_id"])
        run = self.state.get_run(run_id)
        config = replace(self.config, workspace=Path(run.workspace), model=run.model)
        self.state.update_run(run_id, status="running", stop_reason="resuming_after_approval")
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
                ),
            )
            self.state.decide_approval(
                str(approval["approval_id"]),
                status="approved",
                decision={"approved": True, "arguments": arguments},
                result=_execution_payload(execution),
            )
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
            status = "blocked" if result.stop_reason == "approval_required" else "completed"
            self.state.update_run(
                run_id,
                status=status,
                assistant_message=result.assistant_message,
                context_chars=result.context_chars,
                tool_count=len(result.tool_executions) + 1,
                stop_reason=result.stop_reason,
            )
            self.events.publish(run_id, "run.blocked" if status == "blocked" else "run.completed", _turn_payload(result))
        except Exception as exc:  # noqa: BLE001
            self.state.update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}", stop_reason="error")
            self.events.publish(run_id, "run.failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            agent.close()

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
            updated = self.state.update_subagent_run(subagent_id, status="completed", result=result.assistant_message)
            if subagent.task_id:
                self.state.update_task_node(
                    subagent.task_id,
                    status="completed",
                    result={"assistant_message": result.assistant_message, "stop_reason": result.stop_reason},
                )
            self.events.publish(run_id, "subagent.completed", asdict(updated))
        except Exception as exc:  # noqa: BLE001
            updated = self.state.update_subagent_run(subagent_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            if subagent.task_id:
                self.state.update_task_node(subagent.task_id, status="failed", result={"error": updated.error})
            self.events.publish(run_id, "subagent.failed", asdict(updated))
        finally:
            agent.close()

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


def _subagent_prompt(profile: str, goal: str) -> str:
    role = {
        "planner": "Break the goal into a concise execution plan with dependencies and checks.",
        "worker": "Execute the bounded subtask using available low-risk tools and report concrete results.",
        "reviewer": "Review the proposed or completed work for risks, missing tests, and next checks.",
    }.get(profile, "Execute the bounded subtask and report concrete results.")
    return f"Subagent profile: {profile}\nRole: {role}\nGoal:\n{goal}"
