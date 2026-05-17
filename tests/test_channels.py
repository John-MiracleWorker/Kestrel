from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from nested_memvid_agent.channels import ChannelEndpointConfig, ChannelManager
from nested_memvid_agent.channels.adapters import DiscordAdapter, GenericWebhookAdapter
from nested_memvid_agent.channels.models import ChannelOutboundMessage
from nested_memvid_agent.config import AgentConfig
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
