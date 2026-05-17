from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from threading import RLock
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
        self._agent: NestedMV2Agent | None = None
        self._agent_lock = RLock()

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
        raw_body: bytes | None = None,
        channel_id: str | None = None,
        send: bool | None = None,
        headers: Mapping[str, str] | None = None,
        require_signature: bool = False,
    ) -> ChannelProcessResult:
        channel = self._resolve_channel(provider=provider, channel_id=channel_id)
        if not channel.enabled:
            raise ChannelPayloadError(f"Channel is disabled: {channel.id}")
        channel = self._with_resolved_secrets(channel)
        _verify_channel_signature(channel, raw_body or b"", headers or {}, require_signature=require_signature)
        adapter = self._adapter_for(channel.provider)
        inbound = adapter.parse_inbound(channel, payload)
        self._event("channel.receive", inbound.to_public_dict())
        with self._agent_lock:
            agent = self._agent_for_hot_path()
            turn = agent.chat(inbound.text, session_id=inbound.session_id, source=inbound.to_turn_source())

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

    def close(self) -> None:
        with self._agent_lock:
            agent = self._agent
            self._agent = None
        if agent is not None:
            agent.close()

    def _agent_for_hot_path(self) -> NestedMV2Agent:
        if self._agent is None:
            self._agent = self.agent_factory(self.config)
        return self._agent

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
            token_env="TELEGRAM_BOT_TOKEN",  # nosec B106
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
    raw_body: bytes,
    headers: Mapping[str, str],
    *,
    require_signature: bool = False,
) -> None:
    secret_env = channel.settings.get("signature_secret_env")
    if not isinstance(secret_env, str) or not secret_env.strip():
        if require_signature and not _unsigned_allowed(channel):
            raise ChannelPayloadError("Unsigned webhooks are disabled for this public endpoint.")
        return
    secret = _channel_secret_value(channel, secret_env.strip())
    if not secret:
        raise ChannelPayloadError(f"Missing webhook signature secret environment variable: {secret_env}")
    provider = str(channel.settings.get("signature_provider") or channel.provider).strip().lower()
    if provider == "discord":
        _verify_discord_signature(channel, raw_body, headers)
        return
    if provider == "stripe":
        _verify_stripe_signature(secret, raw_body, headers, channel)
        return
    header_name = _signature_header_name(channel, provider)
    supplied = _header(headers, header_name)
    if not supplied:
        raise ChannelPayloadError(f"Missing webhook signature header: {header_name}")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if supplied.startswith("sha256="):
        supplied = supplied.removeprefix("sha256=")
    if not hmac.compare_digest(supplied, expected):
        raise ChannelPayloadError("Invalid webhook signature.")


def _signature_header_name(channel: ChannelEndpointConfig, provider: str) -> str:
    configured = channel.settings.get("signature_header")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().lower()
    if provider == "github":
        return "x-hub-signature-256"
    return "x-kestrel-signature"


def _verify_stripe_signature(secret: str, raw_body: bytes, headers: Mapping[str, str], channel: ChannelEndpointConfig) -> None:
    supplied = _header(headers, "stripe-signature")
    if not supplied:
        raise ChannelPayloadError("Missing webhook signature header: stripe-signature")
    fields: dict[str, list[str]] = {}
    for part in supplied.split(","):
        key, _, value = part.partition("=")
        if key and value:
            fields.setdefault(key.strip(), []).append(value.strip())
    timestamp_text = fields.get("t", [""])[0]
    signatures = fields.get("v1", [])
    try:
        timestamp = int(timestamp_text)
    except ValueError as exc:
        raise ChannelPayloadError("Invalid Stripe signature timestamp.") from exc
    tolerance = int(channel.settings.get("stripe_tolerance_seconds", 300))
    if abs(int(time.time()) - timestamp) > tolerance:
        raise ChannelPayloadError("Stripe signature timestamp is outside the tolerance window.")
    signed_payload = str(timestamp).encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(signature, expected) for signature in signatures):
        raise ChannelPayloadError("Invalid webhook signature.")


def _verify_discord_signature(channel: ChannelEndpointConfig, raw_body: bytes, headers: Mapping[str, str]) -> None:
    public_key = str(channel.settings.get("discord_public_key") or "").strip()
    if not public_key:
        raise ChannelPayloadError("Discord webhook signatures require settings.discord_public_key; HMAC secrets are not supported.")
    timestamp = _header(headers, "x-signature-timestamp")
    supplied = _header(headers, "x-signature-ed25519")
    if not timestamp or not supplied:
        raise ChannelPayloadError("Missing Discord Ed25519 signature headers.")
    try:
        signing = __import__("nacl.signing", fromlist=["VerifyKey"])
        exceptions = __import__("nacl.exceptions", fromlist=["BadSignatureError"])
    except ImportError as exc:
        raise ChannelPayloadError("Discord signature verification requires the optional PyNaCl package.") from exc
    try:
        verify_key = signing.VerifyKey(bytes.fromhex(public_key))
        verify_key.verify(timestamp.encode("utf-8") + raw_body, bytes.fromhex(supplied))
    except (ValueError, exceptions.BadSignatureError) as exc:
        raise ChannelPayloadError("Invalid webhook signature.") from exc


def _header(headers: Mapping[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value).strip()
    return ""


def _unsigned_allowed(channel: ChannelEndpointConfig) -> bool:
    value = channel.settings.get("unsigned_allowed", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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
