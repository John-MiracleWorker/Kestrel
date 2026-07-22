from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path, PurePath
from threading import RLock
from typing import Any

from .models import EvidenceRef, MemoryRecord

REDACTED = "<redacted>"

_MIN_REGISTERED_SECRET_LENGTH = 8
_MAX_REGISTERED_SECRET_VALUES = 2048
_REGISTERED_SECRET_VALUES: dict[str, None] = {}
_REGISTERED_SECRET_ENV_NAMES: set[str] = set()
_SECRET_REGISTRY_LOCK = RLock()
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

_SENSITIVE_DIRECTORY_NAMES = frozenset(
    {
        ".aws",
        ".gnupg",
        ".ssh",
        ".secrets",
        "credentials",
        "private_keys",
        "secrets",
    }
)
_SENSITIVE_EXACT_FILENAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "application_default_credentials.json",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "netrc",
        "service-account.json",
        "service_account.json",
    }
)
_SENSITIVE_SUFFIXES = frozenset(
    {
        ".cer",
        ".crt",
        ".der",
        ".jks",
        ".key",
        ".kdbx",
        ".keystore",
        ".p12",
        ".pem",
        ".pfx",
        ".pkcs12",
    }
)
_SENSITIVE_COMPONENT_SEQUENCES = (
    (".git", "config"),
    (".docker", "config.json"),
    (".kube", "config"),
    (".aws", "credentials"),
    (".config", "gh", "hosts.yml"),
    (".config", "gcloud", "application_default_credentials.json"),
)

_SECRET_KEYS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "secrets",
        "token",
    }
)
_SECRET_KEY_SUFFIXES = tuple(f"_{key}" for key in _SECRET_KEYS)
_PUBLIC_SECRET_METADATA_SUFFIXES = (
    "_backend",
    "_configured",
    "_env",
    "_file",
    "_id",
    "_name",
    "_path",
    "_ref",
    "_status",
)

_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [^-\r\n]*(?:PRIVATE KEY|CERTIFICATE)-----.*?"
    r"-----END [^-\r\n]*(?:PRIVATE KEY|CERTIFICATE)-----",
    re.DOTALL | re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+"
)
_BEARER_CREDENTIAL_RE = re.compile(r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{12,}")
_ASSIGNMENT_RE = re.compile(
    r"(?im)^(\s*(?:export\s+)?"
    r"[A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_KEY|ACCESS_TOKEN|AUTH_TOKEN|BOT_TOKEN|"
    r"CLIENT_SECRET|CREDENTIALS?|PASSWORD|PASSWD|PRIVATE_KEY|REFRESH_TOKEN|SECRET)"
    r"[A-Z0-9_]*\s*=\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s#\r\n]+)"
)
_KEY_VALUE_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?key|access[_-]?token|auth[_-]?token|"
    r"bot[_-]?token|client[_-]?secret|credentials?|password|passwd|private[_-]?key|"
    r"refresh[_-]?token|secret|token)[\"']?\s*[:=]\s*)"
    r"(?!//)(?!(?:(?-i:null|true|false))(?=\s*[,}\]\r\n]|$))"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\]}]+)"
)
_URI_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@")
_KNOWN_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)

_EXACT_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "CLOUDFLARE_API_TOKEN",
        "DATABASE_URL",
        "DOCKER_AUTH_CONFIG",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GPG_AGENT_INFO",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "KUBECONFIG",
        "MONGODB_URI",
        "NETRC",
        "NEST_AGENT_API_TOKEN",
        "NEST_AGENT_WEBHOOK_SECRET",
        "POSTGRES_URL",
        "REDIS_URL",
        "SSH_AUTH_SOCK",
        "TELEGRAM_BOT_TOKEN",
    }
)
_CREDENTIAL_ENV_SEGMENTS = frozenset(
    {
        "API_KEY",
        "AUTH_TOKEN",
        "BOT_TOKEN",
        "CLIENT_SECRET",
        "CREDENTIAL",
        "CREDENTIALS",
        "KEY",
        "PASSWORD",
        "PASSWD",
        "PRIVATE_KEY",
        "REFRESH_TOKEN",
        "SECRET",
        "TOKEN",
    }
)


