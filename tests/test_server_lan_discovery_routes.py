from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.lan_discovery_models import LanScanLimits, NetworkInterface
from nested_memvid_agent.lan_discovery_scope import preview_private_scope
from nested_memvid_agent.lan_mdns import MdnsAvailability
from nested_memvid_agent.lan_scan_manager import (
    LAN_PREVIEW_CONTRACT_VERSION,
    LAN_SERVER_VERSION,
    LanPreviewAuthorization,
    LanPreviewAuthorizationError,
    LanScanAdmissionConflict,
)
from nested_memvid_agent.routing.lan_records import (
    LanObservationRecord,
    LanScanEvent,
    LanScanRecord,
    LanScanRevisionConflict,
    LanScanTransitionError,
)

FIXED_OWNER = "owner:local-runtime:v1"
SCAN_ID = "lan_" + "a" * 32
PREVIEW_DIGEST = "sha256:" + "b" * 64
ALLOWED_EVENT_TYPES = (
    "scan_started",
    "scan_progress",
    "scan_cancel_requested",
    "scan_completed",
    "scan_cancelled",
    "scan_failed",
    "scan_interrupted",
)
INTERFACE = NetworkInterface.from_addresses(
    os_identity="darwin:en90",
    display_name="Deterministic LAN fixture",
    addresses=("192.168.90.1/29",),
)


def _task7_routes() -> Any:
    return import_module("nested_memvid_agent.server_lan_discovery_routes")


def _task6() -> Any:
    return import_module("nested_memvid_agent.lan_scan_manager")


def _manual_limits(port: int = 5001) -> dict[str, object]:
    return {
        "mode": "manual",
        "exact_port": port,
        "max_active_hosts": 1,
        "max_scan_concurrency": 1,
        "tcp_connect_timeout_seconds": 0.75,
        "http_probe_timeout_seconds": 2.0,
        "total_scan_deadline_seconds": 45.0,
        "max_probe_response_bytes": 256 * 1024,
        "max_discovered_models": 8,
        "mdns_enabled": False,
    }


def _scan(
    *,
    status: str = "draft",
    revision: int = 1,
    scan_id: str = SCAN_ID,
    owner_principal: str = FIXED_OWNER,
) -> LanScanRecord:
    terminal = status in {"cancelled", "completed", "failed", "interrupted"}
    terminal_reason = {
        "completed": "scan_complete",
        "cancelled": "owner_cancelled",
        "failed": "worker_error",
        "interrupted": "startup_interrupted",
    }.get(status)
    return LanScanRecord(
        scan_id=scan_id,
        status=status,  # type: ignore[arg-type]
        revision=revision,
        owner_principal=owner_principal,
        confirmed_interface_id=INTERFACE.interface_id,
        network="192.168.90.0/30",
        limits={"known_model_service_ports": [1234, 8000, 8080, 11434]},
        limits_digest="sha256:" + "c" * 64,
        preview_digest=PREVIEW_DIGEST,
        created_at="2026-08-01T12:00:00Z",
        updated_at="2026-08-01T12:00:01Z",
        started_at="2026-08-01T12:00:00Z" if status != "draft" else None,
        finished_at="2026-08-01T12:00:01Z" if terminal else None,
        cancel_reason="owner_cancelled" if status in {"cancelling", "cancelled"} else None,
        terminal_reason=terminal_reason,
        candidate_count=1 if terminal else None,
        error_count=0 if terminal else None,
        timeout_count=0 if terminal else None,
        terminal_receipt=(
            {"raw_private_receipt": "must-never-cross-the-route"} if terminal else None
        ),
        terminal_receipt_digest=("sha256:" + "d" * 64 if terminal else None),
    )


def _manual_scan(selected_address: str = "192.168.90.2") -> LanScanRecord:
    return replace(
        _scan(status="running", revision=2),
        network=f"{selected_address}/32",
        limits=_manual_limits(5001),
    )


def _observation(index: int = 1, *, scan_id: str = SCAN_ID) -> LanObservationRecord:
    return LanObservationRecord(
        scan_id=scan_id,
        endpoint_id="sha256:" + f"{index:064x}",
        source="active",
        interface_id=INTERFACE.interface_id,
        address="192.168.90.2",
        port=11434,
        api_shape="ollama",
        tls_enabled=False,
        certificate_sha256=None,
        catalog_digest="sha256:" + "e" * 64,
        capability_digest="sha256:" + "f" * 64,
        public_payload={"model_count": index},
        freshness_timestamp="2026-08-01T12:00:01Z",
        error_category=None,
        created_at="2026-08-01T12:00:01Z",
    )


def _private_event_payload(event_type: str) -> dict[str, object]:
    if event_type == "scan_started":
        return {
            "schema": "kestrel.lan.scan-preview.v1",
            "owner_principal": FIXED_OWNER,
            "interface_id": INTERFACE.interface_id,
            "network": "192.168.90.0/30",
            "limits": asdict(LanScanLimits()),
            "active_host_count": 2,
            "passive_or_manual_only": False,
            "port_count": 8,
            "mdns_status": "available",
            "server_version": LAN_SERVER_VERSION,
            "contract_version": LAN_PREVIEW_CONTRACT_VERSION,
            "preview_digest": PREVIEW_DIGEST,
            "expires_at": "2026-08-01T12:00:30Z",
        }
    if event_type == "scan_progress":
        return {
            "schema": "kestrel.lan.scan-progress.v1",
            "planned_count": 2,
            "admitted_count": 2,
            "completed_count": 1,
            "persisted_observation_count": 1,
            "error_category_counts": {},
            "timeout_count": 0,
            "mdns_status": "available",
        }
    if event_type == "scan_cancel_requested":
        return {"reason": "owner_cancelled"}
    statuses = {
        "scan_completed": ("completed", "scan_complete", None),
        "scan_cancelled": ("cancelled", "owner_cancelled", "owner_cancelled"),
        "scan_failed": ("failed", "worker_error", None),
        "scan_interrupted": ("interrupted", "startup_interrupted", None),
    }
    status, terminal_reason, cancel_reason = statuses[event_type]
    return {
        "schema": "kestrel.lan.scan-terminal.v2",
        "status": status,
        "terminal_reason": terminal_reason,
        "cancel_reason": cancel_reason,
    }


def _public_event_payload(event_type: str) -> dict[str, object]:
    payload = _private_event_payload(event_type)
    if event_type == "scan_started":
        return {
            key: payload[key]
            for key in (
                "interface_id",
                "network",
                "limits",
                "active_host_count",
                "passive_or_manual_only",
                "port_count",
                "mdns_status",
                "server_version",
                "contract_version",
                "preview_digest",
                "expires_at",
            )
        }
    if event_type in {
        "scan_progress",
        "scan_completed",
        "scan_cancelled",
        "scan_failed",
        "scan_interrupted",
    }:
        return {key: value for key, value in payload.items() if key != "schema"}
    return payload


def _manual_private_event_payload(port: int = 5001) -> dict[str, object]:
    return {
        "schema": "kestrel.lan.scan-preview.manual.v1",
        "mode": "manual",
        "endpoint_kind": "manual",
        "observation_source": "manual",
        "owner_principal": FIXED_OWNER,
        "interface_id": INTERFACE.interface_id,
        "network": "192.168.90.2/32",
        "limits": _manual_limits(port),
        "active_host_count": 1,
        "passive_or_manual_only": True,
        "port_count": 1,
        "exact_port": port,
        "mdns_status": "unavailable",
        "server_version": LAN_SERVER_VERSION,
        "contract_version": _task6().LAN_MANUAL_PREVIEW_CONTRACT_VERSION,
        "preview_digest": PREVIEW_DIGEST,
        "expires_at": "2026-08-01T12:00:30Z",
        "confirmed": True,
        "privacy_acknowledged": True,
    }


def _manual_public_event_payload(port: int = 5001) -> dict[str, object]:
    private = _manual_private_event_payload(port)
    return {
        key: private[key]
        for key in (
            "mode",
            "endpoint_kind",
            "observation_source",
            "interface_id",
            "network",
            "limits",
            "active_host_count",
            "passive_or_manual_only",
            "port_count",
            "exact_port",
            "mdns_status",
            "server_version",
            "contract_version",
            "preview_digest",
            "expires_at",
            "confirmed",
            "privacy_acknowledged",
        )
    }


def _event(
    sequence: int,
    event_type: str = "scan_completed",
    *,
    scan_id: str = SCAN_ID,
    payload: dict[str, object] | None = None,
    created_at: str = "2026-08-01T12:00:01Z",
) -> LanScanEvent:
    return LanScanEvent(
        scan_id=scan_id,
        sequence=sequence,
        event_type=event_type,
        payload=_private_event_payload(event_type) if payload is None else payload,
        created_at=created_at,
    )


