from __future__ import annotations

import json
import os
import zipfile
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.security_boundary import redact_text
from nested_memvid_agent.support_bundle import export_support_bundle

_FailBackend = type(
    "Keyring",
    (),
    {
        "__module__": "keyring.backends.fail",
        "priority": 0,
    },
)


class _ThirdPartyBackend:
    __module__ = "third_party.keyring"
    priority = 10


class _MemoryKeyringBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.get_calls: list[tuple[str, str]] = []

    def set_password(
        self,
        service_name: str,
        username: str,
        password: str,
    ) -> None:
        self.values[(service_name, username)] = password

    def get_password(
        self,
        service_name: str,
        username: str,
    ) -> str | None:
        self.get_calls.append((service_name, username))
        return self.values.get((service_name, username))

    def delete_password(
        self,
        service_name: str,
        username: str,
    ) -> None:
        self.values.pop((service_name, username), None)


_MacBackend = type(
    "Keyring",
    (_MemoryKeyringBackend,),
    {
        "__module__": "keyring.backends.macOS",
        "priority": 5,
    },
)
_MacLookalike = type(
    "NotKeyring",
    (),
    {
        "__module__": "keyring.backends.macOS",
        "priority": 5,
    },
)


_WindowsBackend = type(
    "WinVaultKeyring",
    (_MemoryKeyringBackend,),
    {
        "__module__": "keyring.backends.Windows",
        "priority": 5,
    },
)
_WindowsLookalike = type(
    "Keyring",
    (),
    {
        "__module__": "keyring.backends.Windows",
        "priority": 5,
    },
)


_LinuxBackend = type(
    "Keyring",
    (_MemoryKeyringBackend,),
    {
        "__module__": "keyring.backends.SecretService",
        "priority": 5,
    },
)
_LinuxLookalike = type(
    "SecretServiceKeyring",
    (),
    {
        "__module__": "keyring.backends.SecretService",
        "priority": 5,
    },
)


def _init_chainer(
    self: object,
    backends: list[object],
) -> None:
    self.backends = backends  # type: ignore[attr-defined]


_ChainerBackend = type(
    "ChainerBackend",
    (),
    {
        "__module__": "keyring.backends.chainer",
        "priority": 10,
        "__init__": _init_chainer,
    },
)
_ChainerLookalike = type(
    "Keyring",
    (),
    {
        "__module__": "keyring.backends.chainer",
        "priority": 10,
        "__init__": _init_chainer,
    },
)


_KeyringLocked = type(
    "KeyringLocked",
    (RuntimeError,),
    {"__module__": "keyring.errors"},
)

class _FakeKeyringModule:
    def __init__(
        self,
        backend: object | BaseException,
    ) -> None:
        self.backend = backend
        self.values: dict[tuple[str, str], str] = {}
        self.get_calls: list[tuple[str, str]] = []

    def get_keyring(self) -> object:
        if isinstance(self.backend, BaseException):
            raise self.backend
        return self.backend

    def set_password(
        self,
        service_name: str,
        username: str,
        password: str,
    ) -> None:
        self.values[(service_name, username)] = password

    def get_password(
        self,
        service_name: str,
        username: str,
    ) -> str | None:
        self.get_calls.append((service_name, username))
        return self.values.get((service_name, username))

    def delete_password(
        self,
        service_name: str,
        username: str,
    ) -> None:
        self.values.pop((service_name, username), None)


def _desktop_credentials() -> Any:
    return import_module("nested_memvid_agent.desktop_credentials")


def _readiness(
    *,
    state: str,
    backend: str | None,
    persistence: str,
    reason: str,
    remediation: str,
) -> dict[str, object]:
    return {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": state,
        "backend": backend,
        "persistence": persistence,
        "reason": reason,
        "remediation": remediation,
    }


