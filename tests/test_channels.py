from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from nested_memvid_agent.agent import NestedMV2Agent
from nested_memvid_agent.channels import ChannelEndpointConfig, ChannelManager
from nested_memvid_agent.channels.adapters import (
    ChannelAdapter,
    DiscordAdapter,
    GenericWebhookAdapter,
)
from nested_memvid_agent.channels.models import (
    ChannelDelivery,
    ChannelInboundMessage,
    ChannelOutboundMessage,
)
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.runtime_models import AgentTurnResult, ToolCall, ToolExecution
from nested_memvid_agent.server import create_app


def test_telegram_channel_payload_runs_agent_and_records_provenance(tmp_path: Path) -> None:
    manager = ChannelManager(_config(tmp_path))
    payload = {
        "update_id": 101,
        "message": {
            "message_id": 55,
            "text": "hello telegram",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 777},
        },
    }

    result = manager.handle_payload(provider="telegram", payload=payload, send=True)

    assert result.turn.session_id == "channel:telegram:12345"
    assert result.turn.assistant_message == "Mock response: hello telegram"
    assert result.delivery.dry_run is True
    assert result.delivery.blocked_reason == "global_channel_delivery_disabled"

    snapshot = _memory_snapshot(tmp_path, "working")
    user_record = next(item for item in snapshot if item["title"] == "User message")
    assert user_record["metadata"]["channel"] == "telegram"
    assert user_record["metadata"]["channel_id"] == "telegram"
    assert user_record["metadata"]["conversation_id"] == "12345"
    assert user_record["metadata"]["channel_user_id"] == "777"
    assert user_record["metadata"]["channel_message_id"] == "55"
    assert user_record["evidence"][0]["source"] == "channel:telegram"


def test_telegram_channel_sends_typing_action_before_agent_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test-token")
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        calls.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr("nested_memvid_agent.channels.adapters.urlopen", fake_urlopen)
    cfg = replace(_config(tmp_path), enable_channel_delivery=True)
    manager = ChannelManager(
        cfg,
        channel_configs=[ChannelEndpointConfig(id="telegram", provider="telegram", send_enabled=True, auto_reply=True)],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "update_id": 101,
            "message": {
                "message_id": 55,
                "text": "hello telegram",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            },
        },
        send=True,
    )

    assert result.delivery.sent is True
    assert [call["url"] for call in calls] == [
        "https://api.telegram.org/bot123:test-token/sendChatAction",
        "https://api.telegram.org/bot123:test-token/sendMessage",
    ]
    assert calls[0]["payload"] == {"chat_id": "12345", "action": "typing"}
    assert calls[1]["payload"]["text"] == "Mock response: hello telegram"


def test_telegram_channel_reports_tool_progress_before_agent_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test-token")
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        calls.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return FakeResponse()

    class ProgressAgent:
        def chat(self, user_message: str, session_id: str | None = None, **kwargs: Any) -> AgentTurnResult:
            progress_handler = kwargs.get("progress_handler")
            call = ToolCall(name="file.read", arguments={"path": "README.md"}, id="tool_readme")
            execution = ToolExecution(call=call, success=True, content="README contents")
            assert progress_handler is not None
            progress_handler("tool.request", {"tool": call.name, "tool_call_id": call.id})
            progress_handler(
                "tool.result",
                {"tool": call.name, "tool_call_id": call.id, "success": True, "error": None, "content_chars": 15},
            )
            return AgentTurnResult(
                session_id=session_id or "session",
                user_message=user_message,
                assistant_message="Done after using a tool.",
                tool_executions=(execution,),
                context_chars=0,
                memory_writes=(),
                stop_reason="complete",
                run_id="run_progress",
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr("nested_memvid_agent.channels.adapters.urlopen", fake_urlopen)
    cfg = replace(_config(tmp_path), enable_channel_delivery=True)
    manager = ChannelManager(
        cfg,
        agent_factory=lambda config: cast(NestedMV2Agent, ProgressAgent()),
        channel_configs=[ChannelEndpointConfig(id="telegram", provider="telegram", send_enabled=True, auto_reply=True)],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "update_id": 101,
            "message": {
                "message_id": 55,
                "text": "please inspect",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            },
        },
        send=True,
    )

    assert result.delivery.sent is True
    assert [call["url"] for call in calls] == [
        "https://api.telegram.org/bot123:test-token/sendChatAction",
        "https://api.telegram.org/bot123:test-token/sendMessage",
        "https://api.telegram.org/bot123:test-token/sendMessage",
        "https://api.telegram.org/bot123:test-token/sendMessage",
    ]
    assert calls[1]["payload"]["text"] == "🔧 Using tool: file.read"
    assert calls[2]["payload"]["text"] == "✅ Tool finished: file.read"
    assert calls[3]["payload"]["text"] == "Done after using a tool."


