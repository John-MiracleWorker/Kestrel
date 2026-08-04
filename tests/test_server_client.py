from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from nested_memvid_agent.server_client import (
    KestrelServerClient,
    ServerClientError,
)

Route = tuple[int, object] | Callable[[dict[str, object]], tuple[int, object]]


@contextmanager
def _http_server(
    routes: dict[tuple[str, str], Route],
) -> Iterator[tuple[str, list[dict[str, object]]]]:
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._respond()

        def do_POST(self) -> None:  # noqa: N802
            self._respond()

        def _respond(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            raw_body = self.rfile.read(length)
            request = {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": json.loads(raw_body) if raw_body else None,
            }
            requests.append(request)
            route = routes.get((self.command, self.path), (404, {"detail": "missing"}))
            status, payload = route(request) if callable(route) else route
            if isinstance(payload, bytes):
                encoded = payload
            else:
                encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8765",
        "http://example.com:8765",
        "http://user:password@127.0.0.1:8765",
        "http://127.0.0.1:8765?unsafe=1",
        "ftp://127.0.0.1:8765",
    ],
)
def test_client_rejects_non_loopback_or_credential_bearing_urls(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="loopback"):
        KestrelServerClient(base_url)


def test_client_probes_health_and_reads_runtime_payloads() -> None:
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/health"): (200, {"ok": True, "name": "Kestrel"}),
        (
            "GET",
            "/api/runtime/config",
        ): (200, {"provider": {"name": "mock", "model": "mock"}}),
        (
            "GET",
            "/api/product/setup",
        ): (200, {"experience_mode": "demo", "next_action": "kestrel chat"}),
    }
    with _http_server(routes) as (base_url, requests):
        client = KestrelServerClient(base_url)

        probe = client.probe()
        runtime = client.get_runtime_config()
        readiness = client.get_setup_readiness()

    assert probe.reachable is True
    assert probe.healthy is True
    assert probe.locked is False
    assert runtime["provider"] == {"name": "mock", "model": "mock"}
    assert readiness["experience_mode"] == "demo"
    assert [request["path"] for request in requests] == [
        "/api/health",
        "/api/runtime/config",
        "/api/product/setup",
    ]


def test_desktop_compatibility_probe_checks_profile_and_version_without_exposing_token() -> None:
    token = "desktop-compatibility-token-secret"
    routes: dict[tuple[str, str], Route] = {
        (
            "GET",
            "/api/desktop/readiness",
        ): (
            200,
            {
                "schema": "kestrel.desktop.readiness.v1",
                "ready": True,
                "profile_id": "default",
                "launch_nonce_digest": "a" * 64,
                "sidecar_version": "0.5.0",
                "state_schema_version": 21,
                "routing_schema_version": 2,
                "memory_layers": [
                    "working",
                    "episodic",
                    "semantic",
                    "procedural",
                    "self",
                    "policy",
                ],
            },
        ),
    }
    environ = {
        "NEST_AGENT_API_AUTH_TOKEN_ENV": "DESKTOP_COMPATIBILITY_TOKEN",
        "DESKTOP_COMPATIBILITY_TOKEN": token,
    }
    with _http_server(routes) as (base_url, requests):
        result = KestrelServerClient(
            base_url,
            environ=environ,
        ).probe_desktop_compatibility(
            profile_id="default",
            version="0.5.0",
            launch_nonce_digest="a" * 64,
        )

    assert result.disposition == "attach_desktop"
    assert result.profile_id == "default"
    assert result.version == "0.5.0"
    assert token not in repr(result)
    assert token not in str(result.detail)
    assert requests[0]["headers"]["authorization"] == f"Bearer {token}"


