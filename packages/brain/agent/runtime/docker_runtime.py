"""Docker/Hands runtime backend."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

from agent.runtime.base import RuntimeCapabilities, RuntimeErrorResult, RuntimeMode
from agent.runtime.execution_trace import attach_execution_trace
from core.hands_grpc_setup import hands_pb2

logger = logging.getLogger("brain.agent.runtime.docker")

_SHARED_PATH = Path(__file__).resolve().parents[3] / "shared"
if str(_SHARED_PATH) not in sys.path:
    sys.path.append(str(_SHARED_PATH))

from action_event_schema import (
    build_execution_action_event,
    classify_risk_class,
    classify_runtime_class,
)
from action_receipt_schema import normalize_action_receipt


def _receipt_from_proto(receipt: Any) -> dict[str, Any]:
    if not receipt:
        return {}
    artifact_manifest = []
    for entry in getattr(receipt, "artifact_manifest", []):
        artifact_manifest.append(
            {
                "artifact_id": entry.artifact_id,
                "name": entry.name,
                "artifact_type": entry.artifact_type,
                "uri": entry.uri,
                "mime_type": entry.mime_type,
                "size_bytes": entry.size_bytes,
                "checksum": entry.checksum,
                "description": entry.description,
                "metadata": json.loads(entry.metadata_json) if entry.metadata_json else {},
            }
        )
    grants = []
    for grant in getattr(receipt, "grants", []):
        grants.append(
            {
                "grant_id": grant.grant_id,
                "scope": grant.scope,
                "workspace_id": grant.workspace_id,
                "user_id": grant.user_id,
                "agent_profile_id": grant.agent_profile_id,
                "channel": grant.channel,
                "action_selector": grant.action_selector,
                "tool_selector": grant.tool_selector,
                "approval_state": grant.approval_state,
                "expires_at": grant.expires_at,
                "metadata": json.loads(grant.metadata_json) if grant.metadata_json else {},
            }
        )
    return normalize_action_receipt(
        {
            "receipt_id": receipt.receipt_id,
            "request_id": receipt.request_id,
            "runtime_class": receipt.runtime_class,
            "risk_class": receipt.risk_class,
            "failure_class": hands_pb2.FailureClass.Name(receipt.failure_class)
            .replace("FAILURE_CLASS_", "")
            .lower(),
            "logs_pointer": receipt.logs_pointer,
            "stdout_pointer": receipt.stdout_pointer,
            "stderr_pointer": receipt.stderr_pointer,
            "sandbox_id": receipt.sandbox_id,
            "exit_code": receipt.exit_code,
            "audit_summary": receipt.audit_summary,
            "artifact_manifest": artifact_manifest,
            "file_touches": list(receipt.file_touches),
            "network_touches": list(receipt.network_touches),
            "system_touches": list(receipt.system_touches),
            "grants": grants,
            "metadata": json.loads(receipt.metadata_json) if receipt.metadata_json else {},
            "mutating": receipt.mutating,
            "finalized_at": receipt.finalized_at,
        }
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
        request_id = str(payload.get("request_id") or payload.get("session_id") or "")
        capability_grants = list(payload.get("capability_grants") or [])
        routing_context = {
            "session_route": payload.get("session_route") or {},
            "source": payload.get("source") or "",
        }

        skill_map = {
            "python": "python_executor",
            "javascript": "node_executor",
            "shell": "shell_executor",
        }
        request = hands_pb2.ActionExecutionRequest(
            request_id=request_id,
            user_id=str(payload.get("user_id", "")),
            workspace_id=str(payload.get("workspace_id", "")),
            conversation_id=str(payload.get("conversation_id", "")),
            session_id=str(payload.get("session_id", "")),
            action_name=skill_map.get(language, "python_executor"),
            function_name="run",
            arguments_json=json.dumps({"code": code}),
            routing_context_json=json.dumps(routing_context),
            budgets_json=json.dumps({"timeout": timeout}),
            mutating=bool(payload.get("mutating", False)),
            limits=hands_pb2.ResourceLimits(
                timeout_seconds=timeout,
                memory_mb=512,
                network_enabled=True,
            ),
            grants=[
                hands_pb2.CapabilityGrant(
                    grant_id=str(grant.get("grant_id") or ""),
                    scope=str(grant.get("scope") or ""),
                    workspace_id=str(grant.get("workspace_id") or ""),
                    user_id=str(grant.get("user_id") or ""),
                    agent_profile_id=str(grant.get("agent_profile_id") or ""),
                    channel=str(grant.get("channel") or ""),
                    action_selector=str(grant.get("action_selector") or ""),
                    tool_selector=str(grant.get("tool_selector") or ""),
                    approval_state=str(grant.get("approval_state") or ""),
                    expires_at=str(grant.get("expires_at") or ""),
                    metadata_json=json.dumps(grant.get("metadata") or {}),
                )
                for grant in capability_grants
            ],
        )

        output_parts: list[str] = []
        error_parts: list[str] = []
        action_events: list[dict[str, Any]] = []
        status = "ACTION_RUNNING"
        exec_time = 0
        memory_used_mb = 0
        receipt: dict[str, Any] = {}
        logs_pointer = ""

        async for chunk in self._hands_client.ExecuteAction(request):
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
            if getattr(chunk, "receipt", None) and getattr(chunk.receipt, "receipt_id", ""):
                receipt = _receipt_from_proto(chunk.receipt)
            if getattr(chunk, "logs_pointer", ""):
                logs_pointer = chunk.logs_pointer
            status = hands_pb2.ActionExecutionStatus.Name(chunk.status)

        if not action_events:
            action_status = {
                "ACTION_COMPLETED": "success",
                "ACTION_PARTIAL": "partial",
                "ACTION_DENIED": "denied",
                "ACTION_TIMEOUT": "timeout",
                "ACTION_FAILED": "error",
            }.get(status, "error")
            action_events = [
                build_execution_action_event(
                    source="brain.runtime.docker",
                    action_type=f"{tool_name}.{language}",
                    status=action_status,
                    runtime_class=runtime_class,
                    risk_class=risk_class,
                    metadata={
                        "error": "".join(error_parts),
                        "execution_time_ms": exec_time,
                        "memory_used_mb": memory_used_mb,
                        "receipt_id": receipt.get("receipt_id", ""),
                    },
                )
            ]

        return attach_execution_trace(
            {
                "success": status in {"ACTION_COMPLETED", "ACTION_PARTIAL"},
                "output": "".join(output_parts),
                "error": "".join(error_parts),
                "execution_time_ms": exec_time,
                "memory_used_mb": memory_used_mb,
                "logs_pointer": logs_pointer or receipt.get("logs_pointer", ""),
                "audit_log": {
                    "network_requests": receipt.get("network_touches", []),
                    "file_accesses": receipt.get("file_touches", []),
                    "system_calls": receipt.get("system_touches", []),
                    "sandbox_id": receipt.get("sandbox_id", ""),
                    "receipt_id": receipt.get("receipt_id", ""),
                },
                "exit_code": receipt.get("exit_code", 0),
                "receipt": receipt,
                "artifact_manifest": receipt.get("artifact_manifest", []),
            },
            runtime_class=runtime_class,
            risk_class=risk_class,
            action_events=action_events,
        )
