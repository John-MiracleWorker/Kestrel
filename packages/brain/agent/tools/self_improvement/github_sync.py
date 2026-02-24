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
from .utils import *
from .ast_analyzer import _deep_scan

_SYNCED_ISSUES_FILE = "/tmp/kestrel_synced_issues.json"

def _load_synced_hashes() -> set:
    """Load set of issue hashes already synced to GitHub."""
    try:
        with open(_SYNCED_ISSUES_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def _save_synced_hashes(hashes: set) -> None:
    """Save synced issue hashes."""
    try:
        with open(_SYNCED_ISSUES_FILE, "w") as f:
            json.dump(list(hashes), f)
    except IOError as e:
        logger.error(f"Failed to save synced hashes: {e}")

def _issue_hash(issue: dict) -> str:
    """Generate a deterministic hash for an issue to deduplicate."""
    key = f"{issue.get('type')}:{issue.get('file')}:{issue.get('line')}:{issue.get('description', '')[:80]}"
    return hashlib.md5(key.encode()).hexdigest()[:12]

_SEVERITY_LABELS = {
    "critical": "bug",
    "high": "bug",
    "medium": "improvement",
    "low": "enhancement",
    "info": "enhancement",
}

async def _github_sync(package: str = "all") -> dict:
    """
    Scan codebase, filter high-severity issues, and create GitHub Issues.
    Requires GITHUB_PERSONAL_ACCESS_TOKEN and GITHUB_REPO env vars.
    """
    token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN") or os.getenv("GITHUB_PAT", "")
    repo = os.getenv("GITHUB_REPO", "John-MiracleWorker/LibreBird")

    if not token:
        return {"error": "GITHUB_PERSONAL_ACCESS_TOKEN (or GITHUB_PAT) not set. Set it in your .env file."}

    # Run scan (offloaded to thread — _deep_scan does blocking I/O + CPU work)
    results = await asyncio.to_thread(_deep_scan, package)
    all_issues = results.get("issues", [])

    # Filter to high-severity only (critical, high, security)
    high_issues = [
        i for i in all_issues
        if i.get("severity") in ("critical", "high")
        or i.get("type") in ("security", "syntax_error")
    ]

    if not high_issues:
        return {
            "message": "No high-severity issues found.",
            "total_scanned": results.get("total_issues", 0),
            "created": 0,
        }

    # Deduplicate
    synced = _load_synced_hashes()
    new_issues = [i for i in high_issues if _issue_hash(i) not in synced]

    if not new_issues:
        return {
            "message": f"All {len(high_issues)} high-severity issues already synced to GitHub.",
            "total_scanned": results.get("total_issues", 0),
            "created": 0,
        }

    # Create GitHub Issues
    created = []
    for issue in new_issues[:10]:  # Cap at 10 per sync to avoid flooding
        severity = issue.get("severity", "medium")
        issue_type = issue.get("type", "unknown")
        labels = ["kestrel-bot", _SEVERITY_LABELS.get(severity, "enhancement")]
        if issue_type == "security":
            labels.append("security")

        title = f"[{severity.upper()}] {issue.get('description', 'Unknown issue')[:80]}"
        body = (
            f"## Auto-detected by Kestrel Self-Improvement Engine\n\n"
            f"**Severity**: {severity}\n"
            f"**Type**: {issue_type}\n"
            f"**Package**: {issue.get('package', 'unknown')}\n"
            f"**File**: `{issue.get('file', 'unknown')}`\n"
            f"**Line**: {issue.get('line', 'N/A')}\n\n"
            f"### Description\n{issue.get('description', 'No description')}\n\n"
            f"### Suggested Fix\n{issue.get('suggestion', 'No suggestion available')}\n\n"
            f"---\n_Created by Kestrel's automated code analysis_"
        )

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.post(
                    f"https://api.github.com/repos/{repo}/issues",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={"title": title, "body": body, "labels": labels},
                )
                if resp.status_code in (201, 200):
                    resp_data = resp.json()
                    created.append({
                        "number": resp_data.get("number"),
                        "title": title,
                        "url": resp_data.get("html_url"),
                    })
                    synced.add(_issue_hash(issue))
                    logger.info(f"Created GitHub issue #{resp_data.get('number')}: {title}")
                else:
                    logger.warning(f"GitHub issue creation failed ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            logger.error(f"GitHub API error: {e}")

    _save_synced_hashes(synced)

    # Send Telegram notification about new issues
    if created:
        summary_lines = [f"🔔 **{len(created)} new issues filed to GitHub**\n"]
        for c in created:
            summary_lines.append(f"• #{c['number']}: {c['title'][:60]}")
        _send_summary_to_telegram("\n".join(summary_lines))

    return {
        "message": f"Created {len(created)} GitHub issues from {len(new_issues)} new findings.",
        "total_scanned": results.get("total_issues", 0),
        "high_severity": len(high_issues),
        "created": len(created),
        "issues": created,
    }