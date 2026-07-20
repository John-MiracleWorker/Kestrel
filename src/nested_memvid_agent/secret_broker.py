from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from threading import Lock, RLock
from typing import IO, Any, Protocol, cast

from .file_lock import lock_exclusive, lock_shared, unlock
from .private_artifacts import (
    ensure_private_directory,
    harden_private_file,
    open_private_file_descriptor,
    read_private_text,
)
from .security_boundary import register_secret_env_names, register_secret_value
from .state_store import utc_now

_SECRET_ID_RE = re.compile(r"[^a-z0-9_.-]+")
_SECRET_REF_PREFIX = "secret://"  # nosec B105
_VAULT_THREAD_LOCKS: dict[Path, RLock] = {}
_VAULT_THREAD_LOCKS_GUARD = Lock()


@dataclass(frozen=True)
class SecretRecord:
    id: str
    name: str
    purpose: str
    value: str
    validated: bool = False
    last_validated_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


class SecretBroker:
    """Trusted backend broker for local secrets.

    Public methods return metadata only. The raw value is available only through
    `resolve()` for runtime injection into channels, MCP processes, or provider
    adapters.
    """

    def __init__(self, vault_path: Path, *, allowed_env_names: set[str] | None = None) -> None:
        self.vault_path = Path(vault_path)
        self.allowed_env_names = {name.strip() for name in (allowed_env_names or set()) if name.strip()}
        ensure_private_directory(self.vault_path.parent)
        with self._vault_lock(exclusive=False):
            harden_private_file(self.vault_path, missing_ok=True)
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
        clean_value = value.strip()
        if not clean_name:
            raise ValueError("Secret name is required.")
        if not clean_value:
            raise ValueError("Secret value is required.")
        with self._vault_lock(exclusive=True):
            data = self._read_unlocked()
            salt = _ensure_fingerprint_salt(data)
            records = data.setdefault("secrets", {})
            if not isinstance(records, dict):
                raise ValueError("Secret vault records must be a JSON object.")
            now = utc_now()
            sid = _normalize_secret_id(secret_id or clean_name)
            existing_raw = records.get(sid)
            existing: dict[str, Any] = existing_raw if isinstance(existing_raw, dict) else {}
            record = {
                "id": sid,
                "name": clean_name,
                "purpose": purpose.strip(),
                "value": clean_value,
                "validated": bool(validate),
                "last_validated_at": now if validate else existing.get("last_validated_at"),
                "created_at": str(existing.get("created_at") or now),
                "updated_at": now,
            }
            records[sid] = record
            self._write_unlocked(data)
        return self._public(record, salt=salt)

    def list_secrets(self) -> list[dict[str, Any]]:
        data = self._read()
        records_raw = data.get("secrets", {})
        if not isinstance(records_raw, dict):
            return []
        salt = _salt_for_public_payload(data)
        records = [record for record in records_raw.values() if isinstance(record, dict)]
        return [self._public(record, salt=salt) for record in sorted(records, key=lambda item: str(item.get("name", "")))]

    def get_secret(self, secret_id: str) -> dict[str, Any]:
        data = self._read()
        record = self._record_from_data(data, secret_id)
        if record is None:
            raise KeyError(secret_id)
        return self._public(record, salt=_salt_for_public_payload(data))

    def delete_secret(self, secret_id: str) -> None:
        with self._vault_lock(exclusive=True):
            data = self._read_unlocked()
            records = data.setdefault("secrets", {})
            if not isinstance(records, dict) or secret_id not in records:
                raise KeyError(secret_id)
            del records[secret_id]
            self._write_unlocked(data)

    def validate_secret(self, secret_id: str) -> dict[str, Any]:
        with self._vault_lock(exclusive=True):
            data = self._read_unlocked()
            records = data.setdefault("secrets", {})
            if (
                not isinstance(records, dict)
                or secret_id not in records
                or not isinstance(records[secret_id], dict)
            ):
                raise KeyError(secret_id)
            record = records[secret_id]
            salt = _ensure_fingerprint_salt(data)
            if not str(record.get("value", "")).strip():
                raise ValueError("Secret value is missing.")
            now = utc_now()
            record["validated"] = True
            record["last_validated_at"] = now
            record["updated_at"] = now
            self._write_unlocked(data)
        return self._public(record, salt=salt)

    def resolve(self, name_or_ref: str | None) -> str | None:
        ref = (name_or_ref or "").strip()
        if not ref:
            return None
        if is_secret_ref(ref):
            record = self._record(ref.removeprefix(_SECRET_REF_PREFIX))
            return None if record is None else _tracked_secret_value(record.get("value"))
        env_value = os.getenv(ref, "").strip()
        if env_value:
            return _tracked_secret_value(env_value)
        for record in self._records():
            if str(record.get("name", "")).strip() == ref:
                return _tracked_secret_value(record.get("value"))
        return None

    def status(self, name_or_ref: str | None) -> dict[str, Any]:
        ref = (name_or_ref or "").strip()
        if not ref:
            return {"configured": False}
        data = self._read()
        if is_secret_ref(ref):
            record = self._record_from_data(data, ref.removeprefix(_SECRET_REF_PREFIX))
            if record is None:
                return {"secret_ref": ref, "configured": False, "validated": False}
            return self._public(record, salt=_salt_for_public_payload(data))
        for record in self._records_from_data(data):
            if str(record.get("name", "")).strip() == ref:
                public = self._public(record, salt=_salt_for_public_payload(data))
                public["configured"] = True
                public["source_env"] = ref
                public["source"] = "broker"
                return public
        if ref not in self.allowed_env_names:
            return {"source_env": ref, "configured": False, "validated": False, "source": "unregistered"}
        env_configured = bool(os.getenv(ref, "").strip())
        return {"source_env": ref, "configured": env_configured, "validated": False, "source": "env" if env_configured else "missing"}

    def _record(self, secret_id: str) -> dict[str, Any] | None:
        return self._record_from_data(self._read(), secret_id)

    def _record_from_data(self, data: dict[str, Any], secret_id: str) -> dict[str, Any] | None:
        secrets = data.get("secrets", {})
        if not isinstance(secrets, dict):
            return None
        record = secrets.get(secret_id)
        return record if isinstance(record, dict) else None

    def _records(self) -> list[dict[str, Any]]:
        return self._records_from_data(self._read())

    def _records_from_data(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        secrets = data.get("secrets", {})
        if not isinstance(secrets, dict):
            return []
        return [record for record in secrets.values() if isinstance(record, dict)]

    def _read(self) -> dict[str, Any]:
        with self._vault_lock(exclusive=False):
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, Any]:
        raw_text = read_private_text(self.vault_path, missing_ok=True)
        if raw_text is None:
            return {"secrets": {}}
        raw = json.loads(raw_text)
        return raw if isinstance(raw, dict) else {"secrets": {}}

    def _write(self, data: dict[str, Any]) -> None:
        with self._vault_lock(exclusive=True):
            self._write_unlocked(data)

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        ensure_private_directory(self.vault_path.parent)
        harden_private_file(self.vault_path, missing_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.vault_path.name}.",
            suffix=".tmp",
            dir=str(self.vault_path.parent),
        )
        temp_path = Path(temp_name)
        try:
            _chmod_owner_only(fd, temp_path)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.vault_path)
            harden_private_file(self.vault_path)
            _fsync_directory(self.vault_path.parent)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise

    @contextmanager
    def _vault_lock(self, *, exclusive: bool) -> Iterator[None]:
        ensure_private_directory(self.vault_path.parent)
        lock_path = self.vault_path.with_name(f".{self.vault_path.name}.lock")
        fd = open_private_file_descriptor(lock_path)
        thread_lock = _thread_lock_for(self.vault_path)
        with thread_lock, os.fdopen(fd, "r+", encoding="utf-8") as lock_handle:
            _lock_handle(lock_handle, exclusive=exclusive)
            try:
                yield
            finally:
                unlock(lock_handle)

    def _public(self, record: dict[str, Any], *, salt: str | None = None) -> dict[str, Any]:
        value = _tracked_secret_value(record.get("value")) or ""
        secret_id = str(record.get("id", ""))
        return {
            "id": secret_id,
            "name": str(record.get("name", "")),
            "purpose": str(record.get("purpose", "")),
            "secret_ref": f"{_SECRET_REF_PREFIX}{secret_id}",
            "configured": bool(value),
            "validated": bool(record.get("validated", False)),
            "last_validated_at": record.get("last_validated_at"),
            "fingerprint": _fingerprint(value, salt=salt or "") if value else None,
            "created_at": str(record.get("created_at", "")),
            "updated_at": str(record.get("updated_at", "")),
            "source": "broker",
        }


