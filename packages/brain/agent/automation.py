"""
Cron & Webhook Automation — scheduled tasks and event-triggered agents.

Inspired by OpenClaw's automation features:
  - Cron jobs: schedule recurring agent tasks with cron expressions
  - Webhooks: trigger agent tasks from external HTTP events
  - Gmail Pub/Sub: email-triggered automation (future)

Jobs are persisted in PostgreSQL and managed by an asyncio scheduler
that runs inside the Brain service.
"""

import asyncio
import json
import logging
import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.automation")


# ── Cron Expression Helpers ──────────────────────────────────────────

def parse_cron_field(field_str: str, min_val: int, max_val: int) -> list[int]:
    """Parse a single cron field into a list of valid values."""
    values = set()

    for part in field_str.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(min_val, max_val + 1))
        elif "/" in part:
            base, step = part.split("/")
            start = min_val if base == "*" else int(base)
            step = int(step)
            values.update(range(start, max_val + 1, step))
        elif "-" in part:
            low, high = part.split("-")
            values.update(range(int(low), int(high) + 1))
        else:
            values.add(int(part))

    return sorted(v for v in values if min_val <= v <= max_val)


def cron_matches_now(expression: str, now: datetime = None) -> bool:
    """Check if a cron expression matches the current minute."""
    if now is None:
        now = datetime.now(timezone.utc)

    parts = expression.strip().split()
    if len(parts) != 5:
        return False

    minute_f, hour_f, day_f, month_f, dow_f = parts

    return (
        now.minute in parse_cron_field(minute_f, 0, 59) and
        now.hour in parse_cron_field(hour_f, 0, 23) and
        now.day in parse_cron_field(day_f, 1, 31) and
        now.month in parse_cron_field(month_f, 1, 12) and
        now.weekday() in parse_cron_field(dow_f, 0, 6)
    )


# ── Data Models ──────────────────────────────────────────────────────

class JobStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


@dataclass
class CronJob:
    """A scheduled recurring agent task."""
    id: str
    workspace_id: str
    user_id: str
    name: str
    description: str
    cron_expression: str  # Standard 5-field cron (min hour day month dow)
    goal: str             # Agent task goal to execute
    status: JobStatus = JobStatus.ACTIVE
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    run_count: int = 0
    max_runs: Optional[int] = None   # None = unlimited
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "cron_expression": self.cron_expression,
            "goal": self.goal,
            "status": self.status.value,
            "last_run": self.last_run,
            "run_count": self.run_count,
            "max_runs": self.max_runs,
            "created_at": self.created_at,
        }


