from provider_config import ProviderConfig
import json
import logging
import grpc
from core.grpc_setup import brain_pb2
from core.feature_mode import enabled_bundles_for_mode, parse_feature_mode
from .base import BaseServicerMixin
from core import runtime
from agent.task_profiles import TaskProfile, filter_registry_for_profile, infer_task_profile
from db import get_pool, get_redis
from providers_registry import get_provider, resolve_provider

logger = logging.getLogger("brain.services.agent")

class AgentServicerMixin(BaseServicerMixin):
    async def StartTask(self, request, context):
        """Start an autonomous agent task and stream events."""
        import json
        from agent.types import (
            AgentTask,
            GuardrailConfig as GCfg,
            RiskLevel,
            TaskStatus,
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

        # Store the running task handle
        runtime.running_tasks[task.id] = task

        # Dynamically resolve provider from workspace config instead of
        # using the hardcoded "local" provider from the global _agent_loop.
        ws_config = {}
        provider_name = "local"
        try:
            pool = await get_pool()
            ws_config = await ProviderConfig(pool).get_config(workspace_id)
            provider_name = ws_config.get("provider", "local")
            task_provider = get_provider(provider_name)

            # If workspace config specifies an explicit Ollama server URL,
            # override the provider's base URL to connect to that host.
            _ws_settings = ws_config.get("settings") or {}
            if provider_name in ("ollama", "local") and _ws_settings.get("ollama_host"):
                _host = _ws_settings["ollama_host"].rstrip("/")
                logger.info(f"Task using workspace Ollama host: {_host}")
                task_provider.set_explicit_url(_host)
                # Invalidate stale health cache so is_ready() re-checks the new URL
                from providers.ollama import _health_cache
                _health_cache["checked_at"] = 0
        except Exception as e:
            logger.warning(f"Failed to resolve workspace provider for task, using local: {e}")
            task_provider = get_provider("local")

        from agent.tools import build_tool_registry
        from agent.guardrails import Guardrails
        from agent.loop import AgentLoop
        from agent.evidence import EvidenceChain
        from agent.learner import TaskLearner
        from agent.core.memory import WorkingMemory
        from agent.core.reflection import ReflectionEngine
        from agent.simulation import OutcomeSimulator
        from agent.core.verifier import VerifierEngine

        task_model = ws_config.get("model", "") if ws_config else ""
        task_api_key = ws_config.get("api_key", "") if ws_config else ""

        task_tool_registry = build_tool_registry(
            hands_client=runtime.hands_client,
            pool=pool,
            runtime_policy=runtime.execution_runtime,
            enabled_bundles=tuple(getattr(runtime, "enabled_tool_bundles", []) or enabled_bundles_for_mode(feature_mode)),
            feature_mode=feature_mode.value,
        )
        task_tool_registry = filter_registry_for_profile(task_tool_registry, task_profile, feature_mode)
        evidence_chain = EvidenceChain(task_id=task.id, pool=pool)

        task_working_memory = WorkingMemory(
            redis_client=None,
            vector_store=runtime.vector_store,
        )
        task_learner = TaskLearner(
            provider=task_provider,
            model=task_model,
            working_memory=task_working_memory,
        )
        task_reflection = None
        if feature_mode.value != "core":
            task_reflection = ReflectionEngine(
                llm_provider=task_provider,
                model=task_model,
            )

        # Build simulation gate and verifier engine
        task_simulator = None
        if feature_mode.value == "labs":
            task_simulator = OutcomeSimulator(
                llm_provider=task_provider,
                model=task_model,
            )
        task_verifier = VerifierEngine(
            provider=task_provider,
            model=task_model,
        )

        from agent.model_router import ModelRouter

        def custom_provider_checker(name: str) -> bool:
            if name == provider_name and getattr(task_provider, "is_ready", lambda: False)():
                return True
            try:
                return get_provider(name).is_ready()
            except Exception:
                return False

        task_model_router = ModelRouter(
            provider_checker=custom_provider_checker,
            workspace_provider=provider_name,
            workspace_model=task_model,
        )

        task_loop = AgentLoop(
            provider=task_provider,
            tool_registry=task_tool_registry,
            guardrails=Guardrails(),
            persistence=runtime.agent_persistence,
            model=task_model,
            api_key=task_api_key,
            memory_graph=runtime.memory_graph,
            evidence_chain=evidence_chain,
            learner=task_learner,
            reflection_engine=task_reflection,
            provider_resolver=resolve_provider,
            model_router=task_model_router,
            simulator=task_simulator,
            verifier=task_verifier,
            persona_learner=runtime.persona_learner,
        )

        # Register this task as an active session
        if runtime.session_manager:
            try:
                await runtime.session_manager.register_session(
                    session_id=task.id,
                    task_id=task.id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    agent_type="task",
                    model=task_model,
                    goal=goal,
                )
            except Exception as e:
                logger.warning(f"Session registration failed: {e}")

        # Run the task-specific agent loop and stream events
        try:
            async for event in task_loop.run(task):
                event_type_value = event.type.value if hasattr(event.type, "value") else str(event.type)
                task_event = brain_pb2.TaskEvent(
                    type=self._event_type_to_proto(event_type_value),
                    task_id=event.task_id,
                    step_id=event.step_id or "",
                    content=event.content,
                    tool_name=event.tool_name or "",
                    tool_args=event.tool_args or "",
                    tool_result=event.tool_result or "",
                    approval_id=event.approval_id or "",
                    progress={k: str(v) for k, v in (event.progress or {}).items()},
                )
                await self._persist_task_event(task_event)
                yield task_event
        except Exception as e:
            logger.error(f"StartTask error for task {task.id}: {e}", exc_info=True)
            failed_event = brain_pb2.TaskEvent(
                type=brain_pb2.TaskEvent.EventType.TASK_FAILED,
                task_id=task.id,
                content=str(e),
            )
            await self._persist_task_event(failed_event)
            yield failed_event
        finally:
            runtime.running_tasks.pop(task.id, None)
            if runtime.session_manager:
                try:
                    await runtime.session_manager.deregister_session(task.id)
                except Exception as e:
                    logger.warning(f"Session deregistration failed: {e}")

    async def StreamTaskEvents(self, request, context):
        """Reconnect to an already-running task's event stream."""
        task_id = request.task_id
        redis_client = await get_redis()
        history_key = f"kestrel:task_events:{task_id}"
        channel = f"kestrel:task_events:{task_id}:channel"

        history = await redis_client.lrange(history_key, 0, -1)
        for raw in history:
            try:
                payload = json.loads(raw)
                yield self._task_event_from_json(payload)
            except Exception as replay_err:
                logger.warning(f"Failed to replay task event for {task_id}: {replay_err}")

        if task_id not in runtime.running_tasks and not history:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Task {task_id} is not running")
            return

        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                if context.cancelled():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("data"):
                    try:
                        payload = json.loads(message["data"])
                        yield self._task_event_from_json(payload)
                    except Exception as stream_err:
                        logger.warning(f"Failed to parse streamed task event for {task_id}: {stream_err}")

                if task_id not in runtime.running_tasks and not message:
                    break
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass

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

        if task_id in runtime.running_tasks:
            task = runtime.running_tasks[task_id]
            task.status = "cancelled"
            await runtime.agent_persistence.update_task(task)
            runtime.running_tasks.pop(task_id, None)
            return brain_pb2.CancelTaskResponse(success=True)

        # Try updating DB directly
        pool = await get_pool()
        await pool.execute(
            "UPDATE agent_tasks SET status = 'cancelled' WHERE id = $1",
            task_id,
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
        feature_mode = parse_feature_mode(getattr(runtime, "feature_mode", "core"))
        task.task_profile = TaskProfile.OPS.value

        await runtime.agent_persistence.save_task(task)
        logger.info(f"Headless task started: {task.id} — {goal[:50]}...")
        runtime.running_tasks[task.id] = task

        provider_name = "local"
        try:
            pool = await get_pool()
            ws_config = await ProviderConfig(pool).get_config(workspace_id)
            provider_name = ws_config.get("provider", "local")
            task_provider = get_provider(provider_name)
        except Exception as e:
            logger.warning(f"Failed to resolve workspace provider for headless task, using local: {e}")
            task_provider = get_provider("local")

        from agent.tools import build_tool_registry
        from agent.guardrails import Guardrails
        from agent.loop import AgentLoop
        from agent.evidence import EvidenceChain
        from agent.learner import TaskLearner
        from agent.core.memory import WorkingMemory
        from agent.core.reflection import ReflectionEngine
        from agent.simulation import OutcomeSimulator
        from agent.core.verifier import VerifierEngine
        from agent.model_router import ModelRouter

        task_model = ws_config.get("model", "") if ws_config else ""
        task_api_key = ws_config.get("api_key", "") if ws_config else ""
        task_tool_registry = build_tool_registry(
            hands_client=runtime.hands_client,
            pool=pool,
            runtime_policy=runtime.execution_runtime,
            enabled_bundles=tuple(getattr(runtime, "enabled_tool_bundles", []) or enabled_bundles_for_mode(feature_mode)),
            feature_mode=feature_mode.value,
        )
        task_tool_registry = filter_registry_for_profile(task_tool_registry, TaskProfile.OPS, feature_mode)
        evidence_chain = EvidenceChain(task_id=task.id, pool=pool)

        task_working_memory = WorkingMemory(redis_client=None, vector_store=runtime.vector_store)
        task_learner = TaskLearner(provider=task_provider, model=task_model, working_memory=task_working_memory)
        task_reflection = None if feature_mode.value == "core" else ReflectionEngine(llm_provider=task_provider, model=task_model)
        task_simulator = OutcomeSimulator(llm_provider=task_provider, model=task_model) if feature_mode.value == "labs" else None
        task_verifier = VerifierEngine(provider=task_provider, model=task_model)

        def custom_provider_checker(name: str) -> bool:
            if name == provider_name and getattr(task_provider, "is_ready", lambda: False)(): return True
            try: return get_provider(name).is_ready()
            except Exception: return False

        task_model_router = ModelRouter(
            provider_checker=custom_provider_checker,
            workspace_provider=provider_name,
            workspace_model=task_model,
        )

        task_loop = AgentLoop(
            provider=task_provider,
            tool_registry=task_tool_registry,
            guardrails=Guardrails(),
            persistence=runtime.agent_persistence,
            model=task_model,
            api_key=task_api_key,
            memory_graph=runtime.memory_graph,
            evidence_chain=evidence_chain,
            learner=task_learner,
            reflection_engine=task_reflection,
            provider_resolver=resolve_provider,
            model_router=task_model_router,
            simulator=task_simulator,
            verifier=task_verifier,
            persona_learner=runtime.persona_learner,
        )

        iterations = 0
        final_result = ""
        error_msg = ""
        try:
            async for event in task_loop.run(task):
                iterations += 1
                if str(event.type) == "EventType.TASK_COMPLETE" or str(event.type) == "TASK_COMPLETE":
                    final_result = event.content or event.tool_result or ""
                elif str(event.type) == "EventType.TASK_FAILED" or str(event.type) == "TASK_FAILED":
                    error_msg = event.content or "Task failed without explicit error"
        except Exception as e:
            logger.error(f"Headless task error {task.id}: {e}", exc_info=True)
            error_msg = str(e)
        finally:
            runtime.running_tasks.pop(task.id, None)

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
