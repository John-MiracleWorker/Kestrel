"""
Create Skill Tool — allows the agent to define new tools mid-execution.

This enables self-extending behavior where agents can write reusable
functions and register them for current and future task executions.
"""

from agent.types import RiskLevel, ToolDefinition


CREATE_SKILL_TOOL = ToolDefinition(
    name="create_skill",
    description=(
        "Create a new reusable tool (skill) by defining a Python function. "
        "The code must define a `run(args)` function that takes a dict of "
        "arguments and returns a result. The skill will be available for "
        "use in subsequent tool calls and persists across sessions.\n\n"
        "Example:\n"
        "```python\n"
        "def run(args):\n"
        "    # args['numbers'] is a list of numbers\n"
        "    return sum(args['numbers']) / len(args['numbers'])\n"
        "```\n\n"
        "Safety: Skills cannot import os, subprocess, sys, or make network calls."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique name for the skill (valid Python identifier, e.g. 'calculate_average')",
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of what the skill does",
            },
            "python_code": {
                "type": "string",
                "description": "Python code defining a `run(args)` function",
            },
            "parameters_schema": {
                "type": "object",
                "description": "JSON Schema describing the expected arguments",
            },
            "scope": {
                "type": "string",
                "enum": ["global", "workspace"],
                "description": "Where the skill should be available after approval.",
                "default": "global",
            },
        },
        "required": ["name", "description", "python_code"],
    },
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    timeout_seconds=30,
    category="skill",
    availability_requirements=("skill_manager",),
    use_cases=("create a new reusable tool", "persist a custom skill", "close a capability gap"),
)


async def execute_create_skill(args: dict, context: dict) -> dict:
    """Execute skill creation via the SkillManager."""
    skill_manager = context.get("skill_manager")
    current_task = context.get("current_task")

    if not skill_manager or not current_task:
        return {"success": False, "error": "Skill creation not available"}

    name = args.get("name", "")
    description = args.get("description", "")
    python_code = args.get("python_code", "")
    parameters = args.get("parameters_schema", {"type": "object", "properties": {}})
    scope = args.get("scope", "global")

    success, message = await skill_manager.create_skill(
        workspace_id=current_task.workspace_id if scope != "global" else None,
        name=name,
        description=description,
        python_code=python_code,
        parameters=parameters,
        created_by=current_task.user_id,
        scope=scope,
    )

    return {"success": success, "output" if success else "error": message}


def register_create_skill_tools(registry) -> None:
    async def create_skill_handler(
        name: str,
        description: str,
        python_code: str,
        parameters_schema: dict | None = None,
        scope: str = "global",
        skill_manager=None,
        current_task=None,
        **kwargs,
    ) -> dict:
        context = {
            "skill_manager": skill_manager,
            "current_task": current_task or getattr(registry, "_current_task", None),
        }
        return await execute_create_skill(
            {
                "name": name,
                "description": description,
                "python_code": python_code,
                "parameters_schema": parameters_schema or {"type": "object", "properties": {}},
                "scope": scope,
            },
            context,
        )

    registry.register(CREATE_SKILL_TOOL, create_skill_handler)
