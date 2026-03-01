import asyncio
from typing import Any
from .config import logger
from . import runtime
from providers_registry import get_provider, resolve_provider
from provider_config import ProviderConfig
from agent.loop import AgentLoop
from agent.tools import build_tool_registry
from agent.types import AgentTask, GuardrailConfig as GCfg
from agent.guardrails import Guardrails

async def launch_task_from_automation(workspace_id: str, user_id: str, goal: str, source: str = "automation"):
    """Task launcher callback for cron/webhook automation."""
    task = AgentTask(
        user_id=user_id,
        workspace_id=workspace_id,
        goal=goal,
        config=GCfg(),
    )
    if runtime.agent_persistence:
        await runtime.agent_persistence.save_task(task)
    logger.info(f"Automation task started: {task.id} — {goal} (source: {source})")
    # Run in background
    asyncio.create_task(_run_automation_task(task))

async def _run_automation_task(task: AgentTask):
    """Run an automation-triggered task in the background."""
    from db import get_pool # Late import to avoid cycles
    try:
        pool = await get_pool()
        ws_config = await ProviderConfig(pool).get_config(task.workspace_id)
        provider_name = ws_config.get("provider", "local")
        task_provider = get_provider(provider_name)
        
        task_loop = AgentLoop(
            provider=task_provider,
            tool_registry=build_tool_registry(
                hands_client=runtime.hands_client,
                vector_store=runtime.vector_store,
                pool=pool,
            ),
            guardrails=Guardrails(),
            persistence=runtime.agent_persistence,
            memory_graph=runtime.memory_graph,
            provider_resolver=resolve_provider,
        )
        
        # Set context for tools used in automated tasks
        import agent.tools.moltbook as _moltbook_mod
        _moltbook_mod._current_workspace_id = task.workspace_id
        _moltbook_mod._current_user_id = task.user_id
        
        import agent.tools.schedule as _schedule_mod
        _schedule_mod._cron_scheduler = runtime.cron_scheduler
        _schedule_mod._current_workspace_id = task.workspace_id
        _schedule_mod._current_user_id = task.user_id
        
        async for event in task_loop.run(task):
            logger.debug(f"Automation task {task.id}: {event.type}")
    except Exception as e:
        logger.error(f"Automation task {task.id} failed: {e}")

async def bootstrap_moltbook_cron(pool: Any) -> None:
    """
    Ensure every workspace has an autonomous Moltbook session cron job.
    Runs every 2 hours. Skips workspaces that already have the job.
    The job is a no-op if no Moltbook credentials are present.
    """
    _MOLTBOOK_JOB_NAME = "moltbook_autonomous_session"
    _MOLTBOOK_CRON = "0 */6 * * *"   # every 6 hours
    _MOLTBOOK_GOAL = (
        "Run your autonomous Moltbook session. "
        "First call moltbook_session to scan your subscribed submolts and get your "
        "engagement plan. Then use the moltbook tool to engage: upvote quality posts, "
        "leave on-topic comments that add genuine value, and optionally create one "
        "original post if you have something worth sharing. "
        "Stay in character as Kestrel throughout."
    )
    try:
        if not runtime.cron_scheduler:
            return
            
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT wm.workspace_id, wm.user_id
                FROM workspace_members wm
                WHERE wm.role = 'owner'
                ORDER BY wm.workspace_id
                """
            )

        # Check the DATABASE for existing jobs (not in-memory cache which may be stale)
        async with pool.acquire() as conn:
            existing = await conn.fetch(
                "SELECT workspace_id, name FROM automation_cron_jobs WHERE name = $1",
                _MOLTBOOK_JOB_NAME,
            )
        existing_names = {(str(r["workspace_id"]), r["name"]) for r in existing}

        created = 0
        for row in rows:
            ws_id = str(row["workspace_id"])
            u_id = str(row["user_id"])
            if (ws_id, _MOLTBOOK_JOB_NAME) in existing_names:
                continue
            await runtime.cron_scheduler.create_job(
                workspace_id=ws_id,
                user_id=u_id,
                name=_MOLTBOOK_JOB_NAME,
                description="Autonomous Moltbook participation — browse, engage, post",
                cron_expression=_MOLTBOOK_CRON,
                goal=_MOLTBOOK_GOAL,
            )
            created += 1

        if created:
            logger.info(f"Bootstrapped Moltbook autonomous cron job for {created} workspace(s)")
    except Exception as e:
        logger.warning(f"Moltbook cron bootstrap failed (non-fatal): {e}")