class _KeyringModule(Protocol):
    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class KeyringSecretBroker(SecretBroker):
    """Secret broker that keeps raw values in an OS keyring-compatible backend."""

    def __init__(
        self,
        vault_path: Path,
        *,
        allowed_env_names: set[str] | None = None,
        keyring: _KeyringModule | None = None,
        service_name: str = "kestrel.secret_broker",
    ) -> None:
        super().__init__(vault_path, allowed_env_names=allowed_env_names)
        self.keyring: _KeyringModule
        if keyring is None:
            loaded_keyring = import_module("keyring")
            _assert_keyring_available(loaded_keyring)
            self.keyring = cast(_KeyringModule, loaded_keyring)
        else:
            self.keyring = keyring
        self.service_name = service_name

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
        clean_value = value.strip()
        if not clean_name:
            raise ValueError("Secret name is required.")
        if not clean_value:
            raise ValueError("Secret value is required.")
        with self._vault_lock(exclusive=True):
            data = self._read_unlocked()
            data["backend"] = "keyring"
            salt = _ensure_fingerprint_salt(data)
            records = data.setdefault("secrets", {})
            if not isinstance(records, dict):
                raise ValueError("Secret vault records must be a JSON object.")
            now = utc_now()
            sid = _normalize_secret_id(secret_id or clean_name)
            existing_raw = records.get(sid)
            existing: dict[str, Any] = existing_raw if isinstance(existing_raw, dict) else {}
            previous_value = self._keyring_value(sid)
            self.keyring.set_password(self.service_name, sid, clean_value)
            record = {
                "id": sid,
                "name": clean_name,
                "purpose": purpose.strip(),
                "validated": bool(validate),
                "last_validated_at": now if validate else existing.get("last_validated_at"),
                "created_at": str(existing.get("created_at") or now),
                "updated_at": now,
            }
            records[sid] = record
            try:
                self._write_unlocked(data)
            except BaseException:
                _restore_keyring_value(
                    self.keyring,
                    service_name=self.service_name,
                    secret_id=sid,
                    previous_value=previous_value,
                )
                raise
        return self._public(record, salt=salt)

    def delete_secret(self, secret_id: str) -> None:
        with self._vault_lock(exclusive=True):
            data = self._read_unlocked()
            records = data.setdefault("secrets", {})
            if not isinstance(records, dict) or secret_id not in records:
                raise KeyError(secret_id)
            previous_value = self._keyring_value(secret_id)
            if previous_value is not None:
                self.keyring.delete_password(self.service_name, secret_id)
            del records[secret_id]
            try:
                self._write_unlocked(data)
            except BaseException:
                if previous_value is not None:
                    self.keyring.set_password(self.service_name, secret_id, previous_value)
                raise

    def validate_secret(self, secret_id: str) -> dict[str, Any]:
        with self._vault_lock(exclusive=True):
            data = self._read_unlocked()
            records = data.setdefault("secrets", {})
            if (
                not isinstance(records, dict)
                or secret_id not in records
                or not isinstance(records[secret_id], dict)
            ):
                raise KeyError(secret_id)
            record = records[secret_id]
            salt = _ensure_fingerprint_salt(data)
            if not self._keyring_value(str(record.get("id", ""))):
                raise ValueError("Secret value is missing.")
            now = utc_now()
            record["validated"] = True
            record["last_validated_at"] = now
            record["updated_at"] = now
            self._write_unlocked(data)
        return self._public(record, salt=salt)

    def resolve(self, name_or_ref: str | None) -> str | None:
        ref = (name_or_ref or "").strip()
        if not ref:
            return None
        if is_secret_ref(ref):
            record = self._record(ref.removeprefix(_SECRET_REF_PREFIX))
            if record is None:
                return None
            return self._keyring_value(str(record.get("id", "")))
        env_value = os.getenv(ref, "").strip()
        if env_value:
            return _tracked_secret_value(env_value)
        for record in self._records():
            if str(record.get("name", "")).strip() == ref:
                return self._keyring_value(str(record.get("id", "")))
        return None

    def _public(self, record: dict[str, Any], *, salt: str | None = None) -> dict[str, Any]:
        secret_id = str(record.get("id", ""))
        value = self._keyring_value(secret_id) or ""
        return {
            "id": secret_id,
            "name": str(record.get("name", "")),
            "purpose": str(record.get("purpose", "")),
            "secret_ref": f"{_SECRET_REF_PREFIX}{secret_id}",
            "configured": bool(value),
            "validated": bool(record.get("validated", False)),
            "last_validated_at": record.get("last_validated_at"),
            "fingerprint": _fingerprint(value, salt=salt or "") if value else None,
            "created_at": str(record.get("created_at", "")),
            "updated_at": str(record.get("updated_at", "")),
            "source": "keyring",
        }

    def _keyring_value(self, secret_id: str) -> str | None:
        if not secret_id:
            return None
        value = self.keyring.get_password(self.service_name, secret_id)
        return _tracked_secret_value(value)


