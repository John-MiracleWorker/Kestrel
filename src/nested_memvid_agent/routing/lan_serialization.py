"""Canonical, secret-safe serialization for LAN discovery evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from typing import Any

from .lan_records import (
    SCAN_STATES,
    LanObservationDraft,
    LanObservationRecord,
    LanScanEvent,
    LanScanRecord,
)

MAX_PUBLIC_PAYLOAD_BYTES = 16_384
MAX_EVENT_PAYLOAD_BYTES = 8_192
MAX_LIMITS_BYTES = 16_384
MAX_RECEIPT_BYTES = 1_048_576

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SECRET_KEYS = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "client_secret",
    "private_key",
}
_SECRET_SUFFIXES = ("_password", "_secret", "_token", "_api_key", "_apikey")


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("LAN evidence must be deterministic JSON") from exc


def sha256_digest(value: object) -> str:
    encoded = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def bounded_public_payload(
    value: object,
    *,
    kind: str = "public payload",
    max_bytes: int = MAX_PUBLIC_PAYLOAD_BYTES,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"LAN {kind} must be a JSON object")
    redacted = _redact_json(value, depth=0)
    encoded = canonical_json(redacted)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"LAN bounded {kind} exceeds {max_bytes} bytes")
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise ValueError(f"LAN {kind} must be a JSON object")
    return parsed


def validate_digest(value: str | None, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


def validate_required_text(value: object, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum} characters")
    return normalized


def validate_optional_text(
    value: object,
    field: str,
    *,
    maximum: int = 512,
) -> str | None:
    if value is None:
        return None
    return validate_required_text(value, field, maximum=maximum)


def validate_non_negative_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def normalize_network(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=True))
    except ValueError as exc:
        raise ValueError("network must be a canonical network") from exc


def normalize_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("observation address must be a literal IP address") from exc
    return str(address)


def validate_observation(draft: LanObservationDraft) -> dict[str, Any]:
    endpoint_id = validate_digest(draft.endpoint_id, "endpoint_id")
    interface_id = validate_digest(draft.interface_id, "interface_id")
    if draft.source not in {"mdns", "active", "manual"}:
        raise ValueError("observation source must be mdns, active, or manual")
    if isinstance(draft.port, bool) or not isinstance(draft.port, int) or not 1 <= draft.port <= 65535:
        raise ValueError("observation port must be between 1 and 65535")
    if not isinstance(draft.tls_enabled, bool):
        raise ValueError("observation tls_enabled must be boolean")
    certificate = validate_digest(
        draft.certificate_sha256,
        "certificate_sha256",
        optional=True,
    )
    if certificate is not None and not draft.tls_enabled:
        raise ValueError("certificate evidence requires TLS")
    return {
        "endpoint_id": endpoint_id,
        "source": draft.source,
        "interface_id": interface_id,
        "address": normalize_address(draft.address),
        "port": draft.port,
        "api_shape": validate_optional_text(draft.api_shape, "api_shape", maximum=128),
        "tls_enabled": draft.tls_enabled,
        "certificate_sha256": certificate,
        "catalog_digest": validate_digest(
            draft.catalog_digest,
            "catalog_digest",
            optional=True,
        ),
        "capability_digest": validate_digest(
            draft.capability_digest,
            "capability_digest",
            optional=True,
        ),
        "public_payload": bounded_public_payload(draft.public_payload),
        "freshness_timestamp": validate_required_text(
            draft.freshness_timestamp,
            "freshness_timestamp",
            maximum=64,
        ),
        "error_category": validate_optional_text(
            draft.error_category,
            "error_category",
            maximum=128,
        ),
    }


def scan_from_row(row: sqlite3.Row) -> LanScanRecord:
    status = str(row["status"])
    if status not in SCAN_STATES:
        raise ValueError(f"unknown durable LAN scan status: {status}")
    return LanScanRecord(
        scan_id=str(row["scan_id"]),
        status=status,  # type: ignore[arg-type]
        revision=int(row["revision"]),
        owner_principal=str(row["owner_principal"]),
        confirmed_interface_id=str(row["confirmed_interface_id"]),
        network=str(row["network"]),
        limits=_json_object(row["limits_json"]),
        limits_digest=str(row["limits_digest"]),
        preview_digest=str(row["preview_digest"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=_optional_str(row["started_at"]),
        finished_at=_optional_str(row["finished_at"]),
        cancel_reason=_optional_str(row["cancel_reason"]),
        terminal_reason=_optional_str(row["terminal_reason"]),
        candidate_count=_optional_int(row["candidate_count"]),
        error_count=_optional_int(row["error_count"]),
        timeout_count=_optional_int(row["timeout_count"]),
        terminal_receipt=(
            None
            if row["terminal_receipt_json"] is None
            else _json_object(row["terminal_receipt_json"])
        ),
        terminal_receipt_digest=_optional_str(row["terminal_receipt_digest"]),
    )


def observation_from_row(row: sqlite3.Row) -> LanObservationRecord:
    source = str(row["source"])
    if source not in {"mdns", "active", "manual"}:
        raise ValueError(f"unknown durable LAN observation source: {source}")
    return LanObservationRecord(
        scan_id=str(row["scan_id"]),
        endpoint_id=str(row["endpoint_id"]),
        source=source,  # type: ignore[arg-type]
        interface_id=str(row["interface_id"]),
        address=str(row["address"]),
        port=int(row["port"]),
        api_shape=_optional_str(row["api_shape"]),
        tls_enabled=bool(row["tls_enabled"]),
        certificate_sha256=_optional_str(row["certificate_sha256"]),
        catalog_digest=_optional_str(row["catalog_digest"]),
        capability_digest=_optional_str(row["capability_digest"]),
        public_payload=_json_object(row["public_payload_json"]),
        freshness_timestamp=str(row["freshness_timestamp"]),
        error_category=_optional_str(row["error_category"]),
        created_at=str(row["created_at"]),
    )


def event_from_row(row: sqlite3.Row) -> LanScanEvent:
    return LanScanEvent(
        scan_id=str(row["scan_id"]),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),
        payload=_json_object(row["payload_json"]),
        created_at=str(row["created_at"]),
    )


def _redact_json(value: object, *, depth: int) -> object:
    if depth > 8:
        raise ValueError("LAN public payload exceeds maximum nesting depth")
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS or any(
                normalized.endswith(suffix) for suffix in _SECRET_SUFFIXES
            ):
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_json(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("LAN public payload contains a non-JSON value")


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("durable LAN JSON value is not an object")
    return parsed


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str, bytes, bytearray)):
        raise ValueError("durable LAN integer has an unsupported type")
    return int(value)