def test_discord_interaction_payload_is_normalized_and_dry_run_delivered(tmp_path: Path) -> None:
    manager = ChannelManager(_config(tmp_path))
    payload = {
        "id": "interaction_1",
        "application_id": "app_1",
        "token": "interaction_token",
        "channel_id": "channel_1",
        "guild_id": "guild_1",
        "data": {
            "name": "ask",
            "options": [{"name": "prompt", "value": "hello discord"}],
        },
        "member": {"user": {"id": "user_1"}},
    }

    result = manager.handle_payload(provider="discord", payload=payload)

    assert result.turn.session_id == "channel:discord:channel_1"
    assert result.inbound.user_id == "user_1"
    assert result.inbound.metadata["guild_id"] == "guild_1"
    assert result.turn.assistant_message == "Mock response: hello discord"
    assert result.delivery.dry_run is True
    assert result.delivery.request_json == {"content": "Mock response: hello discord"}


def test_custom_provider_uses_generic_webhook_adapter(tmp_path: Path) -> None:
    channel = ChannelEndpointConfig(id="slack-work", provider="slack")
    manager = ChannelManager(_config(tmp_path), channel_configs=[channel])

    result = manager.handle_payload(
        provider="slack",
        channel_id="slack-work",
        payload={"text": "hello custom channel", "thread_id": "T1", "user_id": "U1"},
    )

    assert result.inbound.channel == "slack"
    assert result.inbound.channel_id == "slack-work"
    assert result.turn.session_id == "channel:slack-work:T1"
    assert result.turn.assistant_message == "Mock response: hello custom channel"


