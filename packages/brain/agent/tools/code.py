"""
Code execution tool — runs Python/JS/Shell in a sandboxed environment.

Routes execution through the active runtime policy (docker/native/hybrid)
and exposes runtime capability metadata in responses.
"""

import logging

from agent.runtime import get_active_runtime
from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.code")

def register_code_tools(registry) -> None:
    """Register code execution tools.

    Args:
        registry: The tool registry to register with.
    """

    registry.register(
        definition=ToolDefinition(
            name="code_execute",
            description=(
                "Execute code in a sandboxed environment. Supports Python, "
                "JavaScript, and shell commands. Use for calculations, data "
                "processing, file generation, and any programmatic task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "javascript", "shell"],
                        "description": "Programming language to execute",
                    },
                    "code": {
                        "type": "string",
                        "description": "The code to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds (max 60)",
                        "default": 30,
                    },
                },
                "required": ["language", "code"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=60,
            category="code",
        ),
        handler=execute_code,
    )


async def execute_code(
    language: str,
    code: str,
    timeout: int = 30,
    workspace_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    execution_context=None,
) -> dict:
    """
    Execute code through the active runtime backend selected at startup.
    """
    timeout = min(timeout, 60)  # Cap at 60s

    active_runtime = get_active_runtime()
    if not active_runtime:
        return {
            "success": False,
            "error": "Runtime policy is not initialized.",
        }

    capabilities = active_runtime.capabilities
    if language not in capabilities.supports_code_languages:
        return {
            "success": False,
            "error": f"Language '{language}' is not supported in active runtime mode '{capabilities.mode.value}'.",
            "capabilities": capabilities.as_dict(),
        }

    try:
        result = await active_runtime.execute(
            tool_name="code_execute",
            payload={
                "language": language,
                "code": code,
                "timeout": timeout,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "session_id": getattr(execution_context, "session_id", ""),
                "source": getattr(execution_context, "source", ""),
                "capability_grants": (
                    [grant.to_dict() for grant in getattr(execution_context, "capability_grants", ())]
                    if execution_context
                    else []
                ),
                "session_route": (
                    execution_context.route.to_dict()
                    if execution_context and getattr(execution_context, "route", None)
                    else {}
                ),
                "mutating": True,
            },
        )
        if "capabilities" not in result:
            result["capabilities"] = capabilities.as_dict()
        return result
    except Exception as e:
        logger.error(f"Runtime execution failed: {e}")
        return {"success": False, "error": str(e), "capabilities": capabilities.as_dict()}
