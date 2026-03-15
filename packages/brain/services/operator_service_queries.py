from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.grpc_setup import brain_pb2
from db import get_pool
from .base import BaseServicerMixin
from .operator_service_helpers import _iso, _load_jsonb

class OperatorQueryMixin(BaseServicerMixin):
    async def _get_workspace_task_row(
        self,
        *,
        workspace_id: str,
        task_id: str,
    ):
        pool = await get_pool()
        return await pool.fetchrow(
            """
            SELECT id, user_id, workspace_id, conversation_id, goal, status, plan,
                   result, error, tool_calls_count, iterations, created_at, completed_at
            FROM agent_tasks
            WHERE id = $1
              AND workspace_id = $2
            """,
            task_id,
            workspace_id,
        )

    async def _load_timeline_rows(
        self,
        *,
        workspace_id: str,
        task_id: str,
    ) -> list[dict[str, Any]]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT id, created_at, payload_json
            FROM task_event_journal
            WHERE workspace_id = $1
              AND task_id = $2
            ORDER BY sequence_id ASC
            """,
            workspace_id,
            task_id,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row["payload_json"]) if isinstance(row["payload_json"], dict) else {}
            payload["created_at"] = _iso(row["created_at"])
            payload["journal_event_id"] = str(row["id"])
            result.append(payload)
        return result

    async def _load_receipt_rows(
        self,
        *,
        task_id: str,
    ):
        pool = await get_pool()
        return await pool.fetch(
            """
            SELECT receipt_id, task_id, step_id, tool_name, runtime_class, risk_class,
                   failure_class, logs_pointer, exit_code, audit_summary,
                   artifact_manifest, created_at
            FROM action_receipts
            WHERE task_id = $1
            ORDER BY created_at DESC
            """,
            task_id,
        )

    async def _load_verifier_rows(
        self,
        *,
        task_id: str,
    ):
        pool = await get_pool()
        return await pool.fetch(
            """
            SELECT id, claim_text, verdict, confidence, rationale,
                   supporting_receipt_ids, artifact_refs, created_at
            FROM verifier_claim_evidence
            WHERE task_id = $1
            ORDER BY created_at DESC
            """,
            task_id,
        )

    async def _latest_session_row(self, task_id: str):
        pool = await get_pool()
        return await pool.fetchrow(
            """
            SELECT id, channel, external_conversation_id, external_thread_id,
                   return_route_json, session_metadata_json
            FROM agent_sessions
            WHERE task_id = $1
            ORDER BY last_activity DESC
            LIMIT 1
            """,
            task_id,
        )

    async def _list_artifact_rows(
        self,
        *,
        workspace_id: str,
        task_row: Any | None,
        limit: int,
    ):
        pool = await get_pool()
        if task_row:
            created_at = task_row["created_at"]
            completed_at = task_row["completed_at"] or datetime.now(timezone.utc)
            rows = await pool.fetch(
                """
                SELECT id, title, description, component_type, version,
                       updated_at, created_by, data_source
                FROM ui_artifacts
                WHERE workspace_id = $1
                  AND updated_at >= (CAST($2 AS timestamptz) - INTERVAL '5 minutes')
                  AND updated_at <= (CAST($3 AS timestamptz) + INTERVAL '5 minutes')
                ORDER BY updated_at DESC
                LIMIT $4
                """,
                workspace_id,
                created_at,
                completed_at,
                limit,
            )
            if rows:
                return rows

        return await pool.fetch(
            """
            SELECT id, title, description, component_type, version,
                   updated_at, created_by, data_source
            FROM ui_artifacts
            WHERE workspace_id = $1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            workspace_id,
            limit,
        )

    async def _latest_queue_row(self, task_id: str):
        pool = await get_pool()
        return await pool.fetchrow(
            """
            SELECT status, lease_expires_at, created_at, updated_at
            FROM task_queue
            WHERE agent_task_id = $1 OR terminal_task_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            task_id,
        )

    @staticmethod
    def _derive_execution_summary(
        timeline_rows: list[dict[str, Any]],
    ) -> Any:
        runtime_class = ""
        risk_class = ""
        fallback_summary = ""
        recent_tools: list[str] = []
        seen_tools: set[str] = set()
        last_event_at = ""

        for payload in reversed(timeline_rows):
            if not last_event_at:
                last_event_at = str(payload.get("created_at", ""))

            tool_name = str(payload.get("tool_name", "") or "")
            if tool_name and tool_name not in seen_tools:
                seen_tools.add(tool_name)
                recent_tools.append(tool_name)
                if len(recent_tools) >= 5:
                    pass

            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                raw_metadata = payload.get("event_metadata_json", "")
                metadata = _load_jsonb(raw_metadata)
            execution = metadata.get("execution") if isinstance(metadata, dict) else None
            if isinstance(execution, dict) and not runtime_class:
                runtime_class = str(execution.get("runtime_class", "") or "")
                risk_class = str(execution.get("risk_class", "") or "")
                fallback_used = execution.get("fallback_used") in {True, "true", "True"}
                fallback_from = str(execution.get("fallback_from", "") or "")
                fallback_to = str(execution.get("fallback_to", "") or "")
                if fallback_used and (fallback_from or fallback_to):
                    fallback_summary = f"{fallback_from or 'unknown'} -> {fallback_to or 'unknown'}"

        return brain_pb2.ExecutionTraceSummary(
            runtime_class=runtime_class,
            risk_class=risk_class,
            fallback_summary=fallback_summary,
            recent_tools=list(reversed(recent_tools[:5])),
            last_event_at=last_event_at,
        )

