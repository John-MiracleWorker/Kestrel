"""
Smart Monitors — proactive background watchers that emit web notifications.

Replaces the previous stub implementations with real checks:
  - Stuck tasks   → queries the DB for long-running tasks
  - Disk/Memory   → uses psutil for resource thresholds
  - Key expiry    → queries the api_keys table for soon-to-expire keys
  - Stale PRs     → placeholder until GitHub MCP is wired
  - Dep audit     → placeholder until pip-audit / npm-audit is wired
  - CI status     → placeholder until GitHub Actions API is wired
  - Cron janitor  → cleans stale retry cron jobs from the DB
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("brain.smart_monitors")


class SmartMonitors:
    """
    Proactive monitors for background tasks.
    Triggers web notifications via NotificationRouter.
    """
    def __init__(self, pool, notification_router, metrics, agent_persistence):
        self._pool = pool
        self._router = notification_router
        self._metrics = metrics
        self._agent_persistence = agent_persistence
        self._running = False
        self._tasks: list[asyncio.Task] = []

    def start(self):
        if self._running:
            return
        self._running = True

        self._tasks.append(asyncio.create_task(self._monitor_stuck_tasks()))
        self._tasks.append(asyncio.create_task(self._monitor_disk_memory()))
        self._tasks.append(asyncio.create_task(self._monitor_key_expiry()))
        self._tasks.append(asyncio.create_task(self._monitor_stale_prs()))
        self._tasks.append(asyncio.create_task(self._monitor_dep_audit()))
        self._tasks.append(asyncio.create_task(self._monitor_ci_status()))
        self._tasks.append(asyncio.create_task(self._cron_janitor()))

        logger.info("Smart monitors started (7 monitors)")

    def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    # ── Stuck Tasks ──────────────────────────────────────────────────

    async def _monitor_stuck_tasks(self):
        """Detect agent tasks stuck in 'executing' for >1 hour."""
        while self._running:
            try:
                await asyncio.sleep(900)  # check every 15 min
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT id, user_id, workspace_id, goal
                        FROM agent_tasks
                        WHERE status = 'executing'
                          AND updated_at < NOW() - INTERVAL '1 hour'
                        """
                    )
                    for row in rows:
                        await self._router.send(
                            user_id=row['user_id'],
                            workspace_id=row['workspace_id'],
                            type='warning',
                            title='Task stuck or forgotten',
                            body=f"Your task '{row['goal'][:50]}…' has been running for over an hour.",
                            source='smart_monitors',
                        )
                        logger.warning(f"Stuck task detected: {row['id']}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stuck task monitor error: {e}")

    # ── Disk & Memory (real implementation) ──────────────────────────

    async def _monitor_disk_memory(self):
        """Check disk and memory usage using psutil. Alerts above thresholds."""
        DISK_THRESHOLD = int(os.getenv("MONITOR_DISK_THRESHOLD_PCT", "85"))
        MEM_THRESHOLD = int(os.getenv("MONITOR_MEM_THRESHOLD_PCT", "90"))

        while self._running:
            try:
                await asyncio.sleep(900)  # every 15 min
                try:
                    import psutil
                except ImportError:
                    logger.debug("psutil not installed — disk/memory monitor disabled")
                    return

                disk = psutil.disk_usage("/")
                mem = psutil.virtual_memory()

                if disk.percent > DISK_THRESHOLD:
                    logger.warning(f"Disk usage critical: {disk.percent}%")
                    try:
                        await self._router.send(
                            user_id="system", workspace_id="system",
                            type="warning",
                            title="Disk Space Low",
                            body=f"Disk usage at {disk.percent}% ({disk.free // (1024**3)} GB free)",
                            source="smart_monitors",
                        )
                    except Exception:
                        pass

                if mem.percent > MEM_THRESHOLD:
                    logger.warning(f"Memory usage critical: {mem.percent}%")
                    try:
                        await self._router.send(
                            user_id="system", workspace_id="system",
                            type="warning",
                            title="Memory Usage High",
                            body=f"Memory usage at {mem.percent}% ({mem.available // (1024**2)} MB available)",
                            source="smart_monitors",
                        )
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Disk/Memory monitor error: {e}")

    # ── API Key Expiry ───────────────────────────────────────────────

    async def _monitor_key_expiry(self):
        """Alert on API keys expiring within 7 days."""
        while self._running:
            try:
                await asyncio.sleep(43200)  # every 12 hours
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT k.id, k.name, k.expires_at, k.workspace_id, wm.user_id
                        FROM api_keys k
                        JOIN workspace_members wm
                          ON wm.workspace_id = k.workspace_id AND wm.role = 'owner'
                        WHERE k.expires_at IS NOT NULL
                          AND k.expires_at < NOW() + INTERVAL '7 days'
                          AND k.expires_at > NOW()
                        """
                    )
                for row in rows:
                    days_left = (row['expires_at'] - datetime.now(timezone.utc)).days
                    await self._router.send(
                        user_id=row['user_id'],
                        workspace_id=str(row['workspace_id']),
                        type='warning',
                        title=f"API Key '{row['name']}' expiring soon",
                        body=f"Expires in {days_left} day(s). Rotate it to avoid service disruption.",
                        source='smart_monitors',
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Key expiry monitor error: {e}")

    # ── Cron Janitor (NEW — cleans stale retry cron jobs) ────────────

    async def _cron_janitor(self):
        """
        Clean up stale/orphaned cron jobs from the database.
        Removes:
          - Retry cron jobs older than 24 hours
          - One-shot jobs that have already run
        """
        while self._running:
            try:
                await asyncio.sleep(3600)  # every hour
                async with self._pool.acquire() as conn:
                    result = await conn.execute(
                        """
                        DELETE FROM automation_cron_jobs
                        WHERE (
                            name LIKE '%_retry_%'
                            AND created_at < NOW() - INTERVAL '24 hours'
                        ) OR (
                            name LIKE 'oneshot_%'
                            AND last_run IS NOT NULL
                        )
                        """
                    )
                    count = int(result.split(" ")[-1]) if result else 0
                    if count > 0:
                        logger.info(f"Cron janitor: cleaned {count} stale cron job(s)")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cron janitor error: {e}")

    # ── Stale PRs (placeholder until GitHub MCP is wired) ────────────

    async def _monitor_stale_prs(self):
        """Placeholder for stale PR detection via GitHub API."""
        while self._running:
            try:
                await asyncio.sleep(3600)
                # TODO: Wire to GitHub MCP server when available
                # await self._check_github_prs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stale PR monitor error: {e}")

    # ── Dependency Audit (placeholder) ───────────────────────────────

    async def _monitor_dep_audit(self):
        """Placeholder for dependency vulnerability scanning."""
        while self._running:
            try:
                await asyncio.sleep(21600)  # every 6 hours
                # TODO: Run pip-audit / npm audit and parse results
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dep audit monitor error: {e}")

    # ── CI Status (placeholder) ──────────────────────────────────────

    async def _monitor_ci_status(self):
        """Placeholder for CI/CD pipeline monitoring."""
        while self._running:
            try:
                await asyncio.sleep(300)  # every 5 minutes
                # TODO: Query GitHub Actions API for recent workflow runs
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"CI status monitor error: {e}")
