"""
Execute node — Phase 2 of the agent loop.

Dispatches tool calls via the StepScheduler (DAG-aware parallel execution).
Collects step results and tracks iteration/token budgets.

Wraps existing components:
  - StepScheduler.execute_plan()
  - TaskExecutor (via scheduler)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent.types import (
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.execute")


async def execute_node(
    state: KestrelState,
    *,
    step_scheduler=None,
    persistence=None,
    event_callback=None,
    start_time: float | None = None,
) -> dict[str, Any]:
    """Execute the current plan steps via the step scheduler.

    The step scheduler handles:
    - DAG dependency resolution
    - Parallel step execution for independent steps
    - Tool dispatch, approval gates, and retry logic
    - Budget enforcement (iterations, tokens, wall-clock time)

    Returns partial state update with execution results.
    """
    task = state["task"]
    plan = state.get("plan") or task.plan
    kernel_policy = state.get("kernel_policy", {})
    updates: dict[str, Any] = {}

    if plan is None:
        logger.error("Execute node called with no plan")
        updates["status"] = TaskStatus.FAILED.value
        updates["route"] = "done"
        return updates

    # Ensure task has the latest plan and runtime properties
    task.plan = plan
    task.status = TaskStatus.EXECUTING
    state_msgs = state.get("messages", [])
    logger.info(
        f"execute_node: task.messages={len(task.messages)} items, "
        f"state['messages']={len(state_msgs)} items, "
        f"step_desc={task.plan.steps[0].description[:80] if task.plan and task.plan.steps else 'N/A'}"
    )
    if state_msgs and not task.messages:
        task.messages = state_msgs
        logger.info(f"execute_node: restored {len(state_msgs)} messages from state")

    if persistence:
        await persistence.update_task(task)

    _start = start_time or time.monotonic()
    collected_events: list[TaskEvent] = []
    task_failed = False

    # The step scheduler is an async generator that yields TaskEvents.
    # We collect them and forward via event_callback.
    if step_scheduler:
        try:
            from agent.model_router import RoutingStrategy

            executor = getattr(step_scheduler, "_executor", None)
            router = getattr(executor, "_model_router", None) if executor else None
            strategy_name = kernel_policy.get("routing_strategy", "")
            if router and strategy_name:
                router.set_strategy(RoutingStrategy(strategy_name))
        except Exception:
            pass

        def _should_replan(t):
            """Drift-based replanning check."""
            if not t.plan or t.plan.revision_count >= 3:
                return False
            from agent.types import StepStatus
            recent = [s for s in t.plan.steps if s.status in (StepStatus.COMPLETE, StepStatus.FAILED)]
            failures = 0
            for s in reversed(recent[-3:]):
                if s.status == StepStatus.FAILED:
                    failures += 1
                else:
                    break
            if failures >= 2:
                return True
            done_count = sum(1 for s in t.plan.steps if s.status == StepStatus.COMPLETE)
            total_count = len(t.plan.steps)
            if done_count > 0 and total_count > 0:
                projected = t.iterations * (total_count / done_count)
                if projected > t.config.max_iterations * 0.8:
                    return True
            return t.iterations % 8 == 0

        def _progress(t):
            done, total = t.plan.progress if t.plan else (0, 0)
            return {
                "current_step": done,
                "total_steps": total,
                "iterations": t.iterations,
                "tokens_used": t.token_usage,
                "tool_calls": t.tool_calls_count,
            }

        # Use the dynamically-updated callback from step_scheduler if available,
        # because the event_callback captured by functools.partial at graph build
        # time may be stale (it points to _activity_callback which filters out
        # step_complete events).  engine.run() later rebinds step_scheduler's
        # callback to _bridging_callback which correctly bridges events into the
        # event_queue for gRPC streaming.
        _live_callback = (
            getattr(step_scheduler, '_event_callback', None) or event_callback
        )

        async for event in step_scheduler.execute_plan(
            task=task,
            start_time=_start,
            should_replan_fn=_should_replan,
            progress_fn=_progress,
        ):
            collected_events.append(event)
            if _live_callback:
                await _live_callback(event.type.value, event.to_dict())
            if event.type == TaskEventType.TASK_FAILED:
                task_failed = True
                break

    # ── Determine next route ─────────────────────────────────────
    if task_failed:
        updates["status"] = TaskStatus.FAILED.value
        updates["route"] = "done"
    elif task.plan and task.plan.is_complete:
        updates["route"] = "needs_reflection"
    else:
        # Still steps to execute (unlikely if scheduler ran to completion)
        updates["route"] = "needs_reflection"

    updates["step_results"] = [e for e in collected_events if e.type == TaskEventType.TOOL_RESULT]
    updates["iteration"] = task.iterations

    return updates
