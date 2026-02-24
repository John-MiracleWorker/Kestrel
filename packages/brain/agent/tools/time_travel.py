from __future__ import annotations
"""
Time-Travel tools — let the agent explore alternative execution paths
during active tasks.
"""

import logging
from agent.types import ToolDefinition, RiskLevel

logger = logging.getLogger("brain.agent.tools.time_travel")

_branch_manager = None
_checkpoint_manager = None
_current_task_id = None


TIME_TRAVEL_LIST_TOOL = ToolDefinition(
    name="list_checkpoints",
    description=(
        "Show all available restore points (checkpoints) for the current task. "
        "Use this to see where you can fork or roll back to."
    ),
    parameters={"type": "object", "properties": {}},
    risk_level=RiskLevel.LOW,
    timeout_seconds=10,
    category="control",
)

TIME_TRAVEL_FORK_TOOL = ToolDefinition(
    name="fork_from",
    description=(
        "Create a new execution branch from a past checkpoint and re-run "
        "with a different strategy. Use when the current approach isn't working "
        "and you want to try an alternative."
    ),
    parameters={
        "type": "object",
        "properties": {
            "checkpoint_id": {
                "type": "string",
                "description": "ID of the checkpoint to fork from",
            },
            "strategy": {
                "type": "string",
                "description": "What different approach to try on this branch",
            },
        },
        "required": ["checkpoint_id", "strategy"],
    },
    risk_level=RiskLevel.MEDIUM,
    timeout_seconds=15,
    category="control",
)

TIME_TRAVEL_COMPARE_TOOL = ToolDefinition(
    name="compare_branches",
    description=(
        "Compare two execution branches side-by-side — tools used, token cost, "
        "and outcomes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "branch_a": {"type": "string", "description": "First branch ID"},
            "branch_b": {"type": "string", "description": "Second branch ID"},
        },
        "required": ["branch_a", "branch_b"],
    },
    risk_level=RiskLevel.LOW,
    timeout_seconds=10,
    category="control",
)


async def handle_list_checkpoints() -> dict:
    if not _checkpoint_manager or not _current_task_id:
        return {"error": "Checkpoint manager not available"}
    checkpoints = await _checkpoint_manager.list_checkpoints(_current_task_id)
    return {
        "success": True,
        "checkpoints": [
            {"id": c.id, "step_index": c.step_index, "label": c.label, "created_at": c.created_at}
            for c in checkpoints
        ],
    }


async def handle_fork_from(checkpoint_id: str, strategy: str) -> dict:
    if not _branch_manager:
        return {"error": "Branch manager not available"}
    try:
        branch = await _branch_manager.create_branch(
            task_id=_current_task_id or "",
            checkpoint_id=checkpoint_id,
            strategy_hint=strategy,
        )
        return {"success": True, "branch": branch.to_dict()}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def handle_compare_branches(branch_a: str, branch_b: str) -> dict:
    if not _branch_manager:
        return {"error": "Branch manager not available"}
    try:
        comp = await _branch_manager.compare(branch_a, branch_b)
        return {"success": True, "comparison": comp.to_dict()}
    except Exception as e:
        return {"success": False, "error": str(e)}


def register_time_travel_tools(registry) -> None:
    registry.register(definition=TIME_TRAVEL_LIST_TOOL, handler=handle_list_checkpoints)
    registry.register(definition=TIME_TRAVEL_FORK_TOOL, handler=handle_fork_from)
    registry.register(definition=TIME_TRAVEL_COMPARE_TOOL, handler=handle_compare_branches)
    logger.info("Time-travel tools registered")
