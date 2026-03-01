"""
Delegate Task Tool — allows the agent to spawn specialist sub-agents.

This tool enables the coordinator pattern where a parent agent can
delegate subtasks to focused specialists with filtered tool access.

Includes:
- delegate_task: Run a single specialist sub-agent
- delegate_parallel: Run multiple specialists concurrently
"""

from agent.types import RiskLevel, ToolDefinition


DELEGATE_TOOL = ToolDefinition(
    name="delegate_task",
    description=(
        "Delegate a subtask to a specialist sub-agent. The sub-agent will "
        "execute the task independently and return its result. Available "
        "specialists: 'researcher' (web research), 'coder' (code and files), "
        "'analyst' (data analysis), 'reviewer' (read-only validation), "
        "'explorer' (host filesystem — uses host_tree, host_batch_read, host_find), "
        "'scanner' (deep codebase analysis — reads files and REASONS about them "
        "using LLM, produces structured findings with architecture analysis, "
        "patterns, issues, and implementation plans)."
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
                "enum": ["researcher", "coder", "analyst", "reviewer", "explorer", "scanner"],
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


DELEGATE_PARALLEL_TOOL = ToolDefinition(
    name="delegate_parallel",
    description=(
        "Run MULTIPLE specialist sub-agents IN PARALLEL. Each subtask runs "
        "concurrently and all results are returned together. Up to 5 parallel "
        "subtasks. Use this for tasks that can be split into independent parts "
        "(e.g. 'analyze auth module' + 'review database layer' + 'check API routes')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "What this sub-agent should accomplish",
                        },
                        "specialist": {
                            "type": "string",
                            "enum": ["researcher", "coder", "analyst", "reviewer", "explorer", "scanner"],
                            "description": "Type of specialist",
                        },
                    },
                    "required": ["goal", "specialist"],
                },
                "description": "List of subtasks to run in parallel (max 5)",
            },
        },
        "required": ["subtasks"],
    },
    risk_level=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=600,
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


async def execute_delegate_parallel(
    args: dict,
    context: dict,
) -> dict:
    """
    Execute parallel delegation of multiple specialist sub-agents.

    Context must contain 'coordinator' and 'current_task' keys.
    """
    coordinator = context.get("coordinator")
    current_task = context.get("current_task")

    if not coordinator or not current_task:
        return {
            "success": False,
            "error": "Delegation not available — coordinator not initialized",
        }

    subtasks = args.get("subtasks", [])

    if not subtasks:
        return {"success": False, "error": "At least one subtask is required"}

    if not hasattr(coordinator, 'delegate_parallel'):
        return {"success": False, "error": "Parallel delegation not supported by this coordinator"}

    try:
        results = await coordinator.delegate_parallel(
            parent_task=current_task,
            subtasks=subtasks,
        )
        return {
            "success": True,
            "results": results,
            "count": len(results),
        }
    except Exception as e:
        return {"success": False, "error": f"Parallel delegation failed: {e}"}
