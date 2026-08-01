from __future__ import annotations

import sqlite3

from ..state_store import AgentStateStore, utc_now

ROUTING_SCHEMA_VERSION = 3


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
        if current < 3:
            _apply_routing_schema_v3(conn)
            current = 3
        if current >= 3:
            _ensure_routing_schema_v3_guards(conn)
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


def _apply_routing_schema_v3(conn: sqlite3.Connection) -> None:
    """Add immutable, owner-confirmed private-LAN discovery evidence."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_lan_scans (
            scan_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (
                status IN (
                    'draft', 'running', 'cancelling', 'cancelled',
                    'completed', 'failed', 'interrupted'
                )
            ),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            owner_principal TEXT NOT NULL,
            confirmed_interface_id TEXT NOT NULL,
            network TEXT NOT NULL,
            limits_json TEXT NOT NULL,
            limits_digest TEXT NOT NULL,
            preview_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            cancel_reason TEXT,
            terminal_reason TEXT,
            candidate_count INTEGER CHECK (candidate_count IS NULL OR candidate_count >= 0),
            error_count INTEGER CHECK (error_count IS NULL OR error_count >= 0),
            timeout_count INTEGER CHECK (timeout_count IS NULL OR timeout_count >= 0),
            terminal_receipt_json TEXT,
            terminal_receipt_digest TEXT,
            CHECK (
                (terminal_receipt_json IS NULL AND terminal_receipt_digest IS NULL)
                OR
                (terminal_receipt_json IS NOT NULL AND terminal_receipt_digest IS NOT NULL)
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_lan_observations (
            scan_id TEXT NOT NULL,
            endpoint_id TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('mdns', 'active', 'manual')),
            interface_id TEXT NOT NULL,
            address TEXT NOT NULL,
            port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
            api_shape TEXT,
            tls_enabled INTEGER NOT NULL CHECK (tls_enabled IN (0, 1)),
            certificate_sha256 TEXT,
            catalog_digest TEXT,
            capability_digest TEXT,
            public_payload_json TEXT NOT NULL,
            freshness_timestamp TEXT NOT NULL,
            error_category TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scan_id, endpoint_id),
            UNIQUE (scan_id, endpoint_id),
            FOREIGN KEY (scan_id) REFERENCES routing_lan_scans(scan_id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_lan_scan_events (
            scan_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scan_id, sequence),
            FOREIGN KEY (scan_id) REFERENCES routing_lan_scans(scan_id) ON DELETE RESTRICT
        )
        """
    )
    for statement in (
        (
            "CREATE INDEX IF NOT EXISTS idx_routing_lan_scans_status_updated ON "
            "routing_lan_scans(status, updated_at, scan_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_routing_lan_observations_scan_freshness ON "
            "routing_lan_observations(scan_id, freshness_timestamp, endpoint_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_routing_lan_scan_events_poll ON "
            "routing_lan_scan_events(scan_id, sequence)"
        ),
    ):
        conn.execute(statement)


def _ensure_routing_schema_v3_guards(conn: sqlite3.Connection) -> None:
    """Install idempotent database-level terminal evidence guards."""

    statements = (
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_fields_insert_require_terminal
        BEFORE INSERT ON routing_lan_scans
        WHEN NEW.status NOT IN ('cancelled', 'completed', 'failed', 'interrupted')
         AND (
            NEW.finished_at IS NOT NULL
            OR NEW.terminal_reason IS NOT NULL
            OR NEW.candidate_count IS NOT NULL
            OR NEW.error_count IS NOT NULL
            OR NEW.timeout_count IS NOT NULL
            OR NEW.terminal_receipt_json IS NOT NULL
            OR NEW.terminal_receipt_digest IS NOT NULL
         )
        BEGIN
            SELECT RAISE(ABORT, 'terminal_fields_require_terminal_state');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_fields_require_terminal
        BEFORE UPDATE OF
            status, finished_at, terminal_reason, candidate_count, error_count,
            timeout_count, terminal_receipt_json, terminal_receipt_digest
        ON routing_lan_scans
        WHEN NEW.status NOT IN ('cancelled', 'completed', 'failed', 'interrupted')
         AND (
            NEW.finished_at IS NOT NULL
            OR NEW.terminal_reason IS NOT NULL
            OR NEW.candidate_count IS NOT NULL
            OR NEW.error_count IS NOT NULL
            OR NEW.timeout_count IS NOT NULL
            OR NEW.terminal_receipt_json IS NOT NULL
            OR NEW.terminal_receipt_digest IS NOT NULL
         )
        BEGIN
            SELECT RAISE(ABORT, 'terminal_fields_require_terminal_state');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_scan_update_immutable
        BEFORE UPDATE ON routing_lan_scans
        WHEN OLD.status IN ('cancelled', 'completed', 'failed', 'interrupted')
        BEGIN
            SELECT RAISE(ABORT, 'terminal_lan_scan_immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_scan_delete_immutable
        BEFORE DELETE ON routing_lan_scans
        WHEN OLD.status IN ('cancelled', 'completed', 'failed', 'interrupted')
        BEGIN
            SELECT RAISE(ABORT, 'terminal_lan_scan_immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_observation_insert_immutable
        BEFORE INSERT ON routing_lan_observations
        WHEN EXISTS (
            SELECT 1 FROM routing_lan_scans
            WHERE scan_id = NEW.scan_id
              AND status IN ('cancelled', 'completed', 'failed', 'interrupted')
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal_lan_scan_immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_observation_update_immutable
        BEFORE UPDATE ON routing_lan_observations
        WHEN EXISTS (
                SELECT 1 FROM routing_lan_scans
                WHERE scan_id = OLD.scan_id
                  AND status IN ('cancelled', 'completed', 'failed', 'interrupted')
             )
          OR EXISTS (
                SELECT 1 FROM routing_lan_scans
                WHERE scan_id = NEW.scan_id
                  AND status IN ('cancelled', 'completed', 'failed', 'interrupted')
             )
        BEGIN
            SELECT RAISE(ABORT, 'terminal_lan_scan_immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_observation_delete_immutable
        BEFORE DELETE ON routing_lan_observations
        WHEN EXISTS (
            SELECT 1 FROM routing_lan_scans
            WHERE scan_id = OLD.scan_id
              AND status IN ('cancelled', 'completed', 'failed', 'interrupted')
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal_lan_scan_immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_event_insert_immutable
        BEFORE INSERT ON routing_lan_scan_events
        WHEN EXISTS (
            SELECT 1 FROM routing_lan_scans
            WHERE scan_id = NEW.scan_id
              AND status IN ('cancelled', 'completed', 'failed', 'interrupted')
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal_lan_scan_immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_event_update_immutable
        BEFORE UPDATE ON routing_lan_scan_events
        WHEN EXISTS (
                SELECT 1 FROM routing_lan_scans
                WHERE scan_id = OLD.scan_id
                  AND status IN ('cancelled', 'completed', 'failed', 'interrupted')
             )
          OR EXISTS (
                SELECT 1 FROM routing_lan_scans
                WHERE scan_id = NEW.scan_id
                  AND status IN ('cancelled', 'completed', 'failed', 'interrupted')
             )
        BEGIN
            SELECT RAISE(ABORT, 'terminal_lan_scan_immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_routing_lan_terminal_event_delete_immutable
        BEFORE DELETE ON routing_lan_scan_events
        WHEN EXISTS (
            SELECT 1 FROM routing_lan_scans
            WHERE scan_id = OLD.scan_id
              AND status IN ('cancelled', 'completed', 'failed', 'interrupted')
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal_lan_scan_immutable');
        END
        """,
    )
    for statement in statements:
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
