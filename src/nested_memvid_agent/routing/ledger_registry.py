from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..lan_discovery_models import (
    KNOWN_MODEL_SERVICE_PORTS,
    MAX_ACTIVE_HOSTS,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from ..lan_discovery_scope import PrivateScanScope
from ..lan_http_transport import (
    AuthenticatedLanSource,
    CurrentLanInterfaceInventory,
    InterfaceInventoryResolver,
    LanProbeModel,
    _resolve_current_interface_inventory,
    authenticate_lan_source,
)
from ..lan_runtime_authority import (
    LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    LanRuntimeAuthority,
    derive_lan_runtime_interface_binding_digest,
)
from ..lan_scanner import (
    ApiShape,
    CapabilityName,
    CapabilityObservationStatus,
    CapabilityProvenance,
    LanCapabilityEvidence,
    LanEndpointObservation,
    Reachability,
    TransportSecurity,
)
from ..state_store import AgentStateStore, utc_now
from .lan_serialization import (
    LAN_OBSERVATION_MAX_AGE_SECONDS,
    LAN_OBSERVATION_MAX_FUTURE_SKEW_SECONDS,
    AuthenticatedLanObservation,
    canonical_json,
    load_authenticated_task4_observation,
    normalize_address,
    normalize_network,
    sha256_digest,
    validate_digest,
)
from .ledger_records import (
    ModelTargetEntry,
    ProviderProfileEntry,
    RoutePolicyEntry,
    RoutingRevisionConflict,
)
from .ledger_schema import ensure_routing_schema
from .ledger_serialization import (
    _json,
    _next_revision,
    _policy_entry_from_row,
    _profile_entry_from_row,
    _target_entry_from_row,
    _target_values,
    _validate_base_url,
    _validate_metadata,
    _validate_secret_ref,
)
from .models import ModelTarget, ProviderProfile, RoutePolicy

_LAN_PROFILE_ID_RE = re.compile(r"lan-provider-[0-9a-f]{64}\Z")
_LAN_TARGET_ID_RE = re.compile(r"lan-target-[0-9a-f]{64}\Z")
_LAN_PROFILE_ID_PREFIX = "lan-provider-"
_LAN_TARGET_ID_PREFIX = "lan-target-"
_LAN_PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
    )
)
_LAN_PRIVATE_IPV6_NETWORKS = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)
_LAN_PROFILE_SCHEMA = "kestrel.lan.provider-profile.v1"
_LAN_TARGET_SCHEMA = "kestrel.lan.model-target-binding.v1"
_LAN_STALE_REASON_ORDER = (
    "interface_changed",
    "network_changed",
    "address_changed",
    "port_changed",
    "transport_security_changed",
    "certificate_changed",
    "api_shape_changed",
    "catalog_changed",
    "model_identity_changed",
    "model_missing",
    "capability_changed",
    "freshness_expired",
)
_LAN_ENDPOINT_STALE_REASONS = frozenset(
    {
        "interface_changed",
        "network_changed",
        "address_changed",
        "port_changed",
        "transport_security_changed",
        "certificate_changed",
        "api_shape_changed",
        "freshness_expired",
    }
)
_LAN_PROFILE_PROTECTED_KEYS = frozenset(
    {
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
        "runtime_adapter",
        "runtime_hardening",
        "stale_reason",
        "stale_reasons",
        "stale_transition_terminal_receipt_digest",
    }
)
_LAN_TARGET_PROTECTED_KEYS = frozenset(
    {
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
)


class RoutingRegistry:
    """Durable Adaptive Flock provider, target, and policy inventory.

    The routing schema is module-owned and additive inside Kestrel's existing
    SQLite control-plane database. Raw secret values are never accepted: a
    provider profile may reference only an opaque ``secret://`` broker handle.
    """

    def __init__(self, state: AgentStateStore) -> None:
        self.state = state
        ensure_routing_schema(self.state)

    def schema_version(self) -> int:
        with self.state._connect() as conn:
            row = conn.execute("SELECT version FROM routing_schema_version WHERE id = 1").fetchone()
        return 0 if row is None else int(row["version"])

    def put_provider_profile(
        self,
        profile: ProviderProfile,
        *,
        expected_revision: int | None = None,
    ) -> ProviderProfileEntry:
        _reject_generic_lan_profile(profile)
        _validate_secret_ref(profile.secret_ref)
        _validate_base_url(profile.base_url)
        _validate_metadata(profile.metadata)
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile.profile_id,),
            ).fetchone()
            if row is not None and _row_is_lan_managed(row, profile=True):
                raise ValueError("lan managed profile requires the specialized LAN service")
            revision, created_at = _next_revision(
                "provider_profile",
                profile.profile_id,
                row,
                expected_revision=expected_revision,
                now=now,
            )
            conn.execute(
                """
                INSERT INTO routing_provider_profiles (
                    profile_id, display_name, adapter, base_url, secret_ref, enabled,
                    locality, trust_class, max_concurrency, metadata_json, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    adapter = excluded.adapter,
                    base_url = excluded.base_url,
                    secret_ref = excluded.secret_ref,
                    enabled = excluded.enabled,
                    locality = excluded.locality,
                    trust_class = excluded.trust_class,
                    max_concurrency = excluded.max_concurrency,
                    metadata_json = excluded.metadata_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.profile_id,
                    profile.display_name,
                    profile.adapter,
                    profile.base_url,
                    profile.secret_ref,
                    1 if profile.enabled else 0,
                    profile.locality,
                    profile.trust_class,
                    profile.max_concurrency,
                    _json(profile.metadata),
                    revision,
                    created_at,
                    now,
                ),
            )
            persisted = conn.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile.profile_id,),
            ).fetchone()
        if persisted is None:
            raise RuntimeError("provider_profile_write_lost")
        return _profile_entry_from_row(persisted)

    def get_provider_profile(self, profile_id: str) -> ProviderProfileEntry | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        return None if row is None else _profile_entry_from_row(row)

    def list_provider_profiles(self, *, enabled_only: bool = False) -> list[ProviderProfileEntry]:
        sql = "SELECT * FROM routing_provider_profiles"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY profile_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_profile_entry_from_row(row) for row in rows]

    def put_model_target(
        self,
        target: ModelTarget,
        *,
        expected_revision: int | None = None,
    ) -> ModelTargetEntry:
        _reject_generic_lan_target(target)
        _validate_metadata(target.metadata)
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            profile_row = conn.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (target.provider_profile_id,),
            ).fetchone()
            if profile_row is None:
                raise ValueError(f"unknown provider profile: {target.provider_profile_id}")
            if _row_is_lan_managed(profile_row, profile=True):
                raise ValueError("lan managed profile requires the specialized LAN service")
            profile = _profile_entry_from_row(profile_row).profile
            if target.provider != profile.adapter:
                raise ValueError("target provider does not match provider profile adapter")
            if profile.locality != "hybrid" and target.locality != profile.locality:
                raise ValueError("target locality does not match provider profile locality")
            row = conn.execute(
                "SELECT * FROM routing_model_targets WHERE target_id = ?",
                (target.target_id,),
            ).fetchone()
            if row is not None and _row_is_lan_managed(row, profile=False):
                raise ValueError("lan managed target requires the specialized LAN service")
            revision, created_at = _next_revision(
                "model_target",
                target.target_id,
                row,
                expected_revision=expected_revision,
                now=now,
            )
            conn.execute(
                """
                INSERT INTO routing_model_targets (
                    target_id, provider_profile_id, provider, model, enabled, locality,
                    trust_class, capability_tags_json, role_affinities_json,
                    task_family_affinities_json, max_context_tokens, supports_tools,
                    supports_json, supports_vision, supports_reasoning, supports_streaming,
                    quality_tier, latency_tier, operator_priority, estimated_cost_usd,
                    input_cost_per_million_usd, output_cost_per_million_usd, health,
                    recent_failure_rate, predicted_success, metadata_json, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    provider_profile_id = excluded.provider_profile_id,
                    provider = excluded.provider,
                    model = excluded.model,
                    enabled = excluded.enabled,
                    locality = excluded.locality,
                    trust_class = excluded.trust_class,
                    capability_tags_json = excluded.capability_tags_json,
                    role_affinities_json = excluded.role_affinities_json,
                    task_family_affinities_json = excluded.task_family_affinities_json,
                    max_context_tokens = excluded.max_context_tokens,
                    supports_tools = excluded.supports_tools,
                    supports_json = excluded.supports_json,
                    supports_vision = excluded.supports_vision,
                    supports_reasoning = excluded.supports_reasoning,
                    supports_streaming = excluded.supports_streaming,
                    quality_tier = excluded.quality_tier,
                    latency_tier = excluded.latency_tier,
                    operator_priority = excluded.operator_priority,
                    estimated_cost_usd = excluded.estimated_cost_usd,
                    input_cost_per_million_usd = excluded.input_cost_per_million_usd,
                    output_cost_per_million_usd = excluded.output_cost_per_million_usd,
                    health = excluded.health,
                    recent_failure_rate = excluded.recent_failure_rate,
                    predicted_success = excluded.predicted_success,
                    metadata_json = excluded.metadata_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                _target_values(target, revision=revision, created_at=created_at, updated_at=now),
            )
            persisted = conn.execute(
                "SELECT * FROM routing_model_targets WHERE target_id = ?",
                (target.target_id,),
            ).fetchone()
        if persisted is None:
            raise RuntimeError("model_target_write_lost")
        return _target_entry_from_row(persisted)

    def get_model_target(self, target_id: str) -> ModelTargetEntry | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_model_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
        if row is None:
            return None
        entry = _target_entry_from_row(row)
        if _row_has_exact_legacy_runtime_binding_shape(entry):
            return _strict_lan_target_entry(row)
        return entry

    def list_model_targets(self, *, enabled_only: bool = False) -> list[ModelTargetEntry]:
        sql = "SELECT * FROM routing_model_targets"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY target_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql).fetchall()
        entries: list[ModelTargetEntry] = []
        for row in rows:
            entry = _target_entry_from_row(row)
            entries.append(
                _strict_lan_target_entry(row)
                if _row_has_exact_legacy_runtime_binding_shape(entry)
                else entry
            )
        return entries

    def apply_provider_inventory(
        self,
        profile: ProviderProfile,
        *,
        expected_profile_revision: int,
        target_updates: tuple[tuple[ModelTarget, int], ...],
    ) -> tuple[ProviderProfileEntry, tuple[ModelTargetEntry, ...]]:
        """Atomically publish one catalog refresh and all derived target changes."""

        _reject_generic_lan_profile(profile)
        _validate_exact_expected_revision(expected_profile_revision)
        _validate_secret_ref(profile.secret_ref)
        _validate_base_url(profile.base_url)
        _validate_metadata(profile.metadata)
        target_ids = [target.target_id for target, _revision in target_updates]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("provider inventory target updates must be unique")
        for target, expected_revision in target_updates:
            _reject_generic_lan_target(target)
            _validate_exact_expected_revision(expected_revision)
            _validate_metadata(target.metadata)
            if target.provider_profile_id != profile.profile_id:
                raise ValueError("inventory target does not belong to provider profile")
            if target.provider != profile.adapter:
                raise ValueError("target provider does not match provider profile adapter")
            if profile.locality != "hybrid" and target.locality != profile.locality:
                raise ValueError("target locality does not match provider profile locality")

        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            profile_row = conn.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile.profile_id,),
            ).fetchone()
            if profile_row is not None and _row_is_lan_managed(
                profile_row,
                profile=True,
            ):
                raise ValueError("lan managed profile requires the specialized LAN service")
            profile_revision, profile_created_at = _next_revision(
                "provider_profile",
                profile.profile_id,
                profile_row,
                expected_revision=expected_profile_revision,
                now=now,
            )
            planned_targets: list[tuple[ModelTarget, int, str]] = []
            for target, expected_revision in target_updates:
                target_row = conn.execute(
                    "SELECT * FROM routing_model_targets WHERE target_id = ?",
                    (target.target_id,),
                ).fetchone()
                if target_row is not None and _row_is_lan_managed(
                    target_row,
                    profile=False,
                ):
                    raise ValueError("lan managed target requires the specialized LAN service")
                target_revision, target_created_at = _next_revision(
                    "model_target",
                    target.target_id,
                    target_row,
                    expected_revision=expected_revision,
                    now=now,
                )
                planned_targets.append((target, target_revision, target_created_at))

            conn.execute(
                """
                INSERT INTO routing_provider_profiles (
                    profile_id, display_name, adapter, base_url, secret_ref, enabled,
                    locality, trust_class, max_concurrency, metadata_json, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    adapter = excluded.adapter,
                    base_url = excluded.base_url,
                    secret_ref = excluded.secret_ref,
                    enabled = excluded.enabled,
                    locality = excluded.locality,
                    trust_class = excluded.trust_class,
                    max_concurrency = excluded.max_concurrency,
                    metadata_json = excluded.metadata_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.profile_id,
                    profile.display_name,
                    profile.adapter,
                    profile.base_url,
                    profile.secret_ref,
                    1 if profile.enabled else 0,
                    profile.locality,
                    profile.trust_class,
                    profile.max_concurrency,
                    _json(profile.metadata),
                    profile_revision,
                    profile_created_at,
                    now,
                ),
            )
            for target, target_revision, target_created_at in planned_targets:
                conn.execute(
                    """
                    INSERT INTO routing_model_targets (
                        target_id, provider_profile_id, provider, model, enabled, locality,
                        trust_class, capability_tags_json, role_affinities_json,
                        task_family_affinities_json, max_context_tokens, supports_tools,
                        supports_json, supports_vision, supports_reasoning, supports_streaming,
                        quality_tier, latency_tier, operator_priority, estimated_cost_usd,
                        input_cost_per_million_usd, output_cost_per_million_usd, health,
                        recent_failure_rate, predicted_success, metadata_json, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(target_id) DO UPDATE SET
                        provider_profile_id = excluded.provider_profile_id,
                        provider = excluded.provider,
                        model = excluded.model,
                        enabled = excluded.enabled,
                        locality = excluded.locality,
                        trust_class = excluded.trust_class,
                        capability_tags_json = excluded.capability_tags_json,
                        role_affinities_json = excluded.role_affinities_json,
                        task_family_affinities_json = excluded.task_family_affinities_json,
                        max_context_tokens = excluded.max_context_tokens,
                        supports_tools = excluded.supports_tools,
                        supports_json = excluded.supports_json,
                        supports_vision = excluded.supports_vision,
                        supports_reasoning = excluded.supports_reasoning,
                        supports_streaming = excluded.supports_streaming,
                        quality_tier = excluded.quality_tier,
                        latency_tier = excluded.latency_tier,
                        operator_priority = excluded.operator_priority,
                        estimated_cost_usd = excluded.estimated_cost_usd,
                        input_cost_per_million_usd = excluded.input_cost_per_million_usd,
                        output_cost_per_million_usd = excluded.output_cost_per_million_usd,
                        health = excluded.health,
                        recent_failure_rate = excluded.recent_failure_rate,
                        predicted_success = excluded.predicted_success,
                        metadata_json = excluded.metadata_json,
                        revision = excluded.revision,
                        updated_at = excluded.updated_at
                    """,
                    _target_values(
                        target,
                        revision=target_revision,
                        created_at=target_created_at,
                        updated_at=now,
                    ),
                )
            _before_inventory_commit()
            persisted_profile = conn.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile.profile_id,),
            ).fetchone()
            persisted_targets = [
                conn.execute(
                    "SELECT * FROM routing_model_targets WHERE target_id = ?",
                    (target.target_id,),
                ).fetchone()
                for target, _revision in target_updates
            ]

        if persisted_profile is None:
            raise RuntimeError("provider_inventory_profile_write_lost")
        if any(row is None for row in persisted_targets):
            raise RuntimeError("provider_inventory_target_write_lost")
        return (
            _profile_entry_from_row(persisted_profile),
            tuple(_target_entry_from_row(row) for row in persisted_targets if row is not None),
        )

    def resolve_lan_runtime_authority(
        self,
        target_id: str,
        *,
        clock: Callable[[], datetime] | None = None,
        interface_inventory_resolver: Callable[[], CurrentLanInterfaceInventory]
        | None = None,
    ) -> LanRuntimeAuthority:
        """Resolve one enabled target from a coherent durable read and fresh interface state."""

        if type(target_id) is not str or _LAN_TARGET_ID_RE.fullmatch(target_id) is None:
            raise ValueError("LAN runtime target identifier is invalid")
        runtime_clock = clock or (lambda: datetime.now(UTC))
        try:
            now = runtime_clock()
        except Exception:
            raise ValueError("LAN runtime clock failed") from None
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("LAN runtime clock must be aware UTC")
        now = now.astimezone(UTC)

        with self.state._connect() as connection:
            connection.execute("BEGIN")
            target_row = connection.execute(
                "SELECT * FROM routing_model_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
            if target_row is None:
                raise ValueError("LAN runtime target is unavailable")
            target_entry = _strict_lan_target_entry(target_row)
            profile_id = target_entry.target.provider_profile_id
            profile_row = connection.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if profile_row is None:
                raise ValueError("LAN runtime provider is unavailable")
            profile_entry = _strict_lan_profile_entry(profile_row)
            profile_protected = profile_entry.profile.metadata["lan_discovery"]
            target_protected = target_entry.target.metadata["lan_discovery"]

            if (
                not profile_entry.profile.enabled
                or not target_entry.target.enabled
                or not target_protected["reviewed"]
                or profile_protected["stale_reasons"]
                or target_protected["stale_reasons"]
                or _validated_runtime_hardening_marker(profile_protected)
                != LAN_OPENAI_RUNTIME_HARDENING_VERSION
                or _validated_runtime_hardening_marker(target_protected)
                != LAN_OPENAI_RUNTIME_HARDENING_VERSION
                or profile_entry.profile.adapter != "lan-openai-compatible"
                or target_entry.target.provider != "lan-openai-compatible"
                or target_protected["api_shape"] != ApiShape.OPENAI_COMPATIBLE.value
            ):
                raise ValueError("LAN runtime authority is unavailable")

            binding_fields = (
                "owner_principal",
                "endpoint_binding_digest",
                "endpoint_fingerprint",
                "interface_id",
                "confirmed_network",
                "address",
                "port",
                "transport_security",
                "certificate_sha256",
                "api_shape",
                "runtime_adapter",
                "runtime_hardening",
            )
            if any(
                profile_protected[field] != target_protected[field]
                for field in binding_fields
            ):
                raise ValueError("LAN runtime profile and target bindings disagree")

            profile_evidence = load_authenticated_task4_observation(
                connection,
                scan_id=profile_protected["scan_id"],
                endpoint_binding_digest=profile_protected["endpoint_binding_digest"],
                expected_terminal_receipt_digest=profile_protected["terminal_receipt_digest"],
                expected_observation_digest=profile_protected["observation_digest"],
                authenticated_owner_principal=profile_protected["owner_principal"],
            )
            target_evidence = load_authenticated_task4_observation(
                connection,
                scan_id=target_protected["scan_id"],
                endpoint_binding_digest=target_protected["endpoint_binding_digest"],
                expected_terminal_receipt_digest=target_protected["terminal_receipt_digest"],
                expected_observation_digest=target_protected["observation_digest"],
                authenticated_owner_principal=target_protected["owner_principal"],
            )
            _validate_lan_review_evidence_projection(
                profile_protected,
                profile_evidence,
                now=now,
            )
            _validate_lan_review_evidence_projection(
                target_protected,
                target_evidence,
                now=now,
            )
            if (
                target_entry.target.model
                != target_evidence.observation.selected_model_id
                or target_protected["capability_claims"]
                != _lan_capability_claims(
                    target_evidence,
                    model_id=target_entry.target.model,
                )
            ):
                raise ValueError("LAN runtime target evidence projection is invalid")

            review_receipt = target_protected[
                "review_evidence_terminal_receipt_digest"
            ]
            review_observation = target_protected["review_evidence_observation_digest"]
            review_scan_rows = connection.execute(
                """
                SELECT scan_id FROM routing_lan_scans
                WHERE terminal_receipt_digest = ? ORDER BY scan_id ASC
                """,
                (review_receipt,),
            ).fetchall()
            if len(review_scan_rows) != 1:
                raise ValueError("LAN runtime review evidence is ambiguous")
            review_evidence = load_authenticated_task4_observation(
                connection,
                scan_id=str(review_scan_rows[0]["scan_id"]),
                endpoint_binding_digest=target_protected["endpoint_binding_digest"],
                expected_terminal_receipt_digest=review_receipt,
                expected_observation_digest=review_observation,
                authenticated_owner_principal=target_protected["owner_principal"],
            )
            if (
                target_entry.target.model != review_evidence.observation.selected_model_id
                or review_evidence.observation.capabilities[0].status
                is not CapabilityObservationStatus.OBSERVED_PASS
                or review_evidence.observation.capabilities[0].supported is not True
            ):
                raise ValueError("LAN runtime review evidence is not generation-capable")

            profile_fresh_until = _parse_lan_timestamp(
                profile_protected["fresh_until"],
                "fresh_until",
            )
            target_fresh_until = _parse_lan_timestamp(
                target_protected["fresh_until"],
                "fresh_until",
            )
            fresh_until = min(profile_fresh_until, target_fresh_until)
            if fresh_until <= now:
                raise ValueError("LAN runtime authority evidence expired")

        scope, endpoint, source = _authenticate_lan_runtime_source(
            target_protected,
            interface_inventory_resolver=interface_inventory_resolver,
        )
        runtime_interface_binding = _lan_reviewed_runtime_interface_binding_digest(
            target_protected,
            source=source,
        )
        if runtime_interface_binding != target_protected[
            "reviewed_runtime_interface_binding_digest"
        ]:
            raise ValueError("LAN runtime interface binding changed")

        return LanRuntimeAuthority(
            scope=scope,
            endpoint=endpoint,
            source_address=source.source_address,
            os_interface_identity=source.os_identity,
            interface_index=source.interface_index,
            provider_profile_id=profile_id,
            reviewed_target_id=target_id,
            model_id=target_entry.target.model,
            api_shape=target_protected["api_shape"],
            runtime_adapter=target_protected["runtime_adapter"],
            runtime_hardening_version=target_protected["runtime_hardening"],
            endpoint_binding_digest=target_protected["endpoint_binding_digest"],
            endpoint_fingerprint=target_protected["endpoint_fingerprint"],
            reviewed_material_binding_digest=target_protected[
                "reviewed_material_binding_digest"
            ],
            review_digest=target_protected["review_digest"],
            fresh_until=_lan_timestamp(fresh_until),
        )

    def apply_lan_import(
        self,
        *,
        scan_id: str,
        endpoint_binding_digest: str,
        expected_terminal_receipt_digest: str,
        expected_observation_digest: str,
        expected_profile_revision: int,
        expected_target_revisions: tuple[tuple[str, int], ...],
        replacement: tuple[str, int, str, tuple[str, ...]] | None,
        authenticated_owner_principal: str,
        now: datetime,
        runtime_hardening_version: str | None = None,
    ) -> tuple[
        ProviderProfileEntry | None,
        tuple[ModelTargetEntry, ...],
        str,
        str | None,
        bool,
        tuple[str, ...],
        tuple[str, ...],
        tuple[tuple[str, tuple[str, ...]], ...],
    ]:
        """Atomically import one receipt-bound LAN observation as disabled drafts."""

        if (
            type(scan_id) is not str
            or not scan_id
            or scan_id != scan_id.strip()
            or len(scan_id) > 128
            or unicodedata.normalize("NFC", scan_id) != scan_id
            or any(unicodedata.category(character).startswith("C") for character in scan_id)
        ):
            raise ValueError("LAN import scan identifier is invalid")
        for field, value in (
            ("endpoint_binding_digest", endpoint_binding_digest),
            ("expected_terminal_receipt_digest", expected_terminal_receipt_digest),
            ("expected_observation_digest", expected_observation_digest),
        ):
            validate_digest(value, field)
        _validate_canonical_lan_owner(authenticated_owner_principal)
        _validate_installed_runtime_hardening(runtime_hardening_version)
        now_text = _lan_timestamp(now)
        _validate_exact_expected_revision(expected_profile_revision)
        if type(expected_target_revisions) is not tuple:
            raise ValueError("LAN expected target revisions must be an exact tuple")
        for item in expected_target_revisions:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("LAN expected target revision entry is invalid")
            target_id, revision = item
            if type(target_id) is not str or _LAN_TARGET_ID_RE.fullmatch(target_id) is None:
                raise ValueError("LAN expected target identifier is invalid")
            _validate_exact_expected_revision(revision)
        if replacement is not None:
            if type(replacement) is not tuple or len(replacement) != 4:
                raise ValueError("LAN replacement confirmation is invalid")
            replacement_profile_id, replacement_revision, fingerprint, materials = replacement
            if (
                type(replacement_profile_id) is not str
                or _LAN_PROFILE_ID_RE.fullmatch(replacement_profile_id) is None
            ):
                raise ValueError("LAN replacement provider identifier is invalid")
            _validate_exact_expected_revision(replacement_revision)
            validate_digest(fingerprint, "expected_endpoint_fingerprint")
            if type(materials) is not tuple or not materials:
                raise ValueError("LAN replacement material binding set is invalid")
            for material in materials:
                validate_digest(material, "expected_material_binding_digest")
            if len(materials) != len(set(materials)):
                raise ValueError("LAN replacement material binding set is invalid")
        expected_map = dict(expected_target_revisions)
        if len(expected_map) != len(expected_target_revisions):
            raise ValueError("LAN expected target revisions must be unique")
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            evidence = load_authenticated_task4_observation(
                connection,
                scan_id=scan_id,
                endpoint_binding_digest=endpoint_binding_digest,
                expected_terminal_receipt_digest=expected_terminal_receipt_digest,
                expected_observation_digest=expected_observation_digest,
                authenticated_owner_principal=authenticated_owner_principal,
            )
            if evidence.observed_at < now - timedelta(
                seconds=LAN_OBSERVATION_MAX_AGE_SECONDS
            ) or evidence.observed_at > (
                now + timedelta(seconds=LAN_OBSERVATION_MAX_FUTURE_SKEW_SECONDS)
            ):
                raise ValueError("lan_evidence_freshness_conflict")
            observation = evidence.observation
            generation = observation.capabilities[0]
            positive = (
                observation.reachability is Reachability.REACHABLE
                and generation.status is CapabilityObservationStatus.OBSERVED_PASS
                and generation.supported is True
                and observation.failure_category is None
                and observation.api_shape is not None
                and bool(observation.catalog)
            )
            if not positive:
                if replacement is not None:
                    raise ValueError("LAN outage evidence cannot replace an endpoint")
                result = _apply_lan_outage(
                    connection,
                    evidence=evidence,
                    expected_profile_revision=expected_profile_revision,
                    expected_target_revisions=expected_map,
                    now=now,
                    now_text=now_text,
                )
                if result[5] or (
                    result[0] is not None and result[0].revision != expected_profile_revision
                ):
                    _before_lan_commit()
                return (
                    result[0],
                    result[1],
                    observation.observation_digest,
                    result[2],
                    True,
                    result[3],
                    result[4],
                    result[5],
                )

            profile_id = _lan_profile_id(observation.endpoint_binding_digest)
            endpoint_fingerprint = _lan_endpoint_fingerprint(evidence)
            positive_api_shape = observation.api_shape
            if positive_api_shape is None:
                raise ValueError("positive LAN evidence requires an API shape")
            runtime_adapter = _lan_runtime_adapter(positive_api_shape)
            installed_runtime_hardening = (
                runtime_hardening_version
                if positive_api_shape is ApiShape.OPENAI_COMPATIBLE
                else None
            )
            profile_row = connection.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            existing_profile = (
                None if profile_row is None else _strict_lan_profile_entry(profile_row)
            )
            if (
                existing_profile is not None
                and existing_profile.profile.metadata["lan_discovery"]["owner_principal"]
                != authenticated_owner_principal
            ):
                raise ValueError("LAN managed provider owner mismatch")
            profile_revision, profile_created_at = _next_revision(
                "provider_profile",
                profile_id,
                profile_row,
                expected_revision=expected_profile_revision,
                now=now_text,
            )
            existing_rows = connection.execute(
                """
                SELECT * FROM routing_model_targets
                WHERE provider_profile_id = ? ORDER BY target_id ASC
                """,
                (profile_id,),
            ).fetchall()
            existing_targets = {
                entry.target.target_id: entry
                for entry in (_strict_lan_target_entry(row) for row in existing_rows)
            }
            if any(
                entry.target.metadata["lan_discovery"]["owner_principal"]
                != authenticated_owner_principal
                for entry in existing_targets.values()
            ):
                raise ValueError("LAN managed target owner mismatch")
            previous_profile_protected = (
                None
                if existing_profile is None
                else existing_profile.profile.metadata["lan_discovery"]
            )
            profile_template = _lan_profile_protected(
                evidence,
                endpoint_fingerprint=endpoint_fingerprint,
                runtime_adapter=runtime_adapter,
                runtime_hardening=installed_runtime_hardening,
            )
            detected_profile_reasons = (
                ()
                if previous_profile_protected is None
                else _lan_endpoint_drift_reasons(
                    previous_profile_protected,
                    profile_template,
                )
            )
            profile_expired = previous_profile_protected is not None and now > _parse_lan_timestamp(
                previous_profile_protected["fresh_until"],
                "fresh_until",
            )
            profile_reasons = _merge_lan_stale_reasons(
                (
                    ()
                    if previous_profile_protected is None
                    else tuple(previous_profile_protected["stale_reasons"])
                ),
                detected_profile_reasons,
                (("freshness_expired",) if profile_expired else ()),
            )
            individually_expired_ids = {
                target_id
                for target_id, entry in existing_targets.items()
                if now
                > _parse_lan_timestamp(
                    entry.target.metadata["lan_discovery"]["fresh_until"],
                    "fresh_until",
                )
            }
            model_target_ids = tuple(
                _lan_target_id(profile_id, model_id) for model_id in observation.catalog
            )
            current_catalog_is_complete = (
                observation.catalog_complete and not observation.catalog_truncated
            )
            prior_complete_positive_by_target: dict[str, bool] = {}
            selected_model_changed = False
            if current_catalog_is_complete:
                prior_complete_evidence: list[AuthenticatedLanObservation] = []
                prior_evidence_cache: dict[
                    tuple[str, str, str, str], AuthenticatedLanObservation
                ] = {}
                if previous_profile_protected is not None:
                    profile_cache_key = (
                        previous_profile_protected["scan_id"],
                        previous_profile_protected["endpoint_binding_digest"],
                        previous_profile_protected["terminal_receipt_digest"],
                        previous_profile_protected["observation_digest"],
                    )
                    previous_profile_evidence = load_authenticated_task4_observation(
                        connection,
                        scan_id=profile_cache_key[0],
                        endpoint_binding_digest=profile_cache_key[1],
                        expected_terminal_receipt_digest=profile_cache_key[2],
                        expected_observation_digest=profile_cache_key[3],
                        authenticated_owner_principal=authenticated_owner_principal,
                    )
                    prior_evidence_cache[profile_cache_key] = previous_profile_evidence
                    if _lan_observation_is_complete_positive(previous_profile_evidence.observation):
                        prior_complete_evidence.append(previous_profile_evidence)
                for target_id, entry in existing_targets.items():
                    previous = entry.target.metadata["lan_discovery"]
                    cache_key = (
                        previous["scan_id"],
                        previous["endpoint_binding_digest"],
                        previous["terminal_receipt_digest"],
                        previous["observation_digest"],
                    )
                    previous_evidence = prior_evidence_cache.get(cache_key)
                    if previous_evidence is None:
                        previous_evidence = load_authenticated_task4_observation(
                            connection,
                            scan_id=cache_key[0],
                            endpoint_binding_digest=cache_key[1],
                            expected_terminal_receipt_digest=cache_key[2],
                            expected_observation_digest=cache_key[3],
                            authenticated_owner_principal=authenticated_owner_principal,
                        )
                        prior_evidence_cache[cache_key] = previous_evidence
                    previous_observation = previous_evidence.observation
                    prior_complete_positive_by_target[target_id] = (
                        _lan_observation_is_complete_positive(previous_observation)
                    )
                    if prior_complete_positive_by_target[target_id]:
                        prior_complete_evidence.append(previous_evidence)
                if prior_complete_evidence:
                    latest_complete_timestamp = max(
                        item.observed_at for item in prior_complete_evidence
                    )
                    latest_selected_models = {
                        item.observation.selected_model_id
                        for item in prior_complete_evidence
                        if item.observed_at == latest_complete_timestamp
                    }
                    if len(latest_selected_models) != 1:
                        raise ValueError("LAN prior complete selected model evidence is ambiguous")
                    selected_model_changed = (
                        latest_selected_models.pop() != observation.selected_model_id
                    )
            endpoint_wide_refresh = bool(detected_profile_reasons) or profile_expired
            if current_catalog_is_complete:
                affected_ids = tuple(dict.fromkeys((*model_target_ids, *sorted(existing_targets))))
            else:
                stale_present_ids = tuple(
                    target_id
                    for target_id in model_target_ids
                    if target_id in existing_targets
                    and existing_targets[target_id].target.metadata["lan_discovery"][
                        "stale_reasons"
                    ]
                )
                affected_ids = tuple(
                    dict.fromkeys(
                        (
                            *(
                                target_id
                                for target_id in model_target_ids
                                if target_id not in existing_targets
                            ),
                            *stale_present_ids,
                            *(
                                sorted(existing_targets)
                                if endpoint_wide_refresh
                                else sorted(individually_expired_ids)
                            ),
                        )
                    )
                )

            replacement_entry: ProviderProfileEntry | None = None
            replacement_profile_model: ProviderProfile | None = None
            replacement_profile_revision: int | None = None
            replacement_profile_created_at: str | None = None
            replacement_targets: dict[str, ModelTargetEntry] = {}
            replacement_reasons: tuple[str, ...] = ()
            if replacement is not None:
                (
                    old_profile_id,
                    old_expected_revision,
                    old_expected_fingerprint,
                    old_expected_materials,
                ) = replacement
                if old_profile_id == profile_id:
                    raise ValueError("LAN replacement must bind a distinct endpoint")
                old_profile_row = connection.execute(
                    "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                    (old_profile_id,),
                ).fetchone()
                if old_profile_row is None:
                    raise ValueError("LAN replacement profile is missing")
                replacement_entry = _strict_lan_profile_entry(old_profile_row)
                old_protected = replacement_entry.profile.metadata["lan_discovery"]
                if old_protected["owner_principal"] != authenticated_owner_principal:
                    raise ValueError("LAN replacement owner mismatch")
                if old_protected["endpoint_fingerprint"] != old_expected_fingerprint:
                    raise ValueError("LAN replacement endpoint fingerprint mismatch")
                replacement_profile_revision, replacement_profile_created_at = _next_revision(
                    "provider_profile",
                    old_profile_id,
                    old_profile_row,
                    expected_revision=old_expected_revision,
                    now=now_text,
                )
                old_target_rows = connection.execute(
                    """
                    SELECT * FROM routing_model_targets
                    WHERE provider_profile_id = ? ORDER BY target_id ASC
                    """,
                    (old_profile_id,),
                ).fetchall()
                replacement_targets = {
                    entry.target.target_id: entry
                    for entry in (_strict_lan_target_entry(row) for row in old_target_rows)
                }
                if any(
                    entry.target.metadata["lan_discovery"]["owner_principal"]
                    != authenticated_owner_principal
                    for entry in replacement_targets.values()
                ):
                    raise ValueError("LAN replacement target owner mismatch")
                actual_materials = tuple(
                    sorted(
                        str(entry.target.metadata["lan_discovery"]["material_binding_digest"])
                        for entry in replacement_targets.values()
                    )
                )
                if tuple(sorted(old_expected_materials)) != actual_materials:
                    raise ValueError("LAN replacement material binding set mismatch")
                current_profile_protected = _lan_profile_protected(
                    evidence,
                    endpoint_fingerprint=endpoint_fingerprint,
                    runtime_adapter=runtime_adapter,
                    runtime_hardening=installed_runtime_hardening,
                )
                detected_replacement_reasons = _lan_endpoint_drift_reasons(
                    old_protected,
                    current_profile_protected,
                )
                if not detected_replacement_reasons:
                    raise ValueError("LAN replacement lacks endpoint identity drift")
                replacement_reasons = _merge_lan_stale_reasons(
                    tuple(old_protected["stale_reasons"]),
                    detected_replacement_reasons,
                )
                stale_old_protected = dict(old_protected)
                stale_old_protected.update(
                    {
                        "stale_reason": replacement_reasons[0],
                        "stale_reasons": list(replacement_reasons),
                        "stale_transition_terminal_receipt_digest": (
                            evidence.terminal_receipt_digest
                        ),
                    }
                )
                replacement_profile_model = ProviderProfile(
                    **{
                        **asdict(replacement_entry.profile),
                        "enabled": False,
                        "trust_class": "unconfirmed",
                        "metadata": {"lan_discovery": stale_old_protected},
                    }
                )
                affected_ids = tuple(dict.fromkeys((*affected_ids, *sorted(replacement_targets))))

            if set(expected_map) != set(affected_ids):
                raise ValueError("LAN expected target revision set is not exact")
            planned_revisions: dict[str, tuple[int, str]] = {}
            for target_id in affected_ids:
                row = connection.execute(
                    "SELECT * FROM routing_model_targets WHERE target_id = ?",
                    (target_id,),
                ).fetchone()
                planned_revisions[target_id] = _next_revision(
                    "model_target",
                    target_id,
                    row,
                    expected_revision=expected_map[target_id],
                    now=now_text,
                )

            profile_protected = _lan_profile_protected(
                evidence,
                endpoint_fingerprint=endpoint_fingerprint,
                runtime_adapter=runtime_adapter,
                runtime_hardening=installed_runtime_hardening,
                stale_reasons=profile_reasons,
                transition_receipt_digest=(
                    evidence.terminal_receipt_digest if profile_reasons else None
                ),
            )
            profile = _lan_provider_model(
                evidence,
                profile_id=profile_id,
                protected=profile_protected,
            )
            planned_targets: dict[str, ModelTarget] = {}
            stale_reasons_by_target: dict[str, tuple[str, ...]] = {}
            invalidated_materials: list[str] = []
            model_by_target = dict(zip(model_target_ids, observation.catalog, strict=True))
            for target_id in affected_ids:
                if target_id in replacement_targets:
                    replacement_target_reasons = _merge_lan_stale_reasons(
                        tuple(
                            replacement_targets[target_id].target.metadata["lan_discovery"][
                                "stale_reasons"
                            ]
                        ),
                        replacement_reasons,
                    )
                    stale, invalidated = _lan_mark_target_stale(
                        replacement_targets[target_id],
                        reasons=replacement_target_reasons,
                        transition_receipt_digest=evidence.terminal_receipt_digest,
                    )
                    planned_targets[target_id] = stale
                    stale_reasons_by_target[target_id] = replacement_target_reasons
                    invalidated_materials.append(invalidated)
                    continue
                model_id = model_by_target.get(target_id)
                existing = existing_targets.get(target_id)
                if model_id is None:
                    if existing is None:
                        raise ValueError("LAN affected target identity is inconsistent")
                    if current_catalog_is_complete:
                        comparison = _new_lan_target_protected(
                            evidence,
                            provider_profile_id=profile_id,
                            model_id=existing.target.model,
                            target_id=target_id,
                            endpoint_fingerprint=endpoint_fingerprint,
                            runtime_adapter=runtime_adapter,
                            runtime_hardening=installed_runtime_hardening,
                        )
                        current_reasons = _lan_target_drift_reasons(
                            existing.target.metadata["lan_discovery"],
                            comparison,
                            model_present=False,
                            compare_model_identity=prior_complete_positive_by_target.get(
                                target_id,
                                False,
                            ),
                        )
                    else:
                        current_reasons = detected_profile_reasons
                    reasons = _merge_lan_stale_reasons(
                        tuple(existing.target.metadata["lan_discovery"]["stale_reasons"]),
                        current_reasons,
                        (
                            ("freshness_expired",)
                            if profile_expired or target_id in individually_expired_ids
                            else ()
                        ),
                    )
                    stale, invalidated = _lan_mark_target_stale(
                        existing,
                        reasons=reasons,
                        transition_receipt_digest=evidence.terminal_receipt_digest,
                    )
                    planned_targets[target_id] = stale
                    stale_reasons_by_target[target_id] = reasons
                    invalidated_materials.append(invalidated)
                    continue
                fresh = _new_lan_target_protected(
                    evidence,
                    provider_profile_id=profile_id,
                    model_id=model_id,
                    target_id=target_id,
                    endpoint_fingerprint=endpoint_fingerprint,
                    runtime_adapter=runtime_adapter,
                    runtime_hardening=installed_runtime_hardening,
                )
                detected_reasons: tuple[str, ...] = ()
                if existing is not None:
                    detected_reasons = (
                        _lan_target_drift_reasons(
                            existing.target.metadata["lan_discovery"],
                            fresh,
                            model_present=True,
                            compare_model_identity=prior_complete_positive_by_target.get(
                                target_id,
                                False,
                            ),
                        )
                        if current_catalog_is_complete
                        else detected_profile_reasons
                    )
                reasons = _merge_lan_stale_reasons(
                    (
                        ()
                        if existing is None
                        else tuple(existing.target.metadata["lan_discovery"]["stale_reasons"])
                    ),
                    detected_reasons,
                    (
                        ("model_identity_changed",)
                        if selected_model_changed and model_id == observation.selected_model_id
                        else ()
                    ),
                    (
                        profile_reasons
                        if existing is None
                        else (
                            ("freshness_expired",)
                            if profile_expired or target_id in individually_expired_ids
                            else ()
                        )
                    ),
                )
                trust_class = "unconfirmed"
                roles: tuple[str, ...] = ()
                families: tuple[str, ...] = ()
                target_enabled = False
                health = "unavailable" if reasons else "unknown"
                if existing is not None and not reasons:
                    old = existing.target.metadata["lan_discovery"]
                    for field in (
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
                    ):
                        fresh[field] = old[field]
                    trust_class = existing.target.trust_class
                    roles = existing.target.role_affinities
                    families = existing.target.task_family_affinities
                    target_enabled = (
                        existing.target.enabled
                        and fresh["runtime_hardening"]
                        == LAN_OPENAI_RUNTIME_HARDENING_VERSION
                    )
                    if existing.target.enabled and not target_enabled:
                        invalidated_materials.append(str(old["material_binding_digest"]))
                        fresh.update(
                            {
                                "reviewed": False,
                                "reviewed_profile_revision": None,
                                "reviewed_target_revision": None,
                                "review_evidence_terminal_receipt_digest": None,
                                "review_evidence_observation_digest": None,
                                "reviewed_from_material_binding_digest": None,
                                "reviewed_material_binding_digest": None,
                                "review_acknowledged_stale_reasons": None,
                                "review_acknowledged_stale_transition_terminal_receipt_digest": None,
                                "review_digest": None,
                                "privacy_acknowledgement_digest": None,
                                "reviewed_runtime_interface_binding_digest": None,
                                "intended_roles": [],
                                "task_family_affinities": [],
                            }
                        )
                        trust_class = "unconfirmed"
                        roles = ()
                        families = ()
                else:
                    if existing is not None:
                        invalidated_materials.append(
                            str(
                                existing.target.metadata["lan_discovery"]["material_binding_digest"]
                            )
                        )
                    if reasons:
                        fresh.update(
                            {
                                "stale_reason": reasons[0],
                                "stale_reasons": list(reasons),
                                "stale_transition_terminal_receipt_digest": (
                                    evidence.terminal_receipt_digest
                                ),
                            }
                        )
                fresh["material_binding_digest"] = _lan_material_binding_digest(
                    fresh,
                    target_id=target_id,
                    trust_class=trust_class,
                    privacy_acknowledgement_digest=fresh["privacy_acknowledgement_digest"],
                    intended_roles=roles,
                    task_family_affinities=families,
                )
                planned_targets[target_id] = _lan_target_model(
                    target_id=target_id,
                    provider_profile_id=profile_id,
                    runtime_adapter=runtime_adapter,
                    model_id=model_id,
                    protected=fresh,
                    trust_class=trust_class,
                    role_affinities=roles,
                    task_family_affinities=families,
                    health=health,
                    enabled=target_enabled,
                )
                if reasons:
                    stale_reasons_by_target[target_id] = reasons

            current_profile_targets = {
                **{target_id: entry.target for target_id, entry in existing_targets.items()},
                **{
                    target_id: target
                    for target_id, target in planned_targets.items()
                    if target.provider_profile_id == profile_id
                },
            }
            profile_enabled = not profile_reasons and any(
                target.enabled for target in current_profile_targets.values()
            )
            profile = ProviderProfile(
                **{
                    **asdict(profile),
                    "enabled": profile_enabled,
                    "trust_class": (
                        "operator_confirmed" if profile_enabled else "unconfirmed"
                    ),
                }
            )

            _upsert_lan_provider(
                connection,
                profile,
                revision=profile_revision,
                created_at=profile_created_at,
                updated_at=now_text,
            )
            if (
                replacement_profile_model is not None
                and replacement_profile_revision is not None
                and replacement_profile_created_at is not None
            ):
                _upsert_lan_provider(
                    connection,
                    replacement_profile_model,
                    revision=replacement_profile_revision,
                    created_at=replacement_profile_created_at,
                    updated_at=now_text,
                )
            for target_id in affected_ids:
                target_revision, target_created_at = planned_revisions[target_id]
                _upsert_lan_target(
                    connection,
                    planned_targets[target_id],
                    revision=target_revision,
                    created_at=target_created_at,
                    updated_at=now_text,
                )
            _before_lan_commit()
            persisted_profile = connection.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            persisted_targets = [
                connection.execute(
                    "SELECT * FROM routing_model_targets WHERE target_id = ?",
                    (target_id,),
                ).fetchone()
                for target_id in affected_ids
            ]
        if persisted_profile is None or any(row is None for row in persisted_targets):
            raise RuntimeError("LAN inventory write was lost")
        return (
            _strict_lan_profile_entry(persisted_profile),
            tuple(_strict_lan_target_entry(row) for row in persisted_targets if row is not None),
            expected_observation_digest,
            endpoint_fingerprint,
            False,
            affected_ids,
            tuple(sorted(set(invalidated_materials))),
            tuple(
                (target_id, stale_reasons_by_target[target_id])
                for target_id in affected_ids
                if target_id in stale_reasons_by_target
            ),
        )

    def review_lan_target(
        self,
        *,
        target_id: str,
        expected_profile_revision: int,
        expected_target_revision: int,
        expected_terminal_receipt_digest: str,
        expected_observation_digest: str,
        expected_endpoint_fingerprint: str,
        expected_material_binding_digest: str,
        expected_review_digest: str,
        expected_stale_reasons: tuple[str, ...],
        trust_class: str,
        intended_roles: tuple[str, ...],
        task_family_affinities: tuple[str, ...],
        privacy_acknowledged: bool,
        enabled: bool,
        authenticated_owner_principal: str,
        now: datetime,
        runtime_hardening_version: str | None = None,
        interface_inventory_resolver: InterfaceInventoryResolver | None = None,
    ) -> tuple[ProviderProfileEntry, ModelTargetEntry, str, str]:
        """Atomically record an owner review over one exact LAN preimage."""

        if type(target_id) is not str or _LAN_TARGET_ID_RE.fullmatch(target_id) is None:
            raise ValueError("LAN review target identifier is invalid")
        _validate_exact_expected_revision(expected_profile_revision)
        _validate_exact_expected_revision(expected_target_revision)
        for field, value in (
            ("expected_terminal_receipt_digest", expected_terminal_receipt_digest),
            ("expected_observation_digest", expected_observation_digest),
            ("expected_endpoint_fingerprint", expected_endpoint_fingerprint),
            ("expected_material_binding_digest", expected_material_binding_digest),
            ("expected_review_digest", expected_review_digest),
        ):
            validate_digest(value, field)
        if (
            type(expected_stale_reasons) is not tuple
            or any(type(reason) is not str for reason in expected_stale_reasons)
            or expected_stale_reasons != _merge_lan_stale_reasons(expected_stale_reasons)
        ):
            raise ValueError("LAN review stale reasons are invalid")
        if (
            type(intended_roles) is not tuple
            or _validate_lan_affinities(
                list(intended_roles),
                "intended roles",
            )
            != intended_roles
        ):
            raise ValueError("LAN review intended roles are invalid")
        if (
            type(task_family_affinities) is not tuple
            or _validate_lan_affinities(
                list(task_family_affinities),
                "task-family affinities",
            )
            != task_family_affinities
        ):
            raise ValueError("LAN review task-family affinities are invalid")
        if type(enabled) is not bool:
            raise ValueError("LAN review enabled must be boolean")
        _validate_installed_runtime_hardening(runtime_hardening_version)
        if (
            type(trust_class) is not str
            or trust_class != "operator_confirmed"
            or type(privacy_acknowledged) is not bool
            or privacy_acknowledged is not True
        ):
            raise ValueError("LAN review requires owner-confirmed privacy and trust")
        _validate_canonical_lan_owner(authenticated_owner_principal)
        now_text = _lan_timestamp(now)
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target_row = connection.execute(
                "SELECT * FROM routing_model_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
            if target_row is None:
                raise KeyError(f"Unknown LAN target: {target_id}")
            if _row_has_exact_legacy_runtime_binding_shape(
                _target_entry_from_row(target_row)
            ):
                raise ValueError(
                    "lan_runtime_binding_upgrade_requires_positive_reimport"
                )
            target_entry = _strict_lan_target_entry(target_row)
            profile_id = target_entry.target.provider_profile_id
            profile_row = connection.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if profile_row is None:
                raise ValueError("LAN target provider profile is missing")
            profile_entry = _strict_lan_profile_entry(profile_row)
            target_protected = target_entry.target.metadata["lan_discovery"]
            profile_protected = profile_entry.profile.metadata["lan_discovery"]
            if (
                target_protected["owner_principal"] != authenticated_owner_principal
                or profile_protected["owner_principal"] != authenticated_owner_principal
            ):
                raise ValueError("LAN review owner mismatch")
            if target_entry.revision != expected_target_revision:
                raise RoutingRevisionConflict(
                    "model_target",
                    target_id,
                    target_entry.revision,
                )
            if profile_entry.revision != expected_profile_revision:
                raise RoutingRevisionConflict(
                    "provider_profile",
                    profile_id,
                    profile_entry.revision,
                )
            expected_values = {
                "terminal_receipt_digest": expected_terminal_receipt_digest,
                "observation_digest": expected_observation_digest,
                "endpoint_fingerprint": expected_endpoint_fingerprint,
                "material_binding_digest": expected_material_binding_digest,
            }
            if any(target_protected[field] != value for field, value in expected_values.items()):
                raise ValueError("LAN review evidence preimage mismatch")
            if tuple(target_protected["stale_reasons"]) != expected_stale_reasons:
                raise ValueError("LAN review stale reasons mismatch")
            transition_receipt = target_protected["stale_transition_terminal_receipt_digest"]
            if (
                expected_stale_reasons
                and transition_receipt != target_protected["terminal_receipt_digest"]
            ):
                raise ValueError("LAN stale target requires target-specific fresh evidence")
            for field in (
                "owner_principal",
                "endpoint_binding_digest",
                "endpoint_fingerprint",
                "interface_id",
                "confirmed_network",
                "address",
                "port",
                "transport_security",
                "certificate_sha256",
                "api_shape",
                "runtime_adapter",
                "runtime_hardening",
            ):
                if profile_protected[field] != target_protected[field]:
                    raise ValueError("LAN profile and target endpoint bindings disagree")
            if enabled and (
                runtime_hardening_version != LAN_OPENAI_RUNTIME_HARDENING_VERSION
                or _validated_runtime_hardening_marker(profile_protected)
                != LAN_OPENAI_RUNTIME_HARDENING_VERSION
                or _validated_runtime_hardening_marker(target_protected)
                != LAN_OPENAI_RUNTIME_HARDENING_VERSION
            ):
                raise ValueError("lan_runtime_hardening_unavailable")
            if expected_stale_reasons:
                endpoint_stale_reasons = tuple(
                    reason
                    for reason in expected_stale_reasons
                    if reason in _LAN_ENDPOINT_STALE_REASONS
                )
                profile_stale_reasons = tuple(profile_protected["stale_reasons"])
                profile_transition = profile_protected["stale_transition_terminal_receipt_digest"]
                if profile_stale_reasons:
                    if (
                        profile_stale_reasons != endpoint_stale_reasons
                        or profile_transition != transition_receipt
                    ):
                        raise ValueError(
                            "LAN profile stale transition disagrees with target evidence"
                        )
                elif profile_protected["terminal_receipt_digest"] != transition_receipt:
                    raise ValueError("LAN stale target requires target-specific positive refresh")
            evidence = load_authenticated_task4_observation(
                connection,
                scan_id=target_protected["scan_id"],
                endpoint_binding_digest=target_protected["endpoint_binding_digest"],
                expected_terminal_receipt_digest=expected_terminal_receipt_digest,
                expected_observation_digest=expected_observation_digest,
                authenticated_owner_principal=authenticated_owner_principal,
            )
            _validate_lan_review_evidence_projection(
                target_protected,
                evidence,
                now=now,
            )
            target_evidence_key = (
                target_protected["scan_id"],
                target_protected["endpoint_binding_digest"],
                target_protected["terminal_receipt_digest"],
                target_protected["observation_digest"],
            )
            profile_evidence_key = (
                profile_protected["scan_id"],
                profile_protected["endpoint_binding_digest"],
                profile_protected["terminal_receipt_digest"],
                profile_protected["observation_digest"],
            )
            profile_evidence = evidence
            if profile_evidence_key != target_evidence_key:
                profile_evidence = load_authenticated_task4_observation(
                    connection,
                    scan_id=profile_evidence_key[0],
                    endpoint_binding_digest=profile_evidence_key[1],
                    expected_terminal_receipt_digest=profile_evidence_key[2],
                    expected_observation_digest=profile_evidence_key[3],
                    authenticated_owner_principal=authenticated_owner_principal,
                )
            _validate_lan_review_evidence_projection(
                profile_protected,
                profile_evidence,
                now=now,
            )
            if (
                target_protected["provider_profile_id"] != profile_id
                or target_protected["model_id"] != target_entry.target.model
            ):
                raise ValueError("LAN review target binding is inconsistent")
            if target_protected["capability_claims"] != _lan_capability_claims(
                evidence,
                model_id=target_entry.target.model,
            ):
                raise ValueError("LAN review capability claims disagree with evidence")
            if target_entry.target.model not in evidence.observation.catalog:
                raise ValueError("LAN review model is absent from exact evidence")
            if enabled and (
                target_entry.target.model != evidence.observation.selected_model_id
                or target_protected["capability_claims"][0]
                != evidence.observation.capabilities[0].to_digest_payload()
                or not target_entry.target.capability_tags
            ):
                raise ValueError("LAN runtime generation capability is not eligible")
            authenticated_source: AuthenticatedLanSource | None = None
            if enabled:
                _scope, _endpoint, authenticated_source = (
                    _authenticate_lan_runtime_source(
                        target_protected,
                        interface_inventory_resolver=interface_inventory_resolver,
                    )
                )
            privacy_digest = _lan_privacy_acknowledgement_digest(
                owner_principal=authenticated_owner_principal,
                provider_profile_id=profile_id,
                target_id=target_id,
                observation_digest=expected_observation_digest,
                endpoint_fingerprint=expected_endpoint_fingerprint,
                expected_profile_revision=expected_profile_revision,
                expected_target_revision=expected_target_revision,
                intended_roles=intended_roles,
                task_family_affinities=task_family_affinities,
                expected_stale_reasons=expected_stale_reasons,
                stale_transition_terminal_receipt_digest=transition_receipt,
                enabled=enabled,
            )
            reviewed_material = _lan_material_binding_digest(
                target_protected,
                target_id=target_id,
                trust_class="operator_confirmed",
                privacy_acknowledgement_digest=privacy_digest,
                intended_roles=intended_roles,
                task_family_affinities=task_family_affinities,
            )
            review_digest = sha256_digest(
                {
                    "schema": "kestrel.lan.review.v1",
                    "privacy_acknowledgement_digest": privacy_digest,
                    "expected_terminal_receipt_digest": (expected_terminal_receipt_digest),
                    "expected_observation_digest": expected_observation_digest,
                    "pre_review_material_binding_digest": (expected_material_binding_digest),
                    "reviewed_material_binding_digest": reviewed_material,
                    "expected_stale_reasons": list(expected_stale_reasons),
                    "stale_transition_terminal_receipt_digest": transition_receipt,
                }
            )
            if review_digest != expected_review_digest:
                raise ValueError("LAN review digest does not match its exact preimage")
            reviewed_protected = dict(target_protected)
            reviewed_protected.update(
                {
                    "material_binding_digest": reviewed_material,
                    "reviewed": True,
                    "reviewed_profile_revision": expected_profile_revision,
                    "reviewed_target_revision": expected_target_revision,
                    "review_evidence_terminal_receipt_digest": (expected_terminal_receipt_digest),
                    "review_evidence_observation_digest": expected_observation_digest,
                    "reviewed_from_material_binding_digest": (expected_material_binding_digest),
                    "reviewed_material_binding_digest": reviewed_material,
                    "review_acknowledged_stale_reasons": list(expected_stale_reasons),
                    "review_acknowledged_stale_transition_terminal_receipt_digest": (
                        transition_receipt
                    ),
                    "review_digest": review_digest,
                    "privacy_acknowledgement_digest": privacy_digest,
                    "reviewed_runtime_interface_binding_digest": None,
                    "intended_roles": list(intended_roles),
                    "task_family_affinities": list(task_family_affinities),
                    "stale_reason": None,
                    "stale_reasons": [],
                    "stale_transition_terminal_receipt_digest": None,
                }
            )
            if authenticated_source is not None:
                reviewed_protected["reviewed_runtime_interface_binding_digest"] = (
                    _lan_reviewed_runtime_interface_binding_digest(
                        reviewed_protected,
                        source=authenticated_source,
                    )
                )
            reviewed_target = ModelTarget(
                **{
                    **asdict(target_entry.target),
                    "enabled": enabled,
                    "trust_class": "operator_confirmed",
                    "role_affinities": intended_roles,
                    "task_family_affinities": task_family_affinities,
                    "health": "unknown",
                    "metadata": {"lan_discovery": reviewed_protected},
                }
            )
            reviewed_profile = profile_entry.profile
            profile_stale_reasons = tuple(profile_protected["stale_reasons"])
            if (
                profile_stale_reasons
                and profile_protected["stale_transition_terminal_receipt_digest"]
                == transition_receipt
            ):
                remaining_profile_reasons = tuple(
                    reason
                    for reason in profile_stale_reasons
                    if reason not in expected_stale_reasons
                )
                cleared_profile_protected = dict(profile_protected)
                cleared_profile_protected.update(
                    {
                        "stale_reason": (
                            remaining_profile_reasons[0] if remaining_profile_reasons else None
                        ),
                        "stale_reasons": list(remaining_profile_reasons),
                        "stale_transition_terminal_receipt_digest": (
                            transition_receipt if remaining_profile_reasons else None
                        ),
                    }
                )
                reviewed_profile = ProviderProfile(
                    **{
                        **asdict(profile_entry.profile),
                        "metadata": {"lan_discovery": cleared_profile_protected},
                    }
                )
            sibling_rows = connection.execute(
                """
                SELECT * FROM routing_model_targets
                WHERE provider_profile_id = ? AND target_id != ?
                ORDER BY target_id ASC
                """,
                (profile_id, target_id),
            ).fetchall()
            profile_enabled = enabled or any(
                _strict_lan_target_entry(row).target.enabled for row in sibling_rows
            )
            reviewed_profile = ProviderProfile(
                **{
                    **asdict(reviewed_profile),
                    "enabled": profile_enabled,
                    "trust_class": (
                        "operator_confirmed" if profile_enabled else "unconfirmed"
                    ),
                }
            )
            profile_revision, profile_created_at = _next_revision(
                "provider_profile",
                profile_id,
                profile_row,
                expected_revision=expected_profile_revision,
                now=now_text,
            )
            target_revision, target_created_at = _next_revision(
                "model_target",
                target_id,
                target_row,
                expected_revision=expected_target_revision,
                now=now_text,
            )
            _upsert_lan_provider(
                connection,
                reviewed_profile,
                revision=profile_revision,
                created_at=profile_created_at,
                updated_at=now_text,
            )
            _upsert_lan_target(
                connection,
                reviewed_target,
                revision=target_revision,
                created_at=target_created_at,
                updated_at=now_text,
            )
            _before_lan_commit()
            persisted_profile = connection.execute(
                "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            persisted_target = connection.execute(
                "SELECT * FROM routing_model_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
        if persisted_profile is None or persisted_target is None:
            raise RuntimeError("LAN review write was lost")
        return (
            _strict_lan_profile_entry(persisted_profile),
            _strict_lan_target_entry(persisted_target),
            privacy_digest,
            reviewed_material,
        )

    def put_policy(
        self,
        policy: RoutePolicy,
        *,
        enabled: bool = True,
        expected_revision: int | None = None,
    ) -> RoutePolicyEntry:
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM routing_policies WHERE policy_id = ?",
                (policy.policy_id,),
            ).fetchone()
            revision, created_at = _next_revision(
                "route_policy",
                policy.policy_id,
                row,
                expected_revision=expected_revision,
                now=now,
            )
            conn.execute(
                """
                INSERT INTO routing_policies (
                    policy_id, payload_json, enabled, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    enabled = excluded.enabled,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.policy_id,
                    _json(asdict(policy)),
                    1 if enabled else 0,
                    revision,
                    created_at,
                    now,
                ),
            )
            persisted = conn.execute(
                "SELECT * FROM routing_policies WHERE policy_id = ?",
                (policy.policy_id,),
            ).fetchone()
        if persisted is None:
            raise RuntimeError("route_policy_write_lost")
        return _policy_entry_from_row(persisted)

    def get_policy(self, policy_id: str) -> RoutePolicyEntry | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_policies WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
        return None if row is None else _policy_entry_from_row(row)

    def list_policies(self, *, enabled_only: bool = False) -> list[RoutePolicyEntry]:
        sql = "SELECT * FROM routing_policies"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY policy_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_policy_entry_from_row(row) for row in rows]


