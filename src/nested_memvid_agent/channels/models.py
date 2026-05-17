from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..runtime_models import AgentTurnResult, TurnSource

_SESSION_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


@dataclass(frozen=True)
class ChannelEndpointConfig:
    """Configuration for one inbound/outbound channel endpoint."""

    id: str
    provider: str
    enabled: bool = True
    send_enabled: bool = False
    auto_reply: bool = False
    token_env: str | None = None
    webhook_url_env: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> ChannelEndpointConfig:
        provider = str(raw.get("provider", raw.get("type", "webhook"))).strip().lower() or "webhook"
        channel_id = str(raw.get("id", provider)).strip() or provider
        settings = raw.get("settings", {})
        return cls(
            id=channel_id,
            provider=provider,
            enabled=_as_bool(raw.get("enabled"), True),
            send_enabled=_as_bool(raw.get("send_enabled"), False),
            auto_reply=_as_bool(raw.get("auto_reply"), False),
            token_env=_as_optional_str(raw.get("token_env")),
            webhook_url_env=_as_optional_str(raw.get("webhook_url_env")),
            settings=dict(settings) if isinstance(settings, dict) else {},
        )

    def to_public_dict(self) -> dict[str, Any]:
        settings = {
            key: ("<configured>" if "url" in key.lower() or "token" in key.lower() else value)
            for key, value in self.settings.items()
            if not key.startswith("_")
        }
        return {
            **asdict(self),
            "settings": settings,
        }


@dataclass(frozen=True)
class ChannelInboundMessage:
    channel: str
    channel_id: str
    conversation_id: str
    text: str
    user_id: str | None = None
    message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        return "channel:" + _safe_session_part(self.channel_id) + ":" + _safe_session_part(self.conversation_id)

    def to_turn_source(self) -> TurnSource:
        return TurnSource(
            channel=self.channel,
            channel_id=self.channel_id,
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            message_id=self.message_id,
            metadata=self.metadata,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "channel_id": self.channel_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "text": self.text,
            "metadata": self.metadata,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class ChannelOutboundMessage:
    channel: str
    channel_id: str
    conversation_id: str
    text: str
    reply_to_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelDelivery:
    channel: str
    channel_id: str
    conversation_id: str
    sent: bool
    dry_run: bool
    endpoint: str
    request_json: dict[str, Any]
    status_code: int | None = None
    response_text: str = ""
    error: str | None = None
    blocked_reason: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChannelProcessResult:
    inbound: ChannelInboundMessage
    outbound: ChannelOutboundMessage
    delivery: ChannelDelivery
    turn: AgentTurnResult

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "channel": self.inbound.channel,
            "channel_id": self.inbound.channel_id,
            "conversation_id": self.inbound.conversation_id,
            "session_id": self.turn.session_id,
            "user_message": self.turn.user_message,
            "assistant_message": self.turn.assistant_message,
            "stop_reason": self.turn.stop_reason,
            "context_chars": self.turn.context_chars,
            "tool_count": len(self.turn.tool_executions),
            "memory_writes": list(self.turn.memory_writes),
            "delivery": self.delivery.to_public_dict(),
        }


def _safe_session_part(value: str) -> str:
    safe = _SESSION_SAFE_RE.sub("_", value.strip())
    return safe[:120] or "unknown"


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