@pytest.mark.parametrize(
    ("platform_name", "backend", "label"),
    [
        ("darwin", _MacBackend(), "macOS Keychain"),
        ("win32", _WindowsBackend(), "Windows Credential Manager"),
        ("linux", _LinuxBackend(), "Linux Secret Service"),
    ],
)
def test_desktop_accepts_only_the_expected_native_backend_class(
    tmp_path: Path,
    platform_name: str,
    backend: object,
    label: str,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / platform_name
    keyring = _FakeKeyringModule(backend)

    selected = module.select_desktop_credentials(
        profile,
        platform_name=platform_name,
        load_keyring=lambda: keyring,
        probe_backend=lambda candidate: candidate,
    )

    assert selected.readiness.to_public_payload() == _readiness(
        state="available",
        backend=label,
        persistence="persistent",
        reason="ready",
        remediation="No recovery needed.",
    )
    assert selected.broker.__class__.__name__ == (
        "KeyringSecretBroker"
    )
    assert selected.broker.vault_path == (
        profile / "secrets" / "desktop-keyring-metadata.json"
    )
    assert keyring.get_calls == []


def test_native_backend_stays_unverified_until_an_authorized_store_succeeds(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    backend = _MacBackend()

    selected = module.select_desktop_credentials(
        profile,
        platform_name="darwin",
        load_keyring=lambda: _FakeKeyringModule(backend),
    )

    assert selected.broker is not None
    assert selected.readiness.to_public_payload() == {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": "unavailable",
        "backend": "macOS Keychain",
        "persistence": "none",
        "reason": "backend_unverified",
        "remediation": (
            "Complete an authorized credential operation to verify "
            "the operating system credential backend."
        ),
    }
    assert backend.get_calls == []
    assert backend.values == {}

    selected.broker.store_secret(
        name="OPENAI_API_KEY",
        purpose="Desktop provider API key for OpenAI.",
        value="authorized-readiness-private",
        secret_id="openai_api_key",
        validate=False,
    )

    assert (
        selected.broker.desktop_readiness.to_public_payload()
        == _readiness(
            state="available",
            backend="macOS Keychain",
            persistence="persistent",
            reason="ready",
            remediation="No recovery needed.",
        )
    )


def test_native_selection_does_not_reconcile_keyring_material_at_startup(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    metadata = (
        profile
        / "secrets"
        / "desktop-keyring-metadata.json"
    )
    metadata.parent.mkdir(parents=True)
    pending_username = "openai_api_key.v2.pending"
    metadata.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "keyring_metadata_version": 2,
                "fingerprint_salt": "metadata-salt",
                "keyring_pending_cleanup": {
                    pending_username: {
                        "secret_id": "openai_api_key",
                        "reason": "uncommitted_version",
                    }
                },
                "secrets": {},
            }
        ),
        encoding="utf-8",
    )
    before = metadata.read_bytes()
    operations: list[str] = []

    def set_password(
        _self: object,
        _service_name: str,
        _username: str,
        _password: str,
    ) -> None:
        operations.append("set")

    def get_password(
        _self: object,
        _service_name: str,
        _username: str,
    ) -> str | None:
        operations.append("get")
        return "startup-must-not-read-private"

    def delete_password(
        _self: object,
        _service_name: str,
        _username: str,
    ) -> None:
        operations.append("delete")

    backend_type = type(
        "Keyring",
        (),
        {
            "__module__": "keyring.backends.macOS",
            "priority": 5,
            "set_password": set_password,
            "get_password": get_password,
            "delete_password": delete_password,
        },
    )

    selected = module.select_desktop_credentials(
        profile,
        platform_name="darwin",
        load_keyring=lambda: _FakeKeyringModule(
            backend_type()
        ),
    )

    assert selected.broker is not None
    assert selected.readiness.reason == "backend_unverified"
    assert operations == []
    assert metadata.read_bytes() == before


