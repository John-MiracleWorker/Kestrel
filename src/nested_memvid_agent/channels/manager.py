from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
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
SecretResolver = Callable[[str], str | None]


class ChannelManager:
    """Normalize external channel messages into agent turns and optional replies."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_factory: AgentFactory = build_agent,
        adapters: Mapping[str, ChannelAdapter] | None = None,
        channel_configs: list[ChannelEndpointConfig] | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.config = config
        self.agent_factory = agent_factory
        self.secret_resolver = secret_resolver
        self.adapters = dict(adapters or default_adapters())
        self.channels = {
            channel.id: channel
            for channel in (channel_configs if channel_configs is not None else load_channel_configs(config.channel_config_path))
        }
        self.event_log = JsonlEventLog(config.log_dir / "events.jsonl")

    def list_channels(self) -> list[dict[str, Any]]:
        return [channel.to_public_dict() for channel in sorted(self.channels.values(), key=lambda item: item.id)]

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        channel = self.channels.get(channel_id)
        if channel is None:
            raise KeyError(f"Unknown channel: {channel_id}")
        return channel.to_public_dict()

    def upsert_channel(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel = ChannelEndpointConfig.from_mapping(payload)
        self.channels[channel.id] = channel
        save_channel_configs(self.config.channel_config_path, list(self.channels.values()))
        return channel.to_public_dict()

    def delete_channel(self, channel_id: str) -> None:
        if channel_id not in self.channels:
            raise KeyError(f"Unknown channel: {channel_id}")
        del self.channels[channel_id]
        save_channel_configs(self.config.channel_config_path, list(self.channels.values()))

    def handle_payload(
        self,
        *,
        provider: str,
        payload: dict[str, Any],
        channel_id: str | None = None,
        send: bool | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ChannelProcessResult:
        channel = self._resolve_channel(provider=provider, channel_id=channel_id)
        if not channel.enabled:
            raise ChannelPayloadError(f"Channel is disabled: {channel.id}")
        channel = self._with_resolved_secrets(channel)
        _verify_channel_signature(channel, payload, headers or {})
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
            raise ChannelPayloadError(f"Unknown channel: {channel_id}")
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

    def _with_resolved_secrets(self, channel: ChannelEndpointConfig) -> ChannelEndpointConfig:
        if self.secret_resolver is None:
            return channel
        resolved: dict[str, str] = {}
        for name in _channel_secret_names(channel):
            value = self.secret_resolver(name)
            if value:
                resolved[name] = value
        if not resolved:
            return channel
        settings = dict(channel.settings)
        settings["_resolved_secrets"] = resolved
        return replace(channel, settings=settings)


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


def save_channel_configs(path: Path, channels: list[ChannelEndpointConfig]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channels": [
            {
                "id": channel.id,
                "provider": channel.provider,
                "enabled": channel.enabled,
                "send_enabled": channel.send_enabled,
                "auto_reply": channel.auto_reply,
                "token_env": channel.token_env,
                "webhook_url_env": channel.webhook_url_env,
                "settings": channel.settings,
            }
            for channel in sorted(channels, key=lambda item: item.id)
        ]
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def _verify_channel_signature(
    channel: ChannelEndpointConfig,
    payload: dict[str, Any],
    headers: Mapping[str, str],
) -> None:
    secret_env = channel.settings.get("signature_secret_env")
    if not isinstance(secret_env, str) or not secret_env.strip():
        return
    secret = _channel_secret_value(channel, secret_env.strip())
    if not secret:
        raise ChannelPayloadError(f"Missing webhook signature secret environment variable: {secret_env}")
    header_name = str(channel.settings.get("signature_header") or "x-kestrel-signature").lower()
    supplied = ""
    for key, value in headers.items():
        if str(key).lower() == header_name:
            supplied = str(value).strip()
            break
    if not supplied:
        raise ChannelPayloadError(f"Missing webhook signature header: {header_name}")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    if supplied.startswith("sha256="):
        supplied = supplied.removeprefix("sha256=")
    if not hmac.compare_digest(supplied, expected):
        raise ChannelPayloadError("Invalid webhook signature.")


def _channel_secret_names(channel: ChannelEndpointConfig) -> list[str]:
    names = [
        channel.token_env,
        channel.webhook_url_env,
        channel.settings.get("webhook_url_env"),
        channel.settings.get("signature_secret_env"),
    ]
    return [str(name).strip() for name in names if isinstance(name, str) and str(name).strip()]


def _channel_secret_value(channel: ChannelEndpointConfig, name: str) -> str:
    resolved = channel.settings.get("_resolved_secrets")
    if isinstance(resolved, dict):
        value = resolved.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return os.getenv(name, "")