def _before_inventory_commit() -> None:
    """Test seam immediately before the atomic inventory transaction commits."""


def _before_lan_commit() -> None:
    """Test seam immediately before an atomic LAN inventory commit."""


def _lan_profile_id(endpoint_binding_digest: str) -> str:
    digest = sha256_digest(
        {
            "schema": "kestrel.lan.provider-binding.v1",
            "endpoint_binding_digest": endpoint_binding_digest,
        }
    )
    return "lan-provider-" + digest.removeprefix("sha256:")


def _lan_target_id(provider_profile_id: str, model_id: str) -> str:
    digest = sha256_digest(
        {
            "schema": "kestrel.lan.model-target.v1",
            "provider_profile_id": provider_profile_id,
            "model_id": model_id,
        }
    )
    return "lan-target-" + digest.removeprefix("sha256:")


def _lan_endpoint_fingerprint(evidence: AuthenticatedLanObservation) -> str:
    observation = evidence.observation
    return sha256_digest(
        {
            "schema": "kestrel.lan.endpoint-fingerprint.v1",
            "endpoint_binding_digest": observation.endpoint_binding_digest,
            "interface_id": observation.endpoint.interface_id,
            "confirmed_network": evidence.confirmed_network,
            "address": observation.endpoint.address,
            "port": observation.endpoint.port,
            "transport_security": (
                observation.transport_security.value
                if observation.transport_security is not None
                else None
            ),
            "certificate_sha256": None,
            "api_shape": (
                observation.api_shape.value if observation.api_shape is not None else None
            ),
        }
    )


