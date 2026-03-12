from __future__ import annotations

"""
Autonomy runtime domain types for queue-backed execution.

These types intentionally stay small and JSON-friendly so they can be used
across persistence, runtime orchestration, and tool execution.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _coerce_dict(value: Any) -> dict:
    """Safely coerce a DB value (could be dict, JSON string, or None) to a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return dict(value)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_timestamp(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return [value]
        return parsed if isinstance(parsed, list) else [value]
    return list(value)


@dataclass(frozen=True)
class WorkspaceAgentProfile:
    id: str
    workspace_id: str
    default_mode: str = "ops"
    kernel_preset: str = "ops"
    autonomy_policy: str = "moderate"
    memory_namespace: str = ""
    tool_policy_bundle: tuple[str, ...] = ()
    persona_version: int = 1
    runtime_defaults: dict[str, Any] = field(default_factory=dict)
    kernel_policy_json: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_record(cls, row: Any) -> "WorkspaceAgentProfile":
        bundle = row.get("tool_policy_bundle") if isinstance(row, dict) else row["tool_policy_bundle"]
        defaults = row.get("runtime_defaults") if isinstance(row, dict) else row["runtime_defaults"]
        kernel_policy = (
            row.get("kernel_policy_json")
            if isinstance(row, dict)
            else (row["kernel_policy_json"] if "kernel_policy_json" in row.keys() else None)
        )
        created_at = row.get("created_at") if isinstance(row, dict) else row["created_at"]
        updated_at = row.get("updated_at") if isinstance(row, dict) else row["updated_at"]
        default_mode = row.get("default_mode") if isinstance(row, dict) else row["default_mode"]
        kernel_preset = (
            row.get("kernel_preset")
            if isinstance(row, dict)
            else (row["kernel_preset"] if "kernel_preset" in row.keys() else None)
        )
        return cls(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            default_mode=default_mode or "ops",
            kernel_preset=kernel_preset or default_mode or "ops",
            autonomy_policy=row["autonomy_policy"] or "moderate",
            memory_namespace=row["memory_namespace"] or f"workspace:{row['workspace_id']}",
            tool_policy_bundle=tuple(bundle or ()),
            persona_version=int(row["persona_version"] or 1),
            runtime_defaults=_coerce_dict(defaults),
            kernel_policy_json=_coerce_dict(kernel_policy),
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
class CapabilityGrant:
    grant_id: str
    scope: str = "workspace"
    workspace_id: str = ""
    user_id: str = ""
    agent_profile_id: str = ""
    channel: str = ""
    action_selector: str = ""
    tool_selector: str = ""
    approval_state: str = ""
    expires_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "CapabilityGrant":
        if isinstance(value, CapabilityGrant):
            return value
        payload = _coerce_dict(value)
        return cls(
            grant_id=str(payload.get("grant_id") or uuid.uuid4()),
            scope=str(payload.get("scope") or "workspace"),
            workspace_id=str(payload.get("workspace_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            agent_profile_id=str(payload.get("agent_profile_id") or ""),
            channel=str(payload.get("channel") or ""),
            action_selector=str(payload.get("action_selector") or ""),
            tool_selector=str(payload.get("tool_selector") or ""),
            approval_state=str(payload.get("approval_state") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            metadata=_coerce_dict(payload.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "scope": self.scope,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "agent_profile_id": self.agent_profile_id,
            "channel": self.channel,
            "action_selector": self.action_selector,
            "tool_selector": self.tool_selector,
            "approval_state": self.approval_state,
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
        }

    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        if not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return expires <= (now or datetime.now(timezone.utc))

    def matches(
        self,
        *,
        workspace_id: str = "",
        user_id: str = "",
        agent_profile_id: str = "",
        channel: str = "",
        action_name: str = "",
        tool_name: str = "",
    ) -> bool:
        if self.workspace_id and self.workspace_id != workspace_id:
            return False
        if self.user_id and self.user_id != user_id:
            return False
        if self.agent_profile_id and self.agent_profile_id != agent_profile_id:
            return False
        if self.channel and self.channel != channel:
            return False
        if self.is_expired():
            return False
        if self.action_selector and self.action_selector not in {"*", action_name, tool_name}:
            return False
        if self.tool_selector and self.tool_selector not in {"*", tool_name, action_name}:
            return False
        return True


@dataclass(frozen=True)
class SessionRoute:
    channel: str = ""
    external_conversation_id: str = ""
    external_thread_id: str = ""
    return_route: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "SessionRoute":
        payload = _coerce_dict(value)
        return cls(
            channel=str(payload.get("channel") or ""),
            external_conversation_id=str(payload.get("external_conversation_id") or ""),
            external_thread_id=str(payload.get("external_thread_id") or ""),
            return_route=_coerce_dict(payload.get("return_route")),
            metadata=_coerce_dict(payload.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "external_conversation_id": self.external_conversation_id,
            "external_thread_id": self.external_thread_id,
            "return_route": dict(self.return_route),
            "metadata": dict(self.metadata),
        }


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
            payload_json=_coerce_dict(payload_json),
            checkpoint_json=_coerce_dict(checkpoint_json),
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
    capability_grants: tuple[CapabilityGrant, ...] = ()
    route: Optional[SessionRoute] = None
    autonomy_policy: str = "moderate"
    kernel_preset: str = "ops"
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
        capability_grants: Optional[list[dict[str, Any]] | tuple[dict[str, Any], ...]] = None,
        route: Optional[dict[str, Any] | SessionRoute] = None,
        autonomy_policy: str = "moderate",
        kernel_preset: str = "ops",
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
            capability_grants=tuple(
                CapabilityGrant.from_value(grant) for grant in _coerce_list(capability_grants)
            ),
            route=SessionRoute.from_value(route) if route else None,
            autonomy_policy=autonomy_policy,
            kernel_preset=kernel_preset,
            cancellation_token=cancellation_token,
            approval_mode=approval_mode,
            services=dict(services or {}),
        )

    def has_capability(
        self,
        *,
        action_name: str = "",
        tool_name: str = "",
        channel: str = "",
        require_approval: bool = False,
    ) -> bool:
        for grant in self.capability_grants:
            if require_approval and grant.approval_state not in {"approved", "auto_approved", ""}:
                continue
            if grant.matches(
                workspace_id=self.workspace_id,
                user_id=self.user_id,
                agent_profile_id=self.agent_profile_id,
                channel=channel or self.source,
                action_name=action_name,
                tool_name=tool_name,
            ):
                return True
        return False

    def grants_for(self, *, action_name: str = "", tool_name: str = "", channel: str = "") -> list[dict[str, Any]]:
        return [
            grant.to_dict()
            for grant in self.capability_grants
            if grant.matches(
                workspace_id=self.workspace_id,
                user_id=self.user_id,
                agent_profile_id=self.agent_profile_id,
                channel=channel or self.source,
                action_name=action_name,
                tool_name=tool_name,
            )
        ]

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
            "capability_grants": [grant.to_dict() for grant in self.capability_grants],
        }
        if self.route:
            context["session_route"] = self.route.to_dict()
        context.update(self.services)
        return context