def test_desktop_compatibility_probe_uses_the_lease_base_url() -> None:
    readiness = {
        "schema": "kestrel.desktop.readiness.v1",
        "ready": True,
        "profile_id": "default",
        "launch_nonce_digest": "a" * 64,
        "sidecar_version": "0.5.0",
        "state_schema_version": 21,
        "routing_schema_version": 2,
        "memory_layers": [
            "working",
            "episodic",
            "semantic",
            "procedural",
            "self",
            "policy",
        ],
    }
    configured_routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/desktop/readiness"): (200, readiness),
    }
    lease_routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/desktop/readiness"): (200, readiness),
    }
    with _http_server(configured_routes) as (configured_url, configured_requests):
        with _http_server(lease_routes) as (lease_url, lease_requests):
            result = KestrelServerClient(
                configured_url,
            ).probe_desktop_compatibility(
                profile_id="default",
                version="0.5.0",
                launch_nonce_digest="a" * 64,
                base_url=lease_url,
            )

    assert result.disposition == "attach_desktop"
    assert configured_requests == []
    assert [request["path"] for request in lease_requests] == [
        "/api/desktop/readiness"
    ]


def test_desktop_compatibility_probe_rejects_a_nonce_mismatch_without_exposing_it() -> None:
    expected_nonce_digest = "b" * 64
    routes: dict[tuple[str, str], Route] = {
        (
            "GET",
            "/api/desktop/readiness",
        ): (
            200,
            {
                "schema": "kestrel.desktop.readiness.v1",
                "ready": True,
                "profile_id": "default",
                "launch_nonce_digest": "a" * 64,
                "sidecar_version": "0.5.0",
                "state_schema_version": 21,
                "routing_schema_version": 2,
                "memory_layers": [
                    "working",
                    "episodic",
                    "semantic",
                    "procedural",
                    "self",
                    "policy",
                ],
            },
        ),
    }
    with _http_server(routes) as (base_url, _requests):
        result = KestrelServerClient(base_url).probe_desktop_compatibility(
            profile_id="default",
            version="0.5.0",
            launch_nonce_digest=expected_nonce_digest,
        )

    assert result.disposition == "foreign_or_unrelated"
    assert result.detail == "desktop_nonce_mismatch"
    assert expected_nonce_digest not in repr(result)
    assert expected_nonce_digest not in str(result.detail)


def test_desktop_compatibility_probe_rejects_wrong_profile_or_version() -> None:
    routes: dict[tuple[str, str], Route] = {
        (
            "GET",
            "/api/desktop/readiness",
        ): (
            200,
            {
                "schema": "kestrel.desktop.readiness.v1",
                "ready": True,
                "profile_id": "other",
                "launch_nonce_digest": "a" * 64,
                "sidecar_version": "0.4.11",
                "state_schema_version": 21,
                "routing_schema_version": 2,
                "memory_layers": [
                    "working",
                    "episodic",
                    "semantic",
                    "procedural",
                    "self",
                    "policy",
                ],
            },
        ),
    }
    with _http_server(routes) as (base_url, _requests):
        client = KestrelServerClient(base_url)
        version = client.probe_desktop_compatibility(
            profile_id="other",
            version="0.5.0",
            launch_nonce_digest="a" * 64,
        )
        profile = client.probe_desktop_compatibility(
            profile_id="default",
            version="0.4.11",
            launch_nonce_digest="a" * 64,
        )

    assert version.disposition == "version_conflict"
    assert profile.disposition == "foreign_or_unrelated"


def test_client_uses_the_configured_bearer_token_without_exposing_it() -> None:
    token = "kestrel-client-token-secret-value"
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/health"): (200, {"ok": True, "name": "Kestrel"}),
    }
    environ = {
        "NEST_AGENT_API_AUTH_TOKEN_ENV": "CUSTOM_KESTREL_API_TOKEN",
        "CUSTOM_KESTREL_API_TOKEN": token,
    }
    with _http_server(routes) as (base_url, requests):
        client = KestrelServerClient(base_url, environ=environ)
        assert token not in repr(client)
        assert client.probe().healthy is True

    assert requests[0]["headers"]["authorization"] == f"Bearer {token}"
    assert token not in repr(requests[0]["path"])


def test_authenticated_server_without_client_token_is_locked_not_offline() -> None:
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/health"): (
            401,
            {"detail": "Invalid or missing Kestrel API token."},
        ),
    }
    with _http_server(routes) as (base_url, _requests):
        probe = KestrelServerClient(base_url, environ={}).probe()

    assert probe.reachable is True
    assert probe.healthy is False
    assert probe.locked is True
    assert "token" in str(probe.detail).lower()


