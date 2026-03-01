"""
Delegate Task Tool — allows the agent to spawn specialist sub-agents.

This tool enables the coordinator pattern where a parent agent can
delegate subtasks to focused specialists with filtered tool access.

Includes:
- delegate_task: Run a single specialist sub-agent
- delegate_parallel: Run multiple specialists concurrently
- create_specialist: Define a new specialist type at runtime
- list_specialists: List all available specialists (built-in + dynamic)
"""

from agent.types import RiskLevel, ToolDefinition


DELEGATE_TOOL = ToolDefinition(
    name="delegate_task",
    description=(
        "Delegate a subtask to a specialist sub-agent. The sub-agent will "
        "execute the task independently and return its result. Built-in "
        "specialists: 'researcher' (web research), 'coder' (code and files), "
        "'analyst' (data analysis), 'reviewer' (read-only validation), "
        "'explorer' (host filesystem — uses host_tree, host_batch_read, host_find), "
        "'scanner' (deep codebase analysis — reads files and REASONS about them "
        "using LLM, produces structured findings with architecture analysis, "
        "patterns, issues, and implementation plans). "
        "You can also use dynamically created specialist types — use "
        "'list_specialists' to see all available types, or 'create_specialist' "
        "to define a new one."
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
                "description": (
                    "Type of specialist to delegate to. Built-in types: "
                    "researcher, coder, analyst, reviewer, explorer, scanner. "
                    "Custom types created via create_specialist are also accepted."
                ),
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
        "(e.g. 'analyze auth module' + 'review database layer' + 'check API routes'). "
        "Supports both built-in and dynamically created specialist types."
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
                            "description": (
                                "Type of specialist. Built-in: researcher, coder, "
                                "analyst, reviewer, explorer, scanner. "
                                "Custom types created via create_specialist also work."
                            ),
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


CREATE_SPECIALIST_TOOL = ToolDefinition(
    name="create_specialist",
    description=(
        "Create a NEW specialist agent type at runtime. This lets you define "
        "a custom sub-agent with a specific persona and tool set, tailored to "
        "the current task. The new specialist can then be used with delegate_task "
        "or delegate_parallel. Use this when the built-in specialists don't fit "
        "your needs — for example, creating a 'security_auditor' with specific "
        "security-focused tools and persona, or a 'doc_writer' with file and "
        "memory tools."
    ),
    parameters={
        "type": "object",
        "properties": {
            "type_key": {
                "type": "string",
                "description": (
                    "Unique identifier for this specialist type (lowercase, "
                    "alphanumeric + underscores). Examples: 'security_auditor', "
                    "'doc_writer', 'api_tester'. Cannot match built-in types."
                ),
            },
            "name": {
                "type": "string",
                "description": "Human-readable display name (e.g. 'Security Auditor')",
            },
            "persona": {
                "type": "string",
                "description": (
                    "Detailed system prompt defining the specialist's role, "
                    "expertise, and approach. Be specific about what the specialist "
                    "should focus on and how it should report results."
                ),
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Primary tools this specialist can use. Must be valid tool names "
                    "from the registry. 'task_complete' is added automatically."
                ),
            },
            "adjacent_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional auxiliary tools available as fallback when primary "
                    "tools are insufficient."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "description": "Max reasoning iterations (1-40, default 15)",
                "default": 15,
            },
            "max_tool_calls": {
                "type": "integer",
                "description": "Max tool calls allowed (1-80, default 30)",
                "default": 30,
            },
            "complexity_weight": {
                "type": "number",
                "description": (
                    "Relative token budget weight (0.3-3.0, default 1.0). "
                    "Higher = more tokens allocated. Coder-like tasks ~1.5, "
                    "read-only tasks ~0.6."
                ),
                "default": 1.0,
            },
        },
        "required": ["type_key", "name", "persona", "allowed_tools"],
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="delegation",
)


LIST_SPECIALISTS_TOOL = ToolDefinition(
    name="list_specialists",
    description=(
        "List all available specialist agent types, including both built-in "
        "and dynamically created ones. Shows each specialist's name, persona, "
        "available tools, and whether it's built-in or custom. Use this to "
        "see what specialists are available before delegating tasks."
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=5,
    category="delegation",
)


REMOVE_SPECIALIST_TOOL = ToolDefinition(
    name="remove_specialist",
    description=(
        "Remove a dynamically created specialist type. Cannot remove built-in "
        "specialists. Use this to clean up custom specialists that are no longer needed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "type_key": {
                "type": "string",
                "description": "The type key of the dynamic specialist to remove",
            },
        },
        "required": ["type_key"],
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=5,
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