def build_secret_broker(
    vault_path: Path,
    *,
    backend: str | None = None,
    allowed_env_names: set[str] | None = None,
) -> SecretBroker:
    env_backend = os.getenv("NEST_AGENT_SECRET_BACKEND") or "json"
    selected = (backend or env_backend).strip().lower()
    if selected in {"", "json", "file", "local"}:
        return SecretBroker(vault_path, allowed_env_names=allowed_env_names)
    if selected == "keyring":
        try:
            return KeyringSecretBroker(vault_path, allowed_env_names=allowed_env_names)
        except ImportError as exc:
            raise RuntimeError(
                "Keyring secret backend was requested, but the keyring package is unavailable."
            ) from exc
    raise ValueError(f"Unsupported secret backend: {selected}")


def is_secret_ref(value: str) -> bool:
    return value.startswith(_SECRET_REF_PREFIX) and bool(value.removeprefix(_SECRET_REF_PREFIX).strip())


def _tracked_secret_value(value: object) -> str | None:
    clean_value = value.strip() if isinstance(value, str) else ""
    if not clean_value:
        return None
    register_secret_value(clean_value)
    return clean_value


def _normalize_secret_id(value: str) -> str:
    normalized = _SECRET_ID_RE.sub("_", value.strip().lower()).strip("_.-")
    return normalized or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _fingerprint(value: str, *, salt: str) -> str:
    return "sha256:" + hashlib.sha256((salt + value).encode("utf-8")).hexdigest()[:12]


