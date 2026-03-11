"""
Council node — multi-agent deliberation on complex plans.

Invokes the Council system for plans that exceed the complexity
threshold. The council reviews the plan, debates, and issues a verdict.

Wraps existing components:
  - CouncilSession.deliberate()
  - CouncilSession.deliberate_lite()
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.council")


def _council_debate_enabled() -> bool:
    return os.getenv("COUNCIL_INCLUDE_DEBATE", "false").lower() == "true"


async def council_node(
    state: KestrelState,
    *,
    council=None,
    tool_registry=None,
    event_callback=None,
) -> dict[str, Any]:
    """Run council deliberation on the current plan.

    Sets council_verdict and needs_council in state.
    The approve node will check the verdict to decide if human approval is needed.
    """
    task = state["task"]
    plan = state.get("plan") or task.plan
    plan_complexity = state.get("plan_complexity", 5.0)
    kernel_policy = state.get("kernel_policy", {})
    updates: dict[str, Any] = {}

    if not council or not plan:
        updates["needs_council"] = False
        return updates

    # ── Check if council can be skipped ──────────────────────────
    if not kernel_policy.get("use_council", True):
        updates["needs_council"] = False
        updates["approval_granted"] = True
        return updates

    if plan_complexity < float(kernel_policy.get("council_threshold", 7.0)):
        updates["needs_council"] = False
        return updates

    # Safe-plan bypass: skip council if no HIGH-risk tools
    if tool_registry and plan.steps:
        from agent.types import RiskLevel
        has_high_risk = False
        for step in plan.steps:
            for tc in step.tool_calls:
                tool_name = tc.get("tool", tc.get("function", {}).get("name", ""))
                if tool_name:
                    risk = tool_registry.get_risk_level(tool_name)
                    if risk == RiskLevel.HIGH:
                        has_high_risk = True
                        break
            desc_lower = step.description.lower()
            if any(kw in desc_lower for kw in (
                "delete", "deploy", "credential", "secret", "admin",
                "sudo", "production", "database migration",
            )):
                has_high_risk = True
                break

        if not has_high_risk and plan_complexity < 8.5:
            logger.debug(
                f"Council skip: no HIGH-risk tools in {len(plan.steps)} steps "
                f"(complexity={plan_complexity:.1f})"
            )
            updates["needs_council"] = False
            return updates

    # ── Run council ──────────────────────────────────────────────
    try:
        plan_text = json.dumps(plan.to_dict())
        if plan_complexity <= 9.0:
            verdict = await council.deliberate_lite(
                proposal=plan_text,
                context=task.goal,
                top_n=3,
            )
        else:
            verdict = await council.deliberate(
                proposal=plan_text,
                context=task.goal,
                include_debate=_council_debate_enabled(),
            )

        updates["council_verdict"] = {
            "requires_user_review": verdict.requires_user_review,
            "review_reason": getattr(verdict, "review_reason", ""),
            "concerns": getattr(verdict, "synthesized_concerns", []),
            "consensus": getattr(verdict, "consensus", None),
        }

        if verdict.requires_user_review:
            from agent.council import VoteType
            is_hard_reject = (verdict.consensus == VoteType.REJECT)
            updates["approval_granted"] = None  # Needs human decision
            if is_hard_reject:
                updates["route"] = "needs_approval"
            else:
                # Concerns but not rejected — log warning, proceed
                if event_callback:
                    await event_callback("council_warning", {
                        "reason": verdict.review_reason,
                        "concerns": verdict.synthesized_concerns[:3],
                    })
                updates["approval_granted"] = True
        else:
            updates["approval_granted"] = True

    except Exception as e:
        logger.warning(f"Council deliberation failed: {e}")
        updates["approval_granted"] = True  # Fail-open

    return updates
