"""
Agent type definitions — dataclasses for tasks, steps, plans, and tool calls.

These are the core domain objects shared across the agent runtime.
All state is serializable to/from JSON for database persistence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ── Enums ────────────────────────────────────────────────────────────


class TaskStatus(str, Enum):
    """Agent task lifecycle states."""
    PLANNING = "planning"
    EXECUTING = "executing"
    OBSERVING = "observing"
    REFLECTING = "reflecting"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    """Individual step states."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


class RiskLevel(str, Enum):
    """Tool risk classification."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(str, Enum):
    """Human approval states."""
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


# ── Tool Types ───────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    """Schema for a tool available to the agent."""
    name: str
    description: str
    parameters: dict[str, Any]       # JSON Schema
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False  # Override: always require human approval
    timeout_seconds: int = 30
    category: str = "general"        # code, web, file, data, memory, control

    def to_openai_schema(self) -> dict:
        """Convert to OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    """A tool invocation requested by the LLM."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Result from executing a tool."""
    tool_call_id: str
    success: bool
    output: str = ""
    error: str = ""
    execution_time_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Task Planning ────────────────────────────────────────────────────


@dataclass
class TaskStep:
    """A single step in an agent task plan."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    index: int = 0
    description: str = ""
    expected_tools: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "index": self.index,
            "description": self.description,
            "expected_tools": self.expected_tools,
            "depends_on": self.depends_on,
            "status": self.status.value,
            "tool_calls": self.tool_calls,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TaskStep:
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            index=data.get("index", 0),
            description=data.get("description", ""),
            expected_tools=data.get("expected_tools", []),
            depends_on=data.get("depends_on", []),
            status=StepStatus(data.get("status", "pending")),
            tool_calls=data.get("tool_calls", []),
            result=data.get("result"),
            error=data.get("error"),
            attempts=data.get("attempts", 0),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
        )


@dataclass
class TaskPlan:
    """A DAG of steps decomposed from a user goal."""
    goal: str
    steps: list[TaskStep] = field(default_factory=list)
    reasoning: str = ""             # LLM's reasoning for this plan
    revision_count: int = 0         # How many times the plan was revised

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "reasoning": self.reasoning,
            "revision_count": self.revision_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TaskPlan:
        return cls(
            goal=data.get("goal", ""),
            steps=[TaskStep.from_dict(s) for s in data.get("steps", [])],
            reasoning=data.get("reasoning", ""),
            revision_count=data.get("revision_count", 0),
        )

    @property
    def current_step(self) -> Optional[TaskStep]:
        """Get the next step that should be executed."""
        for step in self.steps:
            if step.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS):
                # Check dependencies are complete
                deps_met = all(
                    any(s.id == dep and s.status == StepStatus.COMPLETE for s in self.steps)
                    for dep in step.depends_on
                )
                if deps_met:
                    return step
        return None

    @property
    def is_complete(self) -> bool:
        return all(
            s.status in (StepStatus.COMPLETE, StepStatus.SKIPPED)
            for s in self.steps
        )

    @property
    def progress(self) -> tuple[int, int]:
        done = sum(1 for s in self.steps if s.status in (StepStatus.COMPLETE, StepStatus.SKIPPED))
        return done, len(self.steps)


# ── Agent Task ───────────────────────────────────────────────────────


@dataclass
class GuardrailConfig:
    """Safety and resource limits for a task."""
    max_iterations: int = 25
    max_tokens: int = 100_000
    max_tool_calls: int = 50
    max_wall_time_seconds: int = 600
    auto_approve_risk: RiskLevel = RiskLevel.MEDIUM
    blocked_patterns: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    require_approval_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "max_iterations": self.max_iterations,
            "max_tokens": self.max_tokens,
            "max_tool_calls": self.max_tool_calls,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "auto_approve_risk": self.auto_approve_risk.value,
            "blocked_patterns": self.blocked_patterns,
            "allowed_domains": self.allowed_domains,
            "require_approval_tools": self.require_approval_tools,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GuardrailConfig:
        return cls(
            max_iterations=data.get("max_iterations", 25),
            max_tokens=data.get("max_tokens", 100_000),
            max_tool_calls=data.get("max_tool_calls", 50),
            max_wall_time_seconds=data.get("max_wall_time_seconds", 600),
            auto_approve_risk=RiskLevel(data.get("auto_approve_risk", "medium")),
            blocked_patterns=data.get("blocked_patterns", []),
            allowed_domains=data.get("allowed_domains", []),
            require_approval_tools=data.get("require_approval_tools", []),
        )


@dataclass
class ApprovalRequest:
    """A pending request for human approval."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    step_id: Optional[str] = None
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.HIGH
    reason: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AgentTask:
    """Top-level agent task — the unit of autonomous work."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    workspace_id: str = ""
    conversation_id: Optional[str] = None
    goal: str = ""
    status: TaskStatus = TaskStatus.PLANNING
    plan: Optional[TaskPlan] = None
    config: GuardrailConfig = field(default_factory=GuardrailConfig)
    result: Optional[str] = None
    error: Optional[str] = None
    token_usage: int = 0
    tool_calls_count: int = 0
    iterations: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    # Runtime state (not persisted to DB)
    pending_approval: Optional[ApprovalRequest] = None
    messages: list[dict[str, Any]] = field(default_factory=list)  # Conversation history


# ── Task Events (streamed to clients) ────────────────────────────────


class TaskEventType(str, Enum):
    """Event types streamed during task execution."""
    PLAN_CREATED = "plan_created"
    STEP_STARTED = "step_started"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    STEP_COMPLETE = "step_complete"
    APPROVAL_NEEDED = "approval_needed"
    THINKING = "thinking"
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"
    TASK_PAUSED = "task_paused"


@dataclass
class TaskEvent:
    """Real-time event from agent execution."""
    type: TaskEventType
    task_id: str
    step_id: Optional[str] = None
    content: str = ""              # Thinking text, result, error
    tool_name: Optional[str] = None
    tool_args: Optional[str] = None  # JSON string
    tool_result: Optional[str] = None
    approval_id: Optional[str] = None
    progress: Optional[dict] = None  # {current_step, total_steps, iterations, ...}

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "content": self.content,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "tool_result": self.tool_result,
            "approval_id": self.approval_id,
            "progress": self.progress,
        }
