"""Docker/Hands runtime backend."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from agent.runtime.base import RuntimeCapabilities, RuntimeErrorResult, RuntimeMode

logger = logging.getLogger("brain.agent.runtime.docker")


class DockerRuntime:
    """Execute supported tool actions via Hands running in container runtime."""

    def __init__(self, hands_client: Any = None):
        self._hands_client = hands_client
        self._capabilities = RuntimeCapabilities(
            mode=RuntimeMode.DOCKER,
            supports_docker_execution=hands_client is not None,
            supports_code_languages=("python", "javascript", "shell"),
        )

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    async def execute(self, *, tool_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if tool_name != "code_execute":
            raise RuntimeErrorResult(f"Docker runtime does not handle tool: {tool_name}")

        if not self._hands_client:
            return {
                "success": False,
                "error": (
                    "Code execution requires the Hands service for secure sandboxing. "
                    "The Hands service is not connected."
                ),
            }

        language = str(payload.get("language", "python"))
        code = str(payload.get("code", ""))
        timeout = int(payload.get("timeout", 30))

        import hands_pb2

        skill_map = {
            "python": "python_executor",
            "javascript": "node_executor",
            "shell": "shell_executor",
        }
        request = hands_pb2.SkillExecutionRequest(
            skill_name=skill_map.get(language, "python_executor"),
            function_name="run",
            arguments=json.dumps({"code": code}),
            limits=hands_pb2.ResourceLimits(
                timeout_seconds=timeout,
                memory_mb=512,
                network_enabled=True,
            ),
        )

        output_parts: list[str] = []
        error_parts: list[str] = []
        status = "RUNNING"
        exec_time = 0

        async for chunk in self._hands_client.ExecuteSkill(request):
            if chunk.output:
                output_parts.append(chunk.output)
            if chunk.error:
                error_parts.append(chunk.error)
            if chunk.execution_time_ms:
                exec_time = chunk.execution_time_ms
            status = hands_pb2.SkillExecutionResponse.Status.Name(chunk.status)

        return {
            "success": status == "SUCCESS",
            "output": "".join(output_parts),
            "error": "".join(error_parts),
            "execution_time_ms": exec_time,
        }
