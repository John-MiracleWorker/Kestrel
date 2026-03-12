"""Shared action/event schema for Brain and Hands execution traces."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "action_event.v1"

RUNTIME_CLASS_SANDBOXED_DOCKER = "sandboxed_docker"
RUNTIME_CLASS_NATIVE_HOST = "native_host"
RUNTIME_CLASS_HYBRID_FALLBACK = "hybrid_native_fallback"

_HIGH_RISK_ACTIONS = {
    "computer_use",
    "host_execution",
    "host_python",
    "host_shell",
}
_MEDIUM_RISK_ACTIONS = {
    "browser_automation",
    "code_execute",
    "node_executor",
    "python_executor",
    "shell_executor",
}
_HIGH_RISK_COMMAND_CLASSES = {
    "code",
    "destructive",
    "shell",
    "system",
}
_MEDIUM_RISK_COMMAND_CLASSES = {
    "network",
    "write",
}


def stable_hash(value: str) -> str:
    """Return a deterministic SHA-256 hash for payload fingerprinting."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def make_state_reference(
    *,
    screenshot_hash: str = "",
    window_title: str = "",
    command_hash: str = "",
    policy_decision: str = "",
) -> dict[str, str]:
    """Create standardized before/after state references for auditability."""
    return {
        "screenshot_hash": screenshot_hash or "",
        "window_title": window_title or "",
        "command_hash": command_hash or "",
        "policy_decision": policy_decision or "",
    }


def build_action_event(
    *,
    source: str,
    action_type: str,
    status: str,
    before_state: dict[str, str] | None = None,
    after_state: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build a standardized action event payload."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id or str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "action_type": action_type,
        "status": status,
        "before_state": make_state_reference(**(before_state or {})),
        "after_state": make_state_reference(**(after_state or {})),
        "metadata": metadata or {},
    }
    return payload


def classify_runtime_class(runtime: str, *, fallback_used: bool = False) -> str:
    """Map an execution backend into an explicit runtime class."""
    if fallback_used:
        return RUNTIME_CLASS_HYBRID_FALLBACK

    normalized = (runtime or "").strip().lower()
    aliases = {
        "docker": RUNTIME_CLASS_SANDBOXED_DOCKER,
        "hands": RUNTIME_CLASS_SANDBOXED_DOCKER,
        "sandbox": RUNTIME_CLASS_SANDBOXED_DOCKER,
        RUNTIME_CLASS_SANDBOXED_DOCKER: RUNTIME_CLASS_SANDBOXED_DOCKER,
        "host": RUNTIME_CLASS_NATIVE_HOST,
        "native": RUNTIME_CLASS_NATIVE_HOST,
        RUNTIME_CLASS_NATIVE_HOST: RUNTIME_CLASS_NATIVE_HOST,
        "hybrid": RUNTIME_CLASS_HYBRID_FALLBACK,
        RUNTIME_CLASS_HYBRID_FALLBACK: RUNTIME_CLASS_HYBRID_FALLBACK,
    }
    return aliases.get(normalized, normalized or "unknown")


def classify_risk_class(
    *,
    action_type: str,
    command_class: str = "",
    network_enabled: bool = False,
    file_system_write: bool = False,
) -> str:
    """Return an explicit risk class for an execution action."""
    normalized_action = (action_type or "").strip().lower().split(".", 1)[0]
    normalized_command_class = (command_class or "").strip().lower()

    if normalized_action in _HIGH_RISK_ACTIONS or normalized_command_class in _HIGH_RISK_COMMAND_CLASSES:
        return "high"

    if (
        normalized_action in _MEDIUM_RISK_ACTIONS
        or normalized_command_class in _MEDIUM_RISK_COMMAND_CLASSES
        or network_enabled
        or file_system_write
    ):
        return "medium"

    return "low" if normalized_action else "high"


def build_execution_action_event(
    *,
    source: str,
    action_type: str,
    status: str,
    runtime_class: str,
    risk_class: str,
    before_state: dict[str, str] | None = None,
    after_state: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build a standardized action event with runtime and risk classifications."""
    merged_metadata = {
        "runtime_class": runtime_class,
        "risk_class": risk_class,
    }
    if metadata:
        merged_metadata.update(metadata)

    return build_action_event(
        source=source,
        action_type=action_type,
        status=status,
        before_state=before_state,
        after_state=after_state,
        metadata=merged_metadata,
        event_id=event_id,
    )


def normalize_action_event(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize an incoming payload to the shared action event schema."""
    payload = payload or {}
    return build_action_event(
        source=str(payload.get("source", "unknown")),
        action_type=str(payload.get("action_type", payload.get("action", "unknown"))),
        status=str(payload.get("status", "unknown")),
        before_state=payload.get("before_state") or payload.get("before") or {},
        after_state=payload.get("after_state") or payload.get("after") or {},
        metadata=payload.get("metadata") or {},
        event_id=payload.get("event_id"),
    )


def dumps_action_event(payload: dict[str, Any]) -> str:
    """Serialize standardized action events as compact JSON."""
    return json.dumps(normalize_action_event(payload), separators=(",", ":"), sort_keys=True)