def _expected_public_event(event: LanScanEvent) -> dict[str, object]:
    return json.loads(
        json.dumps(
            {
                "scan_id": event.scan_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "payload": _public_event_payload(event.event_type),
                "created_at": event.created_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


class RouteManager:
    def __init__(self, *, terminal: bool = False) -> None:
        now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        preview = preview_private_scope(
            INTERFACE.interface_id,
            "192.168.90.0/30",
            interfaces=(INTERFACE,),
        )
        self.authorization = LanPreviewAuthorization(
            owner_principal=FIXED_OWNER,
            preview=preview,
            preview_digest=PREVIEW_DIGEST,
            server_version=LAN_SERVER_VERSION,
            contract_version=LAN_PREVIEW_CONTRACT_VERSION,
            mdns_availability=MdnsAvailability.AVAILABLE,
            issued_at=now,
            expires_at=now + timedelta(seconds=30),
        )
        self.record = _scan(status="completed", revision=4) if terminal else _scan()
        self.calls: list[tuple[object, ...]] = []
        self.durable_events = [_event(1, "scan_started"), _event(2)] if terminal else []

    def interfaces(self) -> tuple[NetworkInterface, ...]:
        self.calls.append(("interfaces",))
        return (INTERFACE,)

    def preview(self, interface_id: str, network: str) -> LanPreviewAuthorization:
        self.calls.append(("preview", interface_id, network))
        return self.authorization

    def manual_preview(self, interface_id: str, host: str, port: int) -> object:
        self.calls.append(("manual-preview", interface_id, host, port))
        now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        return SimpleNamespace(
            schema=_task7_routes().LAN_MANUAL_PREVIEW_SCHEMA,
            interface_id=interface_id,
            port=port,
            resolved_addresses=("192.168.90.2", "192.168.90.3"),
            preview_digest=PREVIEW_DIGEST,
            issued_at=now,
            expires_at=now + timedelta(seconds=30),
            server_version=LAN_SERVER_VERSION,
            contract_version=_task6().LAN_MANUAL_PREVIEW_CONTRACT_VERSION,
            requires_confirmation=True,
            host="raw-private-manual-host.local",
            host_input_digest="sha256:" + "7" * 64,
            owner_principal=FIXED_OWNER,
            interface=INTERFACE,
            inventory_authority=(
                {
                    "interface_id": INTERFACE.interface_id,
                    "os_identity": INTERFACE.os_identity,
                    "addresses": list(INTERFACE.addresses),
                },
            ),
            os_identity=INTERFACE.os_identity,
        )

    def confirm_manual(
        self,
        preview_digest: str,
        selected_address: str,
        *,
        expected_revision: int,
        confirmed: bool,
        privacy_acknowledged: bool,
    ) -> LanScanRecord:
        self.calls.append(
            (
                "manual-confirm",
                preview_digest,
                selected_address,
                expected_revision,
                confirmed,
                privacy_acknowledged,
            )
        )
        if selected_address not in {"192.168.90.2", "192.168.90.3"}:
            raise _task6().LanManualPreviewConflict("raw-secret-nonmember-address")
        self.record = _manual_scan(selected_address)
        return self.record

    def create_draft_for_preview(
        self,
        preview_digest: str,
        *,
        expected_revision: int,
    ) -> LanScanRecord:
        self.calls.append(("create", preview_digest, expected_revision))
        self.record = _scan()
        return self.record

    def start_for_preview(
        self,
        scan_id: str,
        *,
        expected_revision: int,
        preview_digest: str,
    ) -> LanScanRecord:
        self.calls.append(("start", scan_id, expected_revision, preview_digest))
        started = _scan(status="running", revision=2)
        self.record = _scan(status="completed", revision=4)
        return started

    def list(self, *, limit: int) -> list[LanScanRecord]:
        self.calls.append(("list", limit))
        return [self.record]

    def get(self, scan_id: str) -> LanScanRecord | None:
        self.calls.append(("get", scan_id))
        return self.record if scan_id == self.record.scan_id else None

    def observation_page(self, scan_id: str, *, limit: int) -> object | None:
        self.calls.append(("observations", scan_id, limit))
        if scan_id != self.record.scan_id:
            return None
        return SimpleNamespace(
            scan=self.record,
            observations=(_observation(1), _observation(2)),
            total_count=3,
            truncated=True,
        )

    def cancel(self, scan_id: str, *, expected_revision: int) -> LanScanRecord:
        self.calls.append(("cancel", scan_id, expected_revision))
        self.record = _scan(status="cancelling", revision=expected_revision + 1)
        return self.record

    def events(
        self,
        scan_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[LanScanEvent]:
        self.calls.append(("events", scan_id, after_sequence, limit))
        if scan_id != self.record.scan_id:
            return []
        return [item for item in self.durable_events if item.sequence > after_sequence][:limit]


def _route_app(manager: Any, *, stream_runtime: object | None = None) -> Any:
    fastapi = pytest.importorskip("fastapi")
    responses = pytest.importorskip("starlette.responses")
    app = fastapi.FastAPI()
    stream_options = {} if stream_runtime is None else {"stream_runtime": stream_runtime}
    _task7_routes().register_lan_discovery_routes(
        app,
        scan_manager=manager,
        http_exception=fastapi.HTTPException,
        streaming_response=responses.StreamingResponse,
        **stream_options,
    )
    return app


def _route_client(manager: Any) -> Any:
    testclient = pytest.importorskip("fastapi.testclient")
    return testclient.TestClient(_route_app(manager))


class _AsgiConnection:
    def __init__(
        self,
        app: Any,
        path: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body_chunks: tuple[bytes, ...] = (),
    ) -> None:
        self._app = app
        self._path, _separator, query = path.partition("?")
        self._query_string = query.encode("ascii")
        self._request_headers = headers
        self._disconnect = asyncio.Event()
        self._request_messages = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(body_chunks) - 1,
            }
            for index, chunk in enumerate(body_chunks or (b"",))
        ]
        self._started = asyncio.Event()
        self._body_sent = asyncio.Event()
        self.messages: list[dict[str, object]] = []
        self.task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, object]:
        if self._request_messages:
            return self._request_messages.pop(0)
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, object]) -> None:
        self.messages.append(dict(message))
        if message["type"] == "http.response.start":
            self._started.set()
        elif message["type"] == "http.response.body" and message.get("body"):
            self._body_sent.set()

    async def start(self) -> int:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self._path,
            "raw_path": self._path.encode("ascii"),
            "query_string": self._query_string,
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"accept", b"text/event-stream"),
                *self._request_headers,
            ],
            "client": ("127.0.0.1", 41234),
            "server": ("testserver", 80),
            "state": {},
        }
        self.task = asyncio.create_task(
            self._app(scope, self._receive, self._send),
            name=f"test-lan-sse:{self._path}",
        )
        try:
            await asyncio.wait_for(self._started.wait(), timeout=2.0)
        except BaseException:
            self.task.cancel()
            await asyncio.wait_for(
                asyncio.gather(self.task, return_exceptions=True),
                timeout=2.0,
            )
            raise
        start_message = next(
            item for item in self.messages if item["type"] == "http.response.start"
        )
        return int(start_message["status"])

    async def disconnect(self) -> None:
        self._disconnect.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=2.0)

    async def cancel(self) -> None:
        if self.task is None:
            raise AssertionError("connection was not started")
        self.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(self.task, timeout=2.0)

    async def wait(self) -> None:
        if self.task is None:
            raise AssertionError("connection was not started")
        await asyncio.wait_for(self.task, timeout=2.0)

    async def wait_allowing_error(self) -> BaseException | None:
        if self.task is None:
            raise AssertionError("connection was not started")
        try:
            await asyncio.wait_for(self.task, timeout=2.0)
        except TimeoutError:
            raise
        except BaseException as exc:
            return exc
        return None

    async def wait_for_body(self) -> None:
        await asyncio.wait_for(self._body_sent.wait(), timeout=2.0)

    def json_body(self) -> object:
        body = b"".join(
            item.get("body", b"")  # type: ignore[arg-type]
            for item in self.messages
            if item["type"] == "http.response.body"
        )
        return json.loads(body)

    def body(self) -> bytes:
        return b"".join(
            item.get("body", b"")  # type: ignore[arg-type]
            for item in self.messages
            if item["type"] == "http.response.body"
        )

    def response_headers(self) -> dict[str, str]:
        start = next(item for item in self.messages if item["type"] == "http.response.start")
        return {
            bytes(name).decode("latin-1").lower(): bytes(value).decode("latin-1")
            for name, value in start["headers"]  # type: ignore[union-attr]
        }

    def text(self) -> str:
        return self.body().decode("utf-8")


def _bounded_asgi_get(
    manager: Any,
    path: str,
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> tuple[int, _AsgiConnection]:
    async def collect() -> tuple[int, _AsgiConnection]:
        connection = _AsgiConnection(_route_app(manager), path, headers=headers)
        status = await connection.start()
        await connection.wait()
        return status, connection

    return _run_bounded(collect())


def _assert_no_private_authority(payload: object) -> None:
    encoded = json.dumps(payload, sort_keys=True)
    assert "owner_principal" not in encoded
    assert FIXED_OWNER not in encoded
    assert "os_identity" not in encoded
    assert "darwin:en90" not in encoded
    assert 'terminal_receipt"' not in encoded
    assert "must-never-cross-the-route" not in encoded
    assert "raw-private-manual-host.local" not in encoded
    assert "host_input_digest" not in encoded
    assert "inventory_authority" not in encoded


def test_automatic_routes_project_only_bounded_public_manager_results() -> None:
    manager = RouteManager()
    with _route_client(manager) as client:
        interfaces = client.get("/api/routing/lan/interfaces")
        preview = client.post(
            "/api/routing/lan/preview",
            json={"interface_id": INTERFACE.interface_id, "network": "192.168.90.0/30"},
        )
        created = client.post(
            "/api/routing/lan/scans",
            json={
                "preview_digest": PREVIEW_DIGEST,
                "expected_revision": 0,
                "confirmed": True,
            },
        )
        started = client.post(
            f"/api/routing/lan/scans/{SCAN_ID}/start",
            json={
                "expected_revision": 1,
                "preview_digest": PREVIEW_DIGEST,
                "confirmed": True,
            },
        )
        scans = client.get("/api/routing/lan/scans")
        detail = client.get(f"/api/routing/lan/scans/{SCAN_ID}")
        cancelled = client.post(
            f"/api/routing/lan/scans/{SCAN_ID}/cancel",
            json={"expected_revision": 4},
        )

    assert interfaces.status_code == 200
    assert interfaces.json() == [
        {
            "interface_id": INTERFACE.interface_id,
            "display_name": INTERFACE.display_name,
            "addresses": list(INTERFACE.addresses),
        }
    ]
    preview_payload = preview.json()
    assert preview.status_code == 200
    assert preview_payload["interface_id"] == INTERFACE.interface_id
    assert preview_payload["network"] == "192.168.90.0/30"
    assert preview_payload["active_host_count"] == 2
    assert preview_payload["port_count"] == 8
    assert preview_payload["passive_or_manual_only"] is False
    assert preview_payload["mdns_status"] == "available"
    assert preview_payload["preview_digest"] == PREVIEW_DIGEST
    assert preview_payload["issued_at"] == "2026-08-01T12:00:00Z"
    assert preview_payload["expires_at"] == "2026-08-01T12:00:30Z"
    assert preview_payload["contract_version"] == LAN_PREVIEW_CONTRACT_VERSION
    assert preview_payload["server_version"] == LAN_SERVER_VERSION
    assert preview_payload["limits"]["known_model_service_ports"] == [1234, 8000, 8080, 11434]
    assert "active_hosts" not in preview_payload
    assert "port_matrix" not in preview_payload

    assert created.status_code == 201
    assert started.status_code == 202
    assert scans.status_code == 200
    assert isinstance(scans.json(), list) and len(scans.json()) == 1
    assert "observations" not in scans.json()[0]
    assert detail.status_code == 200
    assert detail.json()["terminal_receipt_digest"] == "sha256:" + "d" * 64
    assert detail.json()["candidate_count"] == 1
    assert detail.json()["error_count"] == 0
    assert detail.json()["timeout_count"] == 0
    assert detail.json()["observation_total_count"] == 3
    assert detail.json()["observations_truncated"] is True
    assert len(detail.json()["observations"]) == 2
    assert cancelled.status_code == 202
    for payload in (
        interfaces.json(),
        preview_payload,
        created.json(),
        started.json(),
        scans.json(),
        detail.json(),
        cancelled.json(),
    ):
        _assert_no_private_authority(payload)

    assert ("create", PREVIEW_DIGEST, 0) in manager.calls
    assert ("start", SCAN_ID, 1, PREVIEW_DIGEST) in manager.calls
    assert ("list", 100) in manager.calls
    assert ("get", SCAN_ID) not in manager.calls
    assert ("observations", SCAN_ID, 200) in manager.calls
    assert ("cancel", SCAN_ID, 4) in manager.calls


def test_manual_probe_preview_and_confirm_have_exact_safe_public_shapes() -> None:
    manager = RouteManager()
    with _route_client(manager) as client:
        preview = client.post(
            "/api/routing/lan/manual-probe",
            json={
                "mode": "preview",
                "interface_id": INTERFACE.interface_id,
                "host": "model-box.local",
                "port": 5001,
            },
        )
        nonmember = client.post(
            "/api/routing/lan/manual-probe",
            json={
                "mode": "confirm",
                "expected_revision": 0,
                "preview_digest": PREVIEW_DIGEST,
                "selected_address": "192.168.90.4",
                "confirmed": True,
                "privacy_acknowledged": True,
            },
        )
        confirmed = client.post(
            "/api/routing/lan/manual-probe",
            json={
                "mode": "confirm",
                "expected_revision": 0,
                "preview_digest": PREVIEW_DIGEST,
                "selected_address": "192.168.90.3",
                "confirmed": True,
                "privacy_acknowledged": True,
            },
        )

    assert preview.status_code == 200
    assert _task7_routes().LAN_MANUAL_PREVIEW_SCHEMA == ("kestrel.lan.manual-preview.v1")
    assert preview.json() == {
        "schema": _task7_routes().LAN_MANUAL_PREVIEW_SCHEMA,
        "interface_id": INTERFACE.interface_id,
        "port": 5001,
        "resolved_addresses": ["192.168.90.2", "192.168.90.3"],
        "preview_digest": PREVIEW_DIGEST,
        "issued_at": "2026-08-01T12:00:00Z",
        "expires_at": "2026-08-01T12:00:30Z",
        "server_version": LAN_SERVER_VERSION,
        "contract_version": _task6().LAN_MANUAL_PREVIEW_CONTRACT_VERSION,
        "requires_confirmation": True,
    }
    assert confirmed.status_code == 202
    assert nonmember.status_code == 409
    assert nonmember.json() == {"detail": {"code": "lan_manual_preview_conflict"}}
    assert "192.168.90.4" not in nonmember.text
    assert confirmed.json()["status"] == "running"
    assert confirmed.json()["revision"] == 2
    assert confirmed.json()["network"] == "192.168.90.3/32"
    assert confirmed.json()["limits"] == _manual_limits(5001)
    assert "observations" not in confirmed.json()
    for payload in (preview.json(), nonmember.json(), confirmed.json()):
        _assert_no_private_authority(payload)
        encoded = json.dumps(payload, sort_keys=True)
        assert "model-box.local" not in encoded
        assert "192.168.90.4" not in encoded
        assert '"probe":' not in encoded
    assert manager.calls == [
        ("manual-preview", INTERFACE.interface_id, "model-box.local", 5001),
        (
            "manual-confirm",
            PREVIEW_DIGEST,
            "192.168.90.4",
            0,
            True,
            True,
        ),
        (
            "manual-confirm",
            PREVIEW_DIGEST,
            "192.168.90.3",
            0,
            True,
            True,
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"mode": "unknown"},
        {
            "mode": "preview",
            "interface_id": INTERFACE.interface_id,
            "host": "model-box.local",
            "port": 5001,
            "selected_address": "192.168.90.2",
        },
        {
            "mode": "preview",
            "interface_id": INTERFACE.interface_id,
            "host": "model-box.local",
            "port": 5001,
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": True,
            "privacy_acknowledged": True,
        },
        {
            "mode": "preview",
            "interface_id": INTERFACE.interface_id,
            "host": "model-box.local",
            "port": 5001,
            "owner_principal": FIXED_OWNER,
        },
        {
            "mode": "preview",
            "interface_id": INTERFACE.interface_id,
            "host": "model-box.local",
            "port": 5001,
            "limits": _manual_limits(5001),
        },
        {
            "mode": "preview",
            "interface_id": INTERFACE.interface_id,
            "host": "model-box.local",
            "port": 5001,
            "unexpected": True,
        },
        {
            "mode": "confirm",
            "interface_id": INTERFACE.interface_id,
            "host": "model-box.local",
            "port": 5001,
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": True,
            "privacy_acknowledged": True,
            "interface_id": INTERFACE.interface_id,
            "host": "model-box.local",
            "port": 5001,
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": True,
            "privacy_acknowledged": True,
            "network": "192.168.90.2/32",
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": True,
            "privacy_acknowledged": True,
            "limits": _manual_limits(5001),
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": True,
            "privacy_acknowledged": True,
            "unexpected": True,
        },
        *(
            {
                "mode": "preview",
                "interface_id": INTERFACE.interface_id,
                "host": "model-box.local",
                "port": port,
            }
            for port in (False, True, 0, 65536, 5001.0, "5001")
        ),
        *(
            {
                "mode": "confirm",
                "expected_revision": revision,
                "preview_digest": PREVIEW_DIGEST,
                "selected_address": "192.168.90.2",
                "confirmed": True,
                "privacy_acknowledged": True,
            }
            for revision in (False, True, -1, 1, 0.0, "0")
        ),
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": False,
            "privacy_acknowledged": True,
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": 1,
            "privacy_acknowledged": True,
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": True,
            "privacy_acknowledged": False,
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": "192.168.90.2",
            "confirmed": True,
            "privacy_acknowledged": 1,
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": "b" * 64,
            "selected_address": "192.168.90.2",
            "confirmed": True,
            "privacy_acknowledged": True,
        },
        {
            "mode": "confirm",
            "expected_revision": 0,
            "preview_digest": PREVIEW_DIGEST,
            "selected_address": 1234,
            "confirmed": True,
            "privacy_acknowledged": True,
        },
    ),
)
def test_manual_probe_discriminator_and_authority_fields_are_strict(
    payload: dict[str, object],
) -> None:
    manager = RouteManager()
    with _route_client(manager) as client:
        response = client.post("/api/routing/lan/manual-probe", json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "lan_request_invalid"}}
    assert manager.calls == []


