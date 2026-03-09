"""
Approve node — human-in-the-loop approval gate.

Uses LangGraph's interrupt() to pause the graph and wait for
human approval. Bridges to Kestrel's existing approval system.

When USE_LANGGRAPH_INTERRUPT=true, uses native LangGraph interrupt().
Otherwise, falls back to Kestrel's polling-based approval mechanism.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from agent.types import ApprovalStatus, TaskStatus
from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.approve")


async def approve_node(
    state: KestrelState,
    *,
    guardrails=None,
    executor=None,
    persistence=None,
    event_callback=None,
) -> dict[str, Any]:
    """Pause execution and wait for human approval.

    Uses LangGraph interrupt() when available, otherwise falls back
    to Kestrel's existing approval polling mechanism.
    """
    task = state["task"]
    plan = state.get("plan") or task.plan
    council_verdict = state.get("council_verdict", {})
    updates: dict[str, Any] = {}

    use_interrupt = os.getenv("USE_LANGGRAPH_INTERRUPT", "true").lower() == "true"

    if use_interrupt:
        # ── LangGraph native interrupt ───────────────────────────
        try:
            from langgraph.types import interrupt

            approval_data = interrupt({
                "type": "plan_approval",
                "task_id": task.id,
                "plan": plan.to_dict() if plan else {},
                "council_verdict": council_verdict,
                "risk_level": task.config.auto_approve_risk.value,
            })

            if approval_data.get("approved", False):
                updates["approval_granted"] = True
                updates["status"] = TaskStatus.EXECUTING.value
            else:
                updates["approval_granted"] = False
                updates["status"] = TaskStatus.CANCELLED.value
                task.error = "User denied plan after review."

        except ImportError:
            logger.warning("LangGraph interrupt not available, falling back to polling")
            use_interrupt = False

    if not use_interrupt:
        # ── Fallback: Kestrel's polling-based approval ───────────
        if event_callback:
            reason = council_verdict.get("review_reason", "Plan requires approval")
            concerns = council_verdict.get("concerns", [])
            await event_callback("approval_needed", {
                "task_id": task.id,
                "reason": reason,
                "concerns": concerns,
            })

        task.status = TaskStatus.WAITING_APPROVAL
        if persistence:
            await persistence.update_task(task)

        if executor:
            approved = await executor._wait_for_approval(task)
        else:
            approved = True  # Fail-open if no executor

        if approved:
            updates["approval_granted"] = True
            updates["status"] = TaskStatus.EXECUTING.value
        else:
            updates["approval_granted"] = False
            updates["status"] = TaskStatus.FAILED.value
            task.error = "User denied plan after review."

        if persistence:
            await persistence.update_task(task)

    return updates
