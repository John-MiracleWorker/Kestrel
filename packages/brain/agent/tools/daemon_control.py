from __future__ import annotations
"""
Daemon control tools.

These tools serve two adjacent purposes:
  1. Manage Brain-side background daemon agents when a daemon manager is wired.
  2. Bridge companion surfaces to the native local-operator daemon control plane.
"""

import logging
from typing import Any

from core.local_operator import (
    LocalOperatorClientError,
    control_socket_available,
    read_local_operator_runtime_profile,
    read_local_operator_status_snapshot,
    resolve_local_operator_paths,
    send_local_operator_request,
)
from agent.types import ToolDefinition, RiskLevel

logger = logging.getLogger("brain.agent.tools.daemon_control")

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
    availability_requirements=("daemon_manager",),
)

DAEMON_LIST_TOOL = ToolDefinition(
    name="daemon_list",
    description="List all active background daemon agents in the workspace.",
    parameters={"type": "object", "properties": {}},
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="automation",
    availability_requirements=("daemon_manager",),
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
    availability_requirements=("daemon_manager",),
)

DAEMON_STATUS_TOOL = ToolDefinition(
    name="daemon_status",
    description=(
        "Inspect the native local-operator daemon that powers Kestrel's daemon-first "
        "runtime. Returns health, runtime profile, autonomy mode, and control-plane "
        "summary from the live daemon when available, or the latest local snapshot."
    ),
    parameters={"type": "object", "properties": {}},
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="automation",
)

DAEMON_SUGGESTIONS_TOOL = ToolDefinition(
    name="daemon_suggestions",
    description=(
        "List, accept, or dismiss native daemon background suggestions. "
        "Use this to review the daemon's suggest-first opportunities and "
        "explicitly approve one to start."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "accept", "dismiss"],
                "default": "list",
            },
            "suggestion_id": {
                "type": "string",
                "description": "Required for accept or dismiss actions.",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 100,
            },
        },
    },
    risk_level=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=20,
    category="automation",
)

DAEMON_RESEARCH_TOOL = ToolDefinition(
    name="daemon_research",
    description=(
        "Start or inspect a native daemon research session. The daemon persists "
        "research notebooks, source snapshots, and artifacts in the local control plane."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "list", "detail"],
                "default": "list",
            },
            "prompt": {
                "type": "string",
                "description": "Research prompt to start. Required when action=start.",
            },
            "session_id": {
                "type": "string",
                "description": "Research session ID. Required when action=detail.",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 100,
            },
            "status": {
                "type": "string",
                "description": "Optional research status filter for list.",
            },
        },
    },
    risk_level=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=30,
    category="automation",
)

DAEMON_PROCEDURES_TOOL = ToolDefinition(
    name="daemon_procedures",
    description=(
        "List learned procedures captured by the native local-operator daemon. "
        "These procedures summarize reusable successful workflows."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 100,
            },
            "enabled_only": {
                "type": "boolean",
                "default": False,
            },
        },
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=15,
    category="automation",
)

DAEMON_LEARNING_TOOL = ToolDefinition(
    name="daemon_learning",
    description=(
        "List recent learning events captured by the native daemon, including "
        "task outcomes, suggestion decisions, and learned procedures."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 100,
            },
            "event_type": {
                "type": "string",
                "description": "Optional event type filter.",
            },
        },
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=15,
    category="automation",
)


def _local_operator_paths():
    return resolve_local_operator_paths()


def _local_operator_unavailable() -> dict[str, Any]:
    return {
        "success": False,
        "error": "Local operator daemon is unavailable. Start the native daemon or inspect the local snapshots instead.",
    }