@dataclass
class WebhookEndpoint:
    """A webhook that triggers an agent task on HTTP request."""
    id: str
    workspace_id: str
    user_id: str
    name: str
    description: str
    goal_template: str      # Template with {payload} and {headers} placeholders
    secret: str = ""        # HMAC secret for signature verification
    status: JobStatus = JobStatus.ACTIVE
    trigger_count: int = 0
    allowed_sources: list[str] = field(default_factory=list)  # IP allowlist
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "goal_template": self.goal_template,
            "status": self.status.value,
            "trigger_count": self.trigger_count,
            "has_secret": bool(self.secret),
            "created_at": self.created_at,
        }

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        """Verify HMAC-SHA256 signature of incoming webhook."""
        if not self.secret:
            return True  # No secret configured
        expected = hmac.new(
            self.secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(f"sha256={expected}", signature)


# ── Cron Scheduler ───────────────────────────────────────────────────

class CronScheduler:
    """
    Manages cron jobs — evaluates every minute and triggers matching jobs.

    Runs as a background asyncio task inside the Brain service.
    """

    def __init__(self, pool, task_launcher=None):
        self._pool = pool
        self._launcher = task_launcher  # Callable to start an agent task
        self._jobs: dict[str, CronJob] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the cron scheduler loop."""
        if self._running:
            return
        self._running = True
        await self._load_jobs()
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Cron scheduler started with {len(self._jobs)} jobs")

    async def stop(self) -> None:
        """Stop the cron scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Cron scheduler stopped")

    async def _loop(self) -> None:
        """Main scheduler loop — runs every 60 seconds."""
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                for job in list(self._jobs.values()):
                    if job.status != JobStatus.ACTIVE:
                        continue
                    if job.max_runs and job.run_count >= job.max_runs:
                        continue
                    if cron_matches_now(job.cron_expression, now):
                        await self._trigger_job(job)
            except Exception as e:
                logger.error(f"Cron loop error: {e}")

            # Wait until the next minute boundary
            now = datetime.now(timezone.utc)
            wait = 60 - now.second
            await asyncio.sleep(wait)

    async def _trigger_job(self, job: CronJob) -> None:
        """Trigger a cron job by launching an agent task."""
        logger.info(f"Triggering cron job: {job.name} ({job.id})")

        now = datetime.now(timezone.utc)
        job.last_run = now.isoformat()
        job.run_count += 1

        # Update DB
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE automation_cron_jobs
                    SET last_run = $2, run_count = $3
                    WHERE id = $1
                    """,
                    uuid.UUID(job.id), now, job.run_count,
                )
        except Exception as e:
            logger.error(f"Failed to update cron job: {e}")

        # Launch agent task
        if self._launcher:
            try:
                await self._launcher(
                    workspace_id=job.workspace_id,
                    user_id=job.user_id,
                    goal=job.goal,
                    source=f"cron:{job.name}",
                )
            except Exception as e:
                logger.error(f"Failed to launch cron task: {e}")

    async def create_job(
        self,
        workspace_id: str,
        user_id: str,
        name: str,
        description: str,
        cron_expression: str,
        goal: str,
        max_runs: int = None,
    ) -> CronJob:
        """Create a new cron job."""
        now = datetime.now(timezone.utc)
        job = CronJob(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            description=description,
            cron_expression=cron_expression,
            goal=goal,
            max_runs=max_runs,
            created_at=now.isoformat(),
        )

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO automation_cron_jobs
                        (id, workspace_id, user_id, name, description,
                         cron_expression, goal, status, max_runs, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    uuid.UUID(job.id), uuid.UUID(workspace_id), uuid.UUID(user_id),
                    name, description, cron_expression, goal,
                    JobStatus.ACTIVE.value, max_runs, now,
                )
        except Exception as e:
            logger.error(f"Failed to persist cron job: {e}")

        self._jobs[job.id] = job
        logger.info(f"Cron job created: {name} ({cron_expression})")
        return job

    async def delete_job(self, job_id: str) -> bool:
        """Delete a cron job."""
        self._jobs.pop(job_id, None)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM automation_cron_jobs WHERE id = $1", uuid.UUID(job_id)
                )
            return True
        except Exception as e:
            logger.error(f"Failed to delete cron job: {e}")
            return False

    async def list_jobs(self, workspace_id: str) -> list[dict]:
        """List all cron jobs for a workspace."""
        return [
            j.to_dict() for j in self._jobs.values()
            if j.workspace_id == workspace_id
        ]

    async def _load_jobs(self) -> None:
        """Load all active cron jobs from the database."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM automation_cron_jobs WHERE status = 'active'"
                )
            for row in rows:
                job = CronJob(
                    id=row["id"],
                    workspace_id=row["workspace_id"],
                    user_id=row["user_id"],
                    name=row["name"],
                    description=row["description"],
                    cron_expression=row["cron_expression"],
                    goal=row["goal"],
                    status=JobStatus(row["status"]),
                    last_run=str(row["last_run"]) if row["last_run"] else None,
                    run_count=row["run_count"],
                    max_runs=row["max_runs"],
                    created_at=str(row["created_at"]),
                )
                self._jobs[job.id] = job
        except Exception as e:
            logger.error(f"Failed to load cron jobs: {e}")


# ── Webhook Handler ──────────────────────────────────────────────────

