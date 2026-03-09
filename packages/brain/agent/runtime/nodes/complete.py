"""
Complete node — final phase of the agent loop.

Builds the task result summary, persists final state, emits
completion events, and triggers post-task learning.

Wraps existing components:
  - TaskLearner.extract_lessons()
  - MetricsCollector
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.types import StepStatus, TaskEventType, TaskStatus
from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.complete")


async def complete_node(
    state: KestrelState,
    *,
    persistence=None,
    learner=None,
    metrics=None,
    evidence_chain=None,
    event_callback=None,
) -> dict[str, Any]:
    """Finalize the task: build result, persist, learn, emit events."""
    task = state["task"]
    plan = state.get("plan") or task.plan
    updates: dict[str, Any] = {}

    # ── Handle cancelled/denied tasks ────────────────────────────
    if state.get("approval_granted") is False:
        task.status = TaskStatus.FAILED
        task.error = task.error or "Task denied by user."
        if persistence:
            await persistence.update_task(task)
        updates["status"] = TaskStatus.FAILED.value
        return updates

    # ── Build final result summary ───────────────────────────────
    task.status = TaskStatus.COMPLETE
    task.completed_at = datetime.now(timezone.utc)

    if plan:
        results = []
        for s in plan.steps:
            if s.result:
                if task.messages:
                    results.append(s.result)
                else:
                    results.append(f"**{s.description}**: {s.result}")
        task.result = "\n".join(results) if results else "Task completed successfully."
    else:
        task.result = "Task completed successfully."

    if persistence:
        await persistence.update_task(task)

    # ── Emit completion events ───────────────────────────────────
    if event_callback:
        # Token usage metrics
        metrics_data = metrics.metrics.to_dict() if metrics else {}
        await event_callback("token_usage", {
            "total_tokens": task.token_usage,
            "iterations": task.iterations,
            "tool_calls": task.tool_calls_count,
            "estimated_cost_usd": metrics_data.get("estimated_cost_usd", 0),
            "llm_calls": metrics_data.get("llm_calls", 0),
            "avg_tool_time_ms": metrics_data.get("avg_tool_time_ms", 0),
            "total_elapsed_ms": metrics_data.get("total_elapsed_ms", 0),
        })

        # Evidence summary
        if evidence_chain and evidence_chain._decisions:
            await event_callback("evidence_summary", {
                "decision_count": len(evidence_chain._decisions),
                "decisions": [
                    {"type": d.decision_type.value, "description": d.description[:80]}
                    for d in evidence_chain._decisions[:5]
                ],
            })

    # ── Post-task learning ───────────────────────────────────────
    if learner:
        _is_trivial = (
            task.iterations <= 2
            and task.tool_calls_count < 5
            and task.status == TaskStatus.COMPLETE
            and (not plan or len(plan.steps) <= 1)
        )
        if not _is_trivial:
            try:
                await learner.extract_lessons(task)
            except Exception as e:
                logger.warning(f"Post-task learning failed: {e}")

    updates["status"] = TaskStatus.COMPLETE.value
    return updates
