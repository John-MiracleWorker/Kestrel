import asyncio
import ast
import json
import hashlib
import logging
import os
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError
from agent.types import RiskLevel, ToolDefinition

from .self_improvement.utils import *
from .self_improvement.ast_analyzer import _deep_scan
from .self_improvement.github_sync import _github_sync
from .self_improvement.proposals import _run_tests, _propose_improvements, _telegram_digest, _handle_approval
from .self_improvement.patcher import apply_proposal, rollback, get_history

def register_self_improve_tools(registry) -> None:
    """Register the self-improvement engine tool."""

    registry.register(
        definition=ToolDefinition(
            name="self_improve",
            description=(
                "Kestrel's self-improvement engine. Deeply analyzes the codebase "
                "for issues and sends improvement proposals to Telegram for user "
                "approval. Actions: scan, test, report, propose, approve, deny, "
                "github_sync (file issues to GitHub), telegram_digest (send health report)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["scan", "test", "report", "propose", "approve", "deny",
                                 "apply", "rollback", "history",
                                 "list_pending", "github_sync", "telegram_digest"],
                        "description": (
                            "scan = deep codebase analysis, "
                            "test = run test suite, "
                            "report = last scan summary, "
                            "propose = send proposals to Telegram, "
                            "approve/deny = act on a proposal, "
                            "apply = generate patch + test + hot-reload for a proposal, "
                            "rollback = revert last applied change for a file, "
                            "history = show all applied self-improvements, "
                            "list_pending = show pending proposals, "
                            "github_sync = file high-severity issues to GitHub, "
                            "telegram_digest = send code health summary to Telegram"
                        ),
                    },
                    "package": {
                        "type": "string",
                        "description": "Package to analyze: brain, gateway, web, hands, or 'all'",
                    },
                    "scan_mode": {
                        "type": "string",
                        "enum": ["quick", "standard", "deep"],
                        "description": "Scan breadth: quick (diff/mtime incremental), standard (default), deep (full exhaustive)",
                        "default": "standard",
                    },
                    "proposal_id": {
                        "type": "string",
                        "description": "Proposal ID for approve/deny/apply actions",
                    },
                    "file": {
                        "type": "string",
                        "description": "File path for rollback action (relative to project root)",
                    },
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=120,
            category="development",
        ),
        handler=self_improve_action,
    )

    # Start the background scheduler for periodic health checks
    start_scheduler()
async def self_improve_action(
    action: str,
    package: str = "all",
    proposal_id: str = "",
    scan_mode: str = "standard",
    file: str = "",
) -> dict:
    """Route to the appropriate self-improvement action."""
    global _last_scan_results

    if action == "scan":
        results = await asyncio.to_thread(_deep_scan, package, scan_mode)
        _last_scan_results = results
        return results
    elif action == "test":
        return _run_tests(package)
    elif action == "report":
        return _last_scan_results or {"message": "No scan results. Run 'scan' first."}
    elif action == "propose":
        return await _propose_improvements()
    elif action == "approve":
        return await _handle_approval(proposal_id, approved=True)
    elif action == "deny":
        return await _handle_approval(proposal_id, approved=False)
    elif action == "apply":
        # Apply a specific proposal by ID (generates patch, tests, hot-reloads)
        if not proposal_id:
            return {"error": "proposal_id is required for apply action"}
        pending = _load_proposals()
        matching = None
        for pid, proposal in pending.items():
            if pid == proposal_id or pid.startswith(proposal_id):
                matching = proposal
                break
        if not matching:
            return {"error": f"Proposal not found: {proposal_id}", "pending": list(pending.keys())}
        return await apply_proposal(matching)
    elif action == "rollback":
        if not file:
            return {"error": "'file' parameter required for rollback (relative path, e.g. 'agent/tools/moltbook.py')"}
        return rollback(file)
    elif action == "history":
        history = get_history()
        return {"improvements": history, "count": len(history)}
    elif action == "list_pending":
        pending = _load_proposals()
        return {"pending": list(pending.values()), "count": len(pending)}
    elif action == "github_sync":
        return await _github_sync(package)
    elif action == "telegram_digest":
        return await _telegram_digest(package)
    else:
        return {"error": f"Unknown action: {action}"}
_SCHEDULER_INTERVAL_HOURS = 6
_scheduler_started = False
async def _scheduled_health_check():
    """Background loop: scan → GitHub sync → Telegram digest every N hours."""
    interval = _SCHEDULER_INTERVAL_HOURS * 3600
    # Wait 5 minutes before first run to let the server fully start
    await asyncio.sleep(300)

    while True:
        try:
            logger.info("🔄 Starting scheduled health check...")

            # Only sync to GitHub if token is configured
            if os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN") or os.getenv("GITHUB_PAT"):
                await _github_sync()
            else:
                logger.info("No GITHUB_PERSONAL_ACCESS_TOKEN / GITHUB_PAT — skipping GitHub sync")

            # Always send Telegram digest if bot token is set
            if os.getenv("TELEGRAM_BOT_TOKEN"):
                await _telegram_digest()
            else:
                logger.info("No TELEGRAM_BOT_TOKEN — skipping Telegram digest")

            logger.info(f"✅ Health check complete. Next run in {_SCHEDULER_INTERVAL_HOURS}h.")
        except Exception as e:
            logger.error(f"Scheduled health check failed: {e}")

        await asyncio.sleep(interval)
def start_scheduler():
    """Start the background health check scheduler (called once from server startup)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_scheduled_health_check())
        logger.info(f"📅 Self-improvement scheduler started (every {_SCHEDULER_INTERVAL_HOURS}h)")
    except RuntimeError:
        logger.warning("No running event loop — scheduler will start on first request")