class WebhookHandler:
    """
    Manages webhook endpoints — validates incoming requests
    and triggers agent tasks with the webhook payload.
    """

    def __init__(self, pool, task_launcher=None):
        self._pool = pool
        self._launcher = task_launcher
        self._endpoints: dict[str, WebhookEndpoint] = {}

    async def load_endpoints(self) -> None:
        """Load all webhook endpoints from the database."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM automation_webhooks WHERE status = 'active'"
                )
            for row in rows:
                ep = WebhookEndpoint(
                    id=row["id"],
                    workspace_id=row["workspace_id"],
                    user_id=row["user_id"],
                    name=row["name"],
                    description=row["description"],
                    goal_template=row["goal_template"],
                    secret=row.get("secret", ""),
                    status=JobStatus(row["status"]),
                    trigger_count=row["trigger_count"],
                    created_at=str(row["created_at"]),
                )
                self._endpoints[ep.id] = ep
        except Exception as e:
            logger.error(f"Failed to load webhooks: {e}")

    async def handle_webhook(
        self,
        webhook_id: str,
        payload: bytes,
        headers: dict[str, str],
        source_ip: str = "",
    ) -> dict[str, Any]:
        """Process an incoming webhook request."""
        endpoint = self._endpoints.get(webhook_id)
        if not endpoint:
            return {"success": False, "error": "Webhook not found", "status": 404}

        if endpoint.status != JobStatus.ACTIVE:
            return {"success": False, "error": "Webhook disabled", "status": 403}

        # Verify signature if secret configured
        signature = headers.get("x-signature-256", headers.get("x-hub-signature-256", ""))
        if endpoint.secret and not endpoint.verify_signature(payload, signature):
            logger.warning(f"Webhook {webhook_id}: signature verification failed")
            return {"success": False, "error": "Invalid signature", "status": 401}

        # IP allowlist check
        if endpoint.allowed_sources and source_ip not in endpoint.allowed_sources:
            return {"success": False, "error": "Source IP not allowed", "status": 403}

        # Parse payload
        try:
            payload_data = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload_data = payload.decode(errors="replace")

        # Build goal from template
        goal = endpoint.goal_template.replace(
            "{payload}", json.dumps(payload_data, indent=2)[:5000]
        ).replace(
            "{headers}", json.dumps(dict(headers))[:1000]
        )

        # Update trigger count
        endpoint.trigger_count += 1
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE automation_webhooks SET trigger_count = $2 WHERE id = $1",
                    webhook_id, endpoint.trigger_count,
                )
        except Exception:
            pass

        # Launch agent task
        if self._launcher:
            try:
                await self._launcher(
                    workspace_id=endpoint.workspace_id,
                    user_id=endpoint.user_id,
                    goal=goal,
                    source=f"webhook:{endpoint.name}",
                )
            except Exception as e:
                logger.error(f"Failed to launch webhook task: {e}")
                return {"success": False, "error": str(e), "status": 500}

        logger.info(f"Webhook triggered: {endpoint.name} ({webhook_id})")
        return {"success": True, "message": "Task launched", "status": 200}

    async def create_endpoint(
        self,
        workspace_id: str,
        user_id: str,
        name: str,
        description: str,
        goal_template: str,
        secret: str = "",
    ) -> WebhookEndpoint:
        """Create a new webhook endpoint."""
        ep = WebhookEndpoint(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            description=description,
            goal_template=goal_template,
            secret=secret,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO automation_webhooks
                        (id, workspace_id, user_id, name, description,
                         goal_template, secret, status, trigger_count, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, $9)
                    """,
                    ep.id, workspace_id, user_id, name, description,
                    goal_template, secret, JobStatus.ACTIVE.value, ep.created_at,
                )
        except Exception as e:
            logger.error(f"Failed to persist webhook: {e}")

        self._endpoints[ep.id] = ep
        return ep

    async def delete_endpoint(self, webhook_id: str) -> bool:
        """Delete a webhook endpoint."""
        self._endpoints.pop(webhook_id, None)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM automation_webhooks WHERE id = $1", webhook_id
                )
            return True
        except Exception as e:
            logger.error(f"Failed to delete webhook: {e}")
            return False

    async def list_endpoints(self, workspace_id: str) -> list[dict]:
        """List all webhooks for a workspace."""
        return [
            e.to_dict() for e in self._endpoints.values()
            if e.workspace_id == workspace_id
        ]