@pytest.mark.parametrize(
    ("probe", "expected_state"),
    [
        (lambda: False, "session_only"),
        (lambda: True, "unavailable"),
        (
            lambda: (_ for _ in ()).throw(
                RuntimeError("availability-private-detail")
            ),
            "unavailable",
        ),
        (
            lambda: (_ for _ in ()).throw(
                ModuleNotFoundError("support-private-detail")
            ),
            "unavailable",
        ),
    ],
    ids=[
        "genuinely-absent",
        "present-but-failing",
        "probe-failed",
        "probe-support-missing",
    ],
)
def test_linux_session_fallback_requires_safe_absence_probe(
    tmp_path: Path,
    probe: Any,
    expected_state: str,
) -> None:
    module = _desktop_credentials()

    selected = module.select_desktop_credentials(
        tmp_path / "profile",
        platform_name="linux",
        load_keyring=lambda: _FakeKeyringModule(
            _FailBackend()
        ),
        linux_secret_service_probe=probe,
        fingerprint_salt=lambda: "session-test-salt",
    )

    payload = selected.readiness.to_public_payload()
    assert payload["state"] == expected_state
    assert (
        selected.broker is not None
    ) is (expected_state == "session_only")
    assert "private-detail" not in json.dumps(payload)


def test_explicit_fail_backend_configuration_never_uses_session_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _desktop_credentials()
    monkeypatch.setenv(
        "PYTHON_KEYRING_BACKEND",
        "keyring.backends.fail.Keyring",
    )

    selected = module.select_desktop_credentials(
        tmp_path / "profile",
        platform_name="linux",
        load_keyring=lambda: _FakeKeyringModule(
            _FailBackend()
        ),
        linux_secret_service_probe=lambda: False,
    )

    assert selected.broker is None
    assert selected.readiness.state == "unavailable"


@pytest.mark.skipif(
    os.name != "posix",
    reason="D-Bus Secret Service probe is Linux/POSIX-only; Path mocks assume POSIX path semantics",
)
def test_linux_secret_service_probe_only_lists_dbus_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _desktop_credentials()
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(
        module.Path,
        "is_file",
        lambda path: str(path) == "/usr/bin/gdbus",
    )
    monkeypatch.setattr(
        module.os,
        "access",
        lambda path, mode: (
            str(path) == "/usr/bin/gdbus"
            and mode == module.os.X_OK
        ),
    )

    def run(
        command: list[str],
        **options: object,
    ) -> object:
        calls.append((command, options))
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout="(['org.freedesktop.secrets'],)",
        )

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module._probe_linux_secret_service() is True
    assert [command for command, _options in calls] == [
        [
            "/usr/bin/gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            method,
        ]
        for method in (
            "org.freedesktop.DBus.ListNames",
            "org.freedesktop.DBus.ListActivatableNames",
        )
    ]
    assert all(
        "org.freedesktop.secrets" not in command
        for command, _options in calls
    )
    assert all(
        options
        == {
            "check": False,
            "stdout": module.subprocess.PIPE,
            "stderr": module.subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 2,
        }
        for _command, options in calls
    )


@pytest.mark.parametrize(
    ("platform_name", "expected_backend", "label"),
    [
        ("darwin", _MacBackend(), "macOS Keychain"),
        ("win32", _WindowsBackend(), "Windows Credential Manager"),
        ("linux", _LinuxBackend(), "Linux Secret Service"),
    ],
)
def test_desktop_unwraps_only_one_matching_native_backend_from_chainer(
    tmp_path: Path,
    platform_name: str,
    expected_backend: object,
    label: str,
) -> None:
    module = _desktop_credentials()
    keyring = _FakeKeyringModule(
        _ChainerBackend(
            [_ThirdPartyBackend(), expected_backend]
        )
    )

    selected = module.select_desktop_credentials(
        tmp_path / platform_name,
        platform_name=platform_name,
        load_keyring=lambda: keyring,
        probe_backend=lambda candidate: candidate,
    )

    assert selected.readiness.to_public_payload() == _readiness(
        state="available",
        backend=label,
        persistence="persistent",
        reason="ready",
        remediation="No recovery needed.",
    )