def test_channel_webhook_signature_gate_rejects_bad_signatures(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("KESTREL_WEBHOOK_SECRET", "secret")  # type: ignore[attr-defined]
    channel = ChannelEndpointConfig(
        id="signed",
        provider="webhook",
        settings={"signature_secret_env": "KESTREL_WEBHOOK_SECRET"},
    )
    manager = ChannelManager(_config(tmp_path), channel_configs=[channel])
    payload = {"text": "hello signed channel", "conversation_id": "thread"}
    raw_body = b'{"text":"hello signed channel", "conversation_id":"thread"}'
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()

    result = manager.handle_payload(
        provider="webhook",
        channel_id="signed",
        payload=payload,
        raw_body=raw_body,
        headers={"x-kestrel-signature": f"sha256={signature}"},
    )

    assert result.turn.assistant_message == "Mock response: hello signed channel"

    try:
        manager.handle_payload(
            provider="webhook",
            channel_id="signed",
            payload=payload,
            raw_body=raw_body.replace(b"thread", b"other"),
            headers={"x-kestrel-signature": "sha256=bad"},
        )
    except Exception as exc:  # noqa: BLE001 - assert channel boundary error without importing pytest
        assert "Invalid webhook signature" in str(exc)
    else:
        raise AssertionError("Expected invalid signature to be rejected")


def test_telegram_webhook_secret_token_header_is_verified(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "telegram-secret")  # type: ignore[attr-defined]
    channel = ChannelEndpointConfig(
        id="telegram",
        provider="telegram",
        settings={
            "signature_provider": "telegram",
            "signature_secret_env": "TELEGRAM_WEBHOOK_SECRET",
        },
    )
    manager = ChannelManager(_config(tmp_path), channel_configs=[channel])
    payload = {
        "update_id": 101,
        "message": {
            "message_id": 55,
            "text": "hello telegram",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 777},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")

    result = manager.handle_payload(
        provider="telegram",
        payload=payload,
        raw_body=raw_body,
        headers={"x-telegram-bot-api-secret-token": "telegram-secret"},
        require_signature=True,
    )

    assert result.turn.assistant_message == "Mock response: hello telegram"

    try:
        manager.handle_payload(
            provider="telegram",
            payload=payload,
            raw_body=raw_body,
            headers={"x-telegram-bot-api-secret-token": "wrong"},
            require_signature=True,
        )
    except Exception as exc:  # noqa: BLE001 - assert channel boundary error without importing pytest
        assert "Invalid Telegram webhook secret token" in str(exc)
    else:
        raise AssertionError("Expected invalid Telegram secret token to be rejected")


def test_signed_webhook_uses_raw_body_not_canonical_json(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("KESTREL_WEBHOOK_SECRET", "secret")  # type: ignore[attr-defined]
    channel = ChannelEndpointConfig(
        id="signed",
        provider="webhook",
        settings={"signature_secret_env": "KESTREL_WEBHOOK_SECRET"},
    )
    manager = ChannelManager(_config(tmp_path), channel_configs=[channel])
    raw_body = b'{\n  "conversation_id": "thread",\n  "text": "raw bytes signed"\n}'
    payload = json.loads(raw_body)
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()

    result = manager.handle_payload(
        provider="webhook",
        channel_id="signed",
        payload=payload,
        raw_body=raw_body,
        headers={"x-kestrel-signature": f"sha256={signature}"},
    )

    assert result.turn.assistant_message == "Mock response: raw bytes signed"


def test_github_signature_header_is_supported(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")  # type: ignore[attr-defined]
    channel = ChannelEndpointConfig(
        id="github",
        provider="github",
        settings={"signature_secret_env": "GITHUB_WEBHOOK_SECRET"},
    )
    manager = ChannelManager(_config(tmp_path), channel_configs=[channel])
    raw_body = b'{"text":"hello github","conversation_id":"issue-1"}'
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()

    result = manager.handle_payload(
        provider="github",
        channel_id="github",
        payload=json.loads(raw_body),
        raw_body=raw_body,
        headers={"X-Hub-Signature-256": f"sha256={signature}"},
    )

    assert result.turn.session_id == "channel:github:issue-1"


def test_public_channel_webhook_rejects_unsigned_payloads_by_default(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/api/channels/webhook/webhook",
        content=b'{"conversation_id":"thread","text":"unsigned"}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert "Unsigned webhooks are disabled" in response.json()["detail"]


def test_public_channel_webhook_allows_explicit_unsigned_channel(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    channels = [
        {
            "id": "webhook",
            "provider": "webhook",
            "settings": {"unsigned_allowed": True},
        }
    ]
    (tmp_path / "channels.json").write_text(json.dumps({"channels": channels}), encoding="utf-8")
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/api/channels/webhook/webhook",
        content=b'{"conversation_id":"thread","text":"unsigned allowed"}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["assistant_message"] == "Mock response: unsigned allowed"


def test_unknown_explicit_channel_id_is_rejected(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("KESTREL_WEBHOOK_SECRET", "secret")  # type: ignore[attr-defined]
    signed = ChannelEndpointConfig(
        id="signed",
        provider="webhook",
        settings={"signature_secret_env": "KESTREL_WEBHOOK_SECRET"},
    )
    manager = ChannelManager(_config(tmp_path), channel_configs=[signed])

    try:
        manager.handle_payload(
            provider="webhook",
            channel_id="typo",
            payload={"text": "unsigned bypass", "conversation_id": "thread"},
        )
    except Exception as exc:  # noqa: BLE001 - assert channel boundary error without importing pytest
        assert "Unknown channel" in str(exc)
    else:
        raise AssertionError("Expected unknown explicit channel id to be rejected")

    assert not (tmp_path / "memory" / "working.memory.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://10.0.0.5/hook",
        "https://172.16.0.1/hook",
        "https://192.168.1.10/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_generic_webhook_delivery_rejects_unsafe_urls(url: str) -> None:
    delivery = GenericWebhookAdapter().build_delivery(
        ChannelEndpointConfig(
            id="webhook",
            provider="webhook",
            settings={"webhook_url": url},
        ),
        _outbound(),
        dry_run=False,
    )

    assert delivery.error
    assert "_request_url" not in delivery.request_json


def test_generic_webhook_delivery_allows_public_https() -> None:
    delivery = GenericWebhookAdapter().build_delivery(
        ChannelEndpointConfig(
            id="webhook",
            provider="webhook",
            settings={"webhook_url": "https://93.184.216.34/kestrel/hook"},
        ),
        _outbound(),
        dry_run=False,
    )

    assert delivery.error is None
    assert delivery.request_json["_request_url"] == "https://93.184.216.34/kestrel/hook"
    assert delivery.endpoint == "https://93.184.216.34/kestrel/hook"


def test_discord_webhook_delivery_rejects_unsafe_url() -> None:
    delivery = DiscordAdapter().build_delivery(
        ChannelEndpointConfig(
            id="discord",
            provider="discord",
            settings={"webhook_url": "https://127.0.0.1/discord"},
        ),
        _outbound(),
        dry_run=False,
    )

    assert delivery.error
    assert "_request_url" not in delivery.request_json


def test_server_exposes_channel_ingest_route(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(_config(tmp_path)))
    response = client.post(
        "/api/channels/ingest",
        json={
            "provider": "webhook",
            "payload": {"conversation_id": "thread", "text": "hello api channel"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "channel:webhook:thread"
    assert payload["assistant_message"] == "Mock response: hello api channel"
    assert payload["delivery"]["dry_run"] is True


def test_server_rejects_untrusted_host_and_origin(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(_config(tmp_path)))

    host_response = client.get("/api/health", headers={"host": "evil.example"})
    origin_response = client.get(
        "/api/health",
        headers={"host": "localhost:8765", "origin": "https://evil.example"},
    )

    assert host_response.status_code == 400
    assert origin_response.status_code == 403


def test_server_accepts_trusted_wildcard_tunnel_host(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    config = replace(
        _config(tmp_path),
        trusted_hosts=("127.0.0.1", "localhost", "*.trycloudflare.com"),
    )
    client = TestClient(create_app(config))

    response = client.get(
        "/api/health",
        headers={"host": "coming-emacs-experienced-dome.trycloudflare.com"},
    )

    assert response.status_code == 200


def test_telegram_run_manager_blocked_run_sends_inline_approval_prompt(tmp_path: Path) -> None:
    class FakeRun:
        run_id = "run_approval"
        session_id = "channel:telegram:12345"

    class FakeRunManager:
        def __init__(self) -> None:
            self.created: dict[str, Any] | None = None

        def create_run(self, **kwargs: Any) -> FakeRun:
            self.created = kwargs
            return FakeRun()

        def get_run(self, run_id: str) -> dict[str, Any]:
            assert run_id == "run_approval"
            return {
                "run_id": run_id,
                "status": "blocked",
                "assistant_message": "",
                "stop_reason": "approval_required",
                "approvals": [
                    {
                        "approval_id": "approval_123",
                        "status": "pending",
                        "tool_name": "file.write",
                        "risk": "high",
                        "arguments": {"path": "demo.txt", "content": "hi"},
                    }
                ],
            }

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[ChannelEndpointConfig(id="telegram", provider="telegram", send_enabled=True, auto_reply=True)],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "update_id": 101,
            "message": {
                "message_id": 55,
                "text": "write a file",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            },
        },
        send=True,
    )

    assert result.turn.run_id == "run_approval"
    assert "Approval required" in result.outbound.text
    assert result.delivery.request_json["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": "kestrel_approve:approval_123"},
                {"text": "Deny", "callback_data": "kestrel_deny:approval_123"},
            ]
        ]
    }


def test_telegram_approval_callback_decides_pending_approval(tmp_path: Path) -> None:
    class FakeRunManager:
        def __init__(self) -> None:
            self.decisions: list[tuple[str, bool]] = []

        def decide_approval(self, approval_id: str, *, approved: bool, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
            assert arguments is None
            self.decisions.append((approval_id, approved))
            return {"approval_id": approval_id, "run_id": "run_approval", "status": "approved" if approved else "denied"}

        def get_run(self, run_id: str) -> dict[str, Any]:
            return {"run_id": run_id, "status": "running", "assistant_message": ""}

    fake_runs = FakeRunManager()
    manager = ChannelManager(
        _config(tmp_path),
        run_manager=fake_runs,
        channel_configs=[ChannelEndpointConfig(id="telegram", provider="telegram", send_enabled=True, auto_reply=True)],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "update_id": 102,
            "callback_query": {
                "id": "callback_1",
                "data": "kestrel_approve:approval_123",
                "from": {"id": 777},
                "message": {"message_id": 56, "chat": {"id": 12345, "type": "private"}},
            },
        },
        send=True,
    )

    assert fake_runs.decisions == [("approval_123", True)]
    assert result.outbound.text == "Approved approval_123. Continuing…"


def test_telegram_run_manager_sends_followup_when_run_finishes_after_initial_timeout(tmp_path: Path) -> None:
    class FakeRun:
        run_id = "run_slow"
        session_id = "channel:telegram:12345"

    class FakeRunManager:
        def create_run(self, **kwargs: Any) -> FakeRun:
            return FakeRun()

        def get_run(self, run_id: str) -> dict[str, Any]:
            assert run_id == "run_slow"
            return {"run_id": run_id, "status": "running", "assistant_message": ""}

    class CaptureTelegramAdapter(ChannelAdapter):
        provider = "telegram"

        def __init__(self) -> None:
            self.outbounds: list[ChannelOutboundMessage] = []

        def parse_inbound(self, config: ChannelEndpointConfig, payload: dict[str, Any]) -> ChannelInboundMessage:
            return ChannelInboundMessage(
                channel="telegram",
                channel_id=config.id,
                conversation_id="12345",
                user_id="777",
                message_id="55",
                text="slow request",
            )

        def build_delivery(
            self,
            config: ChannelEndpointConfig,
            outbound: ChannelOutboundMessage,
            *,
            dry_run: bool,
            blocked_reason: str | None = None,
        ) -> ChannelDelivery:
            self.outbounds.append(outbound)
            return ChannelDelivery(
                channel="telegram",
                channel_id=config.id,
                conversation_id=outbound.conversation_id,
                sent=True,
                dry_run=dry_run,
                endpoint="capture://telegram",
                request_json={"text": outbound.text, **outbound.metadata},
                blocked_reason=blocked_reason,
            )

    adapter = CaptureTelegramAdapter()
    cfg = replace(_config(tmp_path), channel_send_timeout_seconds=0, timeout_seconds=1)
    manager = ChannelManager(
        cfg,
        adapters={"telegram": adapter},
        run_manager=FakeRunManager(),
        channel_configs=[ChannelEndpointConfig(id="telegram", provider="telegram", send_enabled=True, auto_reply=True)],
    )
    wait_calls = 0

    def fake_wait_for_run_payload(run_id: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        nonlocal wait_calls
        assert run_id == "run_slow"
        wait_calls += 1
        if wait_calls == 1:
            return {"run_id": run_id, "status": "running", "assistant_message": ""}
        return {
            "run_id": run_id,
            "status": "completed",
            "stop_reason": "complete",
            "assistant_message": "Which folder should I use under /Users/tiuni?",
        }

    manager._wait_for_run_payload = fake_wait_for_run_payload  # type: ignore[method-assign]

    result = manager.handle_payload(provider="telegram", payload={"message": {"text": "slow request"}}, send=True)

    import time

    deadline = time.monotonic() + 1
    while len(adapter.outbounds) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert result.outbound.text == "Kestrel accepted the request and is still working."
    assert [outbound.text for outbound in adapter.outbounds] == [
        "Kestrel accepted the request and is still working.",
        "Which folder should I use under /Users/tiuni?",
    ]
    assert adapter.outbounds[1].metadata["followup"] is True


def test_telegram_admin_command_requires_configured_owner(tmp_path: Path) -> None:
    class FakeRunManager:
        def create_run(self, **kwargs: Any) -> object:
            raise AssertionError("admin commands must not fall through to an agent run")

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                auto_reply=True,
                settings={"admin_user_ids": ["777"]},
            )
        ],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "message": {
                "message_id": 55,
                "text": "/status",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 999},
            }
        },
        send=True,
    )

    assert result.turn.stop_reason == "admin_unauthorized"
    assert result.outbound.text == "Telegram admin command denied: sender is not a configured Kestrel owner."


def test_telegram_admin_status_command_reports_runs_and_pending_approvals(tmp_path: Path) -> None:
    class FakeRunManager:
        def create_run(self, **kwargs: Any) -> object:
            raise AssertionError("admin commands must not create a normal run")

        def list_runs(self) -> list[dict[str, Any]]:
            return [
                {"run_id": "run_done", "status": "completed", "message": "done", "updated_at": "2026-05-24T01:00:00Z"},
                {"run_id": "run_blocked", "status": "blocked", "message": "needs approval", "updated_at": "2026-05-24T02:00:00Z"},
            ]

        def list_approvals(self) -> list[dict[str, Any]]:
            return [
                {
                    "approval_id": "approval_123",
                    "run_id": "run_blocked",
                    "tool_name": "file.write",
                    "risk": "high",
                    "status": "pending",
                }
            ]

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                auto_reply=True,
                settings={"admin_user_ids": ["777"]},
            )
        ],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "message": {
                "message_id": 55,
                "text": "/status",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            }
        },
        send=True,
    )

    assert result.turn.stop_reason == "admin_command"
    assert "Kestrel Telegram Admin" in result.outbound.text
    assert "completed: 1" in result.outbound.text
    assert "blocked: 1" in result.outbound.text
    assert "Pending approvals: 1" in result.outbound.text
    assert result.delivery.request_json["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "Approve file.write", "callback_data": "kestrel_approve:approval_123"},
                {"text": "Deny", "callback_data": "kestrel_deny:approval_123"},
            ]
        ]
    }


