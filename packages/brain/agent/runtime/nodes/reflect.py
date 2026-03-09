"""
Reflect node — Phase 3 of the agent loop.

Evaluates execution results, checks evidence bindings, and decides
whether to continue execution, replan, or complete.

Wraps existing components:
  - EvidenceChain verification

Note: Memory graph entity extraction and persona observation have been
intentionally moved to complete_node, which runs after task.result is
assembled. This guarantees those side-effects always see the real final
answer rather than a potentially empty task.result.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.types import TaskStatus
from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.reflect")


async def reflect_node(
    state: KestrelState,
    *,
    evidence_chain=None,
    provider=None,
    model: str = "",
    api_key: str = "",
    event_callback=None,
) -> dict[str, Any]:
    """Reflect on execution results and decide next action.

    Routes:
      - "done": task is complete, proceed to complete_node
      - "continue": more steps to execute, go back to execute_node
      - "replan": plan needs revision, go back to plan_node
    """
    task = state["task"]
    plan = state.get("plan") or task.plan
    updates: dict[str, Any] = {}

    # ── Check plan completion ────────────────────────────────────
    if plan and plan.is_complete:
        # All steps finished — proceed to completion
        updates["route"] = "done"
        updates["status"] = TaskStatus.REFLECTING.value

        # ── Persist evidence chain ───────────────────────────────
        if evidence_chain:
            try:
                await evidence_chain.persist()
            except Exception as e:
                logger.warning(f"Evidence chain persistence failed: {e}")

        # NOTE: Memory graph entity extraction and persona observation are
        # handled in complete_node, after task.result is fully assembled.

    elif plan and not plan.is_complete:
        # Check if we should replan based on drift
        from agent.types import StepStatus
        recent = [s for s in plan.steps if s.status in (StepStatus.COMPLETE, StepStatus.FAILED)]
        consecutive_failures = 0
        for s in reversed(recent[-3:]):
            if s.status == StepStatus.FAILED:
                consecutive_failures += 1
            else:
                break

        if consecutive_failures >= 2 and plan.revision_count < 3:
            updates["route"] = "replan"
            plan.revision_count += 1
        else:
            updates["route"] = "continue"
    else:
        updates["route"] = "done"

    return updates
