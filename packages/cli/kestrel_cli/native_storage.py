from __future__ import annotations

from . import native_shared as _native_shared

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


