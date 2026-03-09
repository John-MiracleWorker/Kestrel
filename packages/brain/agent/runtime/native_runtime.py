"""Native runtime backend for direct host execution."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any, Mapping

from agent.runtime.base import RuntimeCapabilities, RuntimeErrorResult, RuntimeMode


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
        if tool_name in {"host_shell", "code_execute"} and payload.get("language") in (None, "shell"):
            command = str(payload.get("command") or payload.get("code") or "")
            return await self._run_shell(command)

        if tool_name in {"host_python", "code_execute"} and payload.get("language") in (None, "python"):
            code = str(payload.get("code", ""))
            return await self._run_python(code)

        if tool_name == "code_execute" and payload.get("language") == "javascript":
            return await self._run_node(str(payload.get("code", "")))

        if tool_name == "computer_use":
            return {"success": True, "note": "computer_use executes in tool handler on native runtime."}

        raise RuntimeErrorResult(f"Native runtime does not handle tool: {tool_name}")

    async def _run_shell(self, command: str) -> dict[str, Any]:
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
        }

    async def _run_python(self, code: str) -> dict[str, Any]:
        tmp_path = None
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
            }
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    async def _run_node(self, code: str) -> dict[str, Any]:
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
        }