def test_manual_probe_maps_host_conflict_active_slot_and_unavailable_errors_without_echo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    class HostRejected(RouteManager):
        def manual_preview(self, interface_id: str, host: str, port: int) -> object:
            del interface_id, host, port
            raise ValueError("raw-secret-host-model-box.local")

    class PreviewConflict(RouteManager):
        def confirm_manual(self, *args: object, **kwargs: object) -> LanScanRecord:
            del args, kwargs
            raise _task6().LanManualPreviewConflict("raw-secret-address-192.168.90.2")

    class ActiveConflict(RouteManager):
        def confirm_manual(self, *args: object, **kwargs: object) -> LanScanRecord:
            del args, kwargs
            raise LanScanAdmissionConflict("raw-secret-active-scan")

    class ResolverUnavailable(RouteManager):
        def manual_preview(self, interface_id: str, host: str, port: int) -> object:
            del interface_id, host, port
            raise RuntimeError("raw-secret-resolver-failure")

    preview_body = {
        "mode": "preview",
        "interface_id": INTERFACE.interface_id,
        "host": "model-box.local",
        "port": 5001,
    }
    confirm_body = {
        "mode": "confirm",
        "expected_revision": 0,
        "preview_digest": PREVIEW_DIGEST,
        "selected_address": "192.168.90.2",
        "confirmed": True,
        "privacy_acknowledged": True,
    }
    cases = (
        (HostRejected(), preview_body, 400, "lan_manual_host_rejected"),
        (PreviewConflict(), confirm_body, 409, "lan_manual_preview_conflict"),
        (ActiveConflict(), confirm_body, 409, "lan_scan_active_conflict"),
        (ResolverUnavailable(), preview_body, 503, "lan_scan_unavailable"),
    )
    for manager, body, expected_status, expected_code in cases:
        with _route_client(manager) as client:
            response = client.post("/api/routing/lan/manual-probe", json=body)
        assert response.status_code == expected_status
        assert response.json() == {"detail": {"code": expected_code}}
        assert "raw-secret" not in response.text
        assert "model-box.local" not in response.text
        assert "192.168.90.2" not in response.text

    assert "raw-secret" not in caplog.text
    assert "model-box.local" not in caplog.text
    assert "192.168.90.2" not in caplog.text


@pytest.mark.parametrize(
    "host",
    (
        "8.8.8.8",
        "127.0.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "192.0.2.1",
        "192.168.91.2",
    ),
    ids=(
        "public",
        "loopback",
        "multicast",
        "unspecified",
        "reserved-documentation",
        "private-out-of-interface",
    ),
)
def test_manual_probe_maps_ineligible_literal_rejection_to_fixed_safe_400(
    host: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    class LiteralRejected(RouteManager):
        def manual_preview(
            self,
            interface_id: str,
            requested_host: str,
            port: int,
        ) -> object:
            self.calls.append(("manual-preview", interface_id, requested_host, port))
            raise ValueError(f"raw-secret-ineligible-literal:{requested_host}")

    manager = LiteralRejected()
    with _route_client(manager) as client:
        response = client.post(
            "/api/routing/lan/manual-probe",
            json={
                "mode": "preview",
                "interface_id": INTERFACE.interface_id,
                "host": host,
                "port": 5001,
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "lan_manual_host_rejected"}}
    assert host not in response.text
    assert "raw-secret" not in response.text
    assert host not in caplog.text
    assert "raw-secret" not in caplog.text
    assert manager.calls == [
        ("manual-preview", INTERFACE.interface_id, host, 5001),
    ]


def test_route_projection_enforces_100_scan_and_200_observation_caps_on_oversupply() -> None:
    class OversupplyingManager(RouteManager):
        def __init__(self) -> None:
            super().__init__()
            self.records = [
                _scan(
                    status="completed",
                    revision=4,
                    scan_id="lan_" + f"{index:032x}",
                )
                for index in range(101)
            ]

        def list(self, *, limit: int) -> list[LanScanRecord]:
            self.calls.append(("list", limit))
            return self.records

        def get(self, scan_id: str) -> LanScanRecord | None:
            self.calls.append(("get", scan_id))
            return self.records[0] if scan_id == self.records[0].scan_id else None

        def observation_page(self, scan_id: str, *, limit: int) -> object | None:
            self.calls.append(("observations", scan_id, limit))
            if scan_id != self.records[0].scan_id:
                return None
            return SimpleNamespace(
                scan=self.records[0],
                observations=tuple(
                    _observation(index + 1, scan_id=scan_id) for index in range(201)
                ),
                total_count=201,
                truncated=False,
            )

    manager = OversupplyingManager()
    detail_id = manager.records[0].scan_id
    with _route_client(manager) as client:
        summaries = client.get("/api/routing/lan/scans")
        detail = client.get(f"/api/routing/lan/scans/{detail_id}")

    assert summaries.status_code == 200
    assert len(summaries.json()) == 100
    assert [item["scan_id"] for item in summaries.json()] == [
        item.scan_id for item in manager.records[:100]
    ]
    assert all("observations" not in item for item in summaries.json())
    assert detail.status_code == 200
    assert len(detail.json()["observations"]) == 200
    assert detail.json()["observation_total_count"] == 201
    assert detail.json()["observations_truncated"] is True
    _assert_no_private_authority(summaries.json())
    _assert_no_private_authority(detail.json())
    assert ("list", 100) in manager.calls
    assert ("get", detail_id) not in manager.calls
    assert ("observations", detail_id, 200) in manager.calls


@pytest.mark.parametrize(
    ("path", "body", "expected_status", "expected_code"),
    (
        (
            "/api/routing/lan/preview",
            b'{"interface_id":"secret-value","interface_id":"second","network":"x"}',
            400,
            "lan_request_invalid_json",
        ),
        (
            "/api/routing/lan/preview",
            b'{"interface_id":"\xff","network":"x"}',
            400,
            "lan_request_invalid_json",
        ),
        (
            "/api/routing/lan/preview",
            b'{"interface_id":"secret-value","network":NaN}',
            400,
            "lan_request_invalid_json",
        ),
        (
            "/api/routing/lan/preview",
            b"[" * 2_000 + b"0" + b"]" * 2_000,
            400,
            "lan_request_invalid_json",
        ),
        (
            "/api/routing/lan/manual-probe",
            b'{"mode":"preview","mode":"confirm","host":"raw-secret"}',
            400,
            "lan_request_invalid_json",
        ),
        (
            "/api/routing/lan/manual-probe",
            b'{"mode":"preview","host":"\xff"}',
            400,
            "lan_request_invalid_json",
        ),
        ("/api/routing/lan/preview", b"[]", 422, "lan_request_invalid"),
        ("/api/routing/lan/preview", b"null", 422, "lan_request_invalid"),
        (
            "/api/routing/lan/preview",
            b'{"interface_id":"secret-value","network":"x","owner_principal":"forged"}',
            422,
            "lan_request_invalid",
        ),
        (
            "/api/routing/lan/scans",
            json.dumps(
                {
                    "preview_digest": PREVIEW_DIGEST,
                    "expected_revision": True,
                    "confirmed": True,
                },
                separators=(",", ":"),
            ).encode(),
            422,
            "lan_request_invalid",
        ),
        (
            "/api/routing/lan/scans",
            json.dumps(
                {
                    "preview_digest": PREVIEW_DIGEST,
                    "expected_revision": 0,
                    "confirmed": 1,
                },
                separators=(",", ":"),
            ).encode(),
            422,
            "lan_request_invalid",
        ),
        (
            f"/api/routing/lan/scans/{SCAN_ID}/start",
            json.dumps(
                {
                    "expected_revision": 0,
                    "preview_digest": PREVIEW_DIGEST,
                    "confirmed": True,
                },
                separators=(",", ":"),
            ).encode(),
            422,
            "lan_request_invalid",
        ),
        (
            f"/api/routing/lan/scans/{SCAN_ID}/cancel",
            b'{"expected_revision":2,"reason":"renderer-controlled"}',
            422,
            "lan_request_invalid",
        ),
        (
            "/api/routing/lan/scans",
            json.dumps(
                {
                    "preview_digest": "b" * 64,
                    "expected_revision": 0,
                    "confirmed": True,
                },
                separators=(",", ":"),
            ).encode(),
            422,
            "lan_request_invalid",
        ),
        (
            "/api/routing/lan/scans",
            json.dumps(
                {
                    "preview_digest": PREVIEW_DIGEST,
                    "expected_revision": 0,
                    "confirmed": True,
                    "interface_id": INTERFACE.interface_id,
                },
                separators=(",", ":"),
            ).encode(),
            422,
            "lan_request_invalid",
        ),
    ),
    ids=(
        "duplicate-key",
        "invalid-utf8",
        "nonfinite",
        "excessive-nesting",
        "manual-duplicate-key",
        "manual-invalid-utf8",
        "array",
        "null",
        "extra-field",
        "bool-revision",
        "coerced-confirmation",
        "start-revision-zero",
        "renderer-cancel-reason",
        "bare-digest",
        "renderer-interface",
    ),
)
def test_mutation_parser_rejects_hostile_or_authority_broadening_shapes_without_echo(
    path: str,
    body: bytes,
    expected_status: int,
    expected_code: str,
) -> None:
    manager = RouteManager()
    with _route_client(manager) as client:
        response = client.post(
            path,
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == expected_status
    assert response.json() == {"detail": {"code": expected_code}}
    assert "secret-value" not in response.text
    assert "forged" not in response.text
    assert manager.calls == []


@pytest.mark.parametrize(
    ("path", "payload"),
    (
        pytest.param(
            "/api/routing/lan/scans",
            {
                "preview_digest": PREVIEW_DIGEST,
                "expected_revision": -1,
                "confirmed": True,
            },
            id="create-negative-revision",
        ),
        pytest.param(
            "/api/routing/lan/scans",
            {
                "preview_digest": PREVIEW_DIGEST,
                "expected_revision": 1,
                "confirmed": True,
            },
            id="create-nonzero-revision",
        ),
        pytest.param(
            "/api/routing/lan/scans",
            {
                "preview_digest": PREVIEW_DIGEST,
                "expected_revision": 0.0,
                "confirmed": True,
            },
            id="create-float-revision",
        ),
        pytest.param(
            "/api/routing/lan/scans",
            {
                "preview_digest": PREVIEW_DIGEST,
                "expected_revision": "0",
                "confirmed": True,
            },
            id="create-string-revision",
        ),
        pytest.param(
            "/api/routing/lan/scans",
            {
                "preview_digest": PREVIEW_DIGEST,
                "expected_revision": 0,
                "confirmed": False,
            },
            id="create-explicitly-unconfirmed",
        ),
        *(
            pytest.param(
                f"/api/routing/lan/scans/{SCAN_ID}/start",
                {
                    "expected_revision": revision,
                    "preview_digest": PREVIEW_DIGEST,
                    "confirmed": True,
                },
                id=f"start-revision-{label}",
            )
            for label, revision in (
                ("true", True),
                ("false", False),
                ("zero", 0),
                ("negative", -1),
                ("float", 1.0),
                ("string", "1"),
            )
        ),
        pytest.param(
            f"/api/routing/lan/scans/{SCAN_ID}/start",
            {
                "expected_revision": 1,
                "preview_digest": PREVIEW_DIGEST,
                "confirmed": False,
            },
            id="start-explicitly-unconfirmed",
        ),
        *(
            pytest.param(
                f"/api/routing/lan/scans/{SCAN_ID}/cancel",
                {"expected_revision": revision},
                id=f"cancel-revision-{label}",
            )
            for label, revision in (
                ("true", True),
                ("false", False),
                ("zero", 0),
                ("negative", -1),
                ("float", 1.0),
                ("string", "1"),
            )
        ),
    ),
)
def test_mutation_cas_and_confirmation_fields_are_exact_not_coerced(
    path: str,
    payload: dict[str, object],
) -> None:
    manager = RouteManager()
    with _route_client(manager) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "lan_request_invalid"}}
    assert manager.calls == []


