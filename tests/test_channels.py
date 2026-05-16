from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from nested_memvid_agent.channels import ChannelEndpointConfig, ChannelManager
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
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(b"secret", canonical, hashlib.sha256).hexdigest()

    result = manager.handle_payload(
        provider="webhook",
        channel_id="signed",
        payload=payload,
        headers={"x-kestrel-signature": f"sha256={signature}"},
    )

    assert result.turn.assistant_message == "Mock response: hello signed channel"

    try:
        manager.handle_payload(
            provider="webhook",
            channel_id="signed",
            payload=payload,
            headers={"x-kestrel-signature": "sha256=bad"},
        )
    except Exception as exc:  # noqa: BLE001 - assert channel boundary error without importing pytest
        assert "Invalid webhook signature" in str(exc)
    else:
        raise AssertionError("Expected invalid signature to be rejected")


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


def _memory_snapshot(tmp_path: Path, layer: str) -> list[dict[str, object]]:
    raw = (tmp_path / "memory" / f"{layer}.memory.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert isinstance(payload, list)
    return [dict(item) for item in payload if isinstance(item, dict)]
