"""Canonical, secret-safe serialization for LAN discovery evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast

from ..lan_discovery_models import (
    KNOWN_MODEL_SERVICE_PORTS,
    MAX_ACTIVE_HOSTS,
    MAX_DISCOVERED_MODELS,
    LanScanLimits,
    ManualLanEndpoint,
    ResolvedLanEndpoint,
)
from ..lan_discovery_scope import PrivateScanScope
from ..lan_scanner import (
    ApiShape,
    CapabilityName,
    CapabilityObservationStatus,
    CapabilityProvenance,
    LanCapabilityEvidence,
    LanEndpointObservation,
    LanFailureCategory,
    Reachability,
    TransportSecurity,
    _make_observation,
)
from .lan_records import (
    SCAN_STATES,
    LanObservationDraft,
    LanObservationRecord,
    LanObservationSource,
    LanScanEvent,
    LanScanRecord,
    LanScanRevisionConflict,
)

MAX_PUBLIC_PAYLOAD_BYTES = 16_384
MAX_EVENT_PAYLOAD_BYTES = 8_192
MAX_LIMITS_BYTES = 16_384
MAX_RECEIPT_BYTES = 1_048_576
MAX_LAN_SCAN_OBSERVATIONS = MAX_ACTIVE_HOSTS * len(KNOWN_MODEL_SERVICE_PORTS)
LAN_OBSERVATION_MAX_AGE_SECONDS = 300
LAN_OBSERVATION_MAX_FUTURE_SKEW_SECONDS = 5

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RFC3339_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")
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
_OBSERVATION_METADATA_FIELDS = frozenset({"display_name", "vendor", "product", "description"})
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
_TASK4_OBSERVATION_SCHEMA = "kestrel.lan.durable-observation.v1"
LAN_SCAN_RECEIPT_V2_SCHEMA = "kestrel.lan.scan-receipt.v2"
LAN_OBSERVATION_MEMBERSHIP_SCHEMA = "kestrel.lan.observation-membership.v1"
LAN_SCAN_PREVIEW_EVENT_SCHEMA = "kestrel.lan.scan-preview.v1"
LAN_MANUAL_SCAN_PREVIEW_EVENT_SCHEMA = "kestrel.lan.scan-preview.manual.v1"
_LAN_MANUAL_PREVIEW_CONTRACT_VERSION = "kestrel.lan.manual-preview-authorization.v1"
_LAN_SERVER_VERSION = "kestrel-local-runtime-v1"
LAN_SCAN_PROGRESS_EVENT_SCHEMA = "kestrel.lan.scan-progress.v1"
LAN_SCAN_TERMINAL_EVENT_SCHEMA = "kestrel.lan.scan-terminal.v2"
LAN_MDNS_STATUSES = frozenset({"available", "unavailable", "timed_out"})
LAN_CANCEL_REASONS = frozenset({"owner_cancelled", "shutdown_cancelled"})
LAN_TERMINAL_REASONS = frozenset(
    {
        "scan_complete",
        "owner_cancelled",
        "shutdown_cancelled",
        "deadline_expired",
        "worker_error",
        "startup_interrupted",
        "worker_interrupted",
    }
)
_TASK4_OBSERVATION_PUBLIC_FIELDS = frozenset(
    {
        "schema",
        "endpoint_binding_digest",
        "observation_digest",
        "reachability",
        "transport_security",
        "api_shape",
        "catalog_complete",
        "catalog_truncated",
        "model_ids",
        "capability_route",
        "selected_model_id",
        "capabilities",
        "failure_category",
    }
)
_PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_PRIVATE_IPV6_NETWORKS = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)


@dataclass(frozen=True)
class AuthenticatedLanObservation:
    """One receipt-bound Task 4 observation reconstructed from durable rows."""

    scan_id: str
    owner_principal: str
    confirmed_network: str
    terminal_receipt_digest: str
    observed_at: datetime
    source: LanObservationSource
    observation: LanEndpointObservation


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


def validate_exact_revision(
    value: object,
    *,
    actual_revision: int,
    scan_id: str,
) -> int:
    """Require an exact integer CAS revision, rejecting bool-as-int aliases."""

    if type(value) is not int or value != actual_revision:
        raise LanScanRevisionConflict(scan_id, actual_revision)
    return value


def observation_membership_digest(
    observations: Iterable[LanObservationDraft | LanObservationRecord],
) -> str:
    """Bind canonical endpoint identities to exact durable observation evidence."""

    members: dict[str, str] = {}
    for observation in observations:
        if type(observation) not in {LanObservationDraft, LanObservationRecord}:
            raise ValueError("LAN observation membership requires exact durable records")
        draft = (
            observation
            if type(observation) is LanObservationDraft
            else LanObservationDraft(
                endpoint_id=observation.endpoint_id,
                source=observation.source,
                interface_id=observation.interface_id,
                address=observation.address,
                port=observation.port,
                api_shape=observation.api_shape,
                tls_enabled=observation.tls_enabled,
                certificate_sha256=observation.certificate_sha256,
                catalog_digest=observation.catalog_digest,
                capability_digest=observation.capability_digest,
                public_payload=observation.public_payload,
                freshness_timestamp=observation.freshness_timestamp,
                error_category=observation.error_category,
            )
        )
        values = validate_observation(draft)
        endpoint_id = str(values["endpoint_id"])
        payload = values["public_payload"]
        if type(payload) is dict and payload.get("schema") == _TASK4_OBSERVATION_SCHEMA:
            observation_digest = validate_digest(
                payload.get("observation_digest"),
                "observation_digest",
            )
            assert observation_digest is not None
        else:
            observation_digest = sha256_digest(
                {
                    "schema": "kestrel.lan.observation-projection.v1",
                    **values,
                }
            )
        member: dict[str, str] = {
            "endpoint_id": endpoint_id,
            "observation_digest": observation_digest,
        }
        if draft.source == "manual":
            member.update(
                {
                    "observation_source": "manual",
                    "endpoint_kind": "manual",
                }
            )
        prior_member = members.get(endpoint_id)
        canonical_member = canonical_json(member)
        if prior_member is not None and prior_member != canonical_member:
            raise ValueError(f"conflicting LAN observation endpoint: {endpoint_id}")
        members[endpoint_id] = canonical_member
    ordered = [json.loads(members[endpoint_id]) for endpoint_id in sorted(members)]
    return sha256_digest(
        {
            "schema": LAN_OBSERVATION_MEMBERSHIP_SCHEMA,
            "count": len(ordered),
            "members": ordered,
        }
    )


def bounded_scan_preview_event(value: object) -> dict[str, Any]:
    payload = _require_string_keyed_object(value, "scan preview event")
    if payload.get("schema") == LAN_MANUAL_SCAN_PREVIEW_EVENT_SCHEMA:
        return _bounded_manual_scan_preview_event(payload)
    expected = {
        "schema",
        "owner_principal",
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
    }
    if set(payload) != expected or payload.get("schema") != LAN_SCAN_PREVIEW_EVENT_SCHEMA:
        raise ValueError("LAN scan preview event has an invalid field set")
    passive = payload["passive_or_manual_only"]
    if type(passive) is not bool:
        raise ValueError("passive_or_manual_only must be boolean")
    mdns_status = payload["mdns_status"]
    if type(mdns_status) is not str or mdns_status not in LAN_MDNS_STATUSES:
        raise ValueError("LAN mDNS status is invalid")
    interface_id = payload["interface_id"]
    preview_digest = payload["preview_digest"]
    if type(interface_id) is not str or type(preview_digest) is not str:
        raise ValueError("LAN preview digest binding is invalid")
    result = {
        "schema": LAN_SCAN_PREVIEW_EVENT_SCHEMA,
        "owner_principal": validate_required_text(
            payload["owner_principal"], "owner_principal", maximum=256
        ),
        "interface_id": validate_digest(interface_id, "interface_id"),
        "network": normalize_network(payload["network"]),  # type: ignore[arg-type]
        "limits": bounded_scan_limits(payload["limits"]),
        "active_host_count": validate_non_negative_count(
            payload["active_host_count"], "active_host_count"
        ),
        "passive_or_manual_only": passive,
        "port_count": validate_non_negative_count(payload["port_count"], "port_count"),
        "mdns_status": mdns_status,
        "server_version": validate_required_text(
            payload["server_version"], "server_version", maximum=128
        ),
        "contract_version": validate_required_text(
            payload["contract_version"], "contract_version", maximum=128
        ),
        "preview_digest": validate_digest(preview_digest, "preview_digest"),
        "expires_at": _normalize_observation_timestamp(payload["expires_at"]),
    }
    return _bounded_json_object(
        result,
        kind="scan preview event",
        max_bytes=MAX_EVENT_PAYLOAD_BYTES,
    )


def _bounded_manual_scan_preview_event(payload: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema",
        "mode",
        "endpoint_kind",
        "observation_source",
        "owner_principal",
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
    }
    if set(payload) != expected:
        raise ValueError("LAN manual scan preview event has an invalid field set")
    if (
        payload["mode"] != "manual"
        or payload["endpoint_kind"] != "manual"
        or payload["observation_source"] != "manual"
        or payload["active_host_count"] != 1
        or payload["passive_or_manual_only"] is not True
        or payload["port_count"] != 1
        or payload["mdns_status"] != "unavailable"
        or payload["confirmed"] is not True
        or payload["privacy_acknowledged"] is not True
    ):
        raise ValueError("LAN manual scan preview event binding is invalid")
    interface_id = validate_digest(payload["interface_id"], "interface_id")
    preview_digest = validate_digest(payload["preview_digest"], "preview_digest")
    if interface_id is None or preview_digest is None:
        raise ValueError("LAN manual preview digest binding is invalid")
    limits = bounded_scan_limits(payload["limits"])
    exact_port = payload["exact_port"]
    if type(exact_port) is not int or not 1 <= exact_port <= 65535:
        raise ValueError("LAN manual scan port is invalid")
    if limits.get("mode") != "manual" or limits.get("exact_port") != exact_port:
        raise ValueError("LAN manual scan preview limits do not match exact port")
    network = normalize_network(payload["network"])
    parsed_network = ipaddress.ip_network(network, strict=True)
    if parsed_network.prefixlen != parsed_network.max_prefixlen:
        raise ValueError("LAN manual scan network must be an exact host scope")
    return _bounded_json_object(
        {
            "schema": LAN_MANUAL_SCAN_PREVIEW_EVENT_SCHEMA,
            "mode": "manual",
            "endpoint_kind": "manual",
            "observation_source": "manual",
            "owner_principal": validate_required_text(
                payload["owner_principal"], "owner_principal", maximum=256
            ),
            "interface_id": interface_id,
            "network": network,
            "limits": limits,
            "active_host_count": 1,
            "passive_or_manual_only": True,
            "port_count": 1,
            "exact_port": exact_port,
            "mdns_status": "unavailable",
            "server_version": validate_required_text(
                payload["server_version"], "server_version", maximum=128
            ),
            "contract_version": validate_required_text(
                payload["contract_version"], "contract_version", maximum=128
            ),
            "preview_digest": preview_digest,
            "expires_at": _normalize_observation_timestamp(payload["expires_at"]),
            "confirmed": True,
            "privacy_acknowledged": True,
        },
        kind="manual scan preview event",
        max_bytes=MAX_EVENT_PAYLOAD_BYTES,
    )


def bounded_scan_progress_event(value: object) -> dict[str, Any]:
    payload = _require_string_keyed_object(value, "scan progress event")
    expected = {
        "schema",
        "planned_count",
        "admitted_count",
        "completed_count",
        "persisted_observation_count",
        "error_category_counts",
        "timeout_count",
        "mdns_status",
    }
    if set(payload) != expected or payload.get("schema") != LAN_SCAN_PROGRESS_EVENT_SCHEMA:
        raise ValueError("LAN scan progress event has an invalid field set")
    counts = {
        field: validate_non_negative_count(payload[field], field)
        for field in (
            "planned_count",
            "admitted_count",
            "completed_count",
            "persisted_observation_count",
            "timeout_count",
        )
    }
    if not (counts["completed_count"] <= counts["admitted_count"] <= counts["planned_count"]):
        raise ValueError("LAN scan progress counts are inconsistent")
    if counts["planned_count"] > MAX_LAN_SCAN_OBSERVATIONS:
        raise ValueError("LAN scan planned count exceeds the fixed observation ceiling")
    if counts["persisted_observation_count"] > counts["completed_count"]:
        raise ValueError("persisted observation count exceeds completed count")
    error_counts = bounded_error_category_counts(payload["error_category_counts"])
    if sum(error_counts.values()) > counts["completed_count"]:
        raise ValueError("LAN error counts exceed completed count")
    if counts["timeout_count"] > sum(error_counts.values()):
        raise ValueError("timeout_count exceeds error count")
    mdns_status = payload["mdns_status"]
    if type(mdns_status) is not str or mdns_status not in LAN_MDNS_STATUSES:
        raise ValueError("LAN mDNS status is invalid")
    return _bounded_json_object(
        {
            "schema": LAN_SCAN_PROGRESS_EVENT_SCHEMA,
            **counts,
            "error_category_counts": error_counts,
            "mdns_status": mdns_status,
        },
        kind="scan progress event",
        max_bytes=MAX_EVENT_PAYLOAD_BYTES,
    )


def bounded_error_category_counts(value: object) -> dict[str, int]:
    payload = _require_string_keyed_object(value, "error category counts")
    allowed = {item.value for item in LanFailureCategory}
    if any(key not in allowed for key in payload):
        raise ValueError("LAN error category is invalid")
    return {
        key: validate_non_negative_count(payload[key], f"error_category_counts.{key}")
        for key in sorted(payload)
        if validate_non_negative_count(payload[key], f"error_category_counts.{key}") > 0
    }


def bounded_observation_public_evidence(value: object) -> dict[str, Any]:
    payload = _require_string_keyed_object(value, "observation public evidence")
    if payload.get("schema") == _TASK4_OBSERVATION_SCHEMA:
        return _bounded_task4_observation_public_evidence(payload)
    unknown = sorted(set(payload) - _OBSERVATION_PUBLIC_FIELDS)
    if unknown:
        raise ValueError("unsupported observation public evidence field: " + ", ".join(unknown))
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


def lan_observation_to_draft(
    observation: LanEndpointObservation,
    *,
    scope: PrivateScanScope,
    freshness_timestamp: str,
    source: LanObservationSource = "active",
) -> LanObservationDraft:
    """Project one exact Task 4 observation into durable secret-safe evidence."""

    if type(observation) is not LanEndpointObservation:
        raise ValueError("LAN adapter requires an exact typed observation")
    if type(scope) is not PrivateScanScope:
        raise ValueError("LAN adapter requires an exact confirmed scope")
    canonical_scope = PrivateScanScope.from_request(scope.interface, scope.network)
    if canonical_scope != scope:
        raise ValueError("LAN adapter scope is not canonical")
    if type(observation.endpoint) is ResolvedLanEndpoint:
        if source == "manual":
            raise ValueError("LAN observation source and endpoint kind disagree")
        canonical_endpoint: ResolvedLanEndpoint | ManualLanEndpoint = (
            ResolvedLanEndpoint.from_scope(
                canonical_scope,
                observation.endpoint.address,
                observation.endpoint.port,
            )
        )
    elif type(observation.endpoint) is ManualLanEndpoint:
        if source != "manual":
            raise ValueError("LAN observation source and endpoint kind disagree")
        canonical_endpoint = ManualLanEndpoint.from_exact_scope(
            canonical_scope,
            observation.endpoint.address,
            observation.endpoint.port,
        )
    else:
        raise ValueError("LAN observation endpoint kind is invalid")
    if canonical_endpoint != observation.endpoint:
        raise ValueError("LAN observation endpoint does not match confirmed scope")
    if source not in {"mdns", "active", "manual"}:
        raise ValueError("LAN observation source is invalid")

    capabilities: list[LanCapabilityEvidence] = []
    if type(observation.capabilities) is not tuple:
        raise ValueError("LAN capability evidence must be an exact tuple")
    for evidence in observation.capabilities:
        if type(evidence) is not LanCapabilityEvidence:
            raise ValueError("LAN capability evidence must be exactly typed")
        capabilities.append(
            LanCapabilityEvidence(
                capability=evidence.capability,
                supported=evidence.supported,
                provenance=evidence.provenance,
                status=evidence.status,
            )
        )
    canonical = _make_observation(
        canonical_endpoint,
        reachability=observation.reachability,
        transport_security=observation.transport_security,
        api_shape=observation.api_shape,
        catalog=observation.catalog,
        catalog_complete=observation.catalog_complete,
        catalog_truncated=observation.catalog_truncated,
        capabilities=tuple(capabilities),
        capability_route=observation.capability_route,
        selected_model_id=observation.selected_model_id,
        failure_category=observation.failure_category,
    )
    supplied_digests = (
        observation.endpoint_binding_digest,
        observation.catalog_digest,
        observation.capability_digest,
        observation.observation_digest,
    )
    canonical_digests = (
        canonical.endpoint_binding_digest,
        canonical.catalog_digest,
        canonical.capability_digest,
        canonical.observation_digest,
    )
    if supplied_digests != canonical_digests:
        raise ValueError("LAN observation digest does not match its typed preimage")

    public_payload = _task4_public_payload(canonical)
    return LanObservationDraft(
        endpoint_id=canonical.endpoint_binding_digest,
        source=source,
        interface_id=canonical.endpoint.interface_id,
        address=canonical.endpoint.address,
        port=canonical.endpoint.port,
        api_shape=(canonical.api_shape.value if canonical.api_shape is not None else None),
        tls_enabled=False,
        certificate_sha256=None,
        catalog_digest=canonical.catalog_digest,
        capability_digest=canonical.capability_digest,
        public_payload=public_payload,
        freshness_timestamp=_normalize_observation_timestamp(freshness_timestamp),
        error_category=(
            canonical.failure_category.value if canonical.failure_category is not None else None
        ),
    )


def load_authenticated_task4_observation(
    connection: sqlite3.Connection,
    *,
    scan_id: str,
    endpoint_binding_digest: str,
    expected_terminal_receipt_digest: str,
    expected_observation_digest: str,
    authenticated_owner_principal: str,
) -> AuthenticatedLanObservation:
    """Rebuild receipt membership and all Task 4 digests on one transaction."""

    scan_row = connection.execute(
        "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
        (scan_id,),
    ).fetchone()
    if scan_row is None:
        raise KeyError(f"Unknown LAN scan: {scan_id}")
    if str(scan_row["scan_id"]) != scan_id:
        raise ValueError("durable LAN scan identity is inconsistent")
    if str(scan_row["status"]) != "completed":
        raise ValueError("LAN import requires a completed scan")
    raw_owner_principal = scan_row["owner_principal"]
    if type(raw_owner_principal) is not str:
        raise ValueError("durable LAN scan owner principal is not exact text")
    owner_principal = validate_required_text(
        raw_owner_principal,
        "owner_principal",
        maximum=256,
    )
    if owner_principal != raw_owner_principal:
        raise ValueError("durable LAN scan owner principal is not canonical")
    if unicodedata.normalize("NFC", owner_principal) != owner_principal or any(
        unicodedata.category(character).startswith("C") for character in owner_principal
    ):
        raise ValueError("durable LAN scan owner principal is not canonical")
    if type(authenticated_owner_principal) is not str:
        raise ValueError("authenticated LAN owner principal is not exact text")
    authenticated_owner = validate_required_text(
        authenticated_owner_principal,
        "authenticated_owner_principal",
        maximum=256,
    )
    if authenticated_owner != authenticated_owner_principal:
        raise ValueError("authenticated LAN owner principal is not canonical")
    if unicodedata.normalize("NFC", authenticated_owner) != authenticated_owner or any(
        unicodedata.category(character).startswith("C") for character in authenticated_owner
    ):
        raise ValueError("authenticated LAN owner principal is not canonical")
    if owner_principal != authenticated_owner:
        raise ValueError("LAN scan owner does not match authenticated owner")
    interface_id = validate_digest(
        scan_row["confirmed_interface_id"],
        "confirmed_interface_id",
    )
    if interface_id is None:
        raise ValueError("durable LAN scan interface identifier is missing")
    network = normalize_network(str(scan_row["network"]))
    if network != str(scan_row["network"]):
        raise ValueError("durable LAN scan network is not canonical")
    parsed_network = ipaddress.ip_network(network, strict=True)
    private_scope = (
        any(parsed_network.subnet_of(allowed) for allowed in _PRIVATE_IPV4_NETWORKS)
        if isinstance(parsed_network, ipaddress.IPv4Network)
        else any(parsed_network.subnet_of(allowed) for allowed in _PRIVATE_IPV6_NETWORKS)
    )
    if not private_scope:
        raise ValueError("durable LAN scan network is not a private interface scope")
    if (
        isinstance(parsed_network, ipaddress.IPv4Network)
        and parsed_network.prefixlen < 31
        and parsed_network.num_addresses - 2 > 256
    ):
        raise ValueError("durable LAN scan network exceeds the active-host bound")
    limits_raw = _strict_json_object(scan_row["limits_json"], "scan limits")
    limits = bounded_scan_limits(limits_raw)
    if canonical_json(limits) != str(scan_row["limits_json"]):
        raise ValueError("durable LAN scan limits are not canonical")
    if sha256_digest(limits) != validate_digest(
        scan_row["limits_digest"],
        "limits_digest",
    ):
        raise ValueError("durable LAN scan limits digest is invalid")
    preview_digest = validate_digest(scan_row["preview_digest"], "preview_digest")

    observation_rows = connection.execute(
        """
        SELECT * FROM routing_lan_observations
        WHERE scan_id = ? ORDER BY endpoint_id ASC
        """,
        (scan_id,),
    ).fetchall()
    try:
        receipt_observations = [
            _strict_observation_receipt_payload(row, scan_id=scan_id) for row in observation_rows
        ]
    except (TypeError, ValueError):
        raise ValueError("LAN terminal receipt observation membership is invalid") from None
    candidate_count = validate_non_negative_count(
        scan_row["candidate_count"],
        "candidate_count",
    )
    error_count = validate_non_negative_count(scan_row["error_count"], "error_count")
    timeout_count = validate_non_negative_count(
        scan_row["timeout_count"],
        "timeout_count",
    )
    stored_receipt = _strict_json_object(
        scan_row["terminal_receipt_json"],
        "terminal receipt",
    )
    if canonical_json(stored_receipt) != str(scan_row["terminal_receipt_json"]):
        raise ValueError("durable LAN terminal receipt is not canonical")
    started_at = _strict_optional_text(scan_row["started_at"], "started_at")
    if stored_receipt.get("schema") == LAN_SCAN_RECEIPT_V2_SCHEMA:
        receipt = _validated_v2_terminal_receipt(
            stored_receipt,
            scan_id=scan_id,
            owner_principal=owner_principal,
            interface_id=interface_id,
            network=network,
            limits=limits,
            limits_digest=str(scan_row["limits_digest"]),
            preview_digest=preview_digest,
            started_at=started_at,
            finished_at=validate_required_text(
                scan_row["finished_at"],
                "finished_at",
                maximum=64,
            ),
            cancel_reason=_strict_optional_text(scan_row["cancel_reason"], "cancel_reason"),
            terminal_reason=_strict_optional_text(
                scan_row["terminal_reason"],
                "terminal_reason",
            ),
            candidate_count=candidate_count,
            error_count=error_count,
            timeout_count=timeout_count,
            observations=receipt_observations,
        )
    else:
        receipt = {
            "scan_id": scan_id,
            "status": "completed",
            "owner_principal": owner_principal,
            "confirmed_interface_id": interface_id,
            "network": network,
            "limits": limits,
            "limits_digest": str(scan_row["limits_digest"]),
            "preview_digest": preview_digest,
            "started_at": started_at,
            "finished_at": validate_required_text(
                scan_row["finished_at"],
                "finished_at",
                maximum=64,
            ),
            "cancel_reason": _strict_optional_text(
                scan_row["cancel_reason"],
                "cancel_reason",
            ),
            "terminal_reason": _strict_optional_text(
                scan_row["terminal_reason"],
                "terminal_reason",
            ),
            "candidate_count": candidate_count,
            "error_count": error_count,
            "timeout_count": timeout_count,
            "observations": receipt_observations,
        }
    if stored_receipt != receipt:
        raise ValueError("durable LAN terminal receipt does not match durable rows")
    stored_receipt_digest = validate_digest(
        scan_row["terminal_receipt_digest"],
        "terminal_receipt_digest",
    )
    if (
        sha256_digest(receipt) != stored_receipt_digest
        or expected_terminal_receipt_digest != stored_receipt_digest
    ):
        raise ValueError("LAN terminal receipt digest does not match")

    _authenticate_scan_authority_bindings(
        connection,
        scan_id=scan_id,
        owner_principal=owner_principal,
        interface_id=interface_id,
        network=network,
        limits=limits,
        preview_digest=preview_digest,
        observations=receipt_observations,
        started_at=started_at,
        terminal_receipt=receipt,
    )

    # Authenticate the receipt's durable membership (including manual source/kind)
    # before reconstructing any typed endpoint.  This keeps a forged source from
    # selecting a different endpoint authority class ahead of receipt validation.
    authenticated_observations: dict[
        str, tuple[LanObservationSource, LanEndpointObservation, datetime]
    ] = {}
    for row in observation_rows:
        raw_payload = _strict_json_object(
            row["public_payload_json"],
            "observation public payload",
        )
        if raw_payload.get("schema") != _TASK4_OBSERVATION_SCHEMA:
            raise ValueError("LAN terminal receipt contains non-Task-4 evidence")
        endpoint_id = str(row["endpoint_id"])
        if endpoint_id in authenticated_observations:
            raise ValueError("LAN terminal receipt contains duplicate endpoint evidence")
        raw_source = str(row["source"])
        if raw_source not in {"mdns", "active", "manual"}:
            raise ValueError("durable LAN observation source is invalid")
        source = cast(LanObservationSource, raw_source)
        observation, observed_at = _task4_observation_from_row(
            row,
            expected_interface_id=interface_id,
            expected_network=network,
            expected_source=source,
        )
        authenticated_observations[endpoint_id] = (
            source,
            observation,
            observed_at,
        )

    matching_rows = [
        row for row in observation_rows if str(row["endpoint_id"]) == endpoint_binding_digest
    ]
    if len(matching_rows) != 1 or endpoint_binding_digest not in authenticated_observations:
        raise KeyError(f"Unknown LAN observation endpoint: {endpoint_binding_digest}")
    source, observation, observed_at = authenticated_observations[endpoint_binding_digest]
    if observation.observation_digest != expected_observation_digest:
        raise ValueError("LAN observation digest does not match request")
    return AuthenticatedLanObservation(
        scan_id=scan_id,
        owner_principal=owner_principal,
        confirmed_network=network,
        terminal_receipt_digest=stored_receipt_digest,
        observed_at=observed_at,
        source=source,
        observation=observation,
    )


def _validated_v2_terminal_receipt(
    receipt: dict[str, Any],
    *,
    scan_id: str,
    owner_principal: str,
    interface_id: str,
    network: str,
    limits: dict[str, Any],
    limits_digest: str,
    preview_digest: str | None,
    started_at: str | None,
    finished_at: str,
    cancel_reason: str | None,
    terminal_reason: str | None,
    candidate_count: int,
    error_count: int,
    timeout_count: int,
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_fields = {
        "schema",
        "version",
        "scan_id",
        "status",
        "owner_principal",
        "confirmed_interface_id",
        "network",
        "limits",
        "limits_digest",
        "preview_digest",
        "started_at",
        "finished_at",
        "cancel_reason",
        "terminal_reason",
        "mdns_status",
        "planned_count",
        "admitted_count",
        "completed_count",
        "persisted_observation_count",
        "error_count",
        "timeout_count",
        "error_category_counts",
        "observation_count",
        "observation_membership_digest",
        "evidence_complete",
        "unknown_inflight_count",
    }
    if set(receipt) != expected_fields:
        raise ValueError("durable LAN v2 receipt has an invalid field set")
    if receipt["schema"] != LAN_SCAN_RECEIPT_V2_SCHEMA:
        raise ValueError("durable LAN v2 receipt schema is invalid")
    if type(receipt["version"]) is not int or receipt["version"] != 2:
        raise ValueError("durable LAN v2 receipt version is invalid")
    exact_bindings = {
        "scan_id": scan_id,
        "status": "completed",
        "owner_principal": owner_principal,
        "confirmed_interface_id": interface_id,
        "network": network,
        "limits": limits,
        "limits_digest": limits_digest,
        "preview_digest": preview_digest,
        "started_at": started_at,
        "finished_at": finished_at,
        "cancel_reason": cancel_reason,
        "terminal_reason": terminal_reason,
    }
    if any(receipt[field] != value for field, value in exact_bindings.items()):
        raise ValueError("durable LAN v2 receipt binding does not match scan row")
    if terminal_reason != "scan_complete" or cancel_reason is not None:
        raise ValueError("completed LAN v2 receipt has an invalid terminal reason")
    mdns_status = receipt["mdns_status"]
    if type(mdns_status) is not str or mdns_status not in LAN_MDNS_STATUSES:
        raise ValueError("durable LAN v2 mDNS status is invalid")
    counts = {
        field: validate_non_negative_count(receipt[field], field)
        for field in (
            "planned_count",
            "admitted_count",
            "completed_count",
            "persisted_observation_count",
            "error_count",
            "timeout_count",
            "observation_count",
        )
    }
    if not (counts["completed_count"] == counts["admitted_count"] <= counts["planned_count"]):
        raise ValueError("durable LAN v2 terminal counts are inconsistent")
    durable_count = len(observations)
    if durable_count > MAX_LAN_SCAN_OBSERVATIONS:
        raise ValueError("durable LAN v2 observation count exceeds the fixed ceiling")
    if (
        counts["persisted_observation_count"] != durable_count
        or counts["observation_count"] != durable_count
        or candidate_count != durable_count
    ):
        raise ValueError("durable LAN v2 observation count does not match rows")
    if durable_count != counts["completed_count"]:
        raise ValueError("durable LAN v2 complete evidence does not match completed work")
    error_counts = bounded_error_category_counts(receipt["error_category_counts"])
    durable_error_counts: dict[str, int] = {}
    for observation in observations:
        error_category = observation["error_category"]
        if error_category is not None:
            durable_error_counts[error_category] = durable_error_counts.get(error_category, 0) + 1
    durable_error_counts = dict(sorted(durable_error_counts.items()))
    durable_timeout_count = sum(
        count
        for category, count in durable_error_counts.items()
        if category in {"tcp_timeout", "http_timeout", "scan_deadline_exceeded"}
    )
    if error_counts != durable_error_counts:
        raise ValueError("durable LAN v2 error counts do not match observations")
    if counts["error_count"] != sum(error_counts.values()) or error_count != counts["error_count"]:
        raise ValueError("durable LAN v2 error counts do not match rows")
    if counts["timeout_count"] != timeout_count or timeout_count != durable_timeout_count:
        raise ValueError("durable LAN v2 timeout count does not match row")
    membership = validate_digest(
        receipt["observation_membership_digest"],
        "observation_membership_digest",
    )
    try:
        durable_membership = _raw_observation_membership_digest(observations)
    except (TypeError, ValueError):
        raise ValueError("LAN terminal receipt observation membership is invalid") from None
    if membership != durable_membership:
        raise ValueError("durable LAN v2 observation membership does not match rows")
    if type(receipt["evidence_complete"]) is not bool or not receipt["evidence_complete"]:
        raise ValueError("durable completed LAN v2 receipt must have complete evidence")
    if type(receipt["unknown_inflight_count"]) is not int or receipt["unknown_inflight_count"] != 0:
        raise ValueError("durable completed LAN v2 in-flight count must be exact zero")
    return {
        "schema": LAN_SCAN_RECEIPT_V2_SCHEMA,
        "version": 2,
        **exact_bindings,
        "mdns_status": mdns_status,
        "planned_count": counts["planned_count"],
        "admitted_count": counts["admitted_count"],
        "completed_count": counts["completed_count"],
        "persisted_observation_count": durable_count,
        "error_count": error_count,
        "timeout_count": timeout_count,
        "error_category_counts": error_counts,
        "observation_count": durable_count,
        "observation_membership_digest": membership,
        "evidence_complete": True,
        "unknown_inflight_count": 0,
    }


def _strict_observation_receipt_payload(
    row: sqlite3.Row,
    *,
    scan_id: str,
) -> dict[str, Any]:
    if str(row["scan_id"]) != scan_id:
        raise ValueError("durable LAN observation scan identity is inconsistent")
    source = row["source"]
    if type(source) is not str or source not in {"mdns", "active", "manual"}:
        raise ValueError("durable LAN observation source is invalid")
    if type(row["tls_enabled"]) is not int or row["tls_enabled"] not in {0, 1}:
        raise ValueError("durable LAN observation TLS flag is invalid")
    port = row["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("durable LAN observation port is invalid")
    tls_enabled = bool(row["tls_enabled"])
    certificate = validate_digest(
        _strict_optional_text(
            row["certificate_sha256"],
            "certificate_sha256",
        ),
        "certificate_sha256",
        optional=True,
    )
    if certificate is not None and not tls_enabled:
        raise ValueError("certificate evidence requires TLS")
    public_payload = bounded_observation_public_evidence(
        _strict_json_object(
            row["public_payload_json"],
            "observation public payload",
        )
    )
    values: dict[str, Any] = {
        "endpoint_id": validate_digest(row["endpoint_id"], "endpoint_id"),
        "source": source,
        "interface_id": validate_digest(row["interface_id"], "interface_id"),
        "address": normalize_address(str(row["address"])),
        "port": port,
        "api_shape": validate_optional_text(
            _strict_optional_text(row["api_shape"], "api_shape"),
            "api_shape",
            maximum=128,
        ),
        "tls_enabled": tls_enabled,
        "certificate_sha256": certificate,
        "catalog_digest": validate_digest(
            _strict_optional_text(row["catalog_digest"], "catalog_digest"),
            "catalog_digest",
            optional=True,
        ),
        "capability_digest": validate_digest(
            _strict_optional_text(
                row["capability_digest"],
                "capability_digest",
            ),
            "capability_digest",
            optional=True,
        ),
        "public_payload": public_payload,
        "freshness_timestamp": _normalize_observation_timestamp(row["freshness_timestamp"]),
        "error_category": validate_optional_text(
            _strict_optional_text(
                row["error_category"],
                "error_category",
            ),
            "error_category",
            maximum=128,
        ),
    }
    if canonical_json(public_payload) != str(row["public_payload_json"]):
        raise ValueError("durable LAN observation payload is not canonical")
    created_at = validate_required_text(row["created_at"], "created_at", maximum=64)
    return {
        "scan_id": scan_id,
        **values,
        "created_at": created_at,
    }


def _raw_observation_membership_digest(
    observations: list[dict[str, Any]],
) -> str:
    members: dict[str, str] = {}
    for observation in observations:
        endpoint_id = str(observation["endpoint_id"])
        payload = observation["public_payload"]
        if type(payload) is dict and payload.get("schema") == _TASK4_OBSERVATION_SCHEMA:
            observation_digest = validate_digest(
                payload.get("observation_digest"),
                "observation_digest",
            )
            assert observation_digest is not None
        else:
            projection = {
                key: value
                for key, value in observation.items()
                if key not in {"scan_id", "created_at"}
            }
            observation_digest = sha256_digest(
                {
                    "schema": "kestrel.lan.observation-projection.v1",
                    **projection,
                }
            )
        member: dict[str, str] = {
            "endpoint_id": endpoint_id,
            "observation_digest": observation_digest,
        }
        if observation["source"] == "manual":
            member.update(
                {
                    "observation_source": "manual",
                    "endpoint_kind": "manual",
                }
            )
        prior_member = members.get(endpoint_id)
        canonical_member = canonical_json(member)
        if prior_member is not None and prior_member != canonical_member:
            raise ValueError(f"conflicting LAN observation endpoint: {endpoint_id}")
        members[endpoint_id] = canonical_member
    ordered = [json.loads(members[endpoint_id]) for endpoint_id in sorted(members)]
    return sha256_digest(
        {
            "schema": LAN_OBSERVATION_MEMBERSHIP_SCHEMA,
            "count": len(ordered),
            "members": ordered,
        }
    )


def _authenticate_scan_authority_bindings(
    connection: sqlite3.Connection,
    *,
    scan_id: str,
    owner_principal: str,
    interface_id: str,
    network: str,
    limits: dict[str, Any],
    preview_digest: str | None,
    observations: list[dict[str, Any]],
    started_at: str | None,
    terminal_receipt: dict[str, Any],
) -> None:
    manual_limits = limits.get("mode") == "manual"
    exact_port = limits.get("exact_port") if manual_limits else None
    if manual_limits and (type(exact_port) is not int or len(observations) != 1):
        raise ValueError("manual LAN limits do not match durable observation authority")
    if manual_limits:
        if terminal_receipt.get("schema") != LAN_SCAN_RECEIPT_V2_SCHEMA:
            raise ValueError("manual LAN scan requires a v2 terminal receipt")
        if terminal_receipt.get("mdns_status") != "unavailable":
            raise ValueError("manual LAN terminal mDNS status must be unavailable")
        exact_singleton_counts = (
            "planned_count",
            "admitted_count",
            "completed_count",
            "persisted_observation_count",
            "observation_count",
        )
        if any(
            type(terminal_receipt.get(field)) is not int or terminal_receipt.get(field) != 1
            for field in exact_singleton_counts
        ):
            raise ValueError("manual LAN terminal counts must all be exact one")
    for observation in observations:
        payload = observation["public_payload"]
        if type(payload) is not dict or payload.get("schema") != _TASK4_OBSERVATION_SCHEMA:
            raise ValueError("LAN terminal receipt contains non-Task-4 evidence")
        source = observation["source"]
        manual_payload = (
            payload.get("observation_source") == "manual"
            and payload.get("endpoint_kind") == "manual"
        )
        if manual_limits:
            if source != "manual" or not manual_payload or observation["port"] != exact_port:
                raise ValueError("manual LAN limits do not match observation source, kind, or port")
        elif source == "manual" or manual_payload:
            raise ValueError("automatic LAN limits reject manual observation authority")

    started_rows = connection.execute(
        """
        SELECT sequence, payload_json, created_at FROM routing_lan_scan_events
        WHERE scan_id = ? AND event_type = 'scan_started'
        ORDER BY sequence ASC
        """,
        (scan_id,),
    ).fetchall()
    if manual_limits:
        if len(started_rows) != 1:
            raise ValueError("manual LAN scan requires one exact start event")
        started_row = started_rows[0]
        if (
            type(started_row["sequence"]) is not int
            or started_row["sequence"] != 1
            or type(started_row["created_at"]) is not str
            or started_row["created_at"] != started_at
        ):
            raise ValueError("manual LAN scan start event authority is invalid")
        raw_event = started_row["payload_json"]
        event = bounded_scan_preview_event(
            _strict_json_object(raw_event, "manual scan start event")
        )
        if canonical_json(event) != str(raw_event):
            raise ValueError("manual LAN scan start event is not canonical")
        expected = {
            "schema": LAN_MANUAL_SCAN_PREVIEW_EVENT_SCHEMA,
            "mode": "manual",
            "endpoint_kind": "manual",
            "observation_source": "manual",
            "owner_principal": owner_principal,
            "interface_id": interface_id,
            "network": network,
            "limits": limits,
            "exact_port": exact_port,
            "server_version": _LAN_SERVER_VERSION,
            "contract_version": _LAN_MANUAL_PREVIEW_CONTRACT_VERSION,
            "preview_digest": preview_digest,
        }
        if any(event.get(field) != value for field, value in expected.items()):
            raise ValueError("manual LAN scan start event disagrees with durable authority")
        try:
            authenticated_start = _parse_canonical_durable_utc(started_at)
            preview_expiration = _parse_canonical_utc(event["expires_at"])
        except (TypeError, ValueError):
            raise ValueError("manual LAN preview expiration is invalid") from None
        if preview_expiration <= authenticated_start:
            raise ValueError("manual LAN preview expiration must follow authenticated start")
    else:
        for row in started_rows:
            event = _strict_json_object(row["payload_json"], "scan start event")
            if event.get("schema") == LAN_MANUAL_SCAN_PREVIEW_EVENT_SCHEMA:
                raise ValueError("automatic LAN limits reject a manual start event")


def _task4_observation_from_row(
    row: sqlite3.Row,
    *,
    expected_interface_id: str,
    expected_network: str,
    expected_source: LanObservationSource,
) -> tuple[LanEndpointObservation, datetime]:
    payload = bounded_observation_public_evidence(
        _strict_json_object(row["public_payload_json"], "Task 4 observation")
    )
    endpoint_id = validate_digest(row["endpoint_id"], "endpoint_id")
    if payload["endpoint_binding_digest"] != endpoint_id:
        raise ValueError("LAN endpoint row and public evidence disagree")
    interface_id = validate_digest(row["interface_id"], "interface_id")
    if interface_id != expected_interface_id:
        raise ValueError("LAN observation interface does not match its scan")
    address = normalize_address(str(row["address"]))
    if address != str(row["address"]):
        raise ValueError("LAN observation address is not canonical")
    parsed_address = ipaddress.ip_address(address)
    parsed_network = ipaddress.ip_network(expected_network, strict=True)
    if (
        parsed_address.is_unspecified
        or parsed_address.is_loopback
        or parsed_address.is_multicast
        or parsed_address.is_reserved
    ):
        raise ValueError("LAN observation address is not eligible")
    if parsed_address not in parsed_network:
        raise ValueError("LAN observation address is outside its confirmed network")
    if (
        isinstance(parsed_network, ipaddress.IPv4Network)
        and parsed_network.prefixlen < 31
        and parsed_address in {parsed_network.network_address, parsed_network.broadcast_address}
    ):
        raise ValueError("LAN observation address is not an active host")
    port = row["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("LAN observation port is invalid")
    if expected_source == "manual":
        if (
            parsed_network.prefixlen != parsed_network.max_prefixlen
            or parsed_network.network_address != parsed_address
        ):
            raise ValueError("manual LAN observation requires an exact confirmed host scope")
        endpoint: ResolvedLanEndpoint | ManualLanEndpoint = object.__new__(ManualLanEndpoint)
        object.__setattr__(endpoint, "kind", "manual")
    else:
        if port not in KNOWN_MODEL_SERVICE_PORTS:
            raise ValueError("LAN observation port is not a known model-service port")
        endpoint = object.__new__(ResolvedLanEndpoint)
    object.__setattr__(endpoint, "interface_id", interface_id)
    object.__setattr__(endpoint, "address", address)
    object.__setattr__(endpoint, "port", port)

    capabilities: list[LanCapabilityEvidence] = []
    for index, raw in enumerate(payload["capabilities"]):
        item = dict(raw)
        capabilities.append(
            LanCapabilityEvidence(
                capability=tuple(CapabilityName)[index],
                supported=item["supported"],
                provenance=CapabilityProvenance(item["provenance"]),
                status=CapabilityObservationStatus(item["status"]),
            )
        )
    canonical = _make_observation(
        endpoint,
        reachability=Reachability(payload["reachability"]),
        transport_security=(
            None
            if payload["transport_security"] is None
            else TransportSecurity(payload["transport_security"])
        ),
        api_shape=(None if payload["api_shape"] is None else ApiShape(payload["api_shape"])),
        catalog=tuple(payload["model_ids"]),
        catalog_complete=payload["catalog_complete"],
        catalog_truncated=payload["catalog_truncated"],
        capabilities=tuple(capabilities),
        capability_route=payload["capability_route"],
        selected_model_id=payload["selected_model_id"],
        failure_category=(
            None
            if payload["failure_category"] is None
            else LanFailureCategory(payload["failure_category"])
        ),
    )
    duplicated = {
        "endpoint_id": canonical.endpoint_binding_digest,
        "api_shape": canonical.api_shape.value if canonical.api_shape is not None else None,
        "tls_enabled": 0,
        "certificate_sha256": None,
        "catalog_digest": canonical.catalog_digest,
        "capability_digest": canonical.capability_digest,
        "error_category": (
            canonical.failure_category.value if canonical.failure_category is not None else None
        ),
    }
    if any(row[field] != value for field, value in duplicated.items()):
        raise ValueError("LAN durable columns disagree with Task 4 evidence")
    if payload != _task4_public_payload(canonical):
        raise ValueError("LAN public evidence does not match its canonical preimage")
    observed_at = _parse_canonical_utc(str(row["freshness_timestamp"]))
    return canonical, observed_at


def _task4_public_payload(observation: LanEndpointObservation) -> dict[str, Any]:
    payload = {
        "schema": _TASK4_OBSERVATION_SCHEMA,
        "endpoint_binding_digest": observation.endpoint_binding_digest,
        "observation_digest": observation.observation_digest,
        "reachability": observation.reachability.value,
        "transport_security": (
            observation.transport_security.value
            if observation.transport_security is not None
            else None
        ),
        "api_shape": observation.api_shape.value if observation.api_shape is not None else None,
        "catalog_complete": observation.catalog_complete,
        "catalog_truncated": observation.catalog_truncated,
        "model_ids": list(observation.catalog),
        "capability_route": observation.capability_route,
        "selected_model_id": observation.selected_model_id,
        "capabilities": [item.to_digest_payload() for item in observation.capabilities],
        "failure_category": (
            observation.failure_category.value if observation.failure_category is not None else None
        ),
    }
    if type(observation.endpoint) is ManualLanEndpoint:
        payload.update(
            {
                "observation_source": "manual",
                "endpoint_kind": "manual",
            }
        )
    return payload


def _parse_canonical_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("LAN evidence timestamp must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("LAN evidence timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("LAN evidence timestamp must be UTC")
    if parsed.astimezone(UTC).isoformat().replace("+00:00", "Z") != value:
        raise ValueError("LAN evidence timestamp must be canonical UTC")
    return parsed.astimezone(UTC)


def _parse_canonical_durable_utc(value: object) -> datetime:
    if type(value) is not str or len(value) > 64 or not value.endswith("+00:00"):
        raise ValueError("durable LAN timestamp must use canonical UTC storage encoding")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("durable LAN timestamp is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(parsed)
        or parsed.astimezone(UTC).isoformat() != value
    ):
        raise ValueError("durable LAN timestamp must use canonical UTC storage encoding")
    return parsed.astimezone(UTC)


def _normalize_observation_timestamp(value: object) -> str:
    if type(value) is not str or len(value) > 64 or _RFC3339_UTC_RE.fullmatch(value) is None:
        raise ValueError("LAN evidence timestamp must be RFC3339 UTC")
    parseable = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(parseable)
    except ValueError as exc:
        raise ValueError("LAN evidence timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("LAN evidence timestamp must be UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def _strict_optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return validate_required_text(value, field, maximum=512)


def _strict_json_object(value: object, field: str) -> dict[str, Any]:
    if type(value) is not str:
        raise ValueError(f"durable LAN {field} must be JSON text")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"durable LAN {field} is invalid JSON") from exc
    if type(parsed) is not dict:
        raise ValueError(f"durable LAN {field} must be a JSON object")
    return parsed


def _bounded_task4_observation_public_evidence(
    payload: dict[str, object],
) -> dict[str, Any]:
    fields = set(payload)
    manual_fields = {"observation_source", "endpoint_kind"}
    if fields not in {
        _TASK4_OBSERVATION_PUBLIC_FIELDS,
        _TASK4_OBSERVATION_PUBLIC_FIELDS | manual_fields,
    }:
        raise ValueError("Task 4 observation public evidence has an invalid field set")
    if manual_fields.issubset(fields) and (
        payload["observation_source"] != "manual" or payload["endpoint_kind"] != "manual"
    ):
        raise ValueError("Task 4 observation source and endpoint kind are invalid")
    if payload["schema"] != _TASK4_OBSERVATION_SCHEMA:
        raise ValueError("Task 4 observation public evidence schema is invalid")
    validate_digest(payload["endpoint_binding_digest"], "endpoint_binding_digest")  # type: ignore[arg-type]
    validate_digest(payload["observation_digest"], "observation_digest")  # type: ignore[arg-type]
    if payload["reachability"] not in {item.value for item in Reachability}:
        raise ValueError("Task 4 observation reachability is invalid")
    if payload["transport_security"] not in {
        None,
        TransportSecurity.PLAIN_HTTP.value,
    }:
        raise ValueError("Task 4 observation transport security is invalid")
    if payload["api_shape"] not in {None, *(item.value for item in ApiShape)}:
        raise ValueError("Task 4 observation API shape is invalid")
    for field in ("catalog_complete", "catalog_truncated"):
        if type(payload[field]) is not bool:
            raise ValueError(f"Task 4 observation {field} must be boolean")
    models = payload["model_ids"]
    if type(models) is not list or len(models) > MAX_DISCOVERED_MODELS:
        raise ValueError("Task 4 observation model_ids is not bounded")
    if any(type(item) is not str for item in models):
        raise ValueError("Task 4 observation model_ids must contain text")
    if models != sorted(set(models)):
        raise ValueError("Task 4 observation model_ids is not canonical")
    for field in ("capability_route", "selected_model_id"):
        value = payload[field]
        if value is not None and (type(value) is not str or not value or len(value) > 512):
            raise ValueError(f"Task 4 observation {field} is invalid")
    capabilities = payload["capabilities"]
    if type(capabilities) is not list or len(capabilities) != len(CapabilityName):
        raise ValueError("Task 4 observation capabilities is incomplete")
    expected_capabilities = list(CapabilityName)
    for index, raw in enumerate(capabilities):
        item = _require_string_keyed_object(raw, "Task 4 capability evidence")
        if set(item) != {"capability", "supported", "provenance", "status"}:
            raise ValueError("Task 4 capability evidence has an invalid field set")
        if item["capability"] != expected_capabilities[index].value:
            raise ValueError("Task 4 capability evidence is not ordered")
        try:
            LanCapabilityEvidence(
                capability=expected_capabilities[index],
                supported=item["supported"],  # type: ignore[arg-type]
                provenance=CapabilityProvenance(item["provenance"]),  # type: ignore[arg-type]
                status=CapabilityObservationStatus(item["status"]),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Task 4 capability evidence is invalid") from exc
    failure = payload["failure_category"]
    if failure is not None:
        try:
            LanFailureCategory(failure)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("Task 4 failure category is invalid") from exc
    return _bounded_json_object(
        payload,
        kind="Task 4 observation public evidence",
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
    if supplied_json == expected_json:
        return _bounded_json_object(
            json.loads(expected_json),
            kind="scan limits",
            max_bytes=MAX_LIMITS_BYTES,
        )
    manual_fields = {
        "mode",
        "exact_port",
        "max_active_hosts",
        "max_scan_concurrency",
        "tcp_connect_timeout_seconds",
        "http_probe_timeout_seconds",
        "total_scan_deadline_seconds",
        "max_probe_response_bytes",
        "max_discovered_models",
        "mdns_enabled",
    }
    if set(payload) != manual_fields:
        raise ValueError("LAN scan limits must match the fixed bounded limits")
    exact_port = payload["exact_port"]
    expected_manual = {
        "mode": "manual",
        "exact_port": exact_port,
        "max_active_hosts": 1,
        "max_scan_concurrency": 1,
        "tcp_connect_timeout_seconds": 0.75,
        "http_probe_timeout_seconds": 2.0,
        "total_scan_deadline_seconds": 45.0,
        "max_probe_response_bytes": 256 * 1024,
        "max_discovered_models": 8,
        "mdns_enabled": False,
    }
    if (
        type(exact_port) is not int
        or not 1 <= exact_port <= 65535
        or canonical_json(payload) != canonical_json(expected_manual)
    ):
        raise ValueError("LAN manual scan limits must match the fixed bounded limits")
    return _bounded_json_object(
        expected_manual,
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
    if (
        isinstance(draft.port, bool)
        or not isinstance(draft.port, int)
        or not 1 <= draft.port <= 65535
    ):
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
    values: dict[str, Any] = {
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
        "freshness_timestamp": _normalize_observation_timestamp(draft.freshness_timestamp),
        "error_category": validate_optional_text(
            draft.error_category,
            "error_category",
            maximum=128,
        ),
    }
    if values["public_payload"].get("schema") == _TASK4_OBSERVATION_SCHEMA:
        _validate_task4_draft_preimage(values)
    return values


def _validate_task4_draft_preimage(values: dict[str, Any]) -> None:
    payload = values["public_payload"]
    manual_payload = (
        payload.get("observation_source") == "manual" and payload.get("endpoint_kind") == "manual"
    )
    if (values["source"] == "manual") != manual_payload:
        raise ValueError("LAN Task 4 observation source and endpoint kind disagree")
    if not manual_payload and values["port"] not in KNOWN_MODEL_SERVICE_PORTS:
        raise ValueError("LAN Task 4 draft port is not a known model-service port")
    address = ipaddress.ip_address(values["address"])
    private_scope = (
        any(address in network for network in _PRIVATE_IPV4_NETWORKS)
        if isinstance(address, ipaddress.IPv4Address)
        else any(address in network for network in _PRIVATE_IPV6_NETWORKS)
    )
    if (
        not private_scope
        or address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or address.is_reserved
    ):
        raise ValueError("LAN Task 4 draft endpoint is not eligible")
    if manual_payload:
        endpoint: ResolvedLanEndpoint | ManualLanEndpoint = object.__new__(ManualLanEndpoint)
        object.__setattr__(endpoint, "kind", "manual")
    else:
        endpoint = object.__new__(ResolvedLanEndpoint)
    object.__setattr__(endpoint, "interface_id", values["interface_id"])
    object.__setattr__(endpoint, "address", values["address"])
    object.__setattr__(endpoint, "port", values["port"])
    capabilities = tuple(
        LanCapabilityEvidence(
            capability=tuple(CapabilityName)[index],
            supported=item["supported"],
            provenance=CapabilityProvenance(item["provenance"]),
            status=CapabilityObservationStatus(item["status"]),
        )
        for index, item in enumerate(payload["capabilities"])
    )
    canonical = _make_observation(
        endpoint,
        reachability=Reachability(payload["reachability"]),
        transport_security=(
            None
            if payload["transport_security"] is None
            else TransportSecurity(payload["transport_security"])
        ),
        api_shape=(None if payload["api_shape"] is None else ApiShape(payload["api_shape"])),
        catalog=tuple(payload["model_ids"]),
        catalog_complete=payload["catalog_complete"],
        catalog_truncated=payload["catalog_truncated"],
        capabilities=capabilities,
        capability_route=payload["capability_route"],
        selected_model_id=payload["selected_model_id"],
        failure_category=(
            None
            if payload["failure_category"] is None
            else LanFailureCategory(payload["failure_category"])
        ),
    )
    expected_columns = {
        "endpoint_id": canonical.endpoint_binding_digest,
        "api_shape": (canonical.api_shape.value if canonical.api_shape is not None else None),
        "tls_enabled": False,
        "certificate_sha256": None,
        "catalog_digest": canonical.catalog_digest,
        "capability_digest": canonical.capability_digest,
        "error_category": (
            canonical.failure_category.value if canonical.failure_category is not None else None
        ),
    }
    if any(values[field] != expected for field, expected in expected_columns.items()):
        raise ValueError("LAN Task 4 draft columns disagree with its digest preimage")
    if payload != _task4_public_payload(canonical):
        raise ValueError("LAN Task 4 draft payload disagrees with its digest preimage")


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
        raise ValueError(f"public evidence {field} must be between 1 and {maximum} characters")
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
            "unsupported observation public evidence metadata field: " + ", ".join(unknown)
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