def test_every_lan_request_rejects_identity_alias_query_get_body_and_oversize_first() -> None:
    manager = RouteManager(terminal=True)
    route_requests: tuple[tuple[str, str, dict[str, object] | None], ...] = (
        ("GET", "/api/routing/lan/interfaces", None),
        (
            "POST",
            "/api/routing/lan/preview",
            {"interface_id": INTERFACE.interface_id, "network": "192.168.90.0/30"},
        ),
        (
            "POST",
            "/api/routing/lan/manual-probe",
            {
                "mode": "preview",
                "interface_id": INTERFACE.interface_id,
                "host": "model-box.local",
                "port": 5001,
            },
        ),
        (
            "POST",
            "/api/routing/lan/manual-probe",
            {
                "mode": "confirm",
                "expected_revision": 0,
                "preview_digest": PREVIEW_DIGEST,
                "selected_address": "192.168.90.2",
                "confirmed": True,
                "privacy_acknowledged": True,
            },
        ),
        (
            "POST",
            "/api/routing/lan/scans",
            {
                "preview_digest": PREVIEW_DIGEST,
                "expected_revision": 0,
                "confirmed": True,
            },
        ),
        (
            "POST",
            f"/api/routing/lan/scans/{SCAN_ID}/start",
            {
                "expected_revision": 1,
                "preview_digest": PREVIEW_DIGEST,
                "confirmed": True,
            },
        ),
        ("GET", "/api/routing/lan/scans", None),
        ("GET", f"/api/routing/lan/scans/{SCAN_ID}", None),
        (
            "POST",
            f"/api/routing/lan/scans/{SCAN_ID}/cancel",
            {"expected_revision": 1},
        ),
        ("GET", f"/api/routing/lan/scans/{SCAN_ID}/events", None),
    )
    with _route_client(manager) as client:
        forged = []
        for alias in (
            "X-Kestrel-Owner-Principal",
            "X-Owner-Principal",
            "X-Authenticated-Principal",
        ):
            for method, path, body in route_requests:
                kwargs: dict[str, object] = {"headers": {alias: "raw-secret-owner"}}
                if body is not None:
                    kwargs["json"] = body
                forged.append(client.request(method, path, **kwargs))
        query = client.get("/api/routing/lan/scans?owner_principal=raw-secret-owner")
        manual_query = client.post(
            "/api/routing/lan/manual-probe?host=raw-secret-owner",
            json={
                "mode": "preview",
                "interface_id": INTERFACE.interface_id,
                "host": "model-box.local",
                "port": 5001,
            },
        )
        get_bodies = [
            client.request(
                "GET",
                path,
                content=b"{}",
                headers={"content-type": "application/json"},
            )
            for path in (
                "/api/routing/lan/interfaces",
                "/api/routing/lan/scans",
                f"/api/routing/lan/scans/{SCAN_ID}",
                f"/api/routing/lan/scans/{SCAN_ID}/events",
            )
        ]
        oversized = client.post(
            "/api/routing/lan/preview",
            content=b"x" * (32 * 1024 + 1),
            headers={"content-type": "application/json"},
        )

    for response in (*forged, query, manual_query, *get_bodies):
        assert response.status_code == 400
        assert response.json() == {"detail": {"code": "lan_request_rejected"}}
        assert "raw-secret-owner" not in response.text
    assert oversized.status_code == 413
    assert oversized.json() == {"detail": {"code": "lan_request_too_large"}}
    assert manager.calls == []


def test_chunked_transfer_encoded_get_body_is_rejected_before_event_streaming() -> None:
    async def exercise() -> None:
        manager = RouteManager()
        connection = _AsgiConnection(
            _route_app(manager),
            f"/api/routing/lan/scans/{SCAN_ID}/events",
            headers=(
                (b"content-type", b"application/json"),
                (b"transfer-encoding", b"chunked"),
            ),
            body_chunks=(b"{", b"}"),
        )
        assert await connection.start() == 400
        await connection.wait()
        assert connection.json_body() == {"detail": {"code": "lan_request_rejected"}}
        assert manager.calls == []

    _run_bounded(exercise())


def test_missing_scan_is_fixed_and_never_echoes_dynamic_identity() -> None:
    class MissingManager(RouteManager):
        def start_for_preview(
            self,
            scan_id: str,
            *,
            expected_revision: int,
            preview_digest: str,
        ) -> LanScanRecord:
            del expected_revision, preview_digest
            raise KeyError(f"raw missing scan: {scan_id}")

        def cancel(self, scan_id: str, *, expected_revision: int) -> LanScanRecord:
            del expected_revision
            raise KeyError(f"raw missing scan: {scan_id}")

    manager = MissingManager()
    manager.record = _scan(scan_id="lan_" + "f" * 32)
    with _route_client(manager) as client:
        missing = client.get(f"/api/routing/lan/scans/{SCAN_ID}")
        missing_start = client.post(
            f"/api/routing/lan/scans/{SCAN_ID}/start",
            json={
                "expected_revision": 1,
                "preview_digest": PREVIEW_DIGEST,
                "confirmed": True,
            },
        )
        missing_cancel = client.post(
            f"/api/routing/lan/scans/{SCAN_ID}/cancel",
            json={"expected_revision": 1},
        )
    missing_events_status, missing_events = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )

    for response in (missing, missing_start, missing_cancel):
        assert response.status_code == 404
        assert response.json() == {"detail": {"code": "lan_scan_not_found"}}
        assert SCAN_ID not in response.text
    assert missing_events_status == 404
    assert missing_events.json_body() == {"detail": {"code": "lan_scan_not_found"}}
    assert SCAN_ID not in missing_events.text()


