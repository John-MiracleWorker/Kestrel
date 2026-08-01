"""Canonical, secret-safe serialization for LAN discovery evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from dataclasses import asdict
from typing import Any

from ..lan_discovery_models import LanScanLimits
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
_OBSERVATION_PUBLIC_FIELDS = frozenset(
    {
        "service",
        "service_version",
        "model_count",
        "model_ids",
        "capabilities",
        "metadata",
    }
)
_OBSERVATION_METADATA_FIELDS = frozenset(
    {"display_name", "vendor", "product", "description"}
)
_EVENT_PUBLIC_FIELDS = frozenset(
    {
        "address",
        "candidate_count",
        "completed",
        "endpoint_id",
        "error_category",
        "error_count",
        "interface_id",
        "message",
        "model_count",
        "network",
        "ok",
        "phase",
        "port",
        "reason",
        "source",
        "status",
        "timeout_count",
        "total",
    }
)


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


def bounded_observation_public_evidence(value: object) -> dict[str, Any]:
    payload = _require_string_keyed_object(value, "observation public evidence")
    unknown = sorted(set(payload) - _OBSERVATION_PUBLIC_FIELDS)
    if unknown:
        raise ValueError(
            "unsupported observation public evidence field: " + ", ".join(unknown)
        )
    result: dict[str, Any] = {}
    for key, item in payload.items():
        if key in {"service", "service_version"}:
            result[key] = _bounded_evidence_text(item, key, maximum=128)
        elif key == "model_count":
            result[key] = validate_non_negative_count(item, "model_count")
        elif key in {"model_ids", "capabilities"}:
            result[key] = _bounded_evidence_text_list(item, key)
        elif key == "metadata":
            result[key] = _bounded_observation_metadata(item)
    return _bounded_json_object(
        result,
        kind="observation public evidence",
        max_bytes=MAX_PUBLIC_PAYLOAD_BYTES,
    )


def bounded_event_public_evidence(value: object) -> dict[str, Any]:
    payload = _require_string_keyed_object(value, "event public evidence")
    unknown = sorted(set(payload) - _EVENT_PUBLIC_FIELDS)
    if unknown:
        raise ValueError("unsupported event public evidence field: " + ", ".join(unknown))
    result: dict[str, Any] = {}
    for key, item in payload.items():
        if key in {
            "candidate_count",
            "completed",
            "error_count",
            "model_count",
            "port",
            "timeout_count",
            "total",
        }:
            result[key] = validate_non_negative_count(item, key)
        elif key == "ok":
            if not isinstance(item, bool):
                raise ValueError("event public evidence ok must be boolean")
            result[key] = item
        else:
            result[key] = _bounded_evidence_text(item, key, maximum=512)
    return _bounded_json_object(
        result,
        kind="event public evidence",
        max_bytes=MAX_EVENT_PAYLOAD_BYTES,
    )


def bounded_scan_limits(value: object) -> dict[str, Any]:
    payload = _require_string_keyed_object(value, "scan limits")
    expected_json = canonical_json(asdict(LanScanLimits()))
    supplied_json = canonical_json(payload)
    if supplied_json != expected_json:
        raise ValueError("LAN scan limits must match the fixed bounded limits")
    return _bounded_json_object(
        json.loads(expected_json),
        kind="scan limits",
        max_bytes=MAX_LIMITS_BYTES,
    )


def _bounded_json_object(
    value: object,
    *,
    kind: str = "public payload",
    max_bytes: int = MAX_PUBLIC_PAYLOAD_BYTES,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"LAN {kind} must be a JSON object")
    validated = _validate_json(value, depth=0, kind=kind)
    encoded = canonical_json(validated)
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
        "public_payload": bounded_observation_public_evidence(draft.public_payload),
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


def _require_string_keyed_object(value: object, kind: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{kind} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{kind} requires string keys")
    return dict(value)


def _bounded_evidence_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"public evidence {field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(
            f"public evidence {field} must be between 1 and {maximum} characters"
        )
    return normalized


def _bounded_evidence_text_list(value: object, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        raise ValueError(f"public evidence {field} must be a list of at most 64 strings")
    return [_bounded_evidence_text(item, field, maximum=256) for item in value]


def _bounded_observation_metadata(value: object) -> dict[str, str]:
    metadata = _require_string_keyed_object(value, "observation public evidence metadata")
    unknown = sorted(set(metadata) - _OBSERVATION_METADATA_FIELDS)
    if unknown:
        raise ValueError(
            "unsupported observation public evidence metadata field: "
            + ", ".join(unknown)
        )
    return {
        key: _bounded_evidence_text(item, f"metadata.{key}", maximum=512)
        for key, item in metadata.items()
    }


def _validate_json(value: object, *, depth: int, kind: str) -> object:
    if depth > 8:
        raise ValueError(f"LAN {kind} exceeds maximum nesting depth")
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValueError(f"LAN {kind} requires string keys")
            result[raw_key] = _validate_json(item, depth=depth + 1, kind=kind)
        return result
    if isinstance(value, (list, tuple)):
        return [_validate_json(item, depth=depth + 1, kind=kind) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"LAN {kind} contains a non-JSON value")


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
