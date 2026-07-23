from __future__ import annotations

from dataclasses import asdict

from ..state_store import AgentStateStore, utc_now
from .ledger_records import ModelTargetEntry, ProviderProfileEntry, RoutePolicyEntry
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
            row = conn.execute(
                "SELECT version FROM routing_schema_version WHERE id = 1"
            ).fetchone()
        return 0 if row is None else int(row["version"])

    def put_provider_profile(
        self,
        profile: ProviderProfile,
        *,
        expected_revision: int | None = None,
    ) -> ProviderProfileEntry:
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
            profile = _profile_entry_from_row(profile_row).profile
            if target.provider != profile.adapter:
                raise ValueError("target provider does not match provider profile adapter")
            if profile.locality != "hybrid" and target.locality != profile.locality:
                raise ValueError("target locality does not match provider profile locality")
            row = conn.execute(
                "SELECT * FROM routing_model_targets WHERE target_id = ?",
                (target.target_id,),
            ).fetchone()
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
                    health, recent_failure_rate, predicted_success, metadata_json,
                    revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        return None if row is None else _target_entry_from_row(row)

    def list_model_targets(self, *, enabled_only: bool = False) -> list[ModelTargetEntry]:
        sql = "SELECT * FROM routing_model_targets"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY target_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_target_entry_from_row(row) for row in rows]

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
