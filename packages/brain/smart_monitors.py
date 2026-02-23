import asyncio
import logging
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
        self._tasks = []

    def start(self):
        if self._running:
            return
        self._running = True
        
        # Start individual monitor loops
        self._tasks.append(asyncio.create_task(self._monitor_stale_prs()))
        self._tasks.append(asyncio.create_task(self._monitor_key_expiry()))
        self._tasks.append(asyncio.create_task(self._monitor_stuck_tasks()))
        self._tasks.append(asyncio.create_task(self._monitor_dep_audit()))
        self._tasks.append(asyncio.create_task(self._monitor_ci_status()))
        self._tasks.append(asyncio.create_task(self._monitor_disk_memory()))
        
        logger.info("Smart monitors started")

    def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def _monitor_stale_prs(self):
        """Simulate checking for stale PRs every hour."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # 1 hour
                # In a real app, hit GitHub API here
                # Example:
                # await self._router.send(
                #     user_id="default_user", workspace_id="default_workspace",
                #     type="info", title="Stale PR Detected",
                #     body="PR #42 in John-MiracleWorker/LibreBird has been open for 3 days without review."
                # )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stale PR monitor error: {e}")

    async def _monitor_key_expiry(self):
        """Monitor API key expiry every 12 hours."""
        while self._running:
            try:
                await asyncio.sleep(43200)  # 12 hours
                # In a real app, query `api_keys` for expires_at < NOW() + 7 days
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Key expiry monitor error: {e}")

    async def _monitor_stuck_tasks(self):
        """Monitor for agent tasks stuck in 'executing' state."""
        while self._running:
            try:
                await asyncio.sleep(900)  # 15 minutes
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
                            body=f"Your task '{row['goal'][:50]}...' has been running for over an hour.",
                            source='smart_monitors'
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stuck task monitor error: {e}")

    async def _monitor_dep_audit(self):
        """Monitor for outdated/vulnerable dependencies every 6 hours."""
        while self._running:
            try:
                await asyncio.sleep(21600)  # 6 hours
                # In a real app, parse package.json or requirements.txt against CVE DB
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dep audit monitor error: {e}")

    async def _monitor_ci_status(self):
        """Monitor recent CI/CD pipeline runs every 5 minutes."""
        while self._running:
            try:
                await asyncio.sleep(300)  # 5 minutes
                # In a real app, query GitHub Actions API
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"CI status monitor error: {e}")

    async def _monitor_disk_memory(self):
        """Monitor system disk and memory every 15 minutes."""
        while self._running:
            try:
                await asyncio.sleep(900)  # 15 minutes
                # In a real app, use psutil to check resource usage
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Disk/Memory monitor error: {e}")
