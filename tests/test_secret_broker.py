from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from nested_memvid_agent.secret_broker import KeyringSecretBroker, SecretBroker, build_secret_broker


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


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


def test_secret_status_does_not_enumerate_arbitrary_env_vars(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("UNREGISTERED_SECRET_NAME", "super-secret-value")  # type: ignore[attr-defined]
    broker = SecretBroker(tmp_path / "vault.json", allowed_env_names={"REGISTERED_SECRET"})

    unknown = broker.status("UNREGISTERED_SECRET_NAME")

    assert unknown == {
        "source_env": "UNREGISTERED_SECRET_NAME",
        "configured": False,
        "validated": False,
        "source": "unregistered",
    }


def test_registered_env_status_is_allowed_without_leaking_value(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("REGISTERED_SECRET", "super-secret-value")  # type: ignore[attr-defined]
    broker = SecretBroker(tmp_path / "vault.json", allowed_env_names={"REGISTERED_SECRET"})

    status = broker.status("REGISTERED_SECRET")

    assert status["configured"] is True
    assert status["source"] == "env"
    assert "super-secret-value" not in json.dumps(status)


def test_secret_fingerprint_uses_vault_salt(tmp_path: Path) -> None:
    broker_a = SecretBroker(tmp_path / "vault-a.json")
    broker_b = SecretBroker(tmp_path / "vault-b.json")

    first = broker_a.store_secret(name="TOKEN", purpose="first", value="same-value")
    second = broker_b.store_secret(name="TOKEN", purpose="second", value="same-value")

    assert first["fingerprint"].startswith("sha256:")
    assert first["fingerprint"] != second["fingerprint"]
    assert "fingerprint_salt" in json.loads((tmp_path / "vault-a.json").read_text(encoding="utf-8"))


def test_keyring_secret_broker_stores_raw_value_outside_metadata_file(tmp_path: Path) -> None:
    path = tmp_path / "keyring-metadata.json"
    broker = KeyringSecretBroker(path, keyring=FakeKeyring())

    public = broker.store_secret(name="GITHUB_TOKEN", purpose="GitHub MCP access.", value="ghp_raw_secret", validate=True)

    assert public["configured"] is True
    assert public["validated"] is True
    assert broker.resolve("secret://github_token") == "ghp_raw_secret"
    assert broker.resolve("GITHUB_TOKEN") == "ghp_raw_secret"
    raw_metadata = path.read_text(encoding="utf-8")
    assert "ghp_raw_secret" not in raw_metadata
    assert "ghp_raw_secret" not in json.dumps(broker.list_secrets())


def test_keyring_secret_broker_delete_removes_keyring_value(tmp_path: Path) -> None:
    fake_keyring = FakeKeyring()
    broker = KeyringSecretBroker(tmp_path / "keyring-metadata.json", keyring=fake_keyring)
    broker.store_secret(name="TOKEN", purpose="test", value="raw-token")

    broker.delete_secret("token")

    assert broker.resolve("secret://token") is None
    assert fake_keyring.values == {}


def test_build_secret_broker_falls_back_to_json_when_keyring_missing(tmp_path: Path, monkeypatch: object) -> None:
    def missing_keyring(_: str) -> Any:
        raise ImportError("missing keyring")

    monkeypatch.setattr("nested_memvid_agent.secret_broker.import_module", missing_keyring)  # type: ignore[attr-defined]

    broker = build_secret_broker(tmp_path / "vault.json", backend="keyring")

    assert type(broker) is SecretBroker
