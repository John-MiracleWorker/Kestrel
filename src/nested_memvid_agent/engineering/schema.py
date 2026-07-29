from __future__ import annotations

import sqlite3
from threading import RLock

from ..state_store import AgentStateStore, utc_now

ENGINEERING_SCHEMA_VERSION = 6
_ENGINEERING_SCHEMA_LOCK = RLock()


def ensure_engineering_schema(state: AgentStateStore) -> None:
    """Install the additive engineering workflow schema."""

    with _ENGINEERING_SCHEMA_LOCK, state._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS engineering_schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        row = conn.execute(
            "SELECT version FROM engineering_schema_version WHERE id = 1"
        ).fetchone()
        version = 0 if row is None else int(row["version"])
        if version > ENGINEERING_SCHEMA_VERSION:
            raise RuntimeError(
                "Engineering workflow schema is newer than this Kestrel runtime."
            )
        if version < 1:
            _apply_v1(conn)
            version = 1
        if version < 2:
            _apply_v2(conn)
            version = 2
        if version < 3:
            _apply_v3(conn)
            version = 3
        if version < 4:
            _apply_v4(conn)
            version = 4
        if version < 5:
            _apply_v5(conn)
            version = 5
        if version < 6:
            _apply_v6(conn)
            version = 6
        conn.execute(
            """
            INSERT INTO engineering_schema_version (id, version, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (version, utc_now()),
        )


def _apply_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS graph_amendments (
            amendment_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            base_graph_digest TEXT NOT NULL,
            result_graph_digest TEXT,
            requires_approval INTEGER NOT NULL DEFAULT 0,
            approval_reasons_json TEXT NOT NULL,
            actor TEXT NOT NULL,
            approved_by TEXT,
            evidence_refs_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            applied_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_graph_amendments_run_created
        ON graph_amendments(run_id, created_at, amendment_id);
        """
    )


def _apply_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_fanouts (
            fanout_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            source_task_id TEXT NOT NULL REFERENCES task_nodes(task_id) ON DELETE RESTRICT,
            task_contract_digest TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            estimated_budget_delta_usd REAL NOT NULL,
            actor TEXT NOT NULL,
            selected_candidate_id TEXT,
            created_at TEXT NOT NULL,
            selected_at TEXT
        );

        CREATE TABLE IF NOT EXISTS candidate_attempts (
            candidate_id TEXT PRIMARY KEY,
            fanout_id TEXT NOT NULL REFERENCES candidate_fanouts(fanout_id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES task_nodes(task_id) ON DELETE RESTRICT,
            task_contract_digest TEXT NOT NULL,
            workspace TEXT NOT NULL,
            branch TEXT NOT NULL,
            workspace_identity TEXT NOT NULL,
            status TEXT NOT NULL,
            candidate_digest TEXT,
            validation_id TEXT,
            validation_passed INTEGER,
            validation_evidence_refs_json TEXT NOT NULL,
            review_artifact_refs_json TEXT NOT NULL,
            reviewer_identities_json TEXT NOT NULL,
            reviewer_evidence_refs_json TEXT NOT NULL,
            changed_file_count INTEGER,
            changed_line_count INTEGER,
            risk_notes_json TEXT NOT NULL,
            actual_cost_usd REAL,
            latency_seconds REAL,
            evidence_retained INTEGER NOT NULL DEFAULT 1,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_fanout_task
        ON candidate_attempts(fanout_id, task_id);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_fanout_workspace
        ON candidate_attempts(fanout_id, workspace_identity);

        CREATE TABLE IF NOT EXISTS candidate_selections (
            selection_id TEXT PRIMARY KEY,
            fanout_id TEXT NOT NULL UNIQUE
                REFERENCES candidate_fanouts(fanout_id) ON DELETE CASCADE,
            selected_candidate_id TEXT NOT NULL
                REFERENCES candidate_attempts(candidate_id) ON DELETE RESTRICT,
            actor TEXT NOT NULL,
            ranking_json TEXT NOT NULL,
            ineligible_candidates_json TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_fanouts_run_created
        ON candidate_fanouts(run_id, created_at, fanout_id);
        """
    )


def _apply_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS browser_validations (
            validation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES task_nodes(task_id) ON DELETE RESTRICT,
            candidate_id TEXT,
            candidate_digest TEXT NOT NULL,
            image TEXT NOT NULL,
            target_url TEXT NOT NULL,
            status TEXT NOT NULL,
            failure_codes_json TEXT NOT NULL,
            network_policy_json TEXT NOT NULL,
            report_json TEXT NOT NULL,
            screenshot_sha256 TEXT,
            evidence_refs_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_browser_validations_run_created
        ON browser_validations(run_id, created_at, validation_id);

        CREATE INDEX IF NOT EXISTS idx_browser_validations_candidate
        ON browser_validations(candidate_id, created_at, validation_id);
        """
    )


def _apply_v4(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS approval_packets (
            packet_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            objective TEXT NOT NULL,
            checkpoint TEXT NOT NULL,
            packet_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            actor TEXT NOT NULL,
            decided_by TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS approval_packet_calls (
            packet_call_id TEXT PRIMARY KEY,
            packet_id TEXT NOT NULL
                REFERENCES approval_packets(packet_id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            tool_call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            call_digest TEXT NOT NULL,
            risk TEXT NOT NULL,
            capability_revision INTEGER NOT NULL,
            resource_digest TEXT NOT NULL,
            reason TEXT NOT NULL,
            resource_scope TEXT NOT NULL,
            expected_side_effect TEXT NOT NULL,
            rollback TEXT NOT NULL,
            status TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            consumed_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_packet_call_run_identity
        ON approval_packet_calls(run_id, tool_call_id);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_packet_call_ordinal
        ON approval_packet_calls(packet_id, ordinal);

        CREATE INDEX IF NOT EXISTS idx_approval_packets_run_created
        ON approval_packets(run_id, created_at, packet_id);
        """
    )


def _apply_v5(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS benchmark_cases (
            case_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            task_family TEXT NOT NULL,
            risk TEXT NOT NULL,
            fixture_json TEXT NOT NULL,
            acceptance_criteria_json TEXT NOT NULL,
            case_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS benchmark_replays (
            replay_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL REFERENCES benchmark_cases(case_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
            route_policy_id TEXT,
            context_strategy TEXT NOT NULL,
            baseline TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_benchmark_cases_project_created
        ON benchmark_cases(project_id, created_at, case_id);

        CREATE INDEX IF NOT EXISTS idx_benchmark_replays_case_created
        ON benchmark_replays(case_id, created_at, replay_id);
        """
    )


def _apply_v6(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS github_change_requests (
            request_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
            project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
            review_id TEXT NOT NULL,
            validation_id TEXT NOT NULL,
            candidate_digest TEXT NOT NULL,
            source_head_sha TEXT NOT NULL,
            reviewed_commit_sha TEXT,
            base_branch TEXT NOT NULL,
            head_branch TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            external_number INTEGER,
            external_url TEXT,
            publish_receipt_json TEXT NOT NULL,
            recovery_run_id TEXT,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS github_feedback_events (
            event_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL
                REFERENCES github_change_requests(request_id) ON DELETE CASCADE,
            external_event_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(request_id, external_event_id)
        );

        CREATE INDEX IF NOT EXISTS idx_github_requests_run_created
        ON github_change_requests(run_id, created_at, request_id);

        CREATE INDEX IF NOT EXISTS idx_github_feedback_request_created
        ON github_feedback_events(request_id, created_at, event_id);
        """
    )
