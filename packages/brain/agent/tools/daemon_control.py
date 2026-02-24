from __future__ import annotations
"""
Daemon control tools — let Kestrel create, list, pause, and stop
background daemon agents from chat.
"""

import logging
from agent.types import ToolDefinition, RiskLevel

logger = logging.getLogger("brain.agent.tools.daemon_control")

# Module-level ref set during initialization
_daemon_manager = None
_current_workspace_id = None
_current_user_id = None


DAEMON_CREATE_TOOL = ToolDefinition(
    name="daemon_create",
    description=(
        "Create and start a background daemon agent that continuously monitors "
        "a target and proactively alerts you when something actionable is detected. "
        "Examples: watch a GitHub repo for new issues, monitor CI pipelines, "
        "track system resources."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short name for the daemon (e.g. 'ci-watcher')",
            },
            "description": {
                "type": "string",
                "description": "What the daemon should monitor and why",
            },
            "daemon_type": {
                "type": "string",
                "enum": ["repo_watcher", "ci_monitor", "inbox_monitor",
                         "data_monitor", "system_monitor", "custom"],
                "description": "Type of monitoring to perform",
                "default": "custom",
            },
            "watch_target": {
                "type": "string",
                "description": "What to watch (repo URL, file path, API endpoint, etc.)",
            },
            "poll_interval_minutes": {
                "type": "integer",
                "description": "How often to check (in minutes, default: 5)",
                "default": 5,
            },
            "sensitivity": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "How aggressively to surface findings",
                "default": "medium",
            },
        },
        "required": ["name", "description"],
    },
    risk_level=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=15,
    category="automation",
)

DAEMON_LIST_TOOL = ToolDefinition(
    name="daemon_list",
    description="List all active background daemon agents in the workspace.",
    parameters={"type": "object", "properties": {}},
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="automation",
)

DAEMON_STOP_TOOL = ToolDefinition(
    name="daemon_stop",
    description="Stop a running daemon agent by its ID.",
    parameters={
        "type": "object",
        "properties": {
            "daemon_id": {
                "type": "string",
                "description": "ID of the daemon to stop",
            },
        },
        "required": ["daemon_id"],
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="automation",
)


async def handle_daemon_create(
    name: str,
    description: str,
    daemon_type: str = "custom",
    watch_target: str = "",
    poll_interval_minutes: int = 5,
    sensitivity: str = "medium",
) -> dict:
    if not _daemon_manager:
        return {"error": "Daemon manager not initialized"}
    try:
        config = await _daemon_manager.create_daemon(
            workspace_id=_current_workspace_id or "default",
            user_id=_current_user_id or "system",
            name=name,
            description=description,
            daemon_type=daemon_type,
            watch_target=watch_target,
            poll_interval=poll_interval_minutes * 60,
            sensitivity=sensitivity,
        )
        return {"success": True, "daemon": config.to_dict()}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def handle_daemon_list() -> dict:
    if not _daemon_manager:
        return {"error": "Daemon manager not initialized"}
    daemons = _daemon_manager.list_daemons(_current_workspace_id or "")
    return {"success": True, "daemons": daemons, "count": len(daemons)}


async def handle_daemon_stop(daemon_id: str) -> dict:
    if not _daemon_manager:
        return {"error": "Daemon manager not initialized"}
    ok = await _daemon_manager.stop_daemon(daemon_id)
    return {"success": ok}


def register_daemon_tools(registry) -> None:
    """Register all daemon control tools."""
    registry.register(definition=DAEMON_CREATE_TOOL, handler=handle_daemon_create)
    registry.register(definition=DAEMON_LIST_TOOL, handler=handle_daemon_list)
    registry.register(definition=DAEMON_STOP_TOOL, handler=handle_daemon_stop)
    logger.info("Daemon control tools registered")
