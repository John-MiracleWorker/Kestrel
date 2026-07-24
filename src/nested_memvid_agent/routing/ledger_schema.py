from __future__ import annotations

import sqlite3

from ..state_store import AgentStateStore, utc_now

ROUTING_SCHEMA_VERSION = 1


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
