"""
LangGraph Engine — entry point for running agent tasks via the state graph.

Provides the same interface as the legacy AgentLoop.run() method,
but delegates to a LangGraph compiled graph under the hood.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Optional

from agent.types import (
    AgentTask,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from agent.runtime.state import KestrelState, create_initial_state

logger = logging.getLogger("brain.agent.runtime.engine")

# Sentinel used to signal the event queue that streaming has ended
_QUEUE_DONE = object()


class LangGraphEngine:
    """
    Runs agent tasks through the LangGraph state graph.

    Drop-in replacement for the AgentLoop.run() async generator pattern.
    All existing Kestrel components are injected via the constructor and
    bound to graph nodes.
    """

    def __init__(
        self,
        provider,
        tool_registry,
        guardrails,
        persistence,
        model: str = "",
        api_key: str = "",
        learner=None,
        checkpoint_manager=None,
        memory_graph=None,
        evidence_chain=None,
        event_callback=None,
        reflection_engine=None,
        model_router=None,
        provider_resolver=None,
        approval_memory=None,
        simulator=None,
        persona_learner=None,
        verifier=None,
        kernel_policy_service=None,
        subsystem_bootstrapper=None,
    ):
        self._provider = provider
        self._tool_registry = tool_registry
        self._guardrails = guardrails
        self._persistence = persistence
        self._model = model
        self._api_key = api_key
        self._learner = learner
        self._checkpoint_manager = checkpoint_manager
        self._memory_graph = memory_graph
        self._evidence_chain = evidence_chain
        self._event_callback = event_callback
        self._reflection_engine = reflection_engine
        self._model_router = model_router
        self._provider_resolver = provider_resolver
        self._approval_memory = approval_memory
        self._simulator = simulator
        self._persona_learner = persona_learner
        self._verifier = verifier
        self._kernel_policy_service = kernel_policy_service
        self._subsystem_bootstrapper = subsystem_bootstrapper

        # Lazy-build these on first run
        self._graph = None
        self._planner = None
        self._executor = None
        self._step_scheduler = None
        self._council = None
        self._metrics = None

    def _ensure_components(self):
        """Initialize components that depend on constructor params."""
        if self._planner is not None:
            return

        from agent.core.planner import TaskPlanner
        from agent.core.executor import TaskExecutor
        from agent.step_scheduler import StepScheduler
        from agent.council import CouncilSession
        from agent.observability import MetricsCollector
        from agent.model_router import ModelRouter

        router = self._model_router or ModelRouter()
        self._metrics = MetricsCollector(model=self._model)
        self._planner = TaskPlanner(self._provider, self._model)
        self._council = CouncilSession(
            llm_provider=self._provider,
            model=self._model,
            event_callback=self._event_callback,
        )
        self._executor = TaskExecutor(
            provider=self._provider,
            tool_registry=self._tool_registry,
            guardrails=self._guardrails,
            persistence=self._persistence,
            metrics=self._metrics,
            model=self._model,
            api_key=self._api_key,
            model_router=router,
            provider_resolver=self._provider_resolver,
            event_callback=self._event_callback,
            evidence_chain=self._evidence_chain,
            approval_memory=self._approval_memory,
            verifier=self._verifier,
        )
        self._step_scheduler = StepScheduler(
            executor=self._executor,
            planner=self._planner,
            guardrails=self._guardrails,
            persistence=self._persistence,
            tool_registry=self._tool_registry,
            learner=self._learner,
            event_callback=self._event_callback,
        )

    async def _build_graph(self):
        """Build the LangGraph state graph with all dependencies bound."""
        self._ensure_components()

        from agent.runtime.agent_graph import build_agent_graph

        # NOTE: LangGraph's MemorySaver uses msgpack serialization, which
        # cannot handle complex Python objects like AgentTask, TaskPlan, etc.
        # that live in KestrelState.  Kestrel already persists task state to
        # PostgreSQL via TaskPersistence, so the LangGraph checkpointer is
        # redundant and disabled to avoid serialization errors.
        checkpointer = None

        self._graph = build_agent_graph(
            provider=self._provider,
            tool_registry=self._tool_registry,
            guardrails=self._guardrails,
            persistence=self._persistence,
            model=self._model,
            api_key=self._api_key,
            learner=self._learner,
            checkpoint_manager=self._checkpoint_manager,
            memory_graph=self._memory_graph,
            evidence_chain=self._evidence_chain,
            event_callback=self._event_callback,
            reflection_engine=self._reflection_engine,
            model_router=self._model_router,
            provider_resolver=self._provider_resolver,
            approval_memory=self._approval_memory,
            simulator=self._simulator,
            persona_learner=self._persona_learner,
            metrics=self._metrics,
            council=self._council,
            executor=self._executor,
            step_scheduler=self._step_scheduler,
            planner=self._planner,
            kernel_policy_service=self._kernel_policy_service,
            subsystem_bootstrapper=self._subsystem_bootstrapper,
            checkpointer=checkpointer,
        )

    async def run(self, task: AgentTask) -> AsyncIterator[TaskEvent]:
        """Execute a task through the LangGraph state graph.

        Yields TaskEvent objects, maintaining the same interface as
        the legacy AgentLoop.run() method.

        All node-level events (thinking, tool calls, simulation, approval, etc.)
        are bridged from the event_callback into this generator via an asyncio
        Queue, giving streaming parity with the legacy loop.
        """
        if self._graph is None:
            await self._build_graph()

        initial_state = create_initial_state(task)

        # ── Event bridge: callback → AsyncIterator ───────────────
        # Nodes fire events via event_callback(event_type, payload).
        # We bridge those into TaskEvent objects so callers get the same
        # rich stream as the legacy loop, without changing node interfaces.
        event_queue: asyncio.Queue[TaskEvent | object] = asyncio.Queue()

        _callback_to_event_type = {
            "plan_created": TaskEventType.PLAN_CREATED,
            "step_started": TaskEventType.STEP_STARTED,
            "thinking": TaskEventType.THINKING,
            "tool_called": TaskEventType.TOOL_CALLED,
            "tool_result": TaskEventType.TOOL_RESULT,
            "step_complete": TaskEventType.STEP_COMPLETE,
            "simulation_complete": TaskEventType.SIMULATION_COMPLETE,
            "approval_needed": TaskEventType.APPROVAL_NEEDED,
            "task_complete": TaskEventType.TASK_COMPLETE,
            "task_failed": TaskEventType.TASK_FAILED,
        }

        # Capture the original callback BEFORE defining the closure
        # to avoid infinite recursion when _bridging_callback is later
        # assigned to self._event_callback.
        _original_callback = self._event_callback

        async def _bridging_callback(event_type: str, payload: dict) -> None:
            """Forward event_callback calls as TaskEvents into the queue."""
            # Always call the original outer callback if one was configured
            if _original_callback:
                try:
                    await _original_callback(event_type, payload)
                except Exception as e:
                    logger.warning(f"Outer event_callback error: {e}")

            mapped_type = _callback_to_event_type.get(event_type)
            if mapped_type is None:
                return

            content = payload.get("content") or payload.get("result") or ""

            logger.info(
                f"_bridging_callback: event_type={event_type}, "
                f"mapped={mapped_type}, content_len={len(content) if isinstance(content, str) else '?'}"
            )
            await event_queue.put(TaskEvent(
                type=mapped_type,
                task_id=task.id,
                content=content,
                step_id=payload.get("step_id"),
                tool_name=payload.get("tool_name"),
                tool_args=payload.get("tool_args"),
                tool_result=payload.get("tool_result"),
                approval_id=payload.get("approval_id"),
                progress=self._progress(task),
                metadata=payload.get("metadata"),
                metrics=payload.get("metrics"),
            ))

        # Rebuild the graph with the bridging callback bound to nodes
        # (only if the caller didn't already inject an event_callback directly
        # into the constructor — in that case we still wrap it above)
        if self._event_callback is not _bridging_callback:
            # Temporarily swap in the bridge; restore on exit
            self._event_callback = _bridging_callback
            # Re-bind the callback on already-built components
            if self._council:
                self._council._event_callback = _bridging_callback
            if self._executor:
                self._executor._event_callback = _bridging_callback
            if self._step_scheduler:
                self._step_scheduler._event_callback = _bridging_callback

        try:
            # Wire multi-agent coordinator
            try:
                from agent.coordinator import Coordinator
                coordinator = Coordinator(
                    agent_loop=self,
                    persistence=self._persistence,
                    tool_registry=self._tool_registry,
                    event_callback=self._event_callback,
                )
                self._tool_registry._coordinator = coordinator
                self._tool_registry._current_task = task
            except Exception as e:
                logger.warning(f"Coordinator init skipped: {e}")

            config = {"configurable": {"thread_id": task.id}}

            async def _run_graph():
                """Run the graph and signal the queue when done."""
                try:
                    logger.info(f"LangGraph _run_graph starting for task {task.id}")
                    async for event in self._graph.astream(initial_state, config=config):
                        # LangGraph yields dicts keyed by node name
                        for node_name, node_output in event.items():
                            logger.info(f"LangGraph node '{node_name}' yielded keys={list(node_output.keys()) if isinstance(node_output, dict) else type(node_output).__name__}")
                            if not isinstance(node_output, dict):
                                continue

                            # Convert terminal node outputs to TaskEvents
                            status = node_output.get("status")
                            if status == TaskStatus.COMPLETE.value:
                                await event_queue.put(TaskEvent(
                                    type=TaskEventType.TASK_COMPLETE,
                                    task_id=task.id,
                                    content=task.result or "Task completed.",
                                    progress=self._progress(task),
                                ))
                            elif status == TaskStatus.FAILED.value:
                                await event_queue.put(TaskEvent(
                                    type=TaskEventType.TASK_FAILED,
                                    task_id=task.id,
                                    content=task.error or "Task failed.",
                                    progress=self._progress(task),
                                ))

                            # Emit plan_created when plan node produces a plan
                            plan = node_output.get("plan")
                            if plan and node_name == "plan":
                                import json
                                await event_queue.put(TaskEvent(
                                    type=TaskEventType.PLAN_CREATED,
                                    task_id=task.id,
                                    content=json.dumps(plan.to_dict()),
                                    progress=self._progress(task),
                                ))

                    logger.info(f"LangGraph _run_graph completed normally for task {task.id}")
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    logger.error(f"LangGraph engine error: {e}", exc_info=True)

                    task.status = TaskStatus.FAILED
                    task.error = str(e)
                    try:
                        await self._persistence.update_task(task)
                    except Exception:
                        pass

                    await event_queue.put(TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        content=f"Fatal Error: {e}\n\n```python\n{tb}\n```",
                        progress=self._progress(task),
                    ))
                finally:
                    await event_queue.put(_QUEUE_DONE)

            # Run graph concurrently, draining the event queue as events arrive
            graph_task = asyncio.create_task(_run_graph())

            while True:
                item = await event_queue.get()
                if item is _QUEUE_DONE:
                    logger.info("engine.run: event_queue DONE sentinel received")
                    break
                logger.info(
                    f"engine.run: yielding event type={item.type.value if hasattr(item, 'type') else '?'}, "
                    f"content_len={len(item.content) if hasattr(item, 'content') and isinstance(item.content, str) else '?'}"
                )
                yield item

            # Ensure the graph task is awaited so exceptions propagate
            await graph_task

        finally:
            # Restore original callback on all components
            self._event_callback = _original_callback
            if self._council:
                self._council._event_callback = _original_callback
            if self._executor:
                self._executor._event_callback = _original_callback
            if self._step_scheduler:
                self._step_scheduler._event_callback = _original_callback

    def _progress(self, task: AgentTask) -> dict:
        """Build progress snapshot (same as legacy loop)."""
        done, total = task.plan.progress if task.plan else (0, 0)
        return {
            "current_step": done,
            "total_steps": total,
            "iterations": task.iterations,
            "tokens_used": task.token_usage,
            "tool_calls": task.tool_calls_count,
        }