def _ensure_fingerprint_salt(data: dict[str, Any]) -> str:
    salt = data.get("fingerprint_salt")
    if isinstance(salt, str) and salt.strip():
        return salt
    salt = secrets.token_hex(16)
    data["fingerprint_salt"] = salt
    return salt


def _salt_for_public_payload(data: dict[str, Any]) -> str:
    salt = data.get("fingerprint_salt")
    return salt if isinstance(salt, str) else ""


def _thread_lock_for(vault_path: Path) -> RLock:
    key = vault_path.resolve()
    with _VAULT_THREAD_LOCKS_GUARD:
        return _VAULT_THREAD_LOCKS.setdefault(key, RLock())


def _lock_handle(handle: IO[str], *, exclusive: bool) -> None:
    if exclusive:
        lock_exclusive(handle)
    else:
        lock_shared(handle)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _chmod_owner_only(fd: int, path: Path) -> None:
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        fchmod(fd, 0o600)
    else:
        os.chmod(path, 0o600)


def _assert_keyring_available(keyring: Any) -> None:
    get_keyring = getattr(keyring, "get_keyring", None)
    if not callable(get_keyring):
        raise RuntimeError("Keyring secret backend is unavailable: invalid keyring module.")
    try:
        backend = get_keyring()
        priority = float(getattr(backend, "priority", 0.0))
    except Exception as exc:  # noqa: BLE001 - optional backend discovery varies by platform
        raise RuntimeError(f"Keyring secret backend is unavailable: {type(exc).__name__}.") from exc
    if priority <= 0:
        raise RuntimeError("Keyring secret backend is unavailable: no usable OS keyring backend.")


def _restore_keyring_value(
    keyring: _KeyringModule,
    *,
    service_name: str,
    secret_id: str,
    previous_value: str | None,
) -> None:
    try:
        if previous_value is None:
            keyring.delete_password(service_name, secret_id)
        else:
            keyring.set_password(service_name, secret_id, previous_value)
    except Exception:
        return
