"""Docker/Hands runtime backend."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

from agent.runtime.base import RuntimeCapabilities, RuntimeErrorResult, RuntimeMode
from agent.runtime.execution_trace import attach_execution_trace

logger = logging.getLogger("brain.agent.runtime.docker")

_SHARED_PATH = Path(__file__).resolve().parents[3] / "shared"
if str(_SHARED_PATH) not in sys.path:
    sys.path.append(str(_SHARED_PATH))

from action_event_schema import (
    build_execution_action_event,
    classify_risk_class,
    classify_runtime_class,
)


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
        runtime_class = classify_runtime_class("docker")
        risk_class = classify_risk_class(action_type=tool_name)

        if tool_name != "code_execute":
            raise RuntimeErrorResult(f"Docker runtime does not handle tool: {tool_name}")

        if not self._hands_client:
            error_message = (
                "Code execution requires the Hands service for secure sandboxing. "
                "The Hands service is not connected."
            )
            error_event = build_execution_action_event(
                source="brain.runtime.docker",
                action_type=f"{tool_name}.{payload.get('language', 'python')}",
                status="error",
                runtime_class=runtime_class,
                risk_class=risk_class,
                metadata={"error": error_message},
            )
            return {
                "success": False,
                "error": error_message,
                **attach_execution_trace(
                    {},
                    runtime_class=runtime_class,
                    risk_class=risk_class,
                    action_events=[error_event],
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
            user_id=str(payload.get("user_id", "")),
            workspace_id=str(payload.get("workspace_id", "")),
            conversation_id=str(payload.get("conversation_id", "")),
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
        action_events: list[dict[str, Any]] = []
        status = "RUNNING"
        exec_time = 0
        memory_used_mb = 0
        audit_log: dict[str, Any] = {}

        async for chunk in self._hands_client.ExecuteSkill(request):
            if chunk.output:
                output_parts.append(chunk.output)
            if chunk.error:
                error_parts.append(chunk.error)
            if chunk.execution_time_ms:
                exec_time = chunk.execution_time_ms
            if chunk.memory_used_mb:
                memory_used_mb = chunk.memory_used_mb
            if getattr(chunk, "action_event_json", ""):
                try:
                    action_events.append(json.loads(chunk.action_event_json))
                except json.JSONDecodeError:
                    logger.warning("Hands returned invalid action_event_json")
            if getattr(chunk, "audit_log", None):
                audit_log = {
                    "network_requests": list(chunk.audit_log.network_requests),
                    "file_accesses": list(chunk.audit_log.file_accesses),
                    "system_calls": list(chunk.audit_log.system_calls),
                    "sandbox_id": chunk.audit_log.sandbox_id,
                }
            status = hands_pb2.SkillExecutionResponse.Status.Name(chunk.status)

        if not action_events:
            action_events = [
                build_execution_action_event(
                    source="brain.runtime.docker",
                    action_type=f"{tool_name}.{language}",
                    status="success" if status == "SUCCESS" else "error",
                    runtime_class=runtime_class,
                    risk_class=risk_class,
                    metadata={
                        "error": "".join(error_parts),
                        "execution_time_ms": exec_time,
                        "memory_used_mb": memory_used_mb,
                    },
                )
            ]

        return attach_execution_trace(
            {
                "success": status == "SUCCESS",
                "output": "".join(output_parts),
                "error": "".join(error_parts),
                "execution_time_ms": exec_time,
                "memory_used_mb": memory_used_mb,
                "audit_log": audit_log,
            },
            runtime_class=runtime_class,
            risk_class=risk_class,
            action_events=action_events,
        )
