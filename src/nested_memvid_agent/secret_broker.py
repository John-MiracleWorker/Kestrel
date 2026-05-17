from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state_store import utc_now

_SECRET_ID_RE = re.compile(r"[^a-z0-9_.-]+")
_SECRET_REF_PREFIX = "secret://"


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

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = vault_path

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
        return self._public(record)

    def list_secrets(self) -> list[dict[str, Any]]:
        secrets = self._read().get("secrets", {})
        if not isinstance(secrets, dict):
            return []
        records = [record for record in secrets.values() if isinstance(record, dict)]
        return [self._public(record) for record in sorted(records, key=lambda item: str(item.get("name", "")))]

    def get_secret(self, secret_id: str) -> dict[str, Any]:
        record = self._record(secret_id)
        if record is None:
            raise KeyError(secret_id)
        return self._public(record)

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
        if not str(record.get("value", "")).strip():
            raise ValueError("Secret value is missing.")
        now = utc_now()
        record["validated"] = True
        record["last_validated_at"] = now
        record["updated_at"] = now
        self._write(data)
        return self._public(record)

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
        if is_secret_ref(ref):
            record = self._record(ref.removeprefix(_SECRET_REF_PREFIX))
            if record is None:
                return {"secret_ref": ref, "configured": False, "validated": False}
            return self._public(record)
        env_configured = bool(os.getenv(ref, "").strip())
        for record in self._records():
            if str(record.get("name", "")).strip() == ref:
                public = self._public(record)
                public["configured"] = True
                public["source_env"] = ref
                public["source"] = "broker"
                return public
        return {"source_env": ref, "configured": env_configured, "validated": False, "source": "env" if env_configured else "missing"}

    def _record(self, secret_id: str) -> dict[str, Any] | None:
        secrets = self._read().get("secrets", {})
        if not isinstance(secrets, dict):
            return None
        record = secrets.get(secret_id)
        return record if isinstance(record, dict) else None

    def _records(self) -> list[dict[str, Any]]:
        secrets = self._read().get("secrets", {})
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

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
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
            "fingerprint": _fingerprint(value) if value else None,
            "created_at": str(record.get("created_at", "")),
            "updated_at": str(record.get("updated_at", "")),
            "source": "broker",
        }


def is_secret_ref(value: str) -> bool:
    return value.startswith(_SECRET_REF_PREFIX) and bool(value.removeprefix(_SECRET_REF_PREFIX).strip())


def _normalize_secret_id(value: str) -> str:
    normalized = _SECRET_ID_RE.sub("_", value.strip().lower()).strip("_.-")
    return normalized or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
