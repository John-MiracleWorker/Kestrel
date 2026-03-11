import asyncio
from typing import Any
from .config import logger
from .feature_mode import enabled_bundles_for_mode, parse_feature_mode
from . import runtime
from providers_registry import get_provider, resolve_provider
from provider_config import ProviderConfig
from agent.loop import AgentLoop
from agent.task_profiles import TaskProfile, filter_registry_for_profile
from agent.tools import build_tool_registry
from agent.types import AgentTask, GuardrailConfig as GCfg
from agent.guardrails import Guardrails

async def launch_task_from_automation(workspace_id: str, user_id: str, goal: str, source: str = "automation", model_override: str = None):
    """Task launcher callback for cron/webhook automation."""
    task = AgentTask(
        user_id=user_id,
        workspace_id=workspace_id,
        goal=goal,
        config=GCfg(),
    )
    task.task_profile = TaskProfile.OPS.value
    if runtime.agent_persistence:
        await runtime.agent_persistence.save_task(task)
    logger.info(f"Automation task started: {task.id} — {goal} (source: {source})")
    # Run in background
    asyncio.create_task(_run_automation_task(task, source, model_override))

async def _run_automation_task(task: AgentTask, source: str = "automation", model_override: str = None):
    """Run an automation-triggered task in the background."""
    from db import get_pool # Late import to avoid cycles
    try:
        pool = await get_pool()
        ws_config = await ProviderConfig(pool).get_config(task.workspace_id)
        provider_name = ws_config.get("provider", "local")
        task_provider = get_provider(provider_name)
        feature_mode = parse_feature_mode(getattr(runtime, "feature_mode", "core"))
        task_tool_registry = build_tool_registry(
            hands_client=runtime.hands_client,
            vector_store=runtime.vector_store,
            pool=pool,
            runtime_policy=runtime.execution_runtime,
            enabled_bundles=tuple(getattr(runtime, "enabled_tool_bundles", []) or enabled_bundles_for_mode(feature_mode)),
            feature_mode=feature_mode.value,
        )
        task_tool_registry = filter_registry_for_profile(task_tool_registry, TaskProfile.OPS, feature_mode)
        
        task_loop = AgentLoop(
            provider=task_provider,
            model=model_override or ws_config.get("model", ""),
            tool_registry=task_tool_registry,
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

        import agent.tools.build_automation as _ba_mod
        _ba_mod._current_workspace_id = task.workspace_id
        _ba_mod._current_user_id = task.user_id

        import agent.tools.daemon_control as _dc_mod
        _dc_mod._current_workspace_id = task.workspace_id
        _dc_mod._current_user_id = task.user_id

        # Set context for model swap tool (if used in automation)
        import agent.tools.model_swap as _ms_mod
        _ms_mod._current_workspace_id = task.workspace_id
        _ms_mod._current_user_id = task.user_id

        async for event in task_loop.run(task):
            logger.debug(f"Automation task {task.id}: {event.type}")
    except Exception as e:
        logger.error(f"Automation task {task.id} failed: {e}")

async def _bootstrap_cron_job(
    pool: Any,
    job_name: str,
    cron_expression: str,
    description: str,
    goal: str,
) -> int:
    """Generic helper to bootstrap a cron job for every workspace owner.

    Returns the number of newly created jobs.
    """
    if not runtime.cron_scheduler:
        return 0

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT wm.workspace_id, wm.user_id
            FROM workspace_members wm
            WHERE wm.role = 'owner'
            ORDER BY wm.workspace_id
            """
        )

    async with pool.acquire() as conn:
        existing = await conn.fetch(
            "SELECT workspace_id, name FROM automation_cron_jobs WHERE name = $1",
            job_name,
        )
    existing_names = {(str(r["workspace_id"]), r["name"]) for r in existing}

    created = 0
    for row in rows:
        ws_id = str(row["workspace_id"])
        u_id = str(row["user_id"])
        if (ws_id, job_name) in existing_names:
            continue
        await runtime.cron_scheduler.create_job(
            workspace_id=ws_id,
            user_id=u_id,
            name=job_name,
            description=description,
            cron_expression=cron_expression,
            goal=goal,
        )
        created += 1
    return created


async def bootstrap_gmail_cron(pool: Any) -> None:
    """
    Ensure every workspace has a Gmail monitoring cron job.
    Runs every 2 hours. Checks unread emails and sends a summary to Telegram.
    """
    _GMAIL_JOB_NAME = "gmail_summary"
    _GMAIL_CRON = "0 */2 * * *"  # every 2 hours
    _GMAIL_GOAL = (
        "Check my Gmail inbox for unread and recent emails from the last 2 hours. "
        "Use the Gmail MCP tools (gmail_list_messages, gmail_get_message) to retrieve them. "
        "Summarize the important emails — include sender, subject, and a brief summary of the content. "
        "Group them by priority: urgent/action-needed first, then informational. "
        "Skip obvious spam and marketing emails. "
        "Send the summary to Telegram using the Telegram channel. "
        "If there are no important new emails, send a short 'Inbox clear' message instead."
    )
    try:
        created = await _bootstrap_cron_job(
            pool=pool,
            job_name=_GMAIL_JOB_NAME,
            cron_expression=_GMAIL_CRON,
            description="Gmail inbox monitoring — summarize unread emails to Telegram every 2 hours",
            goal=_GMAIL_GOAL,
        )
        if created:
            logger.info(f"Bootstrapped Gmail summary cron job for {created} workspace(s)")
    except Exception as e:
        logger.warning(f"Gmail cron bootstrap failed (non-fatal): {e}")


async def bootstrap_ai_news_cron(pool: Any) -> None:
    """
    Ensure every workspace has AI news briefing cron jobs.
    Morning briefing at 8am UTC, afternoon briefing at 1pm UTC.
    """
    _NEWS_JOBS = [
        {
            "name": "ai_news_morning",
            "cron": "0 8 * * *",  # daily at 8am UTC
            "description": "Morning AI news briefing — top stories and developments",
            "goal": (
                "Compile a morning AI news briefing. "
                "Use your web search and digest tools to find the latest AI news, research papers, "
                "and industry developments from the last 24 hours. "
                "Focus on: major model releases, breakthrough research, industry moves, "
                "open-source updates, and regulation news. "
                "Format it as a clean briefing with headlines and 1-2 sentence summaries. "
                "Send the briefing to Telegram. "
                "Keep it concise — aim for 5-8 top stories maximum."
            ),
        },
        {
            "name": "ai_news_afternoon",
            "cron": "0 13 * * *",  # daily at 1pm UTC
            "description": "Afternoon AI news briefing — updates and developments",
            "goal": (
                "Compile an afternoon AI news briefing. "
                "Use your web search and digest tools to find AI news and developments "
                "that broke since this morning. "
                "Focus on: new announcements, trending discussions, notable tweets or blog posts, "
                "and any breaking developments in AI/ML. "
                "Format it as a clean briefing with headlines and 1-2 sentence summaries. "
                "Send the briefing to Telegram. "
                "Keep it concise — aim for 3-5 stories. If nothing notable happened, "
                "send a short 'No major updates this afternoon' message."
            ),
        },
    ]
    try:
        total_created = 0
        for job_config in _NEWS_JOBS:
            created = await _bootstrap_cron_job(
                pool=pool,
                job_name=job_config["name"],
                cron_expression=job_config["cron"],
                description=job_config["description"],
                goal=job_config["goal"],
            )
            total_created += created
        if total_created:
            logger.info(f"Bootstrapped AI news cron jobs: {total_created} new job(s)")
    except Exception as e:
        logger.warning(f"AI news cron bootstrap failed (non-fatal): {e}")


async def bootstrap_moltbook_cron(pool: Any) -> None:
    """
    Ensure every workspace has an autonomous Moltbook session cron job.
    Runs every 6 hours. Skips workspaces that already have the job.
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
        created = await _bootstrap_cron_job(
            pool=pool,
            job_name=_MOLTBOOK_JOB_NAME,
            cron_expression=_MOLTBOOK_CRON,
            description="Autonomous Moltbook participation — browse, engage, post",
            goal=_MOLTBOOK_GOAL,
        )
        if created:
            logger.info(f"Bootstrapped Moltbook autonomous cron job for {created} workspace(s)")
    except Exception as e:
        logger.warning(f"Moltbook cron bootstrap failed (non-fatal): {e}")
