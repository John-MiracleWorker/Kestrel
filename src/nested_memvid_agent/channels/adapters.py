from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..net_safety import public_url_allowed
from .models import (
    ChannelDelivery,
    ChannelEndpointConfig,
    ChannelInboundMessage,
    ChannelOutboundMessage,
)


class ChannelPayloadError(ValueError):
    """Raised when a channel payload cannot be normalized into a user message."""


class ChannelAdapter(ABC):
    provider: str = "generic"

    @abstractmethod
    def parse_inbound(self, config: ChannelEndpointConfig, payload: dict[str, Any]) -> ChannelInboundMessage:
        raise NotImplementedError

    @abstractmethod
    def build_delivery(
        self,
        config: ChannelEndpointConfig,
        outbound: ChannelOutboundMessage,
        *,
        dry_run: bool,
        blocked_reason: str | None = None,
    ) -> ChannelDelivery:
        raise NotImplementedError

    def deliver(
        self,
        config: ChannelEndpointConfig,
        outbound: ChannelOutboundMessage,
        *,
        dry_run: bool,
        timeout_seconds: int,
        blocked_reason: str | None = None,
    ) -> ChannelDelivery:
        delivery = self.build_delivery(config, outbound, dry_run=dry_run, blocked_reason=blocked_reason)
        if dry_run or delivery.error is not None:
            return delivery
        return _post_json(delivery, timeout_seconds=timeout_seconds)

    def notify_processing_started(
        self,
        config: ChannelEndpointConfig,
        inbound: ChannelInboundMessage,
        *,
        dry_run: bool,
        timeout_seconds: int,
        blocked_reason: str | None = None,
    ) -> ChannelDelivery | None:
        return None

    def notify_progress(
        self,
        config: ChannelEndpointConfig,
        inbound: ChannelInboundMessage,
        text: str,
        *,
        dry_run: bool,
        timeout_seconds: int,
        blocked_reason: str | None = None,
    ) -> ChannelDelivery | None:
        outbound = ChannelOutboundMessage(
            channel=inbound.channel,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            reply_to_message_id=inbound.message_id,
            text=text,
            metadata={"kind": "progress"},
        )
        return self.deliver(
            config,
            outbound,
            dry_run=dry_run,
            timeout_seconds=timeout_seconds,
            blocked_reason=blocked_reason,
        )


