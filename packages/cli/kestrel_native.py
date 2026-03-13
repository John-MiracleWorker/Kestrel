from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import logging
import math
import os
import platform
import sqlite3
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - packaging guard
    httpx = None

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - packaging guard
    yaml = None


LOGGER = logging.getLogger("kestrel.native")
DEFAULT_CONTROL_HOST = os.getenv("KESTREL_CONTROL_HOST", "127.0.0.1")
DEFAULT_CONTROL_PORT = int(os.getenv("KESTREL_CONTROL_PORT", "8749"))


DEFAULT_CONFIG = {
    "runtime": {
        "mode": "native",
        "allow_loopback_http": False,
        "single_user": True,
    },
    "heartbeat": {
        "interval_seconds": 300,
        "quiet_hours": {
            "enabled": False,
            "start": "23:00",
            "end": "07:00",
        },
    },
    "permissions": {
        "broad_local_control": True,
        "require_approval_for_mutations": True,
    },
    "models": {
        "preferred_provider": "auto",
        "preferred_model": "",
        "ollama_url": "http://127.0.0.1:11434",
        "lmstudio_url": "http://127.0.0.1:1234",
    },
    "watch": {
        "poll_interval_seconds": 5,
    },
}


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class KestrelPaths:
    home: Path
    run_dir: Path
    logs_dir: Path
    audit_dir: Path
    state_dir: Path
    memory_dir: Path
    watchlist_dir: Path
    artifacts_dir: Path
    cache_dir: Path
    models_dir: Path
    control_socket: Path
    control_host: str
    control_port: int
    sqlite_db: Path
    config_yml: Path
    heartbeat_md: Path
    workspace_md: Path
    watchlist_yml: Path
    heartbeat_state_json: Path
    runtime_profile_json: Path


def resolve_paths(home_override: str | None = None) -> KestrelPaths:
    home = Path(home_override or os.getenv("KESTREL_HOME") or "~/.kestrel").expanduser()
    run_dir = home / "run"
    logs_dir = home / "logs"
    audit_dir = home / "audit"
    state_dir = home / "state"
    memory_dir = home / "memory"
    watchlist_dir = home / "watchlist"
    artifacts_dir = home / "artifacts"
    cache_dir = home / "cache"
    models_dir = home / "models"
    return KestrelPaths(
        home=home,
        run_dir=run_dir,
        logs_dir=logs_dir,
        audit_dir=audit_dir,
        state_dir=state_dir,
        memory_dir=memory_dir,
        watchlist_dir=watchlist_dir,
        artifacts_dir=artifacts_dir,
        cache_dir=cache_dir,
        models_dir=models_dir,
        control_socket=run_dir / "control.sock",
        control_host=os.getenv("KESTREL_CONTROL_HOST", DEFAULT_CONTROL_HOST),
        control_port=int(os.getenv("KESTREL_CONTROL_PORT", str(DEFAULT_CONTROL_PORT))),
        sqlite_db=state_dir / "kestrel.db",
        config_yml=home / "config.yml",
        heartbeat_md=home / "HEARTBEAT.md",
        workspace_md=home / "WORKSPACE.md",
        watchlist_yml=watchlist_dir / "paths.yml",
        heartbeat_state_json=state_dir / "heartbeat.json",
        runtime_profile_json=state_dir / "runtime_profile.json",
    )


