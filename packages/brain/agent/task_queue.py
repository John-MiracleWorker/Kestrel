from __future__ import annotations

"""
Queue-backed autonomy kernel for interactive and background agent execution.
"""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from agent.execution_context import (
    ExecutionContext,
    Opportunity,
    QueuedTaskRecord,
    WorkspaceAgentProfile,
)
from agent.task_events import persist_task_event_payload
from agent.task_runtime import build_task_runtime_bundle
from agent.types import TaskEventType, TaskStatus

logger = logging.getLogger("brain.agent.task_queue")

_STREAMED_EVENT_TYPES = frozenset({
    TaskEventType.PLAN_CREATED.value,
    TaskEventType.STEP_STARTED.value,
    TaskEventType.TOOL_CALLED.value,
    TaskEventType.TOOL_RESULT.value,
    TaskEventType.STEP_COMPLETE.value,
    TaskEventType.APPROVAL_NEEDED.value,
    TaskEventType.THINKING.value,
    TaskEventType.TASK_COMPLETE.value,
    TaskEventType.TASK_FAILED.value,
    TaskEventType.TASK_PAUSED.value,
    TaskEventType.SIMULATION_COMPLETE.value,
})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_queue_source(source: str) -> str:
    normalized = str(source or "automation").strip().lower()
    if normalized == "user":
        return "user"
    if normalized.startswith("daemon"):
        return "daemon"
    if normalized.startswith("heartbeat"):
        return "heartbeat"
    if normalized.startswith("self_improve"):
        return "self_improve"
    return "automation"


