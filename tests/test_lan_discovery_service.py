from __future__ import annotations

import hashlib
import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from threading import Event

import pytest

import nested_memvid_agent.lan_discovery_service as lan_discovery_service_module
import nested_memvid_agent.routing.lan_serialization as lan_serialization_module
from nested_memvid_agent.lan_discovery_models import (
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_discovery_service import (
    LAN_OBSERVATION_MAX_AGE_SECONDS,
    LAN_OBSERVATION_MAX_FUTURE_SKEW_SECONDS,
    LanDiscoveryConflict,
    LanDiscoveryService,
    LanExpectedRevision,
    LanImportRequest,
    LanImportResult,
    LanReplacementConfirmation,
    LanReviewRequest,
    LanReviewResult,
)
from nested_memvid_agent.lan_http_transport import (
    CurrentLanInterfaceInventory,
    CurrentLanInterfaceState,
    LanRequestRoute,
)
from nested_memvid_agent.lan_scanner import (
    ApiShape,
    CapabilityName,
    CapabilityObservationStatus,
    CapabilityProvenance,
    LanCapabilityEvidence,
    LanFailureCategory,
    Reachability,
    TransportSecurity,
    _make_observation,
)
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.routing.lan_serialization import (
    lan_observation_to_draft,
    load_authenticated_task4_observation,
)
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.ledger_records import RoutingRevisionConflict
from nested_memvid_agent.routing.models import AgentTaskContract
from nested_memvid_agent.routing.router import RoutingUnavailableError, route_task
from nested_memvid_agent.state_store import AgentStateStore

OWNER = "owner:local-runtime:v1"
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
PREVIEW_DIGEST = "sha256:" + "7" * 64
PROFILE_PROTECTED_KEYS = {
    "schema",
    "managed",
    "owner_principal",
    "scan_id",
    "endpoint_binding_digest",
    "endpoint_fingerprint",
    "interface_id",
    "confirmed_network",
    "address",
    "port",
    "transport_security",
    "certificate_sha256",
    "api_shape",
    "observation_digest",
    "terminal_receipt_digest",
    "catalog_digest",
    "capability_digest",
    "observed_at",
    "fresh_until",
    "observation_source",
    "endpoint_kind",
    "runtime_adapter",
    "runtime_hardening",
    "stale_reason",
    "stale_reasons",
    "stale_transition_terminal_receipt_digest",
}
TARGET_PROTECTED_KEYS = {
    "schema",
    "managed",
    "owner_principal",
    "scan_id",
    "provider_profile_id",
    "model_id",
    "endpoint_binding_digest",
    "endpoint_fingerprint",
    "interface_id",
    "confirmed_network",
    "address",
    "port",
    "transport_security",
    "certificate_sha256",
    "api_shape",
    "observation_digest",
    "terminal_receipt_digest",
    "catalog_digest",
    "capability_digest",
    "capability_claims",
    "observed_at",
    "fresh_until",
    "observation_source",
    "endpoint_kind",
    "runtime_adapter",
    "runtime_hardening",
    "material_binding_digest",
    "reviewed",
    "reviewed_profile_revision",
    "reviewed_target_revision",
    "review_evidence_terminal_receipt_digest",
    "review_evidence_observation_digest",
    "reviewed_from_material_binding_digest",
    "reviewed_material_binding_digest",
    "review_acknowledged_stale_reasons",
    "review_acknowledged_stale_transition_terminal_receipt_digest",
    "review_digest",
    "privacy_acknowledgement_digest",
    "reviewed_runtime_interface_binding_digest",
    "intended_roles",
    "task_family_affinities",
    "stale_reason",
    "stale_reasons",
    "stale_transition_terminal_receipt_digest",
}


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _scope(
    *,
    network: str = "192.168.50.0/29",
    os_identity: str = "test:en0",
    display_name: str = "renderer text must not become authority",
    addresses: tuple[str, ...] = ("192.168.50.1/29",),
) -> PrivateScanScope:
    interface = NetworkInterface.from_addresses(
        os_identity=os_identity,
        display_name=display_name,
        addresses=addresses,
    )
    return PrivateScanScope.from_request(interface, network)


def _capabilities(*, generation_passed: bool = True) -> tuple[LanCapabilityEvidence, ...]:
    generation = (
        LanCapabilityEvidence.observed_pass()
        if generation_passed
        else LanCapabilityEvidence.observed_failure()
    )
    return (
        generation,
        *(LanCapabilityEvidence.not_run(item) for item in tuple(CapabilityName)[1:]),
    )


def _positive_observation(
    *,
    scope: PrivateScanScope | None = None,
    address: str = "192.168.50.2",
    port: int = 1234,
    models: tuple[str, ...] = ("alpha",),
    catalog_complete: bool = True,
    catalog_truncated: bool = False,
):
    active_scope = scope or _scope()
    endpoint = ResolvedLanEndpoint.from_scope(active_scope, address, port)
    return _make_observation(
        endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=TransportSecurity.PLAIN_HTTP,
        api_shape=ApiShape.OPENAI_COMPATIBLE,
        catalog=models,
        catalog_complete=catalog_complete,
        catalog_truncated=catalog_truncated,
        capabilities=_capabilities(),
        capability_route=LanRequestRoute.OPENAI_GENERATION.path,
        selected_model_id=models[0],
        failure_category=None,
    )


def _manual_endpoint_type():
    """Resolve the Task 7A type lazily so this file collects on the frozen base."""

    return import_module("nested_memvid_agent.lan_discovery_models").ManualLanEndpoint


def _manual_limits(port: int = 5001) -> dict[str, object]:
    return {
        "mode": "manual",
        "exact_port": port,
        "max_active_hosts": 1,
        "max_scan_concurrency": 1,
        "tcp_connect_timeout_seconds": 0.75,
        "http_probe_timeout_seconds": 2.0,
        "total_scan_deadline_seconds": 45.0,
        "max_probe_response_bytes": 262144,
        "max_discovered_models": 8,
        "mdns_enabled": False,
    }


def _automatic_limits() -> dict[str, object]:
    return {
        "known_model_service_ports": [1234, 8000, 8080, 11434],
        "max_active_hosts": 256,
        "max_scan_concurrency": 16,
        "tcp_connect_timeout_seconds": 0.75,
        "http_probe_timeout_seconds": 2.0,
        "total_scan_deadline_seconds": 45.0,
        "max_probe_response_bytes": 262144,
        "max_discovered_models": 8,
        "mdns_window_seconds": 2.5,
    }


def _manual_preview_contract_version() -> str:
    value = import_module(
        "nested_memvid_agent.lan_scan_manager"
    ).LAN_MANUAL_PREVIEW_CONTRACT_VERSION
    assert value == "kestrel.lan.manual-preview-authorization.v1"
    return value


def _manual_server_version() -> str:
    value = import_module("nested_memvid_agent.lan_scan_manager").LAN_SERVER_VERSION
    assert value == "kestrel-local-runtime-v1"
    return value


def _manual_scope(
    *,
    network: str = "192.168.50.2/32",
    os_identity: str = "test:en0",
    addresses: tuple[str, ...] = ("192.168.50.1/29",),
) -> PrivateScanScope:
    return _scope(
        network=network,
        os_identity=os_identity,
        addresses=addresses,
    )


def _manual_observation(
    *,
    scope: PrivateScanScope | None = None,
    address: str = "192.168.50.2",
    port: int = 5001,
    api_shape: ApiShape = ApiShape.OPENAI_COMPATIBLE,
    models: tuple[str, ...] = ("alpha",),
):
    active_scope = scope or _manual_scope()
    endpoint = _manual_endpoint_type().from_exact_scope(active_scope, address, port)
    route = (
        LanRequestRoute.OPENAI_GENERATION.path
        if api_shape is ApiShape.OPENAI_COMPATIBLE
        else LanRequestRoute.OLLAMA_GENERATION.path
    )
    return _make_observation(
        endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=TransportSecurity.PLAIN_HTTP,
        api_shape=api_shape,
        catalog=models,
        catalog_complete=True,
        catalog_truncated=False,
        capabilities=_capabilities(),
        capability_route=route,
        selected_model_id=models[0],
        failure_category=None,
    )


def _manual_preview_event(
    scope: PrivateScanScope,
    *,
    port: int = 5001,
) -> dict[str, object]:
    limits = _manual_limits(port)
    return {
        "schema": "kestrel.lan.scan-preview.manual.v1",
        "mode": "manual",
        "endpoint_kind": "manual",
        "observation_source": "manual",
        "owner_principal": OWNER,
        "interface_id": scope.interface.interface_id,
        "network": scope.network,
        "limits": limits,
        "active_host_count": 1,
        "passive_or_manual_only": True,
        "port_count": 1,
        "exact_port": port,
        "mdns_status": "unavailable",
        "server_version": _manual_server_version(),
        "contract_version": _manual_preview_contract_version(),
        "preview_digest": PREVIEW_DIGEST,
        "expires_at": "2099-08-01T12:00:30Z",
        "confirmed": True,
        "privacy_acknowledged": True,
    }


def _outage_observation(*, scope: PrivateScanScope | None = None):
    active_scope = scope or _scope()
    endpoint = ResolvedLanEndpoint.from_scope(active_scope, "192.168.50.2", 1234)
    return _make_observation(
        endpoint,
        reachability=Reachability.UNREACHABLE,
        capabilities=tuple(LanCapabilityEvidence.not_run(item) for item in CapabilityName),
        failure_category=LanFailureCategory.TCP_TIMEOUT,
    )


def _failed_generation_observation(*, scope: PrivateScanScope | None = None):
    active_scope = scope or _scope()
    endpoint = ResolvedLanEndpoint.from_scope(active_scope, "192.168.50.2", 1234)
    return _make_observation(
        endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=TransportSecurity.PLAIN_HTTP,
        api_shape=ApiShape.OPENAI_COMPATIBLE,
        catalog=("alpha",),
        catalog_complete=True,
        catalog_truncated=False,
        capabilities=_capabilities(generation_passed=False),
        capability_route=LanRequestRoute.OPENAI_GENERATION.path,
        selected_model_id="alpha",
        failure_category=LanFailureCategory.GENERATION_RESPONSE_INVALID,
    )


def _empty_catalog_observation(*, scope: PrivateScanScope | None = None):
    active_scope = scope or _scope()
    endpoint = ResolvedLanEndpoint.from_scope(active_scope, "192.168.50.2", 1234)
    return _make_observation(
        endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=TransportSecurity.PLAIN_HTTP,
        api_shape=ApiShape.OPENAI_COMPATIBLE,
        catalog=(),
        catalog_complete=True,
        catalog_truncated=False,
        capabilities=tuple(LanCapabilityEvidence.not_run(item) for item in CapabilityName),
        capability_route=None,
        selected_model_id=None,
        failure_category=LanFailureCategory.CATALOG_EMPTY,
    )


def _ollama_observation(
    *,
    scope: PrivateScanScope | None = None,
    models: tuple[str, ...] = ("alpha",),
):
    active_scope = scope or _scope()
    endpoint = ResolvedLanEndpoint.from_scope(active_scope, "192.168.50.2", 1234)
    return _make_observation(
        endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=TransportSecurity.PLAIN_HTTP,
        api_shape=ApiShape.OLLAMA_COMPATIBLE,
        catalog=models,
        catalog_complete=True,
        catalog_truncated=False,
        capabilities=_capabilities(),
        capability_route=LanRequestRoute.OLLAMA_GENERATION.path,
        selected_model_id=models[0],
        failure_category=None,
    )


def _persist_completed_scan(
    state: AgentStateStore,
    *,
    scan_id: str,
    observation,
    scope: PrivateScanScope | None = None,
    observed_at: datetime = NOW,
    owner_principal: str = OWNER,
    source: str = "active",
):
    active_scope = scope or _scope()
    ledger = LanDiscoveryLedger(state)
    draft = ledger.create_scan(
        scan_id=scan_id,
        owner_principal=owner_principal,
        confirmed_interface_id=active_scope.interface.interface_id,
        network=active_scope.network,
        limits={
            "known_model_service_ports": [1234, 8000, 8080, 11434],
            "max_active_hosts": 256,
            "max_scan_concurrency": 16,
            "tcp_connect_timeout_seconds": 0.75,
            "http_probe_timeout_seconds": 2.0,
            "total_scan_deadline_seconds": 45.0,
            "max_probe_response_bytes": 262144,
            "max_discovered_models": 8,
            "mdns_window_seconds": 2.5,
        },
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        scan_id,
        "running",
        expected_revision=draft.revision,
    )
    persisted = ledger.append_observation(
        scan_id,
        lan_observation_to_draft(
            observation,
            scope=active_scope,
            freshness_timestamp=observed_at.isoformat().replace("+00:00", "Z"),
            source=source,
        ),
        expected_revision=running.revision,
    )
    current = ledger.get_scan(scan_id)
    assert current is not None
    completed = ledger.transition_scan(
        scan_id,
        "completed",
        expected_revision=current.revision,
        terminal_reason="completed",
        candidate_count=1,
        error_count=0,
        timeout_count=0,
    )
    assert completed.terminal_receipt_digest is not None
    return persisted, completed


def _persist_completed_scan_v2(
    state: AgentStateStore,
    *,
    scan_id: str,
    observations: tuple[object, ...],
    scope: PrivateScanScope | None = None,
):
    active_scope = scope or _scope()
    limits = {
        "known_model_service_ports": [1234, 8000, 8080, 11434],
        "max_active_hosts": 256,
        "max_scan_concurrency": 16,
        "tcp_connect_timeout_seconds": 0.75,
        "http_probe_timeout_seconds": 2.0,
        "total_scan_deadline_seconds": 45.0,
        "max_probe_response_bytes": 262144,
        "max_discovered_models": 8,
        "mdns_window_seconds": 2.5,
    }
    ledger = LanDiscoveryLedger(state)
    draft = ledger.create_scan(
        scan_id=scan_id,
        owner_principal=OWNER,
        confirmed_interface_id=active_scope.interface.interface_id,
        network=active_scope.network,
        limits=limits,
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = ledger.claim_scan_start(
        draft.scan_id,
        owner_principal=OWNER,
        expected_revision=draft.revision,
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event={
            "schema": "kestrel.lan.scan-preview.v1",
            "owner_principal": OWNER,
            "interface_id": active_scope.interface.interface_id,
            "network": active_scope.network,
            "limits": limits,
            "active_host_count": len(active_scope.active_hosts),
            "passive_or_manual_only": active_scope.passive_or_manual_only,
            "port_count": 4,
            "mdns_status": "available",
            "server_version": "kestrel-test",
            "contract_version": "kestrel.lan.preview-authorization.v1",
            "preview_digest": PREVIEW_DIGEST,
            "expires_at": "2099-08-01T12:00:30Z",
        },
    )
    drafts = tuple(
        lan_observation_to_draft(
            observation,  # type: ignore[arg-type]
            scope=active_scope,
            freshness_timestamp="2026-08-01T12:00:00Z",
            source="active",
        )
        for observation in observations
    )
    completed = ledger.commit_scan_terminal(
        running.scan_id,
        owner_principal=OWNER,
        expected_revision=running.revision,
        status="completed",
        terminal_reason="scan_complete",
        cancel_reason=None,
        observations=drafts,
        mdns_status="available",
        planned_count=len(drafts),
        admitted_count=len(drafts),
        completed_count=len(drafts),
        error_category_counts={},
        timeout_count=0,
        evidence_complete=True,
        unknown_inflight_count=0,
    )
    persisted = tuple(ledger.list_observations(scan_id))
    assert len(persisted) == len(drafts)
    return persisted, completed


def _persist_completed_manual_scan(
    state: AgentStateStore,
    *,
    scan_id: str,
    observation,
    scope: PrivateScanScope | None = None,
    observed_at: datetime = NOW,
):
    """Persist Task 7B evidence only through the receipt-authenticated public APIs."""

    active_scope = scope or _manual_scope()
    port = observation.endpoint.port
    limits = _manual_limits(port)
    ledger = LanDiscoveryLedger(state)
    running = ledger.create_and_claim_manual_scan(
        scan_id=scan_id,
        owner_principal=OWNER,
        confirmed_interface_id=active_scope.interface.interface_id,
        network=active_scope.network,
        limits=limits,
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event=_manual_preview_event(active_scope, port=port),
        expected_revision=0,
    )
    assert running.status == "running"
    assert running.revision == 2
    draft = lan_observation_to_draft(
        observation,
        scope=active_scope,
        freshness_timestamp=observed_at.isoformat().replace("+00:00", "Z"),
        source="manual",
    )
    completed = ledger.commit_scan_terminal(
        running.scan_id,
        owner_principal=OWNER,
        expected_revision=running.revision,
        status="completed",
        terminal_reason="scan_complete",
        cancel_reason=None,
        observations=(draft,),
        mdns_status="unavailable",
        planned_count=1,
        admitted_count=1,
        completed_count=1,
        error_category_counts={},
        timeout_count=0,
        evidence_complete=True,
        unknown_inflight_count=0,
    )
    rows = tuple(ledger.list_observations(scan_id))
    assert len(rows) == 1
    assert completed.terminal_receipt_digest is not None
    return rows[0], completed


def _unchecked_clone(value, **changes: object):
    cloned = object.__new__(type(value))
    for field_name in value.__dataclass_fields__:
        object.__setattr__(
            cloned,
            field_name,
            changes.get(field_name, getattr(value, field_name)),
        )
    return cloned


def _persist_scan_with_status(
    state: AgentStateStore,
    *,
    scan_id: str,
    observation,
    status: str,
):
    scope = _scope()
    ledger = LanDiscoveryLedger(state)
    draft = ledger.create_scan(
        scan_id=scan_id,
        owner_principal=OWNER,
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits={
            "known_model_service_ports": [1234, 8000, 8080, 11434],
            "max_active_hosts": 256,
            "max_scan_concurrency": 16,
            "tcp_connect_timeout_seconds": 0.75,
            "http_probe_timeout_seconds": 2.0,
            "total_scan_deadline_seconds": 45.0,
            "max_probe_response_bytes": 262144,
            "max_discovered_models": 8,
            "mdns_window_seconds": 2.5,
        },
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = ledger.transition_scan(scan_id, "running", expected_revision=draft.revision)
    ledger.append_observation(
        scan_id,
        lan_observation_to_draft(
            observation,
            scope=scope,
            freshness_timestamp="2026-08-01T12:00:00Z",
            source="active",
        ),
        expected_revision=running.revision,
    )
    current = ledger.get_scan(scan_id)
    assert current is not None
    if status == "running":
        return current
    if status == "cancelled":
        current = ledger.transition_scan(
            scan_id,
            "cancelling",
            expected_revision=current.revision,
            cancel_reason="owner_cancelled",
        )
    return ledger.transition_scan(
        scan_id,
        status,
        expected_revision=current.revision,
        cancel_reason="owner_cancelled" if status == "cancelled" else None,
        terminal_reason=status,
        candidate_count=1,
        error_count=1 if status == "failed" else 0,
        timeout_count=0,
    )


def _provider_id(endpoint_binding_digest: str) -> str:
    digest = _digest(
        {
            "schema": "kestrel.lan.provider-binding.v1",
            "endpoint_binding_digest": endpoint_binding_digest,
        }
    )
    return "lan-provider-" + digest.removeprefix("sha256:")


def _target_id(provider_profile_id: str, model_id: str) -> str:
    digest = _digest(
        {
            "schema": "kestrel.lan.model-target.v1",
            "provider_profile_id": provider_profile_id,
            "model_id": model_id,
        }
    )
    return "lan-target-" + digest.removeprefix("sha256:")


def _import_request(
    observation,
    completed,
    *,
    profile_revision: int,
    target_revisions: tuple[tuple[str, int], ...],
) -> LanImportRequest:
    assert completed.terminal_receipt_digest is not None
    return LanImportRequest(
        scan_id=completed.scan_id,
        endpoint_binding_digest=observation.endpoint_binding_digest,
        expected_terminal_receipt_digest=completed.terminal_receipt_digest,
        expected_observation_digest=observation.observation_digest,
        expected_profile_revision=profile_revision,
        expected_target_revisions=tuple(
            LanExpectedRevision(resource_id, revision) for resource_id, revision in target_revisions
        ),
    )


def _rewrite_terminal_receipt(
    state: AgentStateStore,
    scan_id: str,
    *,
    mutate,
    recompute_digest: bool,
) -> None:
    ledger = LanDiscoveryLedger(state)
    scan = ledger.get_scan(scan_id)
    assert scan is not None and scan.terminal_receipt is not None
    receipt = json.loads(json.dumps(scan.terminal_receipt))
    mutate(receipt)
    receipt_json = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    receipt_digest = _digest(receipt) if recompute_digest else scan.terminal_receipt_digest
    with state._connect() as connection:
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_scan_update_immutable")
        connection.execute(
            """
            UPDATE routing_lan_scans
            SET terminal_receipt_json = ?, terminal_receipt_digest = ?
            WHERE scan_id = ?
            """,
            (receipt_json, receipt_digest, scan_id),
        )
    LanDiscoveryLedger(state)


def _rewrite_scan_limits_and_receipt(
    state: AgentStateStore,
    scan_id: str,
    limits: dict[str, object],
) -> str:
    ledger = LanDiscoveryLedger(state)
    scan = ledger.get_scan(scan_id)
    assert scan is not None and scan.terminal_receipt is not None
    receipt = json.loads(json.dumps(scan.terminal_receipt))
    limits_digest = _digest(limits)
    receipt["limits"] = limits
    receipt["limits_digest"] = limits_digest
    receipt_json = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    receipt_digest = _digest(receipt)
    limits_json = json.dumps(
        limits,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    with state._connect() as connection:
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_scan_update_immutable")
        connection.execute(
            """
            UPDATE routing_lan_scans
            SET limits_json = ?, limits_digest = ?,
                terminal_receipt_json = ?, terminal_receipt_digest = ?
            WHERE scan_id = ?
            """,
            (limits_json, limits_digest, receipt_json, receipt_digest, scan_id),
        )
    LanDiscoveryLedger(state)
    return receipt_digest


def _rewrite_scan_started_event(
    state: AgentStateStore,
    scan_id: str,
    *,
    mutate,
) -> None:
    with state._connect() as connection:
        row = connection.execute(
            """
            SELECT sequence, payload_json FROM routing_lan_scan_events
            WHERE scan_id = ? AND event_type = 'scan_started'
            """,
            (scan_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        mutate(payload)
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_event_update_immutable")
        connection.execute(
            """
            UPDATE routing_lan_scan_events SET payload_json = ?
            WHERE scan_id = ? AND sequence = ?
            """,
            (
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                scan_id,
                row["sequence"],
            ),
        )
    LanDiscoveryLedger(state)


def _rewrite_scan_started_event_row(
    state: AgentStateStore,
    scan_id: str,
    *,
    sequence: int | None = None,
    created_at: str | None = None,
) -> None:
    with state._connect() as connection:
        row = connection.execute(
            """
            SELECT sequence, created_at FROM routing_lan_scan_events
            WHERE scan_id = ? AND event_type = 'scan_started'
            """,
            (scan_id,),
        ).fetchone()
        assert row is not None
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_event_update_immutable")
        connection.execute(
            """
            UPDATE routing_lan_scan_events
            SET sequence = ?, created_at = ?
            WHERE scan_id = ? AND sequence = ?
            """,
            (
                row["sequence"] if sequence is None else sequence,
                row["created_at"] if created_at is None else created_at,
                scan_id,
                row["sequence"],
            ),
        )
    LanDiscoveryLedger(state)


def _rewrite_scan_started_at_encoding(state: AgentStateStore, scan_id: str) -> None:
    ledger = LanDiscoveryLedger(state)
    scan = ledger.get_scan(scan_id)
    assert scan is not None and scan.started_at is not None and scan.terminal_receipt is not None
    forged_started_at = scan.started_at.replace("+00:00", "Z")
    assert forged_started_at != scan.started_at
    receipt = json.loads(json.dumps(scan.terminal_receipt))
    receipt["started_at"] = forged_started_at
    receipt_json = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    with state._connect() as connection:
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_scan_update_immutable")
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_event_update_immutable")
        connection.execute(
            """
            UPDATE routing_lan_scans
            SET started_at = ?, terminal_receipt_json = ?, terminal_receipt_digest = ?
            WHERE scan_id = ?
            """,
            (forged_started_at, receipt_json, _digest(receipt), scan_id),
        )
        connection.execute(
            """
            UPDATE routing_lan_scan_events SET created_at = ?
            WHERE scan_id = ? AND event_type = 'scan_started'
            """,
            (forged_started_at, scan_id),
        )
    LanDiscoveryLedger(state)


def _rewrite_observation_and_membership(
    state: AgentStateStore,
    scan_id: str,
    endpoint_id: str,
    *,
    public_payload_update: dict[str, object] | None = None,
    freshness_timestamp: str | None = None,
) -> None:
    ledger = LanDiscoveryLedger(state)
    scan = ledger.get_scan(scan_id)
    assert scan is not None and scan.terminal_receipt is not None
    receipt = json.loads(json.dumps(scan.terminal_receipt))
    matching = [item for item in receipt["observations"] if item["endpoint_id"] == endpoint_id]
    assert len(matching) == 1
    receipt_observation = matching[0]
    public_payload = dict(receipt_observation["public_payload"])
    if public_payload_update is not None:
        public_payload.update(public_payload_update)
        receipt_observation["public_payload"] = public_payload
    if freshness_timestamp is not None:
        receipt_observation["freshness_timestamp"] = freshness_timestamp
    receipt_json = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    with state._connect() as connection:
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_observation_update_immutable")
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_scan_update_immutable")
        if public_payload_update is not None:
            connection.execute(
                """
                UPDATE routing_lan_observations
                SET public_payload_json = ?
                WHERE scan_id = ? AND endpoint_id = ?
                """,
                (
                    json.dumps(
                        public_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    scan_id,
                    endpoint_id,
                ),
            )
        if freshness_timestamp is not None:
            connection.execute(
                """
                UPDATE routing_lan_observations
                SET freshness_timestamp = ?
                WHERE scan_id = ? AND endpoint_id = ?
                """,
                (freshness_timestamp, scan_id, endpoint_id),
            )
        connection.execute(
            """
            UPDATE routing_lan_scans
            SET terminal_receipt_json = ?, terminal_receipt_digest = ?
            WHERE scan_id = ?
            """,
            (receipt_json, _digest(receipt), scan_id),
        )
    LanDiscoveryLedger(state)


def _endpoint_fingerprint_digest(protected: dict[str, object]) -> str:
    preimage = {
        "schema": "kestrel.lan.endpoint-fingerprint.v1",
        "endpoint_binding_digest": protected["endpoint_binding_digest"],
        "interface_id": protected["interface_id"],
        "confirmed_network": protected["confirmed_network"],
        "address": protected["address"],
        "port": protected["port"],
        "transport_security": protected["transport_security"],
        "certificate_sha256": protected["certificate_sha256"],
        "api_shape": protected["api_shape"],
    }
    if (
        protected.get("observation_source") == "manual"
        or protected.get("endpoint_kind") == "manual"
    ):
        preimage["observation_source"] = protected["observation_source"]
        preimage["endpoint_kind"] = protected["endpoint_kind"]
    return _digest(preimage)


def _review_material_digest(
    protected: dict[str, object],
    *,
    trust_class: str,
    privacy_acknowledgement_digest: str | None,
    intended_roles: tuple[str, ...],
    task_family_affinities: tuple[str, ...],
) -> str:
    preimage = {
        "schema": "kestrel.lan.material-binding.v1",
        "provider_profile_id": protected["provider_profile_id"],
        "target_id": _target_id(
            str(protected["provider_profile_id"]),
            str(protected["model_id"]),
        ),
        "endpoint_fingerprint": protected["endpoint_fingerprint"],
        "endpoint_binding_digest": protected["endpoint_binding_digest"],
        "interface_id": protected["interface_id"],
        "confirmed_network": protected["confirmed_network"],
        "address": protected["address"],
        "port": protected["port"],
        "transport_security": protected["transport_security"],
        "certificate_sha256": protected["certificate_sha256"],
        "api_shape": protected["api_shape"],
        "model_id": protected["model_id"],
        "catalog_digest": protected["catalog_digest"],
        "capability_digest": protected["capability_digest"],
        "capability_claims": protected["capability_claims"],
        "trust_class": trust_class,
        "privacy_acknowledgement_digest": privacy_acknowledgement_digest,
        "intended_roles": list(intended_roles),
        "task_family_affinities": list(task_family_affinities),
    }
    if (
        protected.get("observation_source") == "manual"
        or protected.get("endpoint_kind") == "manual"
    ):
        preimage["observation_source"] = protected["observation_source"]
        preimage["endpoint_kind"] = protected["endpoint_kind"]
    return _digest(preimage)


def _exact_review_request(
    *,
    owner: str,
    profile_revision: int,
    target_revision: int,
    target_id: str,
    protected: dict[str, object],
    intended_roles: tuple[str, ...] = ("reviewer", "worker"),
    task_family_affinities: tuple[str, ...] = ("code-repair",),
    enabled: bool = False,
) -> tuple[LanReviewRequest, str, str]:
    roles = tuple(sorted(set(intended_roles)))
    families = tuple(sorted(set(task_family_affinities)))
    stale_reasons = tuple(protected["stale_reasons"])
    transition_receipt = protected["stale_transition_terminal_receipt_digest"]
    privacy_digest = _digest(
        {
            "schema": "kestrel.lan.privacy-acknowledgement.v1",
            "owner_principal": owner,
            "provider_profile_id": protected["provider_profile_id"],
            "target_id": target_id,
            "observation_digest": protected["observation_digest"],
            "endpoint_fingerprint": protected["endpoint_fingerprint"],
            "expected_profile_revision": profile_revision,
            "expected_target_revision": target_revision,
            "trust_class": "operator_confirmed",
            "intended_roles": list(roles),
            "task_family_affinities": list(families),
            "enabled": enabled,
            "privacy_acknowledged": True,
            "expected_stale_reasons": list(stale_reasons),
            "stale_transition_terminal_receipt_digest": transition_receipt,
        }
    )
    reviewed_material = _review_material_digest(
        protected,
        trust_class="operator_confirmed",
        privacy_acknowledgement_digest=privacy_digest,
        intended_roles=roles,
        task_family_affinities=families,
    )
    review_digest = _digest(
        {
            "schema": "kestrel.lan.review.v1",
            "privacy_acknowledgement_digest": privacy_digest,
            "expected_terminal_receipt_digest": protected["terminal_receipt_digest"],
            "expected_observation_digest": protected["observation_digest"],
            "pre_review_material_binding_digest": protected["material_binding_digest"],
            "reviewed_material_binding_digest": reviewed_material,
            "expected_stale_reasons": list(stale_reasons),
            "stale_transition_terminal_receipt_digest": transition_receipt,
        }
    )
    return (
        LanReviewRequest(
            target_id=target_id,
            expected_profile_revision=profile_revision,
            expected_target_revision=target_revision,
            expected_terminal_receipt_digest=str(protected["terminal_receipt_digest"]),
            expected_observation_digest=str(protected["observation_digest"]),
            expected_endpoint_fingerprint=str(protected["endpoint_fingerprint"]),
            expected_material_binding_digest=str(protected["material_binding_digest"]),
            expected_review_digest=review_digest,
            expected_stale_reasons=stale_reasons,
            trust_class="operator_confirmed",
            intended_roles=roles,
            task_family_affinities=families,
            privacy_acknowledged=True,
            enabled=enabled,
        ),
        privacy_digest,
        reviewed_material,
    )


def _import_first_positive(
    state: AgentStateStore,
    *,
    scan_id: str = "scan-initial",
    scope: PrivateScanScope | None = None,
    models: tuple[str, ...] = ("alpha",),
    observed_at: datetime = NOW,
    clock=lambda: NOW,
):
    active_scope = scope or _scope()
    observation = _positive_observation(scope=active_scope, models=models)
    _row, completed = _persist_completed_scan(
        state,
        scan_id=scan_id,
        scope=active_scope,
        observation=observation,
        observed_at=observed_at,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_ids = tuple(_target_id(provider_id, model) for model in models)
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=clock)
    result = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=tuple((target_id, 0) for target_id in target_ids),
        ),
        authenticated_owner_principal=OWNER,
    )
    return observation, completed, registry, service, result, provider_id, target_ids


def test_task4_adapter_is_exact_bounded_and_recomputes_all_four_digests() -> None:
    scope = _scope()
    observation = _positive_observation(scope=scope, models=("alpha", "beta"))

    draft = lan_observation_to_draft(
        observation,
        scope=scope,
        freshness_timestamp="2026-08-01T12:00:00Z",
        source="active",
    )

    assert draft.endpoint_id == observation.endpoint_binding_digest
    assert draft.interface_id == observation.endpoint.interface_id
    assert draft.address == "192.168.50.2"
    assert draft.port == 1234
    assert draft.public_payload["schema"] == "kestrel.lan.durable-observation.v1"
    assert draft.api_shape == "openai_compatible"
    assert draft.tls_enabled is False
    assert draft.certificate_sha256 is None
    assert set(draft.public_payload) == {
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
    assert draft.public_payload["capabilities"] == [
        item.to_digest_payload() for item in observation.capabilities
    ]
    assert "public_error" not in json.dumps(draft.public_payload)
    assert "renderer text" not in json.dumps(draft.public_payload)


def test_task5a_result_field_order_and_freshness_constants_are_exact() -> None:
    assert tuple(field.name for field in fields(LanImportResult)) == (
        "profile",
        "targets",
        "affected_target_ids",
        "invalidated_binding_digests",
        "stale_reasons_by_target",
        "observation_digest",
        "endpoint_fingerprint",
        "outage_observed",
    )
    assert tuple(field.name for field in fields(LanReviewResult)) == (
        "profile",
        "target",
        "material_binding_digest",
        "privacy_acknowledgement_digest",
    )
    assert LAN_OBSERVATION_MAX_AGE_SECONDS == 300
    assert LAN_OBSERVATION_MAX_FUTURE_SKEW_SECONDS == 5


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-08-01T12:00:00Z", "2026-08-01T12:00:00Z"),
        ("2026-08-01T12:00:00+00:00", "2026-08-01T12:00:00Z"),
        ("2026-08-01T12:00:00.123Z", "2026-08-01T12:00:00.123000Z"),
        (
            "2026-08-01T12:00:00.123+00:00",
            "2026-08-01T12:00:00.123000Z",
        ),
    ],
)
def test_task4_adapter_normalizes_exact_utc_rfc3339_timestamps(
    timestamp: str,
    expected: str,
) -> None:
    draft = lan_observation_to_draft(
        _positive_observation(),
        scope=_scope(),
        freshness_timestamp=timestamp,
    )
    assert draft.freshness_timestamp == expected


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-01T12:00:00",
        "2026-08-01T08:00:00-04:00",
        "2026-08-01 12:00:00Z",
        "2026-08-01T12:00:00.1234567Z",
        1,
    ],
)
def test_task4_adapter_rejects_noncanonical_or_non_utc_timestamps(
    timestamp: object,
) -> None:
    with pytest.raises(ValueError, match="timestamp|UTC|RFC3339"):
        lan_observation_to_draft(
            _positive_observation(),
            scope=_scope(),
            freshness_timestamp=timestamp,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "field",
    [
        "endpoint_binding_digest",
        "catalog_digest",
        "capability_digest",
        "observation_digest",
    ],
)
def test_task4_adapter_rejects_forged_or_cross_domain_digest(field: str) -> None:
    scope = _scope()
    observation = _positive_observation(scope=scope)
    forged = _unchecked_clone(observation, **{field: "sha256:" + "0" * 64})

    with pytest.raises(ValueError, match="digest"):
        lan_observation_to_draft(
            forged,
            scope=scope,
            freshness_timestamp="2026-08-01T12:00:00Z",
        )


@pytest.mark.parametrize(
    ("field", "substitution"),
    [
        ("endpoint_binding_digest", "catalog_digest"),
        ("catalog_digest", "capability_digest"),
        ("capability_digest", "observation_digest"),
        ("observation_digest", "foreign_observation_digest"),
        ("endpoint_binding_digest", "foreign_endpoint_binding_digest"),
    ],
)
def test_adapter_rejects_valid_cross_domain_and_cross_endpoint_substitution(
    field: str,
    substitution: str,
) -> None:
    scope = _scope()
    observation = _positive_observation(scope=scope)
    foreign = _positive_observation(scope=scope, address="192.168.50.3")
    values = {
        "catalog_digest": observation.catalog_digest,
        "capability_digest": observation.capability_digest,
        "observation_digest": observation.observation_digest,
        "foreign_observation_digest": foreign.observation_digest,
        "foreign_endpoint_binding_digest": foreign.endpoint_binding_digest,
    }
    forged = _unchecked_clone(observation, **{field: values[substitution]})

    with pytest.raises(ValueError, match="digest"):
        lan_observation_to_draft(
            forged,
            scope=scope,
            freshness_timestamp="2026-08-01T12:00:00Z",
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "endpoint_binding_digest",
        "catalog_digest",
        "capability_digest",
        "observation_digest",
    ],
)
def test_append_recomputes_task4_digests_after_adapter_and_writes_nothing(
    tmp_path: Path,
    tamper: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scope = _scope()
    observation = _positive_observation(scope=scope)
    draft = lan_observation_to_draft(
        observation,
        scope=scope,
        freshness_timestamp="2026-08-01T12:00:00Z",
    )
    if tamper == "observation_digest":
        public_payload = dict(draft.public_payload)
        public_payload["observation_digest"] = "sha256:" + "9" * 64
        forged = replace(draft, public_payload=public_payload)
    else:
        field = {
            "endpoint_binding_digest": "endpoint_id",
            "catalog_digest": "catalog_digest",
            "capability_digest": "capability_digest",
        }[tamper]
        forged = replace(draft, **{field: "sha256:" + "9" * 64})
    ledger = LanDiscoveryLedger(state)
    created = ledger.create_scan(
        scan_id=f"scan-append-forged-{tamper}",
        owner_principal=OWNER,
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits={
            "known_model_service_ports": [1234, 8000, 8080, 11434],
            "max_active_hosts": 256,
            "max_scan_concurrency": 16,
            "tcp_connect_timeout_seconds": 0.75,
            "http_probe_timeout_seconds": 2.0,
            "total_scan_deadline_seconds": 45.0,
            "max_probe_response_bytes": 262144,
            "max_discovered_models": 8,
            "mdns_window_seconds": 2.5,
        },
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        created.scan_id,
        "running",
        expected_revision=created.revision,
    )

    with pytest.raises(ValueError, match="Task 4|digest|preimage"):
        ledger.append_observation(
            running.scan_id,
            forged,
            expected_revision=running.revision,
        )
    assert ledger.get_scan(running.scan_id) == running
    assert ledger.list_observations(running.scan_id) == []


@pytest.mark.parametrize(
    ("address", "port"),
    [("192.168.50.2", 9999), ("8.8.8.8", 1234)],
)
def test_append_rejects_self_consistent_task4_unknown_port_or_public_endpoint(
    tmp_path: Path,
    address: str,
    port: int,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scope = _scope()
    endpoint = object.__new__(ResolvedLanEndpoint)
    object.__setattr__(endpoint, "interface_id", scope.interface.interface_id)
    object.__setattr__(endpoint, "address", address)
    object.__setattr__(endpoint, "port", port)
    observation = _make_observation(
        endpoint,
        reachability=Reachability.REACHABLE,
        transport_security=TransportSecurity.PLAIN_HTTP,
        api_shape=ApiShape.OPENAI_COMPATIBLE,
        catalog=("alpha",),
        catalog_complete=True,
        catalog_truncated=False,
        capabilities=_capabilities(),
        capability_route=LanRequestRoute.OPENAI_GENERATION.path,
        selected_model_id="alpha",
        failure_category=None,
    )
    template = lan_observation_to_draft(
        _positive_observation(scope=scope),
        scope=scope,
        freshness_timestamp="2026-08-01T12:00:00Z",
    )
    payload = {
        **template.public_payload,
        "endpoint_binding_digest": observation.endpoint_binding_digest,
        "observation_digest": observation.observation_digest,
    }
    forged = replace(
        template,
        endpoint_id=observation.endpoint_binding_digest,
        address=address,
        port=port,
        catalog_digest=observation.catalog_digest,
        capability_digest=observation.capability_digest,
        public_payload=payload,
    )
    ledger = LanDiscoveryLedger(state)
    created = ledger.create_scan(
        scan_id=f"scan-self-consistent-{port}",
        owner_principal=OWNER,
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits={
            "known_model_service_ports": [1234, 8000, 8080, 11434],
            "max_active_hosts": 256,
            "max_scan_concurrency": 16,
            "tcp_connect_timeout_seconds": 0.75,
            "http_probe_timeout_seconds": 2.0,
            "total_scan_deadline_seconds": 45.0,
            "max_probe_response_bytes": 262144,
            "max_discovered_models": 8,
            "mdns_window_seconds": 2.5,
        },
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        created.scan_id,
        "running",
        expected_revision=created.revision,
    )

    with pytest.raises(ValueError, match="port|endpoint|eligible"):
        ledger.append_observation(
            running.scan_id,
            forged,
            expected_revision=running.revision,
        )
    assert ledger.get_scan(running.scan_id) == running
    assert ledger.list_observations(running.scan_id) == []


def test_adapter_rejects_dictionary_subclass_and_mismatched_scope() -> None:
    from nested_memvid_agent.lan_scanner import LanEndpointObservation

    scope = _scope()
    observation = _positive_observation(scope=scope)

    class DerivedObservation(LanEndpointObservation):
        pass

    derived = object.__new__(DerivedObservation)
    for field_name in observation.__dataclass_fields__:
        object.__setattr__(derived, field_name, getattr(observation, field_name))
    mismatched_interface = NetworkInterface.from_addresses(
        os_identity="test:en1",
        display_name="other interface",
        addresses=("192.168.50.1/29",),
    )
    mismatched_scope = PrivateScanScope.from_request(
        mismatched_interface,
        "192.168.50.0/29",
    )

    for rejected in (observation.__dict__, derived):
        with pytest.raises(ValueError, match="typed|exact|observation"):
            lan_observation_to_draft(
                rejected,  # type: ignore[arg-type]
                scope=scope,
                freshness_timestamp="2026-08-01T12:00:00Z",
            )
    with pytest.raises(ValueError, match="scope|interface|endpoint"):
        lan_observation_to_draft(
            observation,
            scope=mismatched_scope,
            freshness_timestamp="2026-08-01T12:00:00Z",
        )


@pytest.mark.parametrize("malformation", ["order", "provenance", "status"])
def test_adapter_rejects_malformed_capability_tuple_even_when_type_is_forged(
    malformation: str,
) -> None:
    scope = _scope()
    observation = _positive_observation(scope=scope)
    if malformation == "order":
        capabilities = tuple(reversed(observation.capabilities))
    else:
        generation = object.__new__(LanCapabilityEvidence)
        object.__setattr__(generation, "capability", CapabilityName.GENERATION)
        object.__setattr__(generation, "supported", True)
        object.__setattr__(
            generation,
            "provenance",
            (
                CapabilityProvenance.NOT_RUN
                if malformation == "provenance"
                else CapabilityProvenance.OBSERVED
            ),
        )
        object.__setattr__(
            generation,
            "status",
            (
                CapabilityObservationStatus.OBSERVED_PASS
                if malformation == "provenance"
                else CapabilityObservationStatus.NOT_RUN
            ),
        )
        capabilities = (generation, *observation.capabilities[1:])
    forged = _unchecked_clone(observation, capabilities=capabilities)

    with pytest.raises(ValueError, match="capability|digest"):
        lan_observation_to_draft(
            forged,
            scope=scope,
            freshness_timestamp="2026-08-01T12:00:00Z",
        )


def test_adapter_excludes_all_untrusted_raw_identity_and_secret_material() -> None:
    scope = _scope()
    observation = _unchecked_clone(
        _positive_observation(scope=scope),
        public_error=("raw-body raw-header hostname.local mdns-display credential-secret"),
    )

    draft = lan_observation_to_draft(
        observation,
        scope=scope,
        freshness_timestamp="2026-08-01T12:00:00Z",
    )
    serialized = json.dumps(draft.public_payload, sort_keys=True)

    for forbidden in (
        "raw-body",
        "raw-header",
        "hostname.local",
        "mdns-display",
        "credential-secret",
        "public_error",
    ):
        assert forbidden not in serialized


def test_adapter_and_import_never_call_generic_probe_catalog_secret_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import urllib.request

    import nested_memvid_agent.provider_probe as provider_probe

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-no-generic-probe",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("generic discovery/network service was invoked")

    monkeypatch.setattr(provider_probe.ProviderProbeService, "__init__", forbidden)
    monkeypatch.setattr(
        provider_probe.BoundedOpenAICompatibleProbeBackend,
        "__init__",
        forbidden,
    )
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    draft = lan_observation_to_draft(
        observation,
        scope=_scope(),
        freshness_timestamp="2026-08-01T12:00:00Z",
    )
    assert draft.endpoint_id == observation.endpoint_binding_digest
    result = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert result.profile is not None


@pytest.mark.parametrize("revision", [False, True, -1, 1.0, "1"])
def test_revision_contracts_reject_non_exact_nonnegative_integers(revision: object) -> None:
    with pytest.raises(ValueError, match="revision"):
        LanExpectedRevision("lan-target-" + "1" * 64, revision)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("request_type", "revision"),
    [
        (request_type, revision)
        for request_type in (
            "import_profile",
            "import_nested_target",
            "replacement_profile",
            "review_profile",
            "review_target",
        )
        for revision in (False, True, -1, 1.0, "1")
    ],
)
def test_all_lan_request_revision_fields_are_exact_nonnegative_ints(
    request_type: str,
    revision: object,
) -> None:
    digest = "sha256:" + "1" * 64
    with pytest.raises(ValueError, match="revision"):
        if request_type == "import_profile":
            LanImportRequest(
                scan_id="scan",
                endpoint_binding_digest=digest,
                expected_terminal_receipt_digest=digest,
                expected_observation_digest=digest,
                expected_profile_revision=revision,  # type: ignore[arg-type]
                expected_target_revisions=(),
            )
        elif request_type == "import_nested_target":
            nested = object.__new__(LanExpectedRevision)
            object.__setattr__(nested, "resource_id", "lan-target-" + "1" * 64)
            object.__setattr__(nested, "revision", revision)
            LanImportRequest(
                scan_id="scan",
                endpoint_binding_digest=digest,
                expected_terminal_receipt_digest=digest,
                expected_observation_digest=digest,
                expected_profile_revision=0,
                expected_target_revisions=(nested,),
            )
        elif request_type == "replacement_profile":
            LanReplacementConfirmation(
                provider_profile_id="lan-provider-" + "1" * 64,
                expected_profile_revision=revision,  # type: ignore[arg-type]
                expected_endpoint_fingerprint=digest,
                expected_material_binding_digests=(digest,),
            )
        else:
            profile_revision: object = 0
            target_revision: object = 0
            if request_type == "review_profile":
                profile_revision = revision
            else:
                target_revision = revision
            LanReviewRequest(
                target_id="lan-target-" + "1" * 64,
                expected_profile_revision=profile_revision,  # type: ignore[arg-type]
                expected_target_revision=target_revision,  # type: ignore[arg-type]
                expected_terminal_receipt_digest=digest,
                expected_observation_digest=digest,
                expected_endpoint_fingerprint=digest,
                expected_material_binding_digest=digest,
                expected_review_digest=digest,
                expected_stale_reasons=(),
                trust_class="operator_confirmed",
                intended_roles=(),
                task_family_affinities=(),
                privacy_acknowledged=True,
                enabled=False,
            )


def test_first_positive_import_creates_only_deterministic_disabled_drafts(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation(models=("alpha", "beta"))
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-first",
        observation=observation,
        observed_at=NOW,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_ids = tuple(_target_id(provider_id, model) for model in observation.catalog)
    service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)

    result = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=tuple((target_id, 0) for target_id in target_ids),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert result.outage_observed is False
    assert result.profile is not None
    assert result.profile.profile.profile_id == provider_id
    assert result.profile.profile.enabled is False
    assert result.profile.profile.secret_ref is None
    assert result.profile.profile.adapter == "lan-openai-compatible"
    assert result.profile.profile.base_url == "http://192.168.50.2:1234/v1"
    assert result.profile.profile.locality == "local"
    assert result.profile.profile.trust_class == "unconfirmed"
    assert result.profile.profile.max_concurrency == 1
    assert result.profile.profile.metadata.keys() == {"lan_discovery"}
    assert set(result.profile.profile.metadata["lan_discovery"]) == PROFILE_PROTECTED_KEYS
    assert len(provider_id) == len("lan-provider-") + 64
    assert tuple(item.target.target_id for item in result.targets) == target_ids
    selected, unselected = result.targets
    assert selected.target.enabled is False
    assert selected.target.locality == "local"
    assert selected.target.trust_class == "unconfirmed"
    assert selected.target.health == "unknown"
    assert selected.target.capability_tags == ("generation",)
    assert selected.target.role_affinities == ()
    assert selected.target.task_family_affinities == ()
    assert selected.target.max_context_tokens is None
    assert selected.target.supports_tools is False
    assert selected.target.supports_json is False
    assert selected.target.supports_vision is False
    assert selected.target.supports_reasoning is False
    assert selected.target.supports_streaming is False
    assert selected.target.quality_tier == 1
    assert selected.target.latency_tier == 3
    assert selected.target.operator_priority == 0
    assert selected.target.estimated_cost_usd is None
    assert selected.target.input_cost_per_million_usd is None
    assert selected.target.output_cost_per_million_usd is None
    assert selected.target.recent_failure_rate == 0.0
    assert selected.target.predicted_success is None
    assert unselected.target.capability_tags == ()
    selected_claims = selected.target.metadata["lan_discovery"]["capability_claims"]
    unselected_claims = unselected.target.metadata["lan_discovery"]["capability_claims"]
    expected_not_run = [
        {
            "capability": capability.value,
            "provenance": "not_run",
            "status": "not_run",
            "supported": None,
        }
        for capability in CapabilityName
    ]
    assert type(selected_claims) is list
    assert len(selected_claims) == 5
    assert selected_claims == [
        observation.capabilities[0].to_digest_payload(),
        *expected_not_run[1:],
    ]
    assert unselected_claims == expected_not_run
    assert "capability_route" not in json.dumps(selected_claims)
    assert "selected_model_id" not in json.dumps(selected_claims)
    assert all(item.target.metadata.keys() == {"lan_discovery"} for item in result.targets)
    assert all(
        set(item.target.metadata["lan_discovery"]) == TARGET_PROTECTED_KEYS
        for item in result.targets
    )
    assert all(
        item.target.metadata["lan_discovery"]["runtime_hardening"] is None
        for item in result.targets
    )


def test_same_endpoint_identity_is_stable_across_scan_and_timestamp_refresh(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _first_row, first_scan = _persist_completed_scan(
        state,
        scan_id="scan-one",
        observation=observation,
        observed_at=NOW - timedelta(seconds=20),
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)
    first = service.import_observation(
        _import_request(
            observation,
            first_scan,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    _second_row, second_scan = _persist_completed_scan(
        state,
        scan_id="scan-two",
        observation=observation,
        observed_at=NOW - timedelta(seconds=5),
    )
    second = service.import_observation(
        _import_request(
            observation,
            second_scan,
            profile_revision=1,
            target_revisions=((target_id, 1),),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert first.profile is not None and second.profile is not None
    assert first.profile.profile.profile_id == second.profile.profile.profile_id
    assert first.targets[0].target.target_id == second.targets[0].target.target_id
    assert (
        first.targets[0].target.metadata["lan_discovery"]["material_binding_digest"]
        == second.targets[0].target.metadata["lan_discovery"]["material_binding_digest"]
    )
    assert first.observation_digest == second.observation_digest


def test_incomplete_catalog_adds_new_draft_without_rewriting_existing_target(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    first_observation = _positive_observation(models=("alpha",))
    _row, first_scan = _persist_completed_scan(
        state,
        scan_id="scan-complete",
        observation=first_observation,
        observed_at=NOW - timedelta(seconds=10),
    )
    provider_id = _provider_id(first_observation.endpoint_binding_digest)
    alpha_id = _target_id(provider_id, "alpha")
    beta_id = _target_id(provider_id, "beta")
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    service.import_observation(
        _import_request(
            first_observation,
            first_scan,
            profile_revision=0,
            target_revisions=((alpha_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    profile_before = registry.get_provider_profile(provider_id)
    alpha_before = registry.get_model_target(alpha_id)
    assert profile_before is not None and alpha_before is not None
    second_observation = _positive_observation(
        models=("alpha", "beta"),
        catalog_complete=False,
    )
    _row, second_scan = _persist_completed_scan(
        state,
        scan_id="scan-incomplete",
        observation=second_observation,
        observed_at=NOW - timedelta(seconds=1),
    )

    result = service.import_observation(
        _import_request(
            second_observation,
            second_scan,
            profile_revision=1,
            target_revisions=((beta_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )

    alpha_after = registry.get_model_target(alpha_id)
    beta_after = registry.get_model_target(beta_id)
    assert alpha_after == alpha_before
    assert beta_after is not None and beta_after.target.enabled is False
    assert result.affected_target_ids == (beta_id,)
    assert result.profile is not None
    assert result.profile.revision == profile_before.revision + 1
    assert result.profile.profile.metadata["lan_discovery"]["scan_id"] == (second_scan.scan_id)
    assert result.profile.profile.metadata["lan_discovery"]["observation_digest"] == (
        second_observation.observation_digest
    )


def test_first_uncorrelated_outage_writes_nothing_and_has_no_fingerprint(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _outage_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-outage",
        observation=observation,
        observed_at=NOW,
    )
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)

    result = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert result.outage_observed is True
    assert result.endpoint_fingerprint is None
    assert result.profile is None
    assert result.targets == ()
    assert registry.list_provider_profiles() == []
    assert registry.list_model_targets() == []


def test_ollama_positive_import_is_secret_free_and_disabled(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _ollama_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-ollama",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")

    result = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert result.profile is not None
    assert result.profile.profile.adapter == "lan-ollama-compatible"
    assert result.profile.profile.base_url == "http://192.168.50.2:1234"
    assert result.profile.profile.secret_ref is None
    assert result.profile.profile.enabled is False
    assert result.targets[0].target.enabled is False
    assert result.targets[0].target.metadata["lan_discovery"]["runtime_hardening"] is None


def test_truncated_catalog_adds_drafts_without_staling_or_refreshing_existing(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        initial,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (alpha_id,),
    ) = _import_first_positive(state)
    profile_before = registry.get_provider_profile(provider_id)
    alpha_before = registry.get_model_target(alpha_id)
    assert profile_before is not None and alpha_before is not None
    models = ("alpha", "beta", "delta", "epsilon", "eta", "gamma", "theta", "zeta")
    truncated = _positive_observation(
        models=models,
        catalog_complete=False,
        catalog_truncated=True,
    )
    _row, scan = _persist_completed_scan(
        state,
        scan_id="scan-truncated",
        observation=truncated,
        observed_at=NOW + timedelta(seconds=1),
    )
    new_target_ids = tuple(_target_id(provider_id, model) for model in models[1:])

    result = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        _import_request(
            truncated,
            scan,
            profile_revision=profile_before.revision,
            target_revisions=tuple((target_id, 0) for target_id in new_target_ids),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert initial.endpoint_binding_digest == truncated.endpoint_binding_digest
    assert registry.get_model_target(alpha_id) == alpha_before
    assert result.profile is not None
    assert result.profile.revision == profile_before.revision + 1
    assert result.affected_target_ids == new_target_ids
    assert result.stale_reasons_by_target == ()
    assert all(
        registry.get_model_target(target_id).target.enabled is False  # type: ignore[union-attr]
        for target_id in new_target_ids
    )


def test_positive_refresh_after_expiry_preserves_stale_transition_and_clears_review(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    clock_now = [NOW]
    (
        observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state, clock=lambda: clock_now[0])
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    review, _privacy, reviewed_material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    reviewed = service.review_lan_target(
        review,
        authenticated_owner_principal=OWNER,
    )
    clock_now[0] = NOW + timedelta(seconds=301)
    _row, refresh_scan = _persist_completed_scan(
        state,
        scan_id="scan-positive-after-expiry",
        observation=observation,
        observed_at=clock_now[0],
    )

    refreshed = service.import_observation(
        _import_request(
            observation,
            refresh_scan,
            profile_revision=reviewed.profile.revision,
            target_revisions=((target_id, reviewed.target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert refreshed.profile is not None
    assert refreshed.profile.revision == reviewed.profile.revision + 1
    assert refreshed.targets[0].revision == reviewed.target.revision + 1
    profile_protected = refreshed.profile.profile.metadata["lan_discovery"]
    target_protected = refreshed.targets[0].target.metadata["lan_discovery"]
    assert profile_protected["stale_reasons"] == ["freshness_expired"]
    assert target_protected["stale_reasons"] == ["freshness_expired"]
    assert profile_protected["stale_transition_terminal_receipt_digest"] == (
        refresh_scan.terminal_receipt_digest
    )
    assert target_protected["stale_transition_terminal_receipt_digest"] == (
        refresh_scan.terminal_receipt_digest
    )
    assert target_protected["scan_id"] == refresh_scan.scan_id
    assert target_protected["reviewed"] is False
    assert target_protected["review_digest"] is None
    assert target_protected["privacy_acknowledgement_digest"] is None
    assert target_protected["material_binding_digest"] != reviewed_material
    assert refreshed.targets[0].target.trust_class == "unconfirmed"
    assert refreshed.targets[0].target.health == "unavailable"


def test_incomplete_endpoint_drift_fans_out_only_endpoint_reasons_and_recovers_present(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    clock_now = [NOW]
    (
        _initial,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (alpha_id, beta_id),
    ) = _import_first_positive(
        state,
        models=("alpha", "beta"),
        clock=lambda: clock_now[0],
    )
    narrowed_scope = _scope(network="192.168.50.0/30")
    drift = _positive_observation(
        scope=narrowed_scope,
        models=("alpha", "gamma"),
        catalog_complete=False,
    )
    gamma_id = _target_id(provider_id, "gamma")
    _row, drift_scan = _persist_completed_scan(
        state,
        scan_id="scan-incomplete-endpoint-drift",
        scope=narrowed_scope,
        observation=drift,
        observed_at=NOW,
    )
    drifted = service.import_observation(
        _import_request(
            drift,
            drift_scan,
            profile_revision=1,
            target_revisions=((alpha_id, 1), (gamma_id, 0), (beta_id, 1)),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert drifted.profile is not None
    assert drifted.profile.profile.metadata["lan_discovery"]["stale_reasons"] == ["network_changed"]
    assert dict(drifted.stale_reasons_by_target) == {
        alpha_id: ("network_changed",),
        gamma_id: ("network_changed",),
        beta_id: ("network_changed",),
    }
    alpha_drifted = registry.get_model_target(alpha_id)
    beta_drifted = registry.get_model_target(beta_id)
    gamma_drifted = registry.get_model_target(gamma_id)
    assert alpha_drifted is not None
    assert beta_drifted is not None
    assert gamma_drifted is not None
    assert alpha_drifted.target.metadata["lan_discovery"]["scan_id"] == (drift_scan.scan_id)
    assert gamma_drifted.target.metadata["lan_discovery"]["scan_id"] == (drift_scan.scan_id)
    assert beta_drifted.target.metadata["lan_discovery"]["scan_id"] != (drift_scan.scan_id)

    clock_now[0] = NOW + timedelta(seconds=1)
    stable_incomplete = _positive_observation(
        scope=narrowed_scope,
        models=("alpha",),
        catalog_complete=False,
    )
    _row, stable_scan = _persist_completed_scan(
        state,
        scan_id="scan-incomplete-present-recovery",
        scope=narrowed_scope,
        observation=stable_incomplete,
        observed_at=clock_now[0],
    )
    recovered = service.import_observation(
        _import_request(
            stable_incomplete,
            stable_scan,
            profile_revision=drifted.profile.revision,
            target_revisions=((alpha_id, alpha_drifted.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert recovered.affected_target_ids == (alpha_id,)
    assert dict(recovered.stale_reasons_by_target) == {
        alpha_id: ("network_changed",),
    }
    assert registry.get_model_target(beta_id) == beta_drifted
    assert registry.get_model_target(gamma_id) == gamma_drifted
    recovered_alpha = registry.get_model_target(alpha_id)
    assert recovered.profile is not None and recovered_alpha is not None
    recovered_protected = recovered_alpha.target.metadata["lan_discovery"]
    assert recovered_protected["scan_id"] == stable_scan.scan_id
    assert recovered_protected["stale_transition_terminal_receipt_digest"] == (
        stable_scan.terminal_receipt_digest
    )
    review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=recovered.profile.revision,
        target_revision=recovered_alpha.revision,
        target_id=alpha_id,
        protected=recovered_protected,
    )
    reviewed = service.review_lan_target(
        review,
        authenticated_owner_principal=OWNER,
    )
    assert reviewed.target.target.metadata["lan_discovery"]["reviewed"] is True


def test_uncorrelated_outage_ignores_unrelated_existing_binding(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile_before = registry.get_provider_profile(provider_id)
    target_before = registry.get_model_target(target_id)
    assert profile_before is not None and target_before is not None
    scope = _scope()
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.3", 1234)
    outage = _make_observation(
        endpoint,
        reachability=Reachability.UNREACHABLE,
        capabilities=tuple(LanCapabilityEvidence.not_run(item) for item in CapabilityName),
        failure_category=LanFailureCategory.TCP_TIMEOUT,
    )
    _row, scan = _persist_completed_scan(
        state,
        scan_id="scan-unrelated-outage",
        observation=outage,
    )

    result = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        _import_request(
            outage,
            scan,
            profile_revision=0,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert result.outage_observed is True
    assert result.endpoint_fingerprint is None
    assert registry.get_provider_profile(provider_id) == profile_before
    assert registry.get_model_target(target_id) == target_before


@pytest.mark.parametrize(
    ("observed_at", "accepted"),
    [
        (NOW - timedelta(seconds=300), True),
        (NOW - timedelta(seconds=301), False),
        (NOW + timedelta(seconds=5), True),
        (NOW + timedelta(seconds=6), False),
    ],
)
def test_freshness_boundaries_are_exact_and_conflicts_are_read_only(
    tmp_path: Path,
    observed_at: datetime,
    accepted: bool,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-freshness",
        observation=observation,
        observed_at=observed_at,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)
    request = _import_request(
        observation,
        completed,
        profile_revision=0,
        target_revisions=((target_id, 0),),
    )

    if accepted:
        assert (
            service.import_observation(
                request,
                authenticated_owner_principal=OWNER,
            ).profile
            is not None
        )
    else:
        with pytest.raises(LanDiscoveryConflict, match="lan_evidence"):
            service.import_observation(
                request,
                authenticated_owner_principal=OWNER,
            )
        assert RoutingLedger(state).list_provider_profiles() == []


def test_complete_model_omission_stales_missing_target_without_enabling_anything(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    initial = _positive_observation(models=("alpha", "beta"))
    _row, first_scan = _persist_completed_scan(
        state,
        scan_id="scan-ab",
        observation=initial,
        observed_at=NOW - timedelta(seconds=10),
    )
    provider_id = _provider_id(initial.endpoint_binding_digest)
    alpha_id = _target_id(provider_id, "alpha")
    beta_id = _target_id(provider_id, "beta")
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    service.import_observation(
        _import_request(
            initial,
            first_scan,
            profile_revision=0,
            target_revisions=((alpha_id, 0), (beta_id, 0)),
        ),
        authenticated_owner_principal=OWNER,
    )
    refreshed = _positive_observation(models=("alpha",))
    _row, second_scan = _persist_completed_scan(
        state,
        scan_id="scan-a",
        observation=refreshed,
        observed_at=NOW,
    )

    result = service.import_observation(
        _import_request(
            refreshed,
            second_scan,
            profile_revision=1,
            target_revisions=((alpha_id, 1), (beta_id, 1)),
        ),
        authenticated_owner_principal=OWNER,
    )

    stale = dict(result.stale_reasons_by_target)
    assert "model_missing" in stale[beta_id]
    beta = registry.get_model_target(beta_id)
    assert beta is not None
    assert beta.target.enabled is False
    assert beta.target.health == "unavailable"
    assert beta.target.trust_class == "unconfirmed"


def test_task5a_enabled_review_fails_before_any_mutation(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-review-enable",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    imported = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert imported.profile is not None
    before_profile = registry.get_provider_profile(provider_id)
    before_target = registry.get_model_target(target_id)
    assert before_profile is not None and before_target is not None
    request = LanReviewRequest(
        target_id=target_id,
        expected_profile_revision=before_profile.revision,
        expected_target_revision=before_target.revision,
        expected_terminal_receipt_digest=completed.terminal_receipt_digest,
        expected_observation_digest=observation.observation_digest,
        expected_endpoint_fingerprint=imported.endpoint_fingerprint,
        expected_material_binding_digest=before_target.target.metadata["lan_discovery"][
            "material_binding_digest"
        ],
        expected_review_digest="sha256:" + "0" * 64,
        expected_stale_reasons=(),
        trust_class="operator_confirmed",
        intended_roles=("worker",),
        task_family_affinities=("code-repair",),
        privacy_acknowledged=True,
        enabled=True,
    )

    with pytest.raises(
        LanDiscoveryConflict,
        match="lan_runtime_hardening_unavailable",
    ):
        service.review_lan_target(
            request,
            authenticated_owner_principal=OWNER,
        )

    assert registry.get_provider_profile(provider_id) == before_profile
    assert registry.get_model_target(target_id) == before_target


def test_import_crash_hook_rolls_back_profile_and_all_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation(models=("alpha", "beta"))
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-crash",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_ids = tuple(_target_id(provider_id, model) for model in observation.catalog)
    service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)

    def crash() -> None:
        raise RuntimeError("injected LAN commit crash")

    monkeypatch.setattr(ledger_registry, "_before_lan_commit", crash)
    with pytest.raises(RuntimeError, match="injected LAN commit crash"):
        service.import_observation(
            _import_request(
                observation,
                completed,
                profile_revision=0,
                target_revisions=tuple((target_id, 0) for target_id in target_ids),
            ),
            authenticated_owner_principal=OWNER,
        )

    registry = RoutingLedger(state)
    assert registry.list_provider_profiles() == []
    assert registry.list_model_targets() == []


def test_task5_loader_authenticates_legacy_v1_and_aggregate_v2_receipts(
    tmp_path: Path,
) -> None:
    legacy_state = AgentStateStore(tmp_path / "legacy" / "state.db")
    legacy_observation = _positive_observation()
    _legacy_row, legacy_scan = _persist_completed_scan(
        legacy_state,
        scan_id="scan-legacy-v1",
        observation=legacy_observation,
    )
    assert legacy_scan.terminal_receipt is not None
    assert "observations" in legacy_scan.terminal_receipt
    assert legacy_scan.terminal_receipt_digest is not None
    with legacy_state._connect() as connection:
        authenticated_legacy = load_authenticated_task4_observation(
            connection,
            scan_id=legacy_scan.scan_id,
            endpoint_binding_digest=legacy_observation.endpoint_binding_digest,
            expected_terminal_receipt_digest=legacy_scan.terminal_receipt_digest,
            expected_observation_digest=legacy_observation.observation_digest,
            authenticated_owner_principal=OWNER,
        )
    assert authenticated_legacy.observation == legacy_observation

    v2_state = AgentStateStore(tmp_path / "v2" / "state.db")
    scope = _scope()
    v2_observations = (
        _positive_observation(scope=scope, address="192.168.50.2", port=1234),
        _positive_observation(scope=scope, address="192.168.50.3", port=8000),
    )
    _v2_rows, v2_scan = _persist_completed_scan_v2(
        v2_state,
        scan_id="scan-aggregate-v2",
        observations=tuple(reversed(v2_observations)),
        scope=scope,
    )
    assert v2_scan.terminal_receipt is not None
    assert v2_scan.terminal_receipt["schema"] == "kestrel.lan.scan-receipt.v2"
    assert "observations" not in v2_scan.terminal_receipt
    assert v2_scan.terminal_receipt_digest is not None
    with v2_state._connect() as connection:
        authenticated_v2 = load_authenticated_task4_observation(
            connection,
            scan_id=v2_scan.scan_id,
            endpoint_binding_digest=v2_observations[0].endpoint_binding_digest,
            expected_terminal_receipt_digest=v2_scan.terminal_receipt_digest,
            expected_observation_digest=v2_observations[0].observation_digest,
            authenticated_owner_principal=OWNER,
        )
    assert authenticated_v2.observation == v2_observations[0]


@pytest.mark.parametrize(
    "tamper",
    (
        "membership_digest",
        "count",
        "error_counts",
        "noncanonical_order",
        "durable_row",
    ),
)
def test_task5_v2_loader_rejects_aggregate_or_member_tampering_without_import(
    tmp_path: Path,
    tamper: str,
) -> None:
    state = AgentStateStore(tmp_path / tamper / "state.db")
    scope = _scope()
    observations = (
        _positive_observation(scope=scope, address="192.168.50.2", port=1234),
        _positive_observation(scope=scope, address="192.168.50.3", port=8000),
    )
    _rows, completed = _persist_completed_scan_v2(
        state,
        scan_id=f"scan-v2-{tamper}",
        observations=observations,
        scope=scope,
    )
    assert completed.terminal_receipt is not None

    if tamper == "membership_digest":
        _rewrite_terminal_receipt(
            state,
            completed.scan_id,
            mutate=lambda receipt: receipt.__setitem__(
                "observation_membership_digest", "sha256:" + "9" * 64
            ),
            recompute_digest=True,
        )
    elif tamper == "count":
        _rewrite_terminal_receipt(
            state,
            completed.scan_id,
            mutate=lambda receipt: receipt.__setitem__("observation_count", 3),
            recompute_digest=True,
        )
    elif tamper == "error_counts":
        receipt = json.loads(json.dumps(completed.terminal_receipt))
        receipt["error_count"] = 1
        receipt["error_category_counts"] = {"tcp_refused": 1}
        receipt_json = json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        with state._connect() as connection:
            connection.execute("DROP TRIGGER trg_routing_lan_terminal_scan_update_immutable")
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET error_count = 1, terminal_receipt_json = ?,
                    terminal_receipt_digest = ?
                WHERE scan_id = ?
                """,
                (receipt_json, _digest(receipt), completed.scan_id),
            )
        LanDiscoveryLedger(state)
    elif tamper == "noncanonical_order":
        members = [
            {
                "endpoint_id": observation.endpoint_binding_digest,
                "observation_digest": observation.observation_digest,
            }
            for observation in observations
        ]
        canonical_members = sorted(members, key=lambda item: item["endpoint_id"])
        reversed_members = list(reversed(canonical_members))
        assert reversed_members != canonical_members
        bad_order_digest = _digest(
            {
                "schema": "kestrel.lan.observation-membership.v1",
                "count": 2,
                "members": reversed_members,
            }
        )
        _rewrite_terminal_receipt(
            state,
            completed.scan_id,
            mutate=lambda receipt: receipt.__setitem__(
                "observation_membership_digest", bad_order_digest
            ),
            recompute_digest=True,
        )
    else:
        with state._connect() as connection:
            connection.execute("DROP TRIGGER trg_routing_lan_terminal_observation_update_immutable")
            connection.execute(
                """
                UPDATE routing_lan_observations
                SET freshness_timestamp = '2026-08-01 12:00:00'
                WHERE scan_id = ? AND endpoint_id = ?
                """,
                (completed.scan_id, observations[0].endpoint_binding_digest),
            )
        LanDiscoveryLedger(state)

    current = LanDiscoveryLedger(state).get_scan(completed.scan_id)
    assert current is not None and current.terminal_receipt_digest is not None
    with state._connect() as connection, pytest.raises((KeyError, ValueError)):
        load_authenticated_task4_observation(
            connection,
            scan_id=current.scan_id,
            endpoint_binding_digest=observations[0].endpoint_binding_digest,
            expected_terminal_receipt_digest=current.terminal_receipt_digest,
            expected_observation_digest=observations[0].observation_digest,
            authenticated_owner_principal=OWNER,
        )

    provider_id = _provider_id(observations[0].endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    with pytest.raises(LanDiscoveryConflict):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            _import_request(
                observations[0],
                current,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


@pytest.mark.parametrize(
    "tamper",
    ["receipt_digest", "membership", "column_public_disagreement", "timestamp"],
)
def test_import_revalidates_terminal_receipt_membership_and_durable_columns(
    tmp_path: Path,
    tamper: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-tamper",
        observation=observation,
    )
    if tamper == "receipt_digest":
        _rewrite_terminal_receipt(
            state,
            completed.scan_id,
            mutate=lambda receipt: receipt.__setitem__("terminal_reason", "tampered"),
            recompute_digest=False,
        )
    elif tamper == "membership":
        _rewrite_terminal_receipt(
            state,
            completed.scan_id,
            mutate=lambda receipt: receipt.__setitem__("observations", []),
            recompute_digest=True,
        )
    elif tamper == "column_public_disagreement":
        _rewrite_observation_and_membership(
            state,
            completed.scan_id,
            observation.endpoint_binding_digest,
            public_payload_update={"api_shape": "ollama_compatible"},
        )
    else:
        _rewrite_observation_and_membership(
            state,
            completed.scan_id,
            observation.endpoint_binding_digest,
            freshness_timestamp="2026-08-01 12:00:00",
        )
    current = LanDiscoveryLedger(state).get_scan(completed.scan_id)
    assert current is not None
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)

    with pytest.raises(LanDiscoveryConflict):
        service.import_observation(
            _import_request(
                observation,
                current,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )

    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


def test_import_rejects_foreign_authenticated_owner_without_writes(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-owner",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)

    with pytest.raises(LanDiscoveryConflict, match="owner"):
        service.import_observation(
            _import_request(
                observation,
                completed,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal="owner:foreign",
        )

    assert RoutingLedger(state).list_provider_profiles() == []


def test_import_rejects_non_utc_aware_transaction_clock_without_writes(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-non-utc-clock",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    eastern = timezone(timedelta(hours=-4))
    service = LanDiscoveryService(
        RoutingLedger(state),
        clock=lambda: datetime(2026, 8, 1, 8, 0, tzinfo=eastern),
    )

    with pytest.raises(ValueError, match="aware UTC"):
        service.import_observation(
            _import_request(
                observation,
                completed,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


def test_import_rejects_whitespace_owner_column_before_receipt_or_inventory_write(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-whitespace-owner-column",
        observation=observation,
    )
    with state._connect() as connection:
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_scan_update_immutable")
        connection.execute(
            "UPDATE routing_lan_scans SET owner_principal = ? WHERE scan_id = ?",
            (f" {OWNER}", completed.scan_id),
        )
    current = LanDiscoveryLedger(state).get_scan(completed.scan_id)
    assert current is not None
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")

    with pytest.raises(LanDiscoveryConflict, match="owner|canonical"):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            _import_request(
                observation,
                current,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


def test_lan_service_security_text_fields_reject_string_subclasses_exactly(
    tmp_path: Path,
) -> None:
    class DerivedStr(str):
        pass

    digest = "sha256:" + "1" * 64
    with pytest.raises(ValueError, match="exact"):
        LanImportRequest(
            scan_id=DerivedStr("scan"),
            endpoint_binding_digest=digest,
            expected_terminal_receipt_digest=digest,
            expected_observation_digest=digest,
            expected_profile_revision=0,
            expected_target_revisions=(),
        )
    with pytest.raises(ValueError, match="exact"):
        LanImportRequest(
            scan_id="scan",
            endpoint_binding_digest=DerivedStr(digest),
            expected_terminal_receipt_digest=digest,
            expected_observation_digest=digest,
            expected_profile_revision=0,
            expected_target_revisions=(),
        )

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-owner-subclass",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    with pytest.raises(ValueError, match="canonical"):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            _import_request(
                observation,
                completed,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal=DerivedStr(OWNER),
        )
    assert RoutingLedger(state).list_provider_profiles() == []


@pytest.mark.parametrize("status", ["running", "cancelled", "failed"])
def test_noncompleted_scan_evidence_is_rejected_without_inventory_writes(
    tmp_path: Path,
    status: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    scan = _persist_scan_with_status(
        state,
        scan_id=f"scan-{status}",
        observation=observation,
        status=status,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    receipt_digest = scan.terminal_receipt_digest or ("sha256:" + "0" * 64)
    request = LanImportRequest(
        scan_id=scan.scan_id,
        endpoint_binding_digest=observation.endpoint_binding_digest,
        expected_terminal_receipt_digest=receipt_digest,
        expected_observation_digest=observation.observation_digest,
        expected_profile_revision=0,
        expected_target_revisions=(LanExpectedRevision(target_id, 0),),
    )

    with pytest.raises(LanDiscoveryConflict, match="completed|scan|evidence"):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            request,
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


def test_missing_scan_and_missing_observation_are_closed_no_write_errors(
    tmp_path: Path,
) -> None:
    digest = "sha256:" + "8" * 64
    missing_state = AgentStateStore(tmp_path / "missing-scan" / "agent.db")
    missing_scan_request = LanImportRequest(
        scan_id="missing-scan",
        endpoint_binding_digest=digest,
        expected_terminal_receipt_digest=digest,
        expected_observation_digest=digest,
        expected_profile_revision=0,
        expected_target_revisions=(),
    )
    with pytest.raises(KeyError, match="scan"):
        LanDiscoveryService(
            RoutingLedger(missing_state),
            clock=lambda: NOW,
        ).import_observation(
            missing_scan_request,
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(missing_state).list_provider_profiles() == []

    state = AgentStateStore(tmp_path / "missing-observation" / "agent.db")
    ledger = LanDiscoveryLedger(state)
    scope = _scope()
    draft = ledger.create_scan(
        scan_id="scan-without-observation",
        owner_principal=OWNER,
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits={
            "known_model_service_ports": [1234, 8000, 8080, 11434],
            "max_active_hosts": 256,
            "max_scan_concurrency": 16,
            "tcp_connect_timeout_seconds": 0.75,
            "http_probe_timeout_seconds": 2.0,
            "total_scan_deadline_seconds": 45.0,
            "max_probe_response_bytes": 262144,
            "max_discovered_models": 8,
            "mdns_window_seconds": 2.5,
        },
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )
    completed = ledger.transition_scan(
        draft.scan_id,
        "completed",
        expected_revision=running.revision,
        terminal_reason="completed",
        candidate_count=0,
        error_count=0,
        timeout_count=0,
    )
    assert completed.terminal_receipt_digest is not None
    missing_observation_request = LanImportRequest(
        scan_id=completed.scan_id,
        endpoint_binding_digest=digest,
        expected_terminal_receipt_digest=completed.terminal_receipt_digest,
        expected_observation_digest=digest,
        expected_profile_revision=0,
        expected_target_revisions=(),
    )
    with pytest.raises(KeyError, match="observation|endpoint"):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            missing_observation_request,
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []


def test_durable_foreign_owner_is_rejected_even_with_fixed_runtime_principal(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-foreign-durable-owner",
        observation=observation,
        owner_principal="owner:foreign",
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")

    with pytest.raises(LanDiscoveryConflict, match="owner"):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            _import_request(
                observation,
                completed,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []


@pytest.mark.parametrize("operation", ["replacement", "outage"])
def test_multirow_lan_mutations_reject_foreign_target_owner(
    tmp_path: Path,
    operation: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _initial,
        _scan,
        registry,
        _service,
        imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    tampered_metadata = json.loads(json.dumps(target.target.metadata))
    tampered_metadata["lan_discovery"]["owner_principal"] = "owner:foreign"
    with state._connect() as connection:
        connection.execute(
            "UPDATE routing_model_targets SET metadata_json = ? WHERE target_id = ?",
            (
                json.dumps(
                    tampered_metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                target_id,
            ),
        )

    if operation == "replacement":
        evidence = _positive_observation(address="192.168.50.3")
        _row, completed = _persist_completed_scan(
            state,
            scan_id="scan-foreign-owner-replacement",
            observation=evidence,
        )
        replacement_profile_id = _provider_id(evidence.endpoint_binding_digest)
        replacement_target_id = _target_id(replacement_profile_id, "alpha")
        request = replace(
            _import_request(
                evidence,
                completed,
                profile_revision=0,
                target_revisions=((target_id, 1), (replacement_target_id, 0)),
            ),
            replacement=LanReplacementConfirmation(
                provider_profile_id=provider_id,
                expected_profile_revision=profile.revision,
                expected_endpoint_fingerprint=imported.endpoint_fingerprint,
                expected_material_binding_digests=(
                    target.target.metadata["lan_discovery"]["material_binding_digest"],
                ),
            ),
        )
        clock = NOW
    else:
        evidence = _outage_observation()
        clock = NOW + timedelta(seconds=301)
        _row, completed = _persist_completed_scan(
            state,
            scan_id="scan-foreign-owner-outage",
            observation=evidence,
            observed_at=clock,
        )
        request = _import_request(
            evidence,
            completed,
            profile_revision=profile.revision,
            target_revisions=((target_id, target.revision),),
        )

    with pytest.raises(LanDiscoveryConflict, match="owner"):
        LanDiscoveryService(registry, clock=lambda: clock).import_observation(
            request,
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_provider_profile(provider_id) == profile
    persisted_target = registry.get_model_target(target_id)
    assert persisted_target is not None and persisted_target.revision == target.revision


@pytest.mark.parametrize("outage_kind", ["failed_generation", "empty_catalog"])
def test_completed_nonpositive_observation_is_outage_only_and_creates_no_draft(
    tmp_path: Path,
    outage_kind: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = (
        _failed_generation_observation()
        if outage_kind == "failed_generation"
        else _empty_catalog_observation()
    )
    _row, completed = _persist_completed_scan(
        state,
        scan_id=f"scan-{outage_kind}",
        observation=observation,
    )

    result = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert result.outage_observed is True
    assert result.profile is None
    assert result.targets == ()
    assert result.endpoint_fingerprint is None
    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


@pytest.mark.parametrize("bad_revision", [False, 0.0, "0"])
def test_direct_registry_outage_noop_rejects_inexact_revision_types(
    tmp_path: Path,
    bad_revision: object,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _outage_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-direct-outage-revision",
        observation=observation,
    )
    request = _import_request(
        observation,
        completed,
        profile_revision=0,
        target_revisions=(),
    )
    registry = RoutingLedger(state)
    kwargs = {
        "scan_id": request.scan_id,
        "endpoint_binding_digest": request.endpoint_binding_digest,
        "expected_terminal_receipt_digest": request.expected_terminal_receipt_digest,
        "expected_observation_digest": request.expected_observation_digest,
        "replacement": None,
        "authenticated_owner_principal": OWNER,
        "now": NOW,
    }

    with pytest.raises(ValueError, match="exact non-negative integer"):
        registry.apply_lan_import(
            **kwargs,
            expected_profile_revision=bad_revision,
            expected_target_revisions=(),
        )
    with pytest.raises(ValueError, match="exact non-negative integer"):
        registry.apply_lan_import(
            **kwargs,
            expected_profile_revision=0,
            expected_target_revisions=(("lan-target-" + "1" * 64, bad_revision),),
        )


def test_uncorrelated_outage_noop_enforces_profile_revision_zero(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _outage_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-direct-outage-cas",
        observation=observation,
    )
    request = _import_request(
        observation,
        completed,
        profile_revision=0,
        target_revisions=(),
    )

    with pytest.raises(RoutingRevisionConflict):
        RoutingLedger(state).apply_lan_import(
            scan_id=request.scan_id,
            endpoint_binding_digest=request.endpoint_binding_digest,
            expected_terminal_receipt_digest=request.expected_terminal_receipt_digest,
            expected_observation_digest=request.expected_observation_digest,
            expected_profile_revision=1,
            expected_target_revisions=(),
            replacement=None,
            authenticated_owner_principal=OWNER,
            now=NOW,
        )


@pytest.mark.parametrize("revision_shape", ["duplicate", "omitted", "extra"])
def test_import_requires_exact_complete_unique_affected_target_revision_set(
    tmp_path: Path,
    revision_shape: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation(models=("alpha", "beta"))
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-revision-set",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    alpha_id = _target_id(provider_id, "alpha")
    beta_id = _target_id(provider_id, "beta")
    revisions = {
        "duplicate": ((alpha_id, 0), (alpha_id, 0), (beta_id, 0)),
        "omitted": ((alpha_id, 0),),
        "extra": ((alpha_id, 0), (beta_id, 0), ("lan-target-" + "9" * 64, 0)),
    }[revision_shape]
    service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)

    with pytest.raises((LanDiscoveryConflict, ValueError), match="revision|target"):
        service.import_observation(
            _import_request(
                observation,
                completed,
                profile_revision=0,
                target_revisions=revisions,
            ),
            authenticated_owner_principal=OWNER,
        )

    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


@pytest.mark.parametrize(
    ("revision_field", "revision"),
    [
        (field, revision)
        for field in ("profile", "nested_target")
        for revision in (False, True, -1, 1.0, "0")
    ],
)
def test_import_service_revalidates_forged_revision_objects_before_mutation(
    tmp_path: Path,
    revision_field: str,
    revision: object,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-forged-import-revision",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    request = _import_request(
        observation,
        completed,
        profile_revision=0,
        target_revisions=((target_id, 0),),
    )
    if revision_field == "profile":
        request = _unchecked_clone(request, expected_profile_revision=revision)
    else:
        nested = object.__new__(LanExpectedRevision)
        object.__setattr__(nested, "resource_id", target_id)
        object.__setattr__(nested, "revision", revision)
        request = _unchecked_clone(request, expected_target_revisions=(nested,))

    with pytest.raises((LanDiscoveryConflict, ValueError), match="revision"):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            request,
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


def test_concurrent_first_import_has_exactly_one_cas_winner(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-race",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    request = _import_request(
        observation,
        completed,
        profile_revision=0,
        target_revisions=((target_id, 0),),
    )

    def attempt() -> str:
        service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)
        try:
            service.import_observation(
                request,
                authenticated_owner_principal=OWNER,
            )
        except LanDiscoveryConflict:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _index: attempt(), range(2)))

    assert outcomes == ["committed", "conflict"]
    profile = RoutingLedger(state).get_provider_profile(provider_id)
    target = RoutingLedger(state).get_model_target(target_id)
    assert profile is not None and profile.revision == 1
    assert target is not None and target.revision == 1


@pytest.mark.parametrize("first_operation", ["import", "review"])
def test_import_profile_advance_vs_review_has_one_deterministic_cas_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_operation: str,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        _scan,
        registry,
        _service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    review_request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    _row, refresh_scan = _persist_completed_scan(
        state,
        scan_id="scan-race-refresh",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    import_request = _import_request(
        observation,
        refresh_scan,
        profile_revision=profile.revision,
        target_revisions=((target_id, target.revision),),
    )
    entered_commit_hook = Event()
    release_commit = Event()

    def ordered_hook() -> None:
        if not entered_commit_hook.is_set():
            entered_commit_hook.set()
            assert release_commit.wait(timeout=5)

    monkeypatch.setattr(ledger_registry, "_before_lan_commit", ordered_hook)

    def run(operation: str) -> str:
        service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)
        try:
            if operation == "import":
                service.import_observation(
                    import_request,
                    authenticated_owner_principal=OWNER,
                )
            else:
                service.review_lan_target(
                    review_request,
                    authenticated_owner_principal=OWNER,
                )
        except LanDiscoveryConflict:
            return f"{operation}:conflict"
        return f"{operation}:committed"

    second_operation = "review" if first_operation == "import" else "import"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, first_operation)
        assert entered_commit_hook.wait(timeout=5)
        second = pool.submit(run, second_operation)
        release_commit.set()
        outcomes = {first.result(timeout=5), second.result(timeout=5)}

    assert outcomes == {
        f"{first_operation}:committed",
        f"{second_operation}:conflict",
    }
    final_profile = registry.get_provider_profile(provider_id)
    final_target = registry.get_model_target(target_id)
    assert final_profile is not None and final_profile.revision == 2
    assert final_target is not None and final_target.revision == 2


def test_injected_review_commit_failure_rolls_back_profile_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    monkeypatch.setattr(
        ledger_registry,
        "_before_lan_commit",
        lambda: (_ for _ in ()).throw(RuntimeError("review crash")),
    )

    with pytest.raises(RuntimeError, match="review crash"):
        service.review_lan_target(
            request,
            authenticated_owner_principal=OWNER,
        )

    assert registry.get_provider_profile(provider_id) == profile
    assert registry.get_model_target(target_id) == target


def test_changed_address_is_independent_without_exact_replacement_confirmation(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        first_observation,
        _first_scan,
        registry,
        _service,
        _first_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state)
    old_profile = registry.get_provider_profile(old_provider_id)
    old_target = registry.get_model_target(old_target_id)
    assert old_profile is not None and old_target is not None
    changed = _positive_observation(address="192.168.50.3")
    _row, changed_scan = _persist_completed_scan(
        state,
        scan_id="scan-new-address",
        observation=changed,
    )
    new_provider_id = _provider_id(changed.endpoint_binding_digest)
    new_target_id = _target_id(new_provider_id, "alpha")
    service = LanDiscoveryService(registry, clock=lambda: NOW)

    result = service.import_observation(
        _import_request(
            changed,
            changed_scan,
            profile_revision=0,
            target_revisions=((new_target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert first_observation.endpoint_binding_digest != changed.endpoint_binding_digest
    assert result.profile is not None
    assert result.profile.profile.profile_id == new_provider_id
    assert registry.get_provider_profile(old_provider_id) == old_profile
    assert registry.get_model_target(old_target_id) == old_target


@pytest.mark.parametrize("crash", [False, True])
def test_exact_replacement_confirmation_stales_old_binding_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash: bool,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _old_observation,
        _old_scan,
        registry,
        _service,
        old_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state)
    old_profile = registry.get_provider_profile(old_provider_id)
    old_target = registry.get_model_target(old_target_id)
    assert old_profile is not None and old_target is not None
    old_protected = old_target.target.metadata["lan_discovery"]
    changed = _positive_observation(address="192.168.50.3")
    _row, changed_scan = _persist_completed_scan(
        state,
        scan_id="scan-replacement",
        observation=changed,
    )
    new_provider_id = _provider_id(changed.endpoint_binding_digest)
    new_target_id = _target_id(new_provider_id, "alpha")
    request = replace(
        _import_request(
            changed,
            changed_scan,
            profile_revision=0,
            target_revisions=((old_target_id, old_target.revision), (new_target_id, 0)),
        ),
        replacement=LanReplacementConfirmation(
            provider_profile_id=old_provider_id,
            expected_profile_revision=old_profile.revision,
            expected_endpoint_fingerprint=old_result.endpoint_fingerprint,
            expected_material_binding_digests=(old_protected["material_binding_digest"],),
        ),
    )
    if crash:
        monkeypatch.setattr(
            ledger_registry,
            "_before_lan_commit",
            lambda: (_ for _ in ()).throw(RuntimeError("replacement crash")),
        )
        with pytest.raises(RuntimeError, match="replacement crash"):
            LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
                request,
                authenticated_owner_principal=OWNER,
            )
        assert registry.get_provider_profile(old_provider_id) == old_profile
        assert registry.get_model_target(old_target_id) == old_target
        assert registry.get_provider_profile(new_provider_id) is None
        assert registry.get_model_target(new_target_id) is None
        return

    result = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        request,
        authenticated_owner_principal=OWNER,
    )
    assert old_protected["material_binding_digest"] in result.invalidated_binding_digests
    stale_old = registry.get_model_target(old_target_id)
    assert stale_old is not None
    assert stale_old.target.metadata["lan_discovery"]["stale_reasons"] == ["address_changed"]
    assert stale_old.target.enabled is False
    assert registry.get_model_target(new_target_id) is not None


def test_reappearing_replaced_binding_preserves_stale_state_until_review(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        old_observation,
        _old_scan,
        registry,
        _service,
        old_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state)
    old_profile = registry.get_provider_profile(old_provider_id)
    old_target = registry.get_model_target(old_target_id)
    assert old_profile is not None and old_target is not None
    changed = _positive_observation(address="192.168.50.3")
    _row, changed_scan = _persist_completed_scan(
        state,
        scan_id="scan-replacement-before-reappearance",
        observation=changed,
    )
    new_provider_id = _provider_id(changed.endpoint_binding_digest)
    new_target_id = _target_id(new_provider_id, "alpha")
    replacement = replace(
        _import_request(
            changed,
            changed_scan,
            profile_revision=0,
            target_revisions=((old_target_id, 1), (new_target_id, 0)),
        ),
        replacement=LanReplacementConfirmation(
            provider_profile_id=old_provider_id,
            expected_profile_revision=old_profile.revision,
            expected_endpoint_fingerprint=old_result.endpoint_fingerprint,
            expected_material_binding_digests=(
                old_target.target.metadata["lan_discovery"]["material_binding_digest"],
            ),
        ),
    )
    LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        replacement,
        authenticated_owner_principal=OWNER,
    )
    stale_profile = registry.get_provider_profile(old_provider_id)
    stale_target = registry.get_model_target(old_target_id)
    assert stale_profile is not None and stale_target is not None
    prior_transition = stale_target.target.metadata["lan_discovery"][
        "stale_transition_terminal_receipt_digest"
    ]
    _row, reappearance_scan = _persist_completed_scan(
        state,
        scan_id="scan-exact-reappearance",
        observation=old_observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    reappeared = LanDiscoveryService(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).import_observation(
        _import_request(
            old_observation,
            reappearance_scan,
            profile_revision=stale_profile.revision,
            target_revisions=((old_target_id, stale_target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert reappeared.profile is not None
    protected = reappeared.targets[0].target.metadata["lan_discovery"]
    assert protected["stale_reasons"] == ["address_changed"]
    assert reappeared.profile.profile.metadata["lan_discovery"]["stale_reasons"] == [
        "address_changed"
    ]
    assert protected["stale_transition_terminal_receipt_digest"] == (
        reappearance_scan.terminal_receipt_digest
    )
    assert protected["stale_transition_terminal_receipt_digest"] != prior_transition
    assert reappeared.targets[0].target.trust_class == "unconfirmed"
    assert reappeared.targets[0].target.enabled is False


@pytest.mark.parametrize(
    "bad_confirmation",
    [
        "omitted",
        "profile_revision",
        "fingerprint",
        "material_omitted",
        "material_extra",
        "material_duplicate",
        "material_unhashable",
    ],
)
def test_attempted_replacement_requires_exact_confirmation_without_partial_writes(
    tmp_path: Path,
    bad_confirmation: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _old_observation,
        _old_scan,
        registry,
        _service,
        old_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state)
    old_profile = registry.get_provider_profile(old_provider_id)
    old_target = registry.get_model_target(old_target_id)
    assert old_profile is not None and old_target is not None
    old_material = old_target.target.metadata["lan_discovery"]["material_binding_digest"]
    changed = _positive_observation(address="192.168.50.3")
    _row, changed_scan = _persist_completed_scan(
        state,
        scan_id="scan-bad-replacement",
        observation=changed,
    )
    new_provider_id = _provider_id(changed.endpoint_binding_digest)
    new_target_id = _target_id(new_provider_id, "alpha")
    request = _import_request(
        changed,
        changed_scan,
        profile_revision=0,
        target_revisions=((old_target_id, old_target.revision), (new_target_id, 0)),
    )
    confirmation = LanReplacementConfirmation(
        provider_profile_id=old_provider_id,
        expected_profile_revision=old_profile.revision,
        expected_endpoint_fingerprint=old_result.endpoint_fingerprint,
        expected_material_binding_digests=(old_material,),
    )
    if bad_confirmation == "omitted":
        replacement = None
    elif bad_confirmation == "profile_revision":
        replacement = _unchecked_clone(
            confirmation,
            expected_profile_revision=old_profile.revision + 1,
        )
    elif bad_confirmation == "fingerprint":
        replacement = _unchecked_clone(
            confirmation,
            expected_endpoint_fingerprint="sha256:" + "0" * 64,
        )
    elif bad_confirmation == "material_omitted":
        replacement = _unchecked_clone(
            confirmation,
            expected_material_binding_digests=(),
        )
    elif bad_confirmation == "material_extra":
        replacement = _unchecked_clone(
            confirmation,
            expected_material_binding_digests=(
                old_material,
                "sha256:" + "9" * 64,
            ),
        )
    elif bad_confirmation == "material_duplicate":
        replacement = _unchecked_clone(
            confirmation,
            expected_material_binding_digests=(old_material, old_material),
        )
    else:
        replacement = _unchecked_clone(
            confirmation,
            expected_material_binding_digests=([old_material],),
        )
    request = _unchecked_clone(request, replacement=replacement)

    with pytest.raises((LanDiscoveryConflict, ValueError)):
        LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
            request,
            authenticated_owner_principal=OWNER,
        )

    assert registry.get_provider_profile(old_provider_id) == old_profile
    assert registry.get_model_target(old_target_id) == old_target
    assert registry.get_provider_profile(new_provider_id) is None
    assert registry.get_model_target(new_target_id) is None


@pytest.mark.parametrize("revision", [False, True, -1, 1.0, "1"])
def test_import_service_revalidates_forged_replacement_revision(
    tmp_path: Path,
    revision: object,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _old_observation,
        _old_scan,
        registry,
        _service,
        old_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state)
    old_profile = registry.get_provider_profile(old_provider_id)
    old_target = registry.get_model_target(old_target_id)
    assert old_profile is not None and old_target is not None
    changed = _positive_observation(address="192.168.50.3")
    _row, scan = _persist_completed_scan(
        state,
        scan_id="scan-forged-replacement-revision",
        observation=changed,
    )
    new_provider_id = _provider_id(changed.endpoint_binding_digest)
    new_target_id = _target_id(new_provider_id, "alpha")
    valid_confirmation = LanReplacementConfirmation(
        provider_profile_id=old_provider_id,
        expected_profile_revision=old_profile.revision,
        expected_endpoint_fingerprint=old_result.endpoint_fingerprint,
        expected_material_binding_digests=(
            old_target.target.metadata["lan_discovery"]["material_binding_digest"],
        ),
    )
    forged_confirmation = _unchecked_clone(
        valid_confirmation,
        expected_profile_revision=revision,
    )
    request = _unchecked_clone(
        _import_request(
            changed,
            scan,
            profile_revision=0,
            target_revisions=((old_target_id, old_target.revision), (new_target_id, 0)),
        ),
        replacement=forged_confirmation,
    )

    with pytest.raises((LanDiscoveryConflict, ValueError), match="revision"):
        LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
            request,
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_provider_profile(old_provider_id) == old_profile
    assert registry.get_model_target(old_target_id) == old_target
    assert registry.get_provider_profile(new_provider_id) is None
    assert registry.get_model_target(new_target_id) is None


def test_direct_registry_replacement_rejects_unhashable_material_before_writes(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _old_observation,
        _old_scan,
        registry,
        _service,
        old_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state)
    old_profile = registry.get_provider_profile(old_provider_id)
    old_target = registry.get_model_target(old_target_id)
    assert old_profile is not None and old_target is not None
    changed = _positive_observation(address="192.168.50.3")
    _row, changed_scan = _persist_completed_scan(
        state,
        scan_id="scan-direct-unhashable-replacement",
        observation=changed,
    )
    new_provider_id = _provider_id(changed.endpoint_binding_digest)
    new_target_id = _target_id(new_provider_id, "alpha")
    request = _import_request(
        changed,
        changed_scan,
        profile_revision=0,
        target_revisions=((old_target_id, old_target.revision), (new_target_id, 0)),
    )
    old_material = old_target.target.metadata["lan_discovery"]["material_binding_digest"]

    with pytest.raises(ValueError, match="digest|material"):
        registry.apply_lan_import(
            scan_id=request.scan_id,
            endpoint_binding_digest=request.endpoint_binding_digest,
            expected_terminal_receipt_digest=(request.expected_terminal_receipt_digest),
            expected_observation_digest=request.expected_observation_digest,
            expected_profile_revision=request.expected_profile_revision,
            expected_target_revisions=tuple(
                (item.resource_id, item.revision) for item in request.expected_target_revisions
            ),
            replacement=(
                old_provider_id,
                old_profile.revision,
                old_result.endpoint_fingerprint,
                ([old_material],),
            ),  # type: ignore[arg-type]
            authenticated_owner_principal=OWNER,
            now=NOW,
        )
    assert registry.get_provider_profile(old_provider_id) == old_profile
    assert registry.get_model_target(old_target_id) == old_target
    assert registry.get_provider_profile(new_provider_id) is None
    assert registry.get_model_target(new_target_id) is None


@pytest.mark.parametrize("stale_source", ["replacement", "outage_expiry"])
def test_nonrecoverable_stale_binding_requires_target_specific_positive_refresh(
    tmp_path: Path,
    stale_source: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    clock_now = [NOW]
    (
        original,
        _scan,
        registry,
        service,
        old_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state, clock=lambda: clock_now[0])
    if stale_source == "replacement":
        old_profile = registry.get_provider_profile(old_provider_id)
        old_target = registry.get_model_target(old_target_id)
        assert old_profile is not None and old_target is not None
        changed = _positive_observation(address="192.168.50.3")
        _row, changed_scan = _persist_completed_scan(
            state,
            scan_id="scan-nonrecoverable-replacement",
            observation=changed,
        )
        new_provider_id = _provider_id(changed.endpoint_binding_digest)
        new_target_id = _target_id(new_provider_id, "alpha")
        service.import_observation(
            replace(
                _import_request(
                    changed,
                    changed_scan,
                    profile_revision=0,
                    target_revisions=(
                        (old_target_id, old_target.revision),
                        (new_target_id, 0),
                    ),
                ),
                replacement=LanReplacementConfirmation(
                    provider_profile_id=old_provider_id,
                    expected_profile_revision=old_profile.revision,
                    expected_endpoint_fingerprint=old_result.endpoint_fingerprint,
                    expected_material_binding_digests=(
                        old_target.target.metadata["lan_discovery"]["material_binding_digest"],
                    ),
                ),
            ),
            authenticated_owner_principal=OWNER,
        )
    else:
        clock_now[0] = NOW + timedelta(seconds=301)
        outage = _outage_observation()
        _row, outage_scan = _persist_completed_scan(
            state,
            scan_id="scan-nonrecoverable-expiry",
            observation=outage,
            observed_at=clock_now[0],
        )
        profile = registry.get_provider_profile(old_provider_id)
        target = registry.get_model_target(old_target_id)
        assert profile is not None and target is not None
        service.import_observation(
            _import_request(
                outage,
                outage_scan,
                profile_revision=profile.revision,
                target_revisions=((old_target_id, target.revision),),
            ),
            authenticated_owner_principal=OWNER,
        )
    stale_profile = registry.get_provider_profile(old_provider_id)
    stale_target = registry.get_model_target(old_target_id)
    assert stale_profile is not None and stale_target is not None
    stale_review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=stale_profile.revision,
        target_revision=stale_target.revision,
        target_id=old_target_id,
        protected=stale_target.target.metadata["lan_discovery"],
    )
    with pytest.raises(LanDiscoveryConflict, match="fresh|evidence|stale|receipt"):
        service.review_lan_target(
            stale_review,
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_model_target(old_target_id) == stale_target

    _row, refresh_scan = _persist_completed_scan(
        state,
        scan_id=f"scan-refresh-{stale_source}",
        observation=original,
        observed_at=clock_now[0],
    )
    refreshed = service.import_observation(
        _import_request(
            original,
            refresh_scan,
            profile_revision=stale_profile.revision,
            target_revisions=((old_target_id, stale_target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert refreshed.profile is not None
    refreshed_target = registry.get_model_target(old_target_id)
    assert refreshed_target is not None
    recoverable_review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=refreshed.profile.revision,
        target_revision=refreshed_target.revision,
        target_id=old_target_id,
        protected=refreshed_target.target.metadata["lan_discovery"],
    )
    recovered = service.review_lan_target(
        recoverable_review,
        authenticated_owner_principal=OWNER,
    )
    assert recovered.target.target.metadata["lan_discovery"]["stale_reasons"] == []
    assert recovered.target.target.metadata["lan_discovery"]["reviewed"] is True


@pytest.mark.parametrize(
    ("endpoint_change", "expected_reason"),
    [
        ("interface", "interface_changed"),
        ("address", "address_changed"),
        ("port", "port_changed"),
    ],
)
def test_replacement_records_exact_endpoint_identity_drift_reason(
    tmp_path: Path,
    endpoint_change: str,
    expected_reason: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _old_observation,
        _old_scan,
        registry,
        _service,
        old_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state)
    old_profile = registry.get_provider_profile(old_provider_id)
    old_target = registry.get_model_target(old_target_id)
    assert old_profile is not None and old_target is not None
    if endpoint_change == "interface":
        interface = NetworkInterface.from_addresses(
            os_identity="test:en1",
            display_name="replacement interface",
            addresses=("192.168.50.1/29",),
        )
        changed_scope = PrivateScanScope.from_request(interface, "192.168.50.0/29")
        changed = _positive_observation(scope=changed_scope)
    elif endpoint_change == "address":
        changed_scope = _scope()
        changed = _positive_observation(scope=changed_scope, address="192.168.50.3")
    else:
        changed_scope = _scope()
        changed = _positive_observation(scope=changed_scope, port=8000)
    _row, changed_scan = _persist_completed_scan(
        state,
        scan_id=f"scan-replacement-{endpoint_change}",
        scope=changed_scope,
        observation=changed,
    )
    new_provider_id = _provider_id(changed.endpoint_binding_digest)
    new_target_id = _target_id(new_provider_id, "alpha")
    request = replace(
        _import_request(
            changed,
            changed_scan,
            profile_revision=0,
            target_revisions=((old_target_id, old_target.revision), (new_target_id, 0)),
        ),
        replacement=LanReplacementConfirmation(
            provider_profile_id=old_provider_id,
            expected_profile_revision=old_profile.revision,
            expected_endpoint_fingerprint=old_result.endpoint_fingerprint,
            expected_material_binding_digests=(
                old_target.target.metadata["lan_discovery"]["material_binding_digest"],
            ),
        ),
    )

    result = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        request,
        authenticated_owner_principal=OWNER,
    )

    assert dict(result.stale_reasons_by_target)[old_target_id] == (expected_reason,)
    stale_old = registry.get_model_target(old_target_id)
    assert stale_old is not None
    assert stale_old.target.metadata["lan_discovery"]["stale_reasons"] == [expected_reason]


def test_endpoint_wide_and_target_only_stale_reasons_have_distinct_scope(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    initial_scope = _scope()
    (
        _initial,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        target_ids,
    ) = _import_first_positive(
        state,
        scope=initial_scope,
        models=("alpha", "beta"),
    )
    narrowed_scope = _scope(network="192.168.50.0/30")
    narrowed = _positive_observation(
        scope=narrowed_scope,
        models=("alpha", "beta"),
    )
    _row, narrowed_scan = _persist_completed_scan(
        state,
        scan_id="scan-narrowed",
        scope=narrowed_scope,
        observation=narrowed,
    )
    endpoint_wide = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        _import_request(
            narrowed,
            narrowed_scan,
            profile_revision=1,
            target_revisions=tuple((target_id, 1) for target_id in target_ids),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert endpoint_wide.profile is not None
    assert endpoint_wide.profile.profile.metadata["lan_discovery"]["stale_reasons"] == [
        "network_changed"
    ]
    assert all(
        "network_changed" in reasons
        for _target_id_value, reasons in endpoint_wide.stale_reasons_by_target
    )

    target_only_state = AgentStateStore(tmp_path / "target-only" / "agent.db")
    (
        initial,
        _scan,
        target_registry,
        _service,
        _result,
        target_provider_id,
        (alpha_id, beta_id),
    ) = _import_first_positive(
        target_only_state,
        models=("alpha", "beta"),
    )
    missing_beta = _positive_observation(models=("alpha",))
    _row, missing_scan = _persist_completed_scan(
        target_only_state,
        scan_id="scan-missing-beta",
        observation=missing_beta,
    )
    target_only = LanDiscoveryService(
        target_registry,
        clock=lambda: NOW,
    ).import_observation(
        _import_request(
            missing_beta,
            missing_scan,
            profile_revision=1,
            target_revisions=((alpha_id, 1), (beta_id, 1)),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert initial.endpoint_binding_digest == missing_beta.endpoint_binding_digest
    assert target_only.profile is not None
    assert target_only.profile.profile.profile_id == target_provider_id
    assert target_only.profile.profile.metadata["lan_discovery"]["stale_reasons"] == []
    assert "model_missing" in dict(target_only.stale_reasons_by_target)[beta_id]
    assert provider_id != ""  # keep the endpoint-wide identity in the asserted fixture


@pytest.mark.parametrize(
    "outage_factory",
    [_outage_observation, _failed_generation_observation, _empty_catalog_observation],
)
def test_existing_binding_outage_is_noop_until_prior_evidence_expires(
    tmp_path: Path,
    outage_factory,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    clock_now = [NOW]
    (
        positive,
        _positive_scan,
        registry,
        service,
        imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state, clock=lambda: clock_now[0])
    profile_before = registry.get_provider_profile(provider_id)
    target_before = registry.get_model_target(target_id)
    assert profile_before is not None and target_before is not None

    outage = outage_factory()
    _row, outage_scan = _persist_completed_scan(
        state,
        scan_id="scan-outage-still-fresh",
        observation=outage,
        observed_at=NOW + timedelta(seconds=1),
    )
    still_fresh = service.import_observation(
        _import_request(
            outage,
            outage_scan,
            profile_revision=profile_before.revision,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert still_fresh.outage_observed is True
    assert still_fresh.endpoint_fingerprint == imported.endpoint_fingerprint
    assert registry.get_provider_profile(provider_id) == profile_before
    assert registry.get_model_target(target_id) == target_before

    clock_now[0] = NOW + timedelta(seconds=301)
    expired_outage = outage_factory()
    _row, expired_scan = _persist_completed_scan(
        state,
        scan_id="scan-outage-expired",
        observation=expired_outage,
        observed_at=clock_now[0],
    )
    expired = service.import_observation(
        _import_request(
            expired_outage,
            expired_scan,
            profile_revision=profile_before.revision,
            target_revisions=((target_id, target_before.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert expired.outage_observed is True
    assert "freshness_expired" in dict(expired.stale_reasons_by_target)[target_id]
    target_after = registry.get_model_target(target_id)
    assert target_after is not None
    assert target_after.target.health == "unavailable"
    assert target_after.target.enabled is False
    assert (
        target_after.target.metadata["lan_discovery"]["observation_digest"]
        == positive.observation_digest
    )


def test_outage_expires_only_individually_expired_target_and_preserves_fresh_sibling(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    clock_now = [NOW]
    (
        _initial,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (alpha_id,),
    ) = _import_first_positive(state, clock=lambda: clock_now[0])
    clock_now[0] = NOW + timedelta(seconds=250)
    incomplete = _positive_observation(
        models=("alpha", "beta"),
        catalog_complete=False,
    )
    beta_id = _target_id(provider_id, "beta")
    _row, incomplete_scan = _persist_completed_scan(
        state,
        scan_id="scan-split-target-freshness",
        observation=incomplete,
        observed_at=clock_now[0],
    )
    refreshed_profile = service.import_observation(
        _import_request(
            incomplete,
            incomplete_scan,
            profile_revision=1,
            target_revisions=((beta_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert refreshed_profile.profile is not None
    alpha_before = registry.get_model_target(alpha_id)
    beta_before = registry.get_model_target(beta_id)
    assert alpha_before is not None and beta_before is not None

    clock_now[0] = NOW + timedelta(seconds=301)
    outage = _outage_observation()
    _row, outage_scan = _persist_completed_scan(
        state,
        scan_id="scan-only-alpha-expired",
        observation=outage,
        observed_at=clock_now[0],
    )
    first_outage = service.import_observation(
        _import_request(
            outage,
            outage_scan,
            profile_revision=refreshed_profile.profile.revision,
            target_revisions=((alpha_id, alpha_before.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert first_outage.affected_target_ids == (alpha_id,)
    assert dict(first_outage.stale_reasons_by_target) == {
        alpha_id: ("freshness_expired",),
    }
    assert first_outage.profile is not None
    assert first_outage.profile.revision == refreshed_profile.profile.revision
    assert registry.get_model_target(beta_id) == beta_before
    alpha_expired = registry.get_model_target(alpha_id)
    assert alpha_expired is not None
    assert alpha_expired.target.health == "unavailable"


def test_profile_only_outage_expiry_invokes_transaction_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as registry_module

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    clock_now = [NOW]
    (
        _initial,
        _scan,
        registry,
        service,
        _imported,
        _provider_id_value,
        (target_id,),
    ) = _import_first_positive(state, clock=lambda: clock_now[0])
    clock_now[0] = NOW + timedelta(seconds=250)
    incomplete = _positive_observation(models=("alpha",), catalog_complete=False)
    _row, refresh_scan = _persist_completed_scan(
        state,
        scan_id="scan-profile-only-refresh",
        observation=incomplete,
        observed_at=clock_now[0],
    )
    profile_refreshed = service.import_observation(
        _import_request(
            incomplete,
            refresh_scan,
            profile_revision=1,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert profile_refreshed.profile is not None
    target = registry.get_model_target(target_id)
    assert target is not None

    clock_now[0] = NOW + timedelta(seconds=301)
    first_outage_observation = _outage_observation()
    _row, first_outage_scan = _persist_completed_scan(
        state,
        scan_id="scan-target-only-expiry",
        observation=first_outage_observation,
        observed_at=clock_now[0],
    )
    first_outage = service.import_observation(
        _import_request(
            first_outage_observation,
            first_outage_scan,
            profile_revision=profile_refreshed.profile.revision,
            target_revisions=((target_id, target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert first_outage.profile is not None
    assert first_outage.profile.revision == profile_refreshed.profile.revision
    assert first_outage.affected_target_ids == (target_id,)

    hook_calls: list[str] = []
    monkeypatch.setattr(
        registry_module,
        "_before_lan_commit",
        lambda: hook_calls.append("called"),
    )
    clock_now[0] = NOW + timedelta(seconds=551)
    second_outage_observation = _outage_observation()
    _row, second_outage_scan = _persist_completed_scan(
        state,
        scan_id="scan-profile-expiry-only",
        observation=second_outage_observation,
        observed_at=clock_now[0],
    )
    profile_only = service.import_observation(
        _import_request(
            second_outage_observation,
            second_outage_scan,
            profile_revision=first_outage.profile.revision,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert hook_calls == ["called"]
    assert profile_only.profile is not None
    assert profile_only.profile.revision == first_outage.profile.revision + 1
    assert profile_only.affected_target_ids == ()
    assert profile_only.targets == ()


def test_stale_reason_order_is_closed_and_deterministic(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _initial,
        _scan,
        registry,
        _service,
        _result,
        _provider_id_value,
        (target_id,),
    ) = _import_first_positive(state)
    changed_scope = _scope(network="192.168.50.0/30")
    changed = _ollama_observation(scope=changed_scope)
    _row, changed_scan = _persist_completed_scan(
        state,
        scan_id="scan-multi-drift",
        scope=changed_scope,
        observation=changed,
    )

    result = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        _import_request(
            changed,
            changed_scan,
            profile_revision=1,
            target_revisions=((target_id, 1),),
        ),
        authenticated_owner_principal=OWNER,
    )

    reasons = dict(result.stale_reasons_by_target)[target_id]
    expected_order = (
        "network_changed",
        "api_shape_changed",
        "catalog_changed",
        "capability_changed",
    )
    assert reasons == expected_order


def test_endpoint_drift_resets_review_authority_and_all_preimage_fields(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        intended_roles=("reviewer", "worker"),
        task_family_affinities=("code-repair", "security-review"),
    )
    reviewed = service.review_lan_target(
        request,
        authenticated_owner_principal=OWNER,
    )
    reviewed_material = reviewed.target.target.metadata["lan_discovery"]["material_binding_digest"]
    narrowed_scope = _scope(network="192.168.50.0/30")
    drift = _positive_observation(scope=narrowed_scope)
    _row, drift_scan = _persist_completed_scan(
        state,
        scan_id="scan-reset-review",
        scope=narrowed_scope,
        observation=drift,
    )
    service.import_observation(
        _import_request(
            drift,
            drift_scan,
            profile_revision=reviewed.profile.revision,
            target_revisions=((target_id, reviewed.target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )

    stale = registry.get_model_target(target_id)
    assert stale is not None
    protected = stale.target.metadata["lan_discovery"]
    assert stale.target.enabled is False
    assert stale.target.trust_class == "unconfirmed"
    assert stale.target.health == "unavailable"
    assert stale.target.role_affinities == ()
    assert stale.target.task_family_affinities == ()
    assert protected["reviewed"] is False
    assert protected["review_digest"] is None
    assert protected["privacy_acknowledgement_digest"] is None
    assert protected["intended_roles"] == []
    assert protected["task_family_affinities"] == []
    assert protected["material_binding_digest"] != reviewed_material
    for field in (
        "reviewed_profile_revision",
        "reviewed_target_revision",
        "review_evidence_terminal_receipt_digest",
        "review_evidence_observation_digest",
        "reviewed_from_material_binding_digest",
        "reviewed_material_binding_digest",
        "review_acknowledged_stale_reasons",
        "review_acknowledged_stale_transition_terminal_receipt_digest",
    ):
        assert protected[field] is None


def test_api_shape_drift_has_exact_closed_ordered_reasons(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        _service,
        _result,
        _provider_id_value,
        (target_id,),
    ) = _import_first_positive(state)
    changed = _ollama_observation()
    _row, scan = _persist_completed_scan(
        state,
        scan_id="scan-api-shape-drift",
        observation=changed,
    )

    result = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        _import_request(
            changed,
            scan,
            profile_revision=1,
            target_revisions=((target_id, 1),),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert dict(result.stale_reasons_by_target)[target_id] == (
        "api_shape_changed",
        "catalog_changed",
        "capability_changed",
    )
    assert result.profile is not None
    assert result.profile.profile.metadata["lan_discovery"]["stale_reasons"] == [
        "api_shape_changed"
    ]


def test_complete_catalog_and_selected_model_drift_have_exact_target_reasons(
    tmp_path: Path,
) -> None:
    catalog_state = AgentStateStore(tmp_path / "catalog" / "agent.db")
    (
        _initial,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (alpha_id,),
    ) = _import_first_positive(catalog_state)
    expanded = _positive_observation(models=("alpha", "beta"))
    _row, expanded_scan = _persist_completed_scan(
        catalog_state,
        scan_id="scan-catalog-expanded",
        observation=expanded,
    )
    beta_id = _target_id(provider_id, "beta")
    catalog_result = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        _import_request(
            expanded,
            expanded_scan,
            profile_revision=1,
            target_revisions=((alpha_id, 1), (beta_id, 0)),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert dict(catalog_result.stale_reasons_by_target)[alpha_id] == (
        "catalog_changed",
        "capability_changed",
    )
    assert catalog_result.profile is not None
    assert catalog_result.profile.profile.metadata["lan_discovery"]["stale_reasons"] == []

    identity_state = AgentStateStore(tmp_path / "identity" / "agent.db")
    (
        _initial,
        _scan,
        identity_registry,
        _service,
        _result,
        identity_provider_id,
        (identity_alpha_id, identity_beta_id),
    ) = _import_first_positive(identity_state, models=("alpha", "beta"))
    selected_beta = _positive_observation(models=("beta",))
    _row, selected_beta_scan = _persist_completed_scan(
        identity_state,
        scan_id="scan-selected-beta",
        observation=selected_beta,
    )
    identity_result = LanDiscoveryService(
        identity_registry,
        clock=lambda: NOW,
    ).import_observation(
        _import_request(
            selected_beta,
            selected_beta_scan,
            profile_revision=1,
            target_revisions=((identity_alpha_id, 1), (identity_beta_id, 1)),
        ),
        authenticated_owner_principal=OWNER,
    )
    identity_reasons = dict(identity_result.stale_reasons_by_target)
    assert identity_reasons[identity_alpha_id] == (
        "catalog_changed",
        "model_identity_changed",
        "model_missing",
    )
    assert identity_reasons[identity_beta_id] == (
        "catalog_changed",
        "model_identity_changed",
        "capability_changed",
    )
    assert identity_provider_id != ""


@pytest.mark.parametrize("alpha_already_exists", [False, True])
def test_complete_selected_model_change_stales_new_selected_binding(
    tmp_path: Path,
    alpha_already_exists: bool,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _initial,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (beta_id,),
    ) = _import_first_positive(state, models=("beta",))
    alpha_id = _target_id(provider_id, "alpha")
    profile_revision = 1
    beta_revision = 1
    alpha_revision = 0
    if alpha_already_exists:
        incomplete = _positive_observation(
            models=("alpha", "beta"),
            catalog_complete=False,
        )
        _row, incomplete_scan = _persist_completed_scan(
            state,
            scan_id="scan-add-alpha-incomplete",
            observation=incomplete,
        )
        added = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
            _import_request(
                incomplete,
                incomplete_scan,
                profile_revision=profile_revision,
                target_revisions=((alpha_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )
        assert added.profile is not None
        profile_revision = added.profile.revision
        alpha_revision = added.targets[0].revision
        beta = registry.get_model_target(beta_id)
        assert beta is not None
        complete_beta = _positive_observation(models=("beta",))
        _row, beta_scan = _persist_completed_scan(
            state,
            scan_id="scan-restore-complete-beta",
            observation=complete_beta,
        )
        restored = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
            _import_request(
                complete_beta,
                beta_scan,
                profile_revision=profile_revision,
                target_revisions=(
                    (beta_id, beta.revision),
                    (alpha_id, alpha_revision),
                ),
            ),
            authenticated_owner_principal=OWNER,
        )
        assert restored.profile is not None
        profile_revision = restored.profile.revision
        beta_revision = registry.get_model_target(beta_id).revision  # type: ignore[union-attr]
        alpha_revision = registry.get_model_target(alpha_id).revision  # type: ignore[union-attr]

    selected_alpha = _positive_observation(models=("alpha", "beta"))
    _row, selected_scan = _persist_completed_scan(
        state,
        scan_id="scan-selected-alpha",
        observation=selected_alpha,
    )
    changed = LanDiscoveryService(registry, clock=lambda: NOW).import_observation(
        _import_request(
            selected_alpha,
            selected_scan,
            profile_revision=profile_revision,
            target_revisions=(
                (alpha_id, alpha_revision),
                (beta_id, beta_revision),
            ),
        ),
        authenticated_owner_principal=OWNER,
    )

    alpha_reasons = dict(changed.stale_reasons_by_target)[alpha_id]
    assert "model_identity_changed" in alpha_reasons
    alpha = registry.get_model_target(alpha_id)
    assert alpha is not None
    assert alpha.target.capability_tags == ("generation",)
    assert alpha.target.health == "unavailable"


def test_incomplete_to_complete_selected_change_does_not_infer_identity_drift(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    incomplete = _positive_observation(
        models=("alpha", "beta"),
        catalog_complete=False,
    )
    _row, first_scan = _persist_completed_scan(
        state,
        scan_id="scan-incomplete-selected-alpha",
        observation=incomplete,
    )
    provider_id = _provider_id(incomplete.endpoint_binding_digest)
    alpha_id = _target_id(provider_id, "alpha")
    beta_id = _target_id(provider_id, "beta")
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    service.import_observation(
        _import_request(
            incomplete,
            first_scan,
            profile_revision=0,
            target_revisions=((alpha_id, 0), (beta_id, 0)),
        ),
        authenticated_owner_principal=OWNER,
    )
    selected_beta = _positive_observation(models=("beta",))
    _row, second_scan = _persist_completed_scan(
        state,
        scan_id="scan-complete-selected-beta",
        observation=selected_beta,
    )

    result = service.import_observation(
        _import_request(
            selected_beta,
            second_scan,
            profile_revision=1,
            target_revisions=((alpha_id, 1), (beta_id, 1)),
        ),
        authenticated_owner_principal=OWNER,
    )

    reasons = dict(result.stale_reasons_by_target)
    assert "model_identity_changed" not in reasons[alpha_id]
    assert "model_identity_changed" not in reasons[beta_id]


def test_complete_selected_identity_survives_intervening_incomplete_refresh(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _initial,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (beta_id,),
    ) = _import_first_positive(state, models=("beta",))
    alpha_id = _target_id(provider_id, "alpha")
    incomplete = _positive_observation(
        models=("alpha", "beta"),
        catalog_complete=False,
    )
    _row, incomplete_scan = _persist_completed_scan(
        state,
        scan_id="scan-intervening-incomplete",
        observation=incomplete,
    )
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    added = service.import_observation(
        _import_request(
            incomplete,
            incomplete_scan,
            profile_revision=1,
            target_revisions=((alpha_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert added.profile is not None
    beta_before = registry.get_model_target(beta_id)
    alpha_before = registry.get_model_target(alpha_id)
    assert beta_before is not None and alpha_before is not None
    complete_selected_alpha = _positive_observation(models=("alpha", "beta"))
    _row, complete_scan = _persist_completed_scan(
        state,
        scan_id="scan-complete-selected-after-incomplete",
        observation=complete_selected_alpha,
    )

    result = service.import_observation(
        _import_request(
            complete_selected_alpha,
            complete_scan,
            profile_revision=added.profile.revision,
            target_revisions=(
                (alpha_id, alpha_before.revision),
                (beta_id, beta_before.revision),
            ),
        ),
        authenticated_owner_principal=OWNER,
    )

    reasons = dict(result.stale_reasons_by_target)
    assert "model_identity_changed" in reasons[alpha_id]
    assert "model_identity_changed" in reasons[beta_id]


def test_stale_target_never_auto_clears_on_old_exact_reappearance(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    original_scope = _scope()
    (
        original,
        _scan,
        registry,
        _service,
        _result,
        _provider_id_value,
        (target_id,),
    ) = _import_first_positive(state, scope=original_scope)
    narrowed_scope = _scope(network="192.168.50.0/30")
    drift = _positive_observation(scope=narrowed_scope)
    _row, drift_scan = _persist_completed_scan(
        state,
        scan_id="scan-first-drift",
        scope=narrowed_scope,
        observation=drift,
    )
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    drifted = service.import_observation(
        _import_request(
            drift,
            drift_scan,
            profile_revision=1,
            target_revisions=((target_id, 1),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert drifted.profile is not None
    stale = registry.get_model_target(target_id)
    assert stale is not None
    _row, reappearance_scan = _persist_completed_scan(
        state,
        scan_id="scan-old-reappearance",
        scope=original_scope,
        observation=original,
    )
    reappeared = service.import_observation(
        _import_request(
            original,
            reappearance_scan,
            profile_revision=drifted.profile.revision,
            target_revisions=((target_id, stale.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    after = registry.get_model_target(target_id)
    assert after is not None
    assert after.target.enabled is False
    assert after.target.trust_class == "unconfirmed"
    assert after.target.metadata["lan_discovery"]["reviewed"] is False
    assert after.target.metadata["lan_discovery"]["stale_reasons"]
    assert reappeared.stale_reasons_by_target


def test_disabled_owner_review_uses_hand_derived_digests_and_persists_preimage(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    protected = target.target.metadata["lan_discovery"]
    request, privacy_digest, reviewed_material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=protected,
    )

    reviewed = service.review_lan_target(
        request,
        authenticated_owner_principal=OWNER,
    )

    reviewed_protected = reviewed.target.target.metadata["lan_discovery"]
    assert reviewed.profile.profile.enabled is False
    assert reviewed.target.target.enabled is False
    assert reviewed.target.target.trust_class == "operator_confirmed"
    assert reviewed.target.target.health == "unknown"
    assert reviewed.privacy_acknowledgement_digest == privacy_digest
    assert reviewed.material_binding_digest == reviewed_material
    assert reviewed_protected["reviewed"] is True
    assert reviewed_protected["reviewed_profile_revision"] == profile.revision
    assert reviewed_protected["reviewed_target_revision"] == target.revision
    assert (
        reviewed_protected["review_evidence_terminal_receipt_digest"]
        == (protected["terminal_receipt_digest"])
    )
    assert reviewed_protected["review_evidence_observation_digest"] == (
        observation.observation_digest
    )
    assert (
        reviewed_protected["reviewed_from_material_binding_digest"]
        == (protected["material_binding_digest"])
    )
    assert reviewed_protected["reviewed_material_binding_digest"] == reviewed_material
    assert reviewed_protected["review_acknowledged_stale_reasons"] == []
    assert (
        reviewed_protected["review_acknowledged_stale_transition_terminal_receipt_digest"] is None
    )
    assert reviewed_protected["review_digest"] == request.expected_review_digest
    assert reviewed_protected["privacy_acknowledgement_digest"] == privacy_digest
    assert reviewed_protected["material_binding_digest"] == reviewed_material
    persisted = registry.get_model_target(target_id)
    assert persisted == reviewed.target


@pytest.mark.parametrize(
    "tamper",
    ["receipt_digest", "membership", "column_public_disagreement", "timestamp"],
)
def test_review_revalidates_terminal_receipt_membership_and_durable_observation(
    tmp_path: Path,
    tamper: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        completed,
        registry,
        _service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )

    if tamper == "receipt_digest":
        _rewrite_terminal_receipt(
            state,
            completed.scan_id,
            mutate=lambda receipt: receipt.__setitem__("terminal_reason", "tampered"),
            recompute_digest=False,
        )
    elif tamper == "membership":
        _rewrite_terminal_receipt(
            state,
            completed.scan_id,
            mutate=lambda receipt: receipt.__setitem__("observations", []),
            recompute_digest=True,
        )
    elif tamper == "column_public_disagreement":
        _rewrite_observation_and_membership(
            state,
            completed.scan_id,
            observation.endpoint_binding_digest,
            public_payload_update={"api_shape": "ollama_compatible"},
        )
    else:
        _rewrite_observation_and_membership(
            state,
            completed.scan_id,
            observation.endpoint_binding_digest,
            freshness_timestamp="2026-08-01 12:00:00",
        )

    with pytest.raises(LanDiscoveryConflict):
        LanDiscoveryService(
            RoutingLedger(state),
            clock=lambda: NOW,
        ).review_lan_target(
            request,
            authenticated_owner_principal=OWNER,
        )

    assert registry.get_provider_profile(provider_id) == profile
    assert registry.get_model_target(target_id) == target


@pytest.mark.parametrize(
    "failure",
    [
        "profile_revision",
        "target_revision",
        "receipt",
        "observation",
        "endpoint",
        "material",
        "digest",
        "owner",
        "privacy",
        "stale_reasons",
        "trust_class",
        "expired",
    ],
)
def test_review_conflicts_are_read_only(
    tmp_path: Path,
    failure: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    owner = OWNER
    if failure == "profile_revision":
        request = _unchecked_clone(
            request,
            expected_profile_revision=profile.revision + 1,
        )
    elif failure == "target_revision":
        request = _unchecked_clone(
            request,
            expected_target_revision=target.revision + 1,
        )
    elif failure == "receipt":
        request = _unchecked_clone(
            request,
            expected_terminal_receipt_digest="sha256:" + "1" * 64,
        )
    elif failure == "observation":
        request = _unchecked_clone(
            request,
            expected_observation_digest="sha256:" + "2" * 64,
        )
    elif failure == "endpoint":
        request = _unchecked_clone(
            request,
            expected_endpoint_fingerprint="sha256:" + "3" * 64,
        )
    elif failure == "material":
        request = _unchecked_clone(
            request,
            expected_material_binding_digest="sha256:" + "4" * 64,
        )
    elif failure == "digest":
        request = _unchecked_clone(
            request,
            expected_review_digest="sha256:" + "5" * 64,
        )
    elif failure == "owner":
        owner = "owner:foreign"
    elif failure == "privacy":
        request = _unchecked_clone(request, privacy_acknowledged=False)
    elif failure == "stale_reasons":
        request = _unchecked_clone(
            request,
            expected_stale_reasons=("capability_changed", "catalog_changed"),
        )
    elif failure == "trust_class":
        request = _unchecked_clone(request, trust_class="standard")
    elif failure == "expired":
        service = LanDiscoveryService(
            registry,
            clock=lambda: NOW + timedelta(seconds=301),
        )
    else:
        raise AssertionError(f"unhandled review failure: {failure}")

    with pytest.raises(LanDiscoveryConflict):
        service.review_lan_target(
            request,
            authenticated_owner_principal=owner,
        )

    assert registry.get_provider_profile(provider_id) == profile
    assert registry.get_model_target(target_id) == target


@pytest.mark.parametrize(
    ("review_clock", "accepted"),
    [
        (NOW - timedelta(seconds=5), True),
        (NOW - timedelta(seconds=6), False),
    ],
)
def test_review_future_skew_boundary_is_exact_and_read_only(
    tmp_path: Path,
    review_clock: datetime,
    accepted: bool,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        _service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    service = LanDiscoveryService(registry, clock=lambda: review_clock)

    if accepted:
        reviewed = service.review_lan_target(
            request,
            authenticated_owner_principal=OWNER,
        )
        assert reviewed.target.revision == target.revision + 1
    else:
        with pytest.raises(LanDiscoveryConflict, match="fresh"):
            service.review_lan_target(
                request,
                authenticated_owner_principal=OWNER,
            )
        assert registry.get_provider_profile(provider_id) == profile
        assert registry.get_model_target(target_id) == target


@pytest.mark.parametrize(
    ("review_clock", "accepted"),
    [(NOW, True), (NOW - timedelta(seconds=1), False)],
)
def test_review_authenticates_independent_newer_profile_evidence_and_future_skew(
    tmp_path: Path,
    review_clock: datetime,
    accepted: bool,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _initial,
        _scan,
        registry,
        _service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    target_before = registry.get_model_target(target_id)
    assert target_before is not None
    profile_only = _positive_observation(models=("alpha",), catalog_complete=False)
    _row, profile_scan = _persist_completed_scan(
        state,
        scan_id="scan-independent-profile-evidence",
        observation=profile_only,
        observed_at=NOW + timedelta(seconds=5),
    )
    advanced = LanDiscoveryService(
        registry,
        clock=lambda: NOW + timedelta(seconds=5),
    ).import_observation(
        _import_request(
            profile_only,
            profile_scan,
            profile_revision=1,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert advanced.profile is not None
    assert registry.get_model_target(target_id) == target_before
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=advanced.profile.revision,
        target_revision=target_before.revision,
        target_id=target_id,
        protected=target_before.target.metadata["lan_discovery"],
    )
    service = LanDiscoveryService(registry, clock=lambda: review_clock)

    if accepted:
        reviewed = service.review_lan_target(
            request,
            authenticated_owner_principal=OWNER,
        )
        assert reviewed.profile.revision == advanced.profile.revision + 1
    else:
        with pytest.raises(LanDiscoveryConflict, match="fresh"):
            service.review_lan_target(
                request,
                authenticated_owner_principal=OWNER,
            )
        assert registry.get_provider_profile(provider_id) == advanced.profile
        assert registry.get_model_target(target_id) == target_before


@pytest.mark.parametrize(
    ("revision_field", "revision"),
    [
        (field, revision)
        for field in ("profile", "target")
        for revision in (False, True, -1, 1.0, "1")
    ],
)
def test_review_service_revalidates_forged_revision_types_read_only(
    tmp_path: Path,
    revision_field: str,
    revision: object,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    field_name = (
        "expected_profile_revision" if revision_field == "profile" else "expected_target_revision"
    )
    request = _unchecked_clone(request, **{field_name: revision})

    with pytest.raises((LanDiscoveryConflict, ValueError), match="revision"):
        service.review_lan_target(
            request,
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_provider_profile(provider_id) == profile
    assert registry.get_model_target(target_id) == target


@pytest.mark.parametrize(
    ("field", "affinities"),
    [
        (field, affinities)
        for field in ("intended_roles", "task_family_affinities")
        for affinities in (
            ("",),
            ("e\u0301",),
            ("bad\ncontrol",),
            ("é" * 33,),
            ("duplicate", "duplicate"),
            tuple(f"item-{index}" for index in range(17)),
        )
    ],
)
def test_review_affinities_are_nfc_unique_control_free_utf8_bounded_and_atomic(
    tmp_path: Path,
    field: str,
    affinities: tuple[str, ...],
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )

    with pytest.raises(ValueError, match="affinit|role|NFC|control|unique|64|16"):
        replace(request, **{field: affinities})
    forged = _unchecked_clone(request, **{field: affinities})
    with pytest.raises(LanDiscoveryConflict):
        service.review_lan_target(
            forged,
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_provider_profile(provider_id) == profile
    assert registry.get_model_target(target_id) == target


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("privacy_acknowledged", 1),
        ("privacy_acknowledged", "true"),
        ("enabled", 0),
        ("enabled", "false"),
        ("trust_class", "standard"),
    ],
)
def test_review_literals_reject_coercible_or_open_values(
    field: str,
    value: object,
) -> None:
    digest = "sha256:" + "1" * 64
    kwargs = {
        "target_id": "lan-target-" + "1" * 64,
        "expected_profile_revision": 1,
        "expected_target_revision": 1,
        "expected_terminal_receipt_digest": digest,
        "expected_observation_digest": digest,
        "expected_endpoint_fingerprint": digest,
        "expected_material_binding_digest": digest,
        "expected_review_digest": digest,
        "expected_stale_reasons": (),
        "trust_class": "operator_confirmed",
        "intended_roles": (),
        "task_family_affinities": (),
        "privacy_acknowledged": True,
        "enabled": False,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match="privacy|boolean|enabled|trust"):
        LanReviewRequest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("expected_profile_revision", True),
        ("expected_target_revision", 1.0),
        ("expected_review_digest", "sha256:" + "1" * 64 + "\n"),
        ("expected_stale_reasons", []),
        ("intended_roles", []),
        ("privacy_acknowledged", 1),
        ("enabled", 0),
        ("authenticated_owner_principal", " owner:local-runtime:v1"),
        ("now", datetime(2026, 8, 1, 12, 0)),
    ),
)
def test_direct_registry_review_boundary_rejects_inexact_inputs_before_writes(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        _service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    kwargs = {
        **request.__dict__,
        "authenticated_owner_principal": OWNER,
        "now": NOW,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        registry.review_lan_target(**kwargs)
    assert registry.get_provider_profile(provider_id) == profile
    assert registry.get_model_target(target_id) == target


def test_unchanged_refresh_preserves_immutable_review_preimage(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    review_request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    reviewed = service.review_lan_target(
        review_request,
        authenticated_owner_principal=OWNER,
    )
    preimage_fields = (
        "reviewed_profile_revision",
        "reviewed_target_revision",
        "review_evidence_terminal_receipt_digest",
        "review_evidence_observation_digest",
        "reviewed_from_material_binding_digest",
        "reviewed_material_binding_digest",
        "review_acknowledged_stale_reasons",
        "review_acknowledged_stale_transition_terminal_receipt_digest",
    )
    before = reviewed.target.target.metadata["lan_discovery"]
    _row, refresh_scan = _persist_completed_scan(
        state,
        scan_id="scan-after-review",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    refreshed = service.import_observation(
        _import_request(
            observation,
            refresh_scan,
            profile_revision=reviewed.profile.revision,
            target_revisions=((target_id, reviewed.target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert refreshed.profile is not None
    assert refreshed.profile.revision == reviewed.profile.revision + 1
    assert refreshed.targets[0].revision == reviewed.target.revision + 1
    after = refreshed.targets[0].target.metadata["lan_discovery"]
    assert {field: after[field] for field in preimage_fields} == {
        field: before[field] for field in preimage_fields
    }
    assert after["reviewed"] is True
    assert after["material_binding_digest"] == before["material_binding_digest"]
    assert after["scan_id"] == refresh_scan.scan_id
    assert after["terminal_receipt_digest"] == refresh_scan.terminal_receipt_digest
    assert after["observed_at"] == "2026-08-01T12:00:01Z"


@pytest.mark.parametrize("advance_profile_only", [False, True])
def test_sibling_review_requires_profile_to_retain_exact_transition_evidence(
    tmp_path: Path,
    advance_profile_only: bool,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _initial,
        _scan,
        registry,
        _service,
        _imported,
        provider_id,
        (alpha_id, beta_id),
    ) = _import_first_positive(state, models=("alpha", "beta"))
    narrowed_scope = _scope(network="192.168.50.0/30")
    drift = _positive_observation(
        scope=narrowed_scope,
        models=("alpha", "beta"),
    )
    _row, drift_scan = _persist_completed_scan(
        state,
        scan_id="scan-sibling-drift",
        scope=narrowed_scope,
        observation=drift,
    )
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    drifted = service.import_observation(
        _import_request(
            drift,
            drift_scan,
            profile_revision=1,
            target_revisions=((alpha_id, 1), (beta_id, 1)),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert drifted.profile is not None
    alpha = registry.get_model_target(alpha_id)
    beta = registry.get_model_target(beta_id)
    assert alpha is not None and beta is not None
    alpha_review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=drifted.profile.revision,
        target_revision=alpha.revision,
        target_id=alpha_id,
        protected=alpha.target.metadata["lan_discovery"],
    )
    reviewed_alpha = service.review_lan_target(
        alpha_review,
        authenticated_owner_principal=OWNER,
    )
    assert reviewed_alpha.profile.profile.metadata["lan_discovery"]["stale_reasons"] == []
    alpha_reviewed_protected = reviewed_alpha.target.target.metadata["lan_discovery"]
    assert alpha_reviewed_protected["review_acknowledged_stale_reasons"] == ["network_changed"]
    assert (
        alpha_reviewed_protected["review_acknowledged_stale_transition_terminal_receipt_digest"]
        == drifted.targets[0].target.metadata["lan_discovery"][
            "stale_transition_terminal_receipt_digest"
        ]
    )
    for field in (
        "reviewed_profile_revision",
        "reviewed_target_revision",
        "review_evidence_terminal_receipt_digest",
        "review_evidence_observation_digest",
        "reviewed_from_material_binding_digest",
        "reviewed_material_binding_digest",
    ):
        assert alpha_reviewed_protected[field] is not None
    assert alpha_reviewed_protected["review_digest"] == alpha_review.expected_review_digest
    assert alpha_reviewed_protected["privacy_acknowledgement_digest"] is not None
    assert (
        alpha_reviewed_protected["material_binding_digest"]
        == (alpha_reviewed_protected["reviewed_material_binding_digest"])
    )
    current_profile = reviewed_alpha.profile

    if advance_profile_only:
        profile_only = _positive_observation(
            scope=narrowed_scope,
            models=("alpha",),
            catalog_complete=False,
        )
        _row, profile_scan = _persist_completed_scan(
            state,
            scan_id="scan-profile-only",
            scope=narrowed_scope,
            observation=profile_only,
            observed_at=NOW + timedelta(seconds=1),
        )
        advanced = service.import_observation(
            _import_request(
                profile_only,
                profile_scan,
                profile_revision=current_profile.revision,
                target_revisions=(),
            ),
            authenticated_owner_principal=OWNER,
        )
        assert advanced.profile is not None
        current_profile = advanced.profile

    current_beta = registry.get_model_target(beta_id)
    assert current_beta is not None
    beta_review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=current_profile.revision,
        target_revision=current_beta.revision,
        target_id=beta_id,
        protected=current_beta.target.metadata["lan_discovery"],
    )
    if advance_profile_only:
        beta_before = current_beta
        with pytest.raises(LanDiscoveryConflict, match="evidence|stale|receipt"):
            service.review_lan_target(
                beta_review,
                authenticated_owner_principal=OWNER,
            )
        assert registry.get_model_target(beta_id) == beta_before
    else:
        reviewed_beta = service.review_lan_target(
            beta_review,
            authenticated_owner_principal=OWNER,
        )
        assert reviewed_beta.target.target.metadata["lan_discovery"]["reviewed"] is True


def test_nonstale_target_review_tolerates_newer_incomplete_profile_evidence(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _initial,
        _scan,
        registry,
        _service,
        _imported,
        _provider_id_value,
        (alpha_id, _beta_id),
    ) = _import_first_positive(state, models=("alpha", "beta"))
    alpha_before = registry.get_model_target(alpha_id)
    profile_before = registry.list_provider_profiles()[0]
    assert alpha_before is not None
    incomplete = _positive_observation(
        models=("alpha",),
        catalog_complete=False,
    )
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-newer-profile-evidence",
        observation=incomplete,
        observed_at=NOW + timedelta(seconds=1),
    )
    service = LanDiscoveryService(registry, clock=lambda: NOW + timedelta(seconds=1))
    advanced = service.import_observation(
        _import_request(
            incomplete,
            completed,
            profile_revision=profile_before.revision,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert advanced.profile is not None
    alpha_current = registry.get_model_target(alpha_id)
    assert alpha_current == alpha_before
    review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=advanced.profile.revision,
        target_revision=alpha_before.revision,
        target_id=alpha_id,
        protected=alpha_before.target.metadata["lan_discovery"],
    )

    reviewed = service.review_lan_target(
        review,
        authenticated_owner_principal=OWNER,
    )
    assert reviewed.target.target.metadata["lan_discovery"]["reviewed"] is True


@pytest.mark.parametrize("tamper", ["base_url", "public_scope", "owner_text"])
def test_strict_lan_profile_row_rejects_recomputed_or_noncanonical_authority(
    tmp_path: Path,
    tamper: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        _scan,
        registry,
        _service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    with state._connect() as connection:
        if tamper == "base_url":
            connection.execute(
                "UPDATE routing_provider_profiles SET base_url = ? WHERE profile_id = ?",
                ("http://192.168.50.2:1234", provider_id),
            )
        else:
            metadata = json.loads(json.dumps(profile.profile.metadata))
            protected = metadata["lan_discovery"]
            if tamper == "owner_text":
                protected["owner_principal"] = " owner:local-runtime:v1"
            else:
                protected["confirmed_network"] = "8.8.8.0/24"
                protected["address"] = "8.8.8.8"
                protected["endpoint_binding_digest"] = _digest(
                    {
                        "address": "8.8.8.8",
                        "interface_id": protected["interface_id"],
                        "port": protected["port"],
                        "schema": "kestrel.lan.endpoint-binding.v1",
                    }
                )
                protected["endpoint_fingerprint"] = _digest(
                    {
                        "schema": "kestrel.lan.endpoint-fingerprint.v1",
                        "endpoint_binding_digest": protected["endpoint_binding_digest"],
                        "interface_id": protected["interface_id"],
                        "confirmed_network": protected["confirmed_network"],
                        "address": protected["address"],
                        "port": protected["port"],
                        "transport_security": protected["transport_security"],
                        "certificate_sha256": protected["certificate_sha256"],
                        "api_shape": protected["api_shape"],
                    }
                )
            connection.execute(
                "UPDATE routing_provider_profiles SET metadata_json = ? WHERE profile_id = ?",
                (
                    json.dumps(
                        metadata,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    provider_id,
                ),
            )
    _row, refresh = _persist_completed_scan(
        state,
        scan_id=f"scan-strict-profile-{tamper}",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(LanDiscoveryConflict):
        LanDiscoveryService(
            registry,
            clock=lambda: NOW + timedelta(seconds=1),
        ).import_observation(
            _import_request(
                observation,
                refresh,
                profile_revision=profile.revision,
                target_revisions=((target_id, target.revision),),
            ),
            authenticated_owner_principal=OWNER,
        )


@pytest.mark.parametrize(
    "tamper",
    ["claim_container", "generation_failure", "secondary_observed"],
)
def test_strict_lan_target_row_rejects_nonexact_capability_projection(
    tmp_path: Path,
    tamper: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    metadata = json.loads(json.dumps(target.target.metadata))
    claims = metadata["lan_discovery"]["capability_claims"]
    if tamper == "claim_container":
        metadata["lan_discovery"]["capability_claims"] = {
            "capabilities": claims,
        }
    elif tamper == "generation_failure":
        claims[0].update(
            {
                "provenance": "observed",
                "status": "observed_failure",
                "supported": False,
            }
        )
    else:
        claims[1].update(
            {
                "provenance": "observed",
                "status": "observed_pass",
                "supported": True,
            }
        )
    with state._connect() as connection:
        connection.execute(
            "UPDATE routing_model_targets SET metadata_json = ? WHERE target_id = ?",
            (
                json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                target_id,
            ),
        )

    with pytest.raises(LanDiscoveryConflict):
        service.review_lan_target(review, authenticated_owner_principal=OWNER)


def _lan_route_contract() -> AgentTaskContract:
    return AgentTaskContract(
        task_id="task-lan-runtime-guard",
        run_id="run-lan-runtime-guard",
        role="worker",
        task_family="general",
        objective="Exercise exact LAN routing guard state.",
        complexity=0.2,
        ambiguity=0.1,
        risk="low",
    )


def test_router_emits_closed_reasons_from_structurally_valid_managed_rows(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    forced_unreviewed = replace(target.target, enabled=True, health="healthy")

    with pytest.raises(RoutingUnavailableError) as unreviewed:
        route_task(
            _lan_route_contract(),
            [forced_unreviewed],
            clock=lambda: NOW,
        )
    assert "lan_owner_review_required" in unreviewed.value.reason_codes
    with pytest.raises(RoutingUnavailableError) as unreviewed_direct:
        route_task(
            _lan_route_contract(),
            [forced_unreviewed],
            direct_target_id=forced_unreviewed.target_id,
            clock=lambda: NOW,
        )
    assert "lan_owner_review_required" in unreviewed_direct.value.reason_codes

    with pytest.raises(RoutingUnavailableError) as expired:
        route_task(
            _lan_route_contract(),
            [forced_unreviewed],
            clock=lambda: NOW + timedelta(seconds=301),
        )
    assert "lan_evidence_expired" in expired.value.reason_codes
    with pytest.raises(RoutingUnavailableError) as expired_direct:
        route_task(
            _lan_route_contract(),
            [forced_unreviewed],
            direct_target_id=forced_unreviewed.target_id,
            clock=lambda: NOW + timedelta(seconds=301),
        )
    assert "lan_evidence_expired" in expired_direct.value.reason_codes

    review_request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    reviewed = service.review_lan_target(
        review_request,
        authenticated_owner_principal=OWNER,
    )
    forced_reviewed = replace(reviewed.target.target, enabled=True, health="healthy")
    with pytest.raises(RoutingUnavailableError) as unhardened:
        route_task(
            _lan_route_contract(),
            [forced_reviewed],
            clock=lambda: NOW,
        )
    assert "lan_binding_invalid" in unhardened.value.reason_codes
    with pytest.raises(RoutingUnavailableError) as unhardened_direct:
        route_task(
            _lan_route_contract(),
            [forced_reviewed],
            direct_target_id=forced_reviewed.target_id,
            clock=lambda: NOW,
        )
    assert "lan_binding_invalid" in unhardened_direct.value.reason_codes

    stale_state = AgentStateStore(tmp_path / "stale" / "agent.db")
    (
        _initial,
        _scan,
        stale_registry,
        _service,
        _result,
        _profile_id_value,
        (stale_target_id,),
    ) = _import_first_positive(stale_state)
    narrowed_scope = _scope(network="192.168.50.0/30")
    drift = _positive_observation(scope=narrowed_scope)
    _row, drift_scan = _persist_completed_scan(
        stale_state,
        scan_id="scan-router-stale",
        scope=narrowed_scope,
        observation=drift,
    )
    LanDiscoveryService(stale_registry, clock=lambda: NOW).import_observation(
        _import_request(
            drift,
            drift_scan,
            profile_revision=1,
            target_revisions=((stale_target_id, 1),),
        ),
        authenticated_owner_principal=OWNER,
    )
    stale_target = stale_registry.get_model_target(stale_target_id)
    assert stale_target is not None
    forced_stale = replace(stale_target.target, enabled=True, health="healthy")
    with pytest.raises(RoutingUnavailableError) as stale:
        route_task(
            _lan_route_contract(),
            [forced_stale],
            clock=lambda: NOW,
        )
    assert "lan_binding_stale" in stale.value.reason_codes
    with pytest.raises(RoutingUnavailableError) as stale_direct:
        route_task(
            _lan_route_contract(),
            [forced_stale],
            direct_target_id=forced_stale.target_id,
            clock=lambda: NOW,
        )
    assert "lan_binding_stale" in stale_direct.value.reason_codes


def _task5b_service(
    registry: RoutingLedger,
    *,
    clock=lambda: NOW,
    interface_inventory_resolver=None,
) -> LanDiscoveryService:
    from nested_memvid_agent.lan_runtime_authority import (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    )

    return LanDiscoveryService(
        registry,
        clock=clock,
        runtime_hardening_version=LAN_OPENAI_RUNTIME_HARDENING_VERSION,
        interface_inventory_resolver=(
            interface_inventory_resolver
            or (
                lambda: CurrentLanInterfaceInventory(
                    (
                        CurrentLanInterfaceState(
                            os_identity="test:en0",
                            interface_index=7,
                            addresses=("192.168.50.1/29",),
                        ),
                    )
                )
            )
        ),
    )


def _enabled_task5b_binding(
    state: AgentStateStore,
    *,
    scan_id: str,
):
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id=scan_id,
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = _task5b_service(registry)
    imported = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert imported.profile is not None
    target = imported.targets[0]
    enable, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=imported.profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )
    enabled = service.review_lan_target(
        enable,
        authenticated_owner_principal=OWNER,
    )
    return observation, registry, service, provider_id, target_id, enabled


def _routing_inventory_snapshot(state: AgentStateStore) -> tuple[tuple[object, ...], ...]:
    with state._connect() as connection:
        return tuple(
            (table, *tuple(row))
            for table in ("routing_provider_profiles", "routing_model_targets")
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        )


def _enabled_task5b_siblings(
    state: AgentStateStore,
    *,
    scan_id: str,
    sibling_observed_at: datetime = NOW + timedelta(seconds=1),
):
    _observation, registry, _service, provider_id, alpha_id, enabled_alpha = (
        _enabled_task5b_binding(state, scan_id=scan_id)
    )
    gamma_observation = _positive_observation(
        models=("gamma",),
        catalog_complete=False,
    )
    _row, gamma_scan = _persist_completed_scan(
        state,
        scan_id=f"{scan_id}-gamma",
        observation=gamma_observation,
        observed_at=sibling_observed_at,
    )
    gamma_id = _target_id(provider_id, "gamma")
    service = _task5b_service(
        registry,
        clock=lambda: sibling_observed_at,
    )
    imported_gamma = service.import_observation(
        _import_request(
            gamma_observation,
            gamma_scan,
            profile_revision=enabled_alpha.profile.revision,
            target_revisions=((gamma_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert imported_gamma.profile is not None
    gamma = imported_gamma.targets[0]
    enable_gamma, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=imported_gamma.profile.revision,
        target_revision=gamma.revision,
        target_id=gamma_id,
        protected=gamma.target.metadata["lan_discovery"],
        enabled=True,
    )
    enabled_gamma = service.review_lan_target(
        enable_gamma,
        authenticated_owner_principal=OWNER,
    )
    alpha = registry.get_model_target(alpha_id)
    gamma = registry.get_model_target(gamma_id)
    assert alpha is not None and gamma is not None
    assert alpha.target.enabled is True and gamma.target.enabled is True
    return registry, provider_id, (alpha_id, gamma_id), enabled_gamma.profile, (alpha, gamma)


def _enabled_alpha_with_fresh_disabled_beta(
    state: AgentStateStore,
    *,
    scan_id: str,
):
    _observation, registry, _service, provider_id, alpha_id, enabled = _enabled_task5b_binding(
        state, scan_id=scan_id
    )
    beta_observation = _positive_observation(
        models=("beta",),
        catalog_complete=False,
    )
    _row, beta_scan = _persist_completed_scan(
        state,
        scan_id=f"{scan_id}-beta-refresh",
        observation=beta_observation,
        observed_at=NOW + timedelta(seconds=250),
    )
    beta_id = _target_id(provider_id, "beta")
    refreshed = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=250),
    ).import_observation(
        _import_request(
            beta_observation,
            beta_scan,
            profile_revision=enabled.profile.revision,
            target_revisions=((beta_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert refreshed.profile is not None
    alpha = registry.get_model_target(alpha_id)
    beta = registry.get_model_target(beta_id)
    assert alpha is not None and beta is not None
    assert alpha.target.enabled is True and beta.target.enabled is False
    return registry, provider_id, alpha_id, beta_id, refreshed.profile, alpha, beta


def test_runtime_interface_binding_digest_preserves_and_clears_with_authority(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation, registry, service, _provider_id_value, target_id, enabled = (
        _enabled_task5b_binding(state, scan_id="scan-binding-lifecycle")
    )
    first_binding = enabled.target.target.metadata["lan_discovery"][
        "reviewed_runtime_interface_binding_digest"
    ]
    assert first_binding is not None

    _row, refresh_scan = _persist_completed_scan(
        state,
        scan_id="scan-binding-exact-refresh",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    refreshed = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).import_observation(
        _import_request(
            observation,
            refresh_scan,
            profile_revision=enabled.profile.revision,
            target_revisions=((target_id, enabled.target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    refreshed_target = refreshed.targets[0]
    assert refreshed_target.target.enabled is True
    assert (
        refreshed_target.target.metadata["lan_discovery"][
            "reviewed_runtime_interface_binding_digest"
        ]
        == first_binding
    )

    disable, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=refreshed.profile.revision,
        target_revision=refreshed_target.revision,
        target_id=target_id,
        protected=refreshed_target.target.metadata["lan_discovery"],
        enabled=False,
    )
    disabled = service.review_lan_target(
        disable,
        authenticated_owner_principal=OWNER,
    )
    assert disabled.target.target.enabled is False
    assert (
        disabled.target.target.metadata["lan_discovery"][
            "reviewed_runtime_interface_binding_digest"
        ]
        is None
    )

    reenable, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=disabled.profile.revision,
        target_revision=disabled.target.revision,
        target_id=target_id,
        protected=disabled.target.target.metadata["lan_discovery"],
        enabled=True,
    )
    reenabled = service.review_lan_target(
        reenable,
        authenticated_owner_principal=OWNER,
    )
    second_binding = reenabled.target.target.metadata["lan_discovery"][
        "reviewed_runtime_interface_binding_digest"
    ]
    assert second_binding is not None
    assert second_binding != first_binding

    _row, downgrade_scan = _persist_completed_scan(
        state,
        scan_id="scan-binding-marker-downgrade",
        observation=observation,
        observed_at=NOW + timedelta(seconds=2),
    )
    downgraded = LanDiscoveryService(
        registry,
        clock=lambda: NOW + timedelta(seconds=2),
    ).import_observation(
        _import_request(
            observation,
            downgrade_scan,
            profile_revision=reenabled.profile.revision,
            target_revisions=((target_id, reenabled.target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert downgraded.targets[0].target.enabled is False
    downgraded_target = downgraded.targets[0].target
    downgraded_protected = downgraded_target.metadata["lan_discovery"]
    assert reenabled.material_binding_digest in downgraded.invalidated_binding_digests
    assert downgraded_target.trust_class == "unconfirmed"
    assert downgraded_target.role_affinities == ()
    assert downgraded_target.task_family_affinities == ()
    assert downgraded_protected["reviewed"] is False
    for field in (
        "reviewed_profile_revision",
        "reviewed_target_revision",
        "review_evidence_terminal_receipt_digest",
        "review_evidence_observation_digest",
        "reviewed_from_material_binding_digest",
        "reviewed_material_binding_digest",
        "review_acknowledged_stale_reasons",
        "review_acknowledged_stale_transition_terminal_receipt_digest",
        "review_digest",
        "privacy_acknowledgement_digest",
        "reviewed_runtime_interface_binding_digest",
    ):
        assert downgraded_protected[field] is None

    _row, reinstall_scan = _persist_completed_scan(
        state,
        scan_id="scan-binding-marker-reinstall",
        observation=observation,
        observed_at=NOW + timedelta(seconds=3),
    )
    assert downgraded.profile is not None
    reinstalled = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=3),
    ).import_observation(
        _import_request(
            observation,
            reinstall_scan,
            profile_revision=downgraded.profile.revision,
            target_revisions=((target_id, downgraded.targets[0].revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert reinstalled.profile is not None
    reinstalled_target = reinstalled.targets[0]
    assert reinstalled_target.target.enabled is False
    assert reinstalled_target.target.trust_class == "unconfirmed"
    assert reinstalled_target.target.metadata["lan_discovery"]["reviewed"] is False
    recover, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=reinstalled.profile.revision,
        target_revision=reinstalled_target.revision,
        target_id=target_id,
        protected=reinstalled_target.target.metadata["lan_discovery"],
        enabled=True,
    )
    recovered = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=3),
    ).review_lan_target(recover, authenticated_owner_principal=OWNER)
    assert recovered.target.target.enabled is True
    assert (
        recovered.target.target.metadata["lan_discovery"][
            "reviewed_runtime_interface_binding_digest"
        ]
        is not None
    )
    assert (
        recovered.target.target.metadata["lan_discovery"][
            "reviewed_runtime_interface_binding_digest"
        ]
        != second_binding
    )


def test_incomplete_marker_downgrade_commits_no_mixed_enabled_generation(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    _observation, registry, _service, provider_id, alpha_id, enabled = _enabled_task5b_binding(
        state, scan_id="scan-before-incomplete-downgrade"
    )
    beta_observation = _positive_observation(
        models=("beta",),
        catalog_complete=False,
    )
    _row, beta_scan = _persist_completed_scan(
        state,
        scan_id="scan-incomplete-beta-downgrade",
        observation=beta_observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    beta_id = _target_id(provider_id, "beta")

    result = LanDiscoveryService(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).import_observation(
        _import_request(
            beta_observation,
            beta_scan,
            profile_revision=enabled.profile.revision,
            target_revisions=(
                (beta_id, 0),
                (alpha_id, enabled.target.revision),
            ),
        ),
        authenticated_owner_principal=OWNER,
    )

    profile = registry.get_provider_profile(provider_id)
    alpha = registry.get_model_target(alpha_id)
    beta = registry.get_model_target(beta_id)
    assert profile is not None and alpha is not None and beta is not None
    assert result.affected_target_ids == (beta_id, alpha_id)
    assert profile.profile.enabled is False
    assert profile.profile.trust_class == "unconfirmed"
    assert profile.profile.metadata["lan_discovery"]["runtime_hardening"] is None
    assert all(target.target.enabled is False for target in (alpha, beta))
    assert all(
        target.target.metadata["lan_discovery"]["runtime_hardening"] is None
        for target in (alpha, beta)
    )


def test_incomplete_marker_downgrade_requires_every_enabled_sibling_revision_before_write(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    _observation, registry, _service, provider_id, _alpha_id, enabled = _enabled_task5b_binding(
        state, scan_id="scan-before-incomplete-missing-cas"
    )
    beta_observation = _positive_observation(
        models=("beta",),
        catalog_complete=False,
    )
    _row, beta_scan = _persist_completed_scan(
        state,
        scan_id="scan-incomplete-beta-missing-cas",
        observation=beta_observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    beta_id = _target_id(provider_id, "beta")
    before = _routing_inventory_snapshot(state)

    with pytest.raises(LanDiscoveryConflict) as raised:
        LanDiscoveryService(
            registry,
            clock=lambda: NOW + timedelta(seconds=1),
        ).import_observation(
            _import_request(
                beta_observation,
                beta_scan,
                profile_revision=enabled.profile.revision,
                target_revisions=((beta_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )

    assert _routing_inventory_snapshot(state) == before
    assert "revision" in str(raised.value).lower()


def test_incomplete_marker_downgrade_clears_every_omitted_enabled_sibling(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    registry, provider_id, sibling_ids, profile, siblings = _enabled_task5b_siblings(
        state,
        scan_id="scan-before-all-sibling-downgrade",
    )
    beta_observation = _positive_observation(
        models=("beta",),
        catalog_complete=False,
    )
    _row, beta_scan = _persist_completed_scan(
        state,
        scan_id="scan-incomplete-beta-all-siblings",
        observation=beta_observation,
        observed_at=NOW + timedelta(seconds=2),
    )
    beta_id = _target_id(provider_id, "beta")
    prior_materials = {
        str(target.target.metadata["lan_discovery"]["material_binding_digest"])
        for target in siblings
    }

    result = LanDiscoveryService(
        registry,
        clock=lambda: NOW + timedelta(seconds=2),
    ).import_observation(
        _import_request(
            beta_observation,
            beta_scan,
            profile_revision=profile.revision,
            target_revisions=(
                (beta_id, 0),
                *(
                    (target_id, target.revision)
                    for target_id, target in zip(
                        sibling_ids,
                        siblings,
                        strict=True,
                    )
                ),
            ),
        ),
        authenticated_owner_principal=OWNER,
    )

    current_profile = registry.get_provider_profile(provider_id)
    current_siblings = tuple(registry.get_model_target(target_id) for target_id in sibling_ids)
    assert current_profile is not None and all(target is not None for target in current_siblings)
    assert result.affected_target_ids == (beta_id, *tuple(sorted(sibling_ids)))
    assert set(result.invalidated_binding_digests) == prior_materials
    assert current_profile.profile.enabled is False
    for target in current_siblings:
        assert target is not None
        protected = target.target.metadata["lan_discovery"]
        assert target.target.enabled is False
        assert target.target.trust_class == "unconfirmed"
        assert target.target.role_affinities == ()
        assert target.target.task_family_affinities == ()
        assert target.target.health == "unknown"
        assert protected["runtime_hardening"] is None
        assert protected["reviewed"] is False
        assert protected["stale_reasons"] == []
        assert protected["stale_transition_terminal_receipt_digest"] is None
        for field in (
            "reviewed_profile_revision",
            "reviewed_target_revision",
            "review_evidence_terminal_receipt_digest",
            "review_evidence_observation_digest",
            "reviewed_from_material_binding_digest",
            "reviewed_material_binding_digest",
            "review_acknowledged_stale_reasons",
            "review_acknowledged_stale_transition_terminal_receipt_digest",
            "review_digest",
            "privacy_acknowledgement_digest",
            "reviewed_runtime_interface_binding_digest",
        ):
            assert protected[field] is None


def test_incomplete_marker_downgrade_post_upsert_failure_rolls_back_every_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    _observation, registry, _service, provider_id, alpha_id, enabled = _enabled_task5b_binding(
        state, scan_id="scan-before-incomplete-post-upsert-crash"
    )
    beta_observation = _positive_observation(
        models=("beta",),
        catalog_complete=False,
    )
    _row, beta_scan = _persist_completed_scan(
        state,
        scan_id="scan-incomplete-beta-post-upsert-crash",
        observation=beta_observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    beta_id = _target_id(provider_id, "beta")
    before = _routing_inventory_snapshot(state)
    monkeypatch.setattr(
        ledger_registry,
        "_before_lan_commit",
        lambda: (_ for _ in ()).throw(RuntimeError("incomplete downgrade post-upsert crash")),
    )

    with pytest.raises(RuntimeError, match="incomplete downgrade post-upsert crash"):
        LanDiscoveryService(
            registry,
            clock=lambda: NOW + timedelta(seconds=1),
        ).import_observation(
            _import_request(
                beta_observation,
                beta_scan,
                profile_revision=enabled.profile.revision,
                target_revisions=(
                    (beta_id, 0),
                    (alpha_id, enabled.target.revision),
                ),
            ),
            authenticated_owner_principal=OWNER,
        )

    assert _routing_inventory_snapshot(state) == before


def test_same_marker_incomplete_refresh_keeps_omitted_enabled_target_unchanged(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.lan_runtime_authority import (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    )

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    _observation, registry, _service, provider_id, alpha_id, enabled = _enabled_task5b_binding(
        state, scan_id="scan-before-same-marker-incomplete"
    )
    alpha_before = registry.get_model_target(alpha_id)
    assert alpha_before is not None
    beta_observation = _positive_observation(
        models=("beta",),
        catalog_complete=False,
    )
    _row, beta_scan = _persist_completed_scan(
        state,
        scan_id="scan-same-marker-incomplete-beta",
        observation=beta_observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    beta_id = _target_id(provider_id, "beta")

    result = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).import_observation(
        _import_request(
            beta_observation,
            beta_scan,
            profile_revision=enabled.profile.revision,
            target_revisions=((beta_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )

    alpha_after = registry.get_model_target(alpha_id)
    beta = registry.get_model_target(beta_id)
    assert alpha_after == alpha_before
    assert beta is not None and beta.target.enabled is False
    assert result.affected_target_ids == (beta_id,)
    assert result.profile is not None and result.profile.profile.enabled is True
    assert (
        result.profile.profile.metadata["lan_discovery"]["runtime_hardening"]
        == LAN_OPENAI_RUNTIME_HARDENING_VERSION
    )


def test_outage_last_enabled_target_expiry_updates_fresh_profile_family(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    registry, provider_id, alpha_id, beta_id, profile, alpha, beta = (
        _enabled_alpha_with_fresh_disabled_beta(
            state,
            scan_id="scan-before-last-enabled-outage",
        )
    )
    outage = _outage_observation()
    _row, outage_scan = _persist_completed_scan(
        state,
        scan_id="scan-last-enabled-outage",
        observation=outage,
        observed_at=NOW + timedelta(seconds=301),
    )

    result = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=301),
    ).import_observation(
        _import_request(
            outage,
            outage_scan,
            profile_revision=profile.revision,
            target_revisions=((alpha_id, alpha.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )

    current_profile = registry.get_provider_profile(provider_id)
    current_alpha = registry.get_model_target(alpha_id)
    current_beta = registry.get_model_target(beta_id)
    assert result.affected_target_ids == (alpha_id,)
    assert result.profile is not None
    assert result.profile.revision == profile.revision + 1
    assert current_profile == result.profile
    assert current_profile.profile.enabled is False
    assert current_profile.profile.trust_class == "unconfirmed"
    assert current_alpha is not None and current_alpha.target.enabled is False
    assert current_beta == beta
    assert result.invalidated_binding_digests == (
        alpha.target.metadata["lan_discovery"]["material_binding_digest"],
    )


def test_outage_expired_target_preserves_profile_when_fresh_enabled_sibling_remains(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    registry, provider_id, sibling_ids, profile, siblings = _enabled_task5b_siblings(
        state,
        scan_id="scan-before-fresh-enabled-sibling-outage",
        sibling_observed_at=NOW + timedelta(seconds=250),
    )
    alpha_id, gamma_id = sibling_ids
    alpha, gamma = siblings
    outage = _outage_observation()
    _row, outage_scan = _persist_completed_scan(
        state,
        scan_id="scan-fresh-enabled-sibling-outage",
        observation=outage,
        observed_at=NOW + timedelta(seconds=301),
    )

    result = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=301),
    ).import_observation(
        _import_request(
            outage,
            outage_scan,
            profile_revision=profile.revision,
            target_revisions=((alpha_id, alpha.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )

    current_profile = registry.get_provider_profile(provider_id)
    current_alpha = registry.get_model_target(alpha_id)
    current_gamma = registry.get_model_target(gamma_id)
    assert result.affected_target_ids == (alpha_id,)
    assert result.profile == profile
    assert current_profile == profile
    assert current_profile.profile.enabled is True
    assert current_profile.profile.trust_class == "operator_confirmed"
    assert current_alpha is not None and current_alpha.target.enabled is False
    assert current_gamma == gamma


@pytest.mark.parametrize("missing_cas", ("profile", "target"))
def test_outage_last_enabled_expiry_requires_complete_cas_before_write(
    tmp_path: Path,
    missing_cas: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    registry, _provider_id_value, alpha_id, _beta_id, profile, alpha, _beta = (
        _enabled_alpha_with_fresh_disabled_beta(
            state,
            scan_id=f"scan-before-last-enabled-{missing_cas}-cas",
        )
    )
    outage = _outage_observation()
    _row, outage_scan = _persist_completed_scan(
        state,
        scan_id=f"scan-last-enabled-{missing_cas}-cas",
        observation=outage,
        observed_at=NOW + timedelta(seconds=301),
    )
    before = _routing_inventory_snapshot(state)

    with pytest.raises(LanDiscoveryConflict):
        _task5b_service(
            registry,
            clock=lambda: NOW + timedelta(seconds=301),
        ).import_observation(
            _import_request(
                outage,
                outage_scan,
                profile_revision=(
                    profile.revision - 1 if missing_cas == "profile" else profile.revision
                ),
                target_revisions=(() if missing_cas == "target" else ((alpha_id, alpha.revision),)),
            ),
            authenticated_owner_principal=OWNER,
        )

    assert _routing_inventory_snapshot(state) == before


def test_outage_last_enabled_expiry_post_upsert_failure_rolls_back_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    registry, _provider_id_value, alpha_id, _beta_id, profile, alpha, _beta = (
        _enabled_alpha_with_fresh_disabled_beta(
            state,
            scan_id="scan-before-last-enabled-outage-crash",
        )
    )
    outage = _outage_observation()
    _row, outage_scan = _persist_completed_scan(
        state,
        scan_id="scan-last-enabled-outage-crash",
        observation=outage,
        observed_at=NOW + timedelta(seconds=301),
    )
    before = _routing_inventory_snapshot(state)
    monkeypatch.setattr(
        ledger_registry,
        "_before_lan_commit",
        lambda: (_ for _ in ()).throw(RuntimeError("last enabled outage post-upsert crash")),
    )

    with pytest.raises(RuntimeError, match="last enabled outage post-upsert crash"):
        _task5b_service(
            registry,
            clock=lambda: NOW + timedelta(seconds=301),
        ).import_observation(
            _import_request(
                outage,
                outage_scan,
                profile_revision=profile.revision,
                target_revisions=((alpha_id, alpha.revision),),
            ),
            authenticated_owner_principal=OWNER,
        )

    assert _routing_inventory_snapshot(state) == before


def test_marker_downgrade_authority_invalidation_rolls_back_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation, registry, _service, provider_id, target_id, enabled = _enabled_task5b_binding(
        state, scan_id="scan-binding-before-downgrade-crash"
    )
    _row, downgrade_scan = _persist_completed_scan(
        state,
        scan_id="scan-binding-downgrade-crash",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    before_profile = registry.get_provider_profile(provider_id)
    before_target = registry.get_model_target(target_id)
    assert before_profile is not None and before_target is not None
    monkeypatch.setattr(
        ledger_registry,
        "_before_lan_commit",
        lambda: (_ for _ in ()).throw(RuntimeError("downgrade commit crash")),
    )

    with pytest.raises(RuntimeError, match="downgrade commit crash"):
        LanDiscoveryService(
            registry,
            clock=lambda: NOW + timedelta(seconds=1),
        ).import_observation(
            _import_request(
                observation,
                downgrade_scan,
                profile_revision=enabled.profile.revision,
                target_revisions=((target_id, enabled.target.revision),),
            ),
            authenticated_owner_principal=OWNER,
        )

    assert registry.get_provider_profile(provider_id) == before_profile
    assert registry.get_model_target(target_id) == before_target


@pytest.mark.parametrize(
    "invalidation",
    ("network_drift", "outage_expiry", "replacement"),
)
def test_runtime_interface_binding_digest_clears_on_every_stale_transition(
    tmp_path: Path,
    invalidation: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation, registry, _service, provider_id, target_id, enabled = _enabled_task5b_binding(
        state,
        scan_id=f"scan-binding-before-{invalidation}",
    )
    old_protected = enabled.target.target.metadata["lan_discovery"]
    assert old_protected["reviewed_runtime_interface_binding_digest"] is not None

    if invalidation == "network_drift":
        changed_scope = _scope(network="192.168.50.0/30")
        changed = _positive_observation(scope=changed_scope)
        observed_at = NOW + timedelta(seconds=1)
        _row, completed = _persist_completed_scan(
            state,
            scan_id="scan-binding-network-drift",
            observation=changed,
            scope=changed_scope,
            observed_at=observed_at,
        )
        request = _import_request(
            changed,
            completed,
            profile_revision=enabled.profile.revision,
            target_revisions=((target_id, enabled.target.revision),),
        )
    elif invalidation == "outage_expiry":
        changed = _outage_observation()
        observed_at = NOW + timedelta(seconds=301)
        _row, completed = _persist_completed_scan(
            state,
            scan_id="scan-binding-outage-expiry",
            observation=changed,
            observed_at=observed_at,
        )
        request = _import_request(
            changed,
            completed,
            profile_revision=enabled.profile.revision,
            target_revisions=((target_id, enabled.target.revision),),
        )
    else:
        changed = _positive_observation(address="192.168.50.3")
        observed_at = NOW + timedelta(seconds=1)
        _row, completed = _persist_completed_scan(
            state,
            scan_id="scan-binding-replacement",
            observation=changed,
            observed_at=observed_at,
        )
        new_provider_id = _provider_id(changed.endpoint_binding_digest)
        new_target_id = _target_id(new_provider_id, "alpha")
        request = replace(
            _import_request(
                changed,
                completed,
                profile_revision=0,
                target_revisions=(
                    (target_id, enabled.target.revision),
                    (new_target_id, 0),
                ),
            ),
            replacement=LanReplacementConfirmation(
                provider_profile_id=provider_id,
                expected_profile_revision=enabled.profile.revision,
                expected_endpoint_fingerprint=str(
                    enabled.profile.profile.metadata["lan_discovery"]["endpoint_fingerprint"]
                ),
                expected_material_binding_digests=(str(old_protected["material_binding_digest"]),),
            ),
        )

    _task5b_service(registry, clock=lambda: observed_at).import_observation(
        request,
        authenticated_owner_principal=OWNER,
    )
    stale = registry.get_model_target(target_id)
    assert stale is not None
    assert stale.target.enabled is False
    assert stale.target.metadata["lan_discovery"]["stale_reasons"]
    assert (
        stale.target.metadata["lan_discovery"]["reviewed_runtime_interface_binding_digest"] is None
    )


@pytest.mark.parametrize("resource", ("profile", "target"))
@pytest.mark.parametrize("missing_field", ("observation_source", "endpoint_kind"))
@pytest.mark.parametrize("operation", ("read", "review", "runtime"))
def test_partial_legacy_source_or_kind_metadata_fails_closed_without_writes(
    tmp_path: Path,
    resource: str,
    missing_field: str,
    operation: str,
) -> None:
    state = AgentStateStore(tmp_path / operation / resource / missing_field / "agent.db")
    (
        _observation,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (target_id,),
    ) = _import_first_positive(
        state,
        scan_id=f"scan-partial-legacy-{resource}-{missing_field}",
    )
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=False,
    )

    table, id_column, resource_id = {
        "profile": ("routing_provider_profiles", "profile_id", provider_id),
        "target": ("routing_model_targets", "target_id", target_id),
    }[resource]
    surviving_field = (
        "endpoint_kind" if missing_field == "observation_source" else "observation_source"
    )
    with state._connect() as connection:
        row = connection.execute(
            f"SELECT metadata_json FROM {table} WHERE {id_column} = ?",
            (resource_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        protected = metadata["lan_discovery"]
        protected["observation_source"] = "active"
        protected["endpoint_kind"] = "automatic"
        protected.pop(missing_field)
        assert missing_field not in protected
        assert protected[surviving_field] in {"active", "automatic"}
        connection.execute(
            f"UPDATE {table} SET metadata_json = ? WHERE {id_column} = ?",
            (
                json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                resource_id,
            ),
        )

    before = _routing_inventory_snapshot(state)
    error_pattern = "protected metadata.*field set"
    inventory_calls = 0

    def forbidden_inventory() -> CurrentLanInterfaceInventory:
        nonlocal inventory_calls
        inventory_calls += 1
        raise AssertionError("partial legacy metadata must fail before inventory")

    if operation == "read":
        if resource == "profile":
            assert registry.get_model_target(target_id) == target
            with pytest.raises(ValueError, match=error_pattern):
                registry.get_provider_profile(provider_id)
        else:
            assert registry.get_provider_profile(provider_id) == profile
            with pytest.raises(ValueError, match=error_pattern):
                registry.get_model_target(target_id)
    elif operation == "review":
        service = _task5b_service(
            registry,
            interface_inventory_resolver=forbidden_inventory,
        )
        with pytest.raises(LanDiscoveryConflict, match=error_pattern):
            service.review_lan_target(
                review,
                authenticated_owner_principal=OWNER,
            )
    else:
        with pytest.raises(ValueError, match=error_pattern):
            registry.resolve_lan_runtime_authority(
                target_id,
                clock=lambda: NOW,
                interface_inventory_resolver=forbidden_inventory,
            )
    assert inventory_calls == 0
    assert _routing_inventory_snapshot(state) == before


def test_task5b_positive_reimport_upgrades_exact_legacy_task5a_target_shape(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        _scan,
        _registry,
        _service,
        _result,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state, scan_id="scan-legacy-task5a")
    with state._connect() as connection:
        row = connection.execute(
            "SELECT metadata_json FROM routing_model_targets WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        legacy_endpoint_binding_digest = metadata["lan_discovery"]["endpoint_binding_digest"]
        legacy_material_binding_digest = metadata["lan_discovery"]["material_binding_digest"]
        metadata["lan_discovery"].pop("reviewed_runtime_interface_binding_digest")
        metadata["lan_discovery"].pop("observation_source", None)
        metadata["lan_discovery"].pop("endpoint_kind", None)
        legacy_json = json.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        connection.execute(
            "UPDATE routing_model_targets SET metadata_json = ? WHERE target_id = ?",
            (legacy_json, target_id),
        )
        profile_row = connection.execute(
            "SELECT metadata_json FROM routing_provider_profiles WHERE profile_id = ?",
            (provider_id,),
        ).fetchone()
        assert profile_row is not None
        profile_metadata = json.loads(str(profile_row[0]))
        profile_metadata["lan_discovery"].pop("observation_source", None)
        profile_metadata["lan_discovery"].pop("endpoint_kind", None)
        legacy_profile_json = json.dumps(
            profile_metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        connection.execute(
            "UPDATE routing_provider_profiles SET metadata_json = ? WHERE profile_id = ?",
            (legacy_profile_json, provider_id),
        )

    registry = RoutingLedger(state)
    profile = registry.get_provider_profile(provider_id)
    normalized = registry.get_model_target(target_id)
    assert profile is not None and normalized is not None
    normalized_profile_protected = profile.profile.metadata["lan_discovery"]
    normalized_protected = normalized.target.metadata["lan_discovery"]
    assert normalized_profile_protected["observation_source"] == "active"
    assert normalized_profile_protected["endpoint_kind"] == "automatic"
    assert normalized_protected["observation_source"] == "active"
    assert normalized_protected["endpoint_kind"] == "automatic"
    assert normalized_protected["endpoint_binding_digest"] == legacy_endpoint_binding_digest
    assert normalized_protected["material_binding_digest"] == legacy_material_binding_digest
    assert normalized_protected["reviewed_runtime_interface_binding_digest"] is None
    with state._connect() as connection:
        persisted = connection.execute(
            "SELECT metadata_json FROM routing_model_targets WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        persisted_profile = connection.execute(
            "SELECT metadata_json FROM routing_provider_profiles WHERE profile_id = ?",
            (provider_id,),
        ).fetchone()
    assert persisted is not None and str(persisted[0]) == legacy_json
    assert persisted_profile is not None and str(persisted_profile[0]) == legacy_profile_json

    blocked_enable, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=normalized.revision,
        target_id=target_id,
        protected=normalized_protected,
        enabled=True,
    )
    with pytest.raises(
        LanDiscoveryConflict,
        match="upgrade_requires_positive_reimport",
    ):
        _task5b_service(registry).review_lan_target(
            blocked_enable,
            authenticated_owner_principal=OWNER,
        )
    blocked_disabled, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=normalized.revision,
        target_id=target_id,
        protected=normalized_protected,
        enabled=False,
    )
    with pytest.raises(
        LanDiscoveryConflict,
        match="upgrade_requires_positive_reimport",
    ):
        _task5b_service(registry).review_lan_target(
            blocked_disabled,
            authenticated_owner_principal=OWNER,
        )
    with state._connect() as connection:
        after_review = connection.execute(
            "SELECT metadata_json FROM routing_model_targets WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        profile_after_review = connection.execute(
            "SELECT metadata_json FROM routing_provider_profiles WHERE profile_id = ?",
            (provider_id,),
        ).fetchone()
    assert after_review is not None and str(after_review[0]) == legacy_json
    assert profile_after_review is not None
    assert str(profile_after_review[0]) == legacy_profile_json

    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-task5b-upgrade",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    upgraded = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=profile.revision,
            target_revisions=((target_id, normalized.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert upgraded.profile is not None
    upgraded_profile_protected = upgraded.profile.profile.metadata["lan_discovery"]
    upgraded_target = upgraded.targets[0]
    upgraded_protected = upgraded_target.target.metadata["lan_discovery"]
    assert upgraded_profile_protected["observation_source"] == "active"
    assert upgraded_profile_protected["endpoint_kind"] == "automatic"
    assert set(upgraded_protected) == TARGET_PROTECTED_KEYS
    assert upgraded_protected["observation_source"] == "active"
    assert upgraded_protected["endpoint_kind"] == "automatic"
    assert upgraded_protected["endpoint_binding_digest"] == legacy_endpoint_binding_digest
    assert upgraded_protected["material_binding_digest"] == legacy_material_binding_digest
    assert upgraded_protected["runtime_hardening"] is not None
    assert upgraded_protected["reviewed_runtime_interface_binding_digest"] is None

    enable, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=upgraded.profile.revision,
        target_revision=upgraded_target.revision,
        target_id=target_id,
        protected=upgraded_protected,
        enabled=True,
    )
    enabled = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).review_lan_target(enable, authenticated_owner_principal=OWNER)
    assert enabled.target.target.enabled is True
    assert (
        enabled.target.target.metadata["lan_discovery"]["reviewed_runtime_interface_binding_digest"]
        is not None
    )


@pytest.mark.parametrize("hostile_state", ("marked", "enabled"))
def test_legacy_runtime_binding_adapter_rejects_non_task5a_rows(
    tmp_path: Path,
    hostile_state: str,
) -> None:
    from nested_memvid_agent.lan_runtime_authority import (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    )

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        _registry,
        _service,
        _result,
        _provider_id,
        (target_id,),
    ) = _import_first_positive(state, scan_id=f"scan-legacy-hostile-{hostile_state}")
    with state._connect() as connection:
        row = connection.execute(
            "SELECT metadata_json FROM routing_model_targets WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        protected = metadata["lan_discovery"]
        protected.pop("reviewed_runtime_interface_binding_digest")
        if hostile_state == "marked":
            protected["runtime_hardening"] = LAN_OPENAI_RUNTIME_HARDENING_VERSION
        connection.execute(
            """
            UPDATE routing_model_targets
            SET metadata_json = ?, enabled = ?
            WHERE target_id = ?
            """,
            (
                json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                1 if hostile_state == "enabled" else 0,
                target_id,
            ),
        )

    with pytest.raises(ValueError, match="protected metadata|field set"):
        RoutingLedger(state).get_model_target(target_id)


def test_task5b_marker_requires_post_install_positive_reimport_before_enable(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.lan_runtime_authority import (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    )

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        _task5a_scan,
        registry,
        _task5a_service,
        _task5a_result,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state, scan_id="scan-before-runtime-install")
    before_profile = registry.get_provider_profile(provider_id)
    before_target = registry.get_model_target(target_id)
    assert before_profile is not None and before_target is not None
    assert before_profile.profile.metadata["lan_discovery"]["runtime_hardening"] is None
    assert before_target.target.metadata["lan_discovery"]["runtime_hardening"] is None

    hardened_service = _task5b_service(registry)
    disabled_review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=before_profile.revision,
        target_revision=before_target.revision,
        target_id=target_id,
        protected=before_target.target.metadata["lan_discovery"],
        enabled=False,
    )
    disabled = hardened_service.review_lan_target(
        disabled_review,
        authenticated_owner_principal=OWNER,
    )
    assert disabled.profile.profile.metadata["lan_discovery"]["runtime_hardening"] is None
    assert disabled.target.target.metadata["lan_discovery"]["runtime_hardening"] is None
    after_disabled_profile = registry.get_provider_profile(provider_id)
    after_disabled_target = registry.get_model_target(target_id)
    assert after_disabled_profile is not None and after_disabled_target is not None

    review_only, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=after_disabled_profile.revision,
        target_revision=after_disabled_target.revision,
        target_id=target_id,
        protected=after_disabled_target.target.metadata["lan_discovery"],
        enabled=True,
    )
    with pytest.raises(
        LanDiscoveryConflict,
        match="lan_runtime_hardening_unavailable",
    ):
        hardened_service.review_lan_target(
            review_only,
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_provider_profile(provider_id) == after_disabled_profile
    assert registry.get_model_target(target_id) == after_disabled_target
    assert after_disabled_profile.profile.metadata["lan_discovery"]["runtime_hardening"] is None
    assert after_disabled_target.target.metadata["lan_discovery"]["runtime_hardening"] is None

    _row, refreshed_scan = _persist_completed_scan(
        state,
        scan_id="scan-after-runtime-install",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    refreshed = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).import_observation(
        _import_request(
            observation,
            refreshed_scan,
            profile_revision=after_disabled_profile.revision,
            target_revisions=((target_id, after_disabled_target.revision),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert refreshed.profile is not None
    assert refreshed.profile.profile.metadata["lan_discovery"]["runtime_hardening"] == (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION
    )
    assert refreshed.targets[0].target.metadata["lan_discovery"]["runtime_hardening"] == (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION
    )
    assert refreshed.profile.profile.enabled is False
    assert refreshed.targets[0].target.enabled is False

    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    enable, privacy_digest, reviewed_material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )
    enabled = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).review_lan_target(enable, authenticated_owner_principal=OWNER)
    assert enabled.profile.profile.enabled is True
    assert enabled.target.target.enabled is True
    assert enabled.target.target.health == "unknown"
    assert enabled.target.target.metadata["lan_discovery"]["reviewed"] is True
    assert (
        enabled.target.target.metadata["lan_discovery"]["reviewed_runtime_interface_binding_digest"]
        is not None
    )
    assert enabled.privacy_acknowledgement_digest == privacy_digest
    assert enabled.material_binding_digest == reviewed_material


def test_task5b_enable_and_disable_last_target_are_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-hardened-atomic",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = _task5b_service(registry)
    service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    enable, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )
    before_profile = profile
    before_target = target

    def crash() -> None:
        raise RuntimeError("review commit crash")

    monkeypatch.setattr(ledger_registry, "_before_lan_commit", crash)
    with pytest.raises(RuntimeError, match="review commit crash"):
        service.review_lan_target(enable, authenticated_owner_principal=OWNER)
    assert registry.get_provider_profile(provider_id) == before_profile
    assert registry.get_model_target(target_id) == before_target

    monkeypatch.setattr(ledger_registry, "_before_lan_commit", lambda: None)
    enabled = service.review_lan_target(enable, authenticated_owner_principal=OWNER)
    assert enabled.profile.profile.enabled is True
    assert enabled.target.target.enabled is True

    disable, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=enabled.profile.revision,
        target_revision=enabled.target.revision,
        target_id=target_id,
        protected=enabled.target.target.metadata["lan_discovery"],
        enabled=False,
    )
    before_disable_profile = registry.get_provider_profile(provider_id)
    before_disable_target = registry.get_model_target(target_id)
    assert before_disable_profile is not None and before_disable_target is not None
    monkeypatch.setattr(ledger_registry, "_before_lan_commit", crash)
    with pytest.raises(RuntimeError, match="review commit crash"):
        service.review_lan_target(disable, authenticated_owner_principal=OWNER)
    assert registry.get_provider_profile(provider_id) == before_disable_profile
    assert registry.get_model_target(target_id) == before_disable_target

    monkeypatch.setattr(ledger_registry, "_before_lan_commit", lambda: None)
    disabled = service.review_lan_target(disable, authenticated_owner_principal=OWNER)
    assert disabled.target.target.enabled is False
    assert disabled.profile.profile.enabled is False
    assert (
        disabled.target.target.metadata["lan_discovery"][
            "reviewed_runtime_interface_binding_digest"
        ]
        is None
    )


@pytest.mark.parametrize("failure", ("resolver_exception", "wrong_source"))
def test_enabled_review_inventory_failure_is_closed_and_byte_for_byte_atomic(
    tmp_path: Path,
    failure: str,
) -> None:
    token = f"review-inventory-{failure}-secret"
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id=f"scan-review-inventory-{failure}",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    _task5b_service(registry).import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    enable, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )

    with state._connect() as connection:
        before = tuple(
            tuple(row)
            for table in ("routing_provider_profiles", "routing_model_targets")
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        )
    calls = 0

    def failing_inventory() -> CurrentLanInterfaceInventory:
        nonlocal calls
        calls += 1
        if failure == "resolver_exception":
            raise RuntimeError(token)
        return CurrentLanInterfaceInventory(
            (
                CurrentLanInterfaceState(
                    os_identity="test:en0",
                    interface_index=7,
                    addresses=("192.168.50.3/29",),
                ),
            )
        )

    with pytest.raises(LanDiscoveryConflict) as raised:
        _task5b_service(
            registry,
            interface_inventory_resolver=failing_inventory,
        ).review_lan_target(enable, authenticated_owner_principal=OWNER)

    assert calls == 1
    assert raised.value.__cause__ is None
    assert token not in "".join(
        traceback.format_exception(
            type(raised.value),
            raised.value,
            raised.value.__traceback__,
        )
    )
    with state._connect() as connection:
        after = tuple(
            tuple(row)
            for table in ("routing_provider_profiles", "routing_model_targets")
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        )
    assert after == before
    current = registry.get_model_target(target_id)
    assert current is not None
    assert current.target.enabled is False
    assert (
        current.target.metadata["lan_discovery"]["reviewed_runtime_interface_binding_digest"]
        is None
    )


def test_hardening_marker_reimport_obeys_cas_and_commit_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state, scan_id="scan-pre-marker")
    before_profile = registry.get_provider_profile(provider_id)
    before_target = registry.get_model_target(target_id)
    assert before_profile is not None and before_target is not None
    _row, refreshed_scan = _persist_completed_scan(
        state,
        scan_id="scan-marker-refresh",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    service = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    with pytest.raises(LanDiscoveryConflict):
        service.import_observation(
            _import_request(
                observation,
                refreshed_scan,
                profile_revision=0,
                target_revisions=((target_id, before_target.revision),),
            ),
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_provider_profile(provider_id) == before_profile
    assert registry.get_model_target(target_id) == before_target

    monkeypatch.setattr(
        ledger_registry,
        "_before_lan_commit",
        lambda: (_ for _ in ()).throw(RuntimeError("marker commit crash")),
    )
    with pytest.raises(RuntimeError, match="marker commit crash"):
        service.import_observation(
            _import_request(
                observation,
                refreshed_scan,
                profile_revision=before_profile.revision,
                target_revisions=((target_id, before_target.revision),),
            ),
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_provider_profile(provider_id) == before_profile
    assert registry.get_model_target(target_id) == before_target


def test_wrong_or_forged_runtime_hardening_marker_never_enables(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.lan_runtime_authority import (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    )

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state, scan_id="scan-forged-marker")
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None

    with pytest.raises(ValueError, match="runtime hardening|version"):
        LanDiscoveryService(
            registry,
            clock=lambda: NOW,
            runtime_hardening_version="kestrel.lan.runtime.openai.v0",
        )

    forged_metadata = json.loads(json.dumps(target.target.metadata))
    forged_metadata["lan_discovery"]["runtime_hardening"] = (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION + "-forged"
    )
    with state._connect() as connection:
        connection.execute(
            "UPDATE routing_model_targets SET metadata_json = ? WHERE target_id = ?",
            (
                json.dumps(
                    forged_metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                target_id,
            ),
        )
    forged = registry.get_model_target(target_id)
    assert forged is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=forged.revision,
        target_id=target_id,
        protected=forged.target.metadata["lan_discovery"],
        enabled=True,
    )
    with pytest.raises(LanDiscoveryConflict, match="hardening|binding"):
        _task5b_service(registry).review_lan_target(
            request,
            authenticated_owner_principal=OWNER,
        )
    assert registry.get_provider_profile(provider_id) == profile


def test_ollama_import_stays_disabled_and_cannot_fallback_to_generic_runtime(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _ollama_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-ollama-after-runtime-install",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = _task5b_service(registry)
    imported = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert imported.profile is not None
    assert imported.profile.profile.adapter == "lan-ollama-compatible"
    assert imported.profile.profile.metadata["lan_discovery"]["runtime_hardening"] is None
    assert imported.targets[0].target.metadata["lan_discovery"]["runtime_hardening"] is None
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )

    with pytest.raises(
        LanDiscoveryConflict,
        match="lan_runtime_hardening_unavailable",
    ):
        service.review_lan_target(request, authenticated_owner_principal=OWNER)
    assert registry.get_provider_profile(provider_id) == profile
    assert registry.get_model_target(target_id) == target


def test_task7b_manual_loader_authenticates_receipt_bound_source_before_reconstruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scope = _manual_scope()
    observation = _manual_observation(scope=scope)
    row, completed = _persist_completed_manual_scan(
        state,
        scan_id="scan-task7b-manual-loader",
        observation=observation,
        scope=scope,
    )
    assert completed.terminal_receipt_digest is not None

    with state._connect() as connection:
        authenticated = load_authenticated_task4_observation(
            connection,
            scan_id=completed.scan_id,
            endpoint_binding_digest=observation.endpoint_binding_digest,
            expected_terminal_receipt_digest=completed.terminal_receipt_digest,
            expected_observation_digest=observation.observation_digest,
            authenticated_owner_principal=OWNER,
        )

    assert authenticated.source == "manual"
    assert type(authenticated.observation.endpoint) is _manual_endpoint_type()
    assert authenticated.observation.endpoint.kind == "manual"
    assert authenticated.observation.endpoint.port == 5001
    assert authenticated.confirmed_network == "192.168.50.2/32"

    with state._connect() as connection:
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_observation_update_immutable")
        connection.execute(
            """
            UPDATE routing_lan_observations
            SET source = 'active'
            WHERE scan_id = ? AND endpoint_id = ?
            """,
            (completed.scan_id, row.endpoint_id),
        )

    reconstruction_calls = 0

    def forbidden_reconstruction(*_args: object, **_kwargs: object) -> object:
        nonlocal reconstruction_calls
        reconstruction_calls += 1
        raise AssertionError("receipt membership must authenticate source first")

    for name in (
        "validate_observation",
        "_validate_task4_draft_preimage",
        "_task4_observation_from_row",
        "_make_observation",
    ):
        monkeypatch.setattr(
            lan_serialization_module,
            name,
            forbidden_reconstruction,
        )
    with state._connect() as connection:
        with pytest.raises(ValueError, match="receipt|membership"):
            load_authenticated_task4_observation(
                connection,
                scan_id=completed.scan_id,
                endpoint_binding_digest=observation.endpoint_binding_digest,
                expected_terminal_receipt_digest=completed.terminal_receipt_digest,
                expected_observation_digest=observation.observation_digest,
                authenticated_owner_principal=OWNER,
            )
    assert reconstruction_calls == 0


@pytest.mark.parametrize(
    "authority_mismatch",
    (
        "manual_limits_port",
        "manual_limits_automatic_source",
        "automatic_limits_manual_source",
    ),
)
def test_task7b_loader_and_import_bind_limits_to_authenticated_endpoint_authority(
    tmp_path: Path,
    authority_mismatch: str,
) -> None:
    state = AgentStateStore(tmp_path / authority_mismatch / "agent.db")
    if authority_mismatch == "manual_limits_automatic_source":
        scope = _scope()
        observation = _positive_observation(scope=scope)
        _rows, completed = _persist_completed_scan_v2(
            state,
            scan_id=f"scan-{authority_mismatch}",
            observations=(observation,),
            scope=scope,
        )
        forged_limits = _manual_limits(1234)
    else:
        scope = _manual_scope()
        observation = _manual_observation(scope=scope)
        _row, completed = _persist_completed_manual_scan(
            state,
            scan_id=f"scan-{authority_mismatch}",
            observation=observation,
            scope=scope,
        )
        forged_limits = (
            _manual_limits(5002)
            if authority_mismatch == "manual_limits_port"
            else _automatic_limits()
        )
    receipt_digest = _rewrite_scan_limits_and_receipt(
        state,
        completed.scan_id,
        forged_limits,
    )

    with (
        state._connect() as connection,
        pytest.raises(
            ValueError,
            match="limits|manual|source|kind|port|event",
        ),
    ):
        load_authenticated_task4_observation(
            connection,
            scan_id=completed.scan_id,
            endpoint_binding_digest=observation.endpoint_binding_digest,
            expected_terminal_receipt_digest=receipt_digest,
            expected_observation_digest=observation.observation_digest,
            authenticated_owner_principal=OWNER,
        )

    current = LanDiscoveryLedger(state).get_scan(completed.scan_id)
    assert current is not None and current.terminal_receipt_digest == receipt_digest
    target_id = _target_id(_provider_id(observation.endpoint_binding_digest), "alpha")
    with pytest.raises(LanDiscoveryConflict):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            _import_request(
                observation,
                current,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


@pytest.mark.parametrize(
    "event_mismatch",
    ("network", "port_and_limits", "source", "kind"),
)
def test_task7b_manual_loader_and_import_bind_start_event_to_authenticated_authority(
    tmp_path: Path,
    event_mismatch: str,
) -> None:
    state = AgentStateStore(tmp_path / event_mismatch / "agent.db")
    scope = _manual_scope()
    observation = _manual_observation(scope=scope)
    _row, completed = _persist_completed_manual_scan(
        state,
        scan_id=f"scan-manual-event-{event_mismatch}",
        observation=observation,
        scope=scope,
    )

    def mutate(payload: dict[str, object]) -> None:
        if event_mismatch == "network":
            payload["network"] = "192.168.50.3/32"
        elif event_mismatch == "port_and_limits":
            payload["exact_port"] = 5002
            payload["limits"] = _manual_limits(5002)
        elif event_mismatch == "source":
            payload["observation_source"] = "active"
        else:
            payload["endpoint_kind"] = "automatic"

    _rewrite_scan_started_event(state, completed.scan_id, mutate=mutate)

    assert completed.terminal_receipt_digest is not None
    with (
        state._connect() as connection,
        pytest.raises(
            ValueError,
            match="event|manual|source|kind|network|port|limits",
        ),
    ):
        load_authenticated_task4_observation(
            connection,
            scan_id=completed.scan_id,
            endpoint_binding_digest=observation.endpoint_binding_digest,
            expected_terminal_receipt_digest=completed.terminal_receipt_digest,
            expected_observation_digest=observation.observation_digest,
            authenticated_owner_principal=OWNER,
        )

    target_id = _target_id(_provider_id(observation.endpoint_binding_digest), "alpha")
    with pytest.raises(LanDiscoveryConflict):
        LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW).import_observation(
            _import_request(
                observation,
                completed,
                profile_revision=0,
                target_revisions=((target_id, 0),),
            ),
            authenticated_owner_principal=OWNER,
        )
    assert RoutingLedger(state).list_provider_profiles() == []
    assert RoutingLedger(state).list_model_targets() == []


@pytest.mark.parametrize(
    "forgery",
    (
        "server_version",
        "contract_version",
        "expires_at",
        "started_at_encoding",
        "start_sequence",
        "start_created_at",
        "terminal_mdns_status",
        "terminal_counts",
    ),
)
def test_task7b_manual_loader_rejects_recomputed_nonexact_lifecycle_evidence(
    tmp_path: Path,
    forgery: str,
) -> None:
    state = AgentStateStore(tmp_path / forgery / "agent.db")
    scope = _manual_scope()
    observation = _manual_observation(scope=scope)
    _row, completed = _persist_completed_manual_scan(
        state,
        scan_id=f"scan-manual-lifecycle-{forgery}",
        observation=observation,
        scope=scope,
    )

    if forgery in {"server_version", "contract_version", "expires_at"}:

        def mutate_start(payload: dict[str, object]) -> None:
            if forgery == "server_version":
                payload["server_version"] = "kestrel-local-runtime-v2"
            elif forgery == "contract_version":
                payload["contract_version"] = "kestrel.lan.manual-preview-authorization.v2"
            else:
                with state._connect() as connection:
                    started_at = connection.execute(
                        "SELECT started_at FROM routing_lan_scans WHERE scan_id = ?",
                        (completed.scan_id,),
                    ).fetchone()["started_at"]
                assert type(started_at) is str
                payload["expires_at"] = started_at.replace("+00:00", "Z")

        _rewrite_scan_started_event(state, completed.scan_id, mutate=mutate_start)
    elif forgery == "start_sequence":
        _rewrite_scan_started_event_row(state, completed.scan_id, sequence=3)
    elif forgery == "started_at_encoding":
        _rewrite_scan_started_at_encoding(state, completed.scan_id)
    elif forgery == "start_created_at":
        _rewrite_scan_started_event_row(
            state,
            completed.scan_id,
            created_at="2099-08-01T12:00:00+00:00",
        )
    else:

        def mutate_receipt(receipt: dict[str, object]) -> None:
            if forgery == "terminal_mdns_status":
                receipt["mdns_status"] = "available"
            else:
                receipt["planned_count"] = 2

        _rewrite_terminal_receipt(
            state,
            completed.scan_id,
            mutate=mutate_receipt,
            recompute_digest=True,
        )

    forged = LanDiscoveryLedger(state).get_scan(completed.scan_id)
    assert forged is not None and forged.terminal_receipt_digest is not None
    with (
        state._connect() as connection,
        pytest.raises(ValueError, match="manual|start|preview|version|expiration|mDNS|count"),
    ):
        load_authenticated_task4_observation(
            connection,
            scan_id=forged.scan_id,
            endpoint_binding_digest=observation.endpoint_binding_digest,
            expected_terminal_receipt_digest=forged.terminal_receipt_digest,
            expected_observation_digest=observation.observation_digest,
            authenticated_owner_principal=OWNER,
        )


def test_task7b_observation_projection_requires_source_and_endpoint_kind_to_agree() -> None:
    manual_scope = _manual_scope()
    manual = _manual_observation(scope=manual_scope)
    with pytest.raises(ValueError, match="source|kind|manual|automatic"):
        lan_observation_to_draft(
            manual,
            scope=manual_scope,
            freshness_timestamp="2026-08-01T12:00:00Z",
            source="active",
        )

    automatic_scope = _scope()
    automatic = _positive_observation(scope=automatic_scope)
    with pytest.raises(ValueError, match="source|kind|manual|automatic"):
        lan_observation_to_draft(
            automatic,
            scope=automatic_scope,
            freshness_timestamp="2026-08-01T12:00:00Z",
            source="manual",
        )


def test_task7b_automatic_binding_digest_bytes_remain_v1_compatible(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state, scan_id="scan-task7b-automatic-digest-compat")
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    protected = target.target.metadata["lan_discovery"]

    assert protected.get("observation_source", "active") == "active"
    assert protected.get("endpoint_kind", "automatic") == "automatic"
    assert protected["endpoint_fingerprint"] == (
        "sha256:710f27d898f47ef2d7e65aab6d7f324a8ce39c1f32ec0b3fbd8baeeb3664e1a5"
    )
    assert _endpoint_fingerprint_digest(protected) == protected["endpoint_fingerprint"]
    assert protected["material_binding_digest"] == (
        "sha256:4426eb4158fdd43f417eea3371a43e693258654cac8082aa755d21e5adb5f28c"
    )
    assert (
        _review_material_digest(
            protected,
            trust_class="unconfirmed",
            privacy_acknowledgement_digest=None,
            intended_roles=(),
            task_family_affinities=(),
        )
        == protected["material_binding_digest"]
    )

    request, privacy_digest, reviewed_material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=protected,
    )
    assert privacy_digest == (
        "sha256:d9505217906e0aca8467fc89b1973fce7fcf32ed59ec5c27f02690d6f0701249"
    )
    assert reviewed_material == (
        "sha256:f1bdc81c344616d4bc37f7fa3c38cec5b559e6d1c9234b0e9ada5c4b5bc79b84"
    )

    reviewed = service.review_lan_target(
        request,
        authenticated_owner_principal=OWNER,
    )
    assert reviewed.material_binding_digest == reviewed_material


def test_task7b_mdns_automatic_binding_digest_bytes_remain_v1_compatible(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-task7b-mdns-automatic-digest-compat",
        observation=observation,
        source="mdns",
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = _task5b_service(registry)
    imported = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert imported.profile is not None
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    protected = target.target.metadata["lan_discovery"]

    assert protected["observation_source"] == "mdns"
    assert protected["endpoint_kind"] == "automatic"
    assert protected["endpoint_fingerprint"] == (
        "sha256:710f27d898f47ef2d7e65aab6d7f324a8ce39c1f32ec0b3fbd8baeeb3664e1a5"
    )
    assert _endpoint_fingerprint_digest(protected) == protected["endpoint_fingerprint"]
    assert protected["material_binding_digest"] == (
        "sha256:4426eb4158fdd43f417eea3371a43e693258654cac8082aa755d21e5adb5f28c"
    )
    assert (
        _review_material_digest(
            protected,
            trust_class="unconfirmed",
            privacy_acknowledgement_digest=None,
            intended_roles=(),
            task_family_affinities=(),
        )
        == protected["material_binding_digest"]
    )

    request, privacy_digest, reviewed_material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=protected,
    )
    assert privacy_digest == (
        "sha256:d9505217906e0aca8467fc89b1973fce7fcf32ed59ec5c27f02690d6f0701249"
    )
    assert reviewed_material == (
        "sha256:f1bdc81c344616d4bc37f7fa3c38cec5b559e6d1c9234b0e9ada5c4b5bc79b84"
    )

    reviewed = service.review_lan_target(
        request,
        authenticated_owner_principal=OWNER,
    )
    assert reviewed.material_binding_digest == reviewed_material


def test_task7b_manual_unusual_port_import_is_disabled_until_exact_review(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scope = _manual_scope()
    observation = _manual_observation(scope=scope, port=5001)
    _row, completed = _persist_completed_manual_scan(
        state,
        scan_id="scan-task7b-manual-review",
        observation=observation,
        scope=scope,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = _task5b_service(registry)

    imported = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert imported.profile is not None
    assert imported.profile.profile.enabled is False
    assert imported.profile.profile.trust_class == "unconfirmed"
    assert imported.profile.profile.secret_ref is None
    assert imported.profile.profile.base_url == "http://192.168.50.2:5001/v1"
    assert imported.targets[0].target.enabled is False
    assert imported.targets[0].target.trust_class == "unconfirmed"
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    profile_protected = profile.profile.metadata["lan_discovery"]
    protected = target.target.metadata["lan_discovery"]
    assert profile_protected["observation_source"] == "manual"
    assert profile_protected["endpoint_kind"] == "manual"
    assert protected["observation_source"] == "manual"
    assert protected["endpoint_kind"] == "manual"
    assert protected["port"] == 5001
    assert protected["endpoint_fingerprint"] == (
        "sha256:5c45013ffacb08ca06ddf1ee1895c67b754834c71ede9ee24475e50460c39771"
    )
    assert _endpoint_fingerprint_digest(protected) == protected["endpoint_fingerprint"]
    assert protected["material_binding_digest"] == (
        "sha256:0a4f49afd9dda49085d2ca109c103da74bce824c500edf1340488ee9c566a82d"
    )
    assert (
        _review_material_digest(
            protected,
            trust_class="unconfirmed",
            privacy_acknowledgement_digest=None,
            intended_roles=(),
            task_family_affinities=(),
        )
        == protected["material_binding_digest"]
    )

    review, privacy_digest, material_digest = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=protected,
        enabled=True,
    )
    with state._connect() as connection:
        target_row = connection.execute(
            "SELECT metadata_json FROM routing_model_targets WHERE target_id = ?",
            (target_id,),
        ).fetchone()
    assert target_row is not None
    original_target_json = str(target_row[0])
    for field_name, forged_value in (
        ("observation_source", "active"),
        ("endpoint_kind", "automatic"),
    ):
        forged_target = json.loads(original_target_json)
        forged_target["lan_discovery"][field_name] = forged_value
        with state._connect() as connection:
            connection.execute(
                "UPDATE routing_model_targets SET metadata_json = ? WHERE target_id = ?",
                (
                    json.dumps(
                        forged_target,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    target_id,
                ),
            )
        with pytest.raises(LanDiscoveryConflict, match="source|kind|binding|evidence"):
            service.review_lan_target(
                review,
                authenticated_owner_principal=OWNER,
            )
        with state._connect() as connection:
            connection.execute(
                "UPDATE routing_model_targets SET metadata_json = ? WHERE target_id = ?",
                (original_target_json, target_id),
            )
        assert registry.get_provider_profile(provider_id) == profile
        assert registry.get_model_target(target_id) == target

    invalid_reviews = (
        replace(review, expected_target_revision=target.revision + 1),
        replace(review, privacy_acknowledged=False),
        replace(review, expected_review_digest="sha256:" + "0" * 64),
    )
    for invalid in invalid_reviews:
        with pytest.raises(LanDiscoveryConflict):
            service.review_lan_target(
                invalid,
                authenticated_owner_principal=OWNER,
            )
        assert registry.get_provider_profile(provider_id) == profile
        assert registry.get_model_target(target_id) == target

    reviewed = service.review_lan_target(
        review,
        authenticated_owner_principal=OWNER,
    )

    assert reviewed.profile.profile.enabled is True
    assert reviewed.target.target.enabled is True
    assert reviewed.target.target.trust_class == "operator_confirmed"
    assert reviewed.privacy_acknowledgement_digest == privacy_digest
    assert reviewed.material_binding_digest == material_digest
    reviewed_protected = reviewed.target.target.metadata["lan_discovery"]
    assert reviewed_protected["observation_source"] == "manual"
    assert reviewed_protected["endpoint_kind"] == "manual"
    assert reviewed_protected["port"] == 5001
    assert reviewed_protected["reviewed_runtime_interface_binding_digest"] is not None


def test_task7b_manual_ollama_unusual_port_cannot_be_enabled(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scope = _manual_scope()
    observation = _manual_observation(
        scope=scope,
        port=5001,
        api_shape=ApiShape.OLLAMA_COMPATIBLE,
    )
    _row, completed = _persist_completed_manual_scan(
        state,
        scan_id="scan-task7b-manual-ollama",
        observation=observation,
        scope=scope,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = _task5b_service(registry)
    imported = service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    assert imported.profile is not None
    assert imported.profile.profile.enabled is False
    assert imported.targets[0].target.enabled is False
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    review, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )

    with pytest.raises(
        LanDiscoveryConflict,
        match="lan_runtime_hardening_unavailable|OpenAI|eligible",
    ):
        service.review_lan_target(review, authenticated_owner_principal=OWNER)

    assert registry.get_provider_profile(provider_id) == profile
    assert registry.get_model_target(target_id) == target


@pytest.mark.parametrize(
    "negative_factory",
    (_outage_observation, _failed_generation_observation, _empty_catalog_observation),
    ids=("unreachable", "failed_generation", "empty_catalog"),
)
def test_post_install_negative_reimport_does_not_install_marker_or_mutate_authority(
    tmp_path: Path,
    negative_factory,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _positive,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        (target_id,),
    ) = _import_first_positive(state, scan_id="scan-before-negative-marker")
    before_profile = registry.get_provider_profile(provider_id)
    before_target = registry.get_model_target(target_id)
    assert before_profile is not None and before_target is not None
    negative = negative_factory()
    _row, completed = _persist_completed_scan(
        state,
        scan_id=f"scan-negative-{negative.failure_category}",
        observation=negative,
        observed_at=NOW + timedelta(seconds=1),
    )

    result = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    ).import_observation(
        _import_request(
            negative,
            completed,
            profile_revision=before_profile.revision,
            target_revisions=(),
        ),
        authenticated_owner_principal=OWNER,
    )

    assert result.outage_observed is True
    assert registry.get_provider_profile(provider_id) == before_profile
    assert registry.get_model_target(target_id) == before_target
    assert before_profile.profile.metadata["lan_discovery"]["runtime_hardening"] is None
    assert before_target.target.metadata["lan_discovery"]["runtime_hardening"] is None


def test_forged_profile_marker_mismatch_blocks_enable_without_partial_write(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    observation = _positive_observation()
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-profile-marker-mismatch",
        observation=observation,
    )
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_id = _target_id(provider_id, "alpha")
    registry = RoutingLedger(state)
    service = _task5b_service(registry)
    service.import_observation(
        _import_request(
            observation,
            completed,
            profile_revision=0,
            target_revisions=((target_id, 0),),
        ),
        authenticated_owner_principal=OWNER,
    )
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    forged_profile_metadata = json.loads(json.dumps(profile.profile.metadata))
    forged_profile_metadata["lan_discovery"]["runtime_hardening"] = "kestrel.lan.runtime.openai.v0"
    with state._connect() as connection:
        connection.execute(
            "UPDATE routing_provider_profiles SET metadata_json = ? WHERE profile_id = ?",
            (
                json.dumps(
                    forged_profile_metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                provider_id,
            ),
        )
    forged_profile = registry.get_provider_profile(provider_id)
    assert forged_profile is not None
    request, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=forged_profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
        enabled=True,
    )

    with pytest.raises(LanDiscoveryConflict, match="hardening|binding"):
        service.review_lan_target(request, authenticated_owner_principal=OWNER)
    assert registry.get_model_target(target_id) == target
    assert registry.get_provider_profile(provider_id) == forged_profile


def test_multi_target_positive_reimport_installs_one_marker_atomically_everywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry
    from nested_memvid_agent.lan_runtime_authority import (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    )

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        observation,
        _scan,
        registry,
        _service,
        _result,
        provider_id,
        target_ids,
    ) = _import_first_positive(
        state,
        scan_id="scan-multi-before-marker",
        models=("alpha", "beta"),
    )
    before_profile = registry.get_provider_profile(provider_id)
    before_targets = tuple(registry.get_model_target(item) for item in target_ids)
    assert before_profile is not None and all(item is not None for item in before_targets)
    _row, completed = _persist_completed_scan(
        state,
        scan_id="scan-multi-after-marker",
        observation=observation,
        observed_at=NOW + timedelta(seconds=1),
    )
    service = _task5b_service(
        registry,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    request = _import_request(
        observation,
        completed,
        profile_revision=before_profile.revision,
        target_revisions=tuple(
            (target_id, entry.revision)
            for target_id, entry in zip(target_ids, before_targets, strict=True)
            if entry is not None
        ),
    )
    monkeypatch.setattr(
        ledger_registry,
        "_before_lan_commit",
        lambda: (_ for _ in ()).throw(RuntimeError("multi-marker commit crash")),
    )
    with pytest.raises(RuntimeError, match="multi-marker commit crash"):
        service.import_observation(request, authenticated_owner_principal=OWNER)
    assert registry.get_provider_profile(provider_id) == before_profile
    assert tuple(registry.get_model_target(item) for item in target_ids) == before_targets

    monkeypatch.setattr(ledger_registry, "_before_lan_commit", lambda: None)
    result = service.import_observation(request, authenticated_owner_principal=OWNER)

    assert result.profile is not None
    assert result.profile.profile.metadata["lan_discovery"]["runtime_hardening"] == (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION
    )
    assert tuple(item.target.target_id for item in result.targets) == target_ids
    assert all(
        item.target.metadata["lan_discovery"]["runtime_hardening"]
        == LAN_OPENAI_RUNTIME_HARDENING_VERSION
        for item in result.targets
    )
    assert result.profile.revision == before_profile.revision + 1
    assert tuple(item.revision for item in result.targets) == tuple(
        entry.revision + 1 for entry in before_targets if entry is not None
    )

    beta_id = next(item.target.target_id for item in result.targets if item.target.model == "beta")
    beta = registry.get_model_target(beta_id)
    current_profile = registry.get_provider_profile(provider_id)
    assert beta is not None and current_profile is not None
    before_beta_review = (
        current_profile,
        tuple(registry.get_model_target(item) for item in target_ids),
    )
    enable_beta, _privacy, _material = _exact_review_request(
        owner=OWNER,
        profile_revision=current_profile.revision,
        target_revision=beta.revision,
        target_id=beta_id,
        protected=beta.target.metadata["lan_discovery"],
        enabled=True,
    )
    with pytest.raises(LanDiscoveryConflict, match="capability|generation"):
        service.review_lan_target(enable_beta, authenticated_owner_principal=OWNER)
    assert registry.get_provider_profile(provider_id) == before_beta_review[0]
    assert tuple(registry.get_model_target(item) for item in target_ids) == before_beta_review[1]


def test_server_owned_import_preview_derives_authority_without_mutation(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scan_id = "lan_" + "1" * 32
    observation = _positive_observation(models=("alpha", "beta"))
    _row, completed = _persist_completed_scan(
        state,
        scan_id=scan_id,
        observation=observation,
    )
    assert completed.terminal_receipt_digest is not None
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    before = _routing_inventory_snapshot(state)
    provider_id = _provider_id(observation.endpoint_binding_digest)
    target_ids = (
        _target_id(provider_id, "alpha"),
        _target_id(provider_id, "beta"),
    )

    selector = lan_discovery_service_module.LanImportSelector(
        scan_id=scan_id,
        endpoint_id=observation.endpoint_binding_digest,
    )
    preview = service.prepare_lan_import(
        selector,
        authenticated_owner_principal=OWNER,
    )

    assert _routing_inventory_snapshot(state) == before
    assert preview.selector == selector
    assert preview.preview_digest.startswith("sha256:")
    assert preview.evidence_expires_at == "2026-08-01T12:05:00Z"
    assert preview.requires_confirmation is True
    assert preview.authority.expected_terminal_receipt_digest == (
        completed.terminal_receipt_digest
    )
    assert preview.authority.expected_observation_digest == observation.observation_digest
    assert preview.authority.expected_profile_revision == 0
    assert tuple(
        (item.resource_id, item.revision)
        for item in preview.authority.expected_target_revisions
    ) == tuple((target_id, 0) for target_id in target_ids)
    assert preview.authority.endpoint_fingerprint is not None
    assert preview.authority.replacement is None
    assert preview.result.profile is not None
    assert preview.result.profile.profile.profile_id == provider_id
    assert preview.result.profile.revision == 1
    assert tuple(item.target.target_id for item in preview.result.targets) == target_ids
    assert all(item.revision == 1 for item in preview.result.targets)
    assert all(item.target.enabled is False for item in preview.result.targets)
    assert all(item.target.trust_class == "unconfirmed" for item in preview.result.targets)


def test_server_owned_import_confirmation_commits_exact_preview_once(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scan_id = "lan_" + "2" * 32
    observation = _positive_observation(models=("alpha", "beta"))
    _persist_completed_scan(state, scan_id=scan_id, observation=observation)
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    selector = lan_discovery_service_module.LanImportSelector(
        scan_id=scan_id,
        endpoint_id=observation.endpoint_binding_digest,
    )
    preview = service.prepare_lan_import(
        selector,
        authenticated_owner_principal=OWNER,
    )

    confirmed = service.confirm_lan_import(
        lan_discovery_service_module.LanImportConfirmation(
            selector=selector,
            preview_digest=preview.preview_digest,
            confirmed=True,
        ),
        authenticated_owner_principal=OWNER,
    )

    assert confirmed.preview_digest == preview.preview_digest
    assert confirmed.result == preview.result
    assert confirmed.result.profile is not None
    assert registry.get_provider_profile(
        confirmed.result.profile.profile.profile_id
    ) == confirmed.result.profile
    assert tuple(
        registry.get_model_target(entry.target.target_id)
        for entry in confirmed.result.targets
    ) == confirmed.result.targets
    committed = _routing_inventory_snapshot(state)

    with pytest.raises(LanDiscoveryConflict, match="preview_conflict"):
        service.confirm_lan_import(
            lan_discovery_service_module.LanImportConfirmation(
                selector=selector,
                preview_digest=preview.preview_digest,
                confirmed=True,
            ),
            authenticated_owner_principal=OWNER,
        )
    assert _routing_inventory_snapshot(state) == committed


def test_server_owned_import_confirmation_rejects_tampered_digest_without_mutation(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scan_id = "lan_" + "3" * 32
    observation = _positive_observation()
    _persist_completed_scan(state, scan_id=scan_id, observation=observation)
    service = LanDiscoveryService(RoutingLedger(state), clock=lambda: NOW)
    selector = lan_discovery_service_module.LanImportSelector(
        scan_id=scan_id,
        endpoint_id=observation.endpoint_binding_digest,
    )
    before = _routing_inventory_snapshot(state)

    with pytest.raises(LanDiscoveryConflict, match="preview_conflict"):
        service.confirm_lan_import(
            lan_discovery_service_module.LanImportConfirmation(
                selector=selector,
                preview_digest="sha256:" + "0" * 64,
                confirmed=True,
            ),
            authenticated_owner_principal=OWNER,
        )

    assert _routing_inventory_snapshot(state) == before


def test_server_owned_review_preview_and_confirmation_are_exact_and_atomic(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scan_id = "lan_" + "4" * 32
    observation = _positive_observation()
    _persist_completed_scan(state, scan_id=scan_id, observation=observation)
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    selector = lan_discovery_service_module.LanImportSelector(
        scan_id=scan_id,
        endpoint_id=observation.endpoint_binding_digest,
    )
    imported = service.confirm_lan_import(
        lan_discovery_service_module.LanImportConfirmation(
            selector=selector,
            preview_digest=service.prepare_lan_import(
                selector,
                authenticated_owner_principal=OWNER,
            ).preview_digest,
            confirmed=True,
        ),
        authenticated_owner_principal=OWNER,
    ).result
    target = imported.targets[0]
    options = lan_discovery_service_module.LanReviewOptions(
        target_id=target.target.target_id,
        intended_roles=("chat",),
        task_family_affinities=("general",),
        enabled=False,
    )
    before = _routing_inventory_snapshot(state)

    preview = service.prepare_lan_review(
        options,
        authenticated_owner_principal=OWNER,
    )

    assert _routing_inventory_snapshot(state) == before
    assert preview.options == options
    assert preview.evidence_expires_at == "2026-08-01T12:05:00Z"
    assert preview.authority.provider_profile_id == target.target.provider_profile_id
    assert preview.authority.expected_profile_revision == 1
    assert preview.authority.expected_target_revision == 1
    assert preview.authority.trust_class == "operator_confirmed"
    assert preview.authority.reviewed_runtime_interface_binding_digest is None
    assert preview.profile.revision == 2
    assert preview.target.revision == 2
    assert preview.target.target.enabled is False
    assert preview.target.target.trust_class == "operator_confirmed"

    confirmed = service.confirm_lan_review(
        lan_discovery_service_module.LanReviewConfirmation(
            options=options,
            preview_digest=preview.preview_digest,
            privacy_acknowledged=True,
            confirmed=True,
        ),
        authenticated_owner_principal=OWNER,
    )

    assert confirmed.preview_digest == preview.preview_digest
    assert confirmed.result.profile == preview.profile
    assert confirmed.result.target == preview.target
    assert confirmed.result.material_binding_digest == (
        preview.authority.reviewed_material_binding_digest
    )
    assert confirmed.result.privacy_acknowledgement_digest == (
        preview.authority.privacy_acknowledgement_digest
    )
    assert registry.get_provider_profile(
        confirmed.result.profile.profile.profile_id
    ) == confirmed.result.profile
    assert registry.get_model_target(options.target_id) == confirmed.result.target
    committed = _routing_inventory_snapshot(state)
    with pytest.raises(LanDiscoveryConflict, match="preview_conflict"):
        service.confirm_lan_review(
            lan_discovery_service_module.LanReviewConfirmation(
                options=options,
                preview_digest=preview.preview_digest,
                privacy_acknowledged=True,
                confirmed=True,
            ),
            authenticated_owner_principal=OWNER,
        )
    assert _routing_inventory_snapshot(state) == committed


def test_server_owned_import_derives_replacement_family_and_stales_it_atomically(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _old_observation,
        _old_scan,
        registry,
        _service,
        _old_result,
        old_provider_id,
        (old_target_id,),
    ) = _import_first_positive(state)
    old_profile = registry.get_provider_profile(old_provider_id)
    old_target = registry.get_model_target(old_target_id)
    assert old_profile is not None and old_target is not None
    changed = _positive_observation(address="192.168.50.3")
    scan_id = "lan_" + "5" * 32
    _persist_completed_scan(state, scan_id=scan_id, observation=changed)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    selector = lan_discovery_service_module.LanImportSelector(
        scan_id=scan_id,
        endpoint_id=changed.endpoint_binding_digest,
        replacement_provider_profile_id=old_provider_id,
    )
    before = _routing_inventory_snapshot(state)

    preview = service.prepare_lan_import(
        selector,
        authenticated_owner_principal=OWNER,
    )

    assert _routing_inventory_snapshot(state) == before
    assert preview.authority.replacement is not None
    assert preview.authority.replacement.provider_profile_id == old_provider_id
    assert preview.authority.replacement.expected_profile_revision == old_profile.revision
    assert preview.authority.replacement.expected_material_binding_digests == (
        old_target.target.metadata["lan_discovery"]["material_binding_digest"],
    )
    assert old_target_id in preview.result.affected_target_ids
    assert preview.result.stale_reasons_by_target == (
        (old_target_id, ("address_changed",)),
    )

    confirmed = service.confirm_lan_import(
        lan_discovery_service_module.LanImportConfirmation(
            selector=selector,
            preview_digest=preview.preview_digest,
            confirmed=True,
        ),
        authenticated_owner_principal=OWNER,
    )

    assert confirmed.result == preview.result
    stale_old = registry.get_model_target(old_target_id)
    assert stale_old is not None
    assert stale_old.target.enabled is False
    assert stale_old.target.metadata["lan_discovery"]["stale_reasons"] == [
        "address_changed"
    ]


def test_server_owned_uncorrelated_outage_preview_and_confirmation_write_nothing(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    scan_id = "lan_" + "6" * 32
    observation = _outage_observation()
    _persist_completed_scan(state, scan_id=scan_id, observation=observation)
    registry = RoutingLedger(state)
    service = LanDiscoveryService(registry, clock=lambda: NOW)
    selector = lan_discovery_service_module.LanImportSelector(
        scan_id=scan_id,
        endpoint_id=observation.endpoint_binding_digest,
    )
    before = _routing_inventory_snapshot(state)

    preview = service.prepare_lan_import(
        selector,
        authenticated_owner_principal=OWNER,
    )
    confirmed = service.confirm_lan_import(
        lan_discovery_service_module.LanImportConfirmation(
            selector=selector,
            preview_digest=preview.preview_digest,
            confirmed=True,
        ),
        authenticated_owner_principal=OWNER,
    )

    assert preview.authority.expected_profile_revision == 0
    assert preview.authority.expected_target_revisions == ()
    assert preview.authority.endpoint_fingerprint is None
    assert preview.result.outage_observed is True
    assert preview.result.profile is None
    assert preview.result.targets == ()
    assert confirmed.result == preview.result
    assert _routing_inventory_snapshot(state) == before
