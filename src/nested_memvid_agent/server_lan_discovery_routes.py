"""Secret-safe HTTP adapters for explicit, owner-controlled LAN discovery."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .lan_discovery_models import LanScanLimits
from .lan_scan_manager import (
    LAN_OWNER_PRINCIPAL,
    LanPreviewAuthorization,
    LanPreviewAuthorizationError,
    LanScanAdmissionConflict,
)
from .routing.lan_records import (
    LanObservationRecord,
    LanScanEvent,
    LanScanRecord,
    LanScanRevisionConflict,
    LanScanTransitionError,
)
from .routing.lan_serialization import (
    bounded_observation_public_evidence,
    bounded_scan_preview_event,
    bounded_scan_progress_event,
)
from .server_routing_routes import _parse_lan_json_request, _require_lan_get_request

_SCAN_ID_RE = re.compile(r"lan_[0-9a-f]{32}\Z")
_UTC_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed", "interrupted"})
_ALLOWED_EVENT_TYPES = frozenset(
    {
        "scan_started",
        "scan_progress",
        "scan_cancel_requested",
        "scan_completed",
        "scan_cancelled",
        "scan_failed",
        "scan_interrupted",
    }
)
_MAX_SCAN_SUMMARIES = 100
_MAX_OBSERVATIONS = 200
_MAX_EVENT_BATCH = 500
_MAX_EVENT_FRAME_BYTES = 16 * 1024
_MAX_EVENT_SEQUENCE = 2**63 - 1
_MAX_STREAMS = 4
_HEARTBEAT_SECONDS = 15.0
_POLL_SECONDS = 0.25


class _PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interface_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", strict=True)
    network: str = Field(min_length=1, max_length=128, strict=True)


class _CreateScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", strict=True)
    expected_revision: int = Field(strict=True)
    confirmed: bool = Field(strict=True)

    @model_validator(mode="after")
    def require_initial_confirmation(self) -> _CreateScanRequest:
        if type(self.expected_revision) is not int or self.expected_revision != 0:
            raise ValueError("initial LAN scan revision must be zero")
        if self.confirmed is not True:
            raise ValueError("LAN scan must be explicitly confirmed")
        return self


class _StartScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(strict=True)
    preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", strict=True)
    confirmed: bool = Field(strict=True)

    @model_validator(mode="after")
    def require_start_confirmation(self) -> _StartScanRequest:
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("LAN start revision must be positive")
        if self.confirmed is not True:
            raise ValueError("LAN scan must be explicitly confirmed")
        return self


class _CancelScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(strict=True)

    @model_validator(mode="after")
    def require_positive_revision(self) -> _CancelScanRequest:
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("LAN cancel revision must be positive")
        return self


class _StreamLeases:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self) -> bool:
        with self._lock:
            if self._active >= _MAX_STREAMS:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                return
            self._active -= 1


def _error(http_exception: type[Exception], status: int, code: str) -> Exception:
    factory = cast(Callable[..., Exception], http_exception)
    return factory(status_code=status, detail={"code": code})


def _require_scan_id(
    scan_id: object,
    *,
    http_exception: type[Exception],
) -> str:
    if type(scan_id) is not str or _SCAN_ID_RE.fullmatch(scan_id) is None:
        raise _error(http_exception, 400, "lan_request_rejected")
    return scan_id


def _utc_text(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("LAN route timestamp is invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bounded_limits(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("LAN limits are invalid")
    allowed = set(asdict(LanScanLimits()))
    if any(type(key) is not str for key in value) or not set(value).issubset(allowed):
        raise ValueError("LAN limits are invalid")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("LAN limits are invalid")
    parsed = json.loads(encoded)
    if type(parsed) is not dict:
        raise ValueError("LAN limits are invalid")
    return cast(dict[str, object], parsed)


def _scan_payload(record: LanScanRecord) -> dict[str, object]:
    scan_id = record.scan_id
    status = record.status
    revision = record.revision
    if (
        type(scan_id) is not str
        or _SCAN_ID_RE.fullmatch(scan_id) is None
        or type(status) is not str
        or status
        not in {"draft", "running", "cancelling", "cancelled", "completed", "failed", "interrupted"}
        or type(revision) is not int
        or isinstance(revision, bool)
        or revision < 1
    ):
        raise ValueError("LAN scan record is invalid")
    return {
        "scan_id": scan_id,
        "status": status,
        "revision": revision,
        "confirmed_interface_id": record.confirmed_interface_id,
        "network": record.network,
        "limits": _bounded_limits(record.limits),
        "limits_digest": record.limits_digest,
        "preview_digest": record.preview_digest,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "cancel_reason": record.cancel_reason,
        "terminal_reason": record.terminal_reason,
        "candidate_count": record.candidate_count,
        "error_count": record.error_count,
        "timeout_count": record.timeout_count,
        "terminal_receipt_digest": record.terminal_receipt_digest,
    }


def _observation_payload(observation: LanObservationRecord) -> dict[str, object]:
    payload = bounded_observation_public_evidence(observation.public_payload)
    return {
        "scan_id": observation.scan_id,
        "endpoint_id": observation.endpoint_id,
        "source": observation.source,
        "interface_id": observation.interface_id,
        "address": observation.address,
        "port": observation.port,
        "api_shape": observation.api_shape,
        "tls_enabled": observation.tls_enabled,
        "certificate_sha256": observation.certificate_sha256,
        "catalog_digest": observation.catalog_digest,
        "capability_digest": observation.capability_digest,
        "public_payload": payload,
        "freshness_timestamp": observation.freshness_timestamp,
        "error_category": observation.error_category,
        "created_at": observation.created_at,
    }


def _preview_payload(authorization: LanPreviewAuthorization) -> dict[str, object]:
    preview = authorization.preview
    mdns = authorization.mdns_availability
    return {
        "interface_id": preview.interface_id,
        "network": preview.network,
        "limits": json.loads(json.dumps(asdict(preview.limits), separators=(",", ":"))),
        "active_host_count": preview.active_host_count,
        "passive_or_manual_only": preview.passive_or_manual_only,
        "port_count": len(preview.port_matrix),
        "mdns_status": mdns.value,
        "server_version": authorization.server_version,
        "contract_version": authorization.contract_version,
        "preview_digest": authorization.preview_digest,
        "issued_at": _utc_text(authorization.issued_at),
        "expires_at": _utc_text(authorization.expires_at),
    }


def _validate_timestamp(value: object) -> str:
    if type(value) is not str or len(value.encode("utf-8")) > 64:
        raise ValueError("LAN event timestamp is invalid")
    if _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("LAN event timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("LAN event timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("LAN event timestamp is invalid")
    if parsed.astimezone(UTC).isoformat().replace("+00:00", "Z") != value:
        raise ValueError("LAN event timestamp is invalid")
    return value


def _terminal_payload(event_type: str, value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "schema",
        "status",
        "terminal_reason",
        "cancel_reason",
    }:
        raise ValueError("LAN terminal event is invalid")
    if value.get("schema") != "kestrel.lan.scan-terminal.v2":
        raise ValueError("LAN terminal event is invalid")
    expected_status = event_type.removeprefix("scan_")
    if value.get("status") != expected_status:
        raise ValueError("LAN terminal event is invalid")
    reason = value.get("terminal_reason")
    cancel = value.get("cancel_reason")
    valid = (
        (expected_status == "completed" and reason == "scan_complete" and cancel is None)
        or (
            expected_status == "cancelled"
            and reason in {"owner_cancelled", "shutdown_cancelled"}
            and cancel == reason
        )
        or (
            expected_status == "failed"
            and reason in {"worker_error", "deadline_expired"}
            and cancel is None
        )
        or (
            expected_status == "interrupted"
            and reason == "startup_interrupted"
            and cancel in {None, "owner_cancelled", "shutdown_cancelled"}
        )
    )
    if not valid:
        raise ValueError("LAN terminal event is invalid")
    return {
        "status": expected_status,
        "terminal_reason": reason,
        "cancel_reason": cancel,
    }


def _public_event(event: LanScanEvent, *, scan_id: str) -> dict[str, object]:
    event_scan_id = event.scan_id
    sequence = event.sequence
    event_type = event.event_type
    if (
        event_scan_id != scan_id
        or type(sequence) is not int
        or isinstance(sequence, bool)
        or not 1 <= sequence <= _MAX_EVENT_SEQUENCE
        or type(event_type) is not str
        or event_type not in _ALLOWED_EVENT_TYPES
    ):
        raise ValueError("LAN event envelope is invalid")
    durable_payload = event.payload
    if event_type == "scan_started":
        bounded = bounded_scan_preview_event(durable_payload)
        if bounded.get("owner_principal") != LAN_OWNER_PRINCIPAL:
            raise ValueError("LAN event owner is invalid")
        payload = {
            key: value for key, value in bounded.items() if key not in {"schema", "owner_principal"}
        }
    elif event_type == "scan_progress":
        bounded = bounded_scan_progress_event(durable_payload)
        payload = {key: value for key, value in bounded.items() if key != "schema"}
    elif event_type == "scan_cancel_requested":
        if type(durable_payload) is not dict or set(durable_payload) != {"reason"}:
            raise ValueError("LAN cancel event is invalid")
        reason = durable_payload.get("reason")
        if type(reason) is not str or reason not in {"owner_cancelled", "shutdown_cancelled"}:
            raise ValueError("LAN cancel event is invalid")
        payload = {"reason": reason}
    else:
        payload = _terminal_payload(event_type, durable_payload)
    public = {
        "scan_id": scan_id,
        "sequence": sequence,
        "event_type": event_type,
        "payload": payload,
        "created_at": _validate_timestamp(event.created_at),
    }
    encoded = json.dumps(
        public,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    frame = f"id: {sequence}\nevent: {event_type}\ndata: {encoded}\n\n"
    if len(frame.encode("utf-8")) > _MAX_EVENT_FRAME_BYTES:
        raise ValueError("LAN event frame is too large")
    public["_frame"] = frame
    return public


def _validated_frames(
    events: object,
    *,
    scan_id: str,
    after_sequence: int,
    terminal_status: str | None = None,
) -> tuple[tuple[str, ...], int, str | None]:
    if type(events) not in {list, tuple}:
        raise ValueError("LAN event batch is invalid")
    untyped_events = cast(Sequence[object], events)
    if len(untyped_events) > _MAX_EVENT_BATCH or any(
        type(event) is not LanScanEvent for event in untyped_events
    ):
        raise ValueError("LAN event batch is invalid")
    typed_events = cast(Sequence[LanScanEvent], untyped_events)
    cursor = after_sequence
    if terminal_status is not None and terminal_status not in _TERMINAL_STATUSES:
        raise ValueError("LAN event terminal state is invalid")
    frames: list[str] = []
    for event in typed_events:
        if terminal_status is not None:
            raise ValueError("LAN event follows a terminal event")
        public = _public_event(event, scan_id=scan_id)
        sequence = public["sequence"]
        if type(sequence) is not int or sequence <= cursor:
            raise ValueError("LAN event sequence is invalid")
        cursor = sequence
        event_type = public["event_type"]
        if type(event_type) is str and event_type.startswith("scan_"):
            candidate = event_type.removeprefix("scan_")
            if candidate in _TERMINAL_STATUSES:
                if terminal_status is not None:
                    raise ValueError("LAN event stream has multiple terminals")
                terminal_status = candidate
        frame = public.pop("_frame")
        if type(frame) is not str:
            raise ValueError("LAN event frame is invalid")
        frames.append(frame)
    return tuple(frames), cursor, terminal_status


def _cursor_from_request(
    request: Request,
    *,
    http_exception: type[Exception],
) -> int:
    raw_values = [
        value
        for name, value in request.scope.get("headers", ())
        if isinstance(name, bytes) and name.decode("latin-1").lower() == "last-event-id"
    ]
    if len(raw_values) > 1:
        raise _error(http_exception, 400, "lan_event_cursor_invalid")
    if not raw_values:
        return 0
    try:
        value = raw_values[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise _error(http_exception, 400, "lan_event_cursor_invalid") from exc
    if value == "0":
        return 0
    if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        raise _error(http_exception, 400, "lan_event_cursor_invalid")
    try:
        cursor = int(value)
    except ValueError as exc:
        raise _error(http_exception, 400, "lan_event_cursor_invalid") from exc
    if cursor > _MAX_EVENT_SEQUENCE:
        raise _error(http_exception, 400, "lan_event_cursor_invalid")
    return cursor


async def _manager_call(function: Callable[..., Any], *args: object, **kwargs: object) -> Any:
    return await asyncio.to_thread(function, *args, **kwargs)


def register_lan_discovery_routes(
    app: Any,
    *,
    scan_manager: Any,
    http_exception: type[Exception],
    streaming_response: Callable[..., Any],
    stream_runtime: object | None = None,
) -> None:
    """Register the eight Task 7A automatic-discovery routes exactly once."""

    runtime = stream_runtime or SimpleNamespace(
        monotonic_clock=time.monotonic,
        sleep=asyncio.sleep,
    )
    monotonic_clock = getattr(runtime, "monotonic_clock", None)
    sleep = getattr(runtime, "sleep", None)
    if not callable(monotonic_clock) or not callable(sleep):
        raise TypeError("LAN stream runtime requires a clock and cancellable sleeper")
    leases = _StreamLeases()

    async def parse(request: Request, model: type[BaseModel]) -> BaseModel:
        return await _parse_lan_json_request(
            request,
            model,
            http_exception=http_exception,
        )

    async def require_get(request: Request) -> None:
        await _require_lan_get_request(request, http_exception=http_exception)

    @app.get("/api/routing/lan/interfaces")  # type: ignore[untyped-decorator]
    async def interfaces(request: Request) -> list[dict[str, object]]:
        await require_get(request)
        try:
            values = await _manager_call(scan_manager.interfaces)
            return [
                {
                    "interface_id": item.interface_id,
                    "display_name": item.display_name,
                    "addresses": list(item.addresses),
                }
                for item in tuple(values)[:64]
            ]
        except Exception as exc:
            if isinstance(exc, http_exception):
                raise
            raise _error(http_exception, 503, "lan_scan_unavailable") from None

    @app.post("/api/routing/lan/preview")  # type: ignore[untyped-decorator]
    async def preview(request: Request) -> dict[str, object]:
        parsed = await parse(request, _PreviewRequest)
        assert isinstance(parsed, _PreviewRequest)
        try:
            authorization = await _manager_call(
                scan_manager.preview,
                parsed.interface_id,
                parsed.network,
            )
            return _preview_payload(authorization)
        except LanPreviewAuthorizationError as exc:
            raise _error(http_exception, 409, "lan_preview_conflict") from exc
        except ValueError as exc:
            raise _error(http_exception, 400, "lan_scope_invalid") from exc
        except Exception as exc:
            if isinstance(exc, http_exception):
                raise
            raise _error(http_exception, 503, "lan_scan_unavailable") from None

    @app.post("/api/routing/lan/scans", status_code=201)  # type: ignore[untyped-decorator]
    async def create_scan(request: Request) -> dict[str, object]:
        parsed = await parse(request, _CreateScanRequest)
        assert isinstance(parsed, _CreateScanRequest)
        try:
            record = await _manager_call(
                scan_manager.create_draft_for_preview,
                parsed.preview_digest,
                expected_revision=parsed.expected_revision,
            )
            return _scan_payload(record)
        except LanPreviewAuthorizationError as exc:
            raise _error(http_exception, 409, "lan_preview_conflict") from exc
        except LanScanRevisionConflict as exc:
            raise _error(http_exception, 409, "lan_scan_revision_conflict") from exc
        except Exception as exc:
            if isinstance(exc, http_exception):
                raise
            raise _error(http_exception, 503, "lan_scan_unavailable") from None

    @app.post("/api/routing/lan/scans/{scan_id}/start", status_code=202)  # type: ignore[untyped-decorator]
    async def start_scan(scan_id: str, request: Request) -> dict[str, object]:
        identifier = _require_scan_id(scan_id, http_exception=http_exception)
        parsed = await parse(request, _StartScanRequest)
        assert isinstance(parsed, _StartScanRequest)
        try:
            record = await _manager_call(
                scan_manager.start_for_preview,
                identifier,
                expected_revision=parsed.expected_revision,
                preview_digest=parsed.preview_digest,
            )
            return _scan_payload(record)
        except KeyError as exc:
            raise _error(http_exception, 404, "lan_scan_not_found") from exc
        except LanPreviewAuthorizationError as exc:
            raise _error(http_exception, 409, "lan_preview_conflict") from exc
        except LanScanRevisionConflict as exc:
            raise _error(http_exception, 409, "lan_scan_revision_conflict") from exc
        except LanScanTransitionError as exc:
            raise _error(http_exception, 409, "lan_scan_transition_conflict") from exc
        except LanScanAdmissionConflict as exc:
            raise _error(http_exception, 409, "lan_scan_active_conflict") from exc
        except Exception as exc:
            if isinstance(exc, http_exception):
                raise
            raise _error(http_exception, 503, "lan_scan_unavailable") from None

    @app.get("/api/routing/lan/scans")  # type: ignore[untyped-decorator]
    async def list_scans(request: Request) -> list[dict[str, object]]:
        await require_get(request)
        try:
            records = await _manager_call(scan_manager.list, limit=_MAX_SCAN_SUMMARIES)
            return [_scan_payload(item) for item in list(records)[:_MAX_SCAN_SUMMARIES]]
        except Exception as exc:
            if isinstance(exc, http_exception):
                raise
            raise _error(http_exception, 503, "lan_scan_unavailable") from None

    @app.get("/api/routing/lan/scans/{scan_id}")  # type: ignore[untyped-decorator]
    async def scan_detail(scan_id: str, request: Request) -> dict[str, object]:
        identifier = _require_scan_id(scan_id, http_exception=http_exception)
        await require_get(request)
        try:
            page = await _manager_call(
                scan_manager.observation_page,
                identifier,
                limit=_MAX_OBSERVATIONS,
            )
            if page is None:
                raise _error(http_exception, 404, "lan_scan_not_found")
            observations = list(page.observations)[:_MAX_OBSERVATIONS]
            total = page.total_count
            truncated = page.truncated
            payload = _scan_payload(page.scan)
            payload.update(
                {
                    "observations": [_observation_payload(item) for item in observations],
                    "observation_total_count": total,
                    "observations_truncated": bool(truncated or total > len(observations)),
                }
            )
            return payload
        except Exception as exc:
            if isinstance(exc, http_exception):
                raise
            raise _error(http_exception, 503, "lan_scan_unavailable") from None

    @app.post("/api/routing/lan/scans/{scan_id}/cancel", status_code=202)  # type: ignore[untyped-decorator]
    async def cancel_scan(scan_id: str, request: Request) -> dict[str, object]:
        identifier = _require_scan_id(scan_id, http_exception=http_exception)
        parsed = await parse(request, _CancelScanRequest)
        assert isinstance(parsed, _CancelScanRequest)
        try:
            record = await _manager_call(
                scan_manager.cancel,
                identifier,
                expected_revision=parsed.expected_revision,
            )
            return _scan_payload(record)
        except KeyError as exc:
            raise _error(http_exception, 404, "lan_scan_not_found") from exc
        except LanScanRevisionConflict as exc:
            raise _error(http_exception, 409, "lan_scan_revision_conflict") from exc
        except LanScanTransitionError as exc:
            raise _error(http_exception, 409, "lan_scan_transition_conflict") from exc
        except Exception as exc:
            if isinstance(exc, http_exception):
                raise
            raise _error(http_exception, 503, "lan_scan_unavailable") from None

    @app.get("/api/routing/lan/scans/{scan_id}/events")  # type: ignore[untyped-decorator]
    async def scan_events(scan_id: str, request: Request) -> Any:
        identifier = _require_scan_id(scan_id, http_exception=http_exception)
        await _require_lan_get_request(
            request,
            http_exception=http_exception,
            query_error_code="lan_event_cursor_invalid",
        )
        cursor = _cursor_from_request(request, http_exception=http_exception)
        try:
            record = await _manager_call(scan_manager.get, identifier)
            if record is None:
                raise _error(http_exception, 404, "lan_scan_not_found")
            initial = await _manager_call(
                scan_manager.events,
                identifier,
                after_sequence=cursor,
                limit=_MAX_EVENT_BATCH,
            )
            initial_frames, initial_cursor, initial_terminal = _validated_frames(
                initial,
                scan_id=identifier,
                after_sequence=cursor,
            )
            record_terminal = bool(getattr(record, "is_terminal", False))
            record_status = getattr(record, "status", None)
            if initial_terminal is not None and initial_terminal != record_status:
                raise ValueError("LAN terminal event disagrees with scan")
        except Exception as exc:
            if isinstance(exc, http_exception):
                raise
            if isinstance(exc, ValueError):
                raise _error(http_exception, 409, "lan_event_invalid") from None
            raise _error(http_exception, 503, "lan_scan_unavailable") from None
        if not leases.acquire():
            raise _error(http_exception, 429, "lan_event_stream_limit")

        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                leases.release()

        async def stream() -> AsyncIterator[str]:
            current_cursor = initial_cursor
            event_terminal = initial_terminal
            terminal_record_seen = record_terminal
            terminal_record_status = record_status if record_terminal else None
            frames = initial_frames
            try:
                last_heartbeat = float(monotonic_clock())
                for frame in frames:
                    yield frame
                while True:
                    if event_terminal is not None:
                        final_events = await _manager_call(
                            scan_manager.events,
                            identifier,
                            after_sequence=current_cursor,
                            limit=_MAX_EVENT_BATCH,
                        )
                        final_frames, current_cursor, event_terminal = _validated_frames(
                            final_events,
                            scan_id=identifier,
                            after_sequence=current_cursor,
                            terminal_status=event_terminal,
                        )
                        for frame in final_frames:
                            yield frame
                        return

                    if terminal_record_seen:
                        final_events = await _manager_call(
                            scan_manager.events,
                            identifier,
                            after_sequence=current_cursor,
                            limit=_MAX_EVENT_BATCH,
                        )
                        final_frames, current_cursor, event_terminal = _validated_frames(
                            final_events,
                            scan_id=identifier,
                            after_sequence=current_cursor,
                        )
                        if event_terminal is not None and event_terminal != terminal_record_status:
                            return
                        for frame in final_frames:
                            yield frame
                        if not final_frames:
                            return
                        continue

                    now = float(monotonic_clock())
                    elapsed = now - last_heartbeat
                    if elapsed >= _HEARTBEAT_SECONDS:
                        yield ": heartbeat\n\n"
                        last_heartbeat = now
                    delay = min(_POLL_SECONDS, max(0.001, _HEARTBEAT_SECONDS - elapsed))
                    await sleep(delay)
                    await asyncio.sleep(0)
                    if await request.is_disconnected():
                        return
                    next_events = await _manager_call(
                        scan_manager.events,
                        identifier,
                        after_sequence=current_cursor,
                        limit=_MAX_EVENT_BATCH,
                    )
                    next_frames, current_cursor, event_terminal = _validated_frames(
                        next_events,
                        scan_id=identifier,
                        after_sequence=current_cursor,
                    )
                    current = await _manager_call(scan_manager.get, identifier)
                    if current is None:
                        return
                    current_terminal = bool(getattr(current, "is_terminal", False))
                    current_status = getattr(current, "status", None)
                    if event_terminal is not None and event_terminal != current_status:
                        return
                    for frame in next_frames:
                        yield frame
                    if current_terminal:
                        terminal_record_seen = True
                        terminal_record_status = current_status
            except Exception:
                return
            finally:
                release()

        try:
            return streaming_response(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store, no-transform",
                    "X-Accel-Buffering": "no",
                },
            )
        except BaseException:
            release()
            raise
