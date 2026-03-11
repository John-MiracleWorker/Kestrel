from __future__ import annotations

"""
Shared persistence for task events with Redis fan-out and Postgres audit fallback.
"""

import json
from typing import Any, Optional

from core.config import TASK_EVENT_HISTORY_MAX, TASK_EVENT_TTL_SECONDS
from db import get_pool, get_redis

_AUDIT_TYPE_MAP = {
    "task_queued": "task_queued",
    "tool_called": "tool_executed",
    "tool_result": "tool_executed",
    "approval_needed": "approval_requested",
    "verifier_passed": "verification_passed",
    "verifier_failed": "verification_failed",
    "model_routed": "model_routed",
    "task_failed": "error",
    "task_paused": "warning",
}


def task_event_payload_from_proto(task_event: Any) -> dict[str, Any]:
    return {
        "type": int(task_event.type),
        "task_id": task_event.task_id,
        "step_id": task_event.step_id,
        "content": task_event.content,
        "tool_name": task_event.tool_name,
        "tool_args": task_event.tool_args,
        "tool_result": task_event.tool_result,
        "approval_id": task_event.approval_id,
        "progress": dict(task_event.progress),
    }


async def persist_task_event_payload(
    payload: dict[str, Any],
    *,
    workspace_id: str = "",
    user_id: str = "",
) -> None:
    redis_client = await get_redis()
    key = f"kestrel:task_events:{payload['task_id']}"
    channel = f"kestrel:task_events:{payload['task_id']}:channel"
    event_json = json.dumps(payload, default=str)

    await redis_client.rpush(key, event_json)
    await redis_client.ltrim(key, -TASK_EVENT_HISTORY_MAX, -1)
    await redis_client.expire(key, TASK_EVENT_TTL_SECONDS)
    await redis_client.publish(channel, event_json)

    if not workspace_id:
        return

    pool = await get_pool()
    audit_event_type = _AUDIT_TYPE_MAP.get(str(payload.get("event_type", "")).lower(), "state_transition")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_events (workspace_id, user_id, task_id, event_type, tool_name, details)
            VALUES ($1, NULLIF($2, '')::uuid, NULLIF($3, '')::uuid, $4, NULLIF($5, ''), $6::jsonb)
            """,
            workspace_id,
            user_id,
            payload.get("task_id"),
            audit_event_type,
            payload.get("tool_name", ""),
            json.dumps({"task_event": payload}),
        )


async def load_task_event_history(task_id: str) -> list[dict[str, Any]]:
    redis_client = await get_redis()
    history = await redis_client.lrange(f"kestrel:task_events:{task_id}", 0, -1)
    if history:
        events = []
        for raw in history:
            decoded = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            events.append(json.loads(decoded))
        return events

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT details
            FROM audit_events
            WHERE task_id = $1
              AND details ? 'task_event'
            ORDER BY created_at ASC
            """,
            task_id,
        )
    return [dict(row["details"]["task_event"]) for row in rows]


async def is_task_terminal(task_id: str) -> bool:
    pool = await get_pool()
    status = await pool.fetchval(
        "SELECT status FROM agent_tasks WHERE id = $1",
        task_id,
    )
    return str(status or "") in {"complete", "failed", "cancelled"}
