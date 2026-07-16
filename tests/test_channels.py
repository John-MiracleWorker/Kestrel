from __future__ import annotations

import hashlib
import hmac
import json
import queue
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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


@pytest.fixture(autouse=True)
def _authorized_telegram_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "12345")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "777,999")


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


@pytest.mark.parametrize(
    ("chat_id", "user_id"),
    [(99999, 777), (12345, 888)],
)
def test_telegram_channel_rejects_senders_outside_environment_allowlists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_id: int,
    user_id: int,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "12345")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "777")
    manager = ChannelManager(_config(tmp_path))

    with pytest.raises(ValueError, match="not allowed"):
        manager.handle_payload(
            provider="telegram",
            payload={
                "update_id": 102,
                "message": {
                    "message_id": 56,
                    "text": "untrusted request",
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": user_id},
                },
            },
        )


@pytest.mark.parametrize(
    ("chat_ids", "user_ids"),
    [(None, None), ("", ""), ("not-a-chat", "not-a-user")],
)
def test_telegram_channel_fails_closed_for_missing_empty_or_malformed_allowlists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_ids: str | None,
    user_ids: str | None,
) -> None:
    if chat_ids is None:
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", chat_ids)
    if user_ids is None:
        monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", user_ids)
    manager = ChannelManager(_config(tmp_path))

    with pytest.raises(ValueError, match="not configured|not allowed"):
        manager.handle_payload(
            provider="telegram",
            payload={
                "message": {
                    "text": "must not run",
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 777},
                }
            },
        )

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


def test_signed_telegram_webhook_is_the_only_public_api_ingress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("KESTREL_TEST_API_TOKEN", "control-plane-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "telegram-webhook-secret")
    channels = [
        {
            "id": "telegram",
            "provider": "telegram",
            "enabled": True,
            "token_env": "TELEGRAM_BOT_TOKEN",
            "settings": {
                "allowed_conversation_ids": ["12345"],
                "allowed_user_ids": ["777"],
                "signature_provider": "telegram",
                "signature_secret_env": "TELEGRAM_WEBHOOK_SECRET",
                "unsigned_allowed": True,
            },
        }
    ]
    (tmp_path / "channels.json").write_text(
        json.dumps({"channels": channels}),
        encoding="utf-8",
    )
    config = replace(
        _config(tmp_path),
        channel_config_path=tmp_path / "channels.json",
        require_api_auth=True,
        api_auth_token_env="KESTREL_TEST_API_TOKEN",
    )
    client = TestClient(create_app(config))
    allowed_payload = {
        "update_id": 500,
        "message": {
            "message_id": 50,
            "text": "signed request",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 777},
        },
    }
    auth_headers = {"authorization": "Bearer control-plane-token"}
    webhook_headers = {"x-telegram-bot-api-secret-token": "telegram-webhook-secret"}

    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs", headers=auth_headers).json() == []
    assert client.post(
        "/api/channels/ingest",
        json={"provider": "telegram", "payload": allowed_payload},
    ).status_code == 401
    assert client.post(
        "/api/channels/telegram/webhook/",
        json=allowed_payload,
        headers=webhook_headers,
    ).status_code == 401

    missing_signature = client.post(
        "/api/channels/telegram/webhook",
        json=allowed_payload,
    )
    assert missing_signature.status_code == 400
    assert client.get("/api/runs", headers=auth_headers).json() == []

    denied_payload = json.loads(json.dumps(allowed_payload))
    denied_payload["message"]["chat"]["id"] = 99999
    denied = client.post(
        "/api/channels/telegram/webhook",
        json=denied_payload,
        headers=webhook_headers,
    )
    assert denied.status_code == 400
    assert "not allowed" in denied.json()["detail"]
    assert client.get("/api/runs", headers=auth_headers).json() == []

    accepted = client.post(
        "/api/channels/telegram/webhook",
        json=allowed_payload,
        headers=webhook_headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["session_id"] == "channel:telegram:12345"
    assert len(client.get("/api/runs", headers=auth_headers).json()) == 1


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


def test_server_exposes_telegram_webhook_setup_routes_redacting_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-super-secret")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "telegram-secret-token")
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true,"result":{"url":"https://kestrel.example/telegram"}}'

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        body = request.data.decode("utf-8") if getattr(request, "data", None) else "{}"
        calls.append({"url": request.full_url, "payload": json.loads(body), "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("nested_memvid_agent.net_safety.public_url_allowed", lambda url, require_https=False: (True, ""))
    monkeypatch.setattr("nested_memvid_agent.channels.adapters.urlopen", fake_urlopen)
    channel = {
        "id": "telegram",
        "provider": "telegram",
        "enabled": True,
        "send_enabled": True,
        "auto_reply": True,
        "token_env": "TELEGRAM_BOT_TOKEN",
        "settings": {
            "admin_enabled": True,
            "owner_user_ids": ["777"],
            "signature_provider": "telegram",
            "signature_secret_env": "TELEGRAM_WEBHOOK_SECRET",
        },
    }
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [channel]}), encoding="utf-8")
    config = replace(
        _config(tmp_path),
        channel_config_path=tmp_path / "channels.json",
        enable_channel_delivery=True,
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/api/channels/telegram/telegram/set-webhook",
        json={"url": "https://kestrel.example/api/channels/telegram/webhook?channel_id=telegram"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["method"] == "setWebhook"
    assert payload["delivery"]["endpoint"] == "https://api.telegram.org/bot<token>/setWebhook"
    encoded = json.dumps(payload)
    assert "123456:ABC-super-secret" not in encoded
    assert "telegram-secret-token" not in encoded
    assert payload["delivery"]["request_json"]["secret_token"] == "<configured>"
    assert calls[0]["url"] == "https://api.telegram.org/bot123456:ABC-super-secret/setWebhook"
    assert calls[0]["payload"]["secret_token"] == "telegram-secret-token"


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


def test_zero_bind_address_is_not_a_trusted_host_wildcard(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    config = replace(
        _config(tmp_path),
        trusted_hosts=("0.0.0.0", "localhost", "testserver"),
    )
    client = TestClient(create_app(config))

    host_response = client.get("/api/health", headers={"host": "evil.example"})
    origin_response = client.get(
        "/api/health",
        headers={"host": "localhost", "origin": "https://evil.example"},
    )

    assert host_response.status_code == 400
    assert origin_response.status_code == 403


def test_prometheus_metrics_require_api_auth_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("KESTREL_TEST_API_TOKEN", "metrics-token")
    config = replace(
        _config(tmp_path),
        require_api_auth=True,
        api_auth_token_env="KESTREL_TEST_API_TOKEN",
    )
    client = TestClient(create_app(config))

    unauthenticated = client.get("/metrics")
    authenticated = client.get(
        "/metrics",
        headers={"authorization": "Bearer metrics-token"},
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert "kestrel_up 1" in authenticated.text


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
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                send_enabled=True,
                auto_reply=True,
                settings={"admin_enabled": True, "owner_user_ids": ["777"]},
            )
        ],
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


def test_telegram_approval_callback_requires_explicit_admin_enablement(
    tmp_path: Path,
) -> None:
    class FakeRunManager:
        def decide_approval(
            self,
            approval_id: str,
            *,
            approved: bool,
            arguments: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            raise AssertionError("disabled Telegram admin must not decide approvals")

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                settings={"owner_user_ids": ["777"]},
            )
        ],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "callback_query": {
                "id": "callback_disabled_admin",
                "data": "kestrel_approve:approval_123",
                "from": {"id": 777},
                "message": {
                    "message_id": 56,
                    "chat": {"id": 12345, "type": "private"},
                },
            }
        },
        send=True,
    )

    assert result.turn.stop_reason == "admin_unauthorized"
    assert result.outbound.text == (
        "Telegram admin action denied: sender is not a configured Kestrel owner."
    )


