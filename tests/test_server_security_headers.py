import asyncio
import json
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import nested_memvid_agent.server as server_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.lan_discovery_models import NetworkInterface
from nested_memvid_agent.routing.lan_records import LanScanEvent, LanScanRecord
from nested_memvid_agent.server import create_app


class _BoundedAsgiResponse:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        start = next(item for item in messages if item["type"] == "http.response.start")
        self.status_code = int(start["status"])
        self.headers = {
            bytes(name).decode("latin-1").lower(): bytes(value).decode("latin-1")
            for name, value in start["headers"]  # type: ignore[union-attr]
        }
        self.content = b"".join(
            item.get("body", b"")  # type: ignore[arg-type]
            for item in messages
            if item["type"] == "http.response.body"
        )

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> object:
        return json.loads(self.content)


def _bounded_asgi_get(
    app,
    path: str,
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> _BoundedAsgiResponse:
    async def request() -> _BoundedAsgiResponse:
        request_delivered = False
        never_disconnect = asyncio.Event()
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await never_disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            messages.append(dict(message))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver"), *headers],
            "client": ("127.0.0.1", 41234),
            "server": ("testserver", 80),
            "state": {},
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=2.0)
        return _BoundedAsgiResponse(messages)

    return asyncio.run(request())


def _assert_security_headers(response) -> None:
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data: https:; manifest-src 'self'; object-src 'none'; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; worker-src 'self' blob:"
    )
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["permissions-policy"] == (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    )
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in response.headers


def _isolated_config(root: Path) -> AgentConfig:
    workspace = root / "workspace"
    workspace.mkdir()
    return AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=root / "memory",
        log_dir=root / "logs",
        state_path=root / "state" / "agent.db",
        secret_store_path=root / "secrets" / "vault.json",
        workspace=workspace,
        skills_dir=root / "skills",
        plugins_dir=root / "plugins",
        mcp_config_path=root / "config" / "mcp.json",
        channel_config_path=root / "config" / "channels.json",
        worker_worktree_dir=root / "worktrees",
        require_api_auth=True,
        api_auth_token_env="KESTREL_SECURITY_HEADER_TEST_TOKEN",
    )


def test_security_headers_cover_spa_and_early_auth_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KESTREL_SECURITY_HEADER_TEST_TOKEN", "local-test-token")
    web_dist = tmp_path / "web-dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<!doctype html><title>Kestrel</title>", encoding="utf-8")
    monkeypatch.setattr(server_module, "_resolve_web_dist", lambda: web_dist)

    with TestClient(create_app(_isolated_config(tmp_path))) as client:
        page = client.get("/")
        unauthorized = client.get("/api/health")

    assert page.status_code == 200
    assert unauthorized.status_code == 401
    for response in (page, unauthorized):
        _assert_security_headers(response)


