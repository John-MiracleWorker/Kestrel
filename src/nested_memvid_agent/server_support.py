from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from .config import AgentConfig
from .secret_broker import is_secret_ref


def api_auth_error(config: AgentConfig, headers: Mapping[str, str]) -> tuple[int, str] | None:
    if not config.require_api_auth:
        return None
    expected = os.getenv(config.api_auth_token_env, "").strip()
    if not expected:
        return 503, f"Missing API auth token env: {config.api_auth_token_env}"
    candidate = ""
    authorization = str(headers.get("authorization", ""))
    x_kestrel_api_key = str(headers.get("x-kestrel-api-key", ""))
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    elif x_kestrel_api_key:
        candidate = x_kestrel_api_key.strip()
    if not candidate or not secrets.compare_digest(candidate, expected):
        return 401, "Invalid or missing Kestrel API token."
    return None


def request_headers(request: object) -> Mapping[str, str]:
    headers = getattr(request, "headers", {})
    return headers if isinstance(headers, Mapping) else {}


def csv_layers(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def bounded_limit(value: int, *, default: int, maximum: int) -> int:
    if value < 1:
        return default
    return min(value, maximum)


def hostname_from_header(value: str) -> str:
    host = value.strip()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end >= 0 else host
    return host.split(":", 1)[0]


def hostname_from_url(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.hostname == "::1":
        return "::1"
    return host


def known_secret_env_names(channels: list[dict[str, Any]], servers: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for channel in channels:
        for key in ("token_env", "webhook_url_env"):
            value = channel.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
        settings = channel.get("settings")
        if isinstance(settings, dict):
            for key in ("signature_secret_env", "webhook_url_env"):
                value = settings.get(key)
                if isinstance(value, str) and value.strip():
                    names.add(value.strip())
    for server in servers:
        secret_env = server.get("secret_env")
        if isinstance(secret_env, dict):
            for value in secret_env.values():
                if isinstance(value, str) and value.strip() and not is_secret_ref(value):
                    names.add(value.strip())
    return names


def execution_response(execution: Any) -> dict[str, object]:
    return {
        "tool": execution.call.name,
        "tool_call_id": execution.call.id,
        "success": execution.success,
        "content": execution.content,
        "data": execution.data,
        "error": execution.error,
    }


def tool_response_payload(execution: Any) -> dict[str, object]:
    stripped = str(execution.content).lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        payload = json.loads(execution.content)
        if isinstance(payload, dict):
            return dict(payload)
        if isinstance(payload, list):
            return {"success": execution.success, "items": payload, "error": execution.error}
    data = getattr(execution, "data", None)
    if isinstance(data, dict) and data:
        return {"success": execution.success, **data, "content": execution.content, "error": execution.error}
    return {"success": execution.success, "content": execution.content, "error": execution.error}
