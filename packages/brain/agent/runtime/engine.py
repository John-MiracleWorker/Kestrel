"""
LangGraph Engine — entry point for running agent tasks via the state graph.

Provides the same interface as the legacy AgentLoop.run() method,
but delegates to a LangGraph compiled graph under the hood.
"""

from __future__ import annotations

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

    def _build_graph(self):
        """Build the LangGraph state graph with all dependencies bound."""
        self._ensure_components()

        from agent.runtime.agent_graph import build_agent_graph
        from agent.runtime.checkpointer import PostgresCheckpointer

        checkpointer = None
        if self._checkpoint_manager:
            checkpointer = PostgresCheckpointer(self._checkpoint_manager)

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
            checkpointer=checkpointer,
        )

    async def run(self, task: AgentTask) -> AsyncIterator[TaskEvent]:
        """Execute a task through the LangGraph state graph.

        Yields TaskEvent objects, maintaining the same interface as
        the legacy AgentLoop.run() method.
        """
        if self._graph is None:
            self._build_graph()

        initial_state = create_initial_state(task)
        start_time = time.monotonic()

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

            async for event in self._graph.astream(initial_state, config=config):
                # LangGraph yields dicts keyed by node name
                for node_name, node_output in event.items():
                    if not isinstance(node_output, dict):
                        continue

                    # Convert node outputs to TaskEvents for backward compatibility
                    status = node_output.get("status")
                    if status == TaskStatus.COMPLETE.value:
                        yield TaskEvent(
                            type=TaskEventType.TASK_COMPLETE,
                            task_id=task.id,
                            content=task.result or "Task completed.",
                            progress=self._progress(task),
                        )
                    elif status == TaskStatus.FAILED.value:
                        yield TaskEvent(
                            type=TaskEventType.TASK_FAILED,
                            task_id=task.id,
                            content=task.error or "Task failed.",
                            progress=self._progress(task),
                        )

                    # Emit plan_created when plan node produces a plan
                    plan = node_output.get("plan")
                    if plan and node_name == "plan":
                        import json
                        yield TaskEvent(
                            type=TaskEventType.PLAN_CREATED,
                            task_id=task.id,
                            content=json.dumps(plan.to_dict()),
                            progress=self._progress(task),
                        )

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

            yield TaskEvent(
                type=TaskEventType.TASK_FAILED,
                task_id=task.id,
                content=f"Fatal Error: {e}\n\n```python\n{tb}\n```",
                progress=self._progress(task),
            )

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
