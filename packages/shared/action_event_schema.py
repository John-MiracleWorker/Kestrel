"""Shared action/event schema for Brain and Hands execution traces."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "action_event.v1"


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