def _write_text_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def ensure_home_layout(home_override: str | None = None) -> KestrelPaths:
    paths = resolve_paths(home_override=home_override)
    for directory in (
        paths.home,
        paths.run_dir,
        paths.logs_dir,
        paths.audit_dir,
        paths.state_dir,
        paths.memory_dir,
        paths.watchlist_dir,
        paths.artifacts_dir,
        paths.cache_dir,
        paths.models_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    config_text = """runtime:
  mode: native
  allow_loopback_http: false
  single_user: true
heartbeat:
  interval_seconds: 300
  quiet_hours:
    enabled: false
    start: "23:00"
    end: "07:00"
permissions:
  broad_local_control: true
  require_approval_for_mutations: true
models:
  preferred_provider: auto
  preferred_model: ""
  ollama_url: http://127.0.0.1:11434
  lmstudio_url: http://127.0.0.1:1234
watch:
  poll_interval_seconds: 5
"""
    heartbeat_text = """# Kestrel Heartbeat Tasks

## Every heartbeat
- Refresh runtime profile
- Sync markdown memory
- Reindex watched files
"""
    workspace_text = """# Active Workspace Context

## Current Projects
- Describe what you are working on here.
"""
    watchlist_text = """paths:
  - ~/Downloads
  - ~/Desktop
"""
    _write_text_if_missing(paths.config_yml, config_text)
    _write_text_if_missing(paths.heartbeat_md, heartbeat_text)
    _write_text_if_missing(paths.workspace_md, workspace_text)
    _write_text_if_missing(paths.watchlist_yml, watchlist_text)
    return paths


def load_native_config(paths: KestrelPaths | None = None) -> dict[str, Any]:
    paths = paths or ensure_home_layout()
    if not paths.config_yml.exists():
        return dict(DEFAULT_CONFIG)
    if yaml is None:
        LOGGER.warning("PyYAML unavailable; using native config defaults")
        return dict(DEFAULT_CONFIG)
    try:
        raw = yaml.safe_load(paths.config_yml.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("Failed to parse %s: %s", paths.config_yml, exc)
        raw = {}
    return _deep_merge(DEFAULT_CONFIG, raw)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)


def configure_daemon_logging(paths: KestrelPaths) -> None:
    if any(isinstance(handler, RotatingFileHandler) for handler in logging.getLogger().handlers):
        return
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    daemon_log = paths.logs_dir / "daemon.log"
    handler = RotatingFileHandler(
        daemon_log,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s")
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(handler)


def control_socket_available(paths: KestrelPaths | None = None) -> bool:
    paths = paths or ensure_home_layout()
    if os.name == "nt":
        try:
            with socket.create_connection((paths.control_host, paths.control_port), timeout=1):
                return True
        except OSError:
            return False
    return paths.control_socket.exists()


class ControlClientError(RuntimeError):
    pass


async def send_control_stream(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    paths: KestrelPaths | None = None,
    timeout_seconds: float = 30,
) -> Any:
    paths = paths or ensure_home_layout()
    if os.name != "nt" and not paths.control_socket.exists():
        raise ControlClientError(f"Control socket not found at {paths.control_socket}")

    request_id = str(uuid.uuid4())
    if os.name == "nt":
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(paths.control_host, paths.control_port),
            timeout=timeout_seconds,
        )
    else:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(paths.control_socket)),
            timeout=timeout_seconds,
        )
    payload = {
        "request_id": request_id,
        "method": method,
        "params": params or {},
    }
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()

    try:
        while True:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
            if not raw:
                break
            response = json.loads(raw.decode("utf-8"))
            if response.get("request_id") != request_id:
                continue
            if not response.get("ok", False):
                error = response.get("error") or {}
                raise ControlClientError(error.get("message") or "Unknown control API failure")
            yield response
            if response.get("done"):
                break
    finally:
        writer.close()
        await writer.wait_closed()


async def send_control_request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    paths: KestrelPaths | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    async for response in send_control_stream(
        method,
        params=params,
        paths=paths,
        timeout_seconds=timeout_seconds,
    ):
        if "result" in response:
            return response["result"]
    raise ControlClientError(f"No result received for {method}")