class TelegramAdapter(ChannelAdapter):
    provider = "telegram"

    def parse_inbound(self, config: ChannelEndpointConfig, payload: dict[str, Any]) -> ChannelInboundMessage:
        message_key, message = _first_dict(
            payload,
            ("message", "edited_message", "channel_post", "edited_channel_post", "business_message"),
        )
        if message is None:
            raise ChannelPayloadError("Telegram payload did not include a message object.")
        text = _first_text(message, ("text", "caption"))
        if text is None:
            raise ChannelPayloadError("Telegram message did not include text or caption.")
        chat = _dict_or_empty(message.get("chat"))
        sender = _dict_or_empty(message.get("from")) or _dict_or_empty(message.get("sender_chat"))
        conversation_id = _required_str(chat.get("id") or message.get("chat_id"), "Telegram chat id")
        message_id = _optional_str(message.get("message_id"))
        metadata: dict[str, Any] = {
            "provider_payload": message_key,
            "update_id": _optional_str(payload.get("update_id")),
        }
        if chat.get("type") is not None:
            metadata["chat_type"] = str(chat["type"])
        if message.get("message_thread_id") is not None:
            metadata["message_thread_id"] = str(message["message_thread_id"])
        return ChannelInboundMessage(
            channel=self.provider,
            channel_id=config.id,
            conversation_id=conversation_id,
            user_id=_optional_str(sender.get("id")),
            message_id=message_id,
            text=text,
            metadata={key: value for key, value in metadata.items() if value is not None},
        )

    def build_delivery(
        self,
        config: ChannelEndpointConfig,
        outbound: ChannelOutboundMessage,
        *,
        dry_run: bool,
        blocked_reason: str | None = None,
    ) -> ChannelDelivery:
        token_env = config.token_env or "TELEGRAM_BOT_TOKEN"
        token = _configured_secret(config, token_env)
        request_json: dict[str, Any] = {
            "chat_id": outbound.conversation_id,
            "text": outbound.text,
        }
        if outbound.reply_to_message_id:
            request_json["reply_parameters"] = {"message_id": _message_id_value(outbound.reply_to_message_id)}
        reply_markup = outbound.metadata.get("reply_markup")
        if isinstance(reply_markup, dict):
            request_json["reply_markup"] = reply_markup
        if not dry_run and not token:
            return ChannelDelivery(
                channel=self.provider,
                channel_id=config.id,
                conversation_id=outbound.conversation_id,
                sent=False,
                dry_run=False,
                endpoint="https://api.telegram.org/bot<token>/sendMessage",
                request_json=request_json,
                error=f"Missing Telegram bot token environment variable: {token_env}",
                blocked_reason=blocked_reason,
            )
        endpoint = "https://api.telegram.org/bot<token>/sendMessage"
        if not dry_run and token:
            request_json["_request_url"] = f"https://api.telegram.org/bot{token}/sendMessage"
        return ChannelDelivery(
            channel=self.provider,
            channel_id=config.id,
            conversation_id=outbound.conversation_id,
            sent=False,
            dry_run=dry_run,
            endpoint=endpoint,
            request_json=request_json,
            blocked_reason=blocked_reason,
        )

    def notify_processing_started(
        self,
        config: ChannelEndpointConfig,
        inbound: ChannelInboundMessage,
        *,
        dry_run: bool,
        timeout_seconds: int,
        blocked_reason: str | None = None,
    ) -> ChannelDelivery | None:
        token_env = config.token_env or "TELEGRAM_BOT_TOKEN"
        token = _configured_secret(config, token_env)
        request_json: dict[str, Any] = {
            "chat_id": inbound.conversation_id,
            "action": "typing",
        }
        if inbound.metadata.get("message_thread_id"):
            request_json["message_thread_id"] = _message_id_value(str(inbound.metadata["message_thread_id"]))
        if not dry_run and not token:
            return ChannelDelivery(
                channel=self.provider,
                channel_id=config.id,
                conversation_id=inbound.conversation_id,
                sent=False,
                dry_run=False,
                endpoint="https://api.telegram.org/bot<token>/sendChatAction",
                request_json=request_json,
                error=f"Missing Telegram bot token environment variable: {token_env}",
                blocked_reason=blocked_reason,
            )
        endpoint = "https://api.telegram.org/bot<token>/sendChatAction"
        if not dry_run and token:
            request_json["_request_url"] = f"https://api.telegram.org/bot{token}/sendChatAction"
        delivery = ChannelDelivery(
            channel=self.provider,
            channel_id=config.id,
            conversation_id=inbound.conversation_id,
            sent=False,
            dry_run=dry_run,
            endpoint=endpoint,
            request_json=request_json,
            blocked_reason=blocked_reason,
        )
        if dry_run or delivery.error is not None:
            return delivery
        return _post_json(delivery, timeout_seconds=timeout_seconds)


def telegram_api_request(
    config: ChannelEndpointConfig,
    method: str,
    payload: dict[str, Any],
    *,
    dry_run: bool,
    timeout_seconds: int,
    blocked_reason: str | None = None,
) -> ChannelDelivery:
    token_env = config.token_env or "TELEGRAM_BOT_TOKEN"
    token = _configured_secret(config, token_env)
    request_json = dict(payload)
    if not dry_run and not token:
        return ChannelDelivery(
            channel="telegram",
            channel_id=config.id,
            conversation_id="",
            sent=False,
            dry_run=False,
            endpoint=f"https://api.telegram.org/bot<token>/{method}",
            request_json=_redact_delivery_request(request_json),
            error=f"Missing Telegram bot token environment variable: {token_env}",
            blocked_reason=blocked_reason,
        )
    endpoint = f"https://api.telegram.org/bot<token>/{method}"
    if not dry_run and token:
        request_json["_request_url"] = f"https://api.telegram.org/bot{token}/{method}"
    delivery = ChannelDelivery(
        channel="telegram",
        channel_id=config.id,
        conversation_id="",
        sent=False,
        dry_run=dry_run,
        endpoint=endpoint,
        request_json=request_json,
        blocked_reason=blocked_reason,
    )
    if dry_run or delivery.error is not None:
        return _copy_delivery(delivery)
    return _post_json(delivery, timeout_seconds=timeout_seconds)


