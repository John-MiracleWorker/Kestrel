from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from threading import RLock, Thread
from typing import Any
from uuid import uuid4

from ..agent import NestedMV2Agent
from ..app_factory import build_agent
from ..config import AgentConfig
from ..event_log import AgentEvent, JsonlEventLog
from ..runtime_models import AgentTurnResult
from .adapters import (
    ChannelAdapter,
    ChannelPayloadError,
    GenericWebhookAdapter,
    default_adapters,
    telegram_api_request,
)
from .models import (
    ChannelEndpointConfig,
    ChannelInboundMessage,
    ChannelOutboundMessage,
    ChannelProcessResult,
)

TELEGRAM_ADMIN_CONFIRMATION_TTL_SECONDS = 300.0

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
        run_manager: Any | None = None,
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
        self.run_manager = run_manager
        self._agent: NestedMV2Agent | None = None
        self._agent_lock = RLock()
        self._pending_admin_confirmations: dict[str, dict[str, Any]] = {}
        self._runtime_settings_store: Any | None = None
        self._config_update_handler: Callable[[AgentConfig], None] | None = None

    def configure_runtime_settings(
        self,
        *,
        settings_store: Any | None,
        config_update_handler: Callable[[AgentConfig], None] | None,
    ) -> None:
        self._runtime_settings_store = settings_store
        self._config_update_handler = config_update_handler

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

    def telegram_webhook_info(self, channel_id: str) -> dict[str, Any]:
        channel = self._resolved_telegram_channel(channel_id)
        delivery = telegram_api_request(
            channel,
            "getWebhookInfo",
            {},
            dry_run=False,
            timeout_seconds=self.config.channel_send_timeout_seconds,
        )
        return {"ok": delivery.sent, "channel_id": channel.id, "method": "getWebhookInfo", "delivery": delivery.to_public_dict()}

    def telegram_set_webhook(
        self,
        channel_id: str,
        *,
        url: str,
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        channel = self._resolved_telegram_channel(channel_id)
        clean_url = url.strip()
        if not clean_url:
            raise ChannelPayloadError("Telegram webhook URL is required.")
        from ..net_safety import public_url_allowed

        safe_url, unsafe_reason = public_url_allowed(clean_url, require_https=True)
        if not safe_url:
            raise ChannelPayloadError(unsafe_reason)
        payload: dict[str, Any] = {
            "url": clean_url,
            "drop_pending_updates": bool(drop_pending_updates),
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        secret_env = str(channel.settings.get("signature_secret_env") or "").strip()
        if secret_env:
            secret = _channel_secret_value(channel, secret_env)
            if not secret:
                raise ChannelPayloadError(f"Missing Telegram webhook secret token environment variable: {secret_env}")
            payload["secret_token"] = secret
        delivery = telegram_api_request(
            channel,
            "setWebhook",
            payload,
            dry_run=False,
            timeout_seconds=self.config.channel_send_timeout_seconds,
        )
        return {"ok": delivery.sent, "channel_id": channel.id, "method": "setWebhook", "delivery": delivery.to_public_dict()}

    def telegram_delete_webhook(
        self,
        channel_id: str,
        *,
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        channel = self._resolved_telegram_channel(channel_id)
        delivery = telegram_api_request(
            channel,
            "deleteWebhook",
            {"drop_pending_updates": bool(drop_pending_updates)},
            dry_run=False,
            timeout_seconds=self.config.channel_send_timeout_seconds,
        )
        return {"ok": delivery.sent, "channel_id": channel.id, "method": "deleteWebhook", "delivery": delivery.to_public_dict()}

    def telegram_test_message(self, channel_id: str, *, chat_id: str, text: str) -> dict[str, Any]:
        channel = self._resolved_telegram_channel(channel_id)
        outbound = ChannelOutboundMessage(
            channel="telegram",
            channel_id=channel.id,
            conversation_id=chat_id,
            text=text.strip() or "Kestrel Telegram channel test.",
        )
        adapter = self._adapter_for("telegram")
        delivery = adapter.deliver(
            channel,
            outbound,
            dry_run=False,
            timeout_seconds=self.config.channel_send_timeout_seconds,
        )
        return {"ok": delivery.sent, "channel_id": channel.id, "method": "sendMessage", "delivery": delivery.to_public_dict()}

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
        if channel.provider == "telegram" and "callback_query" in payload and self.run_manager is not None:
            _enforce_channel_allowlist(channel, _telegram_callback_inbound(channel, payload))
            return self._handle_telegram_approval_callback(channel, adapter, payload, send=send)
        inbound = adapter.parse_inbound(channel, payload)
        _enforce_channel_allowlist(channel, inbound)
        self._event("channel.receive", inbound.to_public_dict())
        requested_send = channel.auto_reply if send is None else send
        dry_run, blocked_reason = self._delivery_gate(channel, requested=requested_send)
        if channel.provider == "telegram" and self.run_manager is not None:
            admin_result = self._handle_telegram_admin_command(
                channel,
                adapter,
                inbound,
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )
            if admin_result is not None:
                return admin_result
        adapter.notify_processing_started(
            channel,
            inbound,
            dry_run=dry_run,
            timeout_seconds=self.config.channel_send_timeout_seconds,
            blocked_reason=blocked_reason,
        )
        if self.run_manager is not None:
            return self._handle_payload_with_run_manager(
                channel,
                adapter,
                inbound,
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )
        with self._agent_lock:
            agent = self._agent_for_hot_path()
            turn = agent.chat(
                inbound.text,
                session_id=inbound.session_id,
                source=inbound.to_turn_source(),
                progress_handler=lambda event_type, payload: self._notify_progress(
                    adapter,
                    channel,
                    inbound,
                    event_type,
                    payload,
                    dry_run=dry_run,
                    blocked_reason=blocked_reason,
                ),
            )

        outbound = ChannelOutboundMessage(
            channel=inbound.channel,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            reply_to_message_id=inbound.message_id,
            text=turn.assistant_message,
            metadata={"stop_reason": turn.stop_reason},
        )
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

    def _handle_payload_with_run_manager(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> ChannelProcessResult:
        if self.run_manager is None:
            raise ChannelPayloadError("Run manager is required for channel approval prompts.")
        run = self.run_manager.create_run(message=inbound.text, session_id=inbound.session_id)
        run_id = str(getattr(run, "run_id", "") or "")
        self._start_run_progress_watcher(
            channel,
            adapter,
            inbound,
            run_id,
            dry_run=dry_run,
            blocked_reason=blocked_reason,
        )
        run_payload = self._wait_for_run_payload(run_id)
        status = str(run_payload.get("status") or "")
        assistant, metadata = self._run_reply(run_id, run_payload, fallback_to_working=True)
        outbound = ChannelOutboundMessage(
            channel=inbound.channel,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            reply_to_message_id=inbound.message_id,
            text=assistant,
            metadata=metadata,
        )
        delivery = adapter.deliver(
            channel,
            outbound,
            dry_run=dry_run,
            timeout_seconds=self.config.channel_send_timeout_seconds,
            blocked_reason=blocked_reason,
        )
        turn = AgentTurnResult(
            session_id=inbound.session_id,
            user_message=inbound.text,
            assistant_message=assistant,
            tool_executions=(),
            context_chars=0,
            memory_writes=(),
            stop_reason=str(metadata.get("stop_reason") or status or "unknown"),
            run_id=run_id,
            source=inbound.to_turn_source(),
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
        if status in {"queued", "running"}:
            self._start_run_followup(
                channel,
                adapter,
                inbound,
                run_id,
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )
        return result

    def _wait_for_run_payload(self, run_id: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        if self.run_manager is None:
            raise ChannelPayloadError("Run manager is required for channel approval prompts.")
        timeout = self.config.channel_send_timeout_seconds if timeout_seconds is None else timeout_seconds
        deadline = time.monotonic() + max(float(timeout), 0.1)
        last = self.run_manager.get_run(run_id)
        while str(last.get("status") or "") in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
            last = self.run_manager.get_run(run_id)
        return dict(last)

    def _run_reply(
        self,
        run_id: str,
        run_payload: Mapping[str, Any],
        *,
        fallback_to_working: bool,
    ) -> tuple[str, dict[str, Any]]:
        status = str(run_payload.get("status") or "")
        stop_reason = str(run_payload.get("stop_reason") or status or "unknown")
        assistant = str(run_payload.get("assistant_message") or "")
        metadata: dict[str, Any] = {"run_id": run_id, "stop_reason": stop_reason}
        if status == "blocked":
            pending = _pending_approvals(run_payload)
            assistant = _approval_prompt_text(pending)
            reply_markup = _approval_reply_markup(pending)
            if reply_markup:
                metadata["reply_markup"] = reply_markup
        elif stop_reason == "max_tool_rounds":
            assistant = self._max_tool_rounds_reply(run_id, run_payload)
        elif status == "failed" and not assistant:
            assistant = f"Kestrel run failed: {run_payload.get('error') or stop_reason}"
        elif not assistant and fallback_to_working:
            assistant = "Kestrel accepted the request and is still working."
        return assistant, metadata

    def _max_tool_rounds_reply(self, run_id: str, run_payload: Mapping[str, Any]) -> str:
        summary = ""
        if self.run_manager is not None:
            state = getattr(self.run_manager, "state", None)
            list_run_steps = getattr(state, "list_run_steps", None)
            if callable(list_run_steps):
                try:
                    steps = list_run_steps(run_id, limit=200)
                    summary = _run_steps_tool_summary(steps if isinstance(steps, list) else [])
                except Exception:  # pragma: no cover - best effort user-facing summary
                    summary = ""
        original = str(run_payload.get("assistant_message") or run_payload.get("error") or "").strip()
        lines = [
            "Reached the tool-iteration limit before Kestrel produced a final answer.",
        ]
        if summary:
            lines.append(summary)
        if original and original != "Stopped after max tool rounds.":
            lines.append(f"Last note: {original}")
        lines.append("Try a narrower request, or ask Kestrel to continue from the last run after the tool budget is raised.")
        return "\n\n".join(lines)

    def _start_run_progress_watcher(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        run_id: str,
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> None:
        events = getattr(self.run_manager, "events", None) if self.run_manager is not None else None
        subscribe = getattr(events, "subscribe", None)
        if not callable(subscribe):
            return
        try:
            subscriber = subscribe(run_id, after_id=0)
        except Exception as exc:  # pragma: no cover - defensive logging for optional progress surface
            self._event("channel.progress.subscribe_error", {"channel": inbound.channel, "conversation_id": inbound.conversation_id, "run_id": run_id, "error": str(exc)})
            return
        thread = Thread(
            target=self._deliver_run_progress_events,
            args=(channel, adapter, inbound, run_id, events, subscriber),
            kwargs={"dry_run": dry_run, "blocked_reason": blocked_reason},
            name=f"channel-progress-{run_id}",
            daemon=True,
        )
        thread.start()

    def _deliver_run_progress_events(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        run_id: str,
        events: Any,
        subscriber: Any,
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> None:
        terminal_events = {"run.completed", "run.failed", "run.blocked", "run.cancelled"}
        deadline = time.monotonic() + max(float(self.config.timeout_seconds), float(self.config.channel_send_timeout_seconds), 0.1)
        seen: set[tuple[str, str, str]] = set()
        progress_events: list[tuple[str, dict[str, Any]]] = []
        try:
            while time.monotonic() < deadline:
                try:
                    event = subscriber.get(timeout=0.1)
                except queue.Empty:
                    continue
                event_type = str(getattr(event, "type", "") or "")
                payload = getattr(event, "payload", {})
                if not isinstance(payload, dict):
                    payload = {}
                if event_type in terminal_events:
                    self._notify_progress_summary(
                        adapter,
                        channel,
                        inbound,
                        progress_events,
                        dry_run=dry_run,
                        blocked_reason=blocked_reason,
                    )
                    return
                progress_type = _run_event_to_progress_type(event_type)
                if progress_type is None:
                    continue
                tool = str(payload.get("tool") or payload.get("tool_name") or "").strip()
                tool_call_id = str(payload.get("tool_call_id") or "").strip()
                key = (progress_type, tool, tool_call_id)
                if key in seen:
                    continue
                seen.add(key)
                progress_events.append((progress_type, payload))
            self._notify_progress_summary(
                adapter,
                channel,
                inbound,
                progress_events,
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )
        except Exception as exc:  # pragma: no cover - defensive logging for daemon thread
            self._event("channel.progress.error", {"channel": inbound.channel, "conversation_id": inbound.conversation_id, "run_id": run_id, "error": str(exc)})
        finally:
            unsubscribe = getattr(events, "unsubscribe", None)
            if callable(unsubscribe):
                try:
                    unsubscribe(run_id, subscriber)
                except Exception:
                    return

    def _start_run_followup(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        run_id: str,
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> None:
        thread = Thread(
            target=self._deliver_run_followup,
            args=(channel, adapter, inbound, run_id),
            kwargs={"dry_run": dry_run, "blocked_reason": blocked_reason},
            name=f"channel-followup-{run_id}",
            daemon=True,
        )
        thread.start()

    def _deliver_run_followup(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        run_id: str,
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> None:
        try:
            timeout = max(float(self.config.timeout_seconds), float(self.config.channel_send_timeout_seconds))
            run_payload = self._wait_for_run_payload(run_id, timeout_seconds=timeout)
            if str(run_payload.get("status") or "") in {"queued", "running"}:
                self._event("channel.followup.timeout", {"channel": inbound.channel, "conversation_id": inbound.conversation_id, "run_id": run_id})
                return
            assistant, metadata = self._run_reply(run_id, run_payload, fallback_to_working=False)
            if not assistant:
                return
            outbound = ChannelOutboundMessage(
                channel=inbound.channel,
                channel_id=inbound.channel_id,
                conversation_id=inbound.conversation_id,
                reply_to_message_id=inbound.message_id,
                text=assistant,
                metadata={**metadata, "followup": True},
            )
            delivery = adapter.deliver(
                channel,
                outbound,
                dry_run=dry_run,
                timeout_seconds=self.config.channel_send_timeout_seconds,
                blocked_reason=blocked_reason,
            )
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
                    "followup": True,
                    "run_id": run_id,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive logging for daemon thread
            self._event(
                "channel.followup.error",
                {"channel": inbound.channel, "conversation_id": inbound.conversation_id, "run_id": run_id, "error": str(exc)},
            )

    def _handle_telegram_admin_command(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> ChannelProcessResult | None:
        command, argument = _telegram_admin_intent(inbound.text, natural_language=_telegram_admin_enabled(channel))
        if command is None:
            return None
        if self.run_manager is None:
            raise ChannelPayloadError("Run manager is required for Telegram admin commands.")
        if not _telegram_admin_enabled(channel):
            return self._telegram_admin_result(
                channel,
                adapter,
                inbound,
                text="Telegram admin command denied: admin mode is disabled.",
                stop_reason="admin_disabled",
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )
        owners = _telegram_owner_ids(channel)
        if not owners or str(inbound.user_id or "").strip() not in owners:
            return self._telegram_admin_result(
                channel,
                adapter,
                inbound,
                text="Telegram admin command denied: sender is not a configured Kestrel owner.",
                stop_reason="admin_unauthorized",
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )

        if command == "secret_rejected":
            return self._telegram_admin_result(
                channel,
                adapter,
                inbound,
                text=_telegram_secret_rejection_text(),
                stop_reason="admin_secret_rejected",
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )
        if command == "set_max_tool_rounds":
            value = _parse_positive_int(argument, "max tool calls")
            return self._telegram_admin_confirmation_result(
                channel,
                adapter,
                inbound,
                action={
                    "type": "set_max_tool_rounds",
                    "value": value,
                    "description": f"Set max tool calls to {value}",
                },
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )

        try:
            text, metadata = self._execute_telegram_admin_command(command, argument)
        except ValueError as exc:
            text, metadata = str(exc), {}
        return self._telegram_admin_result(
            channel,
            adapter,
            inbound,
            text=text,
            stop_reason="admin_command",
            dry_run=dry_run,
            blocked_reason=blocked_reason,
            metadata=metadata,
        )

    def _telegram_admin_confirmation_result(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        *,
        action: dict[str, Any],
        dry_run: bool,
        blocked_reason: str | None,
    ) -> ChannelProcessResult:
        confirmation_id = uuid4().hex[:12]
        self._pending_admin_confirmations[confirmation_id] = {
            **action,
            "created_at": time.time(),
            "owner_user_id": inbound.user_id,
        }
        description = str(action.get("description") or "Apply Telegram admin action")
        text = "\n".join(
            [
                "Confirm Telegram admin action:",
                description,
                "No change has been applied yet.",
            ]
        )
        return self._telegram_admin_result(
            channel,
            adapter,
            inbound,
            text=text,
            stop_reason="admin_confirmation_required",
            dry_run=dry_run,
            blocked_reason=blocked_reason,
            metadata={
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "Confirm", "callback_data": f"kestrel_admin_confirm:{confirmation_id}"},
                            {"text": "Cancel", "callback_data": f"kestrel_admin_cancel:{confirmation_id}"},
                        ]
                    ]
                }
            },
        )

    def _execute_telegram_admin_command(self, command: str, argument: str) -> tuple[str, dict[str, Any]]:
        if self.run_manager is None:
            raise ChannelPayloadError("Run manager is required for Telegram admin commands.")
        if command in {"help", "admin"}:
            return (_telegram_admin_help(), {})
        if command == "status":
            runs = _safe_list_runs(self.run_manager)
            approvals = _safe_list_approvals(self.run_manager, status="pending")
            return (_telegram_status_text(runs, approvals), {"reply_markup": _admin_approval_reply_markup(approvals)})
        if command == "runs":
            return (_telegram_runs_text(_safe_list_runs(self.run_manager)), {})
        if command == "run":
            run_id = argument.strip()
            if not run_id:
                raise ValueError("Usage: /run <run_id>")
            return (_telegram_run_text(self.run_manager.get_run(run_id)), {})
        if command == "cancel":
            run_id = argument.strip()
            if not run_id:
                raise ValueError("Usage: /cancel <run_id>")
            payload = self.run_manager.cancel_run(run_id)
            return (f"Cancelled {payload.get('run_id', run_id)}.", {})
        if command in {"approve", "deny"}:
            approval_id = argument.strip().split()[0] if argument.strip() else ""
            if not approval_id:
                raise ValueError(f"Usage: /{command} <approval_id>")
            return self._decide_telegram_approval(approval_id, approved=command == "approve")
        raise ValueError(f"Unknown Telegram admin command: /{command}")

    def _decide_telegram_approval(self, approval_id: str, *, approved: bool) -> tuple[str, dict[str, Any]]:
        if self.run_manager is None:
            raise ChannelPayloadError("Run manager is required for Telegram admin commands.")
        pending = _find_pending_approval(self.run_manager, approval_id)
        arguments = dict(pending.get("arguments") or {}) if pending else None
        decision = self.run_manager.decide_approval(approval_id, approved=approved, arguments=arguments if approved else None)
        if approved:
            run_id = str(decision.get("run_id") or "")
            if run_id:
                run_payload = self._wait_for_run_payload(run_id)
                assistant = str(run_payload.get("assistant_message") or "").strip()
                if str(run_payload.get("status") or "") == "completed" and assistant:
                    return assistant, {"approval_decision": decision}
        action = "Approved" if approved else "Denied"
        suffix = " Continuing…" if approved else ""
        return f"{action} {approval_id}.{suffix}", {"approval_decision": decision}

    def _apply_telegram_admin_action(self, action: Mapping[str, Any]) -> str:
        action_type = str(action.get("type") or "")
        if action_type == "set_max_tool_rounds":
            value = _parse_positive_int(action.get("value"), "max tool calls")
            self._apply_max_tool_rounds(value)
            return f"Max tool calls set to {value}."
        return f"Unsupported Telegram admin action: {action_type}"

    def _apply_max_tool_rounds(self, value: int) -> None:
        next_config = replace(self.config, max_tool_rounds=value)
        if self._runtime_settings_store is not None:
            from ..runtime_settings import apply_runtime_settings, merge_runtime_settings

            current = self._runtime_settings_store.load(self.config)
            saved = self._runtime_settings_store.save(
                merge_runtime_settings(self.config, current, {"max_tool_rounds": value})
            )
            next_config = apply_runtime_settings(self.config, saved)
        if self._config_update_handler is not None:
            self._config_update_handler(next_config)
        else:
            self.config = next_config
            if self.run_manager is not None and hasattr(self.run_manager, "config"):
                self.run_manager.config = next_config

    def _telegram_admin_result(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        *,
        text: str,
        stop_reason: str,
        dry_run: bool,
        blocked_reason: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> ChannelProcessResult:
        outbound = ChannelOutboundMessage(
            channel=inbound.channel,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
            reply_to_message_id=inbound.message_id,
            text=text,
            metadata=metadata or {},
        )
        delivery = adapter.deliver(
            channel,
            outbound,
            dry_run=dry_run,
            timeout_seconds=self.config.channel_send_timeout_seconds,
            blocked_reason=blocked_reason,
        )
        turn = AgentTurnResult(
            session_id=inbound.session_id,
            user_message=inbound.text,
            assistant_message=text,
            tool_executions=(),
            context_chars=0,
            memory_writes=(),
            stop_reason=stop_reason,
            run_id="",
            source=inbound.to_turn_source(),
        )
        return ChannelProcessResult(inbound=inbound, outbound=outbound, delivery=delivery, turn=turn)

    def _handle_telegram_approval_callback(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        payload: dict[str, Any],
        *,
        send: bool | None,
    ) -> ChannelProcessResult:
        if self.run_manager is None:
            raise ChannelPayloadError("Run manager is required for channel approval prompts.")
        callback = _dict_or_empty(payload.get("callback_query"))
        data = str(callback.get("data") or "")
        if not _telegram_owner_authorized(channel, _optional_str(_dict_or_empty(callback.get("from")).get("id"))):
            message = _dict_or_empty(callback.get("message"))
            chat = _dict_or_empty(message.get("chat"))
            conversation_id = str(chat.get("id") or "")
            if not conversation_id:
                raise ChannelPayloadError("Telegram callback did not include a chat id.")
            requested_send = channel.auto_reply if send is None else send
            dry_run, blocked_reason = self._delivery_gate(channel, requested=requested_send)
            self._answer_telegram_callback(channel, callback, dry_run=dry_run, blocked_reason=blocked_reason)
            inbound = ChannelInboundMessage(
                channel="telegram",
                channel_id=channel.id,
                conversation_id=conversation_id,
                user_id=_optional_str(_dict_or_empty(callback.get("from")).get("id")),
                message_id=_optional_str(message.get("message_id")),
                text=data,
                metadata={"callback_query_id": _optional_str(callback.get("id"))},
            )
            return self._telegram_admin_result(
                channel,
                adapter,
                inbound,
                text="Telegram admin action denied: sender is not a configured Kestrel owner.",
                stop_reason="admin_unauthorized",
                dry_run=dry_run,
                blocked_reason=blocked_reason,
            )
        if data.startswith("kestrel_admin_confirm:") or data.startswith("kestrel_admin_cancel:"):
            return self._handle_telegram_admin_confirmation_callback(channel, adapter, callback, data, send=send)
        if data.startswith("kestrel_approve:"):
            approval_id = data.removeprefix("kestrel_approve:")
            approved = True
        elif data.startswith("kestrel_deny:"):
            approval_id = data.removeprefix("kestrel_deny:")
            approved = False
        else:
            raise ChannelPayloadError("Unsupported Telegram callback data.")
        message = _dict_or_empty(callback.get("message"))
        chat = _dict_or_empty(message.get("chat"))
        conversation_id = str(chat.get("id") or "")
        if not conversation_id:
            raise ChannelPayloadError("Telegram callback did not include a chat id.")
        decision = self.run_manager.decide_approval(approval_id, approved=approved, arguments=None)
        text = f"{'Approved' if approved else 'Denied'} {approval_id}." + (" Continuing…" if approved else "")
        if approved:
            run_id = str(decision.get("run_id") or "")
            if run_id:
                run_payload = self._wait_for_run_payload(run_id)
                assistant = str(run_payload.get("assistant_message") or "").strip()
                if str(run_payload.get("status") or "") == "completed" and assistant:
                    text = assistant
        inbound = ChannelInboundMessage(
            channel="telegram",
            channel_id=channel.id,
            conversation_id=conversation_id,
            user_id=_optional_str(_dict_or_empty(callback.get("from")).get("id")),
            message_id=_optional_str(message.get("message_id")),
            text=data,
            metadata={"callback_query_id": _optional_str(callback.get("id")), "approval_id": approval_id},
        )
        requested_send = channel.auto_reply if send is None else send
        dry_run, blocked_reason = self._delivery_gate(channel, requested=requested_send)
        self._answer_telegram_callback(channel, callback, dry_run=dry_run, blocked_reason=blocked_reason)
        outbound = ChannelOutboundMessage(
            channel="telegram",
            channel_id=channel.id,
            conversation_id=conversation_id,
            text=text,
            metadata={"approval_decision": decision},
        )
        delivery = adapter.deliver(
            channel,
            outbound,
            dry_run=dry_run,
            timeout_seconds=self.config.channel_send_timeout_seconds,
            blocked_reason=blocked_reason,
        )
        turn = AgentTurnResult(
            session_id=inbound.session_id,
            user_message=data,
            assistant_message=text,
            tool_executions=(),
            context_chars=0,
            memory_writes=(),
            stop_reason="approval_decided",
            run_id=str(decision.get("run_id") or ""),
            source=inbound.to_turn_source(),
        )
        return ChannelProcessResult(inbound=inbound, outbound=outbound, delivery=delivery, turn=turn)

    def _handle_telegram_admin_confirmation_callback(
        self,
        channel: ChannelEndpointConfig,
        adapter: ChannelAdapter,
        callback: dict[str, Any],
        data: str,
        *,
        send: bool | None,
    ) -> ChannelProcessResult:
        if data.startswith("kestrel_admin_confirm:"):
            confirmation_id = data.removeprefix("kestrel_admin_confirm:")
            confirmed = True
        else:
            confirmation_id = data.removeprefix("kestrel_admin_cancel:")
            confirmed = False
        message = _dict_or_empty(callback.get("message"))
        chat = _dict_or_empty(message.get("chat"))
        conversation_id = str(chat.get("id") or "")
        if not conversation_id:
            raise ChannelPayloadError("Telegram callback did not include a chat id.")
        inbound = ChannelInboundMessage(
            channel="telegram",
            channel_id=channel.id,
            conversation_id=conversation_id,
            user_id=_optional_str(_dict_or_empty(callback.get("from")).get("id")),
            message_id=_optional_str(message.get("message_id")),
            text=data,
            metadata={"callback_query_id": _optional_str(callback.get("id")), "confirmation_id": confirmation_id},
        )
        requested_send = channel.auto_reply if send is None else send
        dry_run, blocked_reason = self._delivery_gate(channel, requested=requested_send)
        self._answer_telegram_callback(channel, callback, dry_run=dry_run, blocked_reason=blocked_reason)
        action = self._pending_admin_confirmations.pop(confirmation_id, None)
        if action is None:
            text = "Telegram admin confirmation expired or was already used."
            stop_reason = "admin_confirmation_missing"
        elif not _telegram_admin_confirmation_is_current(action):
            text = "Telegram admin confirmation expired or was already used."
            stop_reason = "admin_confirmation_expired"
        elif str(action.get("owner_user_id") or "").strip() != str(inbound.user_id or "").strip():
            text = "Telegram admin confirmation denied: owner identity changed."
            stop_reason = "admin_confirmation_owner_mismatch"
        elif not confirmed:
            text = "Telegram admin action cancelled."
            stop_reason = "admin_action_cancelled"
        else:
            text = self._apply_telegram_admin_action(action)
            stop_reason = "admin_action_confirmed"
        return self._telegram_admin_result(
            channel,
            adapter,
            inbound,
            text=text,
            stop_reason=stop_reason,
            dry_run=dry_run,
            blocked_reason=blocked_reason,
        )

    def _answer_telegram_callback(
        self,
        channel: ChannelEndpointConfig,
        callback: Mapping[str, Any],
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> None:
        callback_id = _optional_str(callback.get("id"))
        if not callback_id:
            return
        delivery = telegram_api_request(
            channel,
            "answerCallbackQuery",
            {"callback_query_id": callback_id},
            dry_run=dry_run,
            timeout_seconds=self.config.channel_send_timeout_seconds,
            blocked_reason=blocked_reason,
        )
        self._event(
            "channel.callback.answer",
            {
                "channel": "telegram",
                "channel_id": channel.id,
                "sent": delivery.sent,
                "dry_run": delivery.dry_run,
                "error": delivery.error,
                "blocked_reason": delivery.blocked_reason,
            },
        )

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

    def _resolved_telegram_channel(self, channel_id: str) -> ChannelEndpointConfig:
        channel = self._resolve_channel(provider="telegram", channel_id=channel_id)
        if channel.provider != "telegram":
            raise ChannelPayloadError(f"Channel is not a Telegram channel: {channel.id}")
        return self._with_resolved_secrets(channel)

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

    def _notify_progress(
        self,
        adapter: ChannelAdapter,
        channel: ChannelEndpointConfig,
        inbound: ChannelInboundMessage,
        event_type: str,
        payload: dict[str, Any],
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> None:
        text = _progress_text(event_type, payload)
        if not text:
            return
        delivery = adapter.notify_progress(
            channel,
            inbound,
            text,
            dry_run=dry_run,
            timeout_seconds=self.config.channel_send_timeout_seconds,
            blocked_reason=blocked_reason,
        )
        self._event(
            "channel.progress",
            {
                "channel": inbound.channel,
                "channel_id": inbound.channel_id,
                "conversation_id": inbound.conversation_id,
                "event_type": event_type,
                "text": text,
                "sent": delivery.sent if delivery is not None else False,
                "dry_run": delivery.dry_run if delivery is not None else dry_run,
                "error": delivery.error if delivery is not None else None,
                "blocked_reason": delivery.blocked_reason if delivery is not None else blocked_reason,
            },
        )

    def _notify_progress_summary(
        self,
        adapter: ChannelAdapter,
        channel: ChannelEndpointConfig,
        inbound: ChannelInboundMessage,
        events: list[tuple[str, dict[str, Any]]],
        *,
        dry_run: bool,
        blocked_reason: str | None,
    ) -> None:
        text = _progress_summary_text(events)
        if not text:
            return
        delivery = adapter.notify_progress(
            channel,
            inbound,
            text,
            dry_run=dry_run,
            timeout_seconds=self.config.channel_send_timeout_seconds,
            blocked_reason=blocked_reason,
        )
        self._event(
            "channel.progress.summary",
            {
                "channel": inbound.channel,
                "channel_id": inbound.channel_id,
                "conversation_id": inbound.conversation_id,
                "text": text,
                "event_count": len(events),
                "sent": delivery.sent if delivery is not None else False,
                "dry_run": delivery.dry_run if delivery is not None else dry_run,
                "error": delivery.error if delivery is not None else None,
                "blocked_reason": delivery.blocked_reason if delivery is not None else blocked_reason,
            },
        )

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


def _pending_approvals(run_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    approvals = run_payload.get("approvals")
    if not isinstance(approvals, list):
        return []
    return [dict(item) for item in approvals if isinstance(item, dict) and item.get("status") == "pending"]


def _approval_prompt_text(approvals: list[dict[str, Any]]) -> str:
    if not approvals:
        return "Approval required, but no pending approval record was found."
    lines = ["Approval required before Kestrel can continue:"]
    for approval in approvals:
        tool = str(approval.get("tool_name") or "tool")
        risk = str(approval.get("risk") or "unknown")
        approval_id = str(approval.get("approval_id") or "")
        lines.append(f"- {tool} ({risk}) — {approval_id}")
    return "\n".join(lines)


def _approval_reply_markup(approvals: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not approvals:
        return None
    buttons = []
    for approval in approvals[:1]:
        approval_id = str(approval.get("approval_id") or "").strip()
        if not approval_id:
            continue
        buttons.append(
            [
                {"text": "Approve", "callback_data": f"kestrel_approve:{approval_id}"},
                {"text": "Deny", "callback_data": f"kestrel_deny:{approval_id}"},
            ]
        )
    return {"inline_keyboard": buttons} if buttons else None


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _run_event_to_progress_type(event_type: str) -> str | None:
    if event_type in {"tool.started", "assistant.tool_call"}:
        return "tool.request"
    if event_type in {"tool.completed", "tool.executed"}:
        return "tool.result"
    if event_type == "tool.failed":
        return "tool.error"
    return None


def _progress_text(event_type: str, payload: dict[str, Any]) -> str | None:
    tool = str(payload.get("tool") or "").strip()
    if not tool:
        return None
    if event_type == "tool.request":
        return f"🔧 Using tool: {tool}"
    if event_type == "tool.result":
        return f"✅ Tool finished: {tool}"
    if event_type == "tool.error":
        error = str(payload.get("error") or "failed").strip()
        return f"⚠️ Tool failed: {tool} ({error})"
    return None


def _progress_summary_text(events: list[tuple[str, dict[str, Any]]]) -> str | None:
    completed = [payload for event_type, payload in events if event_type == "tool.result"]
    failed = [payload for event_type, payload in events if event_type == "tool.error"]
    if not completed and not failed:
        return None
    tools = sorted({str(payload.get("tool") or "tool").strip() or "tool" for payload in completed + failed})
    parts: list[str] = []
    if completed:
        noun = "call" if len(completed) == 1 else "calls"
        parts.append(f"{len(completed)} {noun} completed")
    if failed:
        noun = "call" if len(failed) == 1 else "calls"
        parts.append(f"{len(failed)} {noun} failed")
    return f"🧰 Tool activity: {', '.join(parts)}. Tools: {', '.join(tools)}."


def _run_steps_tool_summary(steps: list[dict[str, Any]]) -> str:
    tool_events: list[dict[str, Any]] = []
    for step in steps:
        event_type = str(step.get("type") or "")
        if not event_type.startswith("tool."):
            continue
        payload = step.get("payload")
        if isinstance(payload, dict):
            tool_events.append(payload)
    if not tool_events:
        return ""
    counts = Counter(str(payload.get("tool") or "tool").strip() or "tool" for payload in tool_events)
    tools = ", ".join(f"{tool} ×{count}" for tool, count in sorted(counts.items()))
    errors = sorted({str(payload.get("error") or "").strip() for payload in tool_events if str(payload.get("error") or "").strip()})
    summary = f"Tool summary: {len(tool_events)} tool events. Tools: {tools}."
    if errors:
        summary += f" Errors: {', '.join(errors)}."
    return summary


def _telegram_admin_intent(text: str, *, natural_language: bool) -> tuple[str | None, str]:
    stripped = text.strip()
    if stripped.startswith("/"):
        head, _, rest = stripped.partition(" ")
        command = head[1:].split("@", 1)[0].strip().lower()
        if command in {"status", "runs", "run", "cancel", "approve", "deny", "help", "admin"}:
            return command, rest.strip()
        return None, ""
    if not natural_language:
        return None, ""
    lowered = stripped.lower()
    if _looks_like_raw_secret_request(stripped):
        return "secret_rejected", ""
    max_tool_match = re.search(r"(?:max(?:imum)?\s+)?tool\s+(?:calls?|rounds?|iterations?)\D+(\d{1,2})", lowered)
    if max_tool_match and any(term in lowered for term in ("set", "increase", "raise", "change", "bump")):
        return "set_max_tool_rounds", max_tool_match.group(1)
    run_id = _first_run_id(stripped)
    approval_id = _first_approval_id(stripped)
    if approval_id and any(term in lowered for term in ("approve", "allow", "yes")):
        return "approve", approval_id
    if approval_id and any(term in lowered for term in ("deny", "reject", "no")):
        return "deny", approval_id
    if run_id and any(term in lowered for term in ("cancel", "stop")):
        return "cancel", run_id
    if run_id and any(term in lowered for term in ("show", "inspect", "status")):
        return "run", run_id
    if "recent run" in lowered or "list run" in lowered or lowered in {"runs", "show runs"}:
        return "runs", ""
    if "status" in lowered or "health" in lowered or "pending approval" in lowered:
        return "status", ""
    if "admin help" in lowered or lowered in {"help", "what can you do"}:
        return "help", ""
    return None, ""


def _telegram_admin_enabled(channel: ChannelEndpointConfig) -> bool:
    configured = channel.settings.get("admin_enabled")
    if isinstance(configured, bool):
        return configured
    if isinstance(configured, str):
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _looks_like_raw_secret_request(text: str) -> bool:
    lowered = text.lower()
    if not any(term in lowered for term in ("token", "api key", "apikey", "secret", "password")):
        return False
    return bool(re.search(r"\b(set|store|save|change|update)\b.+\b(to|as)\b\s+\S{8,}", lowered))


def _first_run_id(text: str) -> str:
    match = re.search(r"\brun_[a-zA-Z0-9_:-]+\b", text)
    return match.group(0) if match else ""


def _first_approval_id(text: str) -> str:
    match = re.search(r"\bapproval_[a-zA-Z0-9_:-]+\b", text)
    return match.group(0) if match else ""


def _parse_positive_int(value: object, name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {name}: {value}") from exc
    if parsed < 0 or parsed > 50:
        raise ValueError(f"Invalid {name}: {parsed}. Use a value from 0 to 50.")
    return parsed


def _telegram_secret_rejection_text() -> str:
    return (
        "Raw secrets are not accepted through Telegram. Store tokens and API keys "
        "through the local Kestrel UI or CLI secret broker, then use Telegram to check status."
    )


def _enforce_channel_allowlist(channel: ChannelEndpointConfig, inbound: ChannelInboundMessage) -> None:
    if channel.provider != "telegram":
        return
    allowed_conversations = _channel_allowed_ids(
        channel,
        setting_key="allowed_conversation_ids",
        env_key="TELEGRAM_ALLOWED_CHAT_IDS",
    )
    allowed_users = _channel_allowed_ids(
        channel,
        setting_key="allowed_user_ids",
        env_key="TELEGRAM_ALLOWED_USER_IDS",
    )
    if not allowed_conversations or not allowed_users:
        raise ChannelPayloadError("Telegram ingress allowlists are not configured.")
    if inbound.conversation_id not in allowed_conversations:
        raise ChannelPayloadError(f"{channel.provider.capitalize()} conversation is not allowed.")
    if str(inbound.user_id or "").strip() not in allowed_users:
        raise ChannelPayloadError(f"{channel.provider.capitalize()} sender is not allowed.")


def _telegram_callback_inbound(
    channel: ChannelEndpointConfig,
    payload: dict[str, Any],
) -> ChannelInboundMessage:
    callback = _dict_or_empty(payload.get("callback_query"))
    message = _dict_or_empty(callback.get("message"))
    chat = _dict_or_empty(message.get("chat"))
    conversation_id = str(chat.get("id") or "").strip()
    if not conversation_id:
        raise ChannelPayloadError("Telegram callback did not include a chat id.")
    return ChannelInboundMessage(
        channel="telegram",
        channel_id=channel.id,
        conversation_id=conversation_id,
        user_id=_optional_str(_dict_or_empty(callback.get("from")).get("id")),
        message_id=_optional_str(message.get("message_id")),
        text=str(callback.get("data") or ""),
        metadata={"callback_query_id": _optional_str(callback.get("id"))},
    )


def _channel_allowed_ids(
    channel: ChannelEndpointConfig,
    *,
    setting_key: str,
    env_key: str,
) -> set[str]:
    configured = channel.settings.get(setting_key)
    values = configured if isinstance(configured, (list, tuple, set)) else str(configured or "").split(",")
    identifiers = {str(value).strip() for value in values if str(value).strip()}
    if not identifiers and channel.provider == "telegram":
        identifiers = {
            value.strip() for value in os.getenv(env_key, "").split(",") if value.strip()
        }
    return identifiers


def _telegram_owner_authorized(channel: ChannelEndpointConfig, user_id: str | None) -> bool:
    if not _telegram_admin_enabled(channel):
        return False
    owners = _telegram_owner_ids(channel)
    if not owners:
        return False
    return str(user_id or "").strip() in owners


def _telegram_owner_ids(channel: ChannelEndpointConfig) -> set[str]:
    values: list[object] = []
    for key in ("admin_user_ids", "owner_user_ids", "telegram_owner_ids"):
        configured = channel.settings.get(key)
        if configured is not None:
            values.append(configured)
    ids: set[str] = set()
    for value in values:
        if isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = str(value).split(",")
        ids.update(str(item).strip() for item in candidates if str(item).strip())
    return ids if len(ids) == 1 else set()


def _telegram_admin_confirmation_is_current(action: Mapping[str, Any]) -> bool:
    try:
        age = time.time() - float(action["created_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age <= TELEGRAM_ADMIN_CONFIRMATION_TTL_SECONDS


def _safe_list_runs(run_manager: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in run_manager.list_runs()]


def _safe_list_approvals(run_manager: Any, *, status: str | None = None) -> list[dict[str, Any]]:
    try:
        rows = run_manager.list_approvals(status=status)
    except TypeError:
        rows = run_manager.list_approvals()
    approvals = [dict(item) for item in rows]
    if status is not None:
        approvals = [item for item in approvals if item.get("status") == status]
    return approvals


def _find_pending_approval(run_manager: Any, approval_id: str) -> dict[str, Any] | None:
    for approval in _safe_list_approvals(run_manager, status="pending"):
        if str(approval.get("approval_id") or "") == approval_id:
            return approval
    return None


def _telegram_admin_help() -> str:
    return "\n".join(
        [
            "Kestrel Telegram Admin commands:",
            "/status — runtime summary and pending approval buttons",
            "/runs — recent runs",
            "/run <run_id> — inspect one run",
            "/cancel <run_id> — cancel a run",
            "/approve <approval_id> — approve exact pending arguments",
            "/deny <approval_id> — deny a pending approval",
        ]
    )


def _telegram_status_text(runs: list[dict[str, Any]], approvals: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for run in runs:
        status = str(run.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    lines = ["Kestrel Telegram Admin", f"Runs: {len(runs)}"]
    for status in sorted(counts):
        lines.append(f"- {status}: {counts[status]}")
    lines.append(f"Pending approvals: {len(approvals)}")
    for approval in approvals[:5]:
        lines.append(
            f"- {approval.get('approval_id')} — {approval.get('tool_name', 'tool')} ({approval.get('risk', 'unknown')})"
        )
    return "\n".join(lines)


def _telegram_runs_text(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "No Kestrel runs recorded."
    lines = ["Recent Kestrel runs:"]
    for run in runs[:8]:
        lines.append(
            f"- {run.get('run_id')} — {run.get('status', 'unknown')} — {str(run.get('message') or '')[:80]}"
        )
    return "\n".join(lines)


def _telegram_run_text(run: Mapping[str, Any]) -> str:
    lines = [f"Run {run.get('run_id')} — {run.get('status', 'unknown')}"]
    if run.get("message"):
        lines.append(f"Message: {run.get('message')}")
    if run.get("assistant_message"):
        lines.append(f"Assistant: {run.get('assistant_message')}")
    if run.get("error"):
        lines.append(f"Error: {run.get('error')}")
    pending = _pending_approvals(run)
    if pending:
        lines.append(f"Pending approvals: {len(pending)}")
    return "\n".join(lines)


def _admin_approval_reply_markup(approvals: list[dict[str, Any]]) -> dict[str, Any] | None:
    buttons = []
    for approval in approvals[:3]:
        approval_id = str(approval.get("approval_id") or "").strip()
        if not approval_id:
            continue
        tool = str(approval.get("tool_name") or "tool")
        buttons.append(
            [
                {"text": f"Approve {tool}"[:64], "callback_data": f"kestrel_approve:{approval_id}"},
                {"text": "Deny", "callback_data": f"kestrel_deny:{approval_id}"},
            ]
        )
    return {"inline_keyboard": buttons} if buttons else None


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
        if require_signature and (
            channel.provider == "telegram" or not _unsigned_allowed(channel)
        ):
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
    if provider == "telegram":
        _verify_telegram_secret_token(secret, headers)
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


def _verify_telegram_secret_token(secret: str, headers: Mapping[str, str]) -> None:
    supplied = _header(headers, "x-telegram-bot-api-secret-token")
    if not supplied:
        raise ChannelPayloadError("Missing Telegram webhook secret token header: x-telegram-bot-api-secret-token")
    if not hmac.compare_digest(supplied, secret):
        raise ChannelPayloadError("Invalid Telegram webhook secret token.")


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
