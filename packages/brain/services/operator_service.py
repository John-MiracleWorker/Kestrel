from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

import grpc

from core import runtime
from core.grpc_setup import brain_pb2
from db import get_pool
from .base import BaseServicerMixin

logger = logging.getLogger("brain.services.operator")

_TERMINAL_TASK_STATUSES = {"complete", "failed", "cancelled"}
_ACTIVE_QUEUE_STATUSES = {"queued", "running", "paused"}


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _load_jsonb(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(value) if value else {}


def _load_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return list(value) if value else []


def _receipt_id_from_payload(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    execution = metadata.get("execution")
    if not isinstance(execution, dict):
        return ""
    receipt = execution.get("receipt")
    if not isinstance(receipt, dict):
        return ""
    return str(receipt.get("receipt_id") or "")


def _progress_from_plan(plan: dict[str, Any]) -> tuple[str, str]:
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return "", ""

    total = len(steps)
    complete_statuses = {"complete", "skipped"}
    done = sum(
        1
        for step in steps
        if isinstance(step, dict) and str(step.get("status", "")).lower() in complete_statuses
    )
    current = total if done >= total else done + 1
    return str(current), str(total)


def _build_recovery_hints(
    *,
    status: str,
    stale: bool,
    orphaned: bool,
    pending_approval_id: str,
    last_checkpoint_id: str,
) -> list[Any]:
    hints: list[Any] = []

    if pending_approval_id:
        hints.append(
            brain_pb2.RecoveryHint(
                code="approval_pending",
                title="Approval required",
                description="This task is waiting on an approval decision before it can continue.",
            )
        )
    if orphaned:
        hints.append(
            brain_pb2.RecoveryHint(
                code="orphaned_execution",
                title="Lease expired",
                description="The queue lease expired while the task was still marked running. Requeue or inspect worker health.",
            )
        )
    elif stale:
        hints.append(
            brain_pb2.RecoveryHint(
                code="stalled_queue",
                title="Task appears stalled",
                description="The task has active queue state with a stale lease or no recent progress.",
            )
        )
    if status == "failed":
        hints.append(
            brain_pb2.RecoveryHint(
                code="review_failure",
                title="Review failure trace",
                description="Inspect the recent timeline and execution trace summary before retrying this task.",
            )
        )
    if last_checkpoint_id:
        hints.append(
            brain_pb2.RecoveryHint(
                code="checkpoint_available",
                title="Checkpoint available",
                description="A recent checkpoint exists for this task and can be used for recovery or inspection.",
            )
        )
    return hints


class OperatorServicerMixin(BaseServicerMixin):
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
                  AND updated_at BETWEEN $2 - INTERVAL '5 minutes' AND $3 + INTERVAL '5 minutes'
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

    async def GetMemoryGraph(self, request, context):
        workspace_id = request.workspace_id
        if not workspace_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id is required")

        pool = await get_pool()
        graph_nodes = await pool.fetch(
            """
            SELECT id, entity_type, name, description, weight, mention_count,
                   first_seen, last_seen, source_conversation_id
            FROM memory_graph_nodes
            WHERE workspace_id = $1
            ORDER BY weight DESC
            LIMIT 200
            """,
            workspace_id,
        )
        graph_edges = await pool.fetch(
            """
            SELECT e.id, e.source_id, e.target_id, e.relation_type, e.strength,
                   e.context, e.conversation_id
            FROM memory_graph_edges e
            JOIN memory_graph_nodes n ON e.source_id = n.id
            WHERE n.workspace_id = $1
            LIMIT 500
            """,
            workspace_id,
        )
        conversations = await pool.fetch(
            """
            SELECT id, title, created_at
            FROM conversations
            WHERE workspace_id = $1
            ORDER BY created_at DESC
            LIMIT 30
            """,
            workspace_id,
        )

        node_ids: set[str] = set()
        nodes: list[brain_pb2.MemoryGraphNode] = []
        links: list[brain_pb2.MemoryGraphLink] = []
        center_x = 400.0
        center_y = 300.0

        conv_count = max(len(conversations), 1)
        for index, conv in enumerate(conversations):
            angle = (2 * math.pi * index) / conv_count
            radius = 120.0
            node_id = str(conv["id"])
            node_ids.add(node_id)
            nodes.append(
                brain_pb2.MemoryGraphNode(
                    id=node_id,
                    label=str(conv["title"] or "Untitled")[:30],
                    entity_type="conversation",
                    description=f"Conversation from {_iso(conv['created_at'])[:10]}",
                    weight=2,
                    mentions=1,
                    last_seen=_iso(conv["created_at"]),
                    x=center_x + math.cos(angle) * radius,
                    y=center_y + math.sin(angle) * radius,
                )
            )

        entity_count = max(len(graph_nodes), 1)
        for index, row in enumerate(graph_nodes):
            node_id = str(row["id"])
            if node_id in node_ids:
                continue
            node_ids.add(node_id)
            angle = (2 * math.pi * index) / entity_count
            radius = 250.0
            nodes.append(
                brain_pb2.MemoryGraphNode(
                    id=node_id,
                    label=str(row["name"] or "Unknown")[:30],
                    entity_type=str(row["entity_type"] or "concept"),
                    description=str(row["description"] or "")[:200],
                    weight=max(1, round(float(row["weight"] or 1) * 2)),
                    mentions=int(row["mention_count"] or 1),
                    last_seen=_iso(row["last_seen"]),
                    x=center_x + math.cos(angle) * radius,
                    y=center_y + math.sin(angle) * radius,
                )
            )
            source_conversation_id = row["source_conversation_id"]
            if source_conversation_id and str(source_conversation_id) in node_ids:
                links.append(
                    brain_pb2.MemoryGraphLink(
                        source=str(source_conversation_id),
                        target=node_id,
                        relation="mentioned_in",
                    )
                )

        for edge in graph_edges:
            source_id = str(edge["source_id"])
            target_id = str(edge["target_id"])
            if source_id in node_ids and target_id in node_ids:
                links.append(
                    brain_pb2.MemoryGraphLink(
                        source=source_id,
                        target=target_id,
                        relation=str(edge["relation_type"] or "related_to"),
                    )
                )

        for index in range(len(conversations) - 1):
            links.append(
                brain_pb2.MemoryGraphLink(
                    source=str(conversations[index]["id"]),
                    target=str(conversations[index + 1]["id"]),
                    relation="followed_by",
                )
            )

        return brain_pb2.GetMemoryGraphResponse(nodes=nodes, links=links)

    async def GetTaskDetail(self, request, context):
        workspace_id = request.workspace_id
        task_id = request.task_id
        if not workspace_id or not task_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id and task_id are required")

        task_row = await self._get_workspace_task_row(workspace_id=workspace_id, task_id=task_id)
        if not task_row:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"Task {task_id} not found")

        pool = await get_pool()
        pending_approval = await pool.fetchrow(
            """
            SELECT id, tool_name
            FROM agent_approvals
            WHERE task_id = $1
              AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            task_id,
        )
        latest_checkpoint = await pool.fetchrow(
            """
            SELECT id, label, created_at, journal_event_id
            FROM agent_checkpoints
            WHERE task_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            task_id,
        )
        queue_row = await self._latest_queue_row(task_id)
        timeline_rows = await self._load_timeline_rows(workspace_id=workspace_id, task_id=task_id)
        receipt_rows = await self._load_receipt_rows(task_id=task_id)
        verifier_rows = await self._load_verifier_rows(task_id=task_id)
        session_row = await self._latest_session_row(task_id)

        plan = _load_jsonb(task_row["plan"])
        current_step, total_steps = _progress_from_plan(plan)
        if timeline_rows:
            last_progress = next(
                (
                    payload.get("progress")
                    for payload in reversed(timeline_rows)
                    if isinstance(payload.get("progress"), dict) and payload.get("progress")
                ),
                None,
            )
            if isinstance(last_progress, dict):
                current_step = str(last_progress.get("current_step", current_step) or current_step)
                total_steps = str(last_progress.get("total_steps", total_steps) or total_steps)

        lease_expires_at = queue_row["lease_expires_at"] if queue_row else None
        stale = bool(
            queue_row
            and str(queue_row["status"] or "") in _ACTIVE_QUEUE_STATUSES
            and lease_expires_at
            and lease_expires_at <= datetime.now(timezone.utc)
        )
        orphaned = bool(queue_row and str(queue_row["status"] or "") == "running" and stale)

        artifact_rows = await self._list_artifact_rows(
            workspace_id=workspace_id,
            task_row=task_row,
            limit=6,
        )
        artifact_refs = [
            brain_pb2.TaskArtifactReference(
                id=str(row["id"]),
                title=str(row["title"] or ""),
                component_type=str(row["component_type"] or ""),
                version=int(row["version"] or 1),
                updated_at=_iso(row["updated_at"]),
                data_source=str(row["data_source"] or ""),
            )
            for row in artifact_rows
        ]

        pending_approval_id = str(pending_approval["id"]) if pending_approval else ""
        last_checkpoint_id = str(latest_checkpoint["id"]) if latest_checkpoint else ""
        detail = brain_pb2.TaskDetail(
            id=str(task_row["id"]),
            goal=str(task_row["goal"] or ""),
            status=str(task_row["status"] or ""),
            iterations=int(task_row["iterations"] or 0),
            tool_calls=int(task_row["tool_calls_count"] or 0),
            result=str(task_row["result"] or ""),
            error=str(task_row["error"] or ""),
            created_at=_iso(task_row["created_at"]),
            completed_at=_iso(task_row["completed_at"]),
            workspace_id=str(task_row["workspace_id"]),
            user_id=str(task_row["user_id"]),
            conversation_id=str(task_row["conversation_id"] or ""),
            current_step=current_step,
            total_steps=total_steps,
            pending_approval_id=pending_approval_id,
            pending_approval_tool=str(pending_approval["tool_name"] or "") if pending_approval else "",
            last_checkpoint_id=last_checkpoint_id,
            last_checkpoint_label=str(latest_checkpoint["label"] or "") if latest_checkpoint else "",
            last_checkpoint_at=_iso(latest_checkpoint["created_at"]) if latest_checkpoint else "",
            execution=self._derive_execution_summary(timeline_rows),
            artifact_refs=artifact_refs,
            stale=stale,
            orphaned=orphaned,
            recovery_hints=_build_recovery_hints(
                status=str(task_row["status"] or ""),
                stale=stale,
                orphaned=orphaned,
                pending_approval_id=pending_approval_id,
                last_checkpoint_id=last_checkpoint_id,
            ),
            receipts=[
                brain_pb2.ReceiptSummary(
                    receipt_id=str(row["receipt_id"]),
                    tool_name=str(row["tool_name"] or ""),
                    step_id=str(row["step_id"] or ""),
                    runtime_class=str(row["runtime_class"] or ""),
                    risk_class=str(row["risk_class"] or ""),
                    failure_class=str(row["failure_class"] or ""),
                    logs_pointer=str(row["logs_pointer"] or ""),
                    exit_code=int(row["exit_code"] or 0),
                    audit_summary=str(row["audit_summary"] or ""),
                    artifact_manifest_json=json.dumps(row["artifact_manifest"] or [], default=str),
                    created_at=_iso(row["created_at"]),
                )
                for row in receipt_rows
            ],
            verifier_evidence=[
                brain_pb2.VerifierEvidenceReference(
                    id=str(row["id"]),
                    claim_text=str(row["claim_text"] or ""),
                    verdict=str(row["verdict"] or ""),
                    confidence=float(row["confidence"] or 0.0),
                    rationale=str(row["rationale"] or ""),
                    supporting_receipt_ids_json=json.dumps(
                        row["supporting_receipt_ids"] or [], default=str
                    ),
                    artifact_refs_json=json.dumps(row["artifact_refs"] or [], default=str),
                    created_at=_iso(row["created_at"]),
                )
                for row in verifier_rows
            ],
            session=brain_pb2.SessionProvenance(
                session_id=str(session_row["id"]) if session_row else "",
                channel=str(session_row["channel"] or "") if session_row else "",
                external_conversation_id=str(session_row["external_conversation_id"] or "")
                if session_row
                else "",
                external_thread_id=str(session_row["external_thread_id"] or "")
                if session_row
                else "",
                return_route_json=json.dumps(
                    session_row["return_route_json"] or {}, default=str
                )
                if session_row
                else "",
                metadata_json=json.dumps(
                    session_row["session_metadata_json"] or {}, default=str
                )
                if session_row
                else "",
            ),
        )
        return brain_pb2.GetTaskDetailResponse(task=detail)

    async def ListTaskTimeline(self, request, context):
        workspace_id = request.workspace_id
        task_id = request.task_id
        if not workspace_id or not task_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id and task_id are required")

        task_row = await self._get_workspace_task_row(workspace_id=workspace_id, task_id=task_id)
        if not task_row:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"Task {task_id} not found")

        events = [
            brain_pb2.TaskTimelineItem(
                type=str(payload.get("type", payload.get("event_type", ""))),
                task_id=str(payload.get("task_id", "")),
                step_id=str(payload.get("step_id", "")),
                content=str(payload.get("content", "")),
                tool_name=str(payload.get("tool_name", "")),
                tool_args=str(payload.get("tool_args", "")),
                tool_result=str(payload.get("tool_result", "")),
                approval_id=str(payload.get("approval_id", "")),
                progress={
                    str(key): str(value)
                    for key, value in (payload.get("progress") or {}).items()
                },
                event_metadata_json=json.dumps(payload.get("metadata") or {}, default=str),
                metrics_json=json.dumps(payload.get("metrics") or {}, default=str),
                created_at=str(payload.get("created_at", "")),
                journal_event_id=str(payload.get("journal_event_id", "")),
                receipt_id=_receipt_id_from_payload(payload),
                verifier_evidence_ids_json=json.dumps(
                    (payload.get("metadata") or {}).get("verifier_evidence_ids", [])
                    if isinstance(payload.get("metadata"), dict)
                    else [],
                    default=str,
                ),
            )
            for payload in await self._load_timeline_rows(workspace_id=workspace_id, task_id=task_id)
        ]
        return brain_pb2.ListTaskTimelineResponse(events=events)

    async def ListTaskCheckpoints(self, request, context):
        workspace_id = request.workspace_id
        task_id = request.task_id
        if not workspace_id or not task_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id and task_id are required")

        task_row = await self._get_workspace_task_row(workspace_id=workspace_id, task_id=task_id)
        if not task_row:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"Task {task_id} not found")

        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT id, step_index, label, created_at, journal_event_id
            FROM agent_checkpoints
            WHERE task_id = $1
            ORDER BY created_at ASC
            """,
            task_id,
        )
        checkpoints = [
            brain_pb2.TaskCheckpointItem(
                id=str(row["id"]),
                step_index=int(row["step_index"] or 0),
                label=str(row["label"] or ""),
                created_at=_iso(row["created_at"]),
                journal_event_id=str(row["journal_event_id"] or ""),
            )
            for row in rows
        ]
        return brain_pb2.ListTaskCheckpointsResponse(checkpoints=checkpoints)

    async def ListTaskArtifacts(self, request, context):
        workspace_id = request.workspace_id
        task_id = request.task_id or ""
        if not workspace_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id is required")

        task_row = None
        if task_id:
            task_row = await self._get_workspace_task_row(workspace_id=workspace_id, task_id=task_id)
            if not task_row:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"Task {task_id} not found")

        rows = await self._list_artifact_rows(workspace_id=workspace_id, task_row=task_row, limit=50)
        artifacts = [
            brain_pb2.TaskArtifactItem(
                id=str(row["id"]),
                title=str(row["title"] or ""),
                description=str(row["description"] or ""),
                component_type=str(row["component_type"] or ""),
                version=int(row["version"] or 1),
                updated_at=_iso(row["updated_at"]),
                created_by=str(row["created_by"] or ""),
                data_source=str(row["data_source"] or ""),
            )
            for row in rows
        ]
        return brain_pb2.ListTaskArtifactsResponse(artifacts=artifacts)

    async def GetApprovalAudit(self, request, context):
        workspace_id = request.workspace_id
        if not workspace_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id is required")

        task_id = request.task_id or ""
        status = request.status or ""
        pool = await get_pool()

        clauses = ["t.workspace_id = $1"]
        params: list[Any] = [workspace_id]
        if task_id:
            clauses.append(f"a.task_id = ${len(params) + 1}")
            params.append(task_id)
        if status:
            clauses.append(f"a.status = ${len(params) + 1}")
            params.append(status)

        query = f"""
            SELECT a.id, a.task_id, a.step_id, a.tool_name, a.reason,
                   a.risk_level, a.status, a.decided_by, a.decided_at,
                   a.created_at, a.tool_args, a.capability_grants_json,
                   (
                       SELECT r.receipt_id
                       FROM action_receipts r
                       WHERE r.task_id = a.task_id
                         AND r.step_id = COALESCE(a.step_id, '')
                         AND r.tool_name = COALESCE(a.tool_name, '')
                       ORDER BY r.created_at DESC
                       LIMIT 1
                   ) AS receipt_id
            FROM agent_approvals a
            JOIN agent_tasks t ON t.id = a.task_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.created_at DESC
            """
        rows = await pool.fetch(query, *params)
        approvals = [
            brain_pb2.ApprovalAuditItem(
                approval_id=str(row["id"]),
                task_id=str(row["task_id"]),
                step_id=str(row["step_id"] or ""),
                tool_name=str(row["tool_name"] or ""),
                reason=str(row["reason"] or ""),
                risk_level=str(row["risk_level"] or ""),
                status=str(row["status"] or ""),
                decided_by=str(row["decided_by"] or ""),
                decided_at=_iso(row["decided_at"]),
                created_at=_iso(row["created_at"]),
                tool_args_json=json.dumps(row["tool_args"] or {}, default=str),
                capability_grants_json=json.dumps(
                    row["capability_grants_json"] or [], default=str
                ),
                receipt_id=str(row["receipt_id"] or ""),
            )
            for row in rows
        ]
        return brain_pb2.GetApprovalAuditResponse(approvals=approvals)

    async def ListOperatorTasks(self, request, context):
        workspace_id = request.workspace_id
        if not workspace_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id is required")

        pool = await get_pool()
        params: list[Any] = [workspace_id]
        status_clause = ""
        if request.status:
            status_clause = "AND t.status = $2"
            params.append(request.status)

        rows = await pool.fetch(
            f"""
            SELECT t.id, t.goal, t.status, t.iterations, t.tool_calls_count,
                   t.result, t.error, t.created_at, t.completed_at,
                   t.conversation_id, t.plan,
                   COALESCE(p.pending_approval_count, 0) AS pending_approval_count,
                   q.status AS queue_status,
                   q.lease_expires_at,
                   s.channel AS session_channel,
                   s.external_conversation_id,
                   r.receipt_id AS latest_receipt_id
            FROM agent_tasks t
            LEFT JOIN LATERAL (
                SELECT COUNT(*)::int AS pending_approval_count
                FROM agent_approvals a
                WHERE a.task_id = t.id
                  AND a.status = 'pending'
            ) p ON TRUE
            LEFT JOIN LATERAL (
                SELECT status, lease_expires_at
                FROM task_queue
                WHERE agent_task_id = t.id OR terminal_task_id = t.id
                ORDER BY created_at DESC
                LIMIT 1
            ) q ON TRUE
            LEFT JOIN LATERAL (
                SELECT channel, external_conversation_id
                FROM agent_sessions
                WHERE task_id = t.id
                ORDER BY last_activity DESC
                LIMIT 1
            ) s ON TRUE
            LEFT JOIN LATERAL (
                SELECT receipt_id
                FROM action_receipts
                WHERE task_id = t.id
                ORDER BY created_at DESC
                LIMIT 1
            ) r ON TRUE
            WHERE t.workspace_id = $1
            {status_clause}
            ORDER BY t.created_at DESC
            LIMIT 100
            """,
            *params,
        )

        now = datetime.now(timezone.utc)
        items: list[brain_pb2.OperatorTaskItem] = []
        for row in rows:
            plan = _load_jsonb(row["plan"])
            current_step, total_steps = _progress_from_plan(plan)
            lease_expires_at = row["lease_expires_at"]
            queue_status = str(row["queue_status"] or "")
            stale = bool(queue_status in _ACTIVE_QUEUE_STATUSES and lease_expires_at and lease_expires_at <= now)
            orphaned = bool(queue_status == "running" and stale)
            items.append(
                brain_pb2.OperatorTaskItem(
                    summary=brain_pb2.TaskSummary(
                        id=str(row["id"]),
                        goal=str(row["goal"] or ""),
                        status=str(row["status"] or ""),
                        iterations=int(row["iterations"] or 0),
                        tool_calls=int(row["tool_calls_count"] or 0),
                        result=str(row["result"] or ""),
                        error=str(row["error"] or ""),
                        created_at=_iso(row["created_at"]),
                        completed_at=_iso(row["completed_at"]),
                    ),
                    pending_approval_count=int(row["pending_approval_count"] or 0),
                    stale=stale,
                    orphaned=orphaned,
                    current_step=current_step,
                    total_steps=total_steps,
                    lease_expires_at=_iso(lease_expires_at),
                    queue_status=queue_status,
                    conversation_id=str(row["conversation_id"] or ""),
                    session_channel=str(row["session_channel"] or ""),
                    external_conversation_id=str(row["external_conversation_id"] or ""),
                    latest_receipt_id=str(row["latest_receipt_id"] or ""),
                )
            )
        return brain_pb2.ListOperatorTasksResponse(tasks=items)

    async def GetRuntimeProfile(self, request, context):
        workspace_id = request.workspace_id
        if not workspace_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id is required")

        pool = await get_pool()
        workspace_agent = await pool.fetchrow(
            """
            SELECT autonomy_policy, persona_version, default_mode, runtime_defaults
            FROM workspace_agents
            WHERE workspace_id = $1
            """,
            workspace_id,
        )
        provider_rows = await pool.fetch(
            """
            SELECT provider, model, is_default
            FROM workspace_provider_config
            WHERE workspace_id = $1
            ORDER BY is_default DESC, provider ASC
            """,
            workspace_id,
        )
        routing_rows = await pool.fetch(
            """
            SELECT step_type, provider, model
            FROM model_routing_config
            WHERE workspace_id = $1
            ORDER BY step_type ASC
            """,
            workspace_id,
        )

        execution_runtime = getattr(runtime, "execution_runtime", None)
        capabilities = execution_runtime.capabilities.as_dict() if execution_runtime else {}
        runtime_mode = str(capabilities.get("mode", getattr(runtime, "feature_mode", "core")))

        subsystem_bootstrapper = getattr(runtime, "subsystem_bootstrapper", None)
        subsystem_snapshot = subsystem_bootstrapper.snapshot() if subsystem_bootstrapper else {}
        subsystems: list[brain_pb2.RuntimeSubsystem] = []
        for name, raw in subsystem_snapshot.items():
            if isinstance(raw, dict):
                subsystems.append(
                    brain_pb2.RuntimeSubsystem(
                        name=str(name),
                        status=str(raw.get("status", "unknown")),
                        detail=str(raw.get("error") or raw.get("detail") or ""),
                    )
                )
            else:
                subsystems.append(
                    brain_pb2.RuntimeSubsystem(
                        name=str(name),
                        status=str(raw or "unknown"),
                        detail="",
                    )
                )

        provider_routes = [
            brain_pb2.ProviderRoutingItem(
                provider=str(row["provider"] or ""),
                model=str(row["model"] or ""),
                is_default=bool(row["is_default"]),
                source="workspace_default",
            )
            for row in provider_rows
        ]
        provider_routes.extend(
            brain_pb2.ProviderRoutingItem(
                provider=str(row["provider"] or ""),
                model=str(row["model"] or ""),
                is_default=False,
                source=f"step_override:{row['step_type']}",
            )
            for row in routing_rows
        )

        host_mounts: list[brain_pb2.RuntimeMount] = []
        if request.include_sensitive:
            try:
                from agent.tools.fs.utils import _get_host_mounts

                write_enabled = os.getenv("KESTREL_ENABLE_HOST_WRITE", "false").lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                host_mounts = [
                    brain_pb2.RuntimeMount(path=mount, mode="rw" if write_enabled else "ro")
                    for mount in _get_host_mounts()
                ]
            except Exception as exc:
                logger.warning("Failed to enumerate host mounts for runtime profile: %s", exc)

        profile = brain_pb2.RuntimeProfile(
            runtime_mode=runtime_mode,
            policy_name=str(
                workspace_agent["autonomy_policy"] if workspace_agent else "moderate"
            ),
            policy_version=str(
                workspace_agent["persona_version"] if workspace_agent else 1
            ),
            docker_enabled=bool(capabilities.get("supports_docker_execution")),
            native_enabled=bool(capabilities.get("supports_native_execution")),
            hybrid_fallback_visible=runtime_mode == "hybrid",
            host_mounts=host_mounts,
            subsystems=subsystems,
            provider_routes=provider_routes,
            runtime_capabilities={str(key): json.dumps(value) if isinstance(value, list) else str(value) for key, value in capabilities.items()},
        )
        return brain_pb2.GetRuntimeProfileResponse(profile=profile)
