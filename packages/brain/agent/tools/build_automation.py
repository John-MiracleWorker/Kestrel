from __future__ import annotations
"""
Build Automation tool — lets Kestrel create persistent cron automations
from natural language descriptions during chat.
"""

import logging
from agent.types import ToolDefinition, RiskLevel

logger = logging.getLogger("brain.agent.tools.build_automation")

BUILD_AUTOMATION_TOOL = ToolDefinition(
    name="build_automation",
    description=(
        "Create a persistent recurring automation from a natural language description. "
        "Converts descriptions like 'Every weekday at 8am check my GitHub repos' "
        "into a scheduled cron job that runs automatically. "
        "Returns a preview of the automation before saving."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": (
                    "Natural language description of the recurring task. "
                    "Include schedule (daily, weekly, every N hours) and what to do."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": "Set to true to save the automation. Omit or false for preview only.",
                "default": False,
            },
        },
        "required": ["description"],
    },
    risk_level=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=30,
    category="automation",
    availability_requirements=("automation_builder", "cron_scheduler"),
)


async def handle_build_automation(
    description: str,
    confirm: bool = False,
    execution_context=None,
    automation_builder=None,
    cron_scheduler=None,
    workspace_id: str = "",
    user_id: str = "",
) -> dict:
    """Handle the build_automation tool call."""
    builder = automation_builder
    scheduler = cron_scheduler
    ws_id = workspace_id or getattr(execution_context, "workspace_id", None)
    actor_id = user_id or getattr(execution_context, "user_id", None)

    if not builder:
        return {"error": "Automation builder not initialized"}

    try:
        # Parse the NL description into an AutomationSpec
        spec = await builder.build(description)

        # Validate
        issues = await builder.validate(spec)
        if issues:
            return {
                "success": False,
                "error": "Validation failed",
                "issues": issues,
                "parsed": spec.to_dict(),
            }

        # Preview
        preview = await builder.preview(spec)

        if not confirm:
            return {
                "success": True,
                "mode": "preview",
                "preview": preview,
                "message": (
                    "Here's what this automation will do. "
                    "Call build_automation again with confirm=true to save it."
                ),
            }

        # Save to cron scheduler
        if not scheduler:
            return {"error": "Cron scheduler not available"}

        job = await scheduler.create_job(
            workspace_id=ws_id or "default",
            user_id=actor_id or "system",
            name=spec.name,
            description=spec.description,
            cron_expression=spec.cron_expression,
            goal=spec.goal,
        )

        return {
            "success": True,
            "mode": "saved",
            "job": job.to_dict() if hasattr(job, "to_dict") else {"id": str(job)},
            "message": f"Automation '{spec.name}' created and will run on schedule: {spec.cron_expression}",
        }

    except Exception as e:
        logger.error(f"build_automation failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def register_build_automation_tools(registry) -> None:
    """Register the build_automation tool."""
    registry.register(
        definition=BUILD_AUTOMATION_TOOL,
        handler=handle_build_automation,
    )
    logger.info("Build automation tool registered")
