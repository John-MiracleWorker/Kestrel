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
        },
        "required": ["name", "description", "python_code"],
    },
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    timeout_seconds=30,
    category="skill",
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

    success, message = await skill_manager.create_skill(
        workspace_id=current_task.workspace_id,
        name=name,
        description=description,
        python_code=python_code,
        parameters=parameters,
        created_by=current_task.user_id,
    )

    return {"success": success, "output" if success else "error": message}
