from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterable, Iterator
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
_MAX_SECRET_ID_LENGTH = 240
_KEYRING_METADATA_VERSION = 2
_KEYRING_USERNAME_RE = re.compile(r"^[a-z0-9_.:-]{1,1024}$")
_LEGACY_KEYRING_IDENTIFIER_RE = re.compile(r"^[a-z0-9_.:-]+$")
_KEYRING_USERNAME_FIELD = "keyring_username"
_KEYRING_STATE_FIELD = "keyring_state"
_KEYRING_STATE_ACTIVE = "active"
_KEYRING_STATE_PENDING_DELETE = "pending_delete"
_KEYRING_DELETE_USERNAMES_FIELD = "keyring_delete_usernames"
_KEYRING_PENDING_CLEANUP_FIELD = "keyring_pending_cleanup"
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


class SecretBrokerPartialCommitError(RuntimeError):
    """A keyring mutation committed only far enough to require reconciliation."""

    def __init__(
        self,
        *,
        operation: str,
        stage: str,
        secret_ids: tuple[str, ...],
        recovery_usernames: tuple[str, ...],
    ) -> None:
        safe_operation = _safe_keyring_identifier(operation)
        safe_stage = _safe_keyring_identifier(stage)
        safe_ids = tuple(_safe_keyring_identifier(item) for item in secret_ids)
        safe_usernames = tuple(
            _safe_keyring_identifier(item) for item in recovery_usernames
        )
        self.operation = safe_operation
        self.stage = safe_stage
        self.secret_ids = safe_ids
        self.recovery_usernames = safe_usernames
        super().__init__(
            "Keyring secret operation partially committed; "
            f"operation={safe_operation}; stage={safe_stage}; "
            f"secret_ids={','.join(safe_ids) or '-'}; "
            f"recovery_usernames={','.join(safe_usernames) or '-'}"
        )


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
            # Prime the in-process redaction registry from an existing local
            # vault before any tool or channel output can echo an opaque value.
            self._read_unlocked()
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
        data = raw if isinstance(raw, dict) else {"secrets": {}}
        if type(self) is SecretBroker and data.get("backend") == "keyring":
            raise ValueError(
                "Refusing to open keyring metadata with the JSON secret backend; "
                "select the keyring backend to preserve the active secret mapping."
            )
        self._register_loaded_secret_values(data)
        return data

    def _register_loaded_secret_values(self, data: dict[str, Any]) -> None:
        records = data.get("secrets", {})
        if not isinstance(records, dict):
            return
        for record in records.values():
            if isinstance(record, dict):
                _tracked_secret_value(record.get("value"))

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
    """Secret broker backed by immutable, metadata-selected keyring versions."""

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
        self._reconcile_keyring_state()

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
            self._prepare_keyring_metadata_unlocked(data)
            salt = _ensure_fingerprint_salt(data)
            records = data.setdefault("secrets", {})
            if not isinstance(records, dict):
                raise ValueError("Secret vault records must be a JSON object.")
            now = utc_now()
            sid = _normalize_secret_id(secret_id or clean_name)
            existing_raw = records.get(sid)
            existing: dict[str, Any] = existing_raw if isinstance(existing_raw, dict) else {}
            if existing and self._record_state(existing) == _KEYRING_STATE_PENDING_DELETE:
                raise SecretBrokerPartialCommitError(
                    operation="store",
                    stage="delete_pending",
                    secret_ids=(sid,),
                    recovery_usernames=self._delete_usernames(existing, sid),
                )
            previous_username = (
                self._record_keyring_username(existing, sid) if existing else None
            )
            new_username = self._allocate_versioned_username(data, sid)
            pending_cleanup = self._pending_cleanup_unlocked(data)
            pending_cleanup[new_username] = {
                "secret_id": sid,
                "reason": "uncommitted_version",
            }
            # Persist cleanup intent before the new raw value can exist.
            self._write_unlocked(data)
            try:
                self.keyring.set_password(
                    self.service_name,
                    new_username,
                    clean_value,
                )
            except Exception:
                self._abort_uncommitted_version(sid, new_username)
            record = {
                "id": sid,
                "name": clean_name,
                "purpose": purpose.strip(),
                "validated": bool(validate),
                "last_validated_at": now if validate else existing.get("last_validated_at"),
                "created_at": str(existing.get("created_at") or now),
                "updated_at": now,
                _KEYRING_USERNAME_FIELD: new_username,
                _KEYRING_STATE_FIELD: _KEYRING_STATE_ACTIVE,
            }
            records[sid] = record
            pending_cleanup.pop(new_username, None)
            if previous_username and previous_username != new_username:
                pending_cleanup[previous_username] = {
                    "secret_id": sid,
                    "reason": "superseded_version",
                }
            try:
                self._write_unlocked(data)
            except BaseException as exc:
                self._recover_failed_version_commit(sid, new_username, exc)
            self._finish_pending_cleanup(data, secret_id=sid, operation="store")
        return self._public(record, salt=salt)

    def delete_secret(self, secret_id: str) -> None:
        with self._vault_lock(exclusive=True):
            data = self._read_unlocked()
            self._prepare_keyring_metadata_unlocked(data)
            records = data.setdefault("secrets", {})
            if not isinstance(records, dict) or secret_id not in records:
                raise KeyError(secret_id)
            record = records[secret_id]
            if not isinstance(record, dict):
                raise KeyError(secret_id)
            sid = str(record.get("id") or secret_id)
            if self._record_state(record) != _KEYRING_STATE_PENDING_DELETE:
                usernames = [self._record_keyring_username(record, sid)]
                pending_cleanup = self._pending_cleanup_unlocked(data)
                for username, entry in tuple(pending_cleanup.items()):
                    if str(entry.get("secret_id") or "") == sid:
                        usernames.append(username)
                        pending_cleanup.pop(username, None)
                record[_KEYRING_STATE_FIELD] = _KEYRING_STATE_PENDING_DELETE
                record[_KEYRING_DELETE_USERNAMES_FIELD] = _unique_keyring_usernames(
                    usernames
                )
                record["updated_at"] = utc_now()
                # The durable tombstone makes every subsequent resolve fail closed.
                try:
                    self._write_unlocked(data)
                except BaseException as exc:
                    self._recover_failed_delete_tombstone(
                        record_key=secret_id,
                        sid=sid,
                        usernames=self._delete_usernames(record, sid),
                        original_error=exc,
                    )
            self._finish_pending_delete(
                data,
                record_key=secret_id,
                sid=sid,
                record=record,
            )

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
            if self._record_state(record) != _KEYRING_STATE_ACTIVE:
                raise ValueError("Secret deletion is pending.")
            if not self._record_keyring_value(record):
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
            return self._record_keyring_value(record)
        records = self._records()
        # Once a broker-owned name has a durable deletion tombstone, do not
        # silently substitute an environment value while keyring cleanup is
        # incomplete. The public name and secret:// reference must both fail
        # closed until reconciliation removes the record.
        if any(
            str(record.get("name", "")).strip() == ref
            and self._record_state(record) == _KEYRING_STATE_PENDING_DELETE
            for record in records
        ):
            return None
        env_value = os.getenv(ref, "").strip()
        if env_value:
            return _tracked_secret_value(env_value)
        for record in records:
            if str(record.get("name", "")).strip() == ref:
                return self._record_keyring_value(record)
        return None

    def _public(self, record: dict[str, Any], *, salt: str | None = None) -> dict[str, Any]:
        secret_id = str(record.get("id", ""))
        pending_delete = self._record_state(record) == _KEYRING_STATE_PENDING_DELETE
        value = "" if pending_delete else (self._record_keyring_value(record) or "")
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

    def _record_keyring_value(self, record: dict[str, Any]) -> str | None:
        if self._record_state(record) != _KEYRING_STATE_ACTIVE:
            return None
        sid = str(record.get("id") or "")
        username = self._record_keyring_username(record, sid)
        return self._keyring_value(username)

    def _register_loaded_secret_values(self, data: dict[str, Any]) -> None:
        # Keyring metadata intentionally contains no raw values. Do not turn a
        # metadata read into an eager OS-keyring enumeration or access prompt.
        return

    def _keyring_value(self, username: str) -> str | None:
        if not _valid_legacy_keyring_identifier(username):
            return None
        value = self.keyring.get_password(self.service_name, username)
        return _tracked_secret_value(value)

    def _allocate_versioned_username(self, data: dict[str, Any], sid: str) -> str:
        records = data.get("secrets", {})
        if not isinstance(records, dict):
            raise ValueError("Secret vault records must be a JSON object.")
        reserved = set(self._pending_cleanup_unlocked(data))
        for raw_sid, raw_record in records.items():
            if not isinstance(raw_record, dict):
                continue
            record_sid = str(raw_record.get("id") or raw_sid)
            if self._record_state(raw_record) == _KEYRING_STATE_PENDING_DELETE:
                reserved.update(self._delete_usernames(raw_record, record_sid))
            else:
                reserved.add(self._record_keyring_username(raw_record, record_sid))
        for _ in range(16):
            candidate = _versioned_keyring_username(sid)
            if candidate in reserved:
                continue
            try:
                existing = self.keyring.get_password(self.service_name, candidate)
            except Exception:
                raise RuntimeError(
                    f"Unable to verify a new keyring username for secret id {sid}."
                ) from None
            if existing is None:
                return candidate
            _tracked_secret_value(existing)
        raise RuntimeError(f"Unable to allocate a new keyring username for secret id {sid}.")

    def _record_state(self, record: dict[str, Any]) -> str:
        state = str(record.get(_KEYRING_STATE_FIELD) or _KEYRING_STATE_ACTIVE)
        if state not in {_KEYRING_STATE_ACTIVE, _KEYRING_STATE_PENDING_DELETE}:
            raise ValueError("Secret keyring metadata has an invalid state.")
        return state

    def _record_keyring_username(self, record: dict[str, Any], sid: str) -> str:
        raw_username = record.get(_KEYRING_USERNAME_FIELD)
        username = str(raw_username) if raw_username is not None else sid
        if not _valid_legacy_keyring_identifier(username):
            raise ValueError("Secret keyring metadata has an invalid username.")
        return username

    def _delete_usernames(self, record: dict[str, Any], sid: str) -> tuple[str, ...]:
        raw_usernames = record.get(_KEYRING_DELETE_USERNAMES_FIELD)
        if isinstance(raw_usernames, list):
            usernames = tuple(str(item) for item in raw_usernames)
        else:
            usernames = (self._record_keyring_username(record, sid),)
        if not usernames or not all(
            _valid_legacy_keyring_identifier(item) for item in usernames
        ):
            raise ValueError("Secret keyring deletion metadata is invalid.")
        return _unique_keyring_usernames(usernames)

    def _pending_cleanup_unlocked(self, data: dict[str, Any]) -> dict[str, dict[str, str]]:
        raw_pending = data.setdefault(_KEYRING_PENDING_CLEANUP_FIELD, {})
        if not isinstance(raw_pending, dict):
            raise ValueError("Secret keyring pending cleanup metadata must be an object.")
        pending: dict[str, dict[str, str]] = {}
        for raw_username, raw_entry in raw_pending.items():
            username = str(raw_username)
            if not _valid_legacy_keyring_identifier(username) or not isinstance(
                raw_entry, dict
            ):
                raise ValueError("Secret keyring pending cleanup metadata is invalid.")
            sid = str(raw_entry.get("secret_id") or "")
            reason = str(raw_entry.get("reason") or "")
            if not _valid_legacy_keyring_identifier(sid) or reason not in {
                "uncommitted_version",
                "superseded_version",
            }:
                raise ValueError("Secret keyring pending cleanup metadata is invalid.")
            pending[username] = {"secret_id": sid, "reason": reason}
        if pending != raw_pending:
            data[_KEYRING_PENDING_CLEANUP_FIELD] = pending
        return cast(dict[str, dict[str, str]], data[_KEYRING_PENDING_CLEANUP_FIELD])

    def _prepare_keyring_metadata_unlocked(self, data: dict[str, Any]) -> bool:
        changed = False
        if data.get("backend") != "keyring":
            data["backend"] = "keyring"
            changed = True
        if data.get("keyring_metadata_version") != _KEYRING_METADATA_VERSION:
            data["keyring_metadata_version"] = _KEYRING_METADATA_VERSION
            changed = True
        pending_before = json.dumps(
            data.get(_KEYRING_PENDING_CLEANUP_FIELD),
            sort_keys=True,
            default=str,
        )
        self._pending_cleanup_unlocked(data)
        pending_after = json.dumps(
            data.get(_KEYRING_PENDING_CLEANUP_FIELD),
            sort_keys=True,
            default=str,
        )
        if pending_before != pending_after:
            changed = True
        records = data.setdefault("secrets", {})
        if not isinstance(records, dict):
            raise ValueError("Secret vault records must be a JSON object.")
        for raw_sid, raw_record in records.items():
            if not isinstance(raw_record, dict):
                continue
            sid = str(raw_record.get("id") or raw_sid)
            if not _valid_legacy_keyring_identifier(sid):
                raise ValueError("Secret keyring metadata has an invalid secret id.")
            if "value" in raw_record:
                raise ValueError(
                    "Refusing to open a populated JSON secret vault as keyring metadata; "
                    "rotate or re-enter those secrets into a separate keyring-backed vault."
                )
            if _KEYRING_USERNAME_FIELD not in raw_record:
                raw_record[_KEYRING_USERNAME_FIELD] = sid
                changed = True
            self._record_keyring_username(raw_record, sid)
            if _KEYRING_STATE_FIELD not in raw_record:
                raw_record[_KEYRING_STATE_FIELD] = _KEYRING_STATE_ACTIVE
                changed = True
            state = self._record_state(raw_record)
            if state == _KEYRING_STATE_PENDING_DELETE:
                usernames = self._delete_usernames(raw_record, sid)
                canonical = list(usernames)
                if raw_record.get(_KEYRING_DELETE_USERNAMES_FIELD) != canonical:
                    raw_record[_KEYRING_DELETE_USERNAMES_FIELD] = canonical
                    changed = True
        return changed

    def _reconcile_keyring_state(self) -> None:
        with self._vault_lock(exclusive=True):
            data = self._read_unlocked()
            changed = self._prepare_keyring_metadata_unlocked(data)
            reconcile_changed, touched = self._reconcile_unlocked(data)
            if not (changed or reconcile_changed):
                return
            try:
                self._write_unlocked(data)
            except BaseException:
                if touched:
                    raise SecretBrokerPartialCommitError(
                        operation="reconcile",
                        stage="metadata_commit",
                        secret_ids=tuple(sorted({sid for sid, _ in touched})),
                        recovery_usernames=tuple(sorted({username for _, username in touched})),
                    ) from None
                raise

    def _reconcile_unlocked(
        self,
        data: dict[str, Any],
    ) -> tuple[bool, tuple[tuple[str, str], ...]]:
        changed = False
        touched: list[tuple[str, str]] = []
        records = data.get("secrets", {})
        if not isinstance(records, dict):
            raise ValueError("Secret vault records must be a JSON object.")
        active_usernames = self._active_keyring_usernames(data)
        for raw_sid, raw_record in tuple(records.items()):
            if not isinstance(raw_record, dict):
                continue
            sid = str(raw_record.get("id") or raw_sid)
            if self._record_state(raw_record) != _KEYRING_STATE_PENDING_DELETE:
                continue
            usernames = self._delete_usernames(raw_record, sid)
            failures: list[str] = []
            for username in usernames:
                if username in active_usernames:
                    failures.append(username)
                elif self._delete_keyring_username(username):
                    touched.append((sid, username))
                else:
                    failures.append(username)
            if failures:
                if tuple(failures) != usernames:
                    raw_record[_KEYRING_DELETE_USERNAMES_FIELD] = failures
                    changed = True
            else:
                records.pop(raw_sid, None)
                changed = True

        pending_cleanup = self._pending_cleanup_unlocked(data)
        for username, entry in tuple(pending_cleanup.items()):
            sid = str(entry["secret_id"])
            if username in active_usernames:
                pending_cleanup.pop(username, None)
                changed = True
                continue
            if self._delete_keyring_username(username):
                touched.append((sid, username))
                pending_cleanup.pop(username, None)
                changed = True
        return changed, tuple(touched)

    def _finish_pending_cleanup(
        self,
        data: dict[str, Any],
        *,
        secret_id: str,
        operation: str,
    ) -> None:
        pending_cleanup = self._pending_cleanup_unlocked(data)
        active_usernames = self._active_keyring_usernames(data)
        attempted: list[str] = []
        failures: list[str] = []
        changed = False
        for username, entry in tuple(pending_cleanup.items()):
            if str(entry.get("secret_id") or "") != secret_id:
                continue
            attempted.append(username)
            if username in active_usernames or self._delete_keyring_username(username):
                pending_cleanup.pop(username, None)
                changed = True
            else:
                failures.append(username)
        if changed:
            try:
                self._write_unlocked(data)
            except BaseException:
                raise SecretBrokerPartialCommitError(
                    operation=operation,
                    stage="cleanup_metadata_commit",
                    secret_ids=(secret_id,),
                    recovery_usernames=tuple(attempted),
                ) from None
        if failures:
            raise SecretBrokerPartialCommitError(
                operation=operation,
                stage="cleanup_pending",
                secret_ids=(secret_id,),
                recovery_usernames=tuple(failures),
            ) from None

    def _abort_uncommitted_version(self, sid: str, username: str) -> None:
        cleaned = self._delete_keyring_username(username)
        if cleaned:
            recovery_data = self._read_unlocked()
            self._prepare_keyring_metadata_unlocked(recovery_data)
            self._pending_cleanup_unlocked(recovery_data).pop(username, None)
            try:
                self._write_unlocked(recovery_data)
            except BaseException:
                raise SecretBrokerPartialCommitError(
                    operation="store",
                    stage="set_failure_cleanup_metadata",
                    secret_ids=(sid,),
                    recovery_usernames=(username,),
                ) from None
            raise RuntimeError(f"Keyring store failed for secret id {sid}.") from None
        raise SecretBrokerPartialCommitError(
            operation="store",
            stage="set_failure_cleanup_pending",
            secret_ids=(sid,),
            recovery_usernames=(username,),
        ) from None

    def _recover_failed_version_commit(
        self,
        sid: str,
        username: str,
        original_error: BaseException,
    ) -> None:
        try:
            recovery_data = self._read_unlocked()
            self._prepare_keyring_metadata_unlocked(recovery_data)
            username_is_referenced = self._metadata_references_username(
                recovery_data,
                username,
            )
        except BaseException:
            raise SecretBrokerPartialCommitError(
                operation="store",
                stage="metadata_commit_state_unknown",
                secret_ids=(sid,),
                recovery_usernames=(username,),
            ) from None
        if username_is_referenced:
            # Atomic replacement may have committed even though a post-replace
            # permission hardening or directory fsync failed. Deleting this
            # version would then leave the durable active pointer dangling.
            raise SecretBrokerPartialCommitError(
                operation="store",
                stage="metadata_commit_uncertain",
                secret_ids=(sid,),
                recovery_usernames=(username,),
            ) from None
        cleaned = self._delete_keyring_username(username)
        if cleaned:
            self._pending_cleanup_unlocked(recovery_data).pop(username, None)
            try:
                self._write_unlocked(recovery_data)
            except BaseException:
                raise SecretBrokerPartialCommitError(
                    operation="store",
                    stage="metadata_rollback_pending",
                    secret_ids=(sid,),
                    recovery_usernames=(username,),
                ) from None
            raise original_error
        raise SecretBrokerPartialCommitError(
            operation="store",
            stage="metadata_commit_cleanup_pending",
            secret_ids=(sid,),
            recovery_usernames=(username,),
        ) from None

    def _recover_failed_delete_tombstone(
        self,
        *,
        record_key: str,
        sid: str,
        usernames: tuple[str, ...],
        original_error: BaseException,
    ) -> None:
        try:
            recovery_data = self._read_unlocked()
            recovery_records = recovery_data.get("secrets", {})
            if not isinstance(recovery_records, dict):
                raise ValueError("Secret vault records must be a JSON object.")
            recovery_record = recovery_records.get(record_key)
            tombstone_visible = (
                isinstance(recovery_record, dict)
                and self._record_state(recovery_record)
                == _KEYRING_STATE_PENDING_DELETE
            )
            recovery_usernames = (
                self._delete_usernames(recovery_record, sid)
                if tombstone_visible and isinstance(recovery_record, dict)
                else usernames
            )
        except BaseException:
            raise SecretBrokerPartialCommitError(
                operation="delete",
                stage="tombstone_commit_state_unknown",
                secret_ids=(sid,),
                recovery_usernames=usernames,
            ) from None
        if tombstone_visible:
            raise SecretBrokerPartialCommitError(
                operation="delete",
                stage="tombstone_commit_uncertain",
                secret_ids=(sid,),
                recovery_usernames=recovery_usernames,
            ) from None
        raise original_error

    def _finish_pending_delete(
        self,
        data: dict[str, Any],
        *,
        record_key: str,
        sid: str,
        record: dict[str, Any],
    ) -> None:
        usernames = self._delete_usernames(record, sid)
        active_usernames = self._active_keyring_usernames(data)
        failures = [
            username
            for username in usernames
            if username in active_usernames
            or not self._delete_keyring_username(username)
        ]
        records = data.get("secrets", {})
        if not isinstance(records, dict):
            raise ValueError("Secret vault records must be a JSON object.")
        if failures:
            if tuple(failures) != usernames:
                record[_KEYRING_DELETE_USERNAMES_FIELD] = failures
                try:
                    self._write_unlocked(data)
                except BaseException:
                    raise SecretBrokerPartialCommitError(
                        operation="delete",
                        stage="pending_progress_metadata",
                        secret_ids=(sid,),
                        recovery_usernames=usernames,
                    ) from None
            raise SecretBrokerPartialCommitError(
                operation="delete",
                stage="keyring_delete_pending",
                secret_ids=(sid,),
                recovery_usernames=tuple(failures),
            ) from None
        records.pop(record_key, None)
        try:
            self._write_unlocked(data)
        except BaseException:
            raise SecretBrokerPartialCommitError(
                operation="delete",
                stage="final_metadata_pending",
                secret_ids=(sid,),
                recovery_usernames=usernames,
            ) from None

    def _delete_keyring_username(self, username: str) -> bool:
        if not _valid_legacy_keyring_identifier(username):
            return False
        try:
            self.keyring.delete_password(self.service_name, username)
            return True
        except Exception:
            try:
                return self._keyring_value(username) is None
            except Exception:
                return False

    def _active_keyring_usernames(self, data: dict[str, Any]) -> set[str]:
        records = data.get("secrets", {})
        if not isinstance(records, dict):
            raise ValueError("Secret vault records must be a JSON object.")
        return {
            self._record_keyring_username(record, str(record.get("id") or sid))
            for sid, record in records.items()
            if isinstance(record, dict)
            and self._record_state(record) == _KEYRING_STATE_ACTIVE
        }

    def _metadata_references_username(
        self,
        data: dict[str, Any],
        username: str,
    ) -> bool:
        records = data.get("secrets", {})
        if not isinstance(records, dict):
            raise ValueError("Secret vault records must be a JSON object.")
        for record_key, record in records.items():
            if not isinstance(record, dict):
                continue
            sid = str(record.get("id") or record_key)
            if self._record_state(record) == _KEYRING_STATE_PENDING_DELETE:
                if username in self._delete_usernames(record, sid):
                    return True
            elif self._record_keyring_username(record, sid) == username:
                return True
        return False


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
    if not normalized:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    if len(normalized) <= _MAX_SECRET_ID_LENGTH:
        return normalized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    prefix = normalized[: _MAX_SECRET_ID_LENGTH - len(digest) - 1].rstrip("_.-")
    return f"{prefix}.{digest}" if prefix else digest


def _valid_keyring_username(value: str) -> bool:
    return bool(_KEYRING_USERNAME_RE.fullmatch(value))


def _valid_legacy_keyring_identifier(value: str) -> bool:
    """Accept pre-v2 normalized IDs while keeping all new usernames bounded."""

    return bool(_LEGACY_KEYRING_IDENTIFIER_RE.fullmatch(value))


def _safe_keyring_identifier(value: str) -> str:
    return value if _valid_keyring_username(value) else "<invalid>"


def _versioned_keyring_username(secret_id: str) -> str:
    suffix = secrets.token_hex(16)
    candidate = f"{secret_id}.v{_KEYRING_METADATA_VERSION}.{suffix}"
    if _valid_keyring_username(candidate):
        return candidate
    digest = hashlib.sha256(secret_id.encode("utf-8")).hexdigest()[:24]
    return f"secret.{digest}.v{_KEYRING_METADATA_VERSION}.{suffix}"


def _unique_keyring_usernames(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))


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