def test_telegram_approval_callback_checks_chat_allowlist_before_decision(
    tmp_path: Path,
) -> None:
    class FakeRunManager:
        def decide_approval(
            self,
            approval_id: str,
            *,
            approved: bool,
            arguments: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            raise AssertionError("disallowed callback must not decide an approval")

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                settings={"owner_user_ids": ["777"]},
            )
        ],
    )

    with pytest.raises(ValueError, match="conversation is not allowed"):
        manager.handle_payload(
            provider="telegram",
            payload={
                "callback_query": {
                    "id": "callback_disallowed_chat",
                    "data": "kestrel_approve:approval_123",
                    "from": {"id": 777},
                    "message": {
                        "message_id": 57,
                        "chat": {"id": 99999, "type": "private"},
                    },
                }
            },
            send=True,
        )


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


def test_telegram_run_manager_batches_tool_progress_before_final_reply(tmp_path: Path) -> None:
    class FakeRun:
        run_id = "run_progress_managed"
        session_id = "channel:telegram:12345"

    class FakeEvents:
        def __init__(self) -> None:
            self.subscribers: list[queue.Queue[object]] = []

        def subscribe(self, run_id: str, after_id: int = 0) -> queue.Queue[object]:
            assert run_id == "run_progress_managed"
            assert after_id == 0
            subscriber: queue.Queue[object] = queue.Queue()
            self.subscribers.append(subscriber)
            return subscriber

        def unsubscribe(self, run_id: str, subscriber: queue.Queue[object]) -> None:
            assert run_id == "run_progress_managed"

    class FakeRunManager:
        def __init__(self) -> None:
            self.events = FakeEvents()

        def create_run(self, **kwargs: Any) -> FakeRun:
            return FakeRun()

        def get_run(self, run_id: str) -> dict[str, Any]:
            assert run_id == "run_progress_managed"
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
                text="please inspect",
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

    fake_runs = FakeRunManager()
    adapter = CaptureTelegramAdapter()
    cfg = replace(_config(tmp_path), channel_send_timeout_seconds=1, timeout_seconds=1)
    manager = ChannelManager(
        cfg,
        adapters={"telegram": adapter},
        run_manager=fake_runs,
        channel_configs=[ChannelEndpointConfig(id="telegram", provider="telegram", send_enabled=True, auto_reply=True)],
    )

    def fake_wait_for_run_payload(run_id: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + 1
        while not fake_runs.events.subscribers and time.monotonic() < deadline:
            time.sleep(0.01)
        subscriber = fake_runs.events.subscribers[0]
        for index in range(3):
            subscriber.put(SimpleNamespace(type="tool.started", payload={"tool": "file.read", "tool_call_id": f"tool_read_{index}"}))
            subscriber.put(SimpleNamespace(type="tool.completed", payload={"tool": "file.read", "tool_call_id": f"tool_read_{index}"}))
        subscriber.put(SimpleNamespace(type="run.completed", payload={"stop_reason": "complete"}))
        deadline = time.monotonic() + 1
        while len(adapter.outbounds) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        return {
            "run_id": run_id,
            "status": "completed",
            "stop_reason": "complete",
            "assistant_message": "Done after using a tool.",
        }

    manager._wait_for_run_payload = fake_wait_for_run_payload  # type: ignore[method-assign]

    result = manager.handle_payload(provider="telegram", payload={"message": {"text": "please inspect"}}, send=True)

    assert result.outbound.text == "Done after using a tool."
    assert [outbound.text for outbound in adapter.outbounds] == [
        "🧰 Tool activity: 3 calls completed. Tools: file.read.",
        "Done after using a tool.",
    ]


def test_telegram_max_tool_rounds_reply_includes_run_summary(tmp_path: Path) -> None:
    class FakeState:
        def list_run_steps(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
            assert run_id == "run_exhausted"
            return [
                {
                    "type": "tool.executed",
                    "payload": {"tool": "file.read", "success": True},
                },
                {
                    "type": "tool.executed",
                    "payload": {"tool": "repo.search", "success": True},
                },
                {
                    "type": "tool.failed",
                    "payload": {"tool": "file.read", "error": "file_read_failed"},
                },
            ]

    class FakeRunManager:
        state = FakeState()

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[ChannelEndpointConfig(id="telegram", provider="telegram", send_enabled=True, auto_reply=True)],
    )

    text, metadata = manager._run_reply(
        "run_exhausted",
        {
            "run_id": "run_exhausted",
            "status": "failed",
            "stop_reason": "max_tool_rounds",
            "assistant_message": "Stopped after max tool rounds.",
            "error": "Stopped after max tool rounds.",
        },
        fallback_to_working=False,
    )

    assert metadata["stop_reason"] == "max_tool_rounds"
    assert "Reached the tool-iteration limit" in text
    assert "Tool summary: 3 tool events" in text
    assert "file.read ×2" in text
    assert "repo.search ×1" in text
    assert "file_read_failed" in text


def test_telegram_allowed_user_is_not_implicitly_an_admin_owner(tmp_path: Path) -> None:
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

    assert result.turn.stop_reason == "admin_disabled"
    assert result.outbound.text == "Telegram admin command denied: admin mode is disabled."


@pytest.mark.parametrize("owners", [[], ["777", "999"]])
def test_telegram_admin_mode_requires_exactly_one_explicit_owner(
    tmp_path: Path,
    owners: list[str],
) -> None:
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
                settings={"admin_enabled": True, "owner_user_ids": owners},
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

    assert result.turn.stop_reason == "admin_unauthorized"
    assert result.outbound.text == (
        "Telegram admin command denied: sender is not a configured Kestrel owner."
    )


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
                settings={"admin_enabled": True, "admin_user_ids": ["777"]},
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
                settings={"admin_enabled": True, "admin_user_ids": ["777"]},
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
                settings={"admin_enabled": True, "admin_user_ids": ["777"]},
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


def test_telegram_natural_language_max_tool_calls_requires_confirmation(tmp_path: Path) -> None:
    class FakeRunManager:
        def __init__(self) -> None:
            self.config = _config(tmp_path)

        def create_run(self, **kwargs: Any) -> object:
            raise AssertionError("natural-language admin settings changes must not create a normal run")

    fake_runs = FakeRunManager()
    manager = ChannelManager(
        _config(tmp_path),
        run_manager=fake_runs,
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                auto_reply=True,
                settings={"admin_enabled": True, "owner_user_ids": ["777"]},
            )
        ],
    )

    preview = manager.handle_payload(
        provider="telegram",
        payload={
            "message": {
                "message_id": 55,
                "text": "increase max tool calls to 12",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            }
        },
        send=True,
    )

    assert preview.turn.stop_reason == "admin_confirmation_required"
    assert "Confirm Telegram admin action" in preview.outbound.text
    assert "Set max tool calls to 12" in preview.outbound.text
    assert manager.config.max_tool_rounds != 12
    confirm_data = preview.delivery.request_json["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert str(confirm_data).startswith("kestrel_admin_confirm:")

    applied = manager.handle_payload(
        provider="telegram",
        payload={
            "callback_query": {
                "id": "callback_confirm",
                "data": confirm_data,
                "from": {"id": 777},
                "message": {"message_id": 56, "chat": {"id": 12345, "type": "private"}},
            }
        },
        send=True,
    )

    assert applied.turn.stop_reason == "admin_action_confirmed"
    assert applied.outbound.text == "Max tool calls set to 12."
    assert manager.config.max_tool_rounds == 12
    assert fake_runs.config.max_tool_rounds == 12


def test_telegram_admin_confirmation_expires_fail_closed(tmp_path: Path) -> None:
    class FakeRunManager:
        def __init__(self) -> None:
            self.config = _config(tmp_path)

        def create_run(self, **kwargs: Any) -> object:
            raise AssertionError("admin settings changes must not create a normal run")

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                auto_reply=True,
                settings={"admin_enabled": True, "owner_user_ids": ["777"]},
            )
        ],
    )
    preview = manager.handle_payload(
        provider="telegram",
        payload={
            "message": {
                "message_id": 55,
                "text": "increase max tool calls to 12",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            }
        },
        send=True,
    )
    confirm_data = preview.delivery.request_json["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    confirmation_id = str(confirm_data).removeprefix("kestrel_admin_confirm:")
    manager._pending_admin_confirmations[confirmation_id]["created_at"] = time.time() - 301

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "callback_query": {
                "id": "callback_expired",
                "data": confirm_data,
                "from": {"id": 777},
                "message": {
                    "message_id": 56,
                    "chat": {"id": 12345, "type": "private"},
                },
            }
        },
        send=True,
    )

    assert result.turn.stop_reason == "admin_confirmation_expired"
    assert manager.config.max_tool_rounds != 12


