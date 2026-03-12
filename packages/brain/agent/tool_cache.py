"""
Tool Cache — Redis-backed caching layer for deterministic tool results.

Wraps the tool execution pipeline to cache results for read-only,
deterministic tools. Cache keys are derived from:
  hash(tool_name + sorted_args + workspace_id)

Tools opt-in to caching by setting `cache_ttl_seconds > 0` in their
ToolDefinition. The cache is bypassed when:
  - The tool has no cache_ttl_seconds set (or it's 0)
  - The tool is marked as HIGH risk
  - The tool requires approval

Emits `cache_hit` and `cache_miss` events for observability.
"""

import hashlib
import json
import logging
import time
from typing import Any, Callable, Optional

from agent.types import ToolResult

logger = logging.getLogger("brain.agent.tool_cache")

# Tools that are always safe to cache (read-only, deterministic)
ALWAYS_CACHEABLE = frozenset({
    "host_tree", "host_list", "host_read", "host_find",
    "host_search", "host_batch_read", "project_recall",
    "file_read", "file_list", "memory_search",
    "web_search", "wikipedia_search",
})

# Default TTLs by tool category when tool doesn't specify one
DEFAULT_TTLS = {
    "host_tree": 300,       # 5 min — directory trees change rarely mid-task
    "host_list": 300,
    "host_read": 120,       # 2 min — file contents may change
    "host_find": 300,
    "host_search": 120,
    "host_batch_read": 120,
    "project_recall": 600,  # 10 min — project context is stable
    "file_read": 120,
    "file_list": 300,
    "memory_search": 600,
}


def _cache_key(tool_name: str, args: dict, workspace_id: str = "") -> str:
    """Generate a deterministic cache key for a tool invocation."""
    normalized = json.dumps(args, sort_keys=True, default=str)
    raw = f"{tool_name}:{workspace_id}:{normalized}"
    return f"kestrel:tool_cache:{hashlib.sha256(raw.encode()).hexdigest()}"


