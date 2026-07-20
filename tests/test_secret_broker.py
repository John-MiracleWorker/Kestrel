from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.secret_broker import KeyringSecretBroker, SecretBroker, build_secret_broker
from nested_memvid_agent.security_boundary import redact_text


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
    assert redact_text("echo=123456:ABC-super-secret", environ={}) == "echo=<redacted>"


@pytest.mark.skipif(os.name == "nt", reason="Windows stat modes do not expose NTFS ACLs")
def test_secret_broker_vault_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    broker = SecretBroker(path)

    broker.store_secret(name="GITHUB_TOKEN", purpose="MCP GitHub access.", value="ghp_secret")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows stat modes do not expose NTFS ACLs")
def test_secret_broker_repairs_existing_vault_before_first_read(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    path.write_text(
        json.dumps(
            {
                "secrets": {
                    "token": {
                        "id": "token",
                        "name": "TOKEN",
                        "purpose": "legacy vault",
                        "value": "legacy-secret",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o644)

    broker = SecretBroker(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert broker.resolve("secret://token") == "legacy-secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX link and mode contract")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_secret_broker_rejects_vault_alias_without_mutating_target(
    tmp_path: Path,
    link_kind: str,
) -> None:
    outside = tmp_path / "outside-vault.json"
    outside.write_text('{"secrets": {"token": {"value": "outside-secret"}}}', encoding="utf-8")
    os.chmod(outside, 0o644)
    path = tmp_path / "vault.json"
    if link_kind == "symlink":
        path.symlink_to(outside)
    else:
        path.hardlink_to(outside)

    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        SecretBroker(path)

    assert outside.read_text(encoding="utf-8") == (
        '{"secrets": {"token": {"value": "outside-secret"}}}'
    )
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX link and mode contract")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_secret_broker_rejects_lock_alias_without_mutating_target(
    tmp_path: Path,
    link_kind: str,
) -> None:
    outside = tmp_path / "outside-vault-lock"
    outside.write_text("outside lock", encoding="utf-8")
    os.chmod(outside, 0o644)
    lock_path = tmp_path / ".vault.json.lock"
    if link_kind == "symlink":
        lock_path.symlink_to(outside)
    else:
        lock_path.hardlink_to(outside)

    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        SecretBroker(tmp_path / "vault.json")

    assert outside.read_text(encoding="utf-8") == "outside lock"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


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


def test_build_secret_broker_fails_closed_when_keyring_missing(tmp_path: Path, monkeypatch: object) -> None:
    def missing_keyring(_: str) -> Any:
        raise ImportError("missing keyring")

    monkeypatch.setattr("nested_memvid_agent.secret_broker.import_module", missing_keyring)  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="keyring package is unavailable"):
        build_secret_broker(tmp_path / "vault.json", backend="keyring")


def test_build_secret_broker_fails_closed_without_usable_keyring_backend(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    class UnusableBackend:
        priority = 0

    class UnusableKeyringModule:
        @staticmethod
        def get_keyring() -> UnusableBackend:
            return UnusableBackend()

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "nested_memvid_agent.secret_broker.import_module",
        lambda _: UnusableKeyringModule(),
    )

    with pytest.raises(RuntimeError, match="no usable OS keyring backend"):
        build_secret_broker(tmp_path / "vault.json", backend="keyring")


def test_secret_broker_atomic_replace_is_owner_only_before_exposure(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    import nested_memvid_agent.secret_broker as secret_broker_module

    path = tmp_path / "vault.json"
    observed_modes: list[int] = []
    real_replace = secret_broker_module.os.replace

    def inspect_replace(source: str | Path, destination: str | Path) -> None:
        observed_modes.append(stat.S_IMODE(Path(source).stat().st_mode))
        assert not Path(destination).exists()
        real_replace(source, destination)

    monkeypatch.setattr(secret_broker_module.os, "replace", inspect_replace)  # type: ignore[attr-defined]

    SecretBroker(path).store_secret(name="TOKEN", purpose="test", value="raw-token-value")

    assert len(observed_modes) == 1
    if os.name != "nt":
        assert observed_modes == [0o600]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secret_broker_failed_atomic_replace_preserves_previous_vault(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    import nested_memvid_agent.secret_broker as secret_broker_module

    path = tmp_path / "vault.json"
    broker = SecretBroker(path)
    broker.store_secret(name="TOKEN", purpose="test", value="original-token-value")

    def fail_replace(_: str | Path, __: str | Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(secret_broker_module.os, "replace", fail_replace)  # type: ignore[attr-defined]

    with pytest.raises(OSError, match="injected replace failure"):
        broker.store_secret(name="TOKEN", purpose="test", value="replacement-token-value")

    assert broker.resolve("secret://token") == "original-token-value"
    assert not list(tmp_path.glob(".vault.json.*.tmp"))


def test_secret_broker_cross_process_writes_do_not_lose_records(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from nested_memvid_agent.secret_broker import SecretBroker\n"
        "SecretBroker(Path(sys.argv[1])).store_secret("
        "name=sys.argv[2], purpose='concurrency test', value=sys.argv[3])\n"
    )
    processes = [
        subprocess.Popen(  # noqa: S603 - fixed interpreter and inline test script
            [sys.executable, "-c", script, str(path), f"TOKEN_{index}", f"secret-value-{index}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(6)
    ]

    for process in processes:
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr

    broker = SecretBroker(path)
    assert {item["name"] for item in broker.list_secrets()} == {
        f"TOKEN_{index}" for index in range(6)
    }
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
