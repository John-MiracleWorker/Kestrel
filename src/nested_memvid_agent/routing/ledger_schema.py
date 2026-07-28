from __future__ import annotations

import sqlite3

from ..state_store import AgentStateStore, utc_now

ROUTING_SCHEMA_VERSION = 2


def ensure_routing_schema(state: AgentStateStore) -> None:
    with state._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS routing_schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        row = conn.execute(
            "SELECT version FROM routing_schema_version WHERE id = 1"
        ).fetchone()
        current = 0 if row is None else int(row["version"])
        if current > ROUTING_SCHEMA_VERSION:
            raise RuntimeError(
                f"Routing schema {current} is newer than supported schema "
                f"{ROUTING_SCHEMA_VERSION}."
            )
        if current < 1:
            _apply_routing_schema_v1(conn)
            current = 1
        if current < 2:
            _apply_routing_schema_v2(conn)
            current = 2
        conn.execute(
            """
            INSERT INTO routing_schema_version (id, version, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (current, utc_now()),
        )


def _apply_routing_schema_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_provider_profiles (
            profile_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            adapter TEXT NOT NULL,
            base_url TEXT,
            secret_ref TEXT,
            enabled INTEGER NOT NULL,
            locality TEXT NOT NULL,
            trust_class TEXT NOT NULL,
            max_concurrency INTEGER NOT NULL,
            metadata_json TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_model_targets (
            target_id TEXT PRIMARY KEY,
            provider_profile_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            locality TEXT NOT NULL,
            trust_class TEXT NOT NULL,
            capability_tags_json TEXT NOT NULL,
            role_affinities_json TEXT NOT NULL,
            task_family_affinities_json TEXT NOT NULL,
            max_context_tokens INTEGER,
            supports_tools INTEGER NOT NULL,
            supports_json INTEGER NOT NULL,
            supports_vision INTEGER NOT NULL,
            supports_reasoning INTEGER NOT NULL,
            supports_streaming INTEGER NOT NULL,
            quality_tier INTEGER NOT NULL,
            latency_tier INTEGER NOT NULL,
            operator_priority INTEGER NOT NULL,
            estimated_cost_usd REAL,
            health TEXT NOT NULL,
            recent_failure_rate REAL NOT NULL,
            predicted_success REAL,
            metadata_json TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (provider_profile_id)
                REFERENCES routing_provider_profiles(profile_id)
                ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_policies (
            policy_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_decisions (
            decision_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            subagent_id TEXT,
            attempt INTEGER NOT NULL,
            status TEXT NOT NULL,
            mode TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            policy_revision INTEGER NOT NULL,
            contract_digest TEXT NOT NULL,
            selected_target_id TEXT NOT NULL,
            selected_target_revision INTEGER NOT NULL,
            selected_profile_id TEXT NOT NULL,
            selected_profile_revision INTEGER NOT NULL,
            selected_provider TEXT NOT NULL,
            selected_model TEXT NOT NULL,
            selection_kind TEXT NOT NULL,
            score REAL NOT NULL,
            predicted_success REAL,
            estimated_cost_usd REAL,
            reason_codes_json TEXT NOT NULL,
            candidate_snapshot_json TEXT NOT NULL,
            actionable INTEGER NOT NULL,
            router_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY (policy_id) REFERENCES routing_policies(policy_id),
            FOREIGN KEY (selected_target_id) REFERENCES routing_model_targets(target_id),
            FOREIGN KEY (selected_profile_id) REFERENCES routing_provider_profiles(profile_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_outcomes (
            outcome_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            subagent_id TEXT,
            attempt INTEGER NOT NULL,
            execution_status TEXT NOT NULL,
            validation_passed INTEGER NOT NULL,
            validation_codes_json TEXT NOT NULL,
            failure_category TEXT,
            provider_failure_code TEXT,
            latency_seconds REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            actual_cost_usd REAL,
            tool_count INTEGER NOT NULL,
            changed_file_count INTEGER,
            retry_count INTEGER NOT NULL,
            escalated INTEGER NOT NULL,
            reward_components_json TEXT NOT NULL,
            outcome_labels_json TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (decision_id) REFERENCES routing_decisions(decision_id)
        )
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_routing_targets_profile ON routing_model_targets(provider_profile_id)",
        "CREATE INDEX IF NOT EXISTS idx_routing_targets_enabled ON routing_model_targets(enabled)",
        "CREATE INDEX IF NOT EXISTS idx_routing_decisions_run ON routing_decisions(run_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_routing_decisions_task ON routing_decisions(task_id, attempt)",
        "CREATE INDEX IF NOT EXISTS idx_routing_decisions_subagent ON routing_decisions(subagent_id, attempt)",
        "CREATE INDEX IF NOT EXISTS idx_routing_outcomes_run ON routing_outcomes(run_id, created_at)",
    ):
        conn.execute(statement)


def _apply_routing_schema_v2(conn: sqlite3.Connection) -> None:
    """Add measured, project-scoped learned-routing state.

    The migration is intentionally additive. Existing v1 decisions remain
    readable with empty scope and pricing snapshots, so a routing ledger can be
    upgraded without rewriting historical evidence.
    """

    for statement in (
        "ALTER TABLE routing_model_targets ADD COLUMN input_cost_per_million_usd REAL",
        "ALTER TABLE routing_model_targets ADD COLUMN output_cost_per_million_usd REAL",
        "ALTER TABLE routing_decisions ADD COLUMN project_id TEXT",
        "ALTER TABLE routing_decisions ADD COLUMN task_family TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE routing_decisions ADD COLUMN risk TEXT NOT NULL DEFAULT ''",
        (
            "ALTER TABLE routing_decisions ADD COLUMN "
            "required_capabilities_json TEXT NOT NULL DEFAULT '[]'"
        ),
        "ALTER TABLE routing_decisions ADD COLUMN capability_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE routing_decisions ADD COLUMN input_cost_per_million_usd REAL",
        "ALTER TABLE routing_decisions ADD COLUMN output_cost_per_million_usd REAL",
    ):
        conn.execute(statement)
    conn.execute(
        """
        CREATE TABLE routing_shadow_evaluations (
            shadow_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            task_family TEXT NOT NULL,
            risk TEXT NOT NULL,
            capability_key TEXT NOT NULL,
            static_target_id TEXT NOT NULL,
            learned_target_id TEXT,
            actual_target_id TEXT,
            actual_provider TEXT NOT NULL,
            actual_model TEXT NOT NULL,
            evidence_count INTEGER NOT NULL,
            target_example_count INTEGER NOT NULL,
            cost_coverage REAL NOT NULL,
            confidence REAL NOT NULL,
            static_utility REAL,
            learned_utility REAL,
            utility_delta REAL NOT NULL,
            estimated_savings_usd REAL,
            route_regret_usd REAL,
            activated INTEGER NOT NULL,
            abstention_reason TEXT,
            config_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            actual_validation_passed INTEGER,
            actual_cost_usd REAL,
            FOREIGN KEY (decision_id) REFERENCES routing_decisions(decision_id),
            FOREIGN KEY (static_target_id) REFERENCES routing_model_targets(target_id),
            FOREIGN KEY (learned_target_id) REFERENCES routing_model_targets(target_id),
            FOREIGN KEY (actual_target_id) REFERENCES routing_model_targets(target_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE routing_target_calibrations (
            calibration_key TEXT PRIMARY KEY,
            project_id TEXT,
            target_id TEXT NOT NULL,
            task_family TEXT NOT NULL,
            risk TEXT NOT NULL,
            capability_key TEXT NOT NULL,
            validation_rate REAL NOT NULL,
            recent_failure_rate REAL NOT NULL,
            provider_outage_rate REAL NOT NULL,
            average_cost_usd REAL,
            average_latency_seconds REAL,
            cost_coverage REAL NOT NULL,
            example_count INTEGER NOT NULL,
            effective_sample_size REAL NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (target_id) REFERENCES routing_model_targets(target_id)
        )
        """
    )
    for statement in (
        (
            "CREATE INDEX idx_routing_decisions_learning_scope ON "
            "routing_decisions(project_id, task_family, risk, capability_key, created_at)"
        ),
        (
            "CREATE INDEX idx_routing_shadow_run ON "
            "routing_shadow_evaluations(decision_id, created_at)"
        ),
        (
            "CREATE INDEX idx_routing_calibration_scope ON "
            "routing_target_calibrations(project_id, task_family, risk, capability_key, target_id)"
        ),
    ):
        conn.execute(statement)