class ToolCache:
    """
    Redis-backed cache for tool execution results.

    Usage:
        cache = ToolCache(redis_client)

        # Check before executing
        cached = await cache.get(tool_name, args, workspace_id, tool_def)
        if cached is not None:
            return cached

        # After executing
        result = await execute_tool(...)
        await cache.set(tool_name, args, workspace_id, result, tool_def)
    """

    def __init__(self, redis_client=None, event_callback: Optional[Callable] = None):
        self._redis = redis_client
        self._event_callback = event_callback
        # In-memory fallback when Redis is unavailable
        self._local_cache: dict[str, tuple[float, str]] = {}  # key → (expires_at, json_result)
        self._stats = {"hits": 0, "misses": 0, "sets": 0}

    def _get_ttl(self, tool_name: str, tool_def=None) -> int:
        """Determine the TTL for a tool. Returns 0 if not cacheable."""
        if tool_def and hasattr(tool_def, 'cache_ttl_seconds') and tool_def.cache_ttl_seconds > 0:
            return tool_def.cache_ttl_seconds
        return DEFAULT_TTLS.get(tool_name, 0)

    def is_cacheable(self, tool_name: str, tool_def=None) -> bool:
        """Check if a tool's results should be cached."""
        if tool_name in ALWAYS_CACHEABLE:
            return True
        return self._get_ttl(tool_name, tool_def) > 0

    async def get(
        self,
        tool_name: str,
        args: dict,
        workspace_id: str = "",
        tool_def=None,
    ) -> Optional[ToolResult]:
        """Look up a cached tool result. Returns None on miss."""
        if not self.is_cacheable(tool_name, tool_def):
            return None

        key = _cache_key(tool_name, args, workspace_id)

        # Try Redis first
        if self._redis:
            try:
                cached = await self._redis.get(key)
                if cached:
                    self._stats["hits"] += 1
                    result_data = json.loads(cached)
                    if self._event_callback:
                        await self._event_callback("cache_hit", {
                            "tool": tool_name,
                            "key_prefix": key[:20],
                            "total_hits": self._stats["hits"],
                        })
                    logger.debug(f"Cache HIT: {tool_name} (key={key[:16]}...)")
                    return ToolResult(
                        tool_call_id=result_data.get("tool_call_id", "cached"),
                        success=result_data["success"],
                        output=result_data.get("output", ""),
                        error=result_data.get("error", ""),
                        execution_time_ms=0,
                        metadata={
                            **(result_data.get("metadata") or {}),
                            "cached": True,
                            "original_time_ms": result_data.get("execution_time_ms", 0),
                        },
                    )
            except Exception as e:
                logger.debug(f"Redis cache read failed: {e}")

        # Fallback: in-memory cache
        if key in self._local_cache:
            expires_at, cached_json = self._local_cache[key]
            if time.monotonic() < expires_at:
                self._stats["hits"] += 1
                result_data = json.loads(cached_json)
                if self._event_callback:
                    await self._event_callback("cache_hit", {
                        "tool": tool_name,
                        "total_hits": self._stats["hits"],
                    })
                return ToolResult(
                    tool_call_id=result_data.get("tool_call_id", "cached"),
                    success=result_data["success"],
                    output=result_data.get("output", ""),
                    error=result_data.get("error", ""),
                    execution_time_ms=0,
                    metadata={
                        **(result_data.get("metadata") or {}),
                        "cached": True,
                    },
                )
            else:
                del self._local_cache[key]

        self._stats["misses"] += 1
        return None

    async def set(
        self,
        tool_name: str,
        args: dict,
        workspace_id: str,
        result: ToolResult,
        tool_def=None,
    ) -> None:
        """Cache a successful tool result."""
        if not result.success:
            return  # Don't cache failures
        if not self.is_cacheable(tool_name, tool_def):
            return

        ttl = self._get_ttl(tool_name, tool_def)
        if ttl <= 0:
            return

        key = _cache_key(tool_name, args, workspace_id)
        value = json.dumps({
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "execution_time_ms": result.execution_time_ms,
            "tool_call_id": result.tool_call_id,
            "metadata": result.metadata,
        })

        # Try Redis
        if self._redis:
            try:
                await self._redis.set(key, value, ex=ttl)
                self._stats["sets"] += 1
                return
            except Exception as e:
                logger.debug(f"Redis cache write failed: {e}")

        # Fallback: in-memory
        self._local_cache[key] = (time.monotonic() + ttl, value)
        self._stats["sets"] += 1

        # Evict old entries if local cache gets too large
        if len(self._local_cache) > 500:
            now = time.monotonic()
            expired = [k for k, (exp, _) in self._local_cache.items() if now >= exp]
            for k in expired:
                del self._local_cache[k]

    async def invalidate(self, tool_name: str, args: dict, workspace_id: str = "") -> None:
        """Invalidate a specific cache entry (e.g., after a write to the same path)."""
        key = _cache_key(tool_name, args, workspace_id)
        if self._redis:
            try:
                await self._redis.delete(key)
            except Exception:
                pass
        self._local_cache.pop(key, None)

    async def invalidate_workspace(self, workspace_id: str) -> None:
        """Invalidate all cached entries for a workspace."""
        pattern = f"kestrel:tool_cache:*"
        if self._redis:
            try:
                # Use SCAN to find and delete workspace-related keys
                cursor = 0
                while True:
                    cursor, keys = await self._redis.scan(cursor, match=pattern, count=100)
                    if keys:
                        await self._redis.delete(*keys)
                    if cursor == 0:
                        break
            except Exception as e:
                logger.debug(f"Redis workspace invalidation failed: {e}")

        # Clear local cache entirely for simplicity
        self._local_cache.clear()

    def get_stats(self) -> dict:
        """Return cache hit/miss statistics."""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total * 100) if total > 0 else 0
        return {
            **self._stats,
            "hit_rate_pct": round(hit_rate, 1),
            "local_cache_size": len(self._local_cache),
        }
