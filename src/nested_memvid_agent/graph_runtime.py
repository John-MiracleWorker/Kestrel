from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .agent import NestedMV2Agent, ProgressHandler, StreamHandler
from .config import AgentConfig
from .diagnosis import classify_failure
from .runtime_models import AgentTurnResult, ToolExecution
from .state_store import AgentStateStore, RunRecord
from .tools.base import ApprovalHandler
from .tracing import SpanRecorder


class EventPublisher(Protocol):
    def publish(self, run_id: str, type: str, payload: dict[str, Any]) -> Any:
        raise NotImplementedError


@dataclass
class GraphRunState:
    run_id: str
    config: AgentConfig
    message: str
    session_id: str
    agent: NestedMV2Agent | None = None
    result: AgentTurnResult | None = None
    exception: Exception | None = None
    review: dict[str, Any] = field(default_factory=dict)
    finalized: bool = False


@dataclass(frozen=True)
class GraphRuntimeServices:
    state: AgentStateStore
    events: EventPublisher
    spans: SpanRecorder
    build_agent: Callable[[AgentConfig], NestedMV2Agent]
    approval_handler: ApprovalHandler
    stream_handler_factory: Callable[[str], StreamHandler]
    progress_handler_factory: Callable[[str], ProgressHandler]
    publish_turn_observability: Callable[[str, AgentTurnResult], None]
    publish_tool_executions: Callable[[str, tuple[ToolExecution, ...]], None]
    complete_capsule: Callable[[str, AgentConfig, NestedMV2Agent, AgentTurnResult], None]
    run_scheduler_until_idle: Callable[[str, int | None, int | None], dict[str, Any]]
    scheduler_outcome: Callable[[dict[str, Any]], tuple[str, str]]
    is_cancelled: Callable[[str], bool]


class PlannerNode:
    name = "PlannerNode"
    span_type = "plan"

    def run(self, ctx: GraphRunState, services: GraphRuntimeServices, parent_span_id: str | None) -> None:
        with services.spans.start(
            run_id=ctx.run_id,
            span_type=self.span_type,
            name=self.name,
            parent_span_id=parent_span_id,
            metadata={"session_id": ctx.session_id},
        ) as span:
            services.state.transition_run(ctx.run_id, "running")
            services.events.publish(ctx.run_id, "run.started", {"session_id": ctx.session_id})
            tasks = services.state.list_task_nodes(ctx.run_id)
            root = next((task for task in tasks if task.parent_id is None and task.profile == "planner"), None)
            if root is not None:
                existing_plan = dict(root.plan or {})
                existing_plan["graph_runtime"] = {
                    "revision": int(existing_plan.get("revision", 0) or 0) + 1,
                    "nodes": [
                        "PlannerNode",
                        "ExecutorNode",
                        "ReviewerNode",
                        "RecoveryNode",
                        "MemoryPromotionNode",
                        "FinalizerNode",
                    ],
                    "can_revise_plan": True,
                    "approval_pause_resume": True,
                    "reviewer_gate": True,
                    "worker_assignment": "task_nodes_and_subagent_records",
                }
                services.state.update_task_node(root.task_id, plan=existing_plan)
            services.events.publish(
                ctx.run_id,
                "orchestration.plan",
                {
                    "node": self.name,
                    "task_count": len(tasks),
                    "root_task_id": root.task_id if root is not None else None,
                },
            )
            span.set_result(output={"task_count": len(tasks), "root_task_id": root.task_id if root is not None else None})


class ExecutorNode:
    name = "ExecutorNode"
    span_type = "llm.request"

    def run(self, ctx: GraphRunState, services: GraphRuntimeServices, parent_span_id: str | None) -> None:
        with services.spans.start(
            run_id=ctx.run_id,
            span_type=self.span_type,
            name=self.name,
            parent_span_id=parent_span_id,
            metadata={"provider": ctx.config.provider, "model": ctx.config.model},
        ) as span:
            ctx.agent = services.build_agent(ctx.config)
            try:
                ctx.result = ctx.agent.chat(
                    ctx.message,
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    approval_handler=services.approval_handler,
                    stream_handler=services.stream_handler_factory(ctx.run_id),
                    progress_handler=services.progress_handler_factory(ctx.run_id),
                )
            except Exception as exc:  # noqa: BLE001 - durable graph records and recovers below
                ctx.exception = exc
                services.events.publish(
                    ctx.run_id,
                    "orchestration.executor_failed",
                    {"node": self.name, "error": f"{type(exc).__name__}: {exc}"},
                )
                span.set_result(status="failed", error=f"{type(exc).__name__}: {exc}")
                return
            services.publish_turn_observability(ctx.run_id, ctx.result)
            services.publish_tool_executions(ctx.run_id, ctx.result.tool_executions)
            span.set_result(
                output={
                    "stop_reason": ctx.result.stop_reason,
                    "tool_count": len(ctx.result.tool_executions),
                    "context_chars": ctx.result.context_chars,
                }
            )


