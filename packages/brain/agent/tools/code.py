"""
Code execution tool — runs Python/JS/Shell in a sandboxed environment.

Routes execution through the Hands gRPC service for containerized sandboxing
with resource limits, network controls, and audit logging.

SECURITY: Local fallback execution has been removed. If the Hands service
is not available, code execution fails safely. Python's exec() cannot be
secured via globals restriction — object introspection allows trivial escape.
"""

import json
import logging
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.code")

# Hands gRPC client reference (set during registration)
_hands_client = None


def register_code_tools(registry, hands_client=None) -> None:
    """Register code execution tools.

    Args:
        registry: The tool registry to register with.
        hands_client: The Hands gRPC client for sandboxed execution.
            If None, code execution will return an error directing
            the user to start the Hands service.
    """
    global _hands_client
    _hands_client = hands_client

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
) -> dict:
    """
    Execute code in a sandboxed environment via the Hands gRPC service.

    If the Hands service is not available, returns an error. Local fallback
    execution has been removed for security — Python's exec() cannot be
    safely sandboxed via globals restriction.
    """
    timeout = min(timeout, 60)  # Cap at 60s

    if not _hands_client:
        return {
            "success": False,
            "error": (
                "Code execution requires the Hands service for secure sandboxing. "
                "The Hands service is not connected. Please ensure it is running "
                "and properly configured."
            ),
        }

    return await _execute_via_hands(language, code, timeout)


async def _execute_via_hands(language: str, code: str, timeout: int) -> dict:
    """Execute code via the Hands gRPC service."""
    try:
        # Map language to skill name
        skill_map = {
            "python": "python_executor",
            "javascript": "node_executor",
            "shell": "shell_executor",
        }

        skill_name = skill_map.get(language, "python_executor")

        # Call Hands service
        result = await _hands_client.execute_skill(
            skill_name=skill_name,
            function_name="run",
            arguments=json.dumps({"code": code}),
            timeout_seconds=timeout,
            memory_mb=512,
            network_enabled=False,
        )

        return {
            "success": result.get("status") == "SUCCESS",
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "execution_time_ms": result.get("execution_time_ms", 0),
        }

    except Exception as e:
        logger.error(f"Hands execution failed: {e}")
        return {"success": False, "error": str(e)}
