from __future__ import annotations

import json
import os
import secrets
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from .config import AgentConfig
from .secret_broker import is_secret_ref
from .security_boundary import redact_secrets


class RequestRateLimiter:
    """Small fixed-window limiter for the single-process control plane."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def allow(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        max_keys: int = 2048,
    ) -> bool:
        now = monotonic()
        cutoff = now - max(0.01, window_seconds)
        with self._lock:
            if key not in self._requests and len(self._requests) >= max(1, max_keys):
                expired = [
                    existing
                    for existing, history in self._requests.items()
                    if not history or history[-1] <= cutoff
                ]
                for existing in expired:
                    self._requests.pop(existing, None)
                if len(self._requests) >= max(1, max_keys):
                    if "__overflow_clients__" not in self._requests:
                        self._requests.pop(next(iter(self._requests)))
                    key = "__overflow_clients__"
            history = self._requests[key]
            while history and history[0] <= cutoff:
                history.popleft()
            if len(history) >= max(1, limit):
                return False
            history.append(now)
            return True

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._requests)


class RequestBodyTooLarge(ValueError):
    pass


async def cache_bounded_request_body(request: Any, *, limit: int) -> int:
    """Read and cache an ASGI request body without trusting Content-Length."""
    maximum = max(1, limit)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise RequestBodyTooLarge("request body exceeds configured maximum")
        body.extend(chunk)
    request._body = bytes(body)
    return len(body)


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


def host_is_trusted(host: str, trusted_hosts: Iterable[str]) -> bool:
    """Return whether an HTTP Host/Origin hostname is allowed.

    Exact hostnames remain the default. Wildcard entries are intentionally
    limited to a leading ``*.`` suffix match so temporary tunnel hosts such as
    ``*.trycloudflare.com`` can be trusted without allowing arbitrary domains.
    """

    normalized = host.strip().lower().rstrip(".")
    trusted = {item.strip().lower().rstrip(".") for item in trusted_hosts if item.strip()}
    if "*" in trusted or normalized in trusted:
        return True
    for item in trusted:
        if not item.startswith("*."):
            continue
        suffix = item[1:]
        if normalized.endswith(suffix) and normalized != suffix.lstrip("."):
            return True
    return False


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
    payload = {
        "tool": execution.call.name,
        "tool_call_id": execution.call.id,
        "success": execution.success,
        "content": execution.content,
        "data": execution.data,
        "error": execution.error,
    }
    safe_payload = redact_secrets(payload)
    return dict(safe_payload) if isinstance(safe_payload, dict) else {}


def tool_response_payload(execution: Any) -> dict[str, object]:
    stripped = str(execution.content).lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        payload = json.loads(execution.content)
        if isinstance(payload, dict):
            safe_payload = redact_secrets(payload)
            return dict(safe_payload) if isinstance(safe_payload, dict) else {}
        if isinstance(payload, list):
            response = {
                "success": execution.success,
                "items": payload,
                "error": execution.error,
            }
            safe_response = redact_secrets(response)
            return dict(safe_response) if isinstance(safe_response, dict) else {}
    data = getattr(execution, "data", None)
    if isinstance(data, dict) and data:
        response = {
            "success": execution.success,
            **data,
            "content": execution.content,
            "error": execution.error,
        }
    else:
        response = {
            "success": execution.success,
            "content": execution.content,
            "error": execution.error,
        }
    safe_response = redact_secrets(response)
    return dict(safe_response) if isinstance(safe_response, dict) else {}
