"""
Host Execution Tools — Native shell/python execution on the host OS.
WARNING: These tools bypass container isolation and execute directly on host.
Policy decisions and approval are delegated to native policy evaluators/providers.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import uuid
from datetime import datetime

from agent.security.native_policy import (
    NativeExecutionRequest,
    DEFAULT_NATIVE_POLICY_EVALUATOR,
    make_default_approval_provider,
)
from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.host_execution")

_POLICY_EVALUATOR = DEFAULT_NATIVE_POLICY_EVALUATOR


def _audit_log(
    *,
    exec_id: str,
    workspace_id: str,
    user_id: str,
    tool_name: str,
    function_name: str,
    arguments: str,
    status: str,
    policy_reason: str,
    approval_provider: str,
    approval_reason: str,
    started_at: datetime,
    error: str = "",
    exit_code: int | None = None,
):
    """Write native execution audit entries using hands-compatible schema concepts."""
    audit_dir = os.path.expanduser("~/.kestrel/audit")
    os.makedirs(audit_dir, exist_ok=True)
    log_file = os.path.join(audit_dir, f"audit-{datetime.utcnow().strftime('%Y-%m-%d')}.jsonl")

    elapsed_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)

    entry = {
        "exec_id": exec_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "skill_name": tool_name,
        "function_name": function_name,
        "arguments_hash": hash(arguments),
        "started_at": started_at.isoformat(),
        "completed_at": datetime.utcnow().isoformat(),
        "status": status,
        "execution_time_ms": elapsed_ms,
        "native_policy": {
            "policy_reason": policy_reason,
            "approval_provider": approval_provider,
            "approval_reason": approval_reason,
        },
    }
    if error:
        entry["error"] = error
    if exit_code is not None:
        entry["exit_code"] = exit_code

    try:
        with open(log_file, "a", encoding="utf-8") as file_handle:
            file_handle.write(json.dumps(entry) + "\n")
    except Exception as exc:  # pragma: no cover - file system issues are env-specific
        logger.error("Failed to write audit log: %s", exc)


async def _authorize_native_execution(tool_name: str, command: str) -> tuple[bool, str, str, str]:
    workspace_id = os.getenv("KESTREL_WORKSPACE_ID", "default")
    interactive = os.getenv("NATIVE_APPROVAL_INTERACTIVE", "false").lower() == "true"

    request = NativeExecutionRequest(
        workspace_id=workspace_id,
        tool_name=tool_name,
        function_name="execute",
        command=command,
        command_class=_POLICY_EVALUATOR.classify(tool_name, command),
        interactive=interactive,
    )

    decision = _POLICY_EVALUATOR.evaluate(request)
    if not decision.allowed:
        return False, decision.reason, "policy", "DENIED_BY_POLICY"

    if not decision.requires_approval:
        return True, decision.reason, "policy", "NOT_REQUIRED"

    provider = make_default_approval_provider(interactive=interactive)
    approval = await provider.approve(request)
    return approval.approved, decision.reason, approval.provider, approval.reason


async def execute_host_shell(command: str) -> dict:
    """Execute a shell command directly on the host OS."""
    started_at = datetime.utcnow()
    exec_id = str(uuid.uuid4())
    workspace_id = os.getenv("KESTREL_WORKSPACE_ID", "default")
    user_id = os.getenv("KESTREL_USER_ID", "agent")

    approved, policy_reason, approval_provider, approval_reason = await _authorize_native_execution(
        "host_shell",
        command,
    )
    if not approved:
        _audit_log(
            exec_id=exec_id,
            workspace_id=workspace_id,
            user_id=user_id,
            tool_name="host_execution",
            function_name="host_shell",
            arguments=command,
            status="denied",
            policy_reason=policy_reason,
            approval_provider=approval_provider,
            approval_reason=approval_reason,
            started_at=started_at,
            error="Native shell execution denied by policy/approval",
        )
        return {
            "success": False,
            "error": "Native shell execution denied by policy/approval.",
            "output": "",
            "policy_reason": policy_reason,
            "approval_reason": approval_reason,
        }

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        status = "success" if proc.returncode == 0 else "error"
        _audit_log(
            exec_id=exec_id,
            workspace_id=workspace_id,
            user_id=user_id,
            tool_name="host_execution",
            function_name="host_shell",
            arguments=command,
            status=status,
            policy_reason=policy_reason,
            approval_provider=approval_provider,
            approval_reason=approval_reason,
            started_at=started_at,
            error=stderr.decode() if proc.returncode != 0 else "",
            exit_code=proc.returncode,
        )
        return {
            "success": proc.returncode == 0,
            "output": stdout.decode(),
            "error": stderr.decode(),
            "exit_code": proc.returncode,
            "policy_reason": policy_reason,
            "approval_reason": approval_reason,
        }
    except Exception as exc:
        _audit_log(
            exec_id=exec_id,
            workspace_id=workspace_id,
            user_id=user_id,
            tool_name="host_execution",
            function_name="host_shell",
            arguments=command,
            status="error",
            policy_reason=policy_reason,
            approval_provider=approval_provider,
            approval_reason=approval_reason,
            started_at=started_at,
            error=str(exc),
        )
        return {
            "success": False,
            "error": str(exc),
            "output": "",
            "policy_reason": policy_reason,
            "approval_reason": approval_reason,
        }


async def execute_host_python(code: str) -> dict:
    """Execute python code directly on the host OS."""
    started_at = datetime.utcnow()
    exec_id = str(uuid.uuid4())
    workspace_id = os.getenv("KESTREL_WORKSPACE_ID", "default")
    user_id = os.getenv("KESTREL_USER_ID", "agent")

    command_preview = f"python(script_length={len(code)})"
    approved, policy_reason, approval_provider, approval_reason = await _authorize_native_execution(
        "host_python",
        command_preview,
    )
    if not approved:
        _audit_log(
            exec_id=exec_id,
            workspace_id=workspace_id,
            user_id=user_id,
            tool_name="host_execution",
            function_name="host_python",
            arguments=command_preview,
            status="denied",
            policy_reason=policy_reason,
            approval_provider=approval_provider,
            approval_reason=approval_reason,
            started_at=started_at,
            error="Native python execution denied by policy/approval",
        )
        return {
            "success": False,
            "error": "Native python execution denied by policy/approval.",
            "output": "",
            "policy_reason": policy_reason,
            "approval_reason": approval_reason,
        }

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as file_handle:
            file_handle.write(code)
            tmp_path = file_handle.name

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        status = "success" if proc.returncode == 0 else "error"
        _audit_log(
            exec_id=exec_id,
            workspace_id=workspace_id,
            user_id=user_id,
            tool_name="host_execution",
            function_name="host_python",
            arguments=command_preview,
            status=status,
            policy_reason=policy_reason,
            approval_provider=approval_provider,
            approval_reason=approval_reason,
            started_at=started_at,
            error=stderr.decode() if proc.returncode != 0 else "",
            exit_code=proc.returncode,
        )
        return {
            "success": proc.returncode == 0,
            "output": stdout.decode(),
            "error": stderr.decode(),
            "exit_code": proc.returncode,
            "policy_reason": policy_reason,
            "approval_reason": approval_reason,
        }
    except Exception as exc:
        _audit_log(
            exec_id=exec_id,
            workspace_id=workspace_id,
            user_id=user_id,
            tool_name="host_execution",
            function_name="host_python",
            arguments=command_preview,
            status="error",
            policy_reason=policy_reason,
            approval_provider=approval_provider,
            approval_reason=approval_reason,
            started_at=started_at,
            error=str(exc),
        )
        return {
            "success": False,
            "error": str(exc),
            "output": "",
            "policy_reason": policy_reason,
            "approval_reason": approval_reason,
        }
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def register_host_execution_tools(registry) -> None:
    registry.register(
        definition=ToolDefinition(
            name="host_shell",
            description=(
                "Execute a shell command DIRECTLY on the host OS. "
                "Uses workspace-scoped native policy evaluation and approval providers. "
                "WARNING: High risk operation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    }
                },
                "required": ["command"],
            },
            risk_level=RiskLevel.HIGH,
            category="control",
        ),
        handler=execute_host_shell,
    )

    registry.register(
        definition=ToolDefinition(
            name="host_python",
            description=(
                "Execute Python code DIRECTLY on the host OS. "
                "Uses workspace-scoped native policy evaluation and approval providers. "
                "WARNING: High risk operation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The python code to execute",
                    }
                },
                "required": ["code"],
            },
            risk_level=RiskLevel.HIGH,
            category="control",
        ),
        handler=execute_host_python,
    )
