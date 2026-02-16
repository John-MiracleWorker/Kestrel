"""
Delegate Task Tool — allows the agent to spawn specialist sub-agents.

This tool enables the coordinator pattern where a parent agent can
delegate subtasks to focused specialists with filtered tool access.
"""

from agent.types import RiskLevel, ToolDefinition


DELEGATE_TOOL = ToolDefinition(
    name="delegate_task",
    description=(
        "Delegate a subtask to a specialist sub-agent. The sub-agent will "
        "execute the task independently and return its result. Available "
        "specialists: 'researcher' (web research), 'coder' (code and files), "
        "'analyst' (data analysis), 'reviewer' (read-only validation)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "Clear description of what the sub-agent should accomplish",
            },
            "specialist": {
                "type": "string",
                "enum": ["researcher", "coder", "analyst", "reviewer"],
                "description": "Type of specialist to delegate to",
            },
        },
        "required": ["goal", "specialist"],
    },
    risk_level=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=300,
    category="delegation",
)


async def execute_delegate(
    args: dict,
    context: dict,
) -> dict:
    """
    Execute delegation to a specialist sub-agent.

    Context must contain 'coordinator' and 'current_task' keys.
    """
    coordinator = context.get("coordinator")
    current_task = context.get("current_task")

    if not coordinator or not current_task:
        return {
            "success": False,
            "error": "Delegation not available — coordinator not initialized",
        }

    goal = args.get("goal", "")
    specialist = args.get("specialist", "researcher")

    if not goal:
        return {"success": False, "error": "Goal is required"}

    try:
        result = await coordinator.delegate(
            parent_task=current_task,
            goal=goal,
            specialist_type=specialist,
        )
        return {"success": True, "output": result}
    except Exception as e:
        return {"success": False, "error": f"Delegation failed: {e}"}
