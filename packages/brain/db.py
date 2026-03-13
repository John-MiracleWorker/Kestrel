"""
Database connection pool management for PostgreSQL and Redis.
"""

from __future__ import annotations

import os
import logging
from typing import Optional

try:
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - optional in native mode
    asyncpg = None

from native_backends import LocalRedis, use_local_redis_backend

logger = logging.getLogger("brain.db")

DB_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{os.getenv('POSTGRES_USER', 'kestrel')}:"
    f"{os.getenv('POSTGRES_PASSWORD', 'changeme')}@"
    f"{os.getenv('POSTGRES_HOST', 'localhost')}:"
    f"{os.getenv('POSTGRES_PORT', '5432')}/"
    f"{os.getenv('POSTGRES_DB', 'kestrel')}"
)

_pool: Optional["asyncpg.Pool"] = None
_redis_pool: Optional[object] = None


def _import_redis_module():
    try:
        import redis.asyncio as redis_module
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Redis support requires the 'redis' package. Install packages/brain/requirements.txt."
        ) from exc
    return redis_module


async def get_pool() -> "asyncpg.Pool":
    global _pool
    if _pool is None:
        if asyncpg is None:
            raise RuntimeError(
                "PostgreSQL support requires the 'asyncpg' package. "
                "Install packages/brain/requirements.txt or avoid get_pool() in native mode."
            )
        _pool = await asyncpg.create_pool(
            DB_URL,
            min_size=int(os.getenv("POSTGRES_POOL_MIN", "2")),
            max_size=int(os.getenv("POSTGRES_POOL_MAX", "10")),
        )
    return _pool


async def get_redis():
    global _redis_pool
    if _redis_pool is None:
        if use_local_redis_backend():
            _redis_pool = LocalRedis()
            logger.info("Using local SQLite-backed Redis compatibility store")
        else:
            redis = _import_redis_module()
            _redis_pool = redis.from_url(
                f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}"
            )
    return _redis_pool


async def reset_cached_clients() -> None:
    global _pool, _redis_pool
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _redis_pool is not None and hasattr(_redis_pool, "close"):
        maybe_awaitable = _redis_pool.close()
        if hasattr(maybe_awaitable, "__await__"):
            await maybe_awaitable
    _redis_pool = None