def test_telegram_admin_approval_command_uses_owner_and_exact_pending_arguments(tmp_path: Path) -> None:
    class FakeRunManager:
        def __init__(self) -> None:
            self.decisions: list[tuple[str, bool, dict[str, Any] | None]] = []

        def list_approvals(self) -> list[dict[str, Any]]:
            return [
                {
                    "approval_id": "approval_123",
                    "run_id": "run_blocked",
                    "tool_name": "file.write",
                    "risk": "high",
                    "status": "pending",
                    "arguments": {"path": "demo.txt", "content": "hi"},
                }
            ]

        def decide_approval(self, approval_id: str, *, approved: bool, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
            self.decisions.append((approval_id, approved, arguments))
            return {"approval_id": approval_id, "run_id": "run_blocked", "status": "approved"}

        def get_run(self, run_id: str) -> dict[str, Any]:
            return {"run_id": run_id, "status": "completed", "assistant_message": "approved result"}

    fake_runs = FakeRunManager()
    manager = ChannelManager(
        _config(tmp_path),
        run_manager=fake_runs,
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                auto_reply=True,
                settings={"admin_user_ids": ["777"]},
            )
        ],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "message": {
                "message_id": 55,
                "text": "/approve approval_123",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            }
        },
        send=True,
    )

    assert fake_runs.decisions == [("approval_123", True, {"path": "demo.txt", "content": "hi"})]
    assert result.outbound.text == "approved result"


