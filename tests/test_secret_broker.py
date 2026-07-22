from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.secret_broker import (
    KeyringSecretBroker,
    SecretBroker,
    SecretBrokerPartialCommitError,
    build_secret_broker,
)
from nested_memvid_agent.security_boundary import redact_text


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.set_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.fail_deletes = False
        self.fail_set_usernames: set[str] = set()

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.set_calls.append((service_name, username))
        if username in self.fail_set_usernames:
            raise RuntimeError("injected keyring set failure")
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        self.get_calls.append((service_name, username))
        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        self.delete_calls.append((service_name, username))
        if self.fail_deletes:
            raise RuntimeError("injected keyring delete failure")
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


def test_secret_broker_restart_registers_opaque_existing_values_for_redaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "innocently-named-data.json"
    raw_value = "opaque-restart-value-c4f91b3e2a"
    path.write_text(
        json.dumps(
            {
                "secrets": {
                    "service": {
                        "id": "service",
                        "name": "SERVICE_CONFIGURATION",
                        "purpose": "legacy local vault",
                        "value": raw_value,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    SecretBroker(path)

    assert redact_text(f"subprocess echoed {raw_value}", environ={}) == (
        "subprocess echoed <redacted>"
    )


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


def test_keyring_secret_broker_bounds_long_ids_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "keyring-metadata.json"
    fake_keyring = FakeKeyring()
    broker = KeyringSecretBroker(path, keyring=fake_keyring)
    long_name = "A" * 1_025

    public = broker.store_secret(
        name=long_name,
        purpose="Exercise bounded keyring identifiers.",
        value="long-id-secret-value",
    )

    secret_ref = str(public["secret_ref"])
    secret_id = secret_ref.removeprefix("secret://")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert len(secret_id) <= 240
    assert metadata["secrets"][secret_id]["id"] == secret_id
    assert "long-id-secret-value" not in json.dumps(metadata)

    restarted = KeyringSecretBroker(path, keyring=fake_keyring)

    assert restarted.resolve(secret_ref) == "long-id-secret-value"


def test_keyring_backend_refuses_populated_json_vault_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "populated-json-vault.json"
    json_broker = SecretBroker(path)
    json_broker.store_secret(
        name="TOKEN",
        purpose="Existing plaintext vault.",
        value="json-to-keyring-migration-secret",
    )
    before = path.read_bytes()
    fake_keyring = FakeKeyring()

    with pytest.raises(ValueError, match="Refusing to open a populated JSON secret vault"):
        KeyringSecretBroker(path, keyring=fake_keyring)

    assert path.read_bytes() == before
    assert b"json-to-keyring-migration-secret" in before
    assert fake_keyring.values == {}


def test_keyring_backend_reopens_and_deletes_legacy_oversized_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-keyring-metadata.json"
    legacy_id = "a" * 1_025
    path.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "fingerprint_salt": "legacy-salt",
                "secrets": {
                    legacy_id: {
                        "id": legacy_id,
                        "name": "LEGACY_LONG_TOKEN",
                        "purpose": "Pre-v2 oversized identifier.",
                        "validated": True,
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    fake_keyring = FakeKeyring()
    fake_keyring.values[("kestrel.secret_broker", legacy_id)] = "legacy-long-id-secret"

    broker = KeyringSecretBroker(path, keyring=fake_keyring)

    assert broker.resolve(f"secret://{legacy_id}") == "legacy-long-id-secret"
    assert broker.get_secret(legacy_id)["configured"] is True
    broker.delete_secret(legacy_id)
    assert broker.resolve(f"secret://{legacy_id}") is None
    assert fake_keyring.values == {}


def test_json_backend_refuses_keyring_metadata_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "keyring-metadata.json"
    fake_keyring = FakeKeyring()
    keyring_broker = KeyringSecretBroker(path, keyring=fake_keyring)
    keyring_broker.store_secret(
        name="TOKEN",
        purpose="Keep the keyring pointer authoritative.",
        value="keyring-downgrade-secret",
    )
    before = path.read_bytes()
    keyring_values_before = dict(fake_keyring.values)

    with pytest.raises(ValueError, match="Refusing to open keyring metadata"):
        SecretBroker(path)

    assert path.read_bytes() == before
    assert fake_keyring.values == keyring_values_before
    assert b"keyring-downgrade-secret" not in before


def test_keyring_secret_broker_delete_removes_keyring_value(tmp_path: Path) -> None:
    fake_keyring = FakeKeyring()
    broker = KeyringSecretBroker(tmp_path / "keyring-metadata.json", keyring=fake_keyring)
    broker.store_secret(name="TOKEN", purpose="test", value="raw-token")

    broker.delete_secret("token")

    assert broker.resolve("secret://token") is None
    assert fake_keyring.values == {}


def test_keyring_overwrite_double_failure_keeps_old_version_and_reconciles_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "keyring-metadata.json"
    fake_keyring = FakeKeyring()
    broker = KeyringSecretBroker(path, keyring=fake_keyring)
    original_value = "original-version-secret"
    replacement_value = "replacement-version-secret"
    broker.store_secret(name="TOKEN", purpose="test", value=original_value)
    original_metadata = json.loads(path.read_text(encoding="utf-8"))
    original_username = original_metadata["secrets"]["token"]["keyring_username"]
    assert original_username != "token"

    real_write = broker._write_unlocked  # noqa: SLF001 - deterministic double-fault seam
    write_count = 0

    def fail_active_pointer_commit(data: dict[str, Any]) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected metadata commit failure")
        real_write(data)

    monkeypatch.setattr(broker, "_write_unlocked", fail_active_pointer_commit)
    fake_keyring.fail_deletes = True

    with pytest.raises(SecretBrokerPartialCommitError) as raised:
        broker.store_secret(name="TOKEN", purpose="updated", value=replacement_value)

    error = raised.value
    assert error.operation == "store"
    assert error.stage == "metadata_commit_cleanup_pending"
    assert error.secret_ids == ("token",)
    assert len(error.recovery_usernames) == 1
    recovery_username = error.recovery_usernames[0]
    assert recovery_username != original_username
    assert original_value not in str(error)
    assert replacement_value not in str(error)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["secrets"]["token"]["keyring_username"] == original_username
    assert recovery_username in persisted["keyring_pending_cleanup"]
    assert broker.resolve("secret://token") == original_value
    assert fake_keyring.values[(broker.service_name, original_username)] == original_value
    assert fake_keyring.values[(broker.service_name, recovery_username)] == replacement_value
    assert original_value not in path.read_text(encoding="utf-8")
    assert replacement_value not in path.read_text(encoding="utf-8")

    fake_keyring.fail_deletes = False
    restarted = KeyringSecretBroker(path, keyring=fake_keyring)

    assert restarted.resolve("secret://token") == original_value
    assert (broker.service_name, recovery_username) not in fake_keyring.values
    reconciled = json.loads(path.read_text(encoding="utf-8"))
    assert reconciled["keyring_pending_cleanup"] == {}
    assert reconciled["secrets"]["token"]["keyring_username"] == original_username


def test_keyring_overwrite_postcommit_error_never_deletes_visible_active_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "keyring-metadata.json"
    fake_keyring = FakeKeyring()
    broker = KeyringSecretBroker(path, keyring=fake_keyring)
    broker.store_secret(name="TOKEN", purpose="test", value="original-secret")
    original_username = json.loads(path.read_text(encoding="utf-8"))["secrets"]["token"][
        "keyring_username"
    ]
    fake_keyring.delete_calls.clear()
    real_write = broker._write_unlocked  # noqa: SLF001 - post-commit failure seam
    write_count = 0

    def fail_after_active_pointer_commit(data: dict[str, Any]) -> None:
        nonlocal write_count
        write_count += 1
        real_write(data)
        if write_count == 2:
            raise OSError("injected post-replace metadata failure")

    monkeypatch.setattr(broker, "_write_unlocked", fail_after_active_pointer_commit)

    with pytest.raises(SecretBrokerPartialCommitError) as raised:
        broker.store_secret(name="TOKEN", purpose="updated", value="replacement-secret")

    error = raised.value
    assert error.stage == "metadata_commit_uncertain"
    active_username = error.recovery_usernames[0]
    assert active_username != original_username
    assert (broker.service_name, active_username) not in fake_keyring.delete_calls
    assert broker.resolve("secret://token") == "replacement-secret"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["secrets"]["token"]["keyring_username"] == active_username
    assert original_username in persisted["keyring_pending_cleanup"]
    assert fake_keyring.values[(broker.service_name, original_username)] == "original-secret"
    assert fake_keyring.values[(broker.service_name, active_username)] == "replacement-secret"

    restarted = KeyringSecretBroker(path, keyring=fake_keyring)

    assert restarted.resolve("secret://token") == "replacement-secret"
    assert (broker.service_name, original_username) not in fake_keyring.values
    assert fake_keyring.values[(broker.service_name, active_username)] == "replacement-secret"


def test_keyring_delete_postcommit_tombstone_error_remains_fail_closed_and_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "keyring-metadata.json"
    fake_keyring = FakeKeyring()
    broker = KeyringSecretBroker(path, keyring=fake_keyring)
    broker.store_secret(name="TOKEN", purpose="test", value="delete-secret")
    active_username = json.loads(path.read_text(encoding="utf-8"))["secrets"]["token"][
        "keyring_username"
    ]
    fake_keyring.delete_calls.clear()
    real_write = broker._write_unlocked  # noqa: SLF001 - post-commit failure seam

    def fail_after_tombstone_commit(data: dict[str, Any]) -> None:
        real_write(data)
        raise OSError("injected post-replace tombstone failure")

    monkeypatch.setattr(broker, "_write_unlocked", fail_after_tombstone_commit)

    with pytest.raises(SecretBrokerPartialCommitError) as raised:
        broker.delete_secret("token")

    error = raised.value
    assert error.stage == "tombstone_commit_uncertain"
    assert error.recovery_usernames == (active_username,)
    assert fake_keyring.delete_calls == []
    assert fake_keyring.values[(broker.service_name, active_username)] == "delete-secret"
    assert broker.resolve("secret://token") is None
    assert json.loads(path.read_text(encoding="utf-8"))["secrets"]["token"][
        "keyring_state"
    ] == "pending_delete"

    restarted = KeyringSecretBroker(path, keyring=fake_keyring)

    assert restarted.resolve("secret://token") is None
    assert fake_keyring.values == {}
    assert json.loads(path.read_text(encoding="utf-8"))["secrets"] == {}


def test_keyring_delete_metadata_failure_never_attempts_value_rollback_and_reconciles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "keyring-metadata.json"
    fake_keyring = FakeKeyring()
    broker = KeyringSecretBroker(path, keyring=fake_keyring)
    raw_value = "delete-double-fault-secret"
    broker.store_secret(name="TOKEN", purpose="test", value=raw_value)
    active_username = json.loads(path.read_text(encoding="utf-8"))["secrets"]["token"][
        "keyring_username"
    ]
    fake_keyring.set_calls.clear()
    fake_keyring.fail_set_usernames.add(active_username)
    real_write = broker._write_unlocked  # noqa: SLF001 - deterministic double-fault seam
    write_count = 0

    def fail_final_delete_metadata(data: dict[str, Any]) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected delete metadata failure")
        real_write(data)

    monkeypatch.setattr(broker, "_write_unlocked", fail_final_delete_metadata)

    with pytest.raises(SecretBrokerPartialCommitError) as raised:
        broker.delete_secret("token")

    error = raised.value
    assert error.operation == "delete"
    assert error.stage == "final_metadata_pending"
    assert error.recovery_usernames == (active_username,)
    assert raw_value not in str(error)
    assert fake_keyring.set_calls == []
    assert (broker.service_name, active_username) not in fake_keyring.values
    pending = json.loads(path.read_text(encoding="utf-8"))["secrets"]["token"]
    assert pending["keyring_state"] == "pending_delete"
    assert pending["keyring_delete_usernames"] == [active_username]
    assert broker.resolve("secret://token") is None

    restarted = KeyringSecretBroker(path, keyring=fake_keyring)

    assert restarted.resolve("secret://token") is None
    assert json.loads(path.read_text(encoding="utf-8"))["secrets"] == {}


def test_keyring_delete_failure_stays_recoverable_and_fail_closed_until_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "keyring-metadata.json"
    fake_keyring = FakeKeyring()
    broker = KeyringSecretBroker(path, keyring=fake_keyring)
    raw_value = "recoverable-delete-secret"
    broker.store_secret(name="TOKEN", purpose="test", value=raw_value)
    active_username = json.loads(path.read_text(encoding="utf-8"))["secrets"]["token"][
        "keyring_username"
    ]
    fake_keyring.fail_deletes = True

    with pytest.raises(SecretBrokerPartialCommitError) as raised:
        broker.delete_secret("token")

    error = raised.value
    assert error.stage == "keyring_delete_pending"
    assert error.recovery_usernames == (active_username,)
    assert raw_value not in str(error)
    assert fake_keyring.values[(broker.service_name, active_username)] == raw_value
    monkeypatch.setenv("TOKEN", "must-not-substitute-pending-broker-secret")
    assert broker.resolve("secret://token") is None
    assert broker.resolve("TOKEN") is None
    assert broker.status("secret://token")["configured"] is False
    assert broker.status("TOKEN")["configured"] is False
    pending = json.loads(path.read_text(encoding="utf-8"))["secrets"]["token"]
    assert pending["keyring_state"] == "pending_delete"

    fake_keyring.fail_deletes = False
    restarted = KeyringSecretBroker(path, keyring=fake_keyring)

    assert restarted.resolve("secret://token") is None
    assert fake_keyring.values == {}
    assert json.loads(path.read_text(encoding="utf-8"))["secrets"] == {}


def test_keyring_legacy_sid_metadata_migrates_without_changing_public_reference(
    tmp_path: Path,
) -> None:
    path = tmp_path / "keyring-metadata.json"
    path.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "fingerprint_salt": "legacy-test-salt",
                "secrets": {
                    "token": {
                        "id": "token",
                        "name": "TOKEN",
                        "purpose": "legacy keyring entry",
                        "validated": False,
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    fake_keyring = FakeKeyring()
    fake_keyring.values[("kestrel.secret_broker", "token")] = "legacy-raw-secret"

    broker = KeyringSecretBroker(path, keyring=fake_keyring)

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["keyring_metadata_version"] == 2
    assert migrated["secrets"]["token"]["keyring_username"] == "token"
    assert migrated["secrets"]["token"]["keyring_state"] == "active"
    assert fake_keyring.get_calls == []
    assert broker.resolve("secret://token") == "legacy-raw-secret"
    assert broker.get_secret("token")["secret_ref"] == "secret://token"

    broker.store_secret(name="TOKEN", purpose="migrated", value="versioned-raw-secret")

    versioned = json.loads(path.read_text(encoding="utf-8"))
    active_username = versioned["secrets"]["token"]["keyring_username"]
    assert active_username != "token"
    assert (broker.service_name, "token") not in fake_keyring.values
    assert broker.resolve("secret://token") == "versioned-raw-secret"
    assert "legacy-raw-secret" not in path.read_text(encoding="utf-8")
    assert "versioned-raw-secret" not in path.read_text(encoding="utf-8")


def test_keyring_reconciliation_never_deletes_an_active_alias(tmp_path: Path) -> None:
    path = tmp_path / "keyring-metadata.json"
    path.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "keyring_metadata_version": 2,
                "keyring_pending_cleanup": {},
                "secrets": {
                    "doomed": {
                        "id": "doomed",
                        "name": "DOOMED",
                        "keyring_username": "shared-version",
                        "keyring_state": "pending_delete",
                        "keyring_delete_usernames": ["shared-version"],
                    },
                    "keeper": {
                        "id": "keeper",
                        "name": "KEEPER",
                        "keyring_username": "shared-version",
                        "keyring_state": "active",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    fake_keyring = FakeKeyring()
    fake_keyring.values[("kestrel.secret_broker", "shared-version")] = "shared-raw-secret"

    broker = KeyringSecretBroker(path, keyring=fake_keyring)

    assert broker.resolve("secret://doomed") is None
    assert broker.resolve("secret://keeper") == "shared-raw-secret"
    assert fake_keyring.delete_calls == []
    assert json.loads(path.read_text(encoding="utf-8"))["secrets"]["doomed"][
        "keyring_state"
    ] == "pending_delete"


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