def _lan_runtime_adapter(api_shape: ApiShape) -> str:
    if api_shape is ApiShape.OPENAI_COMPATIBLE:
        return "lan-openai-compatible"
    if api_shape is ApiShape.OLLAMA_COMPATIBLE:
        return "lan-ollama-compatible"
    raise ValueError("unsupported LAN runtime adapter")


def _lan_base_url(evidence: AuthenticatedLanObservation) -> str:
    observation = evidence.observation
    if observation.api_shape is None:
        raise ValueError("positive LAN evidence requires an API shape")
    address = observation.endpoint.address
    authority_host = f"[{address}]" if ":" in address else address
    authority = f"http://{authority_host}:{observation.endpoint.port}"
    if observation.api_shape is ApiShape.OPENAI_COMPATIBLE:
        return authority + "/v1"
    return authority


def _lan_timestamp(value: datetime) -> str:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("LAN transaction clock must be aware UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _authenticate_lan_runtime_source(
    protected: dict[str, Any],
    *,
    interface_inventory_resolver: InterfaceInventoryResolver | None,
) -> tuple[PrivateScanScope, ResolvedLanEndpoint, AuthenticatedLanSource]:
    inventory_loader = (
        interface_inventory_resolver or _resolve_current_interface_inventory
    )
    try:
        inventory = inventory_loader()
    except Exception:
        raise ValueError("LAN runtime interface inventory failed") from None
    if type(inventory) is not CurrentLanInterfaceInventory:
        raise ValueError("LAN runtime interface inventory is invalid")
    matching_interfaces: list[NetworkInterface] = []
    try:
        for state in inventory.interfaces:
            interface = NetworkInterface.from_addresses(
                os_identity=state.os_identity,
                display_name=state.os_identity,
                addresses=state.addresses,
            )
            if interface.interface_id == protected["interface_id"]:
                matching_interfaces.append(interface)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("LAN runtime interface inventory is invalid") from None
    if len(matching_interfaces) != 1:
        raise ValueError("LAN runtime interface binding changed")
    try:
        scope = PrivateScanScope.from_request(
            matching_interfaces[0],
            protected["confirmed_network"],
        )
        endpoint = ResolvedLanEndpoint.from_scope(
            scope,
            protected["address"],
            protected["port"],
        )
        source = authenticate_lan_source(scope, endpoint, lambda: inventory)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("LAN runtime source binding changed") from None
    if source.interface_id != protected["interface_id"]:
        raise ValueError("LAN runtime source binding changed")
    return scope, endpoint, source