def test_preview_lifecycle_and_scan_conflicts_map_to_fixed_distinct_codes() -> None:
    class PreviewConflict(RouteManager):
        def preview(self, interface_id: str, network: str) -> LanPreviewAuthorization:
            del interface_id, network
            raise LanPreviewAuthorizationError("raw-secret-preview-authority")

    class RevisionConflict(RouteManager):
        def create_draft_for_preview(
            self,
            preview_digest: str,
            *,
            expected_revision: int,
        ) -> LanScanRecord:
            del preview_digest, expected_revision
            raise LanScanRevisionConflict(SCAN_ID, 9)

        def start_for_preview(
            self,
            scan_id: str,
            *,
            expected_revision: int,
            preview_digest: str,
        ) -> LanScanRecord:
            del expected_revision, preview_digest
            raise LanScanRevisionConflict(scan_id, 9)

        def cancel(self, scan_id: str, *, expected_revision: int) -> LanScanRecord:
            del expected_revision
            raise LanScanRevisionConflict(scan_id, 9)

    class StateConflict(RouteManager):
        def cancel(self, scan_id: str, *, expected_revision: int) -> LanScanRecord:
            del expected_revision
            raise LanScanTransitionError(scan_id, "completed", "cancelling")

    class ActiveConflict(RouteManager):
        def start_for_preview(
            self,
            scan_id: str,
            *,
            expected_revision: int,
            preview_digest: str,
        ) -> LanScanRecord:
            del scan_id, expected_revision, preview_digest
            raise LanScanAdmissionConflict("raw-secret-active-owner")

    class LifecycleUnavailable(RouteManager):
        def interfaces(self) -> tuple[NetworkInterface, ...]:
            raise RuntimeError("raw-secret-lifecycle-failure")

    class ScopeInvalid(RouteManager):
        def preview(self, interface_id: str, network: str) -> LanPreviewAuthorization:
            del interface_id, network
            raise ValueError("raw-secret-scope-detail")

    with _route_client(PreviewConflict()) as client:
        preview = client.post(
            "/api/routing/lan/preview",
            json={"interface_id": INTERFACE.interface_id, "network": "192.168.90.0/30"},
        )
    with _route_client(RevisionConflict()) as client:
        revision_create = client.post(
            "/api/routing/lan/scans",
            json={
                "preview_digest": PREVIEW_DIGEST,
                "expected_revision": 0,
                "confirmed": True,
            },
        )
        revision = client.post(
            f"/api/routing/lan/scans/{SCAN_ID}/start",
            json={
                "expected_revision": 1,
                "preview_digest": PREVIEW_DIGEST,
                "confirmed": True,
            },
        )
        revision_cancel = client.post(
            f"/api/routing/lan/scans/{SCAN_ID}/cancel",
            json={"expected_revision": 1},
        )
    with _route_client(StateConflict()) as client:
        state = client.post(
            f"/api/routing/lan/scans/{SCAN_ID}/cancel",
            json={"expected_revision": 4},
        )
    with _route_client(ActiveConflict()) as client:
        active = client.post(
            f"/api/routing/lan/scans/{SCAN_ID}/start",
            json={
                "expected_revision": 1,
                "preview_digest": PREVIEW_DIGEST,
                "confirmed": True,
            },
        )
    with _route_client(LifecycleUnavailable()) as client:
        unavailable = client.get("/api/routing/lan/interfaces")
    with _route_client(ScopeInvalid()) as client:
        scope_invalid = client.post(
            "/api/routing/lan/preview",
            json={"interface_id": INTERFACE.interface_id, "network": "192.168.90.0/30"},
        )

    assert preview.status_code == 409
    assert preview.json() == {"detail": {"code": "lan_preview_conflict"}}
    for response, code in (
        (revision_create, "lan_scan_revision_conflict"),
        (revision, "lan_scan_revision_conflict"),
        (revision_cancel, "lan_scan_revision_conflict"),
        (state, "lan_scan_transition_conflict"),
        (active, "lan_scan_active_conflict"),
    ):
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": code}}
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": {"code": "lan_scan_unavailable"}}
    assert scope_invalid.status_code == 400
    assert scope_invalid.json() == {"detail": {"code": "lan_scope_invalid"}}
    for response in (
        preview,
        revision_create,
        revision,
        revision_cancel,
        state,
        active,
        unavailable,
        scope_invalid,
    ):
        assert "raw-secret" not in response.text


@pytest.mark.parametrize(
    "cursor",
    (
        "",
        "abc",
        "1.0",
        "00",
        "01",
        "+1",
        "-1",
        " 1",
        "1 ",
        "9223372036854775808",
        pytest.param("1" * 5_000, id="overlong-decimal"),
    ),
)
def test_sse_cursor_accepts_only_one_canonical_signed_64_bit_header(cursor: str) -> None:
    manager = RouteManager(terminal=True)
    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
        headers=((b"last-event-id", cursor.encode("ascii")),),
    )

    assert status == 400
    assert response.json_body() == {"detail": {"code": "lan_event_cursor_invalid"}}
    if cursor.strip():
        assert cursor.strip() not in response.text()


@pytest.mark.parametrize("cursor", ("0", "1", "7", "9223372036854775807"))
def test_sse_cursor_accepts_zero_and_all_canonical_positive_int64_values(cursor: str) -> None:
    manager = RouteManager(terminal=True)
    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
        headers=((b"last-event-id", cursor.encode("ascii")),),
    )

    assert status == 200
    assert response.response_headers()["content-type"].startswith("text/event-stream")


def test_sse_replays_strictly_after_cursor_with_canonical_bounded_frames_and_headers() -> None:
    manager = RouteManager(terminal=True)
    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
        headers=((b"last-event-id", b"1"),),
    )
    response_headers = response.response_headers()
    response_text = response.text()

    assert status == 200
    assert response_headers["content-type"].startswith("text/event-stream")
    assert response_headers["cache-control"] == "no-store, no-transform"
    assert response_headers["x-accel-buffering"] == "no"
    assert "id: 1\n" not in response_text
    assert "id: 2\n" in response_text
    assert "event: scan_completed\n" in response_text
    assert response_text.endswith("\n\n")
    frames = [item + "\n\n" for item in response_text.split("\n\n") if item]
    assert frames and all(len(item.encode("utf-8")) <= 16 * 1024 for item in frames)
    frame_lines = frames[0].removesuffix("\n\n").splitlines()
    data_line = next(line for line in frame_lines if line.startswith("data: "))
    expected_event = _expected_public_event(_event(2))
    assert json.loads(data_line.removeprefix("data: ")) == expected_event
    assert data_line == "data: " + json.dumps(
        expected_event,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert frame_lines == ["id: 2", "event: scan_completed", data_line]
    _assert_no_private_authority(expected_event)
    _assert_no_private_authority(response_text)

    maximum_status, maximum_cursor = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
        headers=((b"last-event-id", b"9223372036854775807"),),
    )
    assert maximum_status == 200
    assert maximum_cursor.text() == ""


def test_sse_replays_safe_manual_start_projection_then_drains_terminal_event() -> None:
    manager = RouteManager()
    manager.record = replace(
        _scan(status="completed", revision=4),
        network="192.168.90.2/32",
        limits=_manual_limits(5001),
    )
    manual_started = _event(
        1,
        "scan_started",
        payload=_manual_private_event_payload(),
    )
    completed = _event(2, "scan_completed")
    manager.durable_events = [manual_started, completed]

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )

    assert status == 200
    assert response.text().endswith("\n\n")
    frames = [item for item in response.text().split("\n\n") if item]
    assert len(frames) == 2
    assert [frame.splitlines()[1] for frame in frames] == [
        "event: scan_started",
        "event: scan_completed",
    ]
    first_data = next(line for line in frames[0].splitlines() if line.startswith("data: "))
    projected = json.loads(first_data.removeprefix("data: "))
    assert projected == {
        "scan_id": SCAN_ID,
        "sequence": 1,
        "event_type": "scan_started",
        "payload": _manual_public_event_payload(),
        "created_at": manual_started.created_at,
    }
    assert first_data == "data: " + json.dumps(
        projected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    _assert_no_private_authority(projected)
    _assert_no_private_authority(response.text())
    assert "model-box.local" not in response.text()
    assert "host_input_digest" not in response.text()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema", "kestrel.lan.scan-preview.v1"),
        ("mode", "automatic"),
        ("endpoint_kind", "automatic"),
        ("observation_source", "active"),
        ("interface_id", "sha256:" + "9" * 64),
        ("interface_id", "sha256:ABC"),
        ("network", "192.168.90.0/29"),
        ("limits", {**_manual_limits(), "max_active_hosts": 2}),
        ("active_host_count", 2),
        ("passive_or_manual_only", False),
        ("port_count", 2),
        ("exact_port", 5002),
        ("mdns_status", "available"),
        ("server_version", "raw-secret-server-version"),
        ("contract_version", "kestrel.lan.preview-authorization.v1"),
        ("preview_digest", "sha256:" + "9" * 64),
        ("preview_digest", "sha256:ABC"),
        ("expires_at", "raw-secret-expiry"),
        ("owner_principal", "owner:lookalike"),
        ("confirmed", False),
        ("privacy_acknowledged", False),
    ),
)
def test_sse_fails_closed_before_streaming_damaged_manual_start_fields(
    field: str,
    value: object,
) -> None:
    manager = RouteManager()
    manager.record = replace(
        _scan(status="completed", revision=4),
        network="192.168.90.2/32",
        limits=_manual_limits(5001),
    )
    manager.durable_events = [
        _event(
            1,
            "scan_started",
            payload={**_manual_private_event_payload(), field: value},
        ),
        _event(2, "scan_completed"),
    ]

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )

    assert status == 409
    assert response.json_body() == {"detail": {"code": "lan_event_invalid"}}
    assert "raw-secret" not in response.text()
    assert "model-box.local" not in response.text()
    assert "192.168.90.2" not in response.text()


@pytest.mark.parametrize("event_type", ALLOWED_EVENT_TYPES)
def test_sse_projects_each_allowed_durable_event_to_an_explicit_public_envelope(
    event_type: str,
) -> None:
    status_by_event = {
        "scan_started": "running",
        "scan_progress": "running",
        "scan_cancel_requested": "cancelling",
        "scan_completed": "completed",
        "scan_cancelled": "cancelled",
        "scan_failed": "failed",
        "scan_interrupted": "interrupted",
    }
    manager = RouteManager()
    manager.record = _scan(
        status=status_by_event[event_type],
        revision=4
        if status_by_event[event_type] in {"completed", "cancelled", "failed", "interrupted"}
        else 2,
    )
    durable = _event(1, event_type)
    manager.durable_events = [durable]

    async def collect() -> tuple[int, _AsgiConnection]:
        response = _AsgiConnection(
            _route_app(manager),
            f"/api/routing/lan/scans/{SCAN_ID}/events",
        )
        status = await response.start()
        if manager.record.is_terminal:
            await response.wait()
        else:
            await response.wait_for_body()
            await response.disconnect()
        return status, response

    status, response = _run_bounded(collect())
    response_text = response.text()

    assert status == 200
    assert response_text.endswith("\n\n")
    frames = [item for item in response_text.split("\n\n") if item]
    assert len(frames) == 1
    frame_lines = frames[0].splitlines()
    assert frame_lines[:2] == ["id: 1", f"event: {event_type}"]
    data_line = next(line for line in frame_lines if line.startswith("data: "))
    public = json.loads(data_line.removeprefix("data: "))
    assert public == _expected_public_event(durable)
    assert set(public) == {"scan_id", "sequence", "event_type", "payload", "created_at"}
    assert data_line == "data: " + json.dumps(
        public,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert frame_lines == ["id: 1", f"event: {event_type}", data_line]
    _assert_no_private_authority(public)
    _assert_no_private_authority(response_text)


def test_sse_replays_fixed_shutdown_cancel_events_after_runtime_restart() -> None:
    manager = RouteManager(terminal=True)
    manager.record = replace(
        _scan(status="cancelled", revision=4),
        cancel_reason="shutdown_cancelled",
        terminal_reason="shutdown_cancelled",
    )
    manager.durable_events = [
        _event(
            1,
            "scan_cancel_requested",
            payload={"reason": "shutdown_cancelled"},
        ),
        _event(
            2,
            "scan_cancelled",
            payload={
                "schema": "kestrel.lan.scan-terminal.v2",
                "status": "cancelled",
                "terminal_reason": "shutdown_cancelled",
                "cancel_reason": "shutdown_cancelled",
            },
        ),
    ]

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )
    response_text = response.text()

    assert status == 200
    assert response_text.endswith("\n\n")
    frames = [item for item in response_text.split("\n\n") if item]
    assert len(frames) == 2
    assert [frame.splitlines()[1] for frame in frames] == [
        "event: scan_cancel_requested",
        "event: scan_cancelled",
    ]
    public_payloads = [
        json.loads(
            next(line for line in frame.splitlines() if line.startswith("data: ")).removeprefix(
                "data: "
            )
        )["payload"]
        for frame in frames
    ]
    assert public_payloads == [
        {"reason": "shutdown_cancelled"},
        {
            "status": "cancelled",
            "terminal_reason": "shutdown_cancelled",
            "cancel_reason": "shutdown_cancelled",
        },
    ]
    _assert_no_private_authority(response_text)


