from __future__ import annotations

from . import native_shared as _native_shared
from .local_operator_contracts import (
    ArtifactManifest,
    BackgroundSuggestion,
    LearningEvent,
    Procedure,
    ResearchSession,
)

globals().update({name: value for name, value in vars(_native_shared).items() if not name.startswith("__")})

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

    @abc.abstractmethod
    def list_skill_packs(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def get_skill_pack(self, pack_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abc.abstractmethod
    def upsert_skill_pack(
        self,
        *,
        pack_id: str,
        version: str,
        scope: str,
        source_path: str,
        source_type: str,
        enabled: bool,
        trusted: bool,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def set_skill_pack_enabled(self, pack_id: str, enabled: bool) -> dict[str, Any] | None:
        raise NotImplementedError

    @abc.abstractmethod
    def remove_skill_pack(self, pack_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abc.abstractmethod
    def upsert_background_suggestion(
        self,
        *,
        suggestion_id: str,
        workspace_id: str,
        title: str,
        body: str,
        goal: str,
        source: str,
        fingerprint: str,
        notification_type: str = "info",
        task_kind: str = "task",
        auto_start_allowed: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def get_background_suggestion(self, suggestion_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abc.abstractmethod
    def list_background_suggestions(
        self,
        *,
        status: str | None = None,
        workspace_id: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def resolve_background_suggestion(
        self,
        suggestion_id: str,
        *,
        status: str,
        task_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    @abc.abstractmethod
    def create_research_session(
        self,
        *,
        session_id: str,
        workspace_id: str,
        task_id: str,
        title: str,
        prompt: str,
        notebook_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def get_research_session(self, session_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abc.abstractmethod
    def get_research_session_for_task(self, task_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abc.abstractmethod
    def update_research_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        notebook_path: str | None = None,
        summary: str | None = None,
        sources: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_research_sessions(
        self,
        *,
        workspace_id: str | None = None,
        status: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def upsert_procedure(
        self,
        *,
        procedure_id: str,
        workspace_id: str,
        name: str,
        description: str,
        trigger_text: str,
        steps: list[dict[str, Any]],
        source_task_id: str = "",
        enabled: bool = True,
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_procedures(
        self,
        *,
        workspace_id: str | None = None,
        enabled_only: bool = False,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def record_artifact_manifests(self, task_id: str, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_artifact_manifests(self, task_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def append_learning_event(
        self,
        *,
        event_id: str,
        workspace_id: str,
        task_id: str,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_learning_events(
        self,
        *,
        workspace_id: str | None = None,
        task_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
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

    def _ensure_column(self, table_name: str, column_name: str, definition: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        known = {row["name"] for row in rows}
        if column_name not in known:
            self._conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
            )
            self._conn.commit()


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
                decision_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL DEFAULT '{}',
                resume_json TEXT NOT NULL DEFAULT '{}'
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

            CREATE TABLE IF NOT EXISTS skill_packs (
                pack_id TEXT PRIMARY KEY,
                version TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'user',
                source_path TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'directory',
                enabled INTEGER NOT NULL DEFAULT 1,
                trusted INTEGER NOT NULL DEFAULT 0,
                manifest_json TEXT NOT NULL DEFAULT '{}',
                installed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                removed_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS background_suggestions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                goal TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                notification_type TEXT NOT NULL DEFAULT 'info',
                task_kind TEXT NOT NULL DEFAULT 'task',
                auto_start_allowed INTEGER NOT NULL DEFAULT 0,
                task_id TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decided_at TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_background_suggestions_status
                ON background_suggestions(status, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_background_suggestions_workspace
                ON background_suggestions(workspace_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS research_sessions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                notebook_path TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                sources_json TEXT NOT NULL DEFAULT '[]',
                artifacts_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_research_sessions_workspace
                ON research_sessions(workspace_id, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_research_sessions_task
                ON research_sessions(task_id);

            CREATE TABLE IF NOT EXISTS procedures (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                trigger_text TEXT NOT NULL DEFAULT '',
                steps_json TEXT NOT NULL DEFAULT '[]',
                source_task_id TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                confidence REAL NOT NULL DEFAULT 0.5,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_procedures_workspace
                ON procedures(workspace_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS artifact_manifests (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL DEFAULT 'artifact',
                path TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_artifact_manifests_task
                ON artifact_manifests(task_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS learning_events (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_learning_events_workspace
                ON learning_events(workspace_id, created_at DESC);
            """
        )
        self._ensure_column("approvals", "payload_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("approvals", "resume_json", "TEXT NOT NULL DEFAULT '{}'")
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

    def list_skill_packs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM skill_packs
            WHERE removed_at = ''
            ORDER BY pack_id ASC
            """
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["enabled"] = bool(payload.get("enabled"))
            payload["trusted"] = bool(payload.get("trusted"))
            payload["manifest"] = json.loads(row["manifest_json"])
            results.append(payload)
        return results

    def get_skill_pack(self, pack_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM skill_packs
            WHERE pack_id = ? AND removed_at = ''
            """,
            (pack_id,),
        ).fetchone()
        if not row:
            return None
        payload = dict(row)
        payload["enabled"] = bool(payload.get("enabled"))
        payload["trusted"] = bool(payload.get("trusted"))
        payload["manifest"] = json.loads(row["manifest_json"])
        return payload

    def upsert_skill_pack(
        self,
        *,
        pack_id: str,
        version: str,
        scope: str,
        source_path: str,
        source_type: str,
        enabled: bool,
        trusted: bool,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.get_skill_pack(pack_id)
        installed_at = str((current or {}).get("installed_at") or _now_iso())
        updated_at = _now_iso()
        self._conn.execute(
            """
            INSERT INTO skill_packs (
                pack_id, version, scope, source_path, source_type, enabled, trusted,
                manifest_json, installed_at, updated_at, removed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            ON CONFLICT(pack_id) DO UPDATE SET
                version=excluded.version,
                scope=excluded.scope,
                source_path=excluded.source_path,
                source_type=excluded.source_type,
                enabled=excluded.enabled,
                trusted=excluded.trusted,
                manifest_json=excluded.manifest_json,
                updated_at=excluded.updated_at,
                removed_at=''
            """,
            (
                pack_id,
                version,
                scope,
                source_path,
                source_type,
                int(enabled),
                int(trusted),
                json.dumps(manifest),
                installed_at,
                updated_at,
            ),
        )
        self._conn.commit()
        return self.get_skill_pack(pack_id) or {}

    def set_skill_pack_enabled(self, pack_id: str, enabled: bool) -> dict[str, Any] | None:
        current = self.get_skill_pack(pack_id)
        if not current:
            return None
        self._conn.execute(
            """
            UPDATE skill_packs
            SET enabled = ?, updated_at = ?
            WHERE pack_id = ?
            """,
            (int(enabled), _now_iso(), pack_id),
        )
        self._conn.commit()
        return self.get_skill_pack(pack_id)

    def remove_skill_pack(self, pack_id: str) -> dict[str, Any] | None:
        current = self.get_skill_pack(pack_id)
        if not current:
            return None
        removed_at = _now_iso()
        self._conn.execute(
            """
            UPDATE skill_packs
            SET removed_at = ?, enabled = 0, updated_at = ?
            WHERE pack_id = ?
            """,
            (removed_at, removed_at, pack_id),
        )
        self._conn.commit()
        payload = dict(current)
        payload["removed_at"] = removed_at
        payload["enabled"] = False
        return payload

    def _background_suggestion_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = BackgroundSuggestion(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            title=str(row["title"]),
            body=str(row["body"] or ""),
            goal=str(row["goal"] or ""),
            source=str(row["source"] or ""),
            fingerprint=str(row["fingerprint"] or ""),
            status=str(row["status"] or "pending"),
            notification_type=str(row["notification_type"] or "info"),
            task_kind=str(row["task_kind"] or "task"),
            auto_start_allowed=bool(row["auto_start_allowed"]),
            task_id=str(row["task_id"] or ""),
            metadata=json.loads(row["metadata_json"]),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            decided_at=str(row["decided_at"] or ""),
        )
        return payload.to_dict()

    def upsert_background_suggestion(
        self,
        *,
        suggestion_id: str,
        workspace_id: str,
        title: str,
        body: str,
        goal: str,
        source: str,
        fingerprint: str,
        notification_type: str = "info",
        task_kind: str = "task",
        auto_start_allowed: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_background_suggestion(suggestion_id)
        created_at = str((current or {}).get("created_at") or _now_iso())
        payload = BackgroundSuggestion(
            id=suggestion_id,
            workspace_id=workspace_id,
            title=title,
            body=body,
            goal=goal,
            source=source,
            fingerprint=fingerprint,
            status=str((current or {}).get("status") or "pending"),
            notification_type=notification_type,
            task_kind=task_kind,
            auto_start_allowed=auto_start_allowed,
            task_id=str((current or {}).get("task_id") or ""),
            metadata=dict(metadata or (current or {}).get("metadata") or {}),
            created_at=created_at,
            updated_at=_now_iso(),
            decided_at=str((current or {}).get("decided_at") or ""),
        )
        self._conn.execute(
            """
            INSERT INTO background_suggestions (
                id, workspace_id, title, body, goal, source, fingerprint,
                status, notification_type, task_kind, auto_start_allowed,
                task_id, metadata_json, created_at, updated_at, decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id=excluded.workspace_id,
                title=excluded.title,
                body=excluded.body,
                goal=excluded.goal,
                source=excluded.source,
                fingerprint=excluded.fingerprint,
                notification_type=excluded.notification_type,
                task_kind=excluded.task_kind,
                auto_start_allowed=excluded.auto_start_allowed,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                payload.id,
                payload.workspace_id,
                payload.title,
                payload.body,
                payload.goal,
                payload.source,
                payload.fingerprint,
                payload.status,
                payload.notification_type,
                payload.task_kind,
                int(payload.auto_start_allowed),
                payload.task_id,
                json.dumps(payload.metadata),
                payload.created_at,
                payload.updated_at,
                payload.decided_at,
            ),
        )
        self._conn.commit()
        return self.get_background_suggestion(suggestion_id) or payload.to_dict()

    def get_background_suggestion(self, suggestion_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM background_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        return self._background_suggestion_from_row(row)

    def list_background_suggestions(
        self,
        *,
        status: str | None = None,
        workspace_id: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status = ?")
            values.append(status)
        if workspace_id:
            clauses.append("workspace_id = ?")
            values.append(workspace_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM background_suggestions {where} ORDER BY updated_at DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [
            payload
            for payload in (self._background_suggestion_from_row(row) for row in rows)
            if payload is not None
        ]

    def resolve_background_suggestion(
        self,
        suggestion_id: str,
        *,
        status: str,
        task_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_background_suggestion(suggestion_id)
        if not current:
            return None
        merged_metadata = dict(current.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        decided_at = _now_iso()
        self._conn.execute(
            """
            UPDATE background_suggestions
            SET status = ?, task_id = ?, metadata_json = ?, updated_at = ?, decided_at = ?
            WHERE id = ?
            """,
            (
                status,
                task_id or str(current.get("task_id") or ""),
                json.dumps(merged_metadata),
                decided_at,
                decided_at if status in {"accepted", "dismissed", "expired"} else str(current.get("decided_at") or ""),
                suggestion_id,
            ),
        )
        self._conn.commit()
        return self.get_background_suggestion(suggestion_id)

    def _research_session_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = ResearchSession(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            task_id=str(row["task_id"] or ""),
            title=str(row["title"] or ""),
            prompt=str(row["prompt"] or ""),
            status=str(row["status"] or "queued"),
            notebook_path=str(row["notebook_path"] or ""),
            summary=str(row["summary"] or ""),
            sources=json.loads(row["sources_json"]),
            artifacts=json.loads(row["artifacts_json"]),
            metadata=json.loads(row["metadata_json"]),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            completed_at=str(row["completed_at"] or ""),
        )
        return payload.to_dict()

    def create_research_session(
        self,
        *,
        session_id: str,
        workspace_id: str,
        task_id: str,
        title: str,
        prompt: str,
        notebook_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = ResearchSession(
            id=session_id,
            workspace_id=workspace_id,
            task_id=task_id,
            title=title,
            prompt=prompt,
            notebook_path=notebook_path,
            metadata=dict(metadata or {}),
        )
        self._conn.execute(
            """
            INSERT INTO research_sessions (
                id, workspace_id, task_id, title, prompt, status, notebook_path, summary,
                sources_json, artifacts_json, metadata_json, created_at, updated_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.id,
                payload.workspace_id,
                payload.task_id,
                payload.title,
                payload.prompt,
                payload.status,
                payload.notebook_path,
                payload.summary,
                json.dumps(payload.sources),
                json.dumps(payload.artifacts),
                json.dumps(payload.metadata),
                payload.created_at,
                payload.updated_at,
                payload.completed_at,
            ),
        )
        self._conn.commit()
        return self.get_research_session(session_id) or payload.to_dict()

    def get_research_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM research_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return self._research_session_from_row(row)

    def get_research_session_for_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM research_sessions WHERE task_id = ? ORDER BY updated_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._research_session_from_row(row)

    def update_research_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        notebook_path: str | None = None,
        summary: str | None = None,
        sources: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_research_session(session_id)
        if not current:
            raise KeyError(f"Unknown research session {session_id}")
        merged_metadata = dict(current.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        payload = ResearchSession(
            id=session_id,
            workspace_id=str(current.get("workspace_id") or ""),
            task_id=str(current.get("task_id") or ""),
            title=str(current.get("title") or ""),
            prompt=str(current.get("prompt") or ""),
            status=str(status or current.get("status") or "queued"),
            notebook_path=str(notebook_path if notebook_path is not None else current.get("notebook_path") or ""),
            summary=str(summary if summary is not None else current.get("summary") or ""),
            sources=list(sources if sources is not None else current.get("sources") or []),
            artifacts=list(artifacts if artifacts is not None else current.get("artifacts") or []),
            metadata=merged_metadata,
            created_at=str(current.get("created_at") or _now_iso()),
            updated_at=_now_iso(),
            completed_at=str(
                completed_at if completed_at is not None else current.get("completed_at") or ""
            ),
        )
        self._conn.execute(
            """
            UPDATE research_sessions
            SET status = ?, notebook_path = ?, summary = ?, sources_json = ?,
                artifacts_json = ?, metadata_json = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                payload.status,
                payload.notebook_path,
                payload.summary,
                json.dumps(payload.sources),
                json.dumps(payload.artifacts),
                json.dumps(payload.metadata),
                payload.updated_at,
                payload.completed_at,
                session_id,
            ),
        )
        self._conn.commit()
        return self.get_research_session(session_id) or payload.to_dict()

    def list_research_sessions(
        self,
        *,
        workspace_id: str | None = None,
        status: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if workspace_id:
            clauses.append("workspace_id = ?")
            values.append(workspace_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM research_sessions {where} ORDER BY updated_at DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [
            payload
            for payload in (self._research_session_from_row(row) for row in rows)
            if payload is not None
        ]

    def _procedure_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = Procedure(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            name=str(row["name"]),
            description=str(row["description"] or ""),
            trigger_text=str(row["trigger_text"] or ""),
            steps=json.loads(row["steps_json"]),
            source_task_id=str(row["source_task_id"] or ""),
            enabled=bool(row["enabled"]),
            confidence=float(row["confidence"] or 0.0),
            metadata=json.loads(row["metadata_json"]),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )
        return payload.to_dict()

    def upsert_procedure(
        self,
        *,
        procedure_id: str,
        workspace_id: str,
        name: str,
        description: str,
        trigger_text: str,
        steps: list[dict[str, Any]],
        source_task_id: str = "",
        enabled: bool = True,
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self._procedure_from_row(
            self._conn.execute("SELECT * FROM procedures WHERE id = ?", (procedure_id,)).fetchone()
        )
        payload = Procedure(
            id=procedure_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
            trigger_text=trigger_text,
            steps=list(steps),
            source_task_id=source_task_id,
            enabled=enabled,
            confidence=confidence,
            metadata=dict(metadata or (current or {}).get("metadata") or {}),
            created_at=str((current or {}).get("created_at") or _now_iso()),
            updated_at=_now_iso(),
        )
        self._conn.execute(
            """
            INSERT INTO procedures (
                id, workspace_id, name, description, trigger_text, steps_json,
                source_task_id, enabled, confidence, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id=excluded.workspace_id,
                name=excluded.name,
                description=excluded.description,
                trigger_text=excluded.trigger_text,
                steps_json=excluded.steps_json,
                source_task_id=excluded.source_task_id,
                enabled=excluded.enabled,
                confidence=excluded.confidence,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                payload.id,
                payload.workspace_id,
                payload.name,
                payload.description,
                payload.trigger_text,
                json.dumps(payload.steps),
                payload.source_task_id,
                int(payload.enabled),
                payload.confidence,
                json.dumps(payload.metadata),
                payload.created_at,
                payload.updated_at,
            ),
        )
        self._conn.commit()
        return self._procedure_from_row(
            self._conn.execute("SELECT * FROM procedures WHERE id = ?", (procedure_id,)).fetchone()
        ) or payload.to_dict()

    def list_procedures(
        self,
        *,
        workspace_id: str | None = None,
        enabled_only: bool = False,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if workspace_id:
            clauses.append("workspace_id = ?")
            values.append(workspace_id)
        if enabled_only:
            clauses.append("enabled = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM procedures {where} ORDER BY updated_at DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [
            payload
            for payload in (self._procedure_from_row(row) for row in rows)
            if payload is not None
        ]

    def record_artifact_manifests(self, task_id: str, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._conn.execute("DELETE FROM artifact_manifests WHERE task_id = ?", (task_id,))
        records: list[dict[str, Any]] = []
        for index, artifact in enumerate(list(artifacts or []), start=1):
            if not isinstance(artifact, dict):
                continue
            artifact_type = str(artifact.get("type") or artifact.get("artifact_type") or "artifact")
            path = str(artifact.get("path") or "")
            url = str(artifact.get("url") or "")
            mime_type = str(artifact.get("mime_type") or artifact.get("mimeType") or "")
            metadata = {
                key: value
                for key, value in dict(artifact).items()
                if key not in {"type", "artifact_type", "path", "url", "mime_type", "mimeType"}
            }
            record = ArtifactManifest(
                id=f"{task_id}:{index}",
                task_id=task_id,
                artifact_type=artifact_type,
                path=path,
                url=url,
                mime_type=mime_type,
                metadata=metadata,
            )
            self._conn.execute(
                """
                INSERT INTO artifact_manifests (
                    id, task_id, artifact_type, path, url, mime_type, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.task_id,
                    record.artifact_type,
                    record.path,
                    record.url,
                    record.mime_type,
                    json.dumps(record.metadata),
                    record.created_at,
                ),
            )
            records.append(record.to_dict())
        self._conn.commit()
        return records

    def list_artifact_manifests(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM artifact_manifests WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = ArtifactManifest(
                id=str(row["id"]),
                task_id=str(row["task_id"]),
                artifact_type=str(row["artifact_type"] or "artifact"),
                path=str(row["path"] or ""),
                url=str(row["url"] or ""),
                mime_type=str(row["mime_type"] or ""),
                metadata=json.loads(row["metadata_json"]),
                created_at=str(row["created_at"] or ""),
            )
            results.append(payload.to_dict())
        return results

    def append_learning_event(
        self,
        *,
        event_id: str,
        workspace_id: str,
        task_id: str,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = LearningEvent(
            id=event_id,
            workspace_id=workspace_id,
            task_id=task_id,
            event_type=event_type,
            summary=summary,
            payload=dict(payload or {}),
        )
        self._conn.execute(
            """
            INSERT INTO learning_events (id, workspace_id, task_id, event_type, summary, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.workspace_id,
                event.task_id,
                event.event_type,
                event.summary,
                json.dumps(event.payload),
                event.created_at,
            ),
        )
        self._conn.commit()
        return event.to_dict()

    def list_learning_events(
        self,
        *,
        workspace_id: str | None = None,
        task_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if workspace_id:
            clauses.append("workspace_id = ?")
            values.append(workspace_id)
        if task_id:
            clauses.append("task_id = ?")
            values.append(task_id)
        if event_type:
            clauses.append("event_type = ?")
            values.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM learning_events {where} ORDER BY created_at DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = LearningEvent(
                id=str(row["id"]),
                workspace_id=str(row["workspace_id"]),
                task_id=str(row["task_id"] or ""),
                event_type=str(row["event_type"]),
                summary=str(row["summary"]),
                payload=json.loads(row["payload_json"]),
                created_at=str(row["created_at"] or ""),
            )
            results.append(payload.to_dict())
        return results

    def create_approval(
        self,
        *,
        task_id: str,
        operation: str,
        command: str,
        payload: dict[str, Any] | None = None,
        resume: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            "payload": dict(payload or {}),
            "resume": dict(resume or {}),
        }
        self._conn.execute(
            """
            INSERT INTO approvals (
                id, task_id, operation, command, status, created_at, decided_at,
                decision_json, payload_json, resume_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(payload["payload"]),
                json.dumps(payload["resume"]),
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
            payload["payload"] = json.loads(row["payload_json"])
            payload["resume"] = json.loads(row["resume_json"])
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
        payload["payload"] = json.loads(row["payload_json"])
        payload["resume"] = json.loads(row["resume_json"])
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
            payload["payload"] = json.loads(row["payload_json"])
            payload["resume"] = json.loads(row["resume_json"])
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
        if namespace in {"", "*", "all"}:
            rows = self._conn.execute(
                "SELECT doc_id, namespace, content, vector_json, metadata_json FROM memory_vectors"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT doc_id, namespace, content, vector_json, metadata_json FROM memory_vectors WHERE namespace = ?",
                (namespace,),
            ).fetchall()
        ranked: list[dict[str, Any]] = []
        for row in rows:
            candidate = json.loads(row["vector_json"])
            ranked.append(
                {
                    "doc_id": row["doc_id"],
                    "namespace": row["namespace"],
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