def _lan_evidence_fields(
    evidence: AuthenticatedLanObservation,
    *,
    endpoint_fingerprint: str,
    runtime_adapter: str,
    runtime_hardening: str | None,
) -> dict[str, Any]:
    observation = evidence.observation
    return {
        "owner_principal": evidence.owner_principal,
        "scan_id": evidence.scan_id,
        "endpoint_binding_digest": observation.endpoint_binding_digest,
        "endpoint_fingerprint": endpoint_fingerprint,
        "interface_id": observation.endpoint.interface_id,
        "confirmed_network": evidence.confirmed_network,
        "address": observation.endpoint.address,
        "port": observation.endpoint.port,
        "transport_security": (
            observation.transport_security.value
            if observation.transport_security is not None
            else None
        ),
        "certificate_sha256": None,
        "api_shape": (observation.api_shape.value if observation.api_shape is not None else None),
        "observation_digest": observation.observation_digest,
        "terminal_receipt_digest": evidence.terminal_receipt_digest,
        "catalog_digest": observation.catalog_digest,
        "capability_digest": observation.capability_digest,
        "observed_at": _lan_timestamp(evidence.observed_at),
        "fresh_until": _lan_timestamp(
            evidence.observed_at + timedelta(seconds=LAN_OBSERVATION_MAX_AGE_SECONDS)
        ),
        "runtime_adapter": runtime_adapter,
        "runtime_hardening": runtime_hardening,
    }


