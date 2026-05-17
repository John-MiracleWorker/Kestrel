from __future__ import annotations

import json
import stat
from pathlib import Path

from nested_memvid_agent.secret_broker import SecretBroker


def test_secret_broker_never_returns_raw_value_in_public_payloads(tmp_path: Path) -> None:
    broker = SecretBroker(tmp_path / "vault.json")

    public = broker.store_secret(
        name="TELEGRAM_BOT_TOKEN",
        purpose="Enable Telegram channel delivery.",
        value="123456:ABC-super-secret",
        validate=True,
    )

    assert public["secret_ref"] == "secret://telegram_bot_token"
    assert public["configured"] is True
    assert public["validated"] is True
    assert broker.resolve("secret://telegram_bot_token") == "123456:ABC-super-secret"
    assert broker.resolve("TELEGRAM_BOT_TOKEN") == "123456:ABC-super-secret"
    assert "123456:ABC-super-secret" not in json.dumps(public)
    assert "123456:ABC-super-secret" not in json.dumps(broker.list_secrets())
    assert "value" not in public


def test_secret_broker_vault_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    broker = SecretBroker(path)

    broker.store_secret(name="GITHUB_TOKEN", purpose="MCP GitHub access.", value="ghp_secret")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