@pytest.mark.parametrize("cancel_reason", ("owner_cancelled", "shutdown_cancelled"))
def test_sse_replays_failed_worker_event_after_canonical_prior_cancel(
    cancel_reason: str,
) -> None:
    manager = RouteManager(terminal=True)
    manager.record = replace(
        _scan(status="failed", revision=5),
        cancel_reason=cancel_reason,
    )
    manager.durable_events = [
        _event(1, "scan_started"),
        _event(
            2,
            "scan_cancel_requested",
            payload={"reason": cancel_reason},
        ),
        _event(
            3,
            "scan_failed",
            payload={
                "schema": "kestrel.lan.scan-terminal.v2",
                "status": "failed",
                "terminal_reason": "worker_error",
                "cancel_reason": cancel_reason,
            },
        ),
    ]

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )
    response_text = response.text()

    assert status == 200
    frames = [item for item in response_text.split("\n\n") if item]
    assert [frame.splitlines()[1] for frame in frames] == [
        "event: scan_started",
        "event: scan_cancel_requested",
        "event: scan_failed",
    ]
    terminal = json.loads(
        next(line for line in frames[-1].splitlines() if line.startswith("data: ")).removeprefix(
            "data: "
        )
    )
    assert terminal["payload"] == {
        "status": "failed",
        "terminal_reason": "worker_error",
        "cancel_reason": cancel_reason,
    }
    _assert_no_private_authority(response_text)


@pytest.mark.parametrize(
    ("record", "durable_events"),
    (
        pytest.param(
            _scan(status="failed", revision=5),
            (
                _event(
                    1,
                    "scan_failed",
                    payload={
                        "schema": "kestrel.lan.scan-terminal.v2",
                        "status": "failed",
                        "terminal_reason": "deadline_expired",
                        "cancel_reason": None,
                    },
                ),
            ),
            id="terminal-reason",
        ),
        pytest.param(
            replace(
                _scan(status="failed", revision=5),
                cancel_reason="owner_cancelled",
            ),
            (
                _event(
                    1,
                    "scan_failed",
                    payload={
                        "schema": "kestrel.lan.scan-terminal.v2",
                        "status": "failed",
                        "terminal_reason": "worker_error",
                        "cancel_reason": "shutdown_cancelled",
                    },
                ),
            ),
            id="terminal-cancel-authority",
        ),
        pytest.param(
            replace(
                _scan(status="failed", revision=5),
                cancel_reason="owner_cancelled",
            ),
            (
                _event(
                    1,
                    "scan_cancel_requested",
                    payload={"reason": "shutdown_cancelled"},
                ),
                _event(
                    2,
                    "scan_failed",
                    payload={
                        "schema": "kestrel.lan.scan-terminal.v2",
                        "status": "failed",
                        "terminal_reason": "worker_error",
                        "cancel_reason": "owner_cancelled",
                    },
                ),
            ),
            id="cancel-request-authority",
        ),
    ),
)
def test_sse_rejects_canonical_terminal_history_that_disagrees_with_scan_record(
    record: LanScanRecord,
    durable_events: tuple[LanScanEvent, ...],
) -> None:
    manager = RouteManager(terminal=True)
    manager.record = record
    manager.durable_events = list(durable_events)

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )

    assert status == 409
    assert response.json_body() == {"detail": {"code": "lan_event_invalid"}}
    assert "worker_error" not in response.text()
    assert "deadline_expired" not in response.text()
    assert "owner_cancelled" not in response.text()
    assert "shutdown_cancelled" not in response.text()


def test_sse_projects_returned_worker_interruption_bound_to_durable_record() -> None:
    manager = RouteManager(terminal=True)
    manager.record = replace(
        _scan(status="interrupted", revision=5),
        terminal_reason="worker_interrupted",
    )
    manager.durable_events = [
        _event(
            1,
            "scan_interrupted",
            payload={
                "schema": "kestrel.lan.scan-terminal.v2",
                "status": "interrupted",
                "terminal_reason": "worker_interrupted",
                "cancel_reason": None,
            },
        )
    ]

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )

    assert status == 200
    assert response.text().count("event: scan_interrupted\n") == 1
    payload = json.loads(
        next(
            line for line in response.text().splitlines() if line.startswith("data: ")
        ).removeprefix("data: ")
    )["payload"]
    assert payload == {
        "status": "interrupted",
        "terminal_reason": "worker_interrupted",
        "cancel_reason": None,
    }


@pytest.mark.parametrize(
    ("durable_events", "terminal_status", "private_marker"),
    (
        pytest.param(
            (
                _event(
                    1,
                    "scan_started",
                    payload={
                        **_private_event_payload("scan_started"),
                        "owner_principal": "raw-private-start-owner",
                    },
                ),
                _event(2),
            ),
            "completed",
            "raw-private-start-owner",
            id="scan-started-fixed-owner",
        ),
        pytest.param(
            (
                _event(
                    1,
                    "scan_started",
                    payload={
                        **_private_event_payload("scan_started"),
                        "schema": "raw-private-start-schema",
                    },
                ),
                _event(2),
            ),
            "completed",
            "raw-private-start-schema",
            id="scan-started-schema",
        ),
        pytest.param(
            (
                _event(
                    1,
                    "scan_started",
                    payload={
                        **_private_event_payload("scan_started"),
                        "raw_private_extra": "raw-private-start-shape",
                    },
                ),
                _event(2),
            ),
            "completed",
            "raw-private-start-shape",
            id="scan-started-shape",
        ),
        pytest.param(
            (
                _event(1, "scan_started"),
                _event(
                    2,
                    "scan_cancel_requested",
                    payload={"reason": "raw-private-cancel-reason"},
                ),
                _event(
                    3,
                    "scan_failed",
                    payload={
                        **_private_event_payload("scan_failed"),
                        "cancel_reason": "owner_cancelled",
                    },
                ),
            ),
            "failed",
            "raw-private-cancel-reason",
            id="cancel-request-reason",
        ),
        pytest.param(
            (
                _event(1, "scan_started"),
                _event(
                    2,
                    "scan_completed",
                    payload={
                        **_private_event_payload("scan_completed"),
                        "status": "failed",
                        "terminal_reason": "worker_error",
                    },
                ),
            ),
            "completed",
            "worker_error",
            id="terminal-event-status",
        ),
        pytest.param(
            (
                _event(1, "scan_started"),
                _event(
                    2,
                    "scan_failed",
                    payload={
                        **_private_event_payload("scan_failed"),
                        "cancel_reason": "raw-private-terminal-cancel",
                    },
                ),
            ),
            "failed",
            "raw-private-terminal-cancel",
            id="failed-terminal-arbitrary-cancel",
        ),
        pytest.param(
            (
                _event(1, "scan_started"),
                _event(
                    2,
                    "scan_cancel_requested",
                    payload={"reason": "owner_cancelled"},
                ),
                _event(
                    3,
                    "scan_failed",
                    payload={
                        "schema": "kestrel.lan.scan-terminal.v2",
                        "status": "failed",
                        "terminal_reason": "deadline_expired",
                        "cancel_reason": "owner_cancelled",
                    },
                ),
            ),
            "failed",
            "deadline_expired",
            id="failed-deadline-owner-cancel",
        ),
        pytest.param(
            (
                _event(1, "scan_started"),
                _event(
                    2,
                    "scan_cancel_requested",
                    payload={"reason": "shutdown_cancelled"},
                ),
                _event(
                    3,
                    "scan_failed",
                    payload={
                        "schema": "kestrel.lan.scan-terminal.v2",
                        "status": "failed",
                        "terminal_reason": "deadline_expired",
                        "cancel_reason": "shutdown_cancelled",
                    },
                ),
            ),
            "failed",
            "deadline_expired",
            id="failed-deadline-shutdown-cancel",
        ),
        pytest.param(
            (
                _event(1, "scan_started"),
                _event(
                    2,
                    "scan_completed",
                    payload={
                        **_private_event_payload("scan_completed"),
                        "terminal_reason": "raw-private-terminal-reason",
                    },
                ),
            ),
            "completed",
            "raw-private-terminal-reason",
            id="terminal-reason",
        ),
        pytest.param(
            (
                _event(1, "scan_started"),
                _event(
                    2,
                    "scan_completed",
                    payload={
                        **_private_event_payload("scan_completed"),
                        "cancel_reason": "raw-private-terminal-cancel",
                    },
                ),
            ),
            "completed",
            "raw-private-terminal-cancel",
            id="terminal-unexpected-cancel",
        ),
        pytest.param(
            (
                _event(1, "scan_started"),
                _event(
                    2,
                    "scan_cancelled",
                    payload={
                        **_private_event_payload("scan_cancelled"),
                        "cancel_reason": "shutdown_cancelled",
                    },
                ),
            ),
            "cancelled",
            "shutdown_cancelled",
            id="terminal-cancel-mismatch",
        ),
        pytest.param(
            (
                _event(
                    1,
                    "scan_started",
                    scan_id="lan_" + "f" * 32,
                ),
                _event(2),
            ),
            "completed",
            "lan_" + "f" * 32,
            id="cross-scan-id",
        ),
        pytest.param(
            (_event(True, "scan_started"), _event(2)),
            "completed",
            PREVIEW_DIGEST,
            id="boolean-sequence",
        ),
        pytest.param(
            (_event(0, "scan_started"), _event(1)),
            "completed",
            PREVIEW_DIGEST,
            id="zero-sequence",
        ),
        pytest.param(
            (_event(2, "scan_started"), _event(1)),
            "completed",
            PREVIEW_DIGEST,
            id="nonmonotonic-sequence",
        ),
        pytest.param(
            (
                _event(
                    1,
                    "scan_started",
                    created_at="raw-private-created-at",
                ),
                _event(2),
            ),
            "completed",
            "raw-private-created-at",
            id="malformed-created-at",
        ),
        pytest.param(
            (
                _event(
                    1,
                    "scan_started",
                    created_at="raw-private-created-at-" + "x" * 80,
                ),
                _event(2),
            ),
            "completed",
            "raw-private-created-at-",
            id="overlong-created-at",
        ),
    ),
)
def test_sse_rejects_hostile_recognized_initial_events_with_one_fixed_error(
    durable_events: tuple[LanScanEvent, ...],
    terminal_status: str,
    private_marker: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class HostileInitialManager(RouteManager):
        def events(
            self,
            scan_id: str,
            *,
            after_sequence: int = 0,
            limit: int = 500,
        ) -> list[LanScanEvent]:
            self.calls.append(("events", scan_id, after_sequence, limit))
            return list(self.durable_events[:limit])

    caplog.set_level(logging.DEBUG)
    manager = HostileInitialManager(terminal=True)
    manager.record = _scan(status=terminal_status, revision=4)
    manager.durable_events = list(durable_events)

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )

    assert status == 409
    assert response.json_body() == {"detail": {"code": "lan_event_invalid"}}
    assert private_marker not in response.text()
    assert private_marker not in caplog.text


