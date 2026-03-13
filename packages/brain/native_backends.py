from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger("brain.native_backends")


def kestrel_home() -> Path:
    return Path(os.getenv("KESTREL_HOME", "~/.kestrel")).expanduser()


def native_state_dir() -> Path:
    path = kestrel_home() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_mode() -> str:
    return os.getenv("KESTREL_RUNTIME_MODE", "").strip().lower()


def use_local_redis_backend() -> bool:
    backend = os.getenv("BRAIN_REDIS_BACKEND", "").strip().lower()
    if backend:
        return backend == "local"
    return runtime_mode() in {"native", "local"}


def use_local_vector_backend() -> bool:
    backend = os.getenv("BRAIN_VECTOR_BACKEND", "").strip().lower()
    if backend:
        return backend in {"local", "sqlite", "sqlite_exact"}
    return runtime_mode() in {"native", "local"}


class LocalRedisPipeline:
    def __init__(self, client: "LocalRedis") -> None:
        self._client = client
        self._operations: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def delete(self, *keys: str) -> "LocalRedisPipeline":
        self._operations.append(("delete", keys, {}))
        return self

    def setex(self, key: str, ttl: int, value: Any) -> "LocalRedisPipeline":
        self._operations.append(("setex", (key, ttl, value), {}))
        return self

    def sadd(self, key: str, *members: Any) -> "LocalRedisPipeline":
        self._operations.append(("sadd", (key, *members), {}))
        return self

    def expire(self, key: str, ttl: int) -> "LocalRedisPipeline":
        self._operations.append(("expire", (key, ttl), {}))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for method_name, args, kwargs in self._operations:
            method = getattr(self._client, method_name)
            results.append(await method(*args, **kwargs))
        self._operations.clear()
        return results


class LocalRedisPubSub:
    def __init__(self, client: "LocalRedis") -> None:
        self._client = client
        self._queues: dict[str, asyncio.Queue] = {}

    async def subscribe(self, *channels: str) -> None:
        for channel in channels:
            queue: asyncio.Queue = asyncio.Queue()
            self._queues[channel] = queue
            self._client._register_subscriber(channel, queue)

    async def unsubscribe(self, *channels: str) -> None:
        targets = channels or tuple(self._queues.keys())
        for channel in targets:
            queue = self._queues.pop(channel, None)
            if queue is not None:
                self._client._unregister_subscriber(channel, queue)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = True,
        timeout: float | None = 0.0,
    ) -> dict[str, Any] | None:
        del ignore_subscribe_messages
        if not self._queues:
            if timeout:
                await asyncio.sleep(timeout)
            return None

        tasks = [asyncio.create_task(queue.get()) for queue in self._queues.values()]
        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                return None
            message = next(iter(done)).result()
            return {
                "type": "message",
                "channel": message["channel"],
                "data": message["data"],
            }
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def close(self) -> None:
        await self.unsubscribe()


