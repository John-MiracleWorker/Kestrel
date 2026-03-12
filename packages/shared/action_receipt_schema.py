"""Shared action receipt schema for Hands, Brain, and operator surfaces."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "action_receipt.v1"

FAILURE_CLASS_NONE = "none"
FAILURE_CLASS_PERMISSION_DENIED = "permission_denied"
FAILURE_CLASS_TIMEOUT = "timeout"
FAILURE_CLASS_SANDBOX_CRASH = "sandbox_crash"
FAILURE_CLASS_EXECUTION_ERROR = "execution_error"
FAILURE_CLASS_PARTIAL_OUTPUT = "partial_output"
FAILURE_CLASS_VALIDATION_ERROR = "validation_error"
FAILURE_CLASS_ESCALATION_REQUIRED = "escalation_required"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(value)


def _normalize_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def normalize_capability_grant(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    return {
        "grant_id": str(payload.get("grant_id") or uuid.uuid4()),
        "scope": str(payload.get("scope") or "workspace"),
        "workspace_id": str(payload.get("workspace_id") or ""),
        "user_id": str(payload.get("user_id") or ""),
        "agent_profile_id": str(payload.get("agent_profile_id") or ""),
        "channel": str(payload.get("channel") or ""),
        "action_selector": str(payload.get("action_selector") or ""),
        "tool_selector": str(payload.get("tool_selector") or ""),
        "approval_state": str(payload.get("approval_state") or ""),
        "expires_at": str(payload.get("expires_at") or ""),
        "metadata": _normalize_dict(payload.get("metadata")),
    }


def normalize_artifact_manifest_entry(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    return {
        "artifact_id": str(payload.get("artifact_id") or uuid.uuid4()),
        "name": str(payload.get("name") or ""),
        "artifact_type": str(payload.get("artifact_type") or ""),
        "uri": str(payload.get("uri") or ""),
        "mime_type": str(payload.get("mime_type") or ""),
        "size_bytes": int(payload.get("size_bytes") or 0),
        "checksum": str(payload.get("checksum") or ""),
        "description": str(payload.get("description") or ""),
        "metadata": _normalize_dict(payload.get("metadata")),
    }


def build_action_receipt(
    *,
    request_id: str,
    runtime_class: str,
    risk_class: str,
    failure_class: str = FAILURE_CLASS_NONE,
    logs_pointer: str = "",
    stdout_pointer: str = "",
    stderr_pointer: str = "",
    sandbox_id: str = "",
    exit_code: int = 0,
    audit_summary: str = "",
    artifact_manifest: list[dict[str, Any]] | None = None,
    file_touches: list[str] | None = None,
    network_touches: list[str] | None = None,
    system_touches: list[str] | None = None,
    grants: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    mutating: bool = False,
    receipt_id: str | None = None,
    finalized_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_id": receipt_id or str(uuid.uuid4()),
        "request_id": str(request_id or ""),
        "runtime_class": str(runtime_class or ""),
        "risk_class": str(risk_class or ""),
        "failure_class": str(failure_class or FAILURE_CLASS_NONE),
        "logs_pointer": str(logs_pointer or ""),
        "stdout_pointer": str(stdout_pointer or ""),
        "stderr_pointer": str(stderr_pointer or ""),
        "sandbox_id": str(sandbox_id or ""),
        "exit_code": int(exit_code or 0),
        "audit_summary": str(audit_summary or ""),
        "artifact_manifest": [
            normalize_artifact_manifest_entry(entry) for entry in _normalize_list(artifact_manifest)
        ],
        "file_touches": [str(item) for item in _normalize_list(file_touches)],
        "network_touches": [str(item) for item in _normalize_list(network_touches)],
        "system_touches": [str(item) for item in _normalize_list(system_touches)],
        "grants": [normalize_capability_grant(grant) for grant in _normalize_list(grants)],
        "metadata": _normalize_dict(metadata),
        "mutating": bool(mutating),
        "finalized_at": str(finalized_at or _utcnow()),
    }


def normalize_action_receipt(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    return build_action_receipt(
        request_id=str(payload.get("request_id") or ""),
        runtime_class=str(payload.get("runtime_class") or ""),
        risk_class=str(payload.get("risk_class") or ""),
        failure_class=str(payload.get("failure_class") or FAILURE_CLASS_NONE),
        logs_pointer=str(payload.get("logs_pointer") or ""),
        stdout_pointer=str(payload.get("stdout_pointer") or ""),
        stderr_pointer=str(payload.get("stderr_pointer") or ""),
        sandbox_id=str(payload.get("sandbox_id") or ""),
        exit_code=int(payload.get("exit_code") or 0),
        audit_summary=str(payload.get("audit_summary") or ""),
        artifact_manifest=_normalize_list(payload.get("artifact_manifest")),
        file_touches=_normalize_list(payload.get("file_touches")),
        network_touches=_normalize_list(payload.get("network_touches")),
        system_touches=_normalize_list(payload.get("system_touches")),
        grants=_normalize_list(payload.get("grants")),
        metadata=_normalize_dict(payload.get("metadata")),
        mutating=bool(payload.get("mutating")),
        receipt_id=str(payload.get("receipt_id") or uuid.uuid4()),
        finalized_at=str(payload.get("finalized_at") or _utcnow()),
    )


def dumps_action_receipt(value: dict[str, Any] | None) -> str:
    return json.dumps(normalize_action_receipt(value), separators=(",", ":"), sort_keys=True)