def test_security_headers_cover_lan_fixed_errors_and_terminal_sse(
    tmp_path,
    monkeypatch,
) -> None:
    token = "lan-security-header-token"
    monkeypatch.setenv("KESTREL_SECURITY_HEADER_TEST_TOKEN", token)
    scan_id = "lan_" + "a" * 32
    record = LanScanRecord(
        scan_id=scan_id,
        status="completed",
        revision=3,
        owner_principal="owner:local-runtime:v1",
        confirmed_interface_id="sha256:" + "b" * 64,
        network="192.168.90.0/30",
        limits={"known_model_service_ports": [1234, 8000, 8080, 11434]},
        limits_digest="sha256:" + "c" * 64,
        preview_digest="sha256:" + "d" * 64,
        created_at="2026-08-01T12:00:00Z",
        updated_at="2026-08-01T12:00:01Z",
        started_at="2026-08-01T12:00:00Z",
        finished_at="2026-08-01T12:00:01Z",
        terminal_reason="scan_complete",
        candidate_count=0,
        error_count=0,
        timeout_count=0,
        terminal_receipt={"private": "never-project"},
        terminal_receipt_digest="sha256:" + "e" * 64,
    )
    event = LanScanEvent(
        scan_id=scan_id,
        sequence=1,
        event_type="scan_completed",
        payload={
            "schema": "kestrel.lan.scan-terminal.v2",
            "status": "completed",
            "terminal_reason": "scan_complete",
            "cancel_reason": None,
        },
        created_at="2026-08-01T12:00:01Z",
    )

    class TerminalManager:
        def __init__(self, *, ledger) -> None:
            del ledger
            self.executor = None

        def start_lifecycle(self, executor) -> list[LanScanRecord]:
            self.executor = executor
            return []

        def shutdown(self, *, timeout_seconds: float) -> bool:
            del timeout_seconds
            if self.executor is not None:
                self.executor.shutdown(wait=True, cancel_futures=False)
                self.executor = None
            return True

        def get(self, requested_scan_id: str) -> LanScanRecord | None:
            return record if requested_scan_id == scan_id else None

        def observation_page(self, requested_scan_id: str, *, limit: int):
            assert limit == 200
            if requested_scan_id != scan_id:
                return None
            return SimpleNamespace(
                scan=record,
                observations=(),
                total_count=0,
                truncated=False,
            )

        def interfaces(self) -> tuple[NetworkInterface, ...]:
            return (
                NetworkInterface.from_addresses(
                    os_identity="darwin:security-header-fixture",
                    display_name="Security header fixture",
                    addresses=("192.168.90.1/30",),
                ),
            )

        def manual_preview(self, interface_id: str, host: str, port: int) -> object:
            del host
            task6 = import_module("nested_memvid_agent.lan_scan_manager")
            routes = import_module("nested_memvid_agent.server_lan_discovery_routes")
            now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
            return SimpleNamespace(
                schema=routes.LAN_MANUAL_PREVIEW_SCHEMA,
                interface_id=interface_id,
                port=port,
                resolved_addresses=("192.168.90.2",),
                preview_digest="sha256:" + "d" * 64,
                issued_at=now,
                expires_at=now + timedelta(seconds=30),
                server_version=task6.LAN_SERVER_VERSION,
                contract_version=task6.LAN_MANUAL_PREVIEW_CONTRACT_VERSION,
                requires_confirmation=True,
            )

        def confirm_manual(self, *args: object, **kwargs: object) -> LanScanRecord:
            del args, kwargs
            task6 = import_module("nested_memvid_agent.lan_scan_manager")
            raise task6.LanManualPreviewConflict("raw-secret-address-192.168.90.2")

        def events(
            self,
            requested_scan_id: str,
            *,
            after_sequence: int = 0,
            limit: int = 500,
        ) -> list[LanScanEvent]:
            del limit
            if requested_scan_id != scan_id or event.sequence <= after_sequence:
                return []
            return [event]

    monkeypatch.setattr(server_module, "LanScanManager", TerminalManager)
    headers = {"X-Kestrel-API-Key": token}
    with TestClient(create_app(_isolated_config(tmp_path))) as client:
        json_success = client.get(
            "/api/routing/lan/interfaces",
            headers=headers,
        )
        manual_success = client.post(
            "/api/routing/lan/manual-probe",
            headers=headers,
            json={
                "mode": "preview",
                "interface_id": "sha256:" + "b" * 64,
                "host": "model-box.local",
                "port": 5001,
            },
        )
        manual_fixed_error = client.post(
            "/api/routing/lan/manual-probe",
            headers=headers,
            json={
                "mode": "confirm",
                "expected_revision": 0,
                "preview_digest": "sha256:" + "d" * 64,
                "selected_address": "192.168.90.2",
                "confirmed": True,
                "privacy_acknowledged": True,
            },
        )
        fixed_error = client.get(
            "/api/routing/lan/scans/lan_" + "f" * 32,
            headers=headers,
        )
        sse = _bounded_asgi_get(
            client.app,
            f"/api/routing/lan/scans/{scan_id}/events",
            headers=((b"x-kestrel-api-key", token.encode("ascii")),),
        )

    assert json_success.status_code == 200
    assert json_success.headers["content-type"].startswith("application/json")
    assert manual_success.status_code == 200
    assert manual_success.headers["content-type"].startswith("application/json")
    assert manual_fixed_error.status_code == 409
    assert manual_fixed_error.json() == {"detail": {"code": "lan_manual_preview_conflict"}}
    assert "raw-secret" not in manual_fixed_error.text
    assert fixed_error.status_code == 404
    assert fixed_error.json() == {"detail": {"code": "lan_scan_not_found"}}
    assert sse.status_code == 200
    assert sse.headers["content-type"].startswith("text/event-stream")
    assert sse.headers["cache-control"] == "no-store, no-transform"
    assert sse.headers["x-accel-buffering"] == "no"
    assert "event: scan_completed\n" in sse.text
    for response in (
        json_success,
        manual_success,
        manual_fixed_error,
        fixed_error,
        sse,
    ):
        _assert_security_headers(response)