def test_sse_rejects_duplicate_header_query_cursor_and_unknown_durable_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    manager = RouteManager(terminal=True)
    duplicates = [
        _bounded_asgi_get(
            manager,
            f"/api/routing/lan/scans/{SCAN_ID}/events",
            headers=headers,
        )
        for headers in (
            ((b"Last-Event-ID", b"0"), (b"Last-Event-ID", b"1")),
            ((b"Last-Event-ID", b"0"), (b"Last-Event-ID", b"0")),
            ((b"Last-Event-ID", b"0"), (b"last-event-id", b"0")),
        )
    ]
    query_status, query = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events?cursor=1",
    )
    manager.durable_events = [
        LanScanEvent(
            scan_id=SCAN_ID,
            sequence=1,
            event_type="raw_unknown_event",
            payload={"message": "raw-private-event-material"},
            created_at="2026-08-01T12:00:01Z",
        )
    ]
    damaged_status, damaged = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )
    manager.durable_events = [
        LanScanEvent(
            scan_id=SCAN_ID,
            sequence=1,
            event_type="scan_progress",
            payload={"message": "raw-frame-material-" * 1_000},
            created_at="2026-08-01T12:00:01Z",
        )
    ]
    oversized_status, oversized = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )

    for status, duplicate in duplicates:
        assert status == 400
        assert duplicate.json_body() == {"detail": {"code": "lan_event_cursor_invalid"}}
    assert query_status == 400
    assert query.json_body() == {"detail": {"code": "lan_event_cursor_invalid"}}
    assert damaged_status == 409
    assert damaged.json_body() == {"detail": {"code": "lan_event_invalid"}}
    assert "raw_unknown_event" not in damaged.text()
    assert oversized_status == 409
    assert oversized.json_body() == {"detail": {"code": "lan_event_invalid"}}
    assert "raw-frame-material" not in oversized.text()
    for marker in (
        "raw_unknown_event",
        "raw-private-event-material",
        "raw-frame-material",
    ):
        assert marker not in caplog.text


def _run_bounded(awaitable: Any, *, timeout: float = 2.0) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(awaitable, timeout=timeout)

    return asyncio.run(bounded())


def test_registered_sse_endpoint_performs_a_terminal_second_drain() -> None:
    terminal = _scan(status="completed", revision=4)
    final_event = _event(7)

    class FinalRaceManager:
        def __init__(self) -> None:
            self.event_calls = 0

        def events(self, *_args: object, **_kwargs: object) -> list[LanScanEvent]:
            self.event_calls += 1
            return [final_event] if self.event_calls == 2 else []

        def get(self, _scan_id: str) -> LanScanRecord:
            return terminal

    manager = FinalRaceManager()
    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )
    assert status == 200
    assert manager.event_calls >= 2
    assert response.text().count("id: 7\n") == 1
    assert "event: scan_completed\n" in response.text()


def test_sse_preflight_refreshes_record_after_initial_terminal_event_race() -> None:
    running = _scan(status="running", revision=2)
    terminal = _scan(status="completed", revision=4)
    final_event = _event(7)

    class InitialTerminalRaceManager:
        def __init__(self) -> None:
            self.record = running
            self.calls: list[tuple[object, ...]] = []

        def get(self, scan_id: str) -> LanScanRecord:
            self.calls.append(("get", scan_id))
            return self.record

        def events(
            self,
            scan_id: str,
            *,
            after_sequence: int = 0,
            limit: int = 500,
        ) -> list[LanScanEvent]:
            self.calls.append(("events", scan_id, after_sequence, limit))
            self.record = terminal
            return [final_event] if after_sequence < final_event.sequence else []

    manager = InitialTerminalRaceManager()
    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
        headers=((b"last-event-id", b"6"),),
    )

    assert status == 200
    assert response.text().count("id: 7\n") == 1
    assert response.text().count("event: scan_completed\n") == 1
    assert manager.calls[:3] == [
        ("get", SCAN_ID),
        ("events", SCAN_ID, 6, 500),
        ("get", SCAN_ID),
    ]


@pytest.mark.parametrize("event_cancel_reason", ("owner_cancelled", "shutdown_cancelled"))
def test_sse_later_terminal_batch_binds_to_refreshed_record_before_emission(
    event_cancel_reason: str,
) -> None:
    async def exercise() -> tuple[int, str, list[tuple[object, ...]]]:
        class LaterTerminalRaceManager:
            def __init__(self) -> None:
                self.record = _scan(status="running", revision=2)
                self.calls: list[tuple[object, ...]] = []
                self.event_calls = 0

            def get(self, scan_id: str) -> LanScanRecord:
                self.calls.append(("get", scan_id))
                return self.record

            def events(
                self,
                scan_id: str,
                *,
                after_sequence: int = 0,
                limit: int = 500,
            ) -> list[LanScanEvent]:
                self.calls.append(("events", scan_id, after_sequence, limit))
                self.event_calls += 1
                if self.event_calls != 2:
                    return []
                self.record = replace(
                    _scan(status="failed", revision=5),
                    cancel_reason="owner_cancelled",
                )
                return [
                    _event(
                        3,
                        "scan_failed",
                        payload={
                            "schema": "kestrel.lan.scan-terminal.v2",
                            "status": "failed",
                            "terminal_reason": "worker_error",
                            "cancel_reason": event_cancel_reason,
                        },
                    )
                ]

        async def immediate_sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

        manager = LaterTerminalRaceManager()
        app = _route_app(
            manager,
            stream_runtime=SimpleNamespace(
                monotonic_clock=lambda: 0.0,
                sleep=immediate_sleep,
            ),
        )
        connection = _AsgiConnection(
            app,
            f"/api/routing/lan/scans/{SCAN_ID}/events",
        )
        status = await connection.start()
        await connection.wait()
        return status, connection.text(), manager.calls

    status, response_text, calls = _run_bounded(exercise())

    assert status == 200
    if event_cancel_reason == "owner_cancelled":
        assert response_text.count("event: scan_failed\n") == 1
    else:
        assert "event: scan_failed\n" not in response_text
        assert "shutdown_cancelled" not in response_text
    assert [call[0] for call in calls[:5]] == [
        "get",
        "events",
        "get",
        "events",
        "get",
    ]


def test_sse_terminal_replay_drains_every_bounded_page_through_the_terminal() -> None:
    manager = RouteManager(terminal=True)
    manager.durable_events = [
        *(_event(sequence, "scan_progress") for sequence in range(1, 1_002)),
        _event(1_002),
    ]

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )
    response_text = response.text()

    assert status == 200
    assert response_text.count("id: ") == 1_002
    assert "id: 1002\n" in response_text
    assert response_text.count("event: scan_completed\n") == 1
    assert response_text.endswith("\n\n")


def test_sse_terminal_state_is_preserved_across_bounded_pages() -> None:
    manager = RouteManager(terminal=True)
    manager.durable_events = [
        *(_event(sequence, "scan_progress") for sequence in range(1, 500)),
        _event(500),
        _event(501, "scan_progress"),
    ]

    status, response = _bounded_asgi_get(
        manager,
        f"/api/routing/lan/scans/{SCAN_ID}/events",
    )
    response_text = response.text()

    assert status == 200
    assert response_text.count("id: ") == 500
    assert "id: 500\n" in response_text
    assert "id: 501\n" not in response_text
    assert response_text.endswith("\n\n")


class _ObservableStreamManager:
    def __init__(self, count: int = 16) -> None:
        self.scan_ids = tuple("lan_" + f"{index:032x}" for index in range(count))
        self.records = {
            scan_id: _scan(status="running", revision=2, scan_id=scan_id)
            for scan_id in self.scan_ids
        }
        self.durable_events: dict[str, list[LanScanEvent]] = {
            scan_id: [] for scan_id in self.scan_ids
        }
        self.invalid_next: set[str] = set()
        self.error_next: set[str] = set()
        self.get_error_next: set[str] = set()
        self.event_calls: Counter[str] = Counter()
        self.get_calls: Counter[str] = Counter()
        self.cancel_calls = 0

    def get(self, scan_id: str) -> LanScanRecord | None:
        self.get_calls[scan_id] += 1
        if scan_id in self.get_error_next:
            self.get_error_next.remove(scan_id)
            raise RuntimeError("raw-private-stream-get-error")
        return self.records.get(scan_id)

    def events(
        self,
        scan_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[LanScanEvent]:
        self.event_calls[scan_id] += 1
        if scan_id in self.error_next:
            self.error_next.remove(scan_id)
            raise RuntimeError("raw-private-stream-manager-error")
        if scan_id in self.invalid_next:
            self.invalid_next.remove(scan_id)
            return [
                LanScanEvent(
                    scan_id=scan_id,
                    sequence=after_sequence + 1,
                    event_type="scan_progress",
                    payload={"message": "raw-private-stream-event"},
                    created_at="2026-08-01T12:00:01Z",
                )
            ]
        return [event for event in self.durable_events[scan_id] if event.sequence > after_sequence][
            :limit
        ]

    def cancel(self, *_args: object, **_kwargs: object) -> None:
        self.cancel_calls += 1
        raise AssertionError("an SSE transport event must not mutate scan state")

    def complete(self, scan_id: str) -> None:
        self.records[scan_id] = _scan(
            status="completed",
            revision=4,
            scan_id=scan_id,
        )
        self.durable_events[scan_id] = [_event(1, scan_id=scan_id)]


async def _cleanup_connections(connections: list[_AsgiConnection]) -> None:
    tasks: list[asyncio.Task[None]] = []
    for connection in connections:
        connection._disconnect.set()
        if connection.task is not None:
            tasks.append(connection.task)
    if tasks:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=2.0,
        )


