from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from .state_store import utc_now

_SECRET_ID_RE = re.compile(r"[^a-z0-9_.-]+")
_SECRET_REF_PREFIX = "secret://"  # nosec B105


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
        self.vault_path = vault_path
        self.allowed_env_names = {name.strip() for name in (allowed_env_names or set()) if name.strip()}

    def register_allowed_env_names(self, names: set[str]) -> None:
        self.allowed_env_names.update(name.strip() for name in names if name.strip())

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
        data = self._read()
        salt = _ensure_fingerprint_salt(data)
        secrets = data.setdefault("secrets", {})
        now = utc_now()
        sid = _normalize_secret_id(secret_id or clean_name)
        existing = secrets.get(sid) if isinstance(secrets.get(sid), dict) else {}
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
        secrets[sid] = record
        self._write(data)
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
        data = self._read()
        secrets = data.setdefault("secrets", {})
        if not isinstance(secrets, dict) or secret_id not in secrets:
            raise KeyError(secret_id)
        del secrets[secret_id]
        self._write(data)

    def validate_secret(self, secret_id: str) -> dict[str, Any]:
        data = self._read()
        secrets = data.setdefault("secrets", {})
        if not isinstance(secrets, dict) or secret_id not in secrets or not isinstance(secrets[secret_id], dict):
            raise KeyError(secret_id)
        record = secrets[secret_id]
        salt = _ensure_fingerprint_salt(data)
        if not str(record.get("value", "")).strip():
            raise ValueError("Secret value is missing.")
        now = utc_now()
        record["validated"] = True
        record["last_validated_at"] = now
        record["updated_at"] = now
        self._write(data)
        return self._public(record, salt=salt)

    def resolve(self, name_or_ref: str | None) -> str | None:
        ref = (name_or_ref or "").strip()
        if not ref:
            return None
        if is_secret_ref(ref):
            record = self._record(ref.removeprefix(_SECRET_REF_PREFIX))
            return None if record is None else str(record.get("value", "")).strip() or None
        env_value = os.getenv(ref, "").strip()
        if env_value:
            return env_value
        for record in self._records():
            if str(record.get("name", "")).strip() == ref:
                return str(record.get("value", "")).strip() or None
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
        if not self.vault_path.exists():
            return {"secrets": {}}
        raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"secrets": {}}

    def _write(self, data: dict[str, Any]) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        self.vault_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(self.vault_path, 0o600)

    def _public(self, record: dict[str, Any], *, salt: str | None = None) -> dict[str, Any]:
        value = str(record.get("value", ""))
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
        self.keyring: _KeyringModule = keyring or import_module("keyring")
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
        data = self._read()
        data["backend"] = "keyring"
        salt = _ensure_fingerprint_salt(data)
        records = data.setdefault("secrets", {})
        now = utc_now()
        sid = _normalize_secret_id(secret_id or clean_name)
        existing = records.get(sid) if isinstance(records.get(sid), dict) else {}
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
        self._write(data)
        return self._public(record, salt=salt)

    def delete_secret(self, secret_id: str) -> None:
        data = self._read()
        records = data.setdefault("secrets", {})
        if not isinstance(records, dict) or secret_id not in records:
            raise KeyError(secret_id)
        try:
            self.keyring.delete_password(self.service_name, secret_id)
        except Exception:  # nosec B110
            pass
        del records[secret_id]
        self._write(data)

    def validate_secret(self, secret_id: str) -> dict[str, Any]:
        data = self._read()
        records = data.setdefault("secrets", {})
        if not isinstance(records, dict) or secret_id not in records or not isinstance(records[secret_id], dict):
            raise KeyError(secret_id)
        record = records[secret_id]
        salt = _ensure_fingerprint_salt(data)
        if not self._keyring_value(str(record.get("id", ""))):
            raise ValueError("Secret value is missing.")
        now = utc_now()
        record["validated"] = True
        record["last_validated_at"] = now
        record["updated_at"] = now
        self._write(data)
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
            return env_value
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
        return value.strip() if isinstance(value, str) and value.strip() else None


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
        except ImportError:
            return SecretBroker(vault_path, allowed_env_names=allowed_env_names)
    raise ValueError(f"Unsupported secret backend: {selected}")


def is_secret_ref(value: str) -> bool:
    return value.startswith(_SECRET_REF_PREFIX) and bool(value.removeprefix(_SECRET_REF_PREFIX).strip())


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