class WorkspaceAgentStore:
    def __init__(self, pool):
        self._pool = pool

    async def ensure_profile(
        self,
        workspace_id: str,
        *,
        default_mode: str = "ops",
        autonomy_policy: str = "moderate",
    ) -> WorkspaceAgentProfile:
        memory_namespace = f"workspace:{workspace_id}"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO workspace_agents
                    (workspace_id, default_mode, autonomy_policy, memory_namespace, tool_policy_bundle, persona_version, runtime_defaults)
                VALUES ($1, $2, $3, $4, $5::jsonb, 1, '{}'::jsonb)
                ON CONFLICT (workspace_id) DO UPDATE SET
                    default_mode = EXCLUDED.default_mode,
                    autonomy_policy = EXCLUDED.autonomy_policy,
                    updated_at = now()
                RETURNING *
                """,
                workspace_id,
                default_mode,
                autonomy_policy,
                memory_namespace,
                json.dumps(["chat", "research", "coding", "ops"]),
            )
        return WorkspaceAgentProfile.from_record(dict(row))

    async def get_by_workspace(self, workspace_id: str) -> Optional[WorkspaceAgentProfile]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM workspace_agents WHERE workspace_id = $1",
                workspace_id,
            )
        return WorkspaceAgentProfile.from_record(dict(row)) if row else None


class TaskEnqueuer:
    def __init__(self, pool, workspace_agent_store: WorkspaceAgentStore):
        self._pool = pool
        self._profiles = workspace_agent_store

    async def enqueue(
        self,
        *,
        workspace_id: str,
        user_id: str,
        goal: str,
        source: str = "user",
        priority: int = 5,
        agent_task_id: Optional[str] = None,
        payload_json: Optional[dict] = None,
        dedupe_key: str = "",
        parent_queue_id: Optional[str] = None,
        trigger_kind: str = "task",
        scheduled_at: Optional[datetime] = None,
    ) -> QueuedTaskRecord:
        profile = await self._profiles.ensure_profile(workspace_id)
        normalized_source = _normalize_queue_source(source)
        queue_id = str(uuid.uuid4())
        payload_json = dict(payload_json or {})
        payload_json.setdefault("source_label", source)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO task_queue
                    (id, workspace_id, user_id, goal, status, priority, source,
                     agent_task_id, agent_profile_id, payload_json, dedupe_key,
                     parent_queue_id, trigger_kind, scheduled_at)
                VALUES ($1, $2, $3, $4, 'queued', $5, $6,
                        NULLIF($7, '')::uuid, $8, $9::jsonb, $10,
                        NULLIF($11, '')::uuid, $12, $13)
                RETURNING *
                """,
                queue_id,
                workspace_id,
                user_id,
                goal,
                priority,
                normalized_source,
                agent_task_id or "",
                profile.id,
                json.dumps(payload_json),
                dedupe_key,
                parent_queue_id or "",
                trigger_kind,
                scheduled_at,
            )
        return QueuedTaskRecord.from_record(dict(row))

    async def claim_jobs(
        self,
        *,
        worker_id: str,
        limit: int = 1,
        lease_seconds: int = 300,
    ) -> list[QueuedTaskRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH candidates AS (
                    SELECT id
                    FROM task_queue
                    WHERE (
                        (
                            status = 'queued'
                            AND (scheduled_at IS NULL OR scheduled_at <= now())
                        ) OR (
                            status = 'running'
                            AND lease_expires_at IS NOT NULL
                            AND lease_expires_at <= now()
                        )
                    )
                    ORDER BY priority DESC, created_at ASC
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE task_queue AS tq
                SET status = 'running',
                    started_at = COALESCE(started_at, now()),
                    lease_owner = $1,
                    lease_expires_at = now() + make_interval(secs => $3)
                FROM candidates
                WHERE tq.id = candidates.id
                RETURNING tq.*
                """,
                worker_id,
                limit,
                lease_seconds,
            )
        return [QueuedTaskRecord.from_record(dict(row)) for row in rows]

    async def update_checkpoint(self, queue_id: str, checkpoint_json: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET checkpoint_json = $2::jsonb,
                    updated_at = now()
                WHERE id = $1
                """,
                queue_id,
                json.dumps(checkpoint_json or {}),
            )

    async def heartbeat_lease(self, queue_id: str, worker_id: str, lease_seconds: int = 300) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET lease_expires_at = now() + make_interval(secs => $3),
                    updated_at = now()
                WHERE id = $1 AND lease_owner = $2 AND status = 'running'
                """,
                queue_id,
                worker_id,
                lease_seconds,
            )

    async def complete(self, queue_id: str, *, terminal_task_id: Optional[str] = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET status = 'complete',
                    completed_at = now(),
                    terminal_task_id = COALESCE(NULLIF($2, '')::uuid, terminal_task_id),
                    lease_owner = '',
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE id = $1
                """,
                queue_id,
                terminal_task_id or "",
            )

    async def fail(self, queue_id: str, *, error: str, terminal_task_id: Optional[str] = None) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE task_queue
                SET retry_count = retry_count + 1,
                    error = $2,
                    updated_at = now(),
                    terminal_task_id = COALESCE(NULLIF($3, '')::uuid, terminal_task_id)
                WHERE id = $1
                RETURNING retry_count, max_retries
                """,
                queue_id,
                error,
                terminal_task_id or "",
            )
            if not row:
                return
            status = "failed" if row["retry_count"] >= row["max_retries"] else "queued"
            await conn.execute(
                """
                UPDATE task_queue
                SET status = $2,
                    lease_owner = '',
                    lease_expires_at = NULL,
                    updated_at = now(),
                    completed_at = CASE WHEN $2 = 'failed' THEN now() ELSE completed_at END
                WHERE id = $1
                """,
                queue_id,
                status,
            )

    async def cancel(self, queue_id: str, *, error: str = "Cancelled", terminal_task_id: Optional[str] = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET status = 'cancelled',
                    error = $2,
                    completed_at = now(),
                    terminal_task_id = COALESCE(NULLIF($3, '')::uuid, terminal_task_id),
                    lease_owner = '',
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE id = $1
                """,
                queue_id,
                error,
                terminal_task_id or "",
            )

    async def get(self, queue_id: str) -> Optional[QueuedTaskRecord]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM task_queue WHERE id = $1", queue_id)
        return QueuedTaskRecord.from_record(dict(row)) if row else None

    async def get_latest_for_task(self, task_id: str) -> Optional[QueuedTaskRecord]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM task_queue
                WHERE agent_task_id = $1 OR terminal_task_id = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                task_id,
            )
        return QueuedTaskRecord.from_record(dict(row)) if row else None

    async def set_opportunity_state(self, opportunity_id: str, state: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_opportunities
                SET state = $2,
                    updated_at = now()
                WHERE id = NULLIF($1, '')::uuid
                """,
                opportunity_id,
                state,
            )


class OpportunityEngine:
    def __init__(self, pool, workspace_agent_store: WorkspaceAgentStore, task_enqueuer: TaskEnqueuer):
        self._pool = pool
        self._profiles = workspace_agent_store
        self._enqueuer = task_enqueuer

    async def record_opportunity(
        self,
        *,
        workspace_id: str,
        source: str,
        title: str,
        goal_template: str,
        score: float,
        severity: str = "info",
        dedupe_key: str = "",
        payload_json: Optional[dict] = None,
        expires_at: Optional[datetime] = None,
    ) -> Opportunity:
        profile = await self._profiles.ensure_profile(workspace_id)
        opportunity = Opportunity.create(
            agent_profile_id=profile.id,
            source=source,
            title=title,
            goal_template=goal_template,
            score=score,
            severity=severity,
            dedupe_key=dedupe_key or f"{source}:{workspace_id}:{title}",
            expires_at=expires_at.isoformat() if expires_at else "",
            payload_json=payload_json or {},
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_opportunities
                    (id, agent_profile_id, source, title, goal_template, score, severity, dedupe_key, expires_at, state, payload_json)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULLIF($9, '')::timestamptz, 'pending', $10::jsonb)
                ON CONFLICT (agent_profile_id, dedupe_key)
                DO UPDATE SET
                    score = GREATEST(agent_opportunities.score, EXCLUDED.score),
                    severity = EXCLUDED.severity,
                    goal_template = EXCLUDED.goal_template,
                    payload_json = EXCLUDED.payload_json,
                    expires_at = COALESCE(EXCLUDED.expires_at, agent_opportunities.expires_at),
                    state = CASE
                        WHEN agent_opportunities.state IN ('queued', 'completed') THEN agent_opportunities.state
                        ELSE 'pending'
                    END,
                    updated_at = now()
                """,
                opportunity.id,
                opportunity.agent_profile_id,
                opportunity.source,
                opportunity.title,
                opportunity.goal_template,
                opportunity.score,
                opportunity.severity,
                opportunity.dedupe_key,
                opportunity.expires_at,
                json.dumps(opportunity.payload_json),
            )
        return opportunity

    async def enqueue_best(self, *, workspace_id: str, user_id: str, limit: int = 1) -> list[QueuedTaskRecord]:
        profile = await self._profiles.ensure_profile(workspace_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM agent_opportunities
                WHERE agent_profile_id = $1
                  AND state = 'pending'
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY score DESC, created_at ASC
                LIMIT $2
                """,
                profile.id,
                limit,
            )
        queued = []
        for row in rows:
            job = await self._enqueuer.enqueue(
                workspace_id=workspace_id,
                user_id=user_id,
                goal=row["goal_template"],
                source=row["source"],
                priority=max(1, min(10, int(round(float(row["score"]) * 10)))),
                dedupe_key=row["dedupe_key"],
                trigger_kind="opportunity",
                payload_json={"opportunity_id": str(row["id"]), "title": row["title"]},
            )
            queued.append(job)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_opportunities SET state = 'queued' WHERE id = $1",
                    row["id"],
                )
        return queued


@dataclass
class JobRunner:
    runtime_ctx: any
    pool: any

    async def run(self, job: QueuedTaskRecord) -> None:
        task = await self.runtime_ctx.agent_persistence.get_task(job.agent_task_id or job.terminal_task_id)
        if not task:
            raise RuntimeError(f"Task {job.agent_task_id or job.terminal_task_id} not found for queued job {job.id}")

        profile = await self.runtime_ctx.workspace_agent_store.ensure_profile(task.workspace_id)
        execution_context = ExecutionContext.create(
            task_id=task.id,
            queue_id=job.id,
            agent_profile_id=profile.id,
            workspace_id=task.workspace_id,
            user_id=task.user_id,
            source=job.source,
            budgets=task.config.to_dict(),
            permissions={"tool_policy_bundle": list(profile.tool_policy_bundle)},
            autonomy_policy=profile.autonomy_policy,
            services={
                "cron_scheduler": getattr(self.runtime_ctx, "cron_scheduler", None),
                "automation_builder": getattr(self.runtime_ctx, "automation_builder", None),
                "daemon_manager": getattr(self.runtime_ctx, "daemon_manager", None),
                "policy_engine": getattr(self.runtime_ctx, "policy_engine", None),
                "ui_manager": getattr(self.runtime_ctx, "ui_artifact_manager", None),
                "ui_artifact_manager": getattr(self.runtime_ctx, "ui_artifact_manager", None),
            },
        )
        task.execution_context = execution_context

        self.runtime_ctx.running_tasks[task.id] = task
        if getattr(self.runtime_ctx, "session_manager", None):
            await self.runtime_ctx.session_manager.register_session(
                session_id=task.id,
                task_id=task.id,
                workspace_id=task.workspace_id,
                user_id=task.user_id,
                agent_type="task",
                model="",
                goal=task.goal,
                agent_profile_id=profile.id,
                channel=job.source,
                prunable_after=(_utcnow() + timedelta(days=7)).isoformat(),
            )

        async def _refresh_checkpoint_snapshot() -> None:
            checkpoint_manager = getattr(self.runtime_ctx, "checkpoint_manager", None)
            if not checkpoint_manager:
                return
            latest = await checkpoint_manager.get_latest_checkpoint(task.id)
            if not latest:
                return
            await self.runtime_ctx.task_enqueuer.update_checkpoint(
                job.id,
                {
                    "checkpoint_id": latest.id,
                    "label": latest.label,
                    "step_index": latest.step_index,
                    "created_at": latest.created_at,
                },
            )

        async def _event_callback(event_type: str, payload: dict) -> None:
            if event_type in _STREAMED_EVENT_TYPES:
                if event_type in {
                    TaskEventType.STEP_COMPLETE.value,
                    TaskEventType.APPROVAL_NEEDED.value,
                    TaskEventType.TASK_COMPLETE.value,
                    TaskEventType.TASK_FAILED.value,
                    TaskEventType.TASK_PAUSED.value,
                }:
                    await _refresh_checkpoint_snapshot()
                return

            serialized = {
                "type": event_type,
                "event_type": event_type,
                "task_id": task.id,
                "step_id": payload.get("step_id", ""),
                "content": payload.get("content") or payload.get("result", ""),
                "tool_name": payload.get("tool_name", ""),
                "tool_args": json.dumps(payload.get("tool_args", {})) if isinstance(payload.get("tool_args"), dict) else str(payload.get("tool_args", "")),
                "tool_result": payload.get("tool_result") or payload.get("result", ""),
                "approval_id": payload.get("approval_id", ""),
                "progress": payload.get("progress") or {},
            }
            await persist_task_event_payload(
                serialized,
                workspace_id=task.workspace_id,
                user_id=task.user_id,
            )
            if event_type in {"checkpoint_saved", TaskEventType.MODEL_ROUTED.value}:
                await _refresh_checkpoint_snapshot()

        bundle = await build_task_runtime_bundle(
            task=task,
            runtime_ctx=self.runtime_ctx,
            pool=self.pool,
            event_callback=_event_callback,
            model_override=str(job.payload_json.get("model_override", "") or ""),
        )
        try:
            async for event in bundle.task_loop.run(task):
                payload = {
                    "type": event.type.value,
                    "event_type": event.type.value,
                    "task_id": event.task_id,
                    "step_id": event.step_id or "",
                    "content": event.content,
                    "tool_name": event.tool_name or "",
                    "tool_args": event.tool_args or "",
                    "tool_result": event.tool_result or "",
                    "approval_id": event.approval_id or "",
                    "progress": event.progress or {},
                }
                await persist_task_event_payload(
                    payload,
                    workspace_id=task.workspace_id,
                    user_id=task.user_id,
                )
                if event.type.value in {
                    TaskEventType.STEP_COMPLETE.value,
                    TaskEventType.APPROVAL_NEEDED.value,
                    TaskEventType.TASK_COMPLETE.value,
                    TaskEventType.TASK_FAILED.value,
                    TaskEventType.TASK_PAUSED.value,
                }:
                    await _refresh_checkpoint_snapshot()
        finally:
            await _refresh_checkpoint_snapshot()
            self.runtime_ctx.running_tasks.pop(task.id, None)
            if getattr(self.runtime_ctx, "session_manager", None):
                await self.runtime_ctx.session_manager.deregister_session(task.id)


class TaskDispatcher:
    def __init__(
        self,
        *,
        enqueuer: TaskEnqueuer,
        runner: JobRunner,
        concurrency: int = 2,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = 300,
    ):
        self._enqueuer = enqueuer
        self._runner = runner
        self._concurrency = concurrency
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._worker_id = f"{os.uname().nodename}:{uuid.uuid4()}"
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._active: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)

    async def _loop(self) -> None:
        while self._running:
            try:
                available = self._concurrency - len(self._active)
                if available <= 0:
                    await asyncio.sleep(self._poll_interval_seconds)
                    continue

                jobs = await self._enqueuer.claim_jobs(
                    worker_id=self._worker_id,
                    limit=available,
                    lease_seconds=self._lease_seconds,
                )
                if not jobs:
                    await asyncio.sleep(self._poll_interval_seconds)
                    continue

                for job in jobs:
                    task = asyncio.create_task(self._run_job(job))
                    self._active[job.id] = task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Task dispatcher loop failed: %s", exc, exc_info=True)
                await asyncio.sleep(self._poll_interval_seconds)

    async def _run_job(self, job: QueuedTaskRecord) -> None:
        lease_task = asyncio.create_task(self._lease_heartbeat(job.id))
        try:
            await self._runner.run(job)
            await self._enqueuer.complete(job.id, terminal_task_id=job.agent_task_id)
            opportunity_id = str(job.payload_json.get("opportunity_id", "") or "")
            if opportunity_id:
                await self._enqueuer.set_opportunity_state(opportunity_id, "completed")
        except Exception as exc:
            logger.error("Queued job %s failed: %s", job.id, exc, exc_info=True)
            await self._enqueuer.fail(job.id, error=str(exc), terminal_task_id=job.agent_task_id)
            opportunity_id = str(job.payload_json.get("opportunity_id", "") or "")
            if opportunity_id:
                await self._enqueuer.set_opportunity_state(opportunity_id, "pending")
            task = await self._runner.runtime_ctx.agent_persistence.get_task(job.agent_task_id or "")
            if task:
                task.status = TaskStatus.FAILED
                task.error = str(exc)
                await self._runner.runtime_ctx.agent_persistence.update_task(task)
                await persist_task_event_payload(
                    {
                        "type": TaskEventType.TASK_FAILED.value,
                        "event_type": TaskEventType.TASK_FAILED.value,
                        "task_id": task.id,
                        "step_id": "",
                        "content": str(exc),
                        "tool_name": "",
                        "tool_args": "",
                        "tool_result": "",
                        "approval_id": "",
                        "progress": {},
                    },
                    workspace_id=task.workspace_id,
                    user_id=task.user_id,
                )
        finally:
            lease_task.cancel()
            try:
                await lease_task
            except asyncio.CancelledError:
                pass
            self._active.pop(job.id, None)

    async def _lease_heartbeat(self, queue_id: str) -> None:
        interval = max(10, int(self._lease_seconds / 3))
        while self._running:
            await asyncio.sleep(interval)
            await self._enqueuer.heartbeat_lease(
                queue_id,
                self._worker_id,
                lease_seconds=self._lease_seconds,
            )

    async def cancel_job(self, queue_id: str) -> bool:
        active_task = self._active.get(queue_id)
        if not active_task:
            return False
        active_task.cancel()
        return True