def test_desktop_chainer_broker_dispatches_only_to_selected_native_backend(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    native_values: dict[tuple[str, str], str] = {}

    def set_password(
        _self: object,
        service_name: str,
        username: str,
        password: str,
    ) -> None:
        native_values[(service_name, username)] = password

    def get_password(
        _self: object,
        service_name: str,
        username: str,
    ) -> str | None:
        return native_values.get((service_name, username))

    def delete_password(
        _self: object,
        service_name: str,
        username: str,
    ) -> None:
        native_values.pop((service_name, username), None)

    native_backend_type = type(
        "Keyring",
        (),
        {
            "__module__": "keyring.backends.macOS",
            "priority": 5,
            "set_password": set_password,
            "get_password": get_password,
            "delete_password": delete_password,
        },
    )
    native_backend = native_backend_type()
    keyring = _FakeKeyringModule(
        _ChainerBackend(
            [_ThirdPartyBackend(), native_backend]
        )
    )
    raw_value = "selected-native-only-private-sentinel"

    selected = module.select_desktop_credentials(
        tmp_path / "profile",
        platform_name="darwin",
        load_keyring=lambda: keyring,
        probe_backend=lambda candidate: candidate,
    )
    public = selected.broker.store_secret(
        name="OPENAI_API_KEY",
        purpose="Desktop provider API key for OpenAI.",
        value=raw_value,
        secret_id="openai_api_key",
        validate=False,
    )

    assert raw_value in native_values.values()
    assert keyring.values == {}
    assert public["secret_ref"] == "secret://openai_api_key"


@pytest.mark.parametrize(
    ("platform_name", "backend"),
    [
        (
            "darwin",
            _ChainerBackend(
                [_ChainerBackend([_MacBackend()])]
            ),
        ),
        (
            "darwin",
            _ChainerBackend(
                [_MacBackend(), _MacBackend()]
            ),
        ),
        ("darwin", _ChainerBackend([_LinuxBackend()])),
        ("linux", _ChainerBackend([_ThirdPartyBackend()])),
    ],
)
def test_desktop_rejects_nested_or_nonmatching_chainer_backends(
    tmp_path: Path,
    platform_name: str,
    backend: object,
) -> None:
    module = _desktop_credentials()
    selected = module.select_desktop_credentials(
        tmp_path / platform_name,
        platform_name=platform_name,
        load_keyring=lambda: _FakeKeyringModule(backend),
    )

    assert selected.broker is None
    assert selected.readiness.to_public_payload() == _readiness(
        state="unavailable",
        backend=None,
        persistence="none",
        reason="unsupported_backend",
        remediation=(
            "Install and unlock the operating system credential backend."
        ),
    )


def test_desktop_persistent_broker_keeps_raw_value_out_of_metadata(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    backend = _MacBackend()
    keyring = _FakeKeyringModule(backend)
    selected = module.select_desktop_credentials(
        profile,
        platform_name="darwin",
        load_keyring=lambda: keyring,
        probe_backend=lambda candidate: candidate,
    )
    raw_value = "desktop-keyring-private-sentinel"

    public = selected.broker.store_secret(
        name="OPENAI_API_KEY",
        purpose="Desktop provider API key for OpenAI.",
        value=raw_value,
        secret_id="openai_api_key",
        validate=False,
    )
    metadata = (
        profile
        / "secrets"
        / "desktop-keyring-metadata.json"
    ).read_bytes()

    assert raw_value in backend.values.values()
    assert keyring.values == {}
    assert raw_value.encode() not in metadata
    assert raw_value not in json.dumps(public)
    assert public["secret_ref"] == "secret://openai_api_key"
    assert public["validated"] is False
    assert not (profile / "secrets" / "local_vault.json").exists()


@pytest.mark.parametrize(
    ("platform_name", "backend"),
    [
        ("darwin", _MacLookalike()),
        ("win32", _WindowsLookalike()),
        ("linux", _LinuxLookalike()),
        (
            "darwin",
            _ChainerLookalike([_MacBackend()]),
        ),
    ],
    ids=[
        "macos-same-module-wrong-class",
        "windows-same-module-wrong-class",
        "linux-same-module-wrong-class",
        "chainer-same-module-wrong-class",
    ],
)
def test_desktop_rejects_same_module_wrong_class_lookalikes(
    tmp_path: Path,
    platform_name: str,
    backend: object,
) -> None:
    module = _desktop_credentials()
    selected = module.select_desktop_credentials(
        tmp_path / platform_name,
        platform_name=platform_name,
        load_keyring=lambda: _FakeKeyringModule(backend),
    )

    assert selected.broker is None
    assert selected.readiness.to_public_payload() == _readiness(
        state="unavailable",
        backend=None,
        persistence="none",
        reason="unsupported_backend",
        remediation=(
            "Install and unlock the operating system credential backend."
        ),
    )


def test_linux_without_secret_service_uses_nonpersistent_session_broker(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    keyring = _FakeKeyringModule(_FailBackend())

    selected = module.select_desktop_credentials(
        profile,
        platform_name="linux",
        load_keyring=lambda: keyring,
        linux_secret_service_probe=lambda: False,
        fingerprint_salt=lambda: "session-test-salt",
        clock=lambda: "2026-07-30T00:00:00+00:00",
    )

    assert selected.readiness.to_public_payload() == {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": "session_only",
        "backend": "Session memory",
        "persistence": "session",
        "reason": "secret_service_missing",
        "remediation": (
            "Start an unlocked Linux Secret Service to keep "
            "credentials across restarts."
        ),
    }
    public = selected.broker.store_secret(
        name="OPENAI_API_KEY",
        purpose="Desktop provider API key for OpenAI.",
        value="linux-session-private-sentinel",
        secret_id="openai_api_key",
        validate=False,
    )
    assert public["secret_ref"] == "secret://openai_api_key"
    assert public["validated"] is False
    assert "linux-session-private-sentinel" not in json.dumps(public)
    assert (
        selected.broker.resolve("secret://openai_api_key")
        == "linux-session-private-sentinel"
    )
    assert redact_text(
        "echo linux-session-private-sentinel",
        environ={},
    ) == "echo <redacted>"
    assert not list(profile.rglob("*"))

    restarted = module.select_desktop_credentials(
        profile,
        platform_name="linux",
        load_keyring=lambda: keyring,
        linux_secret_service_probe=lambda: False,
        fingerprint_salt=lambda: "new-session-salt",
        clock=lambda: "2026-07-30T00:01:00+00:00",
    )
    assert restarted.broker.resolve("secret://openai_api_key") is None


@pytest.mark.parametrize("platform_name", ["darwin", "win32"])
def test_desktop_rejects_arbitrary_positive_priority_backends(
    tmp_path: Path,
    platform_name: str,
) -> None:
    module = _desktop_credentials()
    keyring = _FakeKeyringModule(_ThirdPartyBackend())

    selected = module.select_desktop_credentials(
        tmp_path / "profile",
        platform_name=platform_name,
        load_keyring=lambda: keyring,
    )

    assert selected.broker is None
    assert selected.readiness.to_public_payload() == {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": "unavailable",
        "backend": None,
        "persistence": "none",
        "reason": "unsupported_backend",
        "remediation": (
            "Install and unlock the operating system credential backend."
        ),
    }
    assert keyring.get_calls == []


def test_missing_keyring_package_is_unavailable_without_file_fallback(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()

    def missing_package() -> object:
        raise ModuleNotFoundError("private-module-detail")

    selected = module.select_desktop_credentials(
        tmp_path / "profile",
        platform_name="darwin",
        load_keyring=missing_package,
    )

    assert selected.broker is None
    assert selected.readiness.to_public_payload() == _readiness(
        state="unavailable",
        backend=None,
        persistence="none",
        reason="keyring_package_missing",
        remediation=(
            "Reinstall Kestrel so the bundled keyring package is available."
        ),
    )
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    ("platform_name", "label"),
    [
        ("darwin", "macOS Keychain"),
        ("win32", "Windows Credential Manager"),
        ("linux", "Linux Secret Service"),
    ],
)
def test_locked_native_vault_has_an_exact_locked_readiness(
    tmp_path: Path,
    platform_name: str,
    label: str,
) -> None:
    module = _desktop_credentials()
    keyring = _FakeKeyringModule(
        _KeyringLocked("native-private-detail")
    )

    selected = module.select_desktop_credentials(
        tmp_path / platform_name,
        platform_name=platform_name,
        load_keyring=lambda: keyring,
    )

    payload = selected.readiness.to_public_payload()
    assert selected.broker is None
    assert payload == _readiness(
        state="locked_vault_required",
        backend=label,
        persistence="none",
        reason="vault_locked",
        remediation=(
            "Unlock the operating system credential store and retry."
        ),
    )
    assert "native-private-detail" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("platform_name", "backend", "label"),
    [
        ("darwin", _MacBackend(), "macOS Keychain"),
        ("win32", _WindowsBackend(), "Windows Credential Manager"),
        ("linux", _LinuxBackend(), "Linux Secret Service"),
    ],
)
def test_explicit_safe_native_backend_probe_failure_is_unavailable(
    tmp_path: Path,
    platform_name: str,
    backend: object,
    label: str,
) -> None:
    module = _desktop_credentials()

    def failed_probe(_candidate: object) -> object:
        raise RuntimeError("probe-private-detail")

    selected = module.select_desktop_credentials(
        tmp_path / platform_name,
        platform_name=platform_name,
        load_keyring=lambda: _FakeKeyringModule(backend),
        probe_backend=failed_probe,
    )

    payload = selected.readiness.to_public_payload()
    assert selected.broker is None
    assert payload == _readiness(
        state="unavailable",
        backend=label,
        persistence="none",
        reason="backend_probe_failed",
        remediation=(
            "Repair or unlock the operating system credential backend."
        ),
    )
    assert "probe-private-detail" not in json.dumps(payload)


def test_existing_keyring_metadata_prevents_linux_session_downgrade(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    metadata = (
        profile
        / "secrets"
        / "desktop-keyring-metadata.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "keyring_metadata_version": 2,
                "fingerprint_salt": "metadata-salt",
                "keyring_pending_cleanup": {},
                "secrets": {},
            }
        ),
        encoding="utf-8",
    )
    before = metadata.read_bytes()

    selected = module.select_desktop_credentials(
        profile,
        platform_name="linux",
        load_keyring=lambda: _FakeKeyringModule(
            _FailBackend()
        ),
        linux_secret_service_probe=lambda: False,
    )

    assert selected.broker is None
    assert selected.readiness.to_public_payload() == _readiness(
        state="unavailable",
        backend="Linux Secret Service",
        persistence="none",
        reason="secret_service_missing",
        remediation=(
            "Start and unlock Linux Secret Service; existing "
            "credential metadata cannot use session storage."
        ),
    )
    assert metadata.read_bytes() == before


def test_authorized_store_reconciles_pending_keyring_cleanup(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    metadata = (
        profile
        / "secrets"
        / "desktop-keyring-metadata.json"
    )
    metadata.parent.mkdir(parents=True)
    username = "stale_secret.v2.pending"
    metadata.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "keyring_metadata_version": 2,
                "fingerprint_salt": "metadata-salt",
                "keyring_pending_cleanup": {
                    username: {
                        "secret_id": "stale_secret",
                        "reason": "uncommitted_version",
                    }
                },
                "secrets": {},
            }
        ),
        encoding="utf-8",
    )
    backend = _MacBackend()
    keyring = _FakeKeyringModule(backend)
    backend.values[
        ("kestrel.secret_broker", username)
    ] = "pending-private-sentinel"

    selected = module.select_desktop_credentials(
        profile,
        platform_name="darwin",
        load_keyring=lambda: keyring,
    )

    assert selected.broker is not None
    untouched = json.loads(metadata.read_text(encoding="utf-8"))
    assert username in untouched["keyring_pending_cleanup"]
    assert (
        "kestrel.secret_broker",
        username,
    ) in backend.values
    assert selected.broker.resolve("") is None
    assert selected.broker.metadata_status(
        "OPENAI_API_KEY"
    )["configured"] is False
    assert json.loads(
        metadata.read_text(encoding="utf-8")
    ) == untouched
    assert (
        "kestrel.secret_broker",
        username,
    ) in backend.values

    selected.broker.store_secret(
        name="OPENAI_API_KEY",
        purpose="Desktop provider API key for OpenAI.",
        value="replacement-private-sentinel",
        secret_id="openai_api_key",
        validate=False,
    )

    reconciled = json.loads(
        metadata.read_text(encoding="utf-8")
    )
    assert reconciled["backend"] == "keyring"
    assert reconciled["keyring_metadata_version"] == 2
    assert reconciled["keyring_pending_cleanup"] == {}
    assert (
        "kestrel.secret_broker",
        username,
    ) not in backend.values
    assert keyring.values == {}


