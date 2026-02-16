"""
Code execution tool — runs Python/JS/Shell in a sandboxed environment.

Routes execution through the Hands gRPC service for containerized sandboxing
with resource limits, network controls, and audit logging.
"""

import json
import logging
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.code")

# Hands gRPC client reference (set during registration)
_hands_client = None


def register_code_tools(registry, hands_client=None) -> None:
    """Register code execution tools."""
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
    Execute code in a sandboxed environment.

    If the Hands service is available, routes through gRPC for containerized
    execution. Otherwise falls back to a restricted local execution mode.
    """
    timeout = min(timeout, 60)  # Cap at 60s

    if _hands_client:
        return await _execute_via_hands(language, code, timeout)

    # Fallback: restricted local execution (Python only)
    if language == "python":
        return await _execute_python_local(code, timeout)
    elif language == "shell":
        return await _execute_shell_local(code, timeout)
    else:
        return {
            "success": False,
            "error": f"Language '{language}' requires the Hands service (not available)",
        }


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


async def _execute_python_local(code: str, timeout: int) -> dict:
    """
    Restricted local Python execution.
    Used when the Hands service is not available.
    """
    import asyncio
    import io
    import sys
    from contextlib import redirect_stdout, redirect_stderr

    # Security: block dangerous operations
    dangerous = [
        "import os", "import sys", "import subprocess",
        "import shutil", "exec(", "eval(", "__import__",
        "open(", "import socket", "import http",
    ]
    for pattern in dangerous:
        if pattern in code:
            return {
                "success": False,
                "error": f"Blocked: '{pattern}' is not allowed in local execution mode. "
                         "Use the Hands service for unrestricted execution.",
            }

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _run_python_restricted, code, stdout_buf, stderr_buf),
            timeout=timeout,
        )

        output = stdout_buf.getvalue()
        errors = stderr_buf.getvalue()

        return {
            "success": True,
            "output": output or str(result) if result is not None else output,
            "error": errors if errors else "",
        }

    except asyncio.TimeoutError:
        return {"success": False, "error": f"Execution timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _run_python_restricted(code: str, stdout_buf, stderr_buf):
    """Run Python code with redirected I/O."""
    from contextlib import redirect_stdout, redirect_stderr

    safe_globals = {
        "__builtins__": {
            "print": print,
            "range": range,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
            "set": set,
            "tuple": tuple,
            "sorted": sorted,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "sum": sum,
            "min": min,
            "max": max,
            "abs": abs,
            "round": round,
            "isinstance": isinstance,
            "type": type,
            "True": True,
            "False": False,
            "None": None,
        },
    }

    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        exec(code, safe_globals)

    return safe_globals.get("result", None)


async def _execute_shell_local(code: str, timeout: int) -> dict:
    """Restricted local shell execution."""
    import asyncio

    # Security: block dangerous commands
    dangerous = [
        "rm -rf", "rm -r", "mkfs", "dd if=",
        "chmod 777", ":(){", "fork", "shutdown",
        "> /dev/sd", "format c:", "del /f /s",
    ]
    code_lower = code.lower()
    for pattern in dangerous:
        if pattern in code_lower:
            return {
                "success": False,
                "error": f"Blocked: dangerous command pattern '{pattern}'",
            }

    try:
        proc = await asyncio.create_subprocess_shell(
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )

        return {
            "success": proc.returncode == 0,
            "output": stdout.decode("utf-8", errors="replace"),
            "error": stderr.decode("utf-8", errors="replace"),
        }

    except asyncio.TimeoutError:
        proc.kill()
        return {"success": False, "error": f"Execution timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}
