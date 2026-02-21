"""
Schedule tool — lets Kestrel create, list, and delete scheduled tasks.

Wraps the CronScheduler from agent.automation, which is already running
as a background loop evaluating cron expressions every 60 seconds.
"""

import logging
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.schedule")

# Set by server.py before tool execution
_cron_scheduler = None
_current_workspace_id: Optional[str] = None
_current_user_id: Optional[str] = None


def register_schedule_tools(registry) -> None:
    """Register the schedule tool."""

    registry.register(
        definition=ToolDefinition(
            name="schedule",
            description=(
                "Create, list, or delete scheduled tasks. Kestrel can schedule "
                "recurring or one-time tasks using cron expressions. "
                "Use action='create' to schedule a new task, 'list' to see active jobs, "
                "or 'delete' to remove one. "
                "Cron format: 'minute hour day month weekday' (e.g. '0 17 * * *' = daily at 5pm, "
                "'30 9 * * 1' = Mondays at 9:30am, '0 */6 * * *' = every 6 hours)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "delete"],
                        "description": "The scheduling action to perform",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name for the scheduled task (for create)",
                    },
                    "description": {
                        "type": "string",
                        "description": "What this task does (for create)",
                    },
                    "cron": {
                        "type": "string",
                        "description": (
                            "Cron expression: 'min hour day month weekday'. "
                            "Examples: '0 17 * * *' (daily 5pm), '*/30 * * * *' (every 30min), "
                            "'0 9 * * 1-5' (weekdays 9am)"
                        ),
                    },
                    "goal": {
                        "type": "string",
                        "description": "The task goal — what Kestrel should do when triggered (for create)",
                    },
                    "max_runs": {
                        "type": "integer",
                        "description": "Maximum number of times to run (omit for unlimited, use 1 for one-shot)",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Job ID to delete (for action='delete')",
                    },
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=10,
            category="automation",
        ),
        handler=schedule_action,
    )


async def schedule_action(
    action: str,
    name: str = "",
    description: str = "",
    cron: str = "",
    goal: str = "",
    max_runs: int = None,
    job_id: str = "",
) -> dict:
    """Route to the appropriate schedule action."""
    if not _cron_scheduler:
        return {
            "error": "Scheduler not available",
            "hint": "The cron scheduler hasn't been initialized yet.",
        }

    if action == "create":
        return await _create_job(name, description, cron, goal, max_runs)
    elif action == "list":
        return await _list_jobs()
    elif action == "delete":
        return await _delete_job(job_id)
    else:
        return {"error": f"Unknown action: {action}. Use 'create', 'list', or 'delete'."}


async def _create_job(name: str, description: str, cron: str, goal: str, max_runs: int = None) -> dict:
    """Create a new scheduled task."""
    if not name:
        return {"error": "A name is required for the scheduled task."}
    if not cron:
        return {"error": "A cron expression is required (e.g. '0 17 * * *' for daily at 5pm)."}
    if not goal:
        return {"error": "A goal is required — what should I do when this task triggers?"}

    # Validate cron expression has 5 fields
    fields = cron.strip().split()
    if len(fields) != 5:
        return {
            "error": f"Invalid cron expression '{cron}'. Must have 5 fields: minute hour day month weekday.",
            "examples": {
                "daily_5pm": "0 17 * * *",
                "every_30min": "*/30 * * * *",
                "weekdays_9am": "0 9 * * 1-5",
                "once_per_hour": "0 * * * *",
            },
        }

    workspace_id = _current_workspace_id
    user_id = _current_user_id

    if not workspace_id or not user_id:
        return {"error": "Cannot determine workspace/user context for scheduling."}

    try:
        job = await _cron_scheduler.create_job(
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            description=description or name,
            cron_expression=cron,
            goal=goal,
            max_runs=max_runs,
        )
        result = {
            "status": "created",
            "job_id": job.id,
            "name": name,
            "cron": cron,
            "goal": goal,
            "message": f"✅ Scheduled '{name}' with cron '{cron}'",
        }
        if max_runs:
            result["max_runs"] = max_runs
            if max_runs == 1:
                result["message"] += " (one-shot — will run once then auto-delete)"
        return result
    except Exception as e:
        logger.error(f"Failed to create scheduled task: {e}")
        return {"error": f"Failed to create scheduled task: {e}"}


async def _list_jobs() -> dict:
    """List all scheduled tasks for the current workspace."""
    workspace_id = _current_workspace_id
    if not workspace_id:
        return {"error": "Cannot determine workspace context."}

    try:
        jobs = await _cron_scheduler.list_jobs(workspace_id)
        if not jobs:
            return {
                "status": "empty",
                "message": "No scheduled tasks. Use action='create' to set one up.",
                "jobs": [],
            }
        return {
            "status": "ok",
            "count": len(jobs),
            "jobs": jobs,
        }
    except Exception as e:
        logger.error(f"Failed to list scheduled tasks: {e}")
        return {"error": f"Failed to list scheduled tasks: {e}"}


async def _delete_job(job_id: str) -> dict:
    """Delete a scheduled task."""
    if not job_id:
        return {"error": "A job_id is required. Use action='list' to see active jobs."}

    try:
        deleted = await _cron_scheduler.delete_job(job_id)
        if deleted:
            return {
                "status": "deleted",
                "job_id": job_id,
                "message": f"🗑️ Deleted scheduled task {job_id}",
            }
        else:
            return {"error": f"Job {job_id} not found or couldn't be deleted."}
    except Exception as e:
        logger.error(f"Failed to delete scheduled task: {e}")
        return {"error": f"Failed to delete scheduled task: {e}"}