class LocalRedis:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (native_state_dir() / "brain_local_redis.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redis_kv (
                key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value_json TEXT NOT NULL,
                expires_at REAL
            )
            """
        )
        self._conn.commit()
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def _register_subscriber(self, channel: str, queue: asyncio.Queue) -> None:
        self._subscribers.setdefault(channel, []).append(queue)

    def _unregister_subscriber(self, channel: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(channel, [])
        if queue in queues:
            queues.remove(queue)
        if not queues and channel in self._subscribers:
            self._subscribers.pop(channel, None)

    def _purge_expired_locked(self) -> None:
        now = time.time()
        self._conn.execute("DELETE FROM redis_kv WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
        self._conn.commit()

    def _load_locked(self, key: str) -> tuple[str, Any, float | None] | tuple[None, None, None]:
        self._purge_expired_locked()
        row = self._conn.execute(
            "SELECT kind, value_json, expires_at FROM redis_kv WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None, None, None
        return row["kind"], json.loads(row["value_json"]), row["expires_at"]

    def _save_locked(self, key: str, kind: str, value: Any, expires_at: float | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO redis_kv (key, kind, value_json, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                kind = excluded.kind,
                value_json = excluded.value_json,
                expires_at = excluded.expires_at
            """,
            (key, kind, json.dumps(value, default=str), expires_at),
        )
        self._conn.commit()

    async def get(self, key: str) -> Any:
        async with self._lock:
            kind, value, _ = self._load_locked(key)
            if kind is None:
                return None
            return value

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        expires_at = time.time() + ex if ex else None
        async with self._lock:
            self._save_locked(key, "string", value, expires_at)
        return True

    async def setex(self, key: str, ttl: int, value: Any) -> bool:
        return await self.set(key, value, ex=ttl)

    async def exists(self, key: str) -> int:
        return 1 if await self.get(key) is not None else 0

    async def expire(self, key: str, ttl: int) -> bool:
        async with self._lock:
            kind, value, _ = self._load_locked(key)
            if kind is None:
                return False
            self._save_locked(key, kind, value, time.time() + ttl)
        return True

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        async with self._lock:
            self._conn.executemany("DELETE FROM redis_kv WHERE key = ?", [(key,) for key in keys])
            self._conn.commit()
        return len(keys)

    async def sadd(self, key: str, *members: Any) -> int:
        if not members:
            return 0
        async with self._lock:
            kind, value, expires_at = self._load_locked(key)
            current = set(value or []) if kind == "set" else set()
            before = len(current)
            current.update(str(member) for member in members)
            self._save_locked(key, "set", sorted(current), expires_at)
        return len(current) - before

    async def smembers(self, key: str) -> set[str]:
        async with self._lock:
            kind, value, _ = self._load_locked(key)
            if kind != "set":
                return set()
            return set(str(member) for member in value or [])

    async def srem(self, key: str, *members: Any) -> int:
        if not members:
            return 0
        async with self._lock:
            kind, value, expires_at = self._load_locked(key)
            if kind != "set":
                return 0
            current = set(str(member) for member in value or [])
            before = len(current)
            current.difference_update(str(member) for member in members)
            self._save_locked(key, "set", sorted(current), expires_at)
        return before - len(current)

    async def hset(self, key: str, *args: Any, mapping: dict[str, Any] | None = None) -> int:
        updates: dict[str, Any] = dict(mapping or {})
        if args:
            if len(args) == 1 and isinstance(args[0], dict):
                updates.update(args[0])
            else:
                if len(args) % 2 != 0:
                    raise ValueError("hset expects field/value pairs")
                for index in range(0, len(args), 2):
                    updates[str(args[index])] = args[index + 1]

        async with self._lock:
            kind, value, expires_at = self._load_locked(key)
            current = dict(value or {}) if kind == "hash" else {}
            before = len(current)
            for field, field_value in updates.items():
                current[str(field)] = str(field_value)
            self._save_locked(key, "hash", current, expires_at)
        return len(current) - before

    async def hget(self, key: str, field: str) -> Any:
        async with self._lock:
            kind, value, _ = self._load_locked(key)
            if kind != "hash":
                return None
            return (value or {}).get(field)

    async def hgetall(self, key: str) -> dict[str, Any]:
        async with self._lock:
            kind, value, _ = self._load_locked(key)
            if kind != "hash":
                return {}
            return dict(value or {})

    async def rpush(self, key: str, *values: Any) -> int:
        async with self._lock:
            kind, current, expires_at = self._load_locked(key)
            items = list(current or []) if kind == "list" else []
            items.extend(str(value) for value in values)
            self._save_locked(key, "list", items, expires_at)
        return len(items)

    @staticmethod
    def _normalize_range(length: int, start: int, stop: int) -> tuple[int, int]:
        if length <= 0:
            return 0, -1
        if start < 0:
            start = length + start
        if stop < 0:
            stop = length + stop
        start = max(start, 0)
        stop = min(stop, length - 1)
        return start, stop

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        async with self._lock:
            kind, current, expires_at = self._load_locked(key)
            items = list(current or []) if kind == "list" else []
            start, stop = self._normalize_range(len(items), start, stop)
            trimmed = items[start:stop + 1] if stop >= start else []
            self._save_locked(key, "list", trimmed, expires_at)
        return True

    async def lrange(self, key: str, start: int, stop: int) -> list[Any]:
        async with self._lock:
            kind, current, _ = self._load_locked(key)
            items = list(current or []) if kind == "list" else []
            start, stop = self._normalize_range(len(items), start, stop)
            return items[start:stop + 1] if stop >= start else []

    async def keys(self, pattern: str) -> list[str]:
        async with self._lock:
            self._purge_expired_locked()
            rows = self._conn.execute("SELECT key FROM redis_kv").fetchall()
        return [row["key"] for row in rows if fnmatch.fnmatch(row["key"], pattern)]

    async def scan(self, cursor: int, match: str | None = None, count: int = 100) -> tuple[int, list[str]]:
        del count
        if cursor:
            return 0, []
        keys = await self.keys(match or "*")
        return 0, keys

    async def publish(self, channel: str, message: Any) -> int:
        payload = message if isinstance(message, str) else json.dumps(message, default=str)
        for queue in list(self._subscribers.get(channel, [])):
            queue.put_nowait({"channel": channel, "data": payload})
        return len(self._subscribers.get(channel, []))

    def pubsub(self) -> LocalRedisPubSub:
        return LocalRedisPubSub(self)

    def pipeline(self) -> LocalRedisPipeline:
        return LocalRedisPipeline(self)

    async def flushdb(self) -> bool:
        async with self._lock:
            self._conn.execute("DELETE FROM redis_kv")
            self._conn.commit()
        return True

    async def close(self) -> None:
        self._conn.close()
