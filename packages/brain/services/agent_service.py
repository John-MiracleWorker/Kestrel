import asyncio
import json
import logging
import time
import grpc
from core.grpc_setup import brain_pb2
from core.feature_mode import parse_feature_mode
from .base import BaseServicerMixin
from core import runtime
from agent.task_events import is_task_terminal, load_task_event_history
from agent.task_profiles import TaskProfile, infer_task_profile
from db import get_pool, get_redis

logger = logging.getLogger("brain.services.agent")

class AgentServicerMixin(BaseServicerMixin):
    async def _stream_task_event_feed(self, task_id: str, context):
        """Replay durable history, then stream live pubsub until the task is terminal."""
        seen_event_keys: set[str] = set()

        def _event_key(payload: dict) -> str:
            return json.dumps(payload, sort_keys=True, default=str)

        history = await load_task_event_history(task_id)
        for payload in history:
            key = _event_key(payload)
            if key in seen_event_keys:
                continue
            seen_event_keys.add(key)
            yield self._task_event_from_json(payload)

        redis_client = await get_redis()
        channel = f"kestrel:task_events:{task_id}:channel"
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                if context.cancelled():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("data"):
                    try:
                        raw = message["data"]
                        payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
                        key = _event_key(payload)
                        if key in seen_event_keys:
                            continue
                        seen_event_keys.add(key)
                        yield self._task_event_from_json(payload)
                    except Exception as stream_err:
                        logger.warning(f"Failed to parse streamed task event for {task_id}: {stream_err}")
                elif await is_task_terminal(task_id):
                    break
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass

    async def StartTask(self, request, context):
        """Start an autonomous agent task and stream events."""
        from agent.types import (
            AgentTask,
            GuardrailConfig as GCfg,
            RiskLevel,
        )

        user_id = request.user_id
        workspace_id = request.workspace_id
        goal = request.goal

        # Build guardrail config from request (or defaults)
        config = GCfg()
        if request.guardrails:
            g = request.guardrails
            if g.max_iterations > 0:
                config.max_iterations = g.max_iterations
            if g.max_tool_calls > 0:
                config.max_tool_calls = g.max_tool_calls
            if g.max_tokens > 0:
                config.max_tokens = g.max_tokens
            if g.max_wall_time_seconds > 0:
                config.max_wall_time_seconds = g.max_wall_time_seconds
            if g.auto_approve_risk:
                config.auto_approve_risk = RiskLevel(g.auto_approve_risk)
            if g.blocked_patterns:
                config.blocked_patterns = list(g.blocked_patterns)
            if g.require_approval_tools:
                config.require_approval_tools = list(g.require_approval_tools)

        # Create the task
        task = AgentTask(
            user_id=user_id,
            workspace_id=workspace_id,
            goal=goal,
            conversation_id=request.conversation_id or None,
            config=config,
        )
        feature_mode = parse_feature_mode(getattr(runtime, "feature_mode", "core"))
        task_profile = infer_task_profile(goal, feature_mode)
        task.task_profile = task_profile.value

        # Save to DB
        await runtime.agent_persistence.save_task(task)
        logger.info(f"Agent task started: {task.id} — {goal}")
        queue_job = await runtime.task_enqueuer.enqueue(
            workspace_id=workspace_id,
            user_id=user_id,
            goal=goal,
            source="user",
            priority=7,
            agent_task_id=task.id,
            payload_json={
                "conversation_id": request.conversation_id or "",
                "task_profile": task_profile.value,
            },
            trigger_kind="interactive",
        )

        queued_event = brain_pb2.TaskEvent(
            type=brain_pb2.TaskEvent.EventType.THINKING,
            task_id=task.id,
            content=f"Task queued for execution ({queue_job.id}).",
            progress={"queue_id": queue_job.id, "status": "queued"},
        )
        await self._persist_task_event(
            queued_event,
            workspace_id=workspace_id,
            user_id=user_id,
        )

        async for event in self._stream_task_event_feed(task.id, context):
            yield event

    async def StreamTaskEvents(self, request, context):
        """Reconnect to an already-running task's event stream."""
        task_id = request.task_id
        history = await load_task_event_history(task_id)
        if not history and not await is_task_terminal(task_id):
            context.abort(grpc.StatusCode.NOT_FOUND, f"Task {task_id} is not running")
            return
        async for event in self._stream_task_event_feed(task_id, context):
            yield event

    async def ApproveAction(self, request, context):
        """Approve or deny a pending agent action."""
        from agent.types import ApprovalStatus

        try:
            updated = await runtime.agent_persistence.resolve_approval(
                approval_id=request.approval_id,
                status=ApprovalStatus.APPROVED if request.approved else ApprovalStatus.DENIED,
                decided_by=request.user_id,
            )
            if not updated:
                return brain_pb2.ApproveActionResponse(
                    success=False,
                    error="Approval not found, already resolved, or not owned by this user.",
                )
            return brain_pb2.ApproveActionResponse(success=True, error="")
        except Exception as e:
            return brain_pb2.ApproveActionResponse(success=False, error=str(e))

    async def ListPendingApprovals(self, request, context):
        """List unresolved approvals for a user/workspace."""
        try:
            approvals = await runtime.agent_persistence.list_pending_approvals(
                user_id=request.user_id,
                workspace_id=request.workspace_id or None,
            )
            items = [
                brain_pb2.PendingApprovalSummary(
                    approval_id=item["approval_id"],
                    task_id=item["task_id"],
                    tool_name=item["tool_name"],
                    reason=item["reason"],
                    created_at=item["created_at"].isoformat() if item["created_at"] else "",
                )
                for item in approvals
            ]
            return brain_pb2.ListPendingApprovalsResponse(approvals=items)
        except Exception as e:
            logger.exception("ListPendingApprovals failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    async def CancelTask(self, request, context):
        """Cancel a running agent task."""
        task_id = request.task_id
        queue_job = None
        if getattr(runtime, "task_enqueuer", None):
            queue_job = await runtime.task_enqueuer.get_latest_for_task(task_id)

        if task_id in runtime.running_tasks:
            task = runtime.running_tasks[task_id]
            task.status = "cancelled"
            await runtime.agent_persistence.update_task(task)
            runtime.running_tasks.pop(task_id, None)
            if queue_job:
                await runtime.task_enqueuer.cancel(
                    queue_job.id,
                    error="Cancelled by user",
                    terminal_task_id=task_id,
                )
                if getattr(runtime, "task_dispatcher", None):
                    await runtime.task_dispatcher.cancel_job(queue_job.id)
            return brain_pb2.CancelTaskResponse(success=True)

        # Try updating DB directly
        pool = await get_pool()
        await pool.execute(
            "UPDATE agent_tasks SET status = 'cancelled', completed_at = now() WHERE id = $1",
            task_id,
        )
        if queue_job:
            await runtime.task_enqueuer.cancel(
                queue_job.id,
                error="Cancelled by user",
                terminal_task_id=task_id,
            )
        return brain_pb2.CancelTaskResponse(success=True)

    async def ListTasks(self, request, context):
        """List agent tasks for a user/workspace."""
        pool = await get_pool()
        query = """
            SELECT id, goal, status, iterations, tool_calls_count,
                   result, error, created_at, completed_at
            FROM agent_tasks
            WHERE user_id = $1
        """
        params = [request.user_id]

        if request.workspace_id:
            query += " AND workspace_id = $2"
            params.append(request.workspace_id)

        if request.status:
            query += f" AND status = ${len(params) + 1}"
            params.append(request.status)

        query += " ORDER BY created_at DESC LIMIT 50"

        rows = await pool.fetch(query, *params)
        tasks = []
        for row in rows:
            tasks.append(brain_pb2.TaskSummary(
                id=str(row["id"]),
                goal=row["goal"],
                status=row["status"],
                iterations=row["iterations"],
                tool_calls=row["tool_calls_count"],
                result=row["result"] or "",
                error=row["error"] or "",
                created_at=row["created_at"].isoformat() if row["created_at"] else "",
                completed_at=row["completed_at"].isoformat() if row["completed_at"] else "",
            ))

        return brain_pb2.ListTasksResponse(tasks=tasks)

    async def RunHeadlessTask(self, request, context):
        """
        Execute a task headlessly, waiting for completion and returning
        a strict JSON output based on the provided schema. No event streaming.
        Useful for CI/CD or background jobs.
        """
        import json
        from agent.types import AgentTask, GuardrailConfig as GCfg, TaskStatus

        user_id = request.user_id
        workspace_id = request.workspace_id
        goal = request.goal
        schema_json = request.expected_schema_json

        # Append schema requirements to the goal
        headless_goal = goal
        if schema_json:
             headless_goal += f"\n\n[HEADLESS EXECUTION SYSTEM PROMPT]\nYou are running in headless mode. Your final answer (via task_complete) MUST be a raw, valid JSON object conforming exactly to this schema:\n{schema_json}\nDo not include any markdown formatting (like ```json), commentary, or extra text in your final summary. Just the raw JSON object."

        config = GCfg()
        if request.guardrails:
            g = request.guardrails
            if g.max_iterations > 0: config.max_iterations = g.max_iterations
            if g.max_tool_calls > 0: config.max_tool_calls = g.max_tool_calls
            if g.max_tokens > 0:     config.max_tokens = g.max_tokens
            if g.max_wall_time_seconds > 0: config.max_wall_time_seconds = g.max_wall_time_seconds

        task = AgentTask(
            user_id=user_id,
            workspace_id=workspace_id,
            goal=headless_goal,
            conversation_id=None,
            config=config,
        )
        task.task_profile = TaskProfile.OPS.value

        await runtime.agent_persistence.save_task(task)
        logger.info(f"Headless task queued: {task.id} — {goal[:50]}...")

        queue_job = await runtime.task_enqueuer.enqueue(
            workspace_id=workspace_id,
            user_id=user_id,
            goal=headless_goal,
            source="user",
            priority=8,
            agent_task_id=task.id,
            payload_json={
                "task_profile": TaskProfile.OPS.value,
                "headless": True,
                "expected_schema_json": schema_json or "",
            },
            trigger_kind="headless",
        )

        timeout_seconds = max(task.config.max_wall_time_seconds, 30) + 30
        deadline = time.monotonic() + timeout_seconds
        final_task = task
        while time.monotonic() < deadline:
            if context.cancelled():
                await runtime.task_enqueuer.cancel(
                    queue_job.id,
                    error="Headless caller cancelled the request.",
                    terminal_task_id=task.id,
                )
                if getattr(runtime, "task_dispatcher", None):
                    await runtime.task_dispatcher.cancel_job(queue_job.id)
                return brain_pb2.RunHeadlessTaskResponse(
                    success=False,
                    result_json="",
                    error="Headless caller cancelled the request.",
                    iterations=final_task.iterations,
                )

            latest_task = await runtime.agent_persistence.get_task(task.id)
            if latest_task:
                final_task = latest_task
                if latest_task.status in {
                    TaskStatus.COMPLETE,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    break
            await asyncio.sleep(0.5)
        else:
            await runtime.task_enqueuer.cancel(
                queue_job.id,
                error="Headless task timed out waiting for queued completion.",
                terminal_task_id=task.id,
            )
            if getattr(runtime, "task_dispatcher", None):
                await runtime.task_dispatcher.cancel_job(queue_job.id)
            latest_task = await runtime.agent_persistence.get_task(task.id)
            if latest_task:
                final_task = latest_task
            final_task.status = TaskStatus.FAILED
            final_task.error = "Headless task timed out waiting for queued completion."
            await runtime.agent_persistence.update_task(final_task)

        iterations = final_task.iterations
        final_result = final_task.result or ""
        error_msg = final_task.error or ""
        if final_task.status == TaskStatus.CANCELLED and not error_msg:
            error_msg = "Task was cancelled."

        if not final_result and task.result:
            final_result = task.result

        # Optional: Auto-strip markdown block if the model included it despite prompt
        if final_result.startswith("```json"):
            final_result = final_result[7:]
            if final_result.endswith("```"):
                final_result = final_result[:-3]
            final_result = final_result.strip()
        elif final_result.startswith("```"):
            final_result = final_result[3:]
            if final_result.endswith("```"):
                final_result = final_result[:-3]
            final_result = final_result.strip()

        # Try to parse it to ensure it is JSON, though we return the string anyway
        # If it fails, that's up to the client, but we log it.
        try:
            if final_result:
                json.loads(final_result)
        except json.JSONDecodeError:
            logger.warning(f"Headless task {task.id} returned invalid JSON: {final_result[:100]}")

        success = (not error_msg) and bool(final_result)

        return brain_pb2.RunHeadlessTaskResponse(
            success=success,
            result_json=final_result,
            error=error_msg,
            iterations=iterations
        )
