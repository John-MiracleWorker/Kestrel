"""
Session Tools — agent-callable tools for cross-session communication.

Based on OpenClaw's sessions_list / sessions_send / sessions_history
pattern, allowing agents to discover peers and coordinate work.
"""

from agent.types import RiskLevel, ToolDefinition


# ── sessions_list ────────────────────────────────────────────────────

SESSIONS_LIST_TOOL = ToolDefinition(
    name="sessions_list",
    description=(
        "Discover active agent sessions in this workspace. "
        "Returns session IDs, agent types, current goals, and status. "
        "Use this to find other agents you can coordinate with."
    ),
    parameters={
        "type": "object",
        "properties": {
            "active_only": {
                "type": "boolean",
                "description": "Only show active sessions (default: true)",
                "default": True,
            },
        },
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="sessions",
)


# ── sessions_send ────────────────────────────────────────────────────

SESSIONS_SEND_TOOL = ToolDefinition(
    name="sessions_send",
    description=(
        "Send a message to another agent session. "
        "Use this to share findings, request help, or coordinate actions. "
        "Message types: 'text' (info sharing), 'request' (ask for action), "
        "'response' (answer a request), 'announce' (broadcast to all)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to_session_id": {
                "type": "string",
                "description": "Target session ID (use sessions_list to discover)",
            },
            "content": {
                "type": "string",
                "description": "Message content to send",
            },
            "message_type": {
                "type": "string",
                "enum": ["text", "request", "response", "announce"],
                "description": "Type of message (default: text)",
                "default": "text",
            },
            "reply_to": {
                "type": "string",
                "description": "Optional message ID this is replying to",
            },
        },
        "required": ["to_session_id", "content"],
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="sessions",
)


# ── sessions_history ─────────────────────────────────────────────────

SESSIONS_HISTORY_TOOL = ToolDefinition(
    name="sessions_history",
    description=(
        "Fetch the message history for a session. "
        "Includes both sent and received messages. "
        "Use this to review what another agent has been doing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session ID to get history for",
            },
            "limit": {
                "type": "integer",
                "description": "Max messages to return (default: 20)",
                "default": 20,
            },
        },
        "required": ["session_id"],
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="sessions",
)


# ── sessions_inbox ───────────────────────────────────────────────────

SESSIONS_INBOX_TOOL = ToolDefinition(
    name="sessions_inbox",
    description=(
        "Check your inbox for messages from other agents. "
        "Returns recent messages sent to your session."
    ),
    parameters={
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "description": "Only get messages after this ISO timestamp (optional)",
            },
        },
    },
    risk_level=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10,
    category="sessions",
)

SESSION_TOOLS = [
    SESSIONS_LIST_TOOL,
    SESSIONS_SEND_TOOL,
    SESSIONS_HISTORY_TOOL,
    SESSIONS_INBOX_TOOL,
]


async def execute_session_tool(tool_name: str, args: dict, context: dict) -> dict:
    """Execute a session tool."""
    session_manager = context.get("session_manager")
    current_task = context.get("current_task")

    if not session_manager or not current_task:
        return {"success": False, "error": "Session tools not available"}

    session_id = getattr(current_task, "id", "unknown")
    workspace_id = getattr(current_task, "workspace_id", "")

    if tool_name == "sessions_list":
        sessions = await session_manager.list_sessions(
            workspace_id=workspace_id,
            active_only=args.get("active_only", True),
        )
        return {"success": True, "output": sessions}

    elif tool_name == "sessions_send":
        msg = await session_manager.send_message(
            from_session_id=session_id,
            to_session_id=args["to_session_id"],
            content=args["content"],
            message_type=args.get("message_type", "text"),
            reply_to=args.get("reply_to"),
        )
        return {"success": True, "output": f"Message sent (id: {msg.id})"}

    elif tool_name == "sessions_history":
        history = await session_manager.get_history(
            session_id=args["session_id"],
            limit=args.get("limit", 20),
        )
        return {"success": True, "output": history}

    elif tool_name == "sessions_inbox":
        messages = await session_manager.get_inbox(
            session_id=session_id,
            since=args.get("since"),
        )
        return {"success": True, "output": messages}

    return {"success": False, "error": f"Unknown session tool: {tool_name}"}
