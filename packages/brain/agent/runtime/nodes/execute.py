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
from typing import Any, AsyncIterator

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
    updates: dict[str, Any] = {}

    if plan is None:
        logger.error("Execute node called with no plan")
        updates["status"] = TaskStatus.FAILED.value
        updates["route"] = "done"
        return updates

    # Ensure task has the latest plan
    task.plan = plan
    task.status = TaskStatus.EXECUTING

    if persistence:
        await persistence.update_task(task)

    _start = start_time or time.monotonic()
    collected_events: list[TaskEvent] = []
    task_failed = False

    # The step scheduler is an async generator that yields TaskEvents.
    # We collect them and forward via event_callback.
    if step_scheduler:
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

        async for event in step_scheduler.execute_plan(
            task=task,
            start_time=_start,
            should_replan_fn=_should_replan,
            progress_fn=_progress,
        ):
            collected_events.append(event)
            if event_callback:
                await event_callback(event.type.value, event.to_dict())
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