class ReviewerNode:
    name = "ReviewerNode"
    span_type = "review"

    def run(self, ctx: GraphRunState, services: GraphRuntimeServices, parent_span_id: str | None) -> None:
        with services.spans.start(
            run_id=ctx.run_id,
            span_type=self.span_type,
            name=self.name,
            parent_span_id=parent_span_id,
        ) as span:
            if ctx.exception is not None:
                ctx.review = {
                    "status": "failed",
                    "stop_reason": "executor_error",
                    "error": f"{type(ctx.exception).__name__}: {ctx.exception}",
                }
            elif ctx.result is None:
                ctx.review = {"status": "failed", "stop_reason": "missing_result", "error": "Executor produced no result"}
            elif ctx.result.stop_reason == "approval_required":
                ctx.review = {"status": "blocked", "stop_reason": "approval_required", "gate": "approval_wait"}
            elif ctx.result.stop_reason in {"provider_error", "empty_response", "loop_exhausted", "max_tool_rounds"}:
                ctx.review = {
                    "status": "failed",
                    "stop_reason": ctx.result.stop_reason,
                    "error": ctx.result.assistant_message,
                }
            else:
                ctx.review = {
                    "status": "completed",
                    "stop_reason": ctx.result.stop_reason,
                    "gate": "review_passed",
                }
            services.events.publish(ctx.run_id, "review.completed", {"node": self.name, **ctx.review})
            span.set_result(status=str(ctx.review["status"]), output=ctx.review)


class RecoveryNode:
    name = "RecoveryNode"
    span_type = "eval"

    def run(self, ctx: GraphRunState, services: GraphRuntimeServices, parent_span_id: str | None) -> None:
        status = str(ctx.review.get("status") or "")
        if status not in {"failed", "blocked"}:
            return
        span_type = "approval.wait" if status == "blocked" else self.span_type
        with services.spans.start(
            run_id=ctx.run_id,
            span_type=span_type,
            name=self.name,
            parent_span_id=parent_span_id,
            metadata={"review_status": status, "stop_reason": ctx.review.get("stop_reason")},
        ) as span:
            if status == "blocked":
                pending = [approval for approval in services.state.list_approvals(status="pending") if approval["run_id"] == ctx.run_id]
                output = {"pending_approval_count": len(pending), "approval_ids": [item["approval_id"] for item in pending]}
                services.events.publish(
                    ctx.run_id,
                    "approval.wait",
                    output,
                )
                span.set_result(status="blocked", output=output)
                return
            error_text = str(ctx.review.get("error") or "Unknown orchestration failure")
            diagnosis = classify_failure(error_text, source="orchestration").to_payload()
            services.events.publish(
                ctx.run_id,
                "diagnosis.classified",
                {"source": "orchestration", **diagnosis},
            )
            root = next(
                (
                    task
                    for task in services.state.list_task_nodes(ctx.run_id)
                    if task.parent_id is None and task.profile == "planner"
                ),
                None,
            )
            if root is not None and root.status not in {"completed", "failed", "cancelled"}:
                services.state.record_task_failure(
                    root.task_id,
                    failure_reason=error_text,
                    diagnosis=diagnosis,
                    retry_strategy={
                        "requires_changed_strategy": True,
                        "retry_allowed": False,
                        "reason": "graph runtime failure must be reviewed before retry",
                    },
                    result={"review": ctx.review},
                )
            span.set_result(status="failed", output={"diagnosis": diagnosis}, error=error_text)


class MemoryPromotionNode:
    name = "MemoryPromotionNode"
    span_type = "memory.write"

    def run(self, ctx: GraphRunState, services: GraphRuntimeServices, parent_span_id: str | None) -> None:
        if ctx.review.get("status") != "completed" or ctx.agent is None or ctx.result is None:
            return
        with services.spans.start(
            run_id=ctx.run_id,
            span_type=self.span_type,
            name=self.name,
            parent_span_id=parent_span_id,
            metadata={"auto_consolidation_enabled": ctx.config.enable_auto_consolidation},
        ) as span:
            services.complete_capsule(ctx.run_id, ctx.config, ctx.agent, ctx.result)
            span.set_result(output={"capsule_attempted": True})


