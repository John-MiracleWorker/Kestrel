from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from .secret_broker import KeyringSecretBroker, SecretBroker, is_secret_ref
from .security_boundary import register_secret_env_names, register_secret_value
from .state_store import utc_now

DesktopCredentialState = Literal[
    "available",
    "session_only",
    "locked_vault_required",
    "unavailable",
]
DesktopCredentialPersistence = Literal["persistent", "session", "none"]

_METADATA_RELATIVE_PATH = Path("secrets") / "desktop-keyring-metadata.json"
_LEGACY_RELATIVE_PATH = Path("secrets") / "local_vault.json"
_MAX_METADATA_BYTES = 1024 * 1024
_SECRET_ID_RE = re.compile(r"[^a-z0-9_.-]+")
_SECRET_REF_PREFIX = "secret://"  # nosec B105

_EXPECTED_BACKENDS = {
    "darwin": (
        "keyring.backends.macOS",
        "Keyring",
        "macOS Keychain",
    ),
    "win32": (
        "keyring.backends.Windows",
        "WinVaultKeyring",
        "Windows Credential Manager",
    ),
    "linux": (
        "keyring.backends.SecretService",
        "Keyring",
        "Linux Secret Service",
    ),
}
_CHAINER_IDENTITY = (
    "keyring.backends.chainer",
    "ChainerBackend",
)
_FAIL_IDENTITY = ("keyring.backends.fail", "Keyring")


class _KeyringModule(Protocol):
    def get_keyring(self) -> object: ...


class _KeyringBackend(Protocol):
    def set_password(
        self,
        service_name: str,
        username: str,
        password: str,
    ) -> None: ...

    def get_password(
        self,
        service_name: str,
        username: str,
    ) -> str | None: ...

    def delete_password(
        self,
        service_name: str,
        username: str,
    ) -> None: ...