def _validate_lan_review_evidence_projection(
    protected: dict[str, Any],
    evidence: AuthenticatedLanObservation,
    *,
    now: datetime,
) -> None:
    if evidence.observation.api_shape is None:
        raise ValueError("LAN review evidence has no runtime API shape")
    runtime_adapter = _lan_runtime_adapter(evidence.observation.api_shape)
    canonical_evidence = _lan_evidence_fields(
        evidence,
        endpoint_fingerprint=_lan_endpoint_fingerprint(evidence),
        runtime_adapter=runtime_adapter,
        runtime_hardening=_validated_runtime_hardening_marker(protected),
    )
    if any(protected[field] != value for field, value in canonical_evidence.items()):
        raise ValueError("LAN review metadata disagrees with durable evidence")
    if (
        _lan_timestamp(evidence.observed_at) != protected["observed_at"]
        or now > _parse_lan_timestamp(protected["fresh_until"], "fresh_until")
        or evidence.observed_at > now + timedelta(seconds=LAN_OBSERVATION_MAX_FUTURE_SKEW_SECONDS)
    ):
        raise ValueError("LAN review evidence is not fresh")


def _lan_profile_protected(
    evidence: AuthenticatedLanObservation,
    *,
    endpoint_fingerprint: str,
    runtime_adapter: str,
    runtime_hardening: str | None,
    stale_reasons: tuple[str, ...] = (),
    transition_receipt_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": _LAN_PROFILE_SCHEMA,
        "managed": True,
        **_lan_evidence_fields(
            evidence,
            endpoint_fingerprint=endpoint_fingerprint,
            runtime_adapter=runtime_adapter,
            runtime_hardening=runtime_hardening,
        ),
        "stale_reason": stale_reasons[0] if stale_reasons else None,
        "stale_reasons": list(stale_reasons),
        "stale_transition_terminal_receipt_digest": transition_receipt_digest,
    }


def _lan_capability_claims(
    evidence: AuthenticatedLanObservation,
    *,
    model_id: str,
) -> list[dict[str, Any]]:
    observation = evidence.observation
    capabilities = [
        (
            item.to_digest_payload()
            if item.capability is CapabilityName.GENERATION
            and model_id == observation.selected_model_id
            else {
                "capability": item.capability.value,
                "provenance": "not_run",
                "status": "not_run",
                "supported": None,
            }
        )
        for item in observation.capabilities
    ]
    return capabilities


