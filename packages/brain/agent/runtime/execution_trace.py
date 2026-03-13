"""Helpers for canonical execution traces and audit entries."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from core.shared_schemas import (
    dumps_action_event,
    normalize_action_event,
    stable_hash,
)


def write_execution_audit_entry(
    *,
    exec_id: str,
    workspace_id: str,
    user_id: str,
    tool_name: str,
    function_name: str,
    arguments: str,
    status: str,
    runtime_class: str,
    risk_class: str,
    action_events: list[dict[str, Any]] | None = None,
    execution_time_ms: int = 0,
    memory_used_mb: int = 0,
    error: str = "",
    exit_code: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an execution audit entry using the shared action-event schema."""
    audit_dir = os.getenv("KESTREL_AUDIT_LOG_DIR", os.path.expanduser("~/.kestrel/audit"))
    os.makedirs(audit_dir, exist_ok=True)

    normalized_events = [normalize_action_event(event) for event in (action_events or [])]
    if normalized_events:
        started_at = normalized_events[0].get("timestamp", datetime.now(timezone.utc).isoformat())
        completed_at = normalized_events[-1].get("timestamp", datetime.now(timezone.utc).isoformat())
    else:
        now = datetime.now(timezone.utc).isoformat()
        started_at = now
        completed_at = now

    entry = {
        "exec_id": exec_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "skill_name": tool_name,
        "function_name": function_name,
        "arguments_hash": stable_hash(arguments),
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "execution_time_ms": execution_time_ms,
        "memory_used_mb": memory_used_mb,
        "runtime_class": runtime_class,
        "risk_class": risk_class,
        "action_events": normalized_events,
        "metadata": metadata or {},
    }
    if error:
        entry["error"] = error
    if exit_code is not None:
        entry["exit_code"] = exit_code

    log_file = os.path.join(
        audit_dir,
        f"audit-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl",
    )
    with open(log_file, "a", encoding="utf-8") as file_handle:
        file_handle.write(json.dumps(entry) + "\n")

    return entry


def attach_execution_trace(
    result: dict[str, Any],
    *,
    runtime_class: str,
    risk_class: str,
    action_events: list[dict[str, Any]],
    fallback_used: bool = False,
    fallback_from: str = "",
    fallback_to: str = "",
    fallback_reason: str = "",
) -> dict[str, Any]:
    """Attach canonical execution-trace fields to a runtime/tool result."""
    normalized_events = [normalize_action_event(event) for event in action_events]
    final_event = normalized_events[-1] if normalized_events else {}

    traced = dict(result)
    traced["runtime_class"] = runtime_class
    traced["risk_class"] = risk_class
    traced["action_events"] = normalized_events
    traced["action_event_json"] = dumps_action_event(final_event) if final_event else ""
    traced["fallback_used"] = fallback_used
    traced["fallback_from"] = fallback_from or ""
    traced["fallback_to"] = fallback_to or ""
    if fallback_reason:
        traced["fallback_reason"] = fallback_reason
    return traced