@pytest.mark.parametrize(
    "process_control",
    [KeyboardInterrupt(), SystemExit(17)],
)
def test_desktop_selector_never_swallows_process_control(
    tmp_path: Path,
    process_control: BaseException,
) -> None:
    module = _desktop_credentials()

    def interrupted_load() -> object:
        raise process_control

    with pytest.raises(type(process_control)):
        module.select_desktop_credentials(
            tmp_path / "profile",
            platform_name="darwin",
            load_keyring=interrupted_load,
        )


@pytest.mark.parametrize(
    "raw_metadata",
    [
        b"\xff",
        b"[]",
        (
            b'{"backend":"keyring","keyring_metadata_version":2,'
            b'"secrets":{"token":{"id":"token","value":"raw"}}}'
        ),
        (
            b'{"backend":"keyring","keyring_metadata_version":2,'
            b'"secrets":"not-an-object"}'
        ),
    ],
    ids=[
        "invalid-utf8",
        "non-object",
        "raw-value",
        "invalid-keyring-shape",
    ],
)
def test_invalid_or_raw_desktop_metadata_locks_without_probe_or_mutation(
    tmp_path: Path,
    raw_metadata: bytes,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    metadata = (
        profile
        / "secrets"
        / "desktop-keyring-metadata.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(raw_metadata)
    load_calls: list[str] = []

    selected = module.select_desktop_credentials(
        profile,
        platform_name="linux",
        load_keyring=lambda: load_calls.append("load"),
    )

    assert selected.broker is None
    assert selected.readiness.to_public_payload() == _readiness(
        state="locked_vault_required",
        backend=None,
        persistence="none",
        reason="metadata_invalid",
        remediation=(
            "Repair or retire the invalid Desktop keyring "
            "metadata before storing credentials."
        ),
    )
    assert metadata.read_bytes() == raw_metadata
    assert load_calls == []


@pytest.mark.parametrize(
    "raw_metadata",
    [
        b"{}",
        b'{"secrets":{}}',
        b'{"backend":"json","secrets":{}}',
    ],
    ids=[
        "empty-object",
        "empty-vault",
        "empty-json-backend",
    ],
)
def test_empty_desktop_metadata_is_adopted_only_during_authorized_store(
    tmp_path: Path,
    raw_metadata: bytes,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    metadata = (
        profile
        / "secrets"
        / "desktop-keyring-metadata.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(raw_metadata)
    keyring = _FakeKeyringModule(_MacBackend())

    selected = module.select_desktop_credentials(
        profile,
        platform_name="darwin",
        load_keyring=lambda: keyring,
    )

    assert selected.readiness.to_public_payload() == _readiness(
        state="unavailable",
        backend="macOS Keychain",
        persistence="none",
        reason="backend_unverified",
        remediation=(
            "Complete an authorized credential operation to verify "
            "the operating system credential backend."
        ),
    )
    assert metadata.read_bytes() == raw_metadata
    assert keyring.get_calls == []

    selected.broker.store_secret(
        name="OPENAI_API_KEY",
        purpose="Desktop provider API key for OpenAI.",
        value="adoption-private-sentinel",
        secret_id="openai_api_key",
        validate=False,
    )

    adopted = json.loads(metadata.read_text(encoding="utf-8"))
    assert adopted["backend"] == "keyring"
    assert adopted["keyring_metadata_version"] == 2
    assert set(adopted["secrets"]) == {"openai_api_key"}
    assert selected.broker.desktop_readiness.reason == "ready"


def test_exact_keyring_metadata_status_never_retrieves_a_value(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    metadata = (
        profile
        / "secrets"
        / "desktop-keyring-metadata.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "keyring_metadata_version": 2,
                "fingerprint_salt": "metadata-salt",
                "keyring_pending_cleanup": {},
                "secrets": {
                    "openai_api_key": {
                        "id": "openai_api_key",
                        "name": "OPENAI_API_KEY",
                        "purpose": (
                            "Desktop provider API key for OpenAI."
                        ),
                        "validated": False,
                        "last_validated_at": None,
                        "created_at": "2026-07-30T00:00:00+00:00",
                        "updated_at": "2026-07-30T00:00:00+00:00",
                        "keyring_username": (
                            "openai_api_key.v2.0123456789abcdef"
                        ),
                        "keyring_state": "active",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    keyring = _FakeKeyringModule(_MacBackend())
    selected = module.select_desktop_credentials(
        profile,
        platform_name="darwin",
        load_keyring=lambda: keyring,
        probe_backend=lambda candidate: candidate,
    )

    assert selected.broker.metadata_status(
        "OPENAI_API_KEY"
    ) == {
        "id": "openai_api_key",
        "name": "OPENAI_API_KEY",
        "secret_ref": "secret://openai_api_key",
        "configured": True,
        "validated": False,
        "source": "keyring",
    }
    assert keyring.get_calls == []


def test_populated_legacy_json_vault_blocks_session_fallback_without_mutation(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    profile = tmp_path / "profile"
    legacy = profile / "secrets" / "local_vault.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                "secrets": {
                    "token": {
                        "id": "token",
                        "name": "TOKEN",
                        "value": "legacy-private-sentinel",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    before = legacy.read_bytes()
    keyring = _FakeKeyringModule(_FailBackend())

    selected = module.select_desktop_credentials(
        profile,
        platform_name="linux",
        load_keyring=lambda: keyring,
    )

    assert selected.broker is None
    assert selected.readiness.to_public_payload() == {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": "locked_vault_required",
        "backend": None,
        "persistence": "none",
        "reason": "legacy_vault_requires_migration",
        "remediation": (
            "Re-enter credentials into the Desktop credential store; "
            "Kestrel will not migrate or delete the legacy vault automatically."
        ),
    }
    assert legacy.read_bytes() == before
    assert not (
        profile / "secrets" / "desktop-keyring-metadata.json"
    ).exists()
    assert keyring.values == {}
    assert keyring.get_calls == []


def test_session_broker_metadata_status_never_reads_a_raw_value(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    broker = module.SessionSecretBroker(
        fingerprint_salt="metadata-only-salt",
        clock=lambda: "2026-07-30T00:00:00+00:00",
    )
    broker.store_secret(
        name="OPENAI_API_KEY",
        purpose="Desktop provider API key for OpenAI.",
        value="metadata-only-private-sentinel",
        secret_id="openai_api_key",
        validate=False,
    )
    broker.resolve = pytest.fail

    assert broker.metadata_status("OPENAI_API_KEY") == {
        "id": "openai_api_key",
        "name": "OPENAI_API_KEY",
        "secret_ref": "secret://openai_api_key",
        "configured": True,
        "validated": False,
        "source": "broker",
    }
    assert not list(tmp_path.rglob("*"))


def test_session_credential_never_enters_support_bundle(
    tmp_path: Path,
) -> None:
    module = _desktop_credentials()
    raw_value = "session-support-private-sentinel"
    broker = module.SessionSecretBroker(
        fingerprint_salt="support-bundle-salt",
        clock=lambda: "2026-07-30T00:00:00+00:00",
    )
    broker.store_secret(
        name="OPENAI_API_KEY",
        purpose="Desktop provider API key for OpenAI.",
        value=raw_value,
        secret_id="openai_api_key",
        validate=False,
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "id": 1,
                "run_id": "desktop-credential",
                "type": "desktop.test",
                "payload": {"error": raw_value},
                "created_at": "2026-07-30T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = AgentConfig(
        provider="mock",
        model="mock",
        workspace=tmp_path,
        memory_dir=tmp_path / "memory",
        log_dir=log_dir,
        state_path=tmp_path / "state" / "agent.db",
        secret_store_path=(
            tmp_path
            / "secrets"
            / "desktop-keyring-metadata.json"
        ),
        secret_backend="keyring",
    )

    result = export_support_bundle(
        config,
        output_path=tmp_path / "support.zip",
    )
    with zipfile.ZipFile(result.bundle_path) as archive:
        combined = b"\n".join(
            archive.read(name)
            for name in archive.namelist()
        )

    assert raw_value.encode() not in combined
    assert b"<redacted>" in combined
