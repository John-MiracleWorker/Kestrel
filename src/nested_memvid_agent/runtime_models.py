from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ChatMessage:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_openai_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload


@dataclass(frozen=True)
class StrategyProposal:
    changed_strategy: str
    why_different: str = ""
    expected_signal: str = ""
    fallback_if_fails: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "changed_strategy": self.changed_strategy,
            "why_different": self.why_different,
            "expected_signal": self.expected_signal,
            "fallback_if_fails": self.fallback_if_fails,
        }


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: f"tool_{uuid4().hex}")
    strategy: StrategyProposal | None = None


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any | None = None
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class LLMOptions:
    stream: bool = False
    timeout_seconds: int = 60
    max_retries: int = 2
    temperature: float = 0.2


LLMStreamEventType = Literal[
    "token",
    "tool_call_delta",
    "tool_call",
    "message_complete",
    "provider_error",
    "usage",
]


@dataclass(frozen=True)
class LLMStreamEvent:
    type: LLMStreamEventType
    content: str = ""
    tool_call: ToolCall | None = None
    response: LLMResponse | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: Literal["low", "medium", "high", "critical"] = "low"
    requires_approval: bool = False
    source: Literal["builtin", "mcp", "skill"] = "builtin"
    server_id: str | None = None
    skill_id: str | None = None
    capabilities: tuple[str, ...] = ()
    produces_validation: bool = False

    def to_prompt_block(self) -> str:
        return (
            f"Tool: {self.name}\n"
            f"Risk: {self.risk}\n"
            f"Requires approval: {self.requires_approval}\n"
            f"Source: {self.source}\n"
            f"Description: {self.description}\n"
            f"Parameters JSON schema: {self.parameters}"
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            "source": self.source,
            "server_id": self.server_id,
            "skill_id": self.skill_id,
            "capabilities": list(self.capabilities),
            "produces_validation": self.produces_validation,
        }


@dataclass(frozen=True)
class ToolExecution:
    call: ToolCall
    success: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class TurnSource:
    """Provenance for a user turn that entered outside the direct CLI/API chat path."""

    channel: str
    channel_id: str
    conversation_id: str
    user_id: str | None = None
    message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentTurnResult:
    session_id: str
    user_message: str
    assistant_message: str
    tool_executions: tuple[ToolExecution, ...]
    context_chars: int
    memory_writes: tuple[str, ...]
    stop_reason: str
    context_prompt: str = ""
    source: TurnSource | None = None
    run_id: str = ""
    error: dict[str, Any] | None = None
    proof_of_work: dict[str, Any] | None = None