class DiscordAdapter(ChannelAdapter):
    provider = "discord"

    def parse_inbound(self, config: ChannelEndpointConfig, payload: dict[str, Any]) -> ChannelInboundMessage:
        root = _dict_or_empty(payload.get("d")) if isinstance(payload.get("d"), dict) else payload
        message = _dict_or_empty(root.get("message")) or root
        text = _first_text(message, ("content", "text"))
        data = _dict_or_empty(root.get("data"))
        if text is None and data:
            text = _discord_option_text(data.get("options")) or _optional_str(data.get("name"))
        if text is None:
            raise ChannelPayloadError("Discord payload did not include message content or command option text.")
        member = _dict_or_empty(root.get("member"))
        sender = _dict_or_empty(message.get("author")) or _dict_or_empty(root.get("user")) or _dict_or_empty(member.get("user"))
        conversation_id = _required_str(
            message.get("channel_id") or root.get("channel_id") or root.get("guild_id") or root.get("id"),
            "Discord channel or interaction id",
        )
        metadata: dict[str, Any] = {
            "guild_id": _optional_str(root.get("guild_id")),
            "interaction_token": _optional_str(root.get("token")),
            "application_id": _optional_str(root.get("application_id")),
            "event_type": _optional_str(payload.get("t") or root.get("type")),
        }
        return ChannelInboundMessage(
            channel=self.provider,
            channel_id=config.id,
            conversation_id=conversation_id,
            user_id=_optional_str(sender.get("id")),
            message_id=_optional_str(message.get("id") or root.get("id")),
            text=text,
            metadata={key: value for key, value in metadata.items() if value is not None},
        )

    def build_delivery(
        self,
        config: ChannelEndpointConfig,
        outbound: ChannelOutboundMessage,
        *,
        dry_run: bool,
        blocked_reason: str | None = None,
    ) -> ChannelDelivery:
        webhook_url = _configured_webhook_url(config, "DISCORD_WEBHOOK_URL")
        request_json = {"content": outbound.text}
        if not dry_run and webhook_url is None:
            return ChannelDelivery(
                channel=self.provider,
                channel_id=config.id,
                conversation_id=outbound.conversation_id,
                sent=False,
                dry_run=False,
                endpoint="discord_webhook_url",
                request_json=request_json,
                error="Missing Discord webhook URL. Set webhook_url_env or DISCORD_WEBHOOK_URL.",
                blocked_reason=blocked_reason,
            )
        if not dry_run and webhook_url:
            safe, reason = public_url_allowed(webhook_url, require_https=True)
            if not safe:
                return ChannelDelivery(
                    channel=self.provider,
                    channel_id=config.id,
                    conversation_id=outbound.conversation_id,
                    sent=False,
                    dry_run=False,
                    endpoint=_redacted_url(webhook_url),
                    request_json=request_json,
                    error=reason,
                    blocked_reason=blocked_reason,
                )
            request_json["_request_url"] = webhook_url
        return ChannelDelivery(
            channel=self.provider,
            channel_id=config.id,
            conversation_id=outbound.conversation_id,
            sent=False,
            dry_run=dry_run,
            endpoint=_redacted_url(webhook_url) if webhook_url else "discord_webhook_url",
            request_json=request_json,
            blocked_reason=blocked_reason,
        )


class GenericWebhookAdapter(ChannelAdapter):
    provider = "webhook"

    def parse_inbound(self, config: ChannelEndpointConfig, payload: dict[str, Any]) -> ChannelInboundMessage:
        event = _dict_or_empty(payload.get("event"))
        body = event or payload
        text = _first_text(body, ("text", "message", "content", "prompt", "query"))
        if text is None:
            raise ChannelPayloadError("Generic webhook payload did not include text, message, content, prompt, or query.")
        conversation_id = _required_str(
            body.get("conversation_id")
            or body.get("thread_id")
            or body.get("chat_id")
            or body.get("channel_id")
            or body.get("room_id")
            or "default",
            "conversation id",
        )
        user = _dict_or_empty(body.get("user")) or _dict_or_empty(body.get("sender")) or _dict_or_empty(body.get("author"))
        return ChannelInboundMessage(
            channel=config.provider,
            channel_id=config.id,
            conversation_id=conversation_id,
            user_id=_optional_str(body.get("user_id") or body.get("sender_id") or user.get("id")),
            message_id=_optional_str(body.get("message_id") or body.get("id")),
            text=text,
            metadata={"provider": config.provider},
        )

    def build_delivery(
        self,
        config: ChannelEndpointConfig,
        outbound: ChannelOutboundMessage,
        *,
        dry_run: bool,
        blocked_reason: str | None = None,
    ) -> ChannelDelivery:
        webhook_url = _configured_webhook_url(config, "NEST_AGENT_CHANNEL_WEBHOOK_URL")
        request_json = {
            "conversation_id": outbound.conversation_id,
            "text": outbound.text,
            "reply_to_message_id": outbound.reply_to_message_id,
        }
        if not dry_run and webhook_url is None:
            return ChannelDelivery(
                channel=config.provider,
                channel_id=config.id,
                conversation_id=outbound.conversation_id,
                sent=False,
                dry_run=False,
                endpoint="generic_webhook_url",
                request_json=request_json,
                error="Missing generic webhook URL. Set webhook_url_env or NEST_AGENT_CHANNEL_WEBHOOK_URL.",
                blocked_reason=blocked_reason,
            )
        if not dry_run and webhook_url:
            safe, reason = public_url_allowed(webhook_url, require_https=True)
            if not safe:
                return ChannelDelivery(
                    channel=config.provider,
                    channel_id=config.id,
                    conversation_id=outbound.conversation_id,
                    sent=False,
                    dry_run=False,
                    endpoint=_redacted_url(webhook_url),
                    request_json=request_json,
                    error=reason,
                    blocked_reason=blocked_reason,
                )
            request_json["_request_url"] = webhook_url
        return ChannelDelivery(
            channel=config.provider,
            channel_id=config.id,
            conversation_id=outbound.conversation_id,
            sent=False,
            dry_run=dry_run,
            endpoint=_redacted_url(webhook_url) if webhook_url else "generic_webhook_url",
            request_json=request_json,
            blocked_reason=blocked_reason,
        )