async def _send_local_operator(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await send_local_operator_request(method, params or {}, paths=_local_operator_paths())


async def handle_daemon_create(
    name: str,
    description: str,
    daemon_type: str = "custom",
    watch_target: str = "",
    poll_interval_minutes: int = 5,
    sensitivity: str = "medium",
    execution_context=None,
    daemon_manager=None,
    workspace_id: str = "",
    user_id: str = "",
) -> dict:
    manager = daemon_manager
    ws_id = workspace_id or getattr(execution_context, "workspace_id", None)
    actor_id = user_id or getattr(execution_context, "user_id", None)
    if not manager:
        return {"error": "Daemon manager not initialized"}
    try:
        config = await manager.create_daemon(
            workspace_id=ws_id or "default",
            user_id=actor_id or "system",
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


async def handle_daemon_list(execution_context=None, daemon_manager=None, workspace_id: str = "") -> dict:
    manager = daemon_manager
    ws_id = workspace_id or getattr(execution_context, "workspace_id", None)
    if not manager:
        return {"error": "Daemon manager not initialized"}
    daemons = manager.list_daemons(ws_id or "")
    return {"success": True, "daemons": daemons, "count": len(daemons)}


async def handle_daemon_stop(daemon_id: str, daemon_manager=None) -> dict:
    manager = daemon_manager
    if not manager:
        return {"error": "Daemon manager not initialized"}
    ok = await manager.stop_daemon(daemon_id)
    return {"success": ok}


async def handle_daemon_status() -> dict[str, Any]:
    paths = _local_operator_paths()
    snapshot = read_local_operator_status_snapshot(paths)
    runtime_profile = read_local_operator_runtime_profile(paths)
    if control_socket_available(paths):
        try:
            status = await _send_local_operator("status")
            return {
                "success": True,
                "source": "daemon",
                "status": status,
                "runtime_profile": status.get("runtime_profile") or runtime_profile,
            }
        except LocalOperatorClientError as exc:
            logger.warning("Falling back to local operator snapshot after control error: %s", exc)
    if snapshot or runtime_profile:
        return {
            "success": True,
            "source": "snapshot",
            "status": snapshot,
            "runtime_profile": runtime_profile,
        }
    return _local_operator_unavailable()


async def handle_daemon_suggestions(
    action: str = "list",
    suggestion_id: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    if not control_socket_available(_local_operator_paths()):
        return _local_operator_unavailable()

    normalized = str(action or "list").strip().lower()
    if normalized == "list":
        result = await _send_local_operator(
            "suggestion.list",
            {"status": "pending", "limit": max(1, min(int(limit or 10), 100))},
        )
        return {"success": True, "source": "daemon", **result}
    if normalized not in {"accept", "dismiss"}:
        return {"success": False, "error": f"Unsupported suggestions action: {action}"}
    if not suggestion_id:
        return {"success": False, "error": "suggestion_id is required"}
    result = await _send_local_operator(
        "suggestion.resolve",
        {"suggestion_id": suggestion_id, "action": normalized},
    )
    return {"success": True, "source": "daemon", **result}


async def handle_daemon_research(
    action: str = "list",
    prompt: str = "",
    session_id: str = "",
    limit: int = 10,
    status: str = "",
) -> dict[str, Any]:
    if not control_socket_available(_local_operator_paths()):
        return _local_operator_unavailable()

    normalized = str(action or "list").strip().lower()
    if normalized == "start":
        if not str(prompt or "").strip():
            return {"success": False, "error": "prompt is required for daemon_research action=start"}
        result = await _send_local_operator("research.start", {"prompt": prompt})
        return {"success": True, "source": "daemon", **result}
    if normalized == "detail":
        if not session_id:
            return {"success": False, "error": "session_id is required for daemon_research action=detail"}
        result = await _send_local_operator("research.detail", {"session_id": session_id})
        return {"success": True, "source": "daemon", **result}
    if normalized != "list":
        return {"success": False, "error": f"Unsupported research action: {action}"}
    result = await _send_local_operator(
        "research.list",
        {
            "status": str(status or "").strip(),
            "limit": max(1, min(int(limit or 10), 100)),
        },
    )
    return {"success": True, "source": "daemon", **result}


async def handle_daemon_procedures(limit: int = 10, enabled_only: bool = False) -> dict[str, Any]:
    if not control_socket_available(_local_operator_paths()):
        return _local_operator_unavailable()
    result = await _send_local_operator(
        "procedure.list",
        {"limit": max(1, min(int(limit or 10), 100)), "enabled_only": bool(enabled_only)},
    )
    return {"success": True, "source": "daemon", **result}


async def handle_daemon_learning(limit: int = 20, event_type: str = "") -> dict[str, Any]:
    if not control_socket_available(_local_operator_paths()):
        return _local_operator_unavailable()
    result = await _send_local_operator(
        "learning.list",
        {
            "limit": max(1, min(int(limit or 20), 100)),
            "event_type": str(event_type or "").strip(),
        },
    )
    return {"success": True, "source": "daemon", **result}


def register_daemon_tools(registry) -> None:
    """Register all daemon control tools."""
    registry.register(definition=DAEMON_CREATE_TOOL, handler=handle_daemon_create)
    registry.register(definition=DAEMON_LIST_TOOL, handler=handle_daemon_list)
    registry.register(definition=DAEMON_STOP_TOOL, handler=handle_daemon_stop)
    registry.register(definition=DAEMON_STATUS_TOOL, handler=handle_daemon_status)
    registry.register(definition=DAEMON_SUGGESTIONS_TOOL, handler=handle_daemon_suggestions)
    registry.register(definition=DAEMON_RESEARCH_TOOL, handler=handle_daemon_research)
    registry.register(definition=DAEMON_PROCEDURES_TOOL, handler=handle_daemon_procedures)
    registry.register(definition=DAEMON_LEARNING_TOOL, handler=handle_daemon_learning)
    logger.info("Daemon control tools registered")