def test_client_creates_runs_with_only_supported_fields() -> None:
    routes: dict[tuple[str, str], Route] = {
        ("POST", "/api/runs"): (
            200,
            {"run_id": "run_123", "status": "queued", "message": "hello"},
        ),
    }
    with _http_server(routes) as (base_url, requests):
        result = KestrelServerClient(base_url).create_run(
            message="hello",
            session_id="session-1",
            workspace="/tmp/workspace",
            provider=None,
            model=None,
            autonomy_mode="background",
        )

    assert result["run_id"] == "run_123"
    assert requests[0]["body"] == {
        "message": "hello",
        "session_id": "session-1",
        "workspace": "/tmp/workspace",
        "autonomy_mode": "background",
    }


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "blocked", "cancelled"])
def test_run_polling_stops_at_every_terminal_status(terminal_status: str) -> None:
    responses = iter(
        [
            (200, {"run_id": "run_terminal", "status": "running"}),
            (
                200,
                {
                    "run_id": "run_terminal",
                    "status": terminal_status,
                    "assistant_message": "done",
                },
            ),
        ]
    )
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/runs/run_terminal"): lambda _request: next(responses),
    }
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with _http_server(routes) as (base_url, requests):
        result = KestrelServerClient(base_url).wait_for_run(
            "run_terminal",
            timeout_seconds=5,
            poll_interval=0.25,
            clock=lambda: now[0],
            sleep=sleep,
        )

    assert result["status"] == terminal_status
    assert len(requests) == 2


def test_run_polling_timeout_preserves_the_durable_run_id() -> None:
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/runs/run_durable"): (
            200,
            {"run_id": "run_durable", "status": "running"},
        ),
    }
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with _http_server(routes) as (base_url, requests):
        with pytest.raises(ServerClientError) as exc_info:
            KestrelServerClient(base_url).wait_for_run(
                "run_durable",
                timeout_seconds=0.5,
                poll_interval=0.25,
                clock=lambda: now[0],
                sleep=sleep,
            )

    assert exc_info.value.code == "run_timeout"
    assert exc_info.value.run_id == "run_durable"
    assert all(request["method"] == "GET" for request in requests)
    assert not any("cancel" in str(request["path"]) for request in requests)


@pytest.mark.parametrize(
    "transient_code",
    ["timeout", "endpoint_unreachable", "service_unavailable"],
)
def test_run_polling_retries_a_transient_error_then_returns_completion(
    monkeypatch: pytest.MonkeyPatch,
    transient_code: str,
) -> None:
    transient_error = ServerClientError(
        "poll was interrupted",
        code=transient_code,
        recovery="retry",
    )
    responses: Iterator[dict[str, object] | ServerClientError] = iter(
        [
            transient_error,
            {
                "run_id": "run_durable",
                "status": "completed",
                "assistant_message": "done",
            },
        ]
    )
    polls: list[str] = []

    def get_run(_client: KestrelServerClient, run_id: str) -> dict[str, object]:
        polls.append(run_id)
        result = next(responses)
        if isinstance(result, ServerClientError):
            raise result
        return result

    monkeypatch.setattr(KestrelServerClient, "get_run", get_run)
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    result = KestrelServerClient("http://127.0.0.1:8765").wait_for_run(
        "run_durable",
        timeout_seconds=1.0,
        poll_interval=0.25,
        clock=lambda: now[0],
        sleep=sleep,
    )

    assert result["status"] == "completed"
    assert polls == ["run_durable", "run_durable"]
    assert sleeps == [0.25]


def test_run_polling_repeated_transient_timeouts_end_as_durable_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls: list[str] = []

    def get_run(_client: KestrelServerClient, run_id: str) -> dict[str, object]:
        polls.append(run_id)
        raise ServerClientError(
            "poll timed out",
            code="timeout",
            recovery="retry",
        )

    monkeypatch.setattr(KestrelServerClient, "get_run", get_run)
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(ServerClientError) as exc_info:
        KestrelServerClient("http://127.0.0.1:8765").wait_for_run(
            "run_durable",
            timeout_seconds=0.5,
            poll_interval=0.25,
            clock=lambda: now[0],
            sleep=sleep,
        )

    assert exc_info.value.code == "run_timeout"
    assert exc_info.value.run_id == "run_durable"
    assert "not cancelled" in exc_info.value.recovery
    assert polls == ["run_durable", "run_durable", "run_durable"]