class FinalizerNode:
    name = "FinalizerNode"
    span_type = "run"

    def run(self, ctx: GraphRunState, services: GraphRuntimeServices, parent_span_id: str | None) -> None:
        with services.spans.start(
            run_id=ctx.run_id,
            span_type=self.span_type,
            name=self.name,
            parent_span_id=parent_span_id,
            metadata={"review_status": ctx.review.get("status")},
        ) as span:
            ctx.finalized = True
            status = str(ctx.review.get("status") or "failed")
            if status == "blocked" and ctx.result is not None:
                services.state.transition_run(
                    ctx.run_id,
                    "blocked",
                    assistant_message=ctx.result.assistant_message,
                    context_chars=ctx.result.context_chars,
                    tool_count=len(ctx.result.tool_executions),
                    stop_reason=str(ctx.review.get("stop_reason") or ctx.result.stop_reason),
                )
                services.events.publish(ctx.run_id, "run.blocked", _turn_payload(ctx.result))
                span.set_result(status="blocked", output={"stop_reason": ctx.review.get("stop_reason")})
                return
            if status != "completed" or ctx.result is None:
                error = str(ctx.review.get("error") or "Orchestration failed")
                services.state.transition_run(
                    ctx.run_id,
                    "failed",
                    assistant_message=ctx.result.assistant_message if ctx.result is not None else "",
                    context_chars=ctx.result.context_chars if ctx.result is not None else 0,
                    tool_count=len(ctx.result.tool_executions) if ctx.result is not None else 0,
                    stop_reason=str(ctx.review.get("stop_reason") or "orchestration_failed"),
                    error=error,
                )
                services.events.publish(ctx.run_id, "run.failed", {"error": error, "review": ctx.review})
                span.set_result(status="failed", output={"review": ctx.review}, error=error)
                return

            run_status = "running" if ctx.config.enable_autonomous_scheduler else "completed"
            stop_reason = "scheduler_running" if run_status == "running" else ctx.result.stop_reason
            services.state.transition_run(
                ctx.run_id,
                run_status,
                assistant_message=ctx.result.assistant_message,
                context_chars=ctx.result.context_chars,
                tool_count=len(ctx.result.tool_executions),
                stop_reason=stop_reason,
            )
            event_type = "run.turn_completed" if ctx.config.enable_autonomous_scheduler else "run.completed"
            services.events.publish(ctx.run_id, event_type, _turn_payload(ctx.result))
            if ctx.config.enable_autonomous_scheduler:
                scheduler = services.run_scheduler_until_idle(
                    ctx.run_id,
                    ctx.config.max_scheduler_tasks,
                    ctx.config.max_scheduler_cycles,
                )
                final_status, scheduler_stop_reason = services.scheduler_outcome(scheduler)
                services.state.transition_run(ctx.run_id, final_status, stop_reason=scheduler_stop_reason)
                services.events.publish(ctx.run_id, f"run.{final_status}", {"scheduler": scheduler, "turn": _turn_payload(ctx.result)})
                span.set_result(status=final_status, output={"stop_reason": scheduler_stop_reason})
                return
            span.set_result(status="completed", output={"stop_reason": ctx.result.stop_reason})


class DurableOrchestrationRuntime:
    """Sequential durable graph runtime layered above the existing chat/tool loop."""

    def __init__(self, services: GraphRuntimeServices) -> None:
        self.services = services
        self.nodes = (
            PlannerNode(),
            ExecutorNode(),
            ReviewerNode(),
            RecoveryNode(),
            MemoryPromotionNode(),
            FinalizerNode(),
        )

    def run_chat_turn(self, *, run: RunRecord, config: AgentConfig, message: str) -> None:
        ctx = GraphRunState(run_id=run.run_id, config=config, message=message, session_id=run.session_id)
        root_span = self.services.spans.start(
            run_id=run.run_id,
            span_type="run",
            name="DurableOrchestrationRuntime",
            metadata={"message_chars": len(message), "provider": config.provider},
        )
        with root_span as span:
            try:
                for node in self.nodes:
                    if self.services.is_cancelled(run.run_id):
                        return
                    node.run(ctx, self.services, span.span_id)
                    if isinstance(node, ExecutorNode) and ctx.exception is not None:
                        continue
                if not ctx.finalized and not self.services.is_cancelled(run.run_id):
                    FinalizerNode().run(ctx, self.services, span.span_id)
            finally:
                if ctx.agent is not None:
                    ctx.agent.close()


def _turn_payload(result: AgentTurnResult) -> dict[str, Any]:
    return {
        "session_id": result.session_id,
        "user_message": result.user_message,
        "assistant_message": result.assistant_message,
        "tool_executions": [_execution_payload(execution) for execution in result.tool_executions],
        "context_chars": result.context_chars,
        "memory_writes": list(result.memory_writes),
        "stop_reason": result.stop_reason,
        "proof_of_work": result.proof_of_work,
    }


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
