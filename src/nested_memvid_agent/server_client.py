from __future__ import annotations

import ipaddress
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .security_boundary import redact_text

_DEFAULT_TOKEN_ENV_NAME = "NEST_AGENT_API_TOKEN"
_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "blocked", "cancelled"}
)
_MAX_ERROR_BODY_BYTES = 16_384


class ServerClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None = None,
        recovery: str,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.recovery = recovery
        self.run_id = run_id


@dataclass(frozen=True)
class ServerProbe:
    reachable: bool
    healthy: bool
    locked: bool
    detail: str | None = None


@dataclass(frozen=True)
class KestrelServerClient:
    base_url: str
    request_timeout_seconds: float = 2.0
    token_env_name: str | None = None
    environ: Mapping[str, str] = field(
        default_factory=lambda: os.environ,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url))
        timeout = float(self.request_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("request timeout must be a finite positive number")
        object.__setattr__(self, "request_timeout_seconds", timeout)

    def probe(self) -> ServerProbe:
        try:
            payload = self._request_json("GET", "/api/health")
        except ServerClientError as exc:
            detail = f"{exc} {exc.recovery}".strip()
            if exc.code == "service_locked":
                return ServerProbe(
                    reachable=True,
                    healthy=False,
                    locked=True,
                    detail=detail,
                )
            return ServerProbe(
                reachable=exc.status_code is not None,
                healthy=False,
                locked=False,
                detail=detail,
            )
        if payload.get("ok") is not True:
            return ServerProbe(
                reachable=True,
                healthy=False,
                locked=False,
                detail="The loopback service responded but did not report healthy.",
            )
        return ServerProbe(reachable=True, healthy=True, locked=False)

    def get_runtime_config(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/runtime/config")

    def get_setup_readiness(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/product/setup")

    def create_run(
        self,
        *,
        message: str,
        session_id: str | None = None,
        workspace: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        autonomy_mode: str = "background",
    ) -> dict[str, Any]:
        if not message.strip():
            raise ValueError("run message must not be empty")
        payload: dict[str, str] = {
            "message": message,
            "autonomy_mode": autonomy_mode,
        }
        for key, value in (
            ("session_id", session_id),
            ("workspace", workspace),
            ("provider", provider),
            ("model", model),
        ):
            if value is not None:
                payload[key] = value
        return self._request_json("POST", "/api/runs", payload=payload)

    def get_run(self, run_id: str) -> dict[str, Any]:
        normalized = run_id.strip()
        if not normalized:
            raise ValueError("run ID must not be empty")
        return self._request_json(
            "GET",
            f"/api/runs/{quote(normalized, safe='')}",
        )

    def wait_for_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
        poll_interval: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        timeout = float(timeout_seconds)
        interval = float(poll_interval)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("run wait timeout must be finite and non-negative")
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("run polling interval must be finite and positive")
        deadline = clock() + timeout
        while True:
            run = self.get_run(run_id)
            status = run.get("status")
            if not isinstance(status, str):
                raise ServerClientError(
                    "Kestrel returned a run without a valid status.",
                    code="invalid_response",
                    recovery="Inspect the run in the Workbench and run `kestrel doctor`.",
                    run_id=run_id,
                )
            if status in _TERMINAL_RUN_STATUSES:
                return run
            remaining = deadline - clock()
            if remaining <= 0:
                raise ServerClientError(
                    f"Run {run_id} is still active after the local wait timeout.",
                    code="run_timeout",
                    recovery=(
                        "Inspect the durable run in the Workbench; it was not "
                        "cancelled automatically."
                    ),
                    run_id=run_id,
                )
            sleep(min(interval, remaining))

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "kestrel-launcher/1",
        }
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = None
        if payload is not None:
            body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=self.request_timeout_seconds,
            ) as response:
                status_code = int(response.status)
                raw = response.read(_MAX_ERROR_BODY_BYTES + 1)
        except HTTPError as exc:
            raw = exc.read(_MAX_ERROR_BODY_BYTES + 1)
            detail = self._error_detail(raw)
            raise self._http_error(exc.code, detail) from None
        except TimeoutError as exc:
            raise ServerClientError(
                "Timed out contacting the Kestrel loopback service.",
                code="timeout",
                recovery="Start Kestrel or inspect `.nest/server.log`, then retry.",
            ) from exc
        except URLError as exc:
            detail = redact_text(str(exc.reason), environ=self.environ)
            raise ServerClientError(
                f"Cannot reach the Kestrel loopback service: {detail}",
                code="endpoint_unreachable",
                recovery="Start Kestrel with `kestrel start`, then retry.",
            ) from None
        if len(raw) > _MAX_ERROR_BODY_BYTES:
            raise ServerClientError(
                "Kestrel returned an oversized JSON response.",
                code="invalid_response",
                status_code=status_code,
                recovery="Run `kestrel doctor` and inspect the service log.",
            )
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServerClientError(
                "Kestrel returned invalid JSON.",
                code="invalid_response",
                status_code=status_code,
                recovery="Run `kestrel doctor` and inspect the service log.",
            ) from None
        if not isinstance(decoded, dict):
            raise ServerClientError(
                "Kestrel returned an unexpected JSON payload.",
                code="invalid_response",
                status_code=status_code,
                recovery="Run `kestrel doctor` and inspect the service log.",
            )
        return decoded

    def _token(self) -> str:
        configured_name = (
            self.token_env_name
            or self.environ.get("NEST_AGENT_API_AUTH_TOKEN_ENV", "").strip()
            or _DEFAULT_TOKEN_ENV_NAME
        )
        return self.environ.get(configured_name, "").strip()

    def _error_detail(self, raw: bytes) -> str:
        truncated = raw[:_MAX_ERROR_BODY_BYTES]
        try:
            decoded = json.loads(truncated)
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = truncated.decode("utf-8", errors="replace")
        else:
            detail_value = (
                decoded.get("detail")
                if isinstance(decoded, dict)
                else decoded
            )
            if isinstance(detail_value, str):
                detail = detail_value
            else:
                detail = json.dumps(detail_value, sort_keys=True)
        return redact_text(detail.strip(), environ=self.environ)

    def _http_error(self, status_code: int, detail: str) -> ServerClientError:
        if status_code == 401:
            return ServerClientError(
                f"Kestrel service access is locked: {detail}",
                code="service_locked",
                status_code=status_code,
                recovery=(
                    "Set the configured Kestrel API token environment variable "
                    "and retry."
                ),
            )
        if (
            status_code == 503
            and "missing api auth token env" in detail.lower()
        ):
            return ServerClientError(
                f"Kestrel service access is locked: {detail}",
                code="service_locked",
                status_code=status_code,
                recovery="Set the named API token in the server environment and restart Kestrel.",
            )
        if status_code == 404:
            return ServerClientError(
                f"Kestrel resource was not found: {detail}",
                code="not_found",
                status_code=status_code,
                recovery="Verify the run ID and that the expected Kestrel version is running.",
            )
        if status_code == 409:
            return ServerClientError(
                f"Kestrel reported a state conflict: {detail}",
                code="conflict",
                status_code=status_code,
                recovery="Refresh the current state in the Workbench, then retry deliberately.",
            )
        if status_code == 429:
            return ServerClientError(
                f"Kestrel is at request capacity: {detail}",
                code="rate_limited",
                status_code=status_code,
                recovery="Wait for active work to finish or reduce concurrent requests.",
            )
        if status_code >= 500:
            return ServerClientError(
                f"Kestrel service is unavailable: {detail}",
                code="service_unavailable",
                status_code=status_code,
                recovery="Run `kestrel doctor`, inspect `.nest/server.log`, and retry.",
            )
        return ServerClientError(
            f"Kestrel request failed with HTTP {status_code}: {detail}",
            code="request_failed",
            status_code=status_code,
            recovery="Run `kestrel doctor` and verify the requested operation.",
        )


def _validate_base_url(raw: str) -> str:
    candidate = raw.strip()
    parsed = urlparse(candidate)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "Kestrel server URL must be a credential-free HTTP loopback origin"
        )
    hostname = parsed.hostname
    if hostname != "localhost":
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            raise ValueError(
                "Kestrel server URL must be a credential-free HTTP loopback origin"
            ) from None
        if not address.is_loopback:
            raise ValueError(
                "Kestrel server URL must be a credential-free HTTP loopback origin"
            )
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(
            "Kestrel server URL must be a credential-free HTTP loopback origin"
        ) from None
    if port is None:
        port = 80
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{host}:{port}"