def _lan_material_binding_digest(
    protected: dict[str, Any],
    *,
    target_id: str,
    trust_class: str,
    privacy_acknowledgement_digest: str | None,
    intended_roles: tuple[str, ...],
    task_family_affinities: tuple[str, ...],
) -> str:
    return sha256_digest(
        {
            "schema": "kestrel.lan.material-binding.v1",
            "provider_profile_id": protected["provider_profile_id"],
            "target_id": target_id,
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
    )


def _lan_privacy_acknowledgement_digest(
    *,
    owner_principal: str,
    provider_profile_id: str,
    target_id: str,
    observation_digest: str,
    endpoint_fingerprint: str,
    expected_profile_revision: int,
    expected_target_revision: int,
    intended_roles: tuple[str, ...],
    task_family_affinities: tuple[str, ...],
    expected_stale_reasons: tuple[str, ...],
    stale_transition_terminal_receipt_digest: str | None,
    enabled: bool,
) -> str:
    return sha256_digest(
        {
            "schema": "kestrel.lan.privacy-acknowledgement.v1",
            "owner_principal": owner_principal,
            "provider_profile_id": provider_profile_id,
            "target_id": target_id,
            "observation_digest": observation_digest,
            "endpoint_fingerprint": endpoint_fingerprint,
            "expected_profile_revision": expected_profile_revision,
            "expected_target_revision": expected_target_revision,
            "trust_class": "operator_confirmed",
            "intended_roles": list(intended_roles),
            "task_family_affinities": list(task_family_affinities),
            "enabled": enabled,
            "privacy_acknowledged": True,
            "expected_stale_reasons": list(expected_stale_reasons),
            "stale_transition_terminal_receipt_digest": (stale_transition_terminal_receipt_digest),
        }
    )


def _lan_reviewed_runtime_interface_binding_digest(
    protected: dict[str, Any],
    *,
    source: AuthenticatedLanSource,
) -> str:
    if type(source) is not AuthenticatedLanSource:
        raise ValueError("LAN reviewed runtime source is invalid")
    os_identity = source.os_identity
    if (
        type(os_identity) is not str
        or not os_identity
        or os_identity != os_identity.strip()
        or len(os_identity.encode("utf-8")) > 256
        or unicodedata.normalize("NFC", os_identity) != os_identity
        or any(unicodedata.category(character).startswith("C") for character in os_identity)
        or source.interface_id != protected["interface_id"]
    ):
        raise ValueError("LAN reviewed runtime source is invalid")
    source_address = normalize_address(source.source_address)
    if (
        source_address != source.source_address
        or isinstance(source.interface_index, bool)
        or not isinstance(source.interface_index, int)
        or not 0 < source.interface_index <= 2**31 - 1
    ):
        raise ValueError("LAN reviewed runtime source is invalid")
    return derive_lan_runtime_interface_binding_digest(
        os_interface_identity=os_identity,
        source_address=source_address,
        interface_index=source.interface_index,
        interface_id=protected["interface_id"],
        confirmed_network=protected["confirmed_network"],
        endpoint_binding_digest=protected["endpoint_binding_digest"],
        endpoint_fingerprint=protected["endpoint_fingerprint"],
        reviewed_material_binding_digest=protected[
            "reviewed_material_binding_digest"
        ],
        review_digest=protected["review_digest"],
    )


def _new_lan_target_protected(
    evidence: AuthenticatedLanObservation,
    *,
    provider_profile_id: str,
    model_id: str,
    target_id: str,
    endpoint_fingerprint: str,
    runtime_adapter: str,
    runtime_hardening: str | None,
    stale_reasons: tuple[str, ...] = (),
    transition_receipt_digest: str | None = None,
) -> dict[str, Any]:
    protected: dict[str, Any] = {
        "schema": _LAN_TARGET_SCHEMA,
        "managed": True,
        **_lan_evidence_fields(
            evidence,
            endpoint_fingerprint=endpoint_fingerprint,
            runtime_adapter=runtime_adapter,
            runtime_hardening=runtime_hardening,
        ),
        "provider_profile_id": provider_profile_id,
        "model_id": model_id,
        "capability_claims": _lan_capability_claims(evidence, model_id=model_id),
        "material_binding_digest": None,
        "reviewed": False,
        "reviewed_profile_revision": None,
        "reviewed_target_revision": None,
        "review_evidence_terminal_receipt_digest": None,
        "review_evidence_observation_digest": None,
        "reviewed_from_material_binding_digest": None,
        "reviewed_material_binding_digest": None,
        "review_acknowledged_stale_reasons": None,
        "review_acknowledged_stale_transition_terminal_receipt_digest": None,
        "review_digest": None,
        "privacy_acknowledgement_digest": None,
        "reviewed_runtime_interface_binding_digest": None,
        "intended_roles": [],
        "task_family_affinities": [],
        "stale_reason": stale_reasons[0] if stale_reasons else None,
        "stale_reasons": list(stale_reasons),
        "stale_transition_terminal_receipt_digest": transition_receipt_digest,
    }
    protected["material_binding_digest"] = _lan_material_binding_digest(
        protected,
        target_id=target_id,
        trust_class="unconfirmed",
        privacy_acknowledgement_digest=None,
        intended_roles=(),
        task_family_affinities=(),
    )
    return protected


def _lan_provider_model(
    evidence: AuthenticatedLanObservation,
    *,
    profile_id: str,
    protected: dict[str, Any],
) -> ProviderProfile:
    adapter = str(protected["runtime_adapter"])
    return ProviderProfile(
        profile_id=profile_id,
        display_name=(
            f"LAN {evidence.observation.endpoint.address}:{evidence.observation.endpoint.port}"
        ),
        adapter=adapter,
        base_url=_lan_base_url(evidence),
        secret_ref=None,
        enabled=False,
        locality="local",
        trust_class="unconfirmed",
        max_concurrency=1,
        metadata={"lan_discovery": protected},
    )


def _lan_target_model(
    *,
    target_id: str,
    provider_profile_id: str,
    runtime_adapter: str,
    model_id: str,
    protected: dict[str, Any],
    trust_class: str = "unconfirmed",
    role_affinities: tuple[str, ...] = (),
    task_family_affinities: tuple[str, ...] = (),
    health: str = "unknown",
    enabled: bool = False,
) -> ModelTarget:
    generation_claimed = protected["capability_claims"][0]["status"] == "observed_pass"
    return ModelTarget(
        target_id=target_id,
        provider_profile_id=provider_profile_id,
        provider=runtime_adapter,
        model=model_id,
        enabled=enabled,
        locality="local",
        trust_class=trust_class,
        capability_tags=("generation",) if generation_claimed else (),
        role_affinities=role_affinities,
        task_family_affinities=task_family_affinities,
        max_context_tokens=None,
        supports_tools=False,
        supports_json=False,
        supports_vision=False,
        supports_reasoning=False,
        supports_streaming=False,
        quality_tier=1,
        latency_tier=3,
        operator_priority=0,
        estimated_cost_usd=None,
        input_cost_per_million_usd=None,
        output_cost_per_million_usd=None,
        health=health,  # type: ignore[arg-type]
        recent_failure_rate=0.0,
        predicted_success=None,
        metadata={"lan_discovery": protected},
    )


def _lan_endpoint_drift_reasons(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> tuple[str, ...]:
    comparisons = (
        ("interface_id", "interface_changed"),
        ("confirmed_network", "network_changed"),
        ("address", "address_changed"),
        ("port", "port_changed"),
        ("transport_security", "transport_security_changed"),
        ("certificate_sha256", "certificate_changed"),
        ("api_shape", "api_shape_changed"),
    )
    return tuple(reason for field, reason in comparisons if previous[field] != current[field])


def _lan_observation_is_complete_positive(
    observation: LanEndpointObservation,
) -> bool:
    generation = observation.capabilities[0]
    return (
        observation.catalog_complete
        and not observation.catalog_truncated
        and observation.reachability is Reachability.REACHABLE
        and generation.status is CapabilityObservationStatus.OBSERVED_PASS
        and generation.supported is True
        and observation.failure_category is None
        and observation.api_shape is not None
        and bool(observation.catalog)
    )


def _merge_lan_stale_reasons(*groups: tuple[str, ...]) -> tuple[str, ...]:
    observed = {reason for group in groups for reason in group}
    if not observed.issubset(_LAN_STALE_REASON_ORDER):
        raise ValueError("LAN stale reason is outside the closed vocabulary")
    return tuple(reason for reason in _LAN_STALE_REASON_ORDER if reason in observed)


def _validate_canonical_lan_owner(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("LAN managed owner principal is invalid")
    return value


def _validate_installed_runtime_hardening(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or value != LAN_OPENAI_RUNTIME_HARDENING_VERSION:
        raise ValueError("LAN runtime hardening version is not installed")
    return value


def _validated_runtime_hardening_marker(protected: dict[str, Any]) -> str | None:
    marker = _validate_installed_runtime_hardening(protected["runtime_hardening"])
    if marker is not None and (
        protected["api_shape"] != ApiShape.OPENAI_COMPATIBLE.value
        or protected["runtime_adapter"] != "lan-openai-compatible"
    ):
        raise ValueError("LAN runtime hardening marker disagrees with API binding")
    return marker


def _lan_target_drift_reasons(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    model_present: bool,
    compare_model_identity: bool,
) -> tuple[str, ...]:
    reasons = list(_lan_endpoint_drift_reasons(previous, current))
    if previous["catalog_digest"] != current["catalog_digest"]:
        reasons.append("catalog_changed")
    previous_selected = previous["capability_claims"][0]["status"] == "observed_pass"
    current_selected = current["capability_claims"][0]["status"] == "observed_pass"
    if compare_model_identity and previous_selected != current_selected:
        reasons.append("model_identity_changed")
    if not model_present:
        reasons.append("model_missing")
    elif previous["capability_digest"] != current["capability_digest"]:
        reasons.append("capability_changed")
    return tuple(reason for reason in _LAN_STALE_REASON_ORDER if reason in reasons)


def _strict_lan_metadata(
    row: sqlite3.Row,
    *,
    target: bool,
) -> dict[str, Any]:
    raw = row["metadata_json"]
    if type(raw) is not str:
        raise ValueError("LAN managed metadata must be JSON text")
    try:
        metadata = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("LAN managed metadata is invalid JSON") from None
    if type(metadata) is not dict or set(metadata) != {"lan_discovery"}:
        raise ValueError("LAN managed metadata has an invalid field set")
    if canonical_json(metadata) != raw:
        raise ValueError("LAN managed metadata is not canonical")
    protected = metadata["lan_discovery"]
    if (
        target
        and type(protected) is dict
        and set(protected)
        == _LAN_TARGET_PROTECTED_KEYS
        - {"reviewed_runtime_interface_binding_digest"}
        and protected.get("runtime_hardening") is None
        and row["enabled"] == 0
    ):
        protected = dict(protected)
        protected["reviewed_runtime_interface_binding_digest"] = None
    return _validate_lan_protected_metadata(protected, target=target)


def _validate_lan_protected_metadata(
    protected: object,
    *,
    target: bool,
) -> dict[str, Any]:
    expected_keys = _LAN_TARGET_PROTECTED_KEYS if target else _LAN_PROFILE_PROTECTED_KEYS
    if type(protected) is not dict or set(protected) != expected_keys:
        raise ValueError("LAN managed protected metadata has an invalid field set")
    expected_schema = _LAN_TARGET_SCHEMA if target else _LAN_PROFILE_SCHEMA
    if protected["schema"] != expected_schema or protected["managed"] is not True:
        raise ValueError("LAN managed metadata schema is invalid")
    for field in (
        "endpoint_binding_digest",
        "endpoint_fingerprint",
        "interface_id",
        "observation_digest",
        "terminal_receipt_digest",
        "catalog_digest",
        "capability_digest",
    ):
        validate_digest(protected[field], field)
    validate_digest(
        protected["certificate_sha256"],
        "certificate_sha256",
        optional=True,
    )
    validate_digest(
        protected["stale_transition_terminal_receipt_digest"],
        "stale_transition_terminal_receipt_digest",
        optional=True,
    )
    _validate_canonical_lan_owner(protected["owner_principal"])
    scan_id = protected["scan_id"]
    if (
        type(scan_id) is not str
        or not scan_id
        or scan_id != scan_id.strip()
        or len(scan_id) > 128
        or unicodedata.normalize("NFC", scan_id) != scan_id
        or any(unicodedata.category(character).startswith("C") for character in scan_id)
    ):
        raise ValueError("LAN managed scan identifier is invalid")
    network = normalize_network(protected["confirmed_network"])
    address = normalize_address(protected["address"])
    if network != protected["confirmed_network"] or address != protected["address"]:
        raise ValueError("LAN managed network identity is not canonical")
    parsed_network = ipaddress.ip_network(network, strict=True)
    parsed_address = ipaddress.ip_address(address)
    private_scope = (
        any(
            parsed_network.subnet_of(private_network)
            for private_network in _LAN_PRIVATE_IPV4_NETWORKS
        )
        if isinstance(parsed_network, ipaddress.IPv4Network)
        else any(
            parsed_network.subnet_of(private_network)
            for private_network in _LAN_PRIVATE_IPV6_NETWORKS
        )
    )
    if not private_scope:
        raise ValueError("LAN managed network is outside private discovery scope")
    if isinstance(parsed_network, ipaddress.IPv4Network):
        host_count = (
            parsed_network.num_addresses
            if parsed_network.prefixlen >= 31
            else parsed_network.num_addresses - 2
        )
        if host_count > MAX_ACTIVE_HOSTS:
            raise ValueError("LAN managed IPv4 network exceeds the active-host bound")
        if parsed_network.prefixlen <= 30 and parsed_address in {
            parsed_network.network_address,
            parsed_network.broadcast_address,
        }:
            raise ValueError("LAN managed endpoint is not an active IPv4 host")
    if (
        parsed_address not in parsed_network
        or parsed_address.is_unspecified
        or parsed_address.is_loopback
        or parsed_address.is_multicast
        or parsed_address.is_reserved
    ):
        raise ValueError("LAN managed endpoint is outside its confirmed network")
    if type(protected["port"]) is not int or protected["port"] not in KNOWN_MODEL_SERVICE_PORTS:
        raise ValueError("LAN managed endpoint port is invalid")
    if (
        protected["transport_security"] != TransportSecurity.PLAIN_HTTP.value
        or protected["certificate_sha256"] is not None
    ):
        raise ValueError("LAN managed transport evidence is invalid")
    try:
        api_shape = ApiShape(protected["api_shape"])
    except (TypeError, ValueError):
        raise ValueError("LAN managed API shape is invalid") from None
    expected_adapter = _lan_runtime_adapter(api_shape)
    if protected["runtime_adapter"] != expected_adapter:
        raise ValueError("LAN managed runtime adapter does not match API evidence")
    expected_endpoint_binding = sha256_digest(
        {
            "address": address,
            "interface_id": protected["interface_id"],
            "port": protected["port"],
            "schema": "kestrel.lan.endpoint-binding.v1",
        }
    )
    if protected["endpoint_binding_digest"] != expected_endpoint_binding:
        raise ValueError("LAN endpoint binding digest does not match its preimage")
    observed_at = _parse_lan_timestamp(protected["observed_at"], "observed_at")
    fresh_until = _parse_lan_timestamp(protected["fresh_until"], "fresh_until")
    if fresh_until != observed_at + timedelta(seconds=LAN_OBSERVATION_MAX_AGE_SECONDS):
        raise ValueError("LAN managed freshness interval is invalid")
    stale_reasons = protected["stale_reasons"]
    if (
        type(stale_reasons) is not list
        or len(stale_reasons) != len(set(stale_reasons))
        or tuple(stale_reasons)
        != tuple(reason for reason in _LAN_STALE_REASON_ORDER if reason in stale_reasons)
        or protected["stale_reason"] != (stale_reasons[0] if stale_reasons else None)
        or (
            bool(stale_reasons)
            != (protected["stale_transition_terminal_receipt_digest"] is not None)
        )
    ):
        raise ValueError("LAN managed stale reasons are invalid")
    _validated_runtime_hardening_marker(protected)
    expected_fingerprint = sha256_digest(
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
    if protected["endpoint_fingerprint"] != expected_fingerprint:
        raise ValueError("LAN endpoint fingerprint does not match its preimage")
    return protected


def _strict_lan_profile_entry(row: sqlite3.Row) -> ProviderProfileEntry:
    protected = _strict_lan_metadata(row, target=False)
    entry = _profile_entry_from_row(row)
    profile = entry.profile
    address = str(protected["address"])
    authority_host = f"[{address}]" if ":" in address else address
    authority = f"http://{authority_host}:{protected['port']}"
    expected_base_url = (
        authority + "/v1"
        if protected["api_shape"] == ApiShape.OPENAI_COMPATIBLE.value
        else authority
    )
    if (
        _LAN_PROFILE_ID_RE.fullmatch(profile.profile_id) is None
        or profile.profile_id != _lan_profile_id(protected["endpoint_binding_digest"])
        or profile.display_name != f"LAN {address}:{protected['port']}"
        or profile.adapter != protected["runtime_adapter"]
        or profile.base_url != expected_base_url
        or profile.secret_ref is not None
        or profile.locality != "local"
        or profile.trust_class
        != ("operator_confirmed" if profile.enabled else "unconfirmed")
        or profile.max_concurrency != 1
        or (
            profile.enabled
            and (
                protected["stale_reasons"]
                or _validated_runtime_hardening_marker(protected)
                != LAN_OPENAI_RUNTIME_HARDENING_VERSION
            )
        )
    ):
        raise ValueError("LAN managed provider row is structurally invalid")
    return entry


def _row_has_exact_legacy_runtime_binding_shape(entry: ModelTargetEntry) -> bool:
    metadata = entry.target.metadata
    if type(metadata) is not dict or set(metadata) != {"lan_discovery"}:
        return False
    protected = metadata["lan_discovery"]
    return (
        type(protected) is dict
        and set(protected)
        == _LAN_TARGET_PROTECTED_KEYS
        - {"reviewed_runtime_interface_binding_digest"}
    )


def _strict_lan_target_entry(row: sqlite3.Row) -> ModelTargetEntry:
    protected = _strict_lan_metadata(row, target=True)
    entry = _target_entry_from_row(row)
    if entry.target.metadata != {"lan_discovery": protected}:
        entry = ModelTargetEntry(
            target=ModelTarget(
                **{
                    **asdict(entry.target),
                    "metadata": {"lan_discovery": protected},
                }
            ),
            revision=entry.revision,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )
    target = entry.target
    _validate_managed_lan_target_model(
        target,
        protected=protected,
        strict_storage_projection=True,
    )
    reviewed = protected["reviewed"]
    if reviewed and protected["reviewed_target_revision"] >= entry.revision:
        raise ValueError("LAN reviewed target revision is not a prior CAS revision")
    return entry


def _validate_lan_affinities(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > 16:
        raise ValueError(f"LAN {field} must contain at most 16 values")
    normalized: list[str] = []
    for item in value:
        if (
            type(item) is not str
            or not item
            or unicodedata.normalize("NFC", item) != item
            or any(unicodedata.category(character).startswith("C") for character in item)
            or len(item.encode("utf-8")) > 64
        ):
            raise ValueError(f"LAN {field} contains an invalid value")
        normalized.append(item)
    if normalized != sorted(set(normalized)):
        raise ValueError(f"LAN {field} must be unique and deterministically ordered")
    return tuple(normalized)


def _validate_managed_lan_target_model(
    target: ModelTarget,
    *,
    protected: object | None = None,
    strict_storage_projection: bool = False,
) -> dict[str, Any]:
    """Validate the complete in-memory authority bound to one managed LAN target."""

    if protected is None:
        if type(target.metadata) is not dict or set(target.metadata) != {"lan_discovery"}:
            raise ValueError("LAN managed target metadata has an invalid field set")
        protected = target.metadata["lan_discovery"]
    protected = _validate_lan_protected_metadata(protected, target=True)
    model_id = protected["model_id"]
    try:
        canonical_model_id = LanProbeModel.from_catalog(model_id).model_id
    except (TypeError, ValueError):
        raise ValueError("LAN managed model identity is invalid") from None
    if canonical_model_id != model_id:
        raise ValueError("LAN managed model identity is invalid")
    intended_roles = _validate_lan_affinities(
        protected["intended_roles"],
        "intended roles",
    )
    task_families = _validate_lan_affinities(
        protected["task_family_affinities"],
        "task-family affinities",
    )
    if (
        _LAN_TARGET_ID_RE.fullmatch(target.target_id) is None
        or _LAN_PROFILE_ID_RE.fullmatch(target.provider_profile_id) is None
        or protected["provider_profile_id"] != _lan_profile_id(protected["endpoint_binding_digest"])
        or target.target_id
        != _lan_target_id(protected["provider_profile_id"], protected["model_id"])
        or target.provider_profile_id != protected["provider_profile_id"]
        or target.model != model_id
        or target.provider != protected["runtime_adapter"]
        or target.locality != "local"
        or target.trust_class not in {"unconfirmed", "operator_confirmed"}
        or target.role_affinities != intended_roles
        or target.task_family_affinities != task_families
        or target.max_context_tokens is not None
        or target.supports_tools
        or target.supports_json
        or target.supports_vision
        or target.supports_reasoning
        or target.supports_streaming
        or target.quality_tier != 1
        or target.latency_tier != 3
        or target.operator_priority != 0
        or target.estimated_cost_usd is not None
        or target.input_cost_per_million_usd is not None
        or target.output_cost_per_million_usd is not None
        or target.recent_failure_rate != 0.0
        or target.predicted_success is not None
    ):
        raise ValueError("LAN managed target projection is structurally invalid")
    if strict_storage_projection:
        expected_health = "unavailable" if protected["stale_reasons"] else "unknown"
        if target.health != expected_health:
            raise ValueError("LAN managed target storage state is invalid")
        if target.enabled and (
            not protected["reviewed"]
            or protected["stale_reasons"]
            or target.trust_class != "operator_confirmed"
            or _validated_runtime_hardening_marker(protected)
            != LAN_OPENAI_RUNTIME_HARDENING_VERSION
        ):
            raise ValueError("LAN enabled target storage state is invalid")

    raw_capabilities = protected["capability_claims"]
    if type(raw_capabilities) is not list or len(raw_capabilities) != len(CapabilityName):
        raise ValueError("LAN managed capability claims are incomplete")
    capabilities: list[LanCapabilityEvidence] = []
    for index, raw_capability in enumerate(raw_capabilities):
        if type(raw_capability) is not dict or set(raw_capability) != {
            "capability",
            "supported",
            "provenance",
            "status",
        }:
            raise ValueError("LAN managed capability claim is malformed")
        capability = tuple(CapabilityName)[index]
        if raw_capability["capability"] != capability.value:
            raise ValueError("LAN managed capability claims are not ordered")
        try:
            capabilities.append(
                LanCapabilityEvidence(
                    capability=capability,
                    supported=raw_capability["supported"],
                    provenance=CapabilityProvenance(raw_capability["provenance"]),
                    status=CapabilityObservationStatus(raw_capability["status"]),
                )
            )
        except (TypeError, ValueError):
            raise ValueError("LAN managed capability claim is invalid") from None
    generation_claimed = (
        capabilities[0].status is CapabilityObservationStatus.OBSERVED_PASS
        and capabilities[0].provenance is CapabilityProvenance.OBSERVED
        and capabilities[0].supported is True
    )
    if target.enabled and not generation_claimed:
        raise ValueError("LAN enabled target lacks observed generation capability")
    derived_route = (
        "/v1/chat/completions"
        if protected["api_shape"] == ApiShape.OPENAI_COMPATIBLE.value
        else "/api/generate"
    )
    derived_selected_model = target.model if generation_claimed else None
    if (
        target.capability_tags != (("generation",) if generation_claimed else ())
        or (generation_claimed and (derived_selected_model is None or not derived_route))
        or any(
            capability.status is not CapabilityObservationStatus.NOT_RUN
            or capability.provenance is not CapabilityProvenance.NOT_RUN
            or capability.supported is not None
            for capability in capabilities[1:]
        )
        or (
            not generation_claimed
            and (
                derived_selected_model is not None
                or any(
                    capability.status is not CapabilityObservationStatus.NOT_RUN
                    or capability.provenance is not CapabilityProvenance.NOT_RUN
                    or capability.supported is not None
                    for capability in capabilities
                )
            )
        )
    ):
        raise ValueError("LAN target capability projection exceeds evidence")

    for field in (
        "material_binding_digest",
        "review_evidence_terminal_receipt_digest",
        "review_evidence_observation_digest",
        "reviewed_from_material_binding_digest",
        "reviewed_material_binding_digest",
        "review_digest",
        "privacy_acknowledgement_digest",
        "reviewed_runtime_interface_binding_digest",
        "review_acknowledged_stale_transition_terminal_receipt_digest",
    ):
        validate_digest(protected[field], field, optional=True)
    reviewed = protected["reviewed"]
    if type(reviewed) is not bool or reviewed != (target.trust_class == "operator_confirmed"):
        raise ValueError("LAN review authority is inconsistent")
    expected_material = _lan_material_binding_digest(
        protected,
        target_id=target.target_id,
        trust_class=target.trust_class,
        privacy_acknowledgement_digest=protected["privacy_acknowledgement_digest"],
        intended_roles=target.role_affinities,
        task_family_affinities=target.task_family_affinities,
    )
    if protected["material_binding_digest"] != expected_material:
        raise ValueError("LAN material binding digest does not match its preimage")

    review_fields = (
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
    )
    if not reviewed:
        if (
            any(protected[field] is not None for field in review_fields)
            or target.trust_class != "unconfirmed"
            or target.role_affinities
            or target.task_family_affinities
        ):
            raise ValueError("unreviewed LAN target carries review authority")
        return protected

    reviewed_profile_revision = protected["reviewed_profile_revision"]
    reviewed_target_revision = protected["reviewed_target_revision"]
    if (
        type(reviewed_profile_revision) is not int
        or reviewed_profile_revision < 0
        or type(reviewed_target_revision) is not int
        or reviewed_target_revision < 0
        or any(
            protected[field] is None
            for field in (
                "review_evidence_terminal_receipt_digest",
                "review_evidence_observation_digest",
                "reviewed_from_material_binding_digest",
                "reviewed_material_binding_digest",
                "review_acknowledged_stale_reasons",
                "review_digest",
                "privacy_acknowledgement_digest",
            )
        )
        or protected["stale_reasons"]
        or protected["stale_transition_terminal_receipt_digest"] is not None
        or (
            target.enabled
            != (protected["reviewed_runtime_interface_binding_digest"] is not None)
        )
    ):
        raise ValueError("LAN review preimage is incomplete")
    acknowledged_reasons = protected["review_acknowledged_stale_reasons"]
    if (
        type(acknowledged_reasons) is not list
        or tuple(acknowledged_reasons) != _merge_lan_stale_reasons(tuple(acknowledged_reasons))
        or (
            bool(acknowledged_reasons)
            != (
                protected["review_acknowledged_stale_transition_terminal_receipt_digest"]
                is not None
            )
        )
    ):
        raise ValueError("LAN review acknowledged stale state is invalid")
    privacy_digest = _lan_privacy_acknowledgement_digest(
        owner_principal=protected["owner_principal"],
        provider_profile_id=target.provider_profile_id,
        target_id=target.target_id,
        observation_digest=protected["review_evidence_observation_digest"],
        endpoint_fingerprint=protected["endpoint_fingerprint"],
        expected_profile_revision=reviewed_profile_revision,
        expected_target_revision=reviewed_target_revision,
        intended_roles=target.role_affinities,
        task_family_affinities=target.task_family_affinities,
        expected_stale_reasons=tuple(acknowledged_reasons),
        stale_transition_terminal_receipt_digest=protected[
            "review_acknowledged_stale_transition_terminal_receipt_digest"
        ],
        enabled=target.enabled,
    )
    reviewed_material = _lan_material_binding_digest(
        protected,
        target_id=target.target_id,
        trust_class="operator_confirmed",
        privacy_acknowledgement_digest=privacy_digest,
        intended_roles=target.role_affinities,
        task_family_affinities=target.task_family_affinities,
    )
    review_digest = sha256_digest(
        {
            "schema": "kestrel.lan.review.v1",
            "privacy_acknowledgement_digest": privacy_digest,
            "expected_terminal_receipt_digest": protected[
                "review_evidence_terminal_receipt_digest"
            ],
            "expected_observation_digest": protected["review_evidence_observation_digest"],
            "pre_review_material_binding_digest": protected[
                "reviewed_from_material_binding_digest"
            ],
            "reviewed_material_binding_digest": reviewed_material,
            "expected_stale_reasons": list(acknowledged_reasons),
            "stale_transition_terminal_receipt_digest": protected[
                "review_acknowledged_stale_transition_terminal_receipt_digest"
            ],
        }
    )
    if (
        protected["privacy_acknowledgement_digest"] != privacy_digest
        or protected["reviewed_material_binding_digest"] != reviewed_material
        or protected["material_binding_digest"] != reviewed_material
        or protected["review_digest"] != review_digest
    ):
        raise ValueError("LAN reviewed authority does not match its stored preimage")
    return protected


def _lan_is_managed_metadata(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower().replace("-", "_") == "lan_discovery":
                return True
            if _lan_is_managed_metadata(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_lan_is_managed_metadata(item) for item in value)
    return False


def _validate_exact_expected_revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected revision must be an exact non-negative integer")
    return value


def _reject_generic_lan_profile(profile: ProviderProfile) -> None:
    if _lan_is_managed_metadata(profile.metadata):
        raise ValueError("lan_discovery managed metadata requires the specialized LAN service")
    if _has_reserved_lan_prefix(profile.profile_id):
        raise ValueError("lan provider identifier is reserved")


def _reject_generic_lan_target(target: ModelTarget) -> None:
    if _lan_is_managed_metadata(target.metadata):
        raise ValueError("lan_discovery managed metadata requires the specialized LAN service")
    if _has_reserved_lan_prefix(target.target_id) or _has_reserved_lan_prefix(
        target.provider_profile_id
    ):
        raise ValueError("lan target identifier is reserved")


def _row_is_lan_managed(row: sqlite3.Row, *, profile: bool) -> bool:
    resource_id = str(row["profile_id"] if profile else row["target_id"])
    reserved = _has_reserved_lan_prefix(resource_id)
    if not profile:
        reserved = reserved or _has_reserved_lan_prefix(str(row["provider_profile_id"]))
    try:
        metadata = json.loads(str(row["metadata_json"]))
    except (TypeError, ValueError):
        return reserved
    return reserved or _lan_is_managed_metadata(metadata)


def _has_reserved_lan_prefix(value: str) -> bool:
    return value.startswith((_LAN_PROFILE_ID_PREFIX, _LAN_TARGET_ID_PREFIX))


def _lan_mark_target_stale(
    entry: ModelTargetEntry,
    *,
    reasons: tuple[str, ...],
    transition_receipt_digest: str,
) -> tuple[ModelTarget, str]:
    protected = dict(entry.target.metadata["lan_discovery"])
    invalidated_material = str(protected["material_binding_digest"])
    protected.update(
        {
            "reviewed": False,
            "reviewed_profile_revision": None,
            "reviewed_target_revision": None,
            "review_evidence_terminal_receipt_digest": None,
            "review_evidence_observation_digest": None,
            "reviewed_from_material_binding_digest": None,
            "reviewed_material_binding_digest": None,
            "review_acknowledged_stale_reasons": None,
            "review_acknowledged_stale_transition_terminal_receipt_digest": None,
            "review_digest": None,
            "privacy_acknowledgement_digest": None,
            "reviewed_runtime_interface_binding_digest": None,
            "intended_roles": [],
            "task_family_affinities": [],
            "stale_reason": reasons[0],
            "stale_reasons": list(reasons),
            "stale_transition_terminal_receipt_digest": transition_receipt_digest,
        }
    )
    protected["material_binding_digest"] = _lan_material_binding_digest(
        protected,
        target_id=entry.target.target_id,
        trust_class="unconfirmed",
        privacy_acknowledgement_digest=None,
        intended_roles=(),
        task_family_affinities=(),
    )
    return (
        ModelTarget(
            **{
                **asdict(entry.target),
                "enabled": False,
                "trust_class": "unconfirmed",
                "role_affinities": (),
                "task_family_affinities": (),
                "health": "unavailable",
                "metadata": {"lan_discovery": protected},
            }
        ),
        invalidated_material,
    )


def _upsert_lan_provider(
    connection: sqlite3.Connection,
    profile: ProviderProfile,
    *,
    revision: int,
    created_at: str,
    updated_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO routing_provider_profiles (
            profile_id, display_name, adapter, base_url, secret_ref, enabled,
            locality, trust_class, max_concurrency, metadata_json, revision,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile_id) DO UPDATE SET
            display_name = excluded.display_name,
            adapter = excluded.adapter,
            base_url = excluded.base_url,
            secret_ref = excluded.secret_ref,
            enabled = excluded.enabled,
            locality = excluded.locality,
            trust_class = excluded.trust_class,
            max_concurrency = excluded.max_concurrency,
            metadata_json = excluded.metadata_json,
            revision = excluded.revision,
            updated_at = excluded.updated_at
        """,
        (
            profile.profile_id,
            profile.display_name,
            profile.adapter,
            profile.base_url,
            profile.secret_ref,
            1 if profile.enabled else 0,
            profile.locality,
            profile.trust_class,
            profile.max_concurrency,
            canonical_json(profile.metadata),
            revision,
            created_at,
            updated_at,
        ),
    )


def _upsert_lan_target(
    connection: sqlite3.Connection,
    target: ModelTarget,
    *,
    revision: int,
    created_at: str,
    updated_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO routing_model_targets (
            target_id, provider_profile_id, provider, model, enabled, locality,
            trust_class, capability_tags_json, role_affinities_json,
            task_family_affinities_json, max_context_tokens, supports_tools,
            supports_json, supports_vision, supports_reasoning, supports_streaming,
            quality_tier, latency_tier, operator_priority, estimated_cost_usd,
            input_cost_per_million_usd, output_cost_per_million_usd, health,
            recent_failure_rate, predicted_success, metadata_json, revision,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_id) DO UPDATE SET
            provider_profile_id = excluded.provider_profile_id,
            provider = excluded.provider,
            model = excluded.model,
            enabled = excluded.enabled,
            locality = excluded.locality,
            trust_class = excluded.trust_class,
            capability_tags_json = excluded.capability_tags_json,
            role_affinities_json = excluded.role_affinities_json,
            task_family_affinities_json = excluded.task_family_affinities_json,
            max_context_tokens = excluded.max_context_tokens,
            supports_tools = excluded.supports_tools,
            supports_json = excluded.supports_json,
            supports_vision = excluded.supports_vision,
            supports_reasoning = excluded.supports_reasoning,
            supports_streaming = excluded.supports_streaming,
            quality_tier = excluded.quality_tier,
            latency_tier = excluded.latency_tier,
            operator_priority = excluded.operator_priority,
            estimated_cost_usd = excluded.estimated_cost_usd,
            input_cost_per_million_usd = excluded.input_cost_per_million_usd,
            output_cost_per_million_usd = excluded.output_cost_per_million_usd,
            health = excluded.health,
            recent_failure_rate = excluded.recent_failure_rate,
            predicted_success = excluded.predicted_success,
            metadata_json = excluded.metadata_json,
            revision = excluded.revision,
            updated_at = excluded.updated_at
        """,
        _target_values(
            target,
            revision=revision,
            created_at=created_at,
            updated_at=updated_at,
        ),
    )


def _apply_lan_outage(
    connection: sqlite3.Connection,
    *,
    evidence: AuthenticatedLanObservation,
    expected_profile_revision: int,
    expected_target_revisions: dict[str, int],
    now: datetime,
    now_text: str,
) -> tuple[
    ProviderProfileEntry | None,
    tuple[ModelTargetEntry, ...],
    str | None,
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, tuple[str, ...]], ...],
]:
    matches: list[ProviderProfileEntry] = []
    for row in connection.execute(
        "SELECT * FROM routing_provider_profiles ORDER BY profile_id ASC"
    ).fetchall():
        profile_id = str(row["profile_id"])
        raw = row["metadata_json"]
        try:
            metadata = json.loads(str(raw))
        except (TypeError, ValueError):
            if _has_reserved_lan_prefix(profile_id):
                raise ValueError("LAN managed provider metadata is invalid") from None
            continue
        marker = type(metadata) is dict and _lan_is_managed_metadata(metadata)
        if not marker and not _has_reserved_lan_prefix(profile_id):
            continue
        entry = _strict_lan_profile_entry(row)
        protected = entry.profile.metadata["lan_discovery"]
        if protected["endpoint_binding_digest"] == evidence.observation.endpoint_binding_digest:
            matches.append(entry)
    if not matches:
        if expected_profile_revision != 0:
            raise RoutingRevisionConflict(
                "provider_profile",
                _lan_profile_id(evidence.observation.endpoint_binding_digest),
                0,
            )
        if expected_target_revisions:
            raise ValueError("LAN outage target revision set must be empty")
        return None, (), None, (), (), ()
    if len(matches) != 1:
        raise ValueError("LAN outage endpoint correlation is ambiguous")
    profile_entry = matches[0]
    if profile_entry.revision != expected_profile_revision:
        raise RoutingRevisionConflict(
            "provider_profile",
            profile_entry.profile.profile_id,
            profile_entry.revision,
        )
    protected = profile_entry.profile.metadata["lan_discovery"]
    if protected["owner_principal"] != evidence.owner_principal:
        raise ValueError("LAN outage owner mismatch")
    endpoint_fingerprint = str(protected["endpoint_fingerprint"])
    target_rows = connection.execute(
        """
        SELECT * FROM routing_model_targets
        WHERE provider_profile_id = ? ORDER BY target_id ASC
        """,
        (profile_entry.profile.profile_id,),
    ).fetchall()
    targets = tuple(_strict_lan_target_entry(row) for row in target_rows)
    if any(
        target.target.metadata["lan_discovery"]["owner_principal"] != evidence.owner_principal
        for target in targets
    ):
        raise ValueError("LAN outage target owner mismatch")
    profile_expired = now > _parse_lan_timestamp(
        protected["fresh_until"],
        "fresh_until",
    )
    profile_needs_transition = (
        profile_expired and "freshness_expired" not in protected["stale_reasons"]
    )
    affected_targets = tuple(
        target
        for target in targets
        if now
        > _parse_lan_timestamp(
            target.target.metadata["lan_discovery"]["fresh_until"],
            "fresh_until",
        )
        and "freshness_expired" not in target.target.metadata["lan_discovery"]["stale_reasons"]
    )
    affected_ids = tuple(entry.target.target_id for entry in affected_targets)
    if set(expected_target_revisions) != set(affected_ids):
        raise ValueError("LAN outage target revision set is not exact")
    if not profile_needs_transition and not affected_targets:
        return profile_entry, (), endpoint_fingerprint, (), (), ()
    profile_row = connection.execute(
        "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
        (profile_entry.profile.profile_id,),
    ).fetchone()
    if profile_row is None:
        raise ValueError("LAN outage provider profile disappeared")
    profile_revision = profile_entry.revision
    profile_created_at = profile_entry.created_at
    if profile_needs_transition:
        profile_revision, profile_created_at = _next_revision(
            "provider_profile",
            profile_entry.profile.profile_id,
            profile_row,
            expected_revision=expected_profile_revision,
            now=now_text,
        )
    planned_revisions: dict[str, tuple[int, str]] = {}
    for target in affected_targets:
        target_id = target.target.target_id
        row = connection.execute(
            "SELECT * FROM routing_model_targets WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        planned_revisions[target_id] = _next_revision(
            "model_target",
            target_id,
            row,
            expected_revision=expected_target_revisions[target_id],
            now=now_text,
        )
    stale_profile = profile_entry.profile
    if profile_needs_transition:
        profile_reasons = _merge_lan_stale_reasons(
            tuple(protected["stale_reasons"]),
            ("freshness_expired",),
        )
        stale_profile_protected = dict(protected)
        stale_profile_protected.update(
            {
                "stale_reason": profile_reasons[0],
                "stale_reasons": list(profile_reasons),
                "stale_transition_terminal_receipt_digest": (evidence.terminal_receipt_digest),
            }
        )
        stale_profile = ProviderProfile(
            **{
                **asdict(profile_entry.profile),
                "enabled": False,
                "trust_class": "unconfirmed",
                "metadata": {"lan_discovery": stale_profile_protected},
            }
        )
    planned_targets: list[ModelTarget] = []
    invalidated: list[str] = []
    stale_map: list[tuple[str, tuple[str, ...]]] = []
    for target in affected_targets:
        target_protected = target.target.metadata["lan_discovery"]
        reasons = tuple(
            reason
            for reason in _LAN_STALE_REASON_ORDER
            if reason in {*target_protected["stale_reasons"], "freshness_expired"}
        )
        stale, material = _lan_mark_target_stale(
            target,
            reasons=reasons,
            transition_receipt_digest=evidence.terminal_receipt_digest,
        )
        planned_targets.append(stale)
        invalidated.append(material)
        stale_map.append((target.target.target_id, reasons))
    if profile_needs_transition:
        _upsert_lan_provider(
            connection,
            stale_profile,
            revision=profile_revision,
            created_at=profile_created_at,
            updated_at=now_text,
        )
    for planned_target in planned_targets:
        revision, created_at = planned_revisions[planned_target.target_id]
        _upsert_lan_target(
            connection,
            planned_target,
            revision=revision,
            created_at=created_at,
            updated_at=now_text,
        )
    persisted_profile_row = connection.execute(
        "SELECT * FROM routing_provider_profiles WHERE profile_id = ?",
        (profile_entry.profile.profile_id,),
    ).fetchone()
    persisted_target_rows = [
        connection.execute(
            "SELECT * FROM routing_model_targets WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        for target_id in affected_ids
    ]
    if persisted_profile_row is None or any(row is None for row in persisted_target_rows):
        raise RuntimeError("LAN outage expiry write was lost")
    return (
        _strict_lan_profile_entry(persisted_profile_row),
        tuple(_strict_lan_target_entry(row) for row in persisted_target_rows if row is not None),
        endpoint_fingerprint,
        affected_ids,
        tuple(sorted(set(invalidated))),
        tuple(stale_map),
    )


def _parse_lan_timestamp(value: object, field: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"LAN {field} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"LAN {field} is invalid") from None
    if parsed.astimezone(UTC).isoformat().replace("+00:00", "Z") != value:
        raise ValueError(f"LAN {field} must be canonical UTC")
    return parsed.astimezone(UTC)
