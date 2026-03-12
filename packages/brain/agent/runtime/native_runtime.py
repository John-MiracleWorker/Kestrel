"""Native runtime backend for direct host execution."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.runtime.base import RuntimeCapabilities, RuntimeErrorResult, RuntimeMode
from agent.runtime.execution_trace import attach_execution_trace, write_execution_audit_entry

import logging

_SHARED_PATH = Path(__file__).resolve().parents[3] / "shared"
if str(_SHARED_PATH) not in sys.path:
    sys.path.append(str(_SHARED_PATH))

from action_event_schema import (
    build_execution_action_event,
    classify_risk_class,
    classify_runtime_class,
    stable_hash,
)

logger = logging.getLogger("brain.agent.runtime.native")


class NativeRuntime:
    """Execute tool payloads directly on the host runtime."""

    def __init__(self):
        self._capabilities = RuntimeCapabilities(
            mode=RuntimeMode.NATIVE,
            supports_native_execution=True,
            supports_computer_use=True,
            supports_host_shell=True,
            supports_host_python=True,
            supports_code_languages=("python", "javascript", "shell"),
        )

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    async def execute(self, *, tool_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        fallback_from = str(payload.get("_fallback_from", ""))
        fallback_reason = str(payload.get("_fallback_reason", ""))
        fallback_used = bool(fallback_from)
        runtime_class = classify_runtime_class("native", fallback_used=fallback_used)
        risk_class = classify_risk_class(action_type=tool_name)
        exec_id = str(uuid.uuid4())
        workspace_id = str(payload.get("workspace_id") or os.getenv("KESTREL_WORKSPACE_ID", "default"))
        user_id = str(payload.get("user_id") or os.getenv("KESTREL_USER_ID", "agent"))
        language = str(payload.get("language") or "")
        action_type = f"{tool_name}.{language}" if language else tool_name
        command_preview = str(payload.get("command") or payload.get("code") or "")
        command_hash = stable_hash(command_preview)

        running_event = build_execution_action_event(
            source="brain.runtime.native",
            action_type=action_type,
            status="running",
            runtime_class=runtime_class,
            risk_class=risk_class,
            before_state={"command_hash": command_hash, "policy_decision": "admitted"},
            after_state={"command_hash": command_hash, "policy_decision": "running"},
            metadata={
                "exec_id": exec_id,
                "fallback_used": fallback_used,
                "fallback_from": fallback_from,
                "fallback_reason": fallback_reason,
            },
        )

        try:
            if tool_name in {"host_shell", "code_execute"} and payload.get("language") in (None, "shell"):
                command = str(payload.get("command") or payload.get("code") or "")
                base_result = await self._run_shell(command)
            elif tool_name in {"host_python", "code_execute"} and payload.get("language") in (None, "python"):
                code = str(payload.get("code", ""))
                base_result = await self._run_python(code)
            elif tool_name == "code_execute" and payload.get("language") == "javascript":
                base_result = await self._run_node(str(payload.get("code", "")))
            elif tool_name == "computer_use":
                base_result = {
                    "success": True,
                    "note": "computer_use executes in tool handler on native runtime.",
                }
            else:
                raise RuntimeErrorResult(f"Native runtime does not handle tool: {tool_name}")

            final_status = "success" if base_result.get("success") else "error"
            final_event = build_execution_action_event(
                source="brain.runtime.native",
                action_type=action_type,
                status=final_status,
                runtime_class=runtime_class,
                risk_class=risk_class,
                before_state={"command_hash": command_hash, "policy_decision": "running"},
                after_state={"command_hash": command_hash, "policy_decision": final_status},
                metadata={
                    "exec_id": exec_id,
                    "error": base_result.get("error", ""),
                    "exit_code": base_result.get("exit_code"),
                    "fallback_used": fallback_used,
                    "fallback_from": fallback_from,
                    "fallback_reason": fallback_reason,
                },
            )
            try:
                write_execution_audit_entry(
                    exec_id=exec_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    tool_name=tool_name,
                    function_name=language or "execute",
                    arguments=command_preview,
                    status=final_status,
                    runtime_class=runtime_class,
                    risk_class=risk_class,
                    action_events=[running_event, final_event],
                    execution_time_ms=int(base_result.get("execution_time_ms", 0)),
                    memory_used_mb=int(base_result.get("memory_used_mb", 0)),
                    error=str(base_result.get("error", "")),
                    exit_code=base_result.get("exit_code"),
                    metadata={
                        "fallback_used": fallback_used,
                        "fallback_from": fallback_from,
                        "fallback_reason": fallback_reason,
                    },
                )
            except Exception as audit_exc:
                logger.warning("Failed to persist native execution audit: %s", audit_exc)
            return attach_execution_trace(
                dict(base_result),
                runtime_class=runtime_class,
                risk_class=risk_class,
                action_events=[running_event, final_event],
                fallback_used=fallback_used,
                fallback_from=fallback_from,
                fallback_to=runtime_class if fallback_used else "",
                fallback_reason=fallback_reason,
            )
        except Exception as exc:
            error_event = build_execution_action_event(
                source="brain.runtime.native",
                action_type=action_type,
                status="error",
                runtime_class=runtime_class,
                risk_class=risk_class,
                before_state={"command_hash": command_hash, "policy_decision": "running"},
                after_state={"command_hash": command_hash, "policy_decision": "error"},
                metadata={
                    "exec_id": exec_id,
                    "error": str(exc),
                    "fallback_used": fallback_used,
                    "fallback_from": fallback_from,
                    "fallback_reason": fallback_reason,
                },
            )
            try:
                write_execution_audit_entry(
                    exec_id=exec_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    tool_name=tool_name,
                    function_name=language or "execute",
                    arguments=command_preview,
                    status="error",
                    runtime_class=runtime_class,
                    risk_class=risk_class,
                    action_events=[running_event, error_event],
                    error=str(exc),
                    metadata={
                        "fallback_used": fallback_used,
                        "fallback_from": fallback_from,
                        "fallback_reason": fallback_reason,
                    },
                )
            except Exception as audit_exc:
                logger.warning("Failed to persist native execution audit: %s", audit_exc)
            if isinstance(exc, RuntimeErrorResult):
                raise
            return attach_execution_trace(
                {
                    "success": False,
                    "error": str(exc),
                    "output": "",
                },
                runtime_class=runtime_class,
                risk_class=risk_class,
                action_events=[running_event, error_event],
                fallback_used=fallback_used,
                fallback_from=fallback_from,
                fallback_to=runtime_class if fallback_used else "",
                fallback_reason=fallback_reason,
            )

    async def _run_shell(self, command: str) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {
            "success": proc.returncode == 0,
            "output": stdout.decode(),
            "error": stderr.decode(),
            "exit_code": proc.returncode,
            "execution_time_ms": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
        }

    async def _run_python(self, code: str) -> dict[str, Any]:
        tmp_path = None
        started_at = datetime.now(timezone.utc)
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(code)
                tmp_path = f.name
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return {
                "success": proc.returncode == 0,
                "output": stdout.decode(),
                "error": stderr.decode(),
                "exit_code": proc.returncode,
                "execution_time_ms": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
            }
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    async def _run_node(self, code: str) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        proc = await asyncio.create_subprocess_exec(
            "node",
            "-e",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {
            "success": proc.returncode == 0,
            "output": stdout.decode(),
            "error": stderr.decode(),
            "exit_code": proc.returncode,
            "execution_time_ms": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
        }
