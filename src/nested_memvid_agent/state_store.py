from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

SCHEMA_VERSION = 6
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: str
    message: str
    session_id: str
    workspace: str
    model: str
    assistant_message: str = ""
    context_chars: int = 0
    tool_count: int = 0
    stop_reason: str = ""
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class TaskNodeRecord:
    task_id: str
    run_id: str
    title: str
    goal: str
    profile: str
    status: str
    parent_id: str | None = None
    approved: bool = False
    plan: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    dependencies: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    risk: str = "low"
    acceptance_criteria: tuple[str, ...] = ()
    attempt_count: int = 0
    failure_reason: str = ""
    diagnosis: dict[str, Any] | None = None
    retry_strategy: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class SubagentRunRecord:
    subagent_id: str
    run_id: str
    profile: str
    goal: str
    status: str
    task_id: str | None = None
    result: str = ""
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


class AgentStateStore:
    """SQLite control-plane state for runs, approvals, MCP servers, and skills."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._migrate_schema()

    def create_run(
        self,
        *,
        run_id: str,
        message: str,
        session_id: str,
        workspace: str,
        model: str,
    ) -> RunRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, status, message, session_id, workspace, model,
                    assistant_message, context_chars, tool_count, stop_reason, error,
                    created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, '', 0, 0, '', NULL, ?, ?)
                """,
                (run_id, message, session_id, workspace, model, now, now),
            )
        return self.get_run(run_id)

    def update_run(self, run_id: str, **fields: object) -> RunRecord:
        if not fields:
            return self.get_run(run_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [_encode(value) for value in fields.values()]
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE run_id = ?", values)
        return self.get_run(run_id)

    def transition_run(self, run_id: str, status: str, **fields: object) -> RunRecord:
        """Apply a guarded run lifecycle transition.

        Invalid transitions leave the current run untouched. This protects terminal
        states like cancelled from late background completions or failures.
        """
        with self._connect() as conn:
            current_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if current_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            current = _run_from_row(current_row)
            if current.status in _TERMINAL_RUN_STATUSES:
                return current
            if not _run_transition_allowed(current.status, status):
                return current
            updates = dict(fields)
            updates["status"] = status
            updates["updated_at"] = utc_now()
            assignments = ", ".join(f"{key} = ?" for key in updates)
            values = [_encode(value) for value in updates.values()]
            values.append(run_id)
            conn.execute(f"UPDATE runs SET {assignments} WHERE run_id = ?", values)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        return _run_from_row(row)

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?",
                (max(limit * 20, limit),),
            ).fetchall()

        sessions: dict[str, dict[str, Any]] = {}
        for row in rows:
            run = _run_from_row(row)
            current = sessions.get(run.session_id)
            if current is None:
                current = {
                    "session_id": run.session_id,
                    "run_count": 0,
                    "status_counts": {},
                    "latest_run_id": run.run_id,
                    "latest_status": run.status,
                    "latest_message": run.message,
                    "created_at": run.created_at,
                    "updated_at": run.updated_at,
                }
                sessions[run.session_id] = current
            current["run_count"] = int(current["run_count"]) + 1
            status_counts = current["status_counts"]
            if isinstance(status_counts, dict):
                status_counts[run.status] = int(status_counts.get(run.status, 0)) + 1
            current["created_at"] = min(str(current["created_at"]), run.created_at)
            current["updated_at"] = max(str(current["updated_at"]), run.updated_at)

        ordered = sorted(sessions.values(), key=lambda item: str(item["updated_at"]), reverse=True)
        return ordered[:limit]

    def append_run_step(self, run_id: str, type: str, payload: dict[str, Any]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO run_steps (run_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (run_id, type, json.dumps(payload), utc_now()),
            )
            return int(cursor.lastrowid or 0)

    def list_run_steps(self, run_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, type, payload_json, created_at
                FROM run_steps
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, after_id, limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "run_id": str(row["run_id"]),
                "type": str(row["type"]),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def create_approval(
        self,
        *,
        approval_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, run_id, tool_call_id, tool_name, arguments_json, risk,
                    status, decision_json, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)
                """,
                (approval_id, run_id, tool_call_id, tool_name, json.dumps(arguments), risk, now, now),
            )
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown approval: {approval_id}")
        return _approval_from_row(row)

    def list_approvals(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM approval_requests"
        params: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_approval_from_row(row) for row in rows]

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: str,
        decision: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            current = conn.execute(
                "SELECT status FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unknown approval: {approval_id}")
            if str(current["status"]) != "pending":
                return self.get_approval(approval_id)
            conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, decision_json = ?, result_json = ?, updated_at = ?
                WHERE approval_id = ?
                """,
                (status, json.dumps(decision), json.dumps(result) if result is not None else None, utc_now(), approval_id),
            )
        return self.get_approval(approval_id)

    def upsert_mcp_server(self, server: dict[str, Any]) -> dict[str, Any]:
        server_id = str(server["id"])
        now = utc_now()
        tools = list(server.get("tools", []))
        capabilities = server.get("capabilities") or sorted(
            {
                str(capability)
                for tool in tools
                for capability in list(dict(tool).get("capabilities", []))
            }
        )
        payload = {
            "name": server.get("name", server_id),
            "transport": server.get("transport", "stdio"),
            "command": server.get("command"),
            "args_json": json.dumps(server.get("args", [])),
            "env_json": json.dumps(server.get("env", {})),
            "url": server.get("url"),
            "enabled": 1 if server.get("enabled", True) else 0,
            "tools_json": json.dumps(tools),
            "status": server.get("status", "configured"),
            "error": server.get("error"),
            "last_synced_at": server.get("last_synced_at"),
            "last_seen_at": server.get("last_seen_at"),
            "tool_count": int(server.get("tool_count", len(tools))),
            "capabilities_json": json.dumps(capabilities),
            "risk_policy": server.get("risk_policy", "approval_by_default"),
            "secret_env_json": json.dumps(server.get("secret_env", {})),
            "session_state": server.get("session_state", "disconnected"),
            "last_call_at": server.get("last_call_at"),
            "last_error_at": server.get("last_error_at"),
            "failure_count": int(server.get("failure_count", 0)),
            "last_latency_ms": server.get("last_latency_ms"),
            "vetting_json": json.dumps(server.get("vetting", {})),
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_servers (
                    id, name, transport, command, args_json, env_json, url, enabled,
                    tools_json, status, error, last_synced_at, last_seen_at, tool_count,
                    capabilities_json, risk_policy, secret_env_json, session_state, last_call_at,
                    last_error_at, failure_count, last_latency_ms, vetting_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    transport = excluded.transport,
                    command = excluded.command,
                    args_json = excluded.args_json,
                    env_json = excluded.env_json,
                    url = excluded.url,
                    enabled = excluded.enabled,
                    tools_json = excluded.tools_json,
                    status = excluded.status,
                    error = excluded.error,
                    last_synced_at = excluded.last_synced_at,
                    last_seen_at = excluded.last_seen_at,
                    tool_count = excluded.tool_count,
                    capabilities_json = excluded.capabilities_json,
                    risk_policy = excluded.risk_policy,
                    secret_env_json = excluded.secret_env_json,
                    session_state = excluded.session_state,
                    last_call_at = excluded.last_call_at,
                    last_error_at = excluded.last_error_at,
                    failure_count = excluded.failure_count,
                    last_latency_ms = excluded.last_latency_ms,
                    vetting_json = excluded.vetting_json,
                    updated_at = excluded.updated_at
                """,
                (server_id, *payload.values()),
            )
        return self.get_mcp_server(server_id)

    def get_mcp_server(self, server_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown MCP server: {server_id}")
        return _mcp_from_row(row)

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM mcp_servers ORDER BY name ASC").fetchall()
        return [_mcp_from_row(row) for row in rows]

    def delete_mcp_server(self, server_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))

    def upsert_skill(self, skill: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(skill["id"])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_registry (id, name, description, path, manifest_json, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    path = excluded.path,
                    manifest_json = excluded.manifest_json,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    skill_id,
                    skill.get("name", skill_id),
                    skill.get("description", ""),
                    skill.get("path", ""),
                    json.dumps(skill.get("manifest", {})),
                    1 if skill.get("enabled", True) else 0,
                    utc_now(),
                ),
            )
        return self.get_skill(skill_id)

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM skill_registry WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown skill: {skill_id}")
        return _skill_from_row(row)

    def list_skills(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM skill_registry ORDER BY name ASC").fetchall()
        return [_skill_from_row(row) for row in rows]

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "UPDATE skill_registry SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, utc_now(), skill_id),
            )
        return self.get_skill(skill_id)

    def create_task_node(
        self,
        *,
        task_id: str,
        run_id: str,
        title: str,
        goal: str,
        profile: str = "planner",
        status: str = "queued",
        parent_id: str | None = None,
        approved: bool = False,
        plan: dict[str, Any] | None = None,
        dependencies: list[str] | tuple[str, ...] = (),
        required_tools: list[str] | tuple[str, ...] = (),
        risk: str = "low",
        acceptance_criteria: list[str] | tuple[str, ...] = (),
        attempt_count: int = 0,
        failure_reason: str = "",
        diagnosis: dict[str, Any] | None = None,
        retry_strategy: dict[str, Any] | None = None,
    ) -> TaskNodeRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_nodes (
                    task_id, run_id, parent_id, title, goal, profile, status, approved,
                    plan_json, result_json, dependencies_json, required_tools_json, risk,
                    acceptance_criteria_json, attempt_count, failure_reason, diagnosis_json,
                    retry_strategy_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    run_id,
                    parent_id,
                    title,
                    goal,
                    profile,
                    status,
                    1 if approved else 0,
                    json.dumps(plan or {}),
                    json.dumps(list(dependencies)),
                    json.dumps(list(required_tools)),
                    risk,
                    json.dumps(list(acceptance_criteria)),
                    attempt_count,
                    failure_reason,
                    json.dumps(diagnosis) if diagnosis is not None else None,
                    json.dumps(retry_strategy) if retry_strategy is not None else None,
                    now,
                    now,
                ),
            )
        return self.get_task_node(task_id)

    def update_task_node(self, task_id: str, **fields: object) -> TaskNodeRecord:
        if not fields:
            return self.get_task_node(task_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{_task_column(key)} = ?" for key in fields)
        values = [_encode(value) for value in fields.values()]
        values.append(task_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE task_nodes SET {assignments} WHERE task_id = ?", values)
        return self.get_task_node(task_id)

    def record_task_failure(
        self,
        task_id: str,
        *,
        failure_reason: str,
        diagnosis: dict[str, Any] | None = None,
        retry_strategy: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> TaskNodeRecord:
        task = self.get_task_node(task_id)
        return self.update_task_node(
            task_id,
            status="failed",
            attempt_count=task.attempt_count + 1,
            failure_reason=failure_reason,
            diagnosis=diagnosis or {},
            retry_strategy=retry_strategy or {},
            result=result or task.result,
        )

    def get_task_node(self, task_id: str) -> TaskNodeRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_nodes WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return _task_from_row(row)

    def list_task_nodes(self, run_id: str) -> list[TaskNodeRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_nodes WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def create_subagent_run(
        self,
        *,
        subagent_id: str,
        run_id: str,
        profile: str,
        goal: str,
        status: str = "queued",
        task_id: str | None = None,
    ) -> SubagentRunRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subagent_runs (
                    subagent_id, run_id, task_id, profile, goal, status, result, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', NULL, ?, ?)
                """,
                (subagent_id, run_id, task_id, profile, goal, status, now, now),
            )
        return self.get_subagent_run(subagent_id)

    def update_subagent_run(self, subagent_id: str, **fields: object) -> SubagentRunRecord:
        if not fields:
            return self.get_subagent_run(subagent_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [_encode(value) for value in fields.values()]
        values.append(subagent_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE subagent_runs SET {assignments} WHERE subagent_id = ?", values)
        return self.get_subagent_run(subagent_id)

    def get_subagent_run(self, subagent_id: str) -> SubagentRunRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM subagent_runs WHERE subagent_id = ?", (subagent_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown subagent run: {subagent_id}")
        return _subagent_from_row(row)

    def list_subagent_runs(self, run_id: str) -> list[SubagentRunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subagent_runs WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [_subagent_from_row(row) for row in rows]

    def schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        return 0 if row is None else int(row["version"])

    def _migrate_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
            current = 0 if row is None else int(row["version"])
            if current < 1:
                _apply_schema_v1(conn)
                current = 1
            if current < 2:
                _apply_schema_v2(conn)
                current = 2
            if current < 3:
                _apply_schema_v3(conn)
                current = 3
            if current < 4:
                _apply_schema_v4(conn)
                current = 4
            if current < 5:
                _apply_schema_v5(conn)
                current = 5
            if current < 6:
                _apply_schema_v6(conn)
                current = 6
            if current < SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported schema migration target: {current} -> {SCHEMA_VERSION}")
            if current == SCHEMA_VERSION:
                conn.execute(
                    """
                    INSERT INTO schema_version (id, version, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        version = excluded.version,
                        updated_at = excluded.updated_at
                    """,
                    (current, utc_now()),
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()


def _apply_schema_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            message TEXT NOT NULL,
            session_id TEXT NOT NULL,
            workspace TEXT NOT NULL,
            model TEXT NOT NULL,
            assistant_message TEXT NOT NULL DEFAULT '',
            context_chars INTEGER NOT NULL DEFAULT 0,
            tool_count INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT NOT NULL DEFAULT '',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS approval_requests (
            approval_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            risk TEXT NOT NULL,
            status TEXT NOT NULL,
            decision_json TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            transport TEXT NOT NULL,
            command TEXT,
            args_json TEXT NOT NULL,
            env_json TEXT NOT NULL,
            url TEXT,
            enabled INTEGER NOT NULL,
            tools_json TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skill_registry (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            path TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
        CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_run_steps_run_id_id ON run_steps(run_id, id);
        CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests(status);
        CREATE INDEX IF NOT EXISTS idx_approval_requests_run_id ON approval_requests(run_id);
        CREATE INDEX IF NOT EXISTS idx_mcp_servers_enabled ON mcp_servers(enabled);
        CREATE INDEX IF NOT EXISTS idx_skill_registry_enabled ON skill_registry(enabled);
        """
    )


def _apply_schema_v2(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "mcp_servers")
    for name, definition in {
        "last_synced_at": "TEXT",
        "last_seen_at": "TEXT",
        "tool_count": "INTEGER NOT NULL DEFAULT 0",
        "capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
        "risk_policy": "TEXT NOT NULL DEFAULT 'default'",
        "secret_env_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE mcp_servers ADD COLUMN {name} {definition}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_nodes (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            parent_id TEXT,
            title TEXT NOT NULL,
            goal TEXT NOT NULL,
            profile TEXT NOT NULL,
            status TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            plan_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subagent_runs (
            subagent_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT,
            profile TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT NOT NULL DEFAULT '',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_task_nodes_run_id ON task_nodes(run_id);
        CREATE INDEX IF NOT EXISTS idx_task_nodes_status ON task_nodes(status);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_run_id ON subagent_runs(run_id);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_status ON subagent_runs(status);
        """
    )


def _apply_schema_v3(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "mcp_servers")
    for name, definition in {
        "session_state": "TEXT NOT NULL DEFAULT 'disconnected'",
        "last_call_at": "TEXT",
        "last_error_at": "TEXT",
        "failure_count": "INTEGER NOT NULL DEFAULT 0",
        "last_latency_ms": "INTEGER",
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE mcp_servers ADD COLUMN {name} {definition}")


def _apply_schema_v4(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "task_nodes")
    for name, definition in {
        "dependencies_json": "TEXT NOT NULL DEFAULT '[]'",
        "required_tools_json": "TEXT NOT NULL DEFAULT '[]'",
        "risk": "TEXT NOT NULL DEFAULT 'low'",
        "acceptance_criteria_json": "TEXT NOT NULL DEFAULT '[]'",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "failure_reason": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE task_nodes ADD COLUMN {name} {definition}")


def _apply_schema_v5(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "mcp_servers")
    if "vetting_json" not in existing:
        conn.execute("ALTER TABLE mcp_servers ADD COLUMN vetting_json TEXT NOT NULL DEFAULT '{}'")


def _apply_schema_v6(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "task_nodes")
    for name, definition in {
        "diagnosis_json": "TEXT",
        "retry_strategy_json": "TEXT",
    }.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE task_nodes ADD COLUMN {name} {definition}")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _run_transition_allowed(current: str, target: str) -> bool:
    allowed = {
        "queued": {"running", "cancelled", "failed"},
        "running": {"blocked", "completed", "failed", "cancelled"},
        "blocked": {"running", "cancelled", "failed"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    }
    if current == target:
        return True
    return target in allowed.get(current, set())


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        message=str(row["message"]),
        session_id=str(row["session_id"]),
        workspace=str(row["workspace"]),
        model=str(row["model"]),
        assistant_message=str(row["assistant_message"]),
        context_chars=int(row["context_chars"]),
        tool_count=int(row["tool_count"]),
        stop_reason=str(row["stop_reason"]),
        error=None if row["error"] is None else str(row["error"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _approval_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "approval_id": str(row["approval_id"]),
        "run_id": str(row["run_id"]),
        "tool_call_id": str(row["tool_call_id"]),
        "tool_name": str(row["tool_name"]),
        "arguments": json.loads(str(row["arguments_json"])),
        "risk": str(row["risk"]),
        "status": str(row["status"]),
        "decision": _json_or_none(row["decision_json"]),
        "result": _json_or_none(row["result_json"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _mcp_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "transport": str(row["transport"]),
        "command": None if row["command"] is None else str(row["command"]),
        "args": json.loads(str(row["args_json"])),
        "env": json.loads(str(row["env_json"])),
        "url": None if row["url"] is None else str(row["url"]),
        "enabled": bool(row["enabled"]),
        "tools": json.loads(str(row["tools_json"])),
        "status": str(row["status"]),
        "error": None if row["error"] is None else str(row["error"]),
        "last_synced_at": _row_get(row, "last_synced_at"),
        "last_seen_at": _row_get(row, "last_seen_at"),
        "tool_count": int(str(_row_get(row, "tool_count", 0) or 0)),
        "capabilities": json.loads(str(_row_get(row, "capabilities_json", "[]") or "[]")),
        "risk_policy": str(_row_get(row, "risk_policy", "approval_by_default") or "approval_by_default"),
        "secret_env": json.loads(str(_row_get(row, "secret_env_json", "{}") or "{}")),
        "session_state": str(_row_get(row, "session_state", "disconnected") or "disconnected"),
        "last_call_at": _row_get(row, "last_call_at"),
        "last_error_at": _row_get(row, "last_error_at"),
        "failure_count": int(str(_row_get(row, "failure_count", 0) or 0)),
        "last_latency_ms": _row_get(row, "last_latency_ms"),
        "vetting": json.loads(str(_row_get(row, "vetting_json", "{}") or "{}")),
        "updated_at": str(row["updated_at"]),
    }


def _skill_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "description": str(row["description"]),
        "path": str(row["path"]),
        "manifest": json.loads(str(row["manifest_json"])),
        "enabled": bool(row["enabled"]),
        "updated_at": str(row["updated_at"]),
    }


def _task_from_row(row: sqlite3.Row) -> TaskNodeRecord:
    return TaskNodeRecord(
        task_id=str(row["task_id"]),
        run_id=str(row["run_id"]),
        parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
        title=str(row["title"]),
        goal=str(row["goal"]),
        profile=str(row["profile"]),
        status=str(row["status"]),
        approved=bool(row["approved"]),
        plan=json.loads(str(row["plan_json"])),
        result=_json_or_none(row["result_json"]),
        dependencies=tuple(json.loads(str(_row_get(row, "dependencies_json", "[]") or "[]"))),
        required_tools=tuple(json.loads(str(_row_get(row, "required_tools_json", "[]") or "[]"))),
        risk=str(_row_get(row, "risk", "low") or "low"),
        acceptance_criteria=tuple(json.loads(str(_row_get(row, "acceptance_criteria_json", "[]") or "[]"))),
        attempt_count=int(str(_row_get(row, "attempt_count", 0) or 0)),
        failure_reason=str(_row_get(row, "failure_reason", "") or ""),
        diagnosis=_json_or_none(_row_get(row, "diagnosis_json")),
        retry_strategy=_json_or_none(_row_get(row, "retry_strategy_json")),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _subagent_from_row(row: sqlite3.Row) -> SubagentRunRecord:
    return SubagentRunRecord(
        subagent_id=str(row["subagent_id"]),
        run_id=str(row["run_id"]),
        task_id=None if row["task_id"] is None else str(row["task_id"]),
        profile=str(row["profile"]),
        goal=str(row["goal"]),
        status=str(row["status"]),
        result=str(row["result"]),
        error=None if row["error"] is None else str(row["error"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _json_or_none(value: object) -> Any | None:
    if value is None:
        return None
    return json.loads(str(value))


def _encode(value: object) -> object:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    return value


def _row_get(row: sqlite3.Row, key: str, default: object = None) -> object:
    return row[key] if key in row.keys() else default


def _task_column(field: str) -> str:
    if field == "plan":
        return "plan_json"
    if field == "result":
        return "result_json"
    if field == "dependencies":
        return "dependencies_json"
    if field == "required_tools":
        return "required_tools_json"
    if field == "acceptance_criteria":
        return "acceptance_criteria_json"
    if field == "diagnosis":
        return "diagnosis_json"
    if field == "retry_strategy":
        return "retry_strategy_json"
    return field
