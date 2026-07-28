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