class StateStore(abc.ABC):
    @abc.abstractmethod
    def initialize(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def set_daemon_state(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def get_daemon_state(self) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def create_task(self, *, goal: str, kind: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_tasks(self, limit: int = 25) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abc.abstractmethod
    def recover_inflight_tasks(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def set_runtime_profile(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def get_runtime_profile(self) -> dict[str, Any]:
        raise NotImplementedError


class EventJournal(abc.ABC):
    @abc.abstractmethod
    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError


class VectorMemoryStore(abc.ABC):
    @abc.abstractmethod
    def upsert_text(
        self,
        *,
        doc_id: str,
        namespace: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def search_text(self, *, namespace: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        raise NotImplementedError


class CredentialStore(abc.ABC):
    @abc.abstractmethod
    def get_secret(self, service: str, account: str) -> str | None:
        raise NotImplementedError

    @abc.abstractmethod
    def set_secret(self, service: str, account: str, secret: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def delete_secret(self, service: str, account: str) -> None:
        raise NotImplementedError


class RuntimePolicy(abc.ABC):
    @abc.abstractmethod
    def runtime_profile(self) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def evaluate_command(self, command: str) -> dict[str, Any]:
        raise NotImplementedError


class _SQLiteBase:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key, value in list(data.items()):
            if key.endswith("_json") and isinstance(value, str):
                try:
                    data[key[:-5]] = json.loads(value)
                except json.JSONDecodeError:
                    data[key[:-5]] = value
        return data


class SQLiteStateStore(_SQLiteBase, StateStore):
    def initialize(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS daemon_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_profile (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'native',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                command TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decision_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS paired_nodes (
                node_id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                platform TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL,
                health TEXT NOT NULL DEFAULT 'unknown',
                address TEXT NOT NULL DEFAULT '',
                auth_json TEXT NOT NULL DEFAULT '{}',
                workspace_binding TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        self._conn.commit()

    def set_daemon_state(self, payload: dict[str, Any]) -> None:
        stamp = _now_iso()
        self._conn.execute(
            """
            INSERT INTO daemon_state (key, value_json, updated_at)
            VALUES ('daemon', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (json.dumps(payload), stamp),
        )
        self._conn.commit()

    def get_daemon_state(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value_json FROM daemon_state WHERE key = 'daemon'"
        ).fetchone()
        if not row:
            return {}
        return json.loads(row["value_json"])

    def create_task(
        self,
        *,
        goal: str,
        kind: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        stamp = _now_iso()
        payload = {
            "id": task_id,
            "kind": kind,
            "goal": goal,
            "status": "queued",
            "source": "native",
            "result": {},
            "error": "",
            "metadata": metadata or {},
            "created_at": stamp,
            "updated_at": stamp,
        }
        self._conn.execute(
            """
            INSERT INTO tasks (id, kind, goal, status, source, result_json, error, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"],
                payload["kind"],
                payload["goal"],
                payload["status"],
                payload["source"],
                json.dumps(payload["result"]),
                payload["error"],
                json.dumps(payload["metadata"]),
                payload["created_at"],
                payload["updated_at"],
            ),
        )
        self._conn.commit()
        return payload

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_task(task_id)
        if not current:
            raise KeyError(f"Unknown task {task_id}")
        current_status = status or current["status"]
        current_result = result if result is not None else current.get("result") or {}
        current_error = error if error is not None else current.get("error") or ""
        merged_metadata = dict(current.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        stamp = _now_iso()
        self._conn.execute(
            """
            UPDATE tasks
            SET status = ?, result_json = ?, error = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                current_status,
                json.dumps(current_result),
                current_error,
                json.dumps(merged_metadata),
                stamp,
                task_id,
            ),
        )
        self._conn.commit()
        return self.get_task(task_id) or {}

    def list_tasks(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM tasks
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            task = self._row_to_dict(row) or {}
            task["result"] = task.get("result") or json.loads(row["result_json"])
            task["metadata"] = task.get("metadata") or json.loads(row["metadata_json"])
            tasks.append(task)
        return tasks

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        task = self._row_to_dict(row) or {}
        task["result"] = task.get("result") or json.loads(row["result_json"])
        task["metadata"] = task.get("metadata") or json.loads(row["metadata_json"])
        return task

    def recover_inflight_tasks(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT id FROM tasks
            WHERE status IN ('queued', 'running')
            """
        ).fetchall()
        recovered: list[dict[str, Any]] = []
        for row in rows:
            task = self.update_task(
                row["id"],
                status="recovering",
                metadata={"recovered_at": _now_iso()},
            )
            recovered.append(task)
        return recovered

    def set_runtime_profile(self, payload: dict[str, Any]) -> None:
        stamp = _now_iso()
        self._conn.execute(
            """
            INSERT INTO runtime_profile (key, value_json, updated_at)
            VALUES ('runtime', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (json.dumps(payload), stamp),
        )
        self._conn.commit()

    def get_runtime_profile(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value_json FROM runtime_profile WHERE key = 'runtime'"
        ).fetchone()
        if not row:
            return {}
        return json.loads(row["value_json"])

    def create_approval(self, *, task_id: str, operation: str, command: str) -> dict[str, Any]:
        approval_id = str(uuid.uuid4())
        stamp = _now_iso()
        payload = {
            "id": approval_id,
            "task_id": task_id,
            "operation": operation,
            "command": command,
            "status": "pending",
            "created_at": stamp,
            "decided_at": None,
            "decision": {},
        }
        self._conn.execute(
            """
            INSERT INTO approvals (id, task_id, operation, command, status, created_at, decided_at, decision_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"],
                payload["task_id"],
                payload["operation"],
                payload["command"],
                payload["status"],
                payload["created_at"],
                payload["decided_at"],
                json.dumps(payload["decision"]),
            ),
        )
        self._conn.commit()
        return payload

    def list_pending_approvals(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["decision"] = json.loads(row["decision_json"])
            results.append(payload)
        return results

    def resolve_approval(self, approval_id: str, approved: bool) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if not row:
            return None
        status = "approved" if approved else "denied"
        decided_at = _now_iso()
        decision = {"approved": approved, "decided_at": decided_at}
        self._conn.execute(
            """
            UPDATE approvals
            SET status = ?, decided_at = ?, decision_json = ?
            WHERE id = ?
            """,
            (status, decided_at, json.dumps(decision), approval_id),
        )
        self._conn.commit()
        payload = dict(row)
        payload["status"] = status
        payload["decided_at"] = decided_at
        payload["decision"] = decision
        return payload

    def list_approvals(self, *, task_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            values.append(task_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM approvals {where} ORDER BY created_at DESC",
            values,
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["decision"] = json.loads(row["decision_json"])
            results.append(payload)
        return results

    def upsert_paired_node(
        self,
        *,
        node_id: str,
        node_type: str,
        capabilities: list[str] | None = None,
        platform_name: str = "",
        health: str = "unknown",
        address: str = "",
        auth: dict[str, Any] | None = None,
        workspace_binding: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "node_id": node_id,
            "node_type": node_type,
            "capabilities": capabilities or [],
            "platform": platform_name,
            "last_seen_at": _now_iso(),
            "health": health,
            "address": address,
            "auth": auth or {},
            "workspace_binding": workspace_binding,
            "metadata": metadata or {},
        }
        self._conn.execute(
            """
            INSERT INTO paired_nodes (
                node_id, node_type, capabilities_json, platform, last_seen_at, health, address,
                auth_json, workspace_binding, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                node_type=excluded.node_type,
                capabilities_json=excluded.capabilities_json,
                platform=excluded.platform,
                last_seen_at=excluded.last_seen_at,
                health=excluded.health,
                address=excluded.address,
                auth_json=excluded.auth_json,
                workspace_binding=excluded.workspace_binding,
                metadata_json=excluded.metadata_json
            """,
            (
                payload["node_id"],
                payload["node_type"],
                json.dumps(payload["capabilities"]),
                payload["platform"],
                payload["last_seen_at"],
                payload["health"],
                payload["address"],
                json.dumps(payload["auth"]),
                payload["workspace_binding"],
                json.dumps(payload["metadata"]),
            ),
        )
        self._conn.commit()
        return payload

    def list_paired_nodes(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM paired_nodes ORDER BY last_seen_at DESC"
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["capabilities"] = json.loads(row["capabilities_json"])
            payload["auth"] = json.loads(row["auth_json"])
            payload["metadata"] = json.loads(row["metadata_json"])
            results.append(payload)
        return results


class SQLiteEventJournal(_SQLiteBase, EventJournal):
    def initialize(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_task_events_task_seq
                ON task_events(task_id, seq);
            """
        )
        self._conn.commit()

    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        envelope = dict(payload)
        envelope.setdefault("type", event_type)
        envelope.setdefault("task_id", task_id)
        envelope.setdefault("created_at", _now_iso())
        cursor = self._conn.execute(
            """
            INSERT INTO task_events (task_id, event_type, created_at, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, event_type, envelope["created_at"], json.dumps(envelope)),
        )
        self._conn.commit()
        envelope["seq"] = cursor.lastrowid
        return envelope

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, payload_json FROM task_events WHERE task_id = ? ORDER BY seq ASC",
            (task_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["seq"] = row["seq"]
            events.append(payload)
        return events


class SQLiteExactVectorStore(_SQLiteBase, VectorMemoryStore):
    def initialize(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_vectors (
                doc_id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                content TEXT NOT NULL,
                vector_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memory_vectors_namespace
                ON memory_vectors(namespace, updated_at DESC);
            """
        )
        self._conn.commit()

    @staticmethod
    def _embed_text(text: str, dims: int = 128) -> list[float]:
        vector = [0.0] * dims
        for token in (text or "").lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "big") % dims
            sign = -1.0 if digest[2] % 2 else 1.0
            vector[index] += sign
        magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / magnitude for value in vector]

    @staticmethod
    def _cosine_similarity(lhs: list[float], rhs: list[float]) -> float:
        return sum(a * b for a, b in zip(lhs, rhs))

    def upsert_text(
        self,
        *,
        doc_id: str,
        namespace: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        vector = self._embed_text(content)
        self._conn.execute(
            """
            INSERT INTO memory_vectors (doc_id, namespace, content, vector_json, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                namespace = excluded.namespace,
                content = excluded.content,
                vector_json = excluded.vector_json,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                doc_id,
                namespace,
                content,
                json.dumps(vector),
                json.dumps(metadata or {}),
                _now_iso(),
            ),
        )
        self._conn.commit()

    def search_text(self, *, namespace: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_vector = self._embed_text(query)
        rows = self._conn.execute(
            "SELECT doc_id, content, vector_json, metadata_json FROM memory_vectors WHERE namespace = ?",
            (namespace,),
        ).fetchall()
        ranked: list[dict[str, Any]] = []
        for row in rows:
            candidate = json.loads(row["vector_json"])
            ranked.append(
                {
                    "doc_id": row["doc_id"],
                    "content": row["content"],
                    "score": self._cosine_similarity(query_vector, candidate),
                    "metadata": json.loads(row["metadata_json"]),
                }
            )
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[:limit]


class MacOSKeychainCredentialStore(CredentialStore):
    def __init__(self) -> None:
        self._enabled = platform.system() == "Darwin"

    def get_secret(self, service: str, account: str) -> str | None:
        if not self._enabled:
            return None
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def set_secret(self, service: str, account: str, secret: str) -> None:
        if not self._enabled:
            raise RuntimeError("macOS Keychain is unavailable on this platform")
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", secret],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Failed to write secret to Keychain")

    def delete_secret(self, service: str, account: str) -> None:
        if not self._enabled:
            return
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            text=True,
        )


class NativeRuntimePolicy(RuntimePolicy):
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def runtime_profile(self) -> dict[str, Any]:
        return {
            "runtime_mode": "native",
            "policy_name": "NativeRuntimePolicy",
            "policy_version": "1",
            "docker_enabled": False,
            "native_enabled": True,
            "hybrid_fallback_visible": False,
            "host_mounts": [{"path": str(Path.home()), "mode": "read-write"}],
            "runtime_capabilities": {
                "unix_socket_control": "true",
                "sqlite_wal": "true",
                "docker_required": "false",
                "loopback_http_primary": "false",
            },
        }

    def evaluate_command(self, command: str) -> dict[str, Any]:
        normalized = (command or "").strip().lower()
        destructive_markers = (
            "rm ",
            "mv ",
            "chmod ",
            "chown ",
            "git reset --hard",
            "diskutil",
            "launchctl unload",
            "killall",
        )
        mutating_markers = destructive_markers + (
            "touch ",
            "mkdir ",
            "rmdir ",
            "cp ",
            "git commit",
            "git checkout ",
            "git clean",
            "pip install",
            "npm install",
            "brew install",
        )
        broad_control = bool(self.config.get("permissions", {}).get("broad_local_control", True))
        require_approval = bool(
            self.config.get("permissions", {}).get("require_approval_for_mutations", True)
        )
        risk_class = "read_only"
        approval_required = False
        if any(marker in normalized for marker in destructive_markers):
            risk_class = "destructive"
            approval_required = True
        elif any(marker in normalized for marker in mutating_markers):
            risk_class = "mutating"
            approval_required = require_approval
        allowed = broad_control or risk_class == "read_only"
        return {
            "allowed": allowed,
            "risk_class": risk_class,
            "approval_required": approval_required,
        }


async def _http_get_json(url: str, timeout_seconds: float = 2.5) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is required for model detection")
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def _http_post_json(url: str, payload: dict[str, Any], timeout_seconds: float = 60) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is required for model inference")
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


async def detect_local_model_runtime(config: dict[str, Any]) -> dict[str, Any]:
    models_cfg = config.get("models", {})
    ollama_url = str(models_cfg.get("ollama_url") or DEFAULT_CONFIG["models"]["ollama_url"]).rstrip("/")
    lmstudio_url = str(models_cfg.get("lmstudio_url") or DEFAULT_CONFIG["models"]["lmstudio_url"]).rstrip("/")
    providers: dict[str, Any] = {}

    try:
        payload = await _http_get_json(f"{ollama_url}/api/tags")
        models = [item.get("name", "") for item in payload.get("models", []) if item.get("name")]
        providers["ollama"] = {"ready": True, "models": models, "base_url": ollama_url}
    except Exception as exc:
        providers["ollama"] = {"ready": False, "models": [], "base_url": ollama_url, "error": str(exc)}

    try:
        payload = await _http_get_json(f"{lmstudio_url}/v1/models")
        models = [item.get("id", "") for item in payload.get("data", []) if item.get("id")]
        providers["lmstudio"] = {"ready": True, "models": models, "base_url": lmstudio_url}
    except Exception as exc:
        providers["lmstudio"] = {"ready": False, "models": [], "base_url": lmstudio_url, "error": str(exc)}

    preferred_provider = models_cfg.get("preferred_provider", "auto")
    preferred_model = models_cfg.get("preferred_model", "")
    default_provider = ""
    default_model = ""
    if preferred_provider != "auto" and providers.get(preferred_provider, {}).get("ready"):
        default_provider = preferred_provider
        default_model = preferred_model or providers[preferred_provider]["models"][:1][0]
    else:
        for name in ("ollama", "lmstudio"):
            if providers.get(name, {}).get("ready") and providers[name]["models"]:
                default_provider = name
                default_model = preferred_model or providers[name]["models"][0]
                break
    return {
        "preferred_provider": preferred_provider,
        "preferred_model": preferred_model,
        "default_provider": default_provider,
        "default_model": default_model,
        "providers": providers,
    }


async def complete_local_prompt(
    *,
    prompt: str,
    config: dict[str, Any],
    system_prompt: str = "You are Kestrel, a local autonomous agent OS focused on concise, actionable assistance.",
) -> dict[str, Any]:
    fake = os.getenv("KESTREL_FAKE_MODEL_RESPONSE")
    if fake:
        return {
            "provider": "fake",
            "model": "fake",
            "content": fake,
        }

    runtime = await detect_local_model_runtime(config)
    provider = runtime.get("default_provider")
    model = runtime.get("default_model")
    if not provider or not model:
        raise RuntimeError(
            "No local model runtime is available. Start Ollama or LM Studio, then retry."
        )

    if provider == "ollama":
        base_url = runtime["providers"]["ollama"]["base_url"]
        payload = await _http_post_json(
            f"{base_url}/api/chat",
            {
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        content = ((payload.get("message") or {}).get("content") or "").strip()
    elif provider == "lmstudio":
        base_url = runtime["providers"]["lmstudio"]["base_url"]
        payload = await _http_post_json(
            f"{base_url}/v1/chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
        )
        choices = payload.get("choices") or []
        content = (((choices[0] or {}).get("message") or {}).get("content") or "").strip() if choices else ""
    else:  # pragma: no cover - defensive
        raise RuntimeError(f"Unsupported local model provider: {provider}")

    if not content:
        raise RuntimeError(f"{provider} returned an empty completion")
    return {
        "provider": provider,
        "model": model,
        "content": content,
    }


def sync_markdown_memory(paths: KestrelPaths, vector_store: VectorMemoryStore) -> dict[str, Any]:
    indexed = 0
    namespaces: set[str] = set()
    for file_path in sorted(paths.memory_dir.rglob("*.md")):
        if not file_path.is_file():
            continue
        relative_parent = file_path.parent.relative_to(paths.memory_dir)
        namespace = str(relative_parent).replace("\\", "/") or "root"
        doc_id = str(file_path.relative_to(paths.home)).replace("\\", "/")
        content = file_path.read_text(encoding="utf-8")
        vector_store.upsert_text(
            doc_id=doc_id,
            namespace=namespace,
            content=content,
            metadata={
                "path": doc_id,
                "mtime_ns": file_path.stat().st_mtime_ns,
            },
        )
        namespaces.add(namespace)
        indexed += 1
    return {
        "indexed_files": indexed,
        "namespaces": sorted(namespaces),
        "synced_at": _now_iso(),
    }


def build_doctor_report(
    *,
    paths: KestrelPaths,
    config: dict[str, Any],
    runtime_profile: dict[str, Any],
    model_runtime: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "name": "platform",
            "status": "ok" if platform.system() == "Darwin" else "warning",
            "detail": f"Running on {platform.system()}",
        }
    )
    checks.append(
        {
            "name": "control_socket",
            "status": "ok" if control_socket_available(paths) else "warning",
            "detail": (
                f"tcp://{paths.control_host}:{paths.control_port}"
                if os.name == "nt"
                else str(paths.control_socket)
            ),
        }
    )
    checks.append(
        {
            "name": "sqlite_state",
            "status": "ok" if paths.sqlite_db.exists() else "warning",
            "detail": str(paths.sqlite_db),
        }
    )
    checks.append(
        {
            "name": "keychain",
            "status": "ok" if platform.system() == "Darwin" else "warning",
            "detail": "macOS Keychain available" if platform.system() == "Darwin" else "Keychain not available",
        }
    )
    model_ready = any(info.get("ready") for info in model_runtime.get("providers", {}).values())
    checks.append(
        {
            "name": "local_models",
            "status": "ok" if model_ready else "warning",
            "detail": (
                f"default={model_runtime.get('default_provider')}:{model_runtime.get('default_model')}"
                if model_ready
                else "No local model runtime detected"
            ),
        }
    )

    warnings = sum(1 for item in checks if item["status"] == "warning")
    errors = sum(1 for item in checks if item["status"] == "error")
    return {
        "timestamp": _now_iso(),
        "summary": {
            "warnings": warnings,
            "errors": errors,
            "healthy": errors == 0,
        },
        "checks": checks,
        "paths": {
            "home": str(paths.home),
            "control_socket": str(paths.control_socket),
            "control_tcp": f"{paths.control_host}:{paths.control_port}",
            "sqlite_db": str(paths.sqlite_db),
        },
        "runtime_profile": runtime_profile,
        "model_runtime": model_runtime,
        "permissions": config.get("permissions", {}),
    }


def install_daemon_service(
    *,
    daemon_path: str,
    python_executable: str,
    paths: KestrelPaths,
) -> dict[str, Any]:
    plat = platform.system()
    if plat == "Darwin":
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "ai.kestrel.daemon.plist"
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.kestrel.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_executable}</string>
        <string>{daemon_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{paths.home}</string>
    <key>StandardOutPath</key>
    <string>{paths.logs_dir / "daemon.stdout.log"}</string>
    <key>StandardErrorPath</key>
    <string>{paths.logs_dir / "daemon.stderr.log"}</string>
</dict>
</plist>
"""
        plist_path.write_text(plist_content, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
        result = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "launchctl load failed")
        return {
            "manager": "launchd",
            "service_path": str(plist_path),
        }
    if plat == "Linux":
        service_dir = Path.home() / ".config" / "systemd" / "user"
        service_dir.mkdir(parents=True, exist_ok=True)
        service_path = service_dir / "kestrel-daemon.service"
        service_text = f"""[Unit]
Description=Kestrel Native Agent OS Daemon
After=network.target

[Service]
ExecStart={python_executable} {daemon_path}
WorkingDirectory={paths.home}
Restart=always
StandardOutput=append:{paths.logs_dir / "daemon.stdout.log"}
StandardError=append:{paths.logs_dir / "daemon.stderr.log"}

[Install]
WantedBy=default.target
"""
        service_path.write_text(service_text, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "kestrel-daemon"], check=True)
        return {
            "manager": "systemd",
            "service_path": str(service_path),
        }
    if plat == "Windows":
        task_name = "KestrelDaemon"
        command = f'"{python_executable}" "{daemon_path}"'
        result = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/TN",
                task_name,
                "/TR",
                command,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "schtasks create failed")
        return {
            "manager": "scheduled-task",
            "service_path": task_name,
        }
    raise RuntimeError(f"Native daemon install is not implemented for {plat}")