def register_secret_value(value: str | None) -> None:
    """Remember an exact runtime secret so opaque echoes can be redacted later."""

    if not isinstance(value, str):
        return
    candidate = value.strip()
    if (
        len(candidate) < _MIN_REGISTERED_SECRET_LENGTH
        or candidate == REDACTED
        or _is_secret_ref(candidate)
    ):
        return
    with _SECRET_REGISTRY_LOCK:
        # Refresh insertion order so values used recently survive bounded eviction.
        _REGISTERED_SECRET_VALUES.pop(candidate, None)
        _REGISTERED_SECRET_VALUES[candidate] = None
        while len(_REGISTERED_SECRET_VALUES) > _MAX_REGISTERED_SECRET_VALUES:
            oldest = next(iter(_REGISTERED_SECRET_VALUES))
            del _REGISTERED_SECRET_VALUES[oldest]


def register_secret_env_names(
    names: Iterable[str | None],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Register configured credential env names, including provider-specific names."""

    registered = {
        name.strip()
        for name in names
        if isinstance(name, str) and _ENV_NAME_RE.fullmatch(name.strip())
    }
    if not registered:
        return
    with _SECRET_REGISTRY_LOCK:
        _REGISTERED_SECRET_ENV_NAMES.update(registered)
    environment = os.environ if environ is None else environ
    for name in registered:
        register_secret_value(environment.get(name))


def assert_path_not_sensitive(
    workspace: Path,
    path: Path,
    *,
    requested_path: str | None = None,
) -> None:
    """Reject credential-bearing paths after resolution and before file access."""

    root = workspace.resolve()
    resolved = path.resolve()
    candidates: list[tuple[str, ...]] = []
    try:
        candidates.append(tuple(part.casefold() for part in resolved.relative_to(root).parts))
    except ValueError:
        raise ValueError("Path escapes workspace.") from None
    if requested_path is not None:
        candidates.append(_requested_path_parts(requested_path))
    if any(_parts_are_sensitive(parts) for parts in candidates):
        raise ValueError("Access to sensitive credential paths is not allowed.")


def is_sensitive_path(
    workspace: Path,
    path: Path,
    *,
    requested_path: str | None = None,
) -> bool:
    try:
        assert_path_not_sensitive(workspace, path, requested_path=requested_path)
    except ValueError:
        return True
    return False


def redact_secrets(value: Any, *, environ: Mapping[str, str] | None = None) -> Any:
    """Recursively remove raw credential material from an untrusted payload."""

    if isinstance(value, dict):
        return {
            redact_text(key, environ=environ) if isinstance(key, str) else key: REDACTED
            if _should_redact_secret_item(str(key), item)
            else redact_secrets(item, environ=environ)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item, environ=environ) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item, environ=environ) for item in value)
    if isinstance(value, str):
        return redact_text(value, environ=environ)
    return value


def redact_text(text: str, *, environ: Mapping[str, str] | None = None) -> str:
    redacted = text
    environment = os.environ if environ is None else environ
    credential_values = {
        value
        for name, value in environment.items()
        if is_credential_env_name(name) and isinstance(value, str) and len(value) >= 8
    }
    with _SECRET_REGISTRY_LOCK:
        credential_values.update(_REGISTERED_SECRET_VALUES)
        registered_env_names = tuple(_REGISTERED_SECRET_ENV_NAMES)
    for name in registered_env_names:
        value = environment.get(name)
        if isinstance(value, str) and len(value.strip()) >= _MIN_REGISTERED_SECRET_LENGTH:
            credential_values.add(value.strip())
    for secret_value in sorted(credential_values, key=len, reverse=True):
        redacted = redacted.replace(secret_value, REDACTED)
    redacted = _PEM_BLOCK_RE.sub(REDACTED, redacted)
    redacted = _AUTHORIZATION_RE.sub(r"\1<redacted>", redacted)
    redacted = _BEARER_CREDENTIAL_RE.sub(r"\1<redacted>", redacted)
    redacted = _ASSIGNMENT_RE.sub(r"\1<redacted>", redacted)
    redacted = _KEY_VALUE_RE.sub(r"\1<redacted>", redacted)
    redacted = _URI_USERINFO_RE.sub(r"\1<redacted>:<redacted>@", redacted)
    for pattern in _KNOWN_TOKEN_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def sanitize_memory_record(record: MemoryRecord) -> MemoryRecord:
    """Return a copy safe for permanent or run-scoped memory storage."""

    safe_tags = redact_secrets(record.tags)
    safe_metadata = redact_secrets(record.metadata)
    return replace(
        record,
        title=redact_text(record.title),
        content=redact_text(record.content),
        tags=safe_tags if isinstance(safe_tags, dict) else {},
        metadata=safe_metadata if isinstance(safe_metadata, dict) else {},
        evidence=[
            EvidenceRef(
                source=redact_text(ref.source),
                locator=redact_text(ref.locator),
                quote=redact_text(ref.quote) if ref.quote else None,
            )
            for ref in record.evidence
        ],
    )


def sanitized_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an inherited environment without credential capabilities."""

    environment = os.environ if source is None else source
    with _SECRET_REGISTRY_LOCK:
        registered_env_names = {name.upper() for name in _REGISTERED_SECRET_ENV_NAMES}
    return {
        name: value
        for name, value in environment.items()
        if name.strip().upper() not in registered_env_names and not is_credential_env_name(name)
    }


def is_credential_env_name(name: str) -> bool:
    normalized = name.strip().upper()
    if normalized in _EXACT_CREDENTIAL_ENV_NAMES:
        return True
    padded = f"_{normalized}_"
    return any(f"_{segment}_" in padded for segment in _CREDENTIAL_ENV_SEGMENTS)


def _requested_path_parts(requested_path: str) -> tuple[str, ...]:
    normalized = requested_path.replace("\\", "/")
    return tuple(part.casefold() for part in PurePath(normalized).parts if part not in {"", "."})


def _parts_are_sensitive(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    if any(part in _SENSITIVE_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    for sequence in _SENSITIVE_COMPONENT_SEQUENCES:
        width = len(sequence)
        if any(parts[index : index + width] == sequence for index in range(len(parts) - width + 1)):
            return True
    filename = parts[-1]
    if filename.startswith(".env"):
        return True
    if filename in _SENSITIVE_EXACT_FILENAMES:
        return True
    if Path(filename).suffix.casefold() in _SENSITIVE_SUFFIXES:
        return True
    if filename.startswith(("client_secret", "service-account", "service_account")):
        return True
    return filename in {"auth-token", "auth_token", "private-key", "private_key"}


def _should_redact_secret_item(key: str, value: Any) -> bool:
    if value is None:
        return False
    normalized = key.strip().lower().replace("-", "_")
    if _is_raw_secret_key(normalized):
        return True
    metadata_suffix = _secret_metadata_suffix(normalized)
    if metadata_suffix is None:
        return False
    return not _safe_secret_metadata_value(metadata_suffix, value)


def _is_raw_secret_key(normalized: str) -> bool:
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_KEY_SUFFIXES)


def _secret_metadata_suffix(normalized: str) -> str | None:
    remaining = normalized
    suffixes: list[str] = []
    while True:
        suffix = next(
            (
                candidate
                for candidate in _PUBLIC_SECRET_METADATA_SUFFIXES
                if remaining.endswith(candidate)
            ),
            None,
        )
        if suffix is None:
            break
        suffixes.append(suffix)
        remaining = remaining[: -len(suffix)]
    if suffixes and _is_raw_secret_key(remaining):
        return suffixes[0]
    return None


def _safe_secret_metadata_value(suffix: str, value: Any) -> bool:
    if suffix == "_configured":
        return isinstance(value, bool)
    if suffix == "_env":
        if isinstance(value, str):
            return bool(_ENV_NAME_RE.fullmatch(value.strip()) or _is_secret_ref(value.strip()))
        if isinstance(value, Mapping):
            if set(value) == {"name", "present"}:
                return (
                    isinstance(value.get("name"), str)
                    and bool(_ENV_NAME_RE.fullmatch(value["name"].strip()))
                    and isinstance(value.get("present"), bool)
                )
            return all(
                isinstance(key, str)
                and bool(_ENV_NAME_RE.fullmatch(key.strip()))
                and isinstance(item, str)
                and bool(_ENV_NAME_RE.fullmatch(item.strip()) or _is_secret_ref(item.strip()))
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return all(
                isinstance(item, str)
                and bool(_ENV_NAME_RE.fullmatch(item.strip()) or _is_secret_ref(item.strip()))
                for item in value
            )
        return False
    if suffix == "_ref":
        return isinstance(value, str) and _is_secret_ref(value.strip())
    if suffix == "_backend":
        return isinstance(value, str) and value.strip().lower() in {
            "file",
            "json",
            "keyring",
            "local",
        }
    if suffix == "_status":
        if isinstance(value, (Mapping, list, tuple)):
            return True
        if isinstance(value, bool):
            return True
        return isinstance(value, str) and value.strip().lower() in {
            "configured",
            "invalid",
            "missing",
            "unconfigured",
            "unregistered",
            "valid",
            "validated",
        }
    # Secret-like file, id, name, and path keys are not safe merely because
    # they end in a metadata-looking suffix.
    return False


def _is_secret_ref(value: str) -> bool:
    return value.startswith("secret://") and bool(value.removeprefix("secret://").strip())
