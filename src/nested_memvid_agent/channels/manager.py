from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..agent import NestedMV2Agent
from ..app_factory import build_agent
from ..config import AgentConfig
from ..event_log import AgentEvent, JsonlEventLog
from .adapters import ChannelAdapter, ChannelPayloadError, GenericWebhookAdapter, default_adapters
from .models import (
    ChannelEndpointConfig,
    ChannelOutboundMessage,
    ChannelProcessResult,
)

AgentFactory = Callable[[AgentConfig], NestedMV2Agent]


class ChannelManager:
    """Normalize external channel messages into agent turns and optional replies."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_factory: AgentFactory = build_agent,
        adapters: Mapping[str, ChannelAdapter] | None = None,
        channel_configs: list[ChannelEndpointConfig] | None = None,
    ) -> None:
        self.config = config
        self.agent_factory = agent_factory
        self.adapters = dict(adapters or default_adapters())
        self.channels = {
            channel.id: channel
            for channel in (channel_configs if channel_configs is not None else load_channel_configs(config.channel_config_path))
        }
        self.event_log = JsonlEventLog(config.log_dir / "events.jsonl")

    def list_channels(self) -> list[dict[str, Any]]:
        return [channel.to_public_dict() for channel in sorted(self.channels.values(), key=lambda item: item.id)]

    def handle_payload(
        self,
        *,
        provider: str,
        payload: dict[str, Any],
        channel_id: str | None = None,
        send: bool | None = None,
    ) -> ChannelProcessResult:
        channel = self._resolve_channel(provider=provider, channel_id=channel_id)
        if not channel.enabled:
            raise ChannelPayloadError(f"Channel is disabled: {channel.id}")
        adapter = self._adapter_for(channel.provider)
        inbound = adapter.parse_inbound(channel, payload)
        self._event("channel.receive", inbound.to_public_dict())
        agent = self.agent_factory(self.config)
        try:
            turn = agent.chat(inbound.text, session_id=inbound.session_id, source=inbound.to_turn_source())
        finally:
            agent.close()

        outbound = ChannelOutboundMessage(
            channel=inbound.channel,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            reply_to_message_id=inbound.message_id,
            text=turn.assistant_message,
            metadata={"stop_reason": turn.stop_reason},
        )
        requested_send = channel.auto_reply if send is None else send
        dry_run, blocked_reason = self._delivery_gate(channel, requested=requested_send)
        delivery = adapter.deliver(
            channel,
            outbound,
            dry_run=dry_run,
            timeout_seconds=self.config.channel_send_timeout_seconds,
            blocked_reason=blocked_reason,
        )
        result = ChannelProcessResult(inbound=inbound, outbound=outbound, delivery=delivery, turn=turn)
        self._event(
            "channel.deliver",
            {
                "channel": inbound.channel,
                "channel_id": inbound.channel_id,
                "conversation_id": inbound.conversation_id,
                "sent": delivery.sent,
                "dry_run": delivery.dry_run,
                "error": delivery.error,
                "blocked_reason": delivery.blocked_reason,
            },
        )
        return result

    def _resolve_channel(self, *, provider: str, channel_id: str | None) -> ChannelEndpointConfig:
        if channel_id:
            channel = self.channels.get(channel_id)
            if channel is not None:
                return channel
            return ChannelEndpointConfig(id=channel_id, provider=provider)
        channel = self.channels.get(provider)
        if channel is not None:
            return channel
        for candidate in self.channels.values():
            if candidate.provider == provider:
                return candidate
        return ChannelEndpointConfig(id=provider, provider=provider)

    def _adapter_for(self, provider: str) -> ChannelAdapter:
        return self.adapters.get(provider, self.adapters.get("generic", GenericWebhookAdapter()))

    def _delivery_gate(self, channel: ChannelEndpointConfig, *, requested: bool) -> tuple[bool, str | None]:
        if not requested:
            return True, "delivery_not_requested"
        if not self.config.enable_channel_delivery:
            return True, "global_channel_delivery_disabled"
        if not channel.send_enabled:
            return True, "channel_send_disabled"
        return False, None

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_log.append(AgentEvent(type=event_type, payload=payload))


def load_channel_configs(path: Path) -> list[ChannelEndpointConfig]:
    if not path.exists():
        return default_channel_configs()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("channels", [])
    else:
        items = []
    configs = [ChannelEndpointConfig.from_mapping(item) for item in items if isinstance(item, dict)]
    return configs or default_channel_configs()


def default_channel_configs() -> list[ChannelEndpointConfig]:
    return [
        ChannelEndpointConfig(
            id="telegram",
            provider="telegram",
            token_env="TELEGRAM_BOT_TOKEN",
        ),
        ChannelEndpointConfig(
            id="discord",
            provider="discord",
            webhook_url_env="DISCORD_WEBHOOK_URL",
        ),
        ChannelEndpointConfig(
            id="webhook",
            provider="webhook",
            webhook_url_env="NEST_AGENT_CHANNEL_WEBHOOK_URL",
        ),
    ]