@pytest.mark.parametrize("failure_point", ("initial_clock", "later_clock", "sleep"))
def test_sse_runtime_failures_close_cleanly_and_release_the_stream_lease(
    failure_point: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def exercise() -> None:
        manager = _ObservableStreamManager(count=6)
        clock_calls = 0
        failed = False

        def clock() -> float:
            nonlocal clock_calls, failed
            clock_calls += 1
            if failure_point == "initial_clock" and not failed:
                failed = True
                raise RuntimeError("raw-private-initial-clock-failure")
            if failure_point == "later_clock" and clock_calls == 2 and not failed:
                failed = True
                raise RuntimeError("raw-private-later-clock-failure")
            return 0.0

        async def sleeper(seconds: float) -> None:
            nonlocal failed
            assert seconds > 0.0
            if failure_point == "sleep" and not failed:
                failed = True
                raise RuntimeError("raw-private-stream-sleep-failure")
            await asyncio.sleep(0.01)

        app = _route_app(
            manager,
            stream_runtime=SimpleNamespace(monotonic_clock=clock, sleep=sleeper),
        )
        failed_connection = _AsgiConnection(
            app,
            f"/api/routing/lan/scans/{manager.scan_ids[0]}/events",
        )
        assert await failed_connection.start() == 200
        assert await failed_connection.wait_allowing_error() is None
        assert failed_connection.body() == b""

        live: list[_AsgiConnection] = []
        try:
            for scan_id in manager.scan_ids[1:5]:
                connection = _AsgiConnection(
                    app,
                    f"/api/routing/lan/scans/{scan_id}/events",
                )
                assert await connection.start() == 200
                live.append(connection)

            rejected = _AsgiConnection(
                app,
                f"/api/routing/lan/scans/{manager.scan_ids[5]}/events",
            )
            assert await rejected.start() == 429
            await rejected.wait()
            assert rejected.json_body() == {"detail": {"code": "lan_event_stream_limit"}}
        finally:
            await _cleanup_connections(live)

    _run_bounded(exercise(), timeout=5.0)
    for marker in (
        "raw-private-initial-clock-failure",
        "raw-private-later-clock-failure",
        "raw-private-stream-sleep-failure",
    ):
        assert marker not in caplog.text


def test_sse_route_limit_releases_exactly_once_on_all_transport_endings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def exercise() -> None:
        manager = _ObservableStreamManager()
        app = _route_app(manager)

        async def open_stream(scan_id: str) -> _AsgiConnection:
            connection = _AsgiConnection(
                app,
                f"/api/routing/lan/scans/{scan_id}/events",
            )
            assert await connection.start() == 200
            assert connection.task is not None and not connection.task.done()
            return connection

        async def reject_stream(scan_id: str) -> None:
            rejected = _AsgiConnection(
                app,
                f"/api/routing/lan/scans/{scan_id}/events",
            )
            assert await rejected.start() == 429
            await rejected.wait()
            assert rejected.json_body() == {"detail": {"code": "lan_event_stream_limit"}}

        live = [await open_stream(scan_id) for scan_id in manager.scan_ids[:4]]
        await reject_stream(manager.scan_ids[4])

        disconnected = live.pop(0)
        await disconnected.disconnect()
        live.append(await open_stream(manager.scan_ids[4]))
        await reject_stream(manager.scan_ids[5])

        terminal = live.pop(0)
        manager.complete(manager.scan_ids[1])
        assert await terminal.wait_allowing_error() is None
        live.append(await open_stream(manager.scan_ids[5]))
        await reject_stream(manager.scan_ids[6])

        invalid = live.pop(0)
        invalid_scan_id = manager.scan_ids[2]
        invalid_record_before = manager.records[invalid_scan_id]
        invalid_events_before = tuple(manager.durable_events[invalid_scan_id])
        manager.invalid_next.add(invalid_scan_id)
        assert await invalid.wait_allowing_error() is None
        assert invalid_scan_id not in manager.invalid_next
        assert invalid.body() == b""
        assert manager.records[invalid_scan_id] == invalid_record_before
        assert tuple(manager.durable_events[invalid_scan_id]) == invalid_events_before
        live.append(await open_stream(manager.scan_ids[6]))
        await reject_stream(manager.scan_ids[7])

        errored = live.pop(0)
        manager.error_next.add(manager.scan_ids[3])
        assert await errored.wait_allowing_error() is None
        assert manager.scan_ids[3] not in manager.error_next
        assert errored.body() == b""
        live.append(await open_stream(manager.scan_ids[7]))
        await reject_stream(manager.scan_ids[8])

        get_errored = live.pop(0)
        get_scan_id = manager.scan_ids[4]
        record_before = manager.records[get_scan_id]
        events_before = tuple(manager.durable_events[get_scan_id])
        manager.get_error_next.add(get_scan_id)
        assert await get_errored.wait_allowing_error() is None
        assert get_scan_id not in manager.get_error_next
        assert manager.get_calls[get_scan_id] >= 2
        assert get_errored.body() == b""
        assert manager.records[get_scan_id] == record_before
        assert tuple(manager.durable_events[get_scan_id]) == events_before
        live.append(await open_stream(manager.scan_ids[8]))
        await reject_stream(manager.scan_ids[9])

        cancelled = live.pop(0)
        await cancelled.cancel()
        live.append(await open_stream(manager.scan_ids[9]))
        await reject_stream(manager.scan_ids[10])

        assert manager.cancel_calls == 0
        await _cleanup_connections(live)

    _run_bounded(exercise(), timeout=5.0)
    assert "scan_progress" not in caplog.text
    assert "raw-private-stream-event" not in caplog.text
    assert "raw-private-stream-manager-error" not in caplog.text
    assert "raw-private-stream-get-error" not in caplog.text


def test_sse_route_limit_is_atomic_under_eight_connection_race() -> None:
    async def exercise() -> None:
        manager = _ObservableStreamManager(count=8)
        app = _route_app(manager)
        connections = [
            _AsgiConnection(app, f"/api/routing/lan/scans/{scan_id}/events")
            for scan_id in manager.scan_ids
        ]
        statuses = await asyncio.gather(*(item.start() for item in connections))
        assert Counter(statuses) == Counter({200: 4, 429: 4})
        accepted = []
        for connection, status in zip(connections, statuses, strict=True):
            if status == 200:
                accepted.append(connection)
            else:
                await connection.wait()
                assert connection.json_body() == {"detail": {"code": "lan_event_stream_limit"}}
        await _cleanup_connections(accepted)
        assert manager.cancel_calls == 0

    _run_bounded(exercise(), timeout=5.0)


def test_sse_preflight_failures_never_consume_a_stream_lease_on_one_app() -> None:
    async def exercise() -> None:
        manager = _ObservableStreamManager(count=7)
        damaged_id = manager.scan_ids[0]
        manager.durable_events[damaged_id] = [
            LanScanEvent(
                scan_id=damaged_id,
                sequence=1,
                event_type="raw_private_initial_event",
                payload={"message": "raw-private-initial-payload"},
                created_at="2026-08-01T12:00:01Z",
            )
        ]
        app = _route_app(manager)

        async def fixed_failure(
            path: str,
            *,
            expected_status: int,
            expected_code: str,
            headers: tuple[tuple[bytes, bytes], ...] = (),
        ) -> None:
            connection = _AsgiConnection(app, path, headers=headers)
            status = await connection.start()
            if status == 200:
                await connection.disconnect()
            else:
                await connection.wait()
            assert status == expected_status
            assert connection.json_body() == {"detail": {"code": expected_code}}
            assert "raw-private" not in connection.text()

        missing_id = "lan_" + "f" * 32
        live: list[_AsgiConnection] = []
        try:
            for scan_id in manager.scan_ids[1:5]:
                connection = _AsgiConnection(
                    app,
                    f"/api/routing/lan/scans/{scan_id}/events",
                )
                assert await connection.start() == 200
                live.append(connection)

            await fixed_failure(
                f"/api/routing/lan/scans/{manager.scan_ids[1]}/events",
                expected_status=400,
                expected_code="lan_event_cursor_invalid",
                headers=((b"last-event-id", b"1.0"),),
            )
            await fixed_failure(
                f"/api/routing/lan/scans/{missing_id}/events",
                expected_status=404,
                expected_code="lan_scan_not_found",
            )
            await fixed_failure(
                f"/api/routing/lan/scans/{damaged_id}/events",
                expected_status=409,
                expected_code="lan_event_invalid",
            )

            rejected = _AsgiConnection(
                app,
                f"/api/routing/lan/scans/{manager.scan_ids[5]}/events",
            )
            rejected_status = await rejected.start()
            if rejected_status == 200:
                await rejected.disconnect()
            else:
                await rejected.wait()
            assert rejected_status == 429
            assert rejected.json_body() == {"detail": {"code": "lan_event_stream_limit"}}
        finally:
            await _cleanup_connections(live)
        assert manager.cancel_calls == 0

    _run_bounded(exercise(), timeout=5.0)


def test_sse_route_heartbeat_boundary_and_disconnect_after_empty_poll_are_observable() -> None:
    async def exercise_heartbeat() -> None:
        manager = _ObservableStreamManager(count=1)
        current = 14.999
        clock_calls = 0
        requested_sleeps: list[float] = []
        threshold_sleep_entered = asyncio.Event()
        allow_threshold = asyncio.Event()
        blocking_sleep_entered = asyncio.Event()
        sleep_cancelled = asyncio.Event()

        def clock() -> float:
            nonlocal clock_calls
            clock_calls += 1
            if clock_calls == 1:
                return 0.0
            return current

        async def advance(seconds: float) -> None:
            nonlocal current
            assert seconds > 0.0
            requested_sleeps.append(seconds)
            if current < 15.0:
                threshold_sleep_entered.set()
                await allow_threshold.wait()
                current = 15.0
                return
            blocking_sleep_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sleep_cancelled.set()
                raise

        app = _route_app(
            manager,
            stream_runtime=SimpleNamespace(monotonic_clock=clock, sleep=advance),
        )
        connection = _AsgiConnection(
            app,
            f"/api/routing/lan/scans/{manager.scan_ids[0]}/events",
        )
        assert await connection.start() == 200
        await asyncio.wait_for(threshold_sleep_entered.wait(), timeout=1.0)
        assert current < 15.0
        assert connection.body() == b""
        allow_threshold.set()
        await connection.wait_for_body()
        assert requested_sleeps and all(item > 0.0 for item in requested_sleeps)
        assert current >= 15.0
        assert connection.body().startswith(b":")
        assert connection.body().endswith(b"\n\n")
        assert connection.body().count(b"\n\n") == 1
        assert b"data:" not in connection.body()
        assert b"event:" not in connection.body()
        await asyncio.wait_for(blocking_sleep_entered.wait(), timeout=1.0)
        await connection.cancel()
        await asyncio.wait_for(sleep_cancelled.wait(), timeout=1.0)
        assert manager.cancel_calls == 0

    async def exercise_disconnect() -> None:
        manager = _ObservableStreamManager(count=1)
        sleep_entered = asyncio.Event()
        release = asyncio.Event()
        requested_sleeps: list[float] = []

        async def blocked_sleep(seconds: float) -> None:
            assert seconds > 0.0
            requested_sleeps.append(seconds)
            sleep_entered.set()
            await release.wait()

        app = _route_app(
            manager,
            stream_runtime=SimpleNamespace(
                monotonic_clock=lambda: 0.0,
                sleep=blocked_sleep,
            ),
        )
        scan_id = manager.scan_ids[0]
        connection = _AsgiConnection(
            app,
            f"/api/routing/lan/scans/{scan_id}/events",
        )
        assert await connection.start() == 200
        await asyncio.wait_for(sleep_entered.wait(), timeout=1.0)
        count_at_disconnect = manager.event_calls[scan_id]
        assert count_at_disconnect >= 1
        connection._disconnect.set()
        release.set()
        await connection.wait()
        assert manager.event_calls[scan_id] == count_at_disconnect
        assert requested_sleeps and all(item > 0.0 for item in requested_sleeps)
        assert manager.cancel_calls == 0

    _run_bounded(exercise_heartbeat())
    _run_bounded(exercise_disconnect())


def test_standalone_discovery_route_registration_adds_one_manual_route_without_duplicates() -> None:
    fastapi = pytest.importorskip("fastapi")
    responses = pytest.importorskip("starlette.responses")
    app = fastapi.FastAPI()
    _task7_routes().register_lan_discovery_routes(
        app,
        scan_manager=RouteManager(),
        http_exception=fastapi.HTTPException,
        streaming_response=responses.StreamingResponse,
    )

    manifest = Counter(
        (method, route.path)
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/routing/lan")
        for method in getattr(route, "methods", ())
    )
    assert manifest == Counter(
        {
            ("GET", "/api/routing/lan/interfaces"): 1,
            ("POST", "/api/routing/lan/preview"): 1,
            ("POST", "/api/routing/lan/scans"): 1,
            ("POST", "/api/routing/lan/scans/{scan_id}/start"): 1,
            ("GET", "/api/routing/lan/scans"): 1,
            ("GET", "/api/routing/lan/scans/{scan_id}"): 1,
            ("POST", "/api/routing/lan/scans/{scan_id}/cancel"): 1,
            ("GET", "/api/routing/lan/scans/{scan_id}/events"): 1,
            ("POST", "/api/routing/lan/manual-probe"): 1,
        }
    )
    assert not any(path.endswith("/import") for _method, path in manifest)
    assert not any(path.endswith("/review") for _method, path in manifest)


def test_route_adapter_has_no_network_ledger_executor_or_sync_subscription_path() -> None:
    routes = _task7_routes()
    source = inspect.getsource(routes)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")

    prohibited = (
        "socket",
        "concurrent.futures",
        "lan_discovery_scope",
        "lan_http_transport",
        "lan_scanner",
        "nested_memvid_agent.lan_discovery_scope",
        "nested_memvid_agent.lan_http_transport",
        "nested_memvid_agent.lan_scanner",
        "nested_memvid_agent.routing.lan_ledger",
        "routing.lan_ledger",
    )
    assert not any(
        module == forbidden or module.startswith(forbidden + ".")
        for module in imported_modules
        for forbidden in prohibited
    )
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(call, ast.Attribute) and call.attr == "subscribe_events" for call in calls
    )
    assert not any(
        isinstance(call, ast.Attribute)
        and call.attr == "sleep"
        and isinstance(call.value, ast.Name)
        and call.value.id == "time"
        for call in calls
    )