@dataclass(frozen=True)
class DesktopCredentialReadiness:
    state: DesktopCredentialState
    backend: str | None
    persistence: DesktopCredentialPersistence
    reason: str
    remediation: str

    def to_public_payload(self) -> dict[str, object]:
        return {
            "schema": "kestrel.desktop_credential_readiness.v1",
            "state": self.state,
            "backend": self.backend,
            "persistence": self.persistence,
            "reason": self.reason,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class DesktopCredentialSelection:
    broker: SecretBroker | None
    readiness: DesktopCredentialReadiness


def _readiness(
    state: DesktopCredentialState,
    *,
    backend: str | None,
    persistence: DesktopCredentialPersistence,
    reason: str,
    remediation: str,
) -> DesktopCredentialReadiness:
    return DesktopCredentialReadiness(
        state=state,
        backend=backend,
        persistence=persistence,
        reason=reason,
        remediation=remediation,
    )


def _class_identity(value: object) -> tuple[str, str]:
    candidate = type(value)
    return candidate.__module__, candidate.__name__


def _is_locked_error(error: BaseException) -> bool:
    return _class_identity(error) == ("keyring.errors", "KeyringLocked")


def _expected_backend(
    backend: object,
    *,
    platform_name: str,
) -> object | None:
    expected = _EXPECTED_BACKENDS.get(platform_name)
    if expected is None:
        return None
    expected_identity = expected[:2]
    identity = _class_identity(backend)
    if identity == expected_identity:
        return backend
    if identity != _CHAINER_IDENTITY:
        return None
    raw_backends = getattr(backend, "backends", None)
    if not isinstance(raw_backends, (list, tuple)) or len(raw_backends) > 32:
        return None
    if any(_class_identity(candidate) == _CHAINER_IDENTITY for candidate in raw_backends):
        return None
    matching = [
        candidate
        for candidate in raw_backends
        if _class_identity(candidate) == expected_identity
    ]
    return matching[0] if len(matching) == 1 else None


def _metadata_contains_raw_value(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key == "value" or _metadata_contains_raw_value(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_metadata_contains_raw_value(item) for item in value)
    return False


def _valid_populated_keyring_metadata(data: dict[str, object]) -> bool:
    if data.get("backend") != "keyring":
        return False
    if data.get("keyring_metadata_version") != 2:
        return False
    if not isinstance(data.get("fingerprint_salt"), str):
        return False
    pending = data.get("keyring_pending_cleanup", {})
    if not isinstance(pending, dict):
        return False
    records = data.get("secrets")
    if not isinstance(records, dict):
        return False
    for record_key, raw_record in records.items():
        if not isinstance(record_key, str) or not isinstance(raw_record, dict):
            return False
        if raw_record.get("id", record_key) != record_key:
            return False
        if not isinstance(raw_record.get("name"), str):
            return False
        if not isinstance(raw_record.get("purpose"), str):
            return False
        if not isinstance(raw_record.get("validated"), bool):
            return False
        if not isinstance(raw_record.get("keyring_username"), str):
            return False
        if raw_record.get("keyring_state", "active") not in {
            "active",
            "pending_delete",
        }:
            return False
    return True


def _metadata_state(path: Path) -> Literal["missing", "empty", "keyring", "invalid"]:
    if not path.exists():
        return "missing"
    try:
        raw = path.read_bytes()
    except OSError:
        return "invalid"
    if len(raw) > _MAX_METADATA_BYTES:
        return "invalid"
    if not raw.strip():
        return "empty"
    try:
        decoded = raw.decode("utf-8", errors="strict")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(parsed, dict) or _metadata_contains_raw_value(parsed):
        return "invalid"
    records = parsed.get("secrets", {})
    if not isinstance(records, dict):
        return "invalid"
    if not records:
        return (
            "keyring"
            if _valid_populated_keyring_metadata(parsed)
            else "empty"
        )
    return "keyring" if _valid_populated_keyring_metadata(parsed) else "invalid"


def _legacy_vault_has_material(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        raw = path.read_bytes()
    except OSError:
        return True
    if not raw.strip():
        return False
    if len(raw) > _MAX_METADATA_BYTES:
        return True
    try:
        parsed = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return True
    if not isinstance(parsed, dict):
        return True
    records = parsed.get("secrets", {})
    return not isinstance(records, dict) or bool(records)


def _load_default_keyring() -> object:
    return import_module("keyring")


def select_desktop_credentials(
    profile_root: Path,
    *,
    platform_name: str | None = None,
    load_keyring: Callable[[], object] = _load_default_keyring,
    probe_backend: Callable[[object], object] | None = None,
    fingerprint_salt: Callable[[], str] | None = None,
    clock: Callable[[], str] = utc_now,
) -> DesktopCredentialSelection:
    root = Path(profile_root)
    metadata_path = root / _METADATA_RELATIVE_PATH
    legacy_path = root / _LEGACY_RELATIVE_PATH
    platform = platform_name or sys.platform
    expected = _EXPECTED_BACKENDS.get(platform)
    expected_label = expected[2] if expected is not None else None

    if _legacy_vault_has_material(legacy_path):
        return DesktopCredentialSelection(
            broker=None,
            readiness=_readiness(
                "locked_vault_required",
                backend=None,
                persistence="none",
                reason="legacy_vault_requires_migration",
                remediation=(
                    "Re-enter credentials into the Desktop credential store; "
                    "Kestrel will not migrate or delete the legacy vault automatically."
                ),
            ),
        )

    metadata_state = _metadata_state(metadata_path)
    if metadata_state == "invalid":
        return DesktopCredentialSelection(
            broker=None,
            readiness=_readiness(
                "locked_vault_required",
                backend=None,
                persistence="none",
                reason="metadata_invalid",
                remediation=(
                    "Repair or retire the invalid Desktop keyring "
                    "metadata before storing credentials."
                ),
            ),
        )

    try:
        loaded = load_keyring()
        keyring_module = cast(_KeyringModule, loaded)
        backend = keyring_module.get_keyring()
    except ModuleNotFoundError:
        return DesktopCredentialSelection(
            broker=None,
            readiness=_readiness(
                "unavailable",
                backend=None,
                persistence="none",
                reason="keyring_package_missing",
                remediation=(
                    "Reinstall Kestrel so the bundled keyring package is available."
                ),
            ),
        )
    except Exception as exc:
        if _is_locked_error(exc):
            return DesktopCredentialSelection(
                broker=None,
                readiness=_readiness(
                    "locked_vault_required",
                    backend=expected_label,
                    persistence="none",
                    reason="vault_locked",
                    remediation=(
                        "Unlock the operating system credential store and retry."
                    ),
                ),
            )
        return DesktopCredentialSelection(
            broker=None,
            readiness=_readiness(
                "unavailable",
                backend=expected_label,
                persistence="none",
                reason="backend_probe_failed",
                remediation=(
                    "Repair or unlock the operating system credential backend."
                ),
            ),
        )

    if (
        platform == "linux"
        and _class_identity(backend) == _FAIL_IDENTITY
    ):
        if metadata_state == "keyring":
            return DesktopCredentialSelection(
                broker=None,
                readiness=_readiness(
                    "unavailable",
                    backend="Linux Secret Service",
                    persistence="none",
                    reason="secret_service_missing",
                    remediation=(
                        "Start and unlock Linux Secret Service; existing "
                        "credential metadata cannot use session storage."
                    ),
                ),
            )
        readiness = _readiness(
            "session_only",
            backend="Session memory",
            persistence="session",
            reason="secret_service_missing",
            remediation=(
                "Start an unlocked Linux Secret Service to keep "
                "credentials across restarts."
            ),
        )
        session_broker = SessionSecretBroker(
            fingerprint_salt=(
                fingerprint_salt()
                if fingerprint_salt is not None
                else os.urandom(32).hex()
            ),
            clock=clock,
        )
        session_broker.desktop_readiness = readiness
        return DesktopCredentialSelection(
            broker=session_broker,
            readiness=readiness,
        )

    selected_backend = _expected_backend(
        backend,
        platform_name=platform,
    )
    if selected_backend is None or expected_label is None:
        return DesktopCredentialSelection(
            broker=None,
            readiness=_readiness(
                "unavailable",
                backend=None,
                persistence="none",
                reason="unsupported_backend",
                remediation=(
                    "Install and unlock the operating system credential backend."
                ),
            ),
        )

    try:
        if probe_backend is not None:
            probe_backend(selected_backend)
        if metadata_state == "empty" and metadata_path.exists():
            metadata_path.write_text("{}\n", encoding="utf-8")
        keyring_broker = KeyringSecretBroker(
            metadata_path,
            keyring=cast(_KeyringBackend, selected_backend),
        )
    except Exception as exc:
        if _is_locked_error(exc):
            return DesktopCredentialSelection(
                broker=None,
                readiness=_readiness(
                    "locked_vault_required",
                    backend=expected_label,
                    persistence="none",
                    reason="vault_locked",
                    remediation=(
                        "Unlock the operating system credential store and retry."
                    ),
                ),
            )
        return DesktopCredentialSelection(
            broker=None,
            readiness=_readiness(
                "unavailable",
                backend=expected_label,
                persistence="none",
                reason="backend_probe_failed",
                remediation=(
                    "Repair or unlock the operating system credential backend."
                ),
            ),
        )

    readiness = _readiness(
        "available",
        backend=expected_label,
        persistence="persistent",
        reason="ready",
        remediation="No recovery needed.",
    )
    keyring_broker.desktop_readiness = readiness
    return DesktopCredentialSelection(
        broker=keyring_broker,
        readiness=readiness,
    )


class SessionSecretBroker(SecretBroker):
    """A process-local broker used only when Linux Secret Service is absent."""

    def __init__(
        self,
        *,
        fingerprint_salt: str,
        clock: Callable[[], str] = utc_now,
        allowed_env_names: set[str] | None = None,
    ) -> None:
        self.fingerprint_salt = fingerprint_salt
        self.clock = clock
        self.allowed_env_names = {
            name.strip()
            for name in (allowed_env_names or set())
            if name.strip()
        }
        self._records_by_id: dict[str, dict[str, Any]] = {}
        self.desktop_readiness: DesktopCredentialReadiness | None = None
        register_secret_env_names(self.allowed_env_names)

    def register_allowed_env_names(self, names: set[str]) -> None:
        registered = {name.strip() for name in names if name.strip()}
        self.allowed_env_names.update(registered)
        register_secret_env_names(registered)

    def store_secret(
        self,
        *,
        name: str,
        purpose: str,
        value: str,
        secret_id: str | None = None,
        validate: bool = False,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Secret name is required.")
        if not value or value != value.strip():
            raise ValueError("Secret value is required.")
        sid = _normalize_secret_id(secret_id or clean_name)
        now = self.clock()
        previous = self._records_by_id.get(sid, {})
        record = {
            "id": sid,
            "name": clean_name,
            "purpose": purpose.strip(),
            "value": value,
            "validated": bool(validate),
            "last_validated_at": (
                now if validate else previous.get("last_validated_at")
            ),
            "created_at": str(previous.get("created_at") or now),
            "updated_at": now,
        }
        self._records_by_id[sid] = record
        register_secret_value(value)
        return self._public_session(record)

    def list_secrets(self) -> list[dict[str, Any]]:
        return [
            self._public_session(record)
            for record in sorted(
                self._records_by_id.values(),
                key=lambda item: str(item.get("name", "")),
            )
        ]

    def get_secret(self, secret_id: str) -> dict[str, Any]:
        record = self._records_by_id.get(secret_id)
        if record is None:
            raise KeyError(secret_id)
        return self._public_session(record)

    def delete_secret(self, secret_id: str) -> None:
        if self._records_by_id.pop(secret_id, None) is None:
            raise KeyError(secret_id)

    def validate_secret(self, secret_id: str) -> dict[str, Any]:
        record = self._records_by_id.get(secret_id)
        if record is None:
            raise KeyError(secret_id)
        now = self.clock()
        record["validated"] = True
        record["last_validated_at"] = now
        record["updated_at"] = now
        return self._public_session(record)

    def resolve(self, name_or_ref: str | None) -> str | None:
        ref = (name_or_ref or "").strip()
        if not ref:
            return None
        record: dict[str, Any] | None = None
        if is_secret_ref(ref):
            record = self._records_by_id.get(
                ref.removeprefix(_SECRET_REF_PREFIX)
            )
        else:
            record = next(
                (
                    candidate
                    for candidate in self._records_by_id.values()
                    if candidate.get("name") == ref
                ),
                None,
            )
        if record is not None:
            value = str(record.get("value") or "")
            register_secret_value(value)
            return value or None
        if ref in self.allowed_env_names:
            value = os.getenv(ref, "").strip()
            register_secret_value(value)
            return value or None
        return None

    def status(self, name_or_ref: str | None) -> dict[str, Any]:
        return self.metadata_status(name_or_ref)

    def metadata_status(
        self,
        name_or_ref: str | None,
    ) -> dict[str, Any]:
        ref = (name_or_ref or "").strip()
        record: dict[str, Any] | None = None
        if is_secret_ref(ref):
            record = self._records_by_id.get(
                ref.removeprefix(_SECRET_REF_PREFIX)
            )
        else:
            record = next(
                (
                    candidate
                    for candidate in self._records_by_id.values()
                    if candidate.get("name") == ref
                ),
                None,
            )
        if record is not None:
            sid = str(record["id"])
            return {
                "id": sid,
                "name": str(record["name"]),
                "secret_ref": f"{_SECRET_REF_PREFIX}{sid}",
                "configured": True,
                "validated": bool(record.get("validated", False)),
                "source": "broker",
            }
        return {
            "source_env": ref,
            "configured": (
                ref in self.allowed_env_names
                and bool(os.getenv(ref, "").strip())
            ),
            "validated": False,
            "source": (
                "env"
                if ref in self.allowed_env_names
                and bool(os.getenv(ref, "").strip())
                else "missing"
            ),
        }

    def _public_session(
        self,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        value = str(record.get("value") or "")
        sid = str(record.get("id") or "")
        fingerprint = "sha256:" + hashlib.sha256(
            (self.fingerprint_salt + value).encode("utf-8")
        ).hexdigest()[:12]
        return {
            "id": sid,
            "name": str(record.get("name") or ""),
            "purpose": str(record.get("purpose") or ""),
            "secret_ref": f"{_SECRET_REF_PREFIX}{sid}",
            "configured": bool(value),
            "validated": bool(record.get("validated", False)),
            "last_validated_at": record.get("last_validated_at"),
            "fingerprint": fingerprint if value else None,
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
            "source": "broker",
        }


class UnavailableDesktopSecretBroker(SessionSecretBroker):
    """Metadata-only broker that keeps Desktop usable without unsafe fallback."""

    def __init__(self, readiness: DesktopCredentialReadiness) -> None:
        super().__init__(fingerprint_salt="")
        self.desktop_readiness = readiness

    def store_secret(self, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("desktop_credential_storage_unavailable")

    def delete_secret(self, secret_id: str) -> None:
        del secret_id
        raise RuntimeError("desktop_credential_storage_unavailable")

    def validate_secret(self, secret_id: str) -> dict[str, Any]:
        del secret_id
        raise RuntimeError("desktop_credential_storage_unavailable")


def build_desktop_secret_broker(profile_root: Path) -> SecretBroker:
    selection = select_desktop_credentials(profile_root)
    if selection.broker is not None:
        return selection.broker
    return UnavailableDesktopSecretBroker(selection.readiness)


def _normalize_secret_id(value: str) -> str:
    normalized = _SECRET_ID_RE.sub("_", value.strip().lower()).strip(
        "_.-"
    )
    return normalized or hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:16]
