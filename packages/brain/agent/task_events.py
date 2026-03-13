"""
Shared persistence for task events with a typed Postgres journal and Redis fan-out.
"""

from __future__ import annotations

import json
from typing import Any

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


def loads_task_event_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dumps_task_event_json(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


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
        "metadata": loads_task_event_json(getattr(task_event, "event_metadata_json", "")),
        "metrics": loads_task_event_json(getattr(task_event, "metrics_json", "")),
    }


def _extract_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = loads_task_event_json(payload.get("metadata"))
    if isinstance(metadata.get("receipt"), dict):
        return dict(metadata["receipt"])
    execution = metadata.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("receipt"), dict):
        return dict(execution["receipt"])
    return {}


async def persist_task_event_payload(
    payload: dict[str, Any],
    *,
    workspace_id: str = "",
    user_id: str = "",
) -> str:
    normalized_payload = dict(payload)
    normalized_payload["metadata"] = loads_task_event_json(normalized_payload.get("metadata"))
    normalized_payload["metrics"] = loads_task_event_json(normalized_payload.get("metrics"))

    redis_client = await get_redis()
    key = f"kestrel:task_events:{normalized_payload['task_id']}"
    channel = f"kestrel:task_events:{normalized_payload['task_id']}:channel"
    event_json = json.dumps(normalized_payload, default=str)

    await redis_client.rpush(key, event_json)
    await redis_client.ltrim(key, -TASK_EVENT_HISTORY_MAX, -1)
    await redis_client.expire(key, TASK_EVENT_TTL_SECONDS)
    await redis_client.publish(channel, event_json)

    if not workspace_id:
        return ""

    pool = await get_pool()
    audit_event_type = _AUDIT_TYPE_MAP.get(str(payload.get("event_type", "")).lower(), "state_transition")
    receipt = _extract_receipt(normalized_payload)
    journal_event_id = ""
    async with pool.acquire() as conn:
        journal_row = await conn.fetchrow(
            """
            INSERT INTO task_event_journal (
                workspace_id,
                user_id,
                task_id,
                event_type,
                step_id,
                tool_name,
                approval_id,
                progress_json,
                metadata_json,
                metrics_json,
                payload_json
            )
            VALUES (
                $1,
                NULLIF($2, '')::uuid,
                NULLIF($3, '')::uuid,
                $4,
                $5,
                $6,
                NULLIF($7, '')::uuid,
                $8::jsonb,
                $9::jsonb,
                $10::jsonb,
                $11::jsonb
            )
            RETURNING id
            """,
            workspace_id,
            user_id,
            normalized_payload.get("task_id"),
            str(normalized_payload.get("event_type", normalized_payload.get("type", ""))),
            normalized_payload.get("step_id", ""),
            normalized_payload.get("tool_name", ""),
            normalized_payload.get("approval_id", ""),
            json.dumps(normalized_payload.get("progress") or {}, default=str),
            json.dumps(normalized_payload.get("metadata") or {}, default=str),
            json.dumps(normalized_payload.get("metrics") or {}, default=str),
            json.dumps(normalized_payload, default=str),
        )
        if journal_row:
            journal_event_id = str(journal_row["id"])

        await conn.execute(
            """
            INSERT INTO audit_events (workspace_id, user_id, task_id, event_type, tool_name, details)
            VALUES ($1, NULLIF($2, '')::uuid, NULLIF($3, '')::uuid, $4, NULLIF($5, ''), $6::jsonb)
            """,
            workspace_id,
            user_id,
            normalized_payload.get("task_id"),
            audit_event_type,
            normalized_payload.get("tool_name", ""),
            json.dumps({"journal_event_id": journal_event_id, "task_event": normalized_payload}),
        )

        if receipt:
            await conn.execute(
                """
                INSERT INTO action_receipts (
                    receipt_id,
                    workspace_id,
                    task_id,
                    step_id,
                    tool_name,
                    request_id,
                    runtime_class,
                    risk_class,
                    failure_class,
                    logs_pointer,
                    stdout_pointer,
                    stderr_pointer,
                    sandbox_id,
                    exit_code,
                    audit_summary,
                    artifact_manifest,
                    file_touches,
                    network_touches,
                    system_touches,
                    grants_json,
                    metadata_json,
                    mutating,
                    updated_at
                )
                VALUES (
                    NULLIF($1, '')::uuid,
                    $2,
                    NULLIF($3, '')::uuid,
                    $4,
                    $5,
                    $6,
                    $7,
                    $8,
                    $9,
                    $10,
                    $11,
                    $12,
                    $13,
                    $14,
                    $15,
                    $16::jsonb,
                    $17::jsonb,
                    $18::jsonb,
                    $19::jsonb,
                    $20::jsonb,
                    $21::jsonb,
                    $22,
                    now()
                )
                ON CONFLICT (receipt_id) DO UPDATE SET
                    task_id = EXCLUDED.task_id,
                    step_id = EXCLUDED.step_id,
                    tool_name = EXCLUDED.tool_name,
                    request_id = EXCLUDED.request_id,
                    runtime_class = EXCLUDED.runtime_class,
                    risk_class = EXCLUDED.risk_class,
                    failure_class = EXCLUDED.failure_class,
                    logs_pointer = EXCLUDED.logs_pointer,
                    stdout_pointer = EXCLUDED.stdout_pointer,
                    stderr_pointer = EXCLUDED.stderr_pointer,
                    sandbox_id = EXCLUDED.sandbox_id,
                    exit_code = EXCLUDED.exit_code,
                    audit_summary = EXCLUDED.audit_summary,
                    artifact_manifest = EXCLUDED.artifact_manifest,
                    file_touches = EXCLUDED.file_touches,
                    network_touches = EXCLUDED.network_touches,
                    system_touches = EXCLUDED.system_touches,
                    grants_json = EXCLUDED.grants_json,
                    metadata_json = EXCLUDED.metadata_json,
                    mutating = EXCLUDED.mutating,
                    updated_at = now()
                """,
                receipt.get("receipt_id", ""),
                workspace_id,
                normalized_payload.get("task_id", ""),
                normalized_payload.get("step_id", ""),
                normalized_payload.get("tool_name", ""),
                receipt.get("request_id", ""),
                receipt.get("runtime_class", ""),
                receipt.get("risk_class", ""),
                receipt.get("failure_class", "none"),
                receipt.get("logs_pointer", ""),
                receipt.get("stdout_pointer", ""),
                receipt.get("stderr_pointer", ""),
                receipt.get("sandbox_id", ""),
                int(receipt.get("exit_code", 0) or 0),
                receipt.get("audit_summary", ""),
                json.dumps(receipt.get("artifact_manifest", []), default=str),
                json.dumps(receipt.get("file_touches", []), default=str),
                json.dumps(receipt.get("network_touches", []), default=str),
                json.dumps(receipt.get("system_touches", []), default=str),
                json.dumps(receipt.get("grants", []), default=str),
                json.dumps(receipt.get("metadata", {}), default=str),
                bool(receipt.get("mutating", False)),
            )
    return journal_event_id


async def load_task_event_history(task_id: str) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payload_json
            FROM task_event_journal
            WHERE task_id = $1
            ORDER BY sequence_id ASC
            """,
            task_id,
        )
    if rows:
        return [dict(row["payload_json"]) for row in rows]

    redis_client = await get_redis()
    history = await redis_client.lrange(f"kestrel:task_events:{task_id}", 0, -1)
    if history:
        events = []
        for raw in history:
            decoded = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            events.append(json.loads(decoded))
        return events

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
