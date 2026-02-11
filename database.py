"""
Libre Bird — SQLite storage layer with FTS5 full-text search.
All data stays local. Zero cloud. Zero telemetry.
"""

import aiosqlite
import json
import os
from datetime import datetime, date
from typing import Optional


DB_PATH = os.path.join(os.path.dirname(__file__), "libre_bird.db")


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self.initialize()

    async def close(self):
        if self._db:
            await self._db.close()

    async def initialize(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS context_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                app_name TEXT,
                window_title TEXT,
                focused_text TEXT,
                bundle_id TEXT
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'New Conversation',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                is_archived INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                context_snapshot_id INTEGER,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY (context_snapshot_id) REFERENCES context_snapshots(id)
            );

            CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL,
                activities TEXT,  -- JSON array
                tasks_extracted TEXT,  -- JSON array
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo', 'in_progress', 'done', 'dismissed')),
                priority TEXT DEFAULT 'medium' CHECK(priority IN ('low', 'medium', 'high')),
                source TEXT,  -- 'journal', 'chat', 'manual'
                source_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                completed_at TEXT
            );

            -- FTS5 for searching context snapshots
            CREATE VIRTUAL TABLE IF NOT EXISTS context_fts USING fts5(
                app_name, window_title, focused_text,
                content=context_snapshots,
                content_rowid=id
            );

            -- FTS triggers
            CREATE TRIGGER IF NOT EXISTS context_ai AFTER INSERT ON context_snapshots BEGIN
                INSERT INTO context_fts(rowid, app_name, window_title, focused_text)
                VALUES (new.id, new.app_name, new.window_title, new.focused_text);
            END;

            -- FTS5 for searching messages
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content=messages,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;

            -- Indexes
            CREATE INDEX IF NOT EXISTS idx_context_timestamp ON context_snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_journal_date ON journal_entries(entry_date);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        """)
        await self._db.commit()

    # ── Settings ─────────────────────────────────────────────────────

    async def get_setting(self, key: str, default: str = None) -> Optional[str]:
        cursor = await self._db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str):
        await self._db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._db.commit()

    # ── Context Snapshots ────────────────────────────────────────────

    async def save_context(self, app_name: str, window_title: str,
                           focused_text: str, bundle_id: str = None) -> int:
        cursor = await self._db.execute(
            """INSERT INTO context_snapshots (app_name, window_title, focused_text, bundle_id)
               VALUES (?, ?, ?, ?)""",
            (app_name, window_title, focused_text, bundle_id),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_recent_context(self, limit: int = 10) -> list:
        cursor = await self._db.execute(
            "SELECT * FROM context_snapshots ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_context_for_date(self, d: str) -> list:
        cursor = await self._db.execute(
            "SELECT * FROM context_snapshots WHERE date(timestamp) = ? ORDER BY timestamp",
            (d,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_context_for_timerange(self, start: str, end: str,
                                         limit: int = 50) -> list:
        """Get context snapshots within a time range (ISO format strings)."""
        cursor = await self._db.execute(
            """SELECT * FROM context_snapshots
               WHERE timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp DESC LIMIT ?""",
            (start, end, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_context(self, query: str, limit: int = 20) -> list:
        cursor = await self._db.execute(
            """SELECT cs.* FROM context_snapshots cs
               JOIN context_fts ON cs.id = context_fts.rowid
               WHERE context_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Conversations ────────────────────────────────────────────────

    async def create_conversation(self, title: str = "New Conversation") -> int:
        cursor = await self._db.execute(
            "INSERT INTO conversations (title) VALUES (?)", (title,)
        )
        await self._db.commit()
        return cursor.lastrowid

    async def list_conversations(self, limit: int = 50) -> list:
        cursor = await self._db.execute(
            """SELECT c.*, COUNT(m.id) as message_count
               FROM conversations c
               LEFT JOIN messages m ON c.id = m.conversation_id
               WHERE c.is_archived = 0
               GROUP BY c.id
               ORDER BY c.updated_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_conversation_title(self, conv_id: int, title: str):
        await self._db.execute(
            "UPDATE conversations SET title = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (title, conv_id),
        )
        await self._db.commit()

    async def delete_conversation(self, conv_id: int):
        await self._db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        await self._db.commit()

    # ── Messages ─────────────────────────────────────────────────────

    async def add_message(self, conversation_id: int, role: str, content: str,
                          context_snapshot_id: int = None) -> int:
        cursor = await self._db.execute(
            """INSERT INTO messages (conversation_id, role, content, context_snapshot_id)
               VALUES (?, ?, ?, ?)""",
            (conversation_id, role, content, context_snapshot_id),
        )
        await self._db.execute(
            "UPDATE conversations SET updated_at = datetime('now', 'localtime') WHERE id = ?",
            (conversation_id,),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_messages(self, conversation_id: int) -> list:
        cursor = await self._db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Journal ──────────────────────────────────────────────────────

    async def save_journal(self, entry_date: str, summary: str,
                           activities: list = None, tasks: list = None):
        await self._db.execute(
            """INSERT OR REPLACE INTO journal_entries
               (entry_date, summary, activities, tasks_extracted)
               VALUES (?, ?, ?, ?)""",
            (entry_date, summary,
             json.dumps(activities) if activities else None,
             json.dumps(tasks) if tasks else None),
        )
        await self._db.commit()

    async def get_journal(self, entry_date: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM journal_entries WHERE entry_date = ?", (entry_date,)
        )
        row = await cursor.fetchone()
        if row:
            d = dict(row)
            if d.get("activities"):
                d["activities"] = json.loads(d["activities"])
            if d.get("tasks_extracted"):
                d["tasks_extracted"] = json.loads(d["tasks_extracted"])
            return d
        return None

    async def list_journals(self, limit: int = 30) -> list:
        cursor = await self._db.execute(
            "SELECT id, entry_date, summary FROM journal_entries ORDER BY entry_date DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Tasks ────────────────────────────────────────────────────────

    async def create_task(self, title: str, description: str = None,
                          priority: str = "medium", source: str = "manual",
                          source_id: int = None) -> int:
        cursor = await self._db.execute(
            """INSERT INTO tasks (title, description, priority, source, source_id)
               VALUES (?, ?, ?, ?, ?)""",
            (title, description, priority, source, source_id),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def list_tasks(self, status: str = None) -> list:
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM tasks ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'todo' THEN 1 WHEN 'done' THEN 2 ELSE 3 END, created_at DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_task_status(self, task_id: int, status: str):
        completed = datetime.now().isoformat() if status == "done" else None
        await self._db.execute(
            "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed, task_id),
        )
        await self._db.commit()

    async def delete_task(self, task_id: int):
        await self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self._db.commit()