def test_telegram_admin_confirmation_is_bound_to_original_owner(tmp_path: Path) -> None:
    class FakeRunManager:
        def __init__(self) -> None:
            self.config = _config(tmp_path)

        def create_run(self, **kwargs: Any) -> object:
            raise AssertionError("admin settings changes must not create a normal run")

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                auto_reply=True,
                settings={"admin_enabled": True, "owner_user_ids": ["777"]},
            )
        ],
    )
    preview = manager.handle_payload(
        provider="telegram",
        payload={
            "message": {
                "message_id": 55,
                "text": "increase max tool calls to 12",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            }
        },
        send=True,
    )
    confirm_data = preview.delivery.request_json["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    manager.upsert_channel(
        {
            "id": "telegram",
            "provider": "telegram",
            "auto_reply": True,
            "settings": {"admin_enabled": True, "owner_user_ids": ["999"]},
        }
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "callback_query": {
                "id": "callback_new_owner",
                "data": confirm_data,
                "from": {"id": 999},
                "message": {
                    "message_id": 56,
                    "chat": {"id": 12345, "type": "private"},
                },
            }
        },
        send=True,
    )

    assert result.turn.stop_reason == "admin_confirmation_owner_mismatch"
    assert manager.config.max_tool_rounds != 12


def test_telegram_natural_language_raw_secret_is_rejected(tmp_path: Path) -> None:
    class FakeRunManager:
        def create_run(self, **kwargs: Any) -> object:
            raise AssertionError("raw secret admin text must not create a normal run")

    manager = ChannelManager(
        _config(tmp_path),
        run_manager=FakeRunManager(),
        channel_configs=[
            ChannelEndpointConfig(
                id="telegram",
                provider="telegram",
                auto_reply=True,
                settings={"admin_enabled": True, "owner_user_ids": ["777"]},
            )
        ],
    )

    result = manager.handle_payload(
        provider="telegram",
        payload={
            "message": {
                "message_id": 55,
                "text": "set TELEGRAM_BOT_TOKEN to 123456:ABC-super-secret",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 777},
            }
        },
        send=True,
    )

    assert result.turn.stop_reason == "admin_secret_rejected"
    assert "Raw secrets are not accepted through Telegram" in result.outbound.text
    assert "123456:ABC-super-secret" not in result.outbound.text


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
                settings={"admin_enabled": True, "admin_user_ids": ["777"]},
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