def default_adapters() -> dict[str, ChannelAdapter]:
    generic = GenericWebhookAdapter()
    return {
        "telegram": TelegramAdapter(),
        "discord": DiscordAdapter(),
        "webhook": generic,
        "generic": generic,
    }


def _post_json(delivery: ChannelDelivery, *, timeout_seconds: int) -> ChannelDelivery:
    request_url = delivery.request_json.get("_request_url")
    if not isinstance(request_url, str) or not request_url.startswith(("http://", "https://")):
        return _copy_delivery(delivery, error="Delivery adapter did not provide a valid request URL.")
    payload = {key: value for key, value in delivery.request_json.items() if key != "_request_url"}
    data = json.dumps(payload).encode("utf-8")
    request = Request(request_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec
            response_text = response.read().decode("utf-8", errors="replace")
            return _copy_delivery(
                delivery,
                sent=200 <= int(response.status) < 300,
                status_code=int(response.status),
                response_text=response_text,
            )
    except HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        return _copy_delivery(delivery, sent=False, status_code=int(exc.code), response_text=response_text, error=str(exc))
    except URLError as exc:
        return _copy_delivery(delivery, sent=False, error=str(exc.reason))


def _copy_delivery(
    delivery: ChannelDelivery,
    *,
    sent: bool | None = None,
    status_code: int | None = None,
    response_text: str | None = None,
    error: str | None = None,
) -> ChannelDelivery:
    return ChannelDelivery(
        channel=delivery.channel,
        channel_id=delivery.channel_id,
        conversation_id=delivery.conversation_id,
        sent=delivery.sent if sent is None else sent,
        dry_run=delivery.dry_run,
        endpoint=delivery.endpoint,
        request_json=_redact_delivery_request(
            {key: value for key, value in delivery.request_json.items() if key != "_request_url"}
        ),
        status_code=delivery.status_code if status_code is None else status_code,
        response_text=delivery.response_text if response_text is None else response_text,
        error=delivery.error if error is None else error,
        blocked_reason=delivery.blocked_reason,
    )


def _redact_delivery_request(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if "secret" in lowered or lowered.endswith("token") or "token" in lowered:
            safe[key] = "<configured>" if value else value
        else:
            safe[key] = value
    return safe


def _configured_webhook_url(config: ChannelEndpointConfig, default_env: str) -> str | None:
    env_name = config.webhook_url_env or str(config.settings.get("webhook_url_env") or default_env)
    url = _configured_secret(config, env_name)
    if url:
        return url
    raw = config.settings.get("webhook_url")
    return str(raw).strip() if raw else None


def _configured_secret(config: ChannelEndpointConfig, name: str) -> str | None:
    resolved = config.settings.get("_resolved_secrets")
    if isinstance(resolved, dict):
        value = resolved.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = os.getenv(name, "").strip()
    return value or None


def _redacted_url(url: str | None) -> str:
    if not url:
        return ""
    if "/webhooks/" in url:
        prefix, _, _suffix = url.partition("/webhooks/")
        return prefix + "/webhooks/<redacted>"
    if len(url) > 48:
        return url[:32] + "...<redacted>"
    return url


def _first_dict(payload: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, dict[str, Any] | None]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return key, value
    return "", None


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _discord_option_text(options: object) -> str | None:
    if not isinstance(options, list):
        return None
    fallback: str | None = None
    for item in options:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if isinstance(value, str) and value.strip():
            name = str(item.get("name", "")).lower()
            if name in {"text", "message", "prompt", "query"}:
                return value.strip()
            fallback = fallback or value.strip()
        nested = _discord_option_text(item.get("options"))
        if nested:
            return nested
    return fallback


def _required_str(value: object, name: str) -> str:
    text = _optional_str(value)
    if text is None:
        raise ChannelPayloadError(f"Missing {name}.")
    return text


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _message_id_value(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value
