from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from typing import Any, Literal
from uuid import uuid4

MessageRole = Literal["system", "user", "assistant", "tool"]
_SESSION_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def new_tool_call_id() -> str:
    """Return a collision-resistant ID when an upstream provider omits one."""

    return f"tool_{uuid4().hex}"


@dataclass(frozen=True)
class ChatMessage:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_openai_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.role == "assistant" and self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": dumps(
                            call.arguments,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in self.tool_calls
            ]
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
    id: str = field(default_factory=new_tool_call_id)
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
    aliases: tuple[str, ...] = ()

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
            "aliases": list(self.aliases),
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

    @property
    def session_id(self) -> str:
        return durable_channel_session_id(
            channel=self.channel,
            channel_id=self.channel_id,
            conversation_id=self.conversation_id,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> TurnSource:
        required: dict[str, str] = {}
        for field_name in ("channel", "channel_id", "conversation_id"):
            value = raw.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Turn source {field_name} must be a non-empty string.")
            required[field_name] = value
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("Turn source metadata must be a mapping.")
        return cls(
            **required,
            user_id=_optional_source_string(raw.get("user_id"), field_name="user_id"),
            message_id=_optional_source_string(raw.get("message_id"), field_name="message_id"),
            metadata=dict(metadata),
        )


def _optional_source_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Turn source {field_name} must be a string or null.")
    return value


def durable_channel_session_id(*, channel: str, channel_id: str, conversation_id: str) -> str:
    """Return a stable, collision-resistant ID for one channel conversation."""

    safe_channel_id = _safe_session_part(channel_id, max_chars=120)
    safe_conversation_id = _safe_session_part(conversation_id, max_chars=120)
    legacy_id = f"channel:{safe_channel_id}:{safe_conversation_id}"
    if _legacy_session_part_is_lossless(channel_id) and _legacy_session_part_is_lossless(
        conversation_id
    ):
        return legacy_id

    original_tuple = dumps(
        [channel, channel_id, conversation_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = sha256(original_tuple).hexdigest()[:32]
    return f"{legacy_id}:v2:{digest}"


def _safe_session_part(value: str, *, max_chars: int) -> str:
    safe = _SESSION_SAFE_RE.sub("_", value.strip())
    return safe[:max_chars] or "unknown"


def _legacy_session_part_is_lossless(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and len(value) <= 120
        and ":" not in value
        and _SESSION_SAFE_RE.search(value) is None
    )


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
