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

SCHEMA_VERSION = 1


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
        payload = {
            "name": server.get("name", server_id),
            "transport": server.get("transport", "stdio"),
            "command": server.get("command"),
            "args_json": json.dumps(server.get("args", [])),
            "env_json": json.dumps(server.get("env", {})),
            "url": server.get("url"),
            "enabled": 1 if server.get("enabled", True) else 0,
            "tools_json": json.dumps(server.get("tools", [])),
            "status": server.get("status", "configured"),
            "error": server.get("error"),
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_servers (
                    id, name, transport, command, args_json, env_json, url, enabled,
                    tools_json, status, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                conn.execute(
                    """
                    INSERT INTO schema_version (id, version, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        version = excluded.version,
                        updated_at = excluded.updated_at
                    """,
                    (SCHEMA_VERSION, utc_now()),
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


def _json_or_none(value: object) -> Any | None:
    if value is None:
        return None
    return json.loads(str(value))


def _encode(value: object) -> object:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    return value
