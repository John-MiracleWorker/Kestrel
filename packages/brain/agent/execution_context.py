from __future__ import annotations

"""
Autonomy runtime domain types for queue-backed execution.

These types intentionally stay small and JSON-friendly so they can be used
across persistence, runtime orchestration, and tool execution.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_timestamp(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


@dataclass(frozen=True)
class WorkspaceAgentProfile:
    id: str
    workspace_id: str
    default_mode: str = "ops"
    autonomy_policy: str = "moderate"
    memory_namespace: str = ""
    tool_policy_bundle: tuple[str, ...] = ()
    persona_version: int = 1
    runtime_defaults: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_record(cls, row: Any) -> "WorkspaceAgentProfile":
        bundle = row.get("tool_policy_bundle") if isinstance(row, dict) else row["tool_policy_bundle"]
        defaults = row.get("runtime_defaults") if isinstance(row, dict) else row["runtime_defaults"]
        created_at = row.get("created_at") if isinstance(row, dict) else row["created_at"]
        updated_at = row.get("updated_at") if isinstance(row, dict) else row["updated_at"]
        return cls(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            default_mode=row["default_mode"] or "ops",
            autonomy_policy=row["autonomy_policy"] or "moderate",
            memory_namespace=row["memory_namespace"] or f"workspace:{row['workspace_id']}",
            tool_policy_bundle=tuple(bundle or ()),
            persona_version=int(row["persona_version"] or 1),
            runtime_defaults=dict(defaults or {}),
            created_at=_coerce_timestamp(created_at),
            updated_at=_coerce_timestamp(updated_at),
        )


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    approval_required: bool
    risk: str
    scope: str
    rationale: str


@dataclass(frozen=True)
class TaskLease:
    worker_id: str
    expires_at: str


@dataclass(frozen=True)
class QueuedTaskRecord:
    id: str
    workspace_id: str
    user_id: str
    goal: str
    status: str
    priority: int
    source: str
    agent_task_id: Optional[str] = None
    agent_profile_id: Optional[str] = None
    payload_json: dict[str, Any] = field(default_factory=dict)
    checkpoint_json: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    parent_queue_id: Optional[str] = None
    trigger_kind: str = "task"
    terminal_task_id: Optional[str] = None
    scheduled_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    retry_count: int = 0
    max_retries: int = 3
    error: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_record(cls, row: Any) -> "QueuedTaskRecord":
        def _ts(name: str) -> str:
            value = row.get(name) if isinstance(row, dict) else row[name]
            return value.isoformat() if getattr(value, "isoformat", None) else (str(value) if value else "")

        payload_json = row.get("payload_json") if isinstance(row, dict) else row["payload_json"]
        checkpoint_json = row.get("checkpoint_json") if isinstance(row, dict) else row["checkpoint_json"]
        return cls(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            user_id=str(row["user_id"]),
            goal=row["goal"],
            status=row["status"],
            priority=int(row["priority"]),
            source=row["source"],
            agent_task_id=str(row["agent_task_id"]) if row["agent_task_id"] else None,
            agent_profile_id=str(row["agent_profile_id"]) if row["agent_profile_id"] else None,
            payload_json=dict(payload_json or {}),
            checkpoint_json=dict(checkpoint_json or {}),
            dedupe_key=row["dedupe_key"] or "",
            lease_owner=row["lease_owner"] or "",
            lease_expires_at=_ts("lease_expires_at"),
            parent_queue_id=str(row["parent_queue_id"]) if row["parent_queue_id"] else None,
            trigger_kind=row["trigger_kind"] or "task",
            terminal_task_id=str(row["terminal_task_id"]) if row["terminal_task_id"] else None,
            scheduled_at=_ts("scheduled_at"),
            started_at=_ts("started_at"),
            completed_at=_ts("completed_at"),
            retry_count=int(row["retry_count"] or 0),
            max_retries=int(row["max_retries"] or 3),
            error=row["error"] or "",
            created_at=_ts("created_at"),
            updated_at=_ts("updated_at"),
        )


@dataclass(frozen=True)
class Opportunity:
    id: str
    agent_profile_id: str
    source: str
    title: str
    goal_template: str
    score: float
    severity: str
    dedupe_key: str
    expires_at: str = ""
    state: str = "pending"
    payload_json: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def create(
        cls,
        *,
        agent_profile_id: str,
        source: str,
        title: str,
        goal_template: str,
        score: float,
        severity: str = "info",
        dedupe_key: str = "",
        expires_at: str = "",
        payload_json: Optional[dict[str, Any]] = None,
    ) -> "Opportunity":
        return cls(
            id=str(uuid.uuid4()),
            agent_profile_id=agent_profile_id,
            source=source,
            title=title,
            goal_template=goal_template,
            score=score,
            severity=severity,
            dedupe_key=dedupe_key,
            expires_at=expires_at,
            payload_json=dict(payload_json or {}),
            created_at=_utcnow_iso(),
        )


@dataclass(frozen=True)
class ExecutionContext:
    task_id: str
    queue_id: str
    agent_profile_id: str
    workspace_id: str
    user_id: str
    session_id: str
    source: str
    trace_id: str
    budgets: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    autonomy_policy: str = "moderate"
    cancellation_token: str = ""
    approval_mode: str = "policy"
    services: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        queue_id: str,
        agent_profile_id: str,
        workspace_id: str,
        user_id: str,
        session_id: str = "",
        source: str,
        budgets: Optional[dict[str, Any]] = None,
        permissions: Optional[dict[str, Any]] = None,
        autonomy_policy: str = "moderate",
        cancellation_token: str = "",
        approval_mode: str = "policy",
        services: Optional[dict[str, Any]] = None,
    ) -> "ExecutionContext":
        return cls(
            task_id=task_id,
            queue_id=queue_id,
            agent_profile_id=agent_profile_id,
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id or task_id,
            source=source,
            trace_id=str(uuid.uuid4()),
            budgets=dict(budgets or {}),
            permissions=dict(permissions or {}),
            autonomy_policy=autonomy_policy,
            cancellation_token=cancellation_token,
            approval_mode=approval_mode,
            services=dict(services or {}),
        )

    def to_tool_context(self) -> dict[str, Any]:
        context = {
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "execution_context": self,
            "task_id": self.task_id,
            "queue_id": self.queue_id,
            "session_id": self.session_id,
            "agent_profile_id": self.agent_profile_id,
            "source": self.source,
            "trace_id": self.trace_id,
        }
        context.update(self.services)
        return context