@pytest.mark.parametrize(
    "code",
    [
        "service_locked",
        "not_found",
        "conflict",
        "invalid_response",
        "rate_limited",
        "request_failed",
    ],
)
def test_run_polling_does_not_retry_non_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    original = ServerClientError(
        "poll failed",
        code=code,
        recovery="operator action required",
    )
    polls: list[str] = []

    def get_run(_client: KestrelServerClient, run_id: str) -> dict[str, object]:
        polls.append(run_id)
        raise original

    monkeypatch.setattr(KestrelServerClient, "get_run", get_run)
    sleeps: list[float] = []

    with pytest.raises(ServerClientError) as exc_info:
        KestrelServerClient("http://127.0.0.1:8765").wait_for_run(
            "run_durable",
            timeout_seconds=1.0,
            poll_interval=0.25,
            clock=lambda: 0.0,
            sleep=sleeps.append,
        )

    assert exc_info.value is original
    assert polls == ["run_durable"]
    assert sleeps == []


@pytest.mark.parametrize(
    ("status", "payload", "expected_code"),
    [
        (401, {"detail": "bad token"}, "service_locked"),
        (404, {"detail": "run missing"}, "not_found"),
        (409, {"detail": "state conflict"}, "conflict"),
        (429, {"detail": "capacity reached"}, "rate_limited"),
        (503, {"detail": "provider unavailable"}, "service_unavailable"),
    ],
)
def test_http_failures_are_normalized(
    status: int,
    payload: object,
    expected_code: str,
) -> None:
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/runtime/config"): (status, payload),
    }
    with _http_server(routes) as (base_url, _requests):
        with pytest.raises(ServerClientError) as exc_info:
            KestrelServerClient(base_url).get_runtime_config()

    assert exc_info.value.code == expected_code
    assert exc_info.value.status_code == status
    assert exc_info.value.recovery


def test_server_error_detail_is_redacted() -> None:
    secret = "sk-proj-serverClientSecret123456"
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/runtime/config"): (
            500,
            {"detail": f"authorization: Bearer {secret}"},
        ),
    }
    with _http_server(routes) as (base_url, _requests):
        with pytest.raises(ServerClientError) as exc_info:
            KestrelServerClient(
                base_url,
                environ={"OPENAI_API_KEY": secret},
            ).get_runtime_config()

    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


def test_server_error_detail_redacts_token_from_opaque_configured_env_name() -> None:
    token = "opaque-loopback-token-value"
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/runtime/config"): (
            500,
            {"detail": f"server reflected configured credential {token}"},
        ),
    }
    environ = {
        "NEST_AGENT_API_AUTH_TOKEN_ENV": "AUTH",
        "AUTH": token,
    }
    with _http_server(routes) as (base_url, _requests):
        with pytest.raises(ServerClientError) as exc_info:
            KestrelServerClient(
                base_url,
                environ=environ,
            ).get_runtime_config()

    assert token not in str(exc_info.value)
    assert token not in repr(exc_info.value)


def test_malformed_json_is_a_reachable_invalid_response() -> None:
    routes: dict[tuple[str, str], Route] = {
        ("GET", "/api/health"): (200, b"not-json"),
    }
    with _http_server(routes) as (base_url, _requests):
        probe = KestrelServerClient(base_url).probe()

    assert probe.reachable is True
    assert probe.healthy is False
    assert probe.locked is False
    assert "JSON" in str(probe.detail)


def test_transport_failure_is_offline_and_actionable() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    _host, port = sock.getsockname()
    sock.close()

    probe = KestrelServerClient(
        f"http://127.0.0.1:{port}",
        request_timeout_seconds=0.2,
    ).probe()

    assert probe.reachable is False
    assert probe.healthy is False
    assert probe.locked is False
    assert "start" in str(probe.detail).lower()