def test_telegram_approval_callback_rejects_non_owner_when_owner_is_configured(tmp_path: Path) -> None:
    class FakeRunManager:
        def decide_approval(self, approval_id: str, *, approved: bool, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
            raise AssertionError("non-owner callback must not decide approval")

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                auto_reply=True,
                settings={"admin_user_ids": ["777"]},
            )
        ],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "callback_query": {
                "id": "callback_1",
                "data": "kestrel_approve:approval_123",
                "from": {"id": 999},
                "message": {"message_id": 56, "chat": {"id": 12345, "type": "private"}},
            }
        },
        send=True,
    )

    assert result.turn.stop_reason == "admin_unauthorized"
    assert result.outbound.text == "Telegram admin action denied: sender is not a configured Kestrel owner."


def test_channel_manager_reuses_agent_for_hot_path(tmp_path: Path) -> None:
    created = 0

    def factory(config: AgentConfig):
        nonlocal created
        created += 1
        from nested_memvid_agent.app_factory import build_agent

        return build_agent(config)

    manager = ChannelManager(_config(tmp_path), agent_factory=factory)

    manager.handle_payload(provider="webhook", payload={"text": "one", "conversation_id": "thread"})
    manager.handle_payload(provider="webhook", payload={"text": "two", "conversation_id": "thread"})
    manager.close()

    assert created == 1


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
        channel_config_path=tmp_path / "channels.json",
    )


def _outbound() -> ChannelOutboundMessage:
    return ChannelOutboundMessage(
        channel="webhook",
        channel_id="webhook",
        conversation_id="thread",
        text="hello",
    )


def _memory_snapshot(tmp_path: Path, layer: str) -> list[dict[str, object]]:
    raw = (tmp_path / "memory" / f"{layer}.memory.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert isinstance(payload, list)
    return [dict(item) for item in payload if isinstance(item, dict)]
