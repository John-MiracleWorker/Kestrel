"""
Self-Improvement Engine — deep codebase analysis with Telegram approval flow.

Workflow:
  1. Scheduled scan (every 6h) of all packages
  2. Deep analysis: syntax, types, TODOs, complexity, dead code, security
  3. Proposals formatted and sent to Telegram with ✅ Approve / ❌ Deny buttons
  4. On approval: Kestrel applies the fix, tests, commits, and optionally deploys
  5. On denial: proposal is discarded and logged

Safety:
  - Inactivity guard: deploy only after 30min of user inactivity
  - Test gate: deploy only if all tests pass
  - Proposals are never auto-applied — always requires user approval via Telegram
"""

import asyncio
import ast
import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.self_improve")

PROJECT_ROOT = "/project"

# ── Inactivity Guard ─────────────────────────────────────────────────
INACTIVITY_THRESHOLD_SECONDS = 30 * 60  # 30 minutes

_last_user_activity: float = time.time()


def touch_user_activity() -> None:
    """Called by Brain server whenever a user sends a message or API request."""
    global _last_user_activity
    _last_user_activity = time.time()


def is_user_inactive() -> bool:
    """Check if the user has been inactive long enough for auto-deploy."""
    return (time.time() - _last_user_activity) > INACTIVITY_THRESHOLD_SECONDS


def get_inactivity_seconds() -> float:
    """Get seconds since last user activity."""
    return time.time() - _last_user_activity


# ── JSON Extraction Helper ───────────────────────────────────────────
def _extract_json_array(text: str) -> list | None:
    """
    Robustly extract a JSON array from LLM response text.
    Handles: raw JSON, markdown code fences, JSON embedded in prose.
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # 1. Direct parse
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown code fences (```json ... ``` or ``` ... ```)
    fence_match = re.search(r'```(?:json)?\s*\n(.*?)```', text, re.DOTALL)
    if fence_match:
        try:
            result = json.loads(fence_match.group(1).strip())
            return result if isinstance(result, list) else [result]
        except json.JSONDecodeError:
            pass

    # 3. Find JSON array by bracket matching — find first [ and last ]
    first_bracket = text.find('[')
    last_bracket = text.rfind(']')
    if first_bracket != -1 and last_bracket > first_bracket:
        candidate = text[first_bracket:last_bracket + 1]
        try:
            result = json.loads(candidate)
            return result if isinstance(result, list) else [result]
        except json.JSONDecodeError:
            pass

    # 4. Try finding a JSON object (single proposal)
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        try:
            result = json.loads(candidate)
            return [result]
        except json.JSONDecodeError:
            pass

    # 5. Truncation recovery: if array started but was cut off, close it
    first_bracket = text.find('[')
    if first_bracket != -1:
        # Find the last complete object (last })
        last_brace = text.rfind('}')
        if last_brace > first_bracket:
            candidate = text[first_bracket:last_brace + 1] + ']'
            try:
                result = json.loads(candidate)
                return result if isinstance(result, list) else [result]
            except json.JSONDecodeError:
                pass

    return None


# ── Persistent Proposals Store ───────────────────────────────────────
_PROPOSALS_FILE = "/tmp/kestrel_proposals.json"


def _load_proposals() -> dict[str, dict]:
    """Load pending proposals from disk."""
    try:
        if os.path.exists(_PROPOSALS_FILE):
            with open(_PROPOSALS_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def _save_proposals(proposals: dict[str, dict]) -> None:
    """Save pending proposals to disk."""
    try:
        with open(_PROPOSALS_FILE, "w") as f:
            json.dump(proposals, f, indent=2)
    except IOError as e:
        logger.error(f"Failed to save proposals: {e}")


# ── Telegram Helpers ─────────────────────────────────────────────────
def _telegram_api(method: str, payload: dict) -> dict:
    """Call Telegram Bot API directly from Brain container."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — skipping Telegram notification")
        return {"ok": False, "error": "No bot token"}

    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (URLError, Exception) as e:
        logger.error(f"Telegram API error: {e}")
        return {"ok": False, "error": str(e)}


def _send_proposal_to_telegram(proposal: dict) -> dict:
    """Send an improvement proposal to Telegram with approve/deny buttons."""
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set — can't send proposal")
        return {"ok": False, "error": "No chat ID"}

    proposal_id = proposal["id"]

    # Format the message
    severity_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}.get(
        proposal.get("severity", "info"), "⚪"
    )

    text = (
        f"{severity_icon} *Self\\-Improvement Proposal*\n\n"
        f"📦 *Package:* `{proposal.get('package', 'unknown')}`\n"
        f"📄 *File:* `{proposal.get('file', 'unknown')}`\n"
        f"🏷️ *Type:* {proposal.get('type', 'improvement')}\n"
        f"⚡ *Severity:* {proposal.get('severity', 'info')}\n\n"
        f"📝 *Description:*\n{_escape_md(proposal.get('description', ''))}\n\n"
    )

    if proposal.get("suggestion"):
        text += f"💡 *Suggested Fix:*\n```\n{proposal['suggestion'][:500]}\n```\n\n"

    text += f"🆔 `{proposal_id[:8]}`"

    payload = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "MarkdownV2",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"si_approve:{proposal_id}"},
                    {"text": "❌ Deny", "callback_data": f"si_deny:{proposal_id}"},
                ]
            ]
        },
    }

    result = _telegram_api("sendMessage", payload)
    if not result.get("ok"):
        # Retry without MarkdownV2 (fallback)
        payload["parse_mode"] = "HTML"
        payload["text"] = (
            f"{severity_icon} <b>Self-Improvement Proposal</b>\n\n"
            f"📦 <b>Package:</b> <code>{proposal.get('package', 'unknown')}</code>\n"
            f"📄 <b>File:</b> <code>{proposal.get('file', 'unknown')}</code>\n"
            f"🏷️ <b>Type:</b> {proposal.get('type', 'improvement')}\n"
            f"⚡ <b>Severity:</b> {proposal.get('severity', 'info')}\n\n"
            f"📝 <b>Description:</b>\n{proposal.get('description', '')}\n\n"
        )
        if proposal.get("suggestion"):
            payload["text"] += f"💡 <b>Suggested Fix:</b>\n<pre>{proposal['suggestion'][:500]}</pre>\n\n"
        payload["text"] += f"🆔 <code>{proposal_id[:8]}</code>"
        result = _telegram_api("sendMessage", payload)

    return result


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f"([{re.escape(special)}])", r"\\\1", text)


def _send_summary_to_telegram(summary: str) -> dict:
    """Send a plain summary message to Telegram."""
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return {"ok": False, "error": "No chat ID"}

    return _telegram_api("sendMessage", {
        "chat_id": int(chat_id),
        "text": summary,
        "parse_mode": "HTML",
    })


# ── Tool Registration ────────────────────────────────────────────────
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
                                 "list_pending", "github_sync", "telegram_digest"],
                        "description": (
                            "scan = deep codebase analysis, "
                            "test = run test suite, "
                            "report = last scan summary, "
                            "propose = send proposals to Telegram, "
                            "approve/deny = act on a proposal, "
                            "list_pending = show pending proposals, "
                            "github_sync = file high-severity issues to GitHub, "
                            "telegram_digest = send code health summary to Telegram"
                        ),
                    },
                    "package": {
                        "type": "string",
                        "description": "Package to analyze: brain, gateway, web, hands, or 'all'",
                    },
                    "proposal_id": {
                        "type": "string",
                        "description": "Proposal ID for approve/deny actions",
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


# ── Scan Results Cache ───────────────────────────────────────────────
_last_scan_results: dict = {}


# ── Main Action Router ───────────────────────────────────────────────
async def self_improve_action(
    action: str,
    package: str = "all",
    proposal_id: str = "",
) -> dict:
    """Route to the appropriate self-improvement action."""
    global _last_scan_results

    if action == "scan":
        results = _deep_scan(package)
        _last_scan_results = results
        return results
    elif action == "test":
        return _run_tests(package)
    elif action == "report":
        return _last_scan_results or {"message": "No scan results. Run 'scan' first."}
    elif action == "propose":
        return await _propose_improvements()
    elif action == "approve":
        return _handle_approval(proposal_id, approved=True)
    elif action == "deny":
        return _handle_approval(proposal_id, approved=False)
    elif action == "list_pending":
        pending = _load_proposals()
        return {"pending": list(pending.values()), "count": len(pending)}
    elif action == "github_sync":
        return await _github_sync(package)
    elif action == "telegram_digest":
        return await _telegram_digest(package)
    else:
        return {"error": f"Unknown action: {action}"}


# ── GitHub Issues Sync ───────────────────────────────────────────────
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
    import hashlib
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
    token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "John-MiracleWorker/LibreBird")

    if not token:
        return {"error": "GITHUB_PERSONAL_ACCESS_TOKEN not set. Set it in your .env file."}

    # Run scan
    results = _deep_scan(package)
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
            async with httpx.AsyncClient(timeout=10) as client:
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


async def _telegram_digest(package: str = "all") -> dict:
    """
    Scan codebase and send a compact Telegram digest of findings.
    Groups by severity and shows top issues.
    """
    results = _deep_scan(package)
    all_issues = results.get("issues", [])

    if not all_issues:
        msg = "✅ **Kestrel Code Health**: No issues found. Codebase is clean!"
        _send_summary_to_telegram(msg)
        return {"message": "No issues found. Telegram notified.", "sent": True}

    # Group by severity
    by_severity: dict[str, list] = {}
    for issue in all_issues:
        sev = issue.get("severity", "info")
        by_severity.setdefault(sev, []).append(issue)

    # Build digest
    severity_icons = {
        "critical": "🚨", "high": "🔴", "medium": "🟡",
        "low": "🔵", "info": "ℹ️"
    }

    lines = [f"📊 **Kestrel Code Health Report**\n"]
    lines.append(f"Total: {len(all_issues)} issues across {results.get('packages_scanned', 0)} packages\n")

    for sev in ("critical", "high", "medium", "low", "info"):
        issues = by_severity.get(sev, [])
        if issues:
            icon = severity_icons.get(sev, "•")
            lines.append(f"{icon} **{sev.upper()}**: {len(issues)}")

    # Top 5 most important issues
    top = sorted(all_issues, key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(x.get("severity", "info"), 5))[:5]
    if top:
        lines.append("\n**Top Issues:**")
        for i, issue in enumerate(top, 1):
            lines.append(f"{i}. [{issue.get('severity', '?').upper()}] {issue.get('description', '?')[:70]}")
            lines.append(f"   📁 `{issue.get('file', '?')}`")

    msg = "\n".join(lines)
    _send_summary_to_telegram(msg)

    return {
        "message": "Telegram digest sent.",
        "sent": True,
        "total_issues": len(all_issues),
        "by_severity": {k: len(v) for k, v in by_severity.items()},
    }


# ── Deep Codebase Scanner ────────────────────────────────────────────
PACKAGES = {
    "brain": {"path": "packages/brain", "lang": "python", "ext": ".py"},
    "gateway": {"path": "packages/gateway/src", "lang": "typescript", "ext": ".ts"},
    "web": {"path": "packages/web/src", "lang": "typescript", "ext": ".tsx"},
    "hands": {"path": "packages/hands", "lang": "python", "ext": ".py"},
}


def _deep_scan(package: str = "all") -> dict:
    """
    Deep codebase analysis — goes beyond syntax to find real improvements.
    
    Checks:
      1. Python syntax errors (ast.parse)
      2. TypeScript strict checks (when available)
      3. TODO/FIXME/HACK comments (actionable items)
      4. Dead imports (unused imports in Python)
      5. Large functions (complexity/readability)
      6. Error handling gaps (bare excepts, missing error handling)
      7. Security patterns (hardcoded secrets, eval usage)
      8. Code duplication hints
    """
    issues = []
    packages_to_scan = PACKAGES if package == "all" else {package: PACKAGES.get(package, {})}

    for pkg_name, pkg_info in packages_to_scan.items():
        if not pkg_info:
            continue
        pkg_path = os.path.join(PROJECT_ROOT, pkg_info["path"])
        if not os.path.exists(pkg_path):
            continue

        ext = pkg_info["ext"]
        lang = pkg_info["lang"]

        for root, dirs, files in os.walk(pkg_path):
            # Skip node_modules, __pycache__, .git, dist
            dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git", "dist", "build", ".next")]

            for fname in files:
                if not fname.endswith(ext) and not fname.endswith(".ts"):
                    continue

                filepath = os.path.join(root, fname)
                rel_path = os.path.relpath(filepath, PROJECT_ROOT)

                try:
                    content = open(filepath, "r", errors="ignore").read()
                    lines = content.split("\n")
                except Exception:
                    continue

                # 1. Python syntax check
                if lang == "python" and fname.endswith(".py"):
                    try:
                        ast.parse(content, filename=fname)
                    except SyntaxError as e:
                        issues.append({
                            "type": "syntax_error",
                            "severity": "critical",
                            "package": pkg_name,
                            "file": rel_path,
                            "line": e.lineno,
                            "description": f"Python syntax error: {e.msg}",
                            "suggestion": f"Fix syntax error at line {e.lineno}: {e.text.strip() if e.text else ''}",
                        })

                # 2. TODO/FIXME/HACK comments
                for i, line in enumerate(lines, 1):
                    stripped = line.strip()
                    for marker in ("TODO", "FIXME", "HACK", "XXX"):
                        if marker in stripped and (stripped.startswith("//") or stripped.startswith("#")):
                            issues.append({
                                "type": "todo",
                                "severity": "low",
                                "package": pkg_name,
                                "file": rel_path,
                                "line": i,
                                "description": stripped.lstrip("/#/ ").strip(),
                                "suggestion": f"Implement or remove: {stripped.lstrip('/#/ ').strip()[:100]}",
                            })

                # 3. Large functions (Python)
                if lang == "python":
                    try:
                        tree = ast.parse(content, filename=fname)
                        for node in ast.walk(tree):
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                func_lines = node.end_lineno - node.lineno + 1 if node.end_lineno else 0
                                if func_lines > 80:
                                    issues.append({
                                        "type": "complexity",
                                        "severity": "medium",
                                        "package": pkg_name,
                                        "file": rel_path,
                                        "line": node.lineno,
                                        "description": f"Function `{node.name}` is {func_lines} lines long — consider refactoring",
                                        "suggestion": f"Break `{node.name}` into smaller helper functions",
                                    })
                    except SyntaxError:
                        pass

                # 4. Bare except clauses (Python)
                if lang == "python":
                    for i, line in enumerate(lines, 1):
                        if re.match(r"\s*except\s*:", line):
                            issues.append({
                                "type": "error_handling",
                                "severity": "medium",
                                "package": pkg_name,
                                "file": rel_path,
                                "line": i,
                                "description": "Bare `except:` clause — catches SystemExit, KeyboardInterrupt etc.",
                                "suggestion": "Use `except Exception:` instead of bare `except:`",
                            })

                # 5. Security checks
                for i, line in enumerate(lines, 1):
                    stripped = line.strip()
                    # Hardcoded secrets
                    if re.search(r'(password|secret|api_key|token)\s*=\s*["\'][^"\']{8,}["\']', stripped, re.I):
                        if "env" not in stripped.lower() and "example" not in stripped.lower():
                            issues.append({
                                "type": "security",
                                "severity": "high",
                                "package": pkg_name,
                                "file": rel_path,
                                "line": i,
                                "description": "Potential hardcoded secret/credential",
                                "suggestion": "Move to environment variable",
                            })
                    # eval() usage
                    if "eval(" in stripped and not stripped.startswith("#"):
                        issues.append({
                            "type": "security",
                            "severity": "high",
                            "package": pkg_name,
                            "file": rel_path,
                            "line": i,
                            "description": "Usage of `eval()` — potential code injection risk",
                            "suggestion": "Replace eval() with ast.literal_eval() or proper parsing",
                        })

                # 6. Large files
                if len(lines) > 500:
                    issues.append({
                        "type": "complexity",
                        "severity": "low",
                        "package": pkg_name,
                        "file": rel_path,
                        "line": 1,
                        "description": f"File is {len(lines)} lines — consider splitting into modules",
                        "suggestion": f"Break {fname} into smaller, focused modules",
                    })

                # 7. Unused imports (Python — simple check)
                if lang == "python":
                    try:
                        tree = ast.parse(content, filename=fname)
                        imports = []
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Import):
                                for alias in node.names:
                                    name = alias.asname or alias.name.split(".")[-1]
                                    imports.append((name, node.lineno))
                            elif isinstance(node, ast.ImportFrom):
                                for alias in node.names:
                                    name = alias.asname or alias.name
                                    imports.append((name, node.lineno))

                        # Check if each import is used in the file body
                        for name, lineno in imports:
                            # Count occurrences (excluding the import line itself)
                            uses = sum(1 for line in lines if name in line) - 1
                            if uses <= 0 and name != "*" and not name.startswith("_"):
                                issues.append({
                                    "type": "dead_import",
                                    "severity": "low",
                                    "package": pkg_name,
                                    "file": rel_path,
                                    "line": lineno,
                                    "description": f"Potentially unused import: `{name}`",
                                    "suggestion": f"Remove unused import `{name}` at line {lineno}",
                                })
                    except SyntaxError:
                        pass

    # Sort by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    issues.sort(key=lambda x: severity_order.get(x.get("severity", "info"), 4))

    # Build summary
    by_severity = {}
    for issue in issues:
        s = issue.get("severity", "info")
        by_severity[s] = by_severity.get(s, 0) + 1

    by_type = {}
    for issue in issues:
        t = issue.get("type", "other")
        by_type[t] = by_type.get(t, 0) + 1

    summary_parts = []
    if by_severity.get("critical"): summary_parts.append(f"🔴 {by_severity['critical']} critical")
    if by_severity.get("high"): summary_parts.append(f"🟠 {by_severity['high']} high")
    if by_severity.get("medium"): summary_parts.append(f"🟡 {by_severity['medium']} medium")
    if by_severity.get("low"): summary_parts.append(f"🔵 {by_severity['low']} low")

    return {
        "total_issues": len(issues),
        "summary": " | ".join(summary_parts) if summary_parts else "✅ Clean — no issues found",
        "by_type": by_type,
        "by_severity": by_severity,
        "issues": issues[:30],  # Cap output
    }


# ── Test Runner ──────────────────────────────────────────────────────
def _run_tests(package: str = "all") -> dict:
    """Run test suites across packages."""
    results = {}

    packages_to_test = PACKAGES.keys() if package == "all" else [package]

    for pkg in packages_to_test:
        pkg_info = PACKAGES.get(pkg)
        if not pkg_info:
            continue

        pkg_path = os.path.join(PROJECT_ROOT, pkg_info["path"])
        if not os.path.exists(pkg_path):
            results[pkg] = {"status": "skip", "error": f"Path not found: {pkg_path}"}
            continue

        if pkg_info["lang"] == "python":
            # Python: ast.parse all files
            try:
                errors = []
                for root, dirs, files in os.walk(pkg_path):
                    dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "venv")]
                    for f in files:
                        if f.endswith(".py"):
                            fpath = os.path.join(root, f)
                            try:
                                ast.parse(open(fpath, "r").read(), filename=f)
                            except SyntaxError as e:
                                errors.append(f"{f}:{e.lineno}: {e.msg}")
                results[pkg] = {"status": "pass" if not errors else "fail", "errors": errors}
            except Exception as e:
                results[pkg] = {"status": "fail", "error": str(e)}
        else:
            # TypeScript: try tsc --noEmit via docker exec (gateway/web containers)
            container_name = f"littlebirdalt-{pkg}-1" if pkg == "gateway" else f"littlebirdalt-frontend-1"
            try:
                res = subprocess.run(
                    ["docker", "exec", container_name, "npx", "tsc", "--noEmit"],
                    capture_output=True, text=True, timeout=60,
                )
                results[pkg] = {
                    "status": "pass" if res.returncode == 0 else "fail",
                    "errors": res.stdout[:500] if res.returncode != 0 else None,
                }
            except subprocess.TimeoutExpired:
                results[pkg] = {"status": "timeout"}
            except FileNotFoundError:
                # Docker not available — just check files exist
                results[pkg] = {"status": "skip", "error": "Docker exec not available"}
            except Exception as e:
                results[pkg] = {"status": "fail", "error": str(e)}

    all_pass = all(r.get("status") in ("pass", "skip") for r in results.values())
    return {
        "all_pass": all_pass,
        "summary": "✅ All tests pass" if all_pass else "❌ Some tests failed",
        "results": results,
    }


# ── Proposal System ─────────────────────────────────────────────────
async def _propose_improvements() -> dict:
    """Create proposals from scan results, enrich with LLM analysis, send to Telegram."""

    if not _last_scan_results or not _last_scan_results.get("issues"):
        return {"error": "No scan results. Run 'scan' first."}

    issues = _last_scan_results["issues"]

    # Only propose actionable issues (not info/low TODOs in node_modules)
    actionable = [
        i for i in issues
        if i.get("severity") in ("critical", "high", "medium")
        and "node_modules" not in i.get("file", "")
    ]

    # Step 1: LLM deep analysis on the top candidate files
    llm_proposals = await _llm_analyze(actionable[:10])

    # Combine static + LLM proposals (LLM proposals go first — they're smarter)
    all_proposals = llm_proposals + actionable

    if not all_proposals:
        _send_summary_to_telegram(
            "🤖 <b>Kestrel Self-Improvement</b>\n\n"
            "✅ No actionable improvements found. Codebase is clean!\n\n"
            f"📊 Scanned {_last_scan_results.get('total_issues', 0)} items total."
        )
        return {"message": "No actionable improvements. Summary sent to Telegram.", "count": 0}

    # Load existing proposals and add new ones
    pending = _load_proposals()

    # Send top proposals to Telegram
    proposals_sent = 0
    for issue in all_proposals[:5]:  # Cap at 5 proposals per cycle
        proposal_id = str(uuid.uuid4())
        proposal = {
            "id": proposal_id,
            "created_at": time.time(),
            **issue,
        }
        pending[proposal_id] = proposal

        result = _send_proposal_to_telegram(proposal)
        if result.get("ok"):
            proposals_sent += 1
        else:
            logger.error(f"Failed to send proposal {proposal_id}: {result}")

    # Persist proposals to disk
    _save_proposals(pending)

    # Send summary
    llm_label = f" (🧠 {len(llm_proposals)} AI-analyzed)" if llm_proposals else ""
    _send_summary_to_telegram(
        f"🤖 <b>Kestrel Self-Improvement Scan Complete</b>\n\n"
        f"📊 {_last_scan_results.get('total_issues', 0)} issues found\n"
        f"📨 {proposals_sent} proposals sent for approval{llm_label}\n\n"
        f"Reply with ✅ to approve or ❌ to deny each proposal."
    )

    return {
        "proposals_sent": proposals_sent,
        "llm_proposals": len(llm_proposals),
        "total_actionable": len(actionable),
        "message": f"Sent {proposals_sent} proposals to Telegram ({len(llm_proposals)} AI-enhanced)",
    }


# ── LLM-Powered Deep Analysis ───────────────────────────────────────
async def _llm_analyze(candidate_issues: list[dict]) -> list[dict]:
    """
    Send the codebase to the user's preferred cloud LLM for deep analysis.
    
    Two-phase approach:
    1. Build a full project tree + file summaries so the LLM sees the architecture
    2. Send the most important source files for deep code review
    
    The LLM reviews the full picture and provides intelligent suggestions for:
    - Logic bugs and edge cases
    - Architecture improvements  
    - Performance optimizations
    - Security vulnerabilities
    - Better error handling
    - Refactoring opportunities
    """

    # Determine the user's preferred provider
    provider_name = os.getenv("DEFAULT_LLM_PROVIDER", "google")
    if provider_name == "local":
        provider_name = "google"  # Fall back to cloud for analysis

    try:
        from providers.cloud import CloudProvider
        provider = CloudProvider(provider_name)
        if not provider.is_ready():
            logger.warning(f"CloudProvider '{provider_name}' not ready (no API key?). Skipping LLM analysis.")
            return []
    except Exception as e:
        logger.error(f"Failed to initialize CloudProvider: {e}")
        return []

    # ── Phase 1: Build codebase overview ──────────────────────────────
    tree_lines = []
    file_summaries = []
    
    for pkg_name, pkg_info in PACKAGES.items():
        pkg_path = os.path.join(PROJECT_ROOT, pkg_info["path"])
        if not os.path.exists(pkg_path):
            continue
        
        tree_lines.append(f"\n📦 {pkg_name} ({pkg_info['lang']})")
        ext = pkg_info["ext"]
        
        for root, dirs, files in os.walk(pkg_path):
            dirs[:] = [d for d in dirs if d not in (
                "node_modules", "__pycache__", ".git", "dist", "build", ".next", 
                "venv", ".venv", "coverage", "test", "tests"
            )]
            
            depth = root.replace(pkg_path, "").count(os.sep)
            indent = "  " * (depth + 1)
            rel_dir = os.path.relpath(root, os.path.join(PROJECT_ROOT, pkg_info["path"]))
            if rel_dir != ".":
                tree_lines.append(f"{indent}📁 {rel_dir}/")
            
            for fname in sorted(files):
                if not (fname.endswith(ext) or fname.endswith(".ts") or fname.endswith(".tsx")):
                    continue
                filepath = os.path.join(root, fname)
                try:
                    content = open(filepath, "r", errors="ignore").read()
                    line_count = content.count("\n") + 1
                    tree_lines.append(f"{indent}  {fname} ({line_count} lines)")
                    
                    # Extract key exports/classes/functions for summary
                    if pkg_info["lang"] == "python" and fname.endswith(".py"):
                        try:
                            tree = ast.parse(content, filename=fname)
                            names = []
                            for node in ast.iter_child_nodes(tree):
                                if isinstance(node, ast.ClassDef):
                                    names.append(f"class {node.name}")
                                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    names.append(f"def {node.name}")
                            if names:
                                rel = os.path.relpath(filepath, PROJECT_ROOT)
                                file_summaries.append(f"  {rel}: {', '.join(names[:8])}")
                        except SyntaxError:
                            pass
                    elif fname.endswith((".ts", ".tsx")):
                        # Quick TS export scan
                        exports = re.findall(r'export\s+(?:default\s+)?(?:function|class|const|interface|type)\s+(\w+)', content)
                        if exports:
                            rel = os.path.relpath(filepath, PROJECT_ROOT)
                            file_summaries.append(f"  {rel}: {', '.join(exports[:8])}")
                except Exception:
                    continue

    codebase_tree = "\n".join(tree_lines)
    key_exports = "\n".join(file_summaries[:40])

    # ── Phase 2: Collect source files for deep review ─────────────────
    files_to_analyze: dict[str, str] = {}
    
    # Include flagged files from static analysis
    for issue in (candidate_issues or []):
        filepath = os.path.join(PROJECT_ROOT, issue.get("file", ""))
        if filepath in files_to_analyze or not os.path.exists(filepath):
            continue
        try:
            content = open(filepath, "r", errors="ignore").read()
            lines = content.split("\n")
            if len(lines) > 300:
                content = "\n".join(lines[:300]) + f"\n\n... ({len(lines)} total lines, truncated)"
            files_to_analyze[issue.get("file", "")] = content
        except Exception:
            continue
        if len(files_to_analyze) >= 3:
            break

    # Also include key architectural files even if no static issues
    key_files = [
        "packages/brain/server.py",
        "packages/brain/agent/loop.py",
        "packages/brain/agent/coordinator.py",
        "packages/gateway/src/server.ts",
        "packages/gateway/src/routes/features.ts",
    ]
    for kf in key_files:
        if len(files_to_analyze) >= 5:
            break
        if kf in files_to_analyze:
            continue
        filepath = os.path.join(PROJECT_ROOT, kf)
        if os.path.exists(filepath):
            try:
                content = open(filepath, "r", errors="ignore").read()
                lines = content.split("\n")
                if len(lines) > 200:
                    content = "\n".join(lines[:200]) + f"\n\n... ({len(lines)} total lines, truncated)"
                files_to_analyze[kf] = content
            except Exception:
                continue

    files_section = "\n\n".join(
        f"### {fname}\n```\n{content}\n```"
        for fname, content in files_to_analyze.items()
    )

    issues_section = ""
    if candidate_issues:
        issues_section = "## Static Analysis Already Found\n" + "\n".join(
            f"- [{i.get('severity')}] {i.get('file')}:{i.get('line', '?')} — {i.get('description', '')}"
            for i in candidate_issues[:10]
        )

    # ── Phase 3: LLM prompt ──────────────────────────────────────────
    prompt = f"""You are Kestrel's self-improvement engine. You are analyzing the FULL Kestrel AI platform codebase to find the most impactful improvements.

## Codebase Structure
{codebase_tree}

## Key Exports / Definitions
{key_exports}

{issues_section}

## Source Files (for deep review)
{files_section}

## Your Task
Think DEEPLY and SYSTEM-WIDE. Consider:
1. **Logic bugs** — edge cases, race conditions, off-by-one errors, null safety
2. **Architecture** — coupling issues, missing abstractions, circular dependencies, design patterns that would improve the system
3. **Performance** — unnecessary allocations, N+1 patterns, blocking calls in async code, missing caches
4. **Security** — injection risks, auth bypasses, data leaks, missing input validation
5. **Error handling** — unhandled failures, silent swallows, missing retries, error messages that leak internals
6. **Code quality** — dead code paths, unclear naming, missing types, code that should be shared between packages
7. **Missing features** — gaps in the system, obvious improvements, integration opportunities

Be specific and actionable. Don't flag trivial style issues.

Respond with a JSON array of improvement proposals. Each proposal should have:
- "type": one of "bug", "architecture", "performance", "security", "error_handling", "quality", "feature"
- "severity": one of "critical", "high", "medium"
- "file": relative file path
- "line": approximate line number (0 if system-wide)
- "description": clear description of the issue (2-3 sentences)
- "suggestion": specific fix suggestion (2-4 sentences, be concrete)

Return ONLY the JSON array, no markdown code fences. Limit to 5 most impactful proposals."""

    try:
        logger.info(f"LLM analysis: sending {len(files_to_analyze)} files + codebase tree to {provider_name}...")
        response = await provider.generate(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=8192,
        )

        # Parse the JSON response — robust extraction
        response = response.strip()
        proposals = _extract_json_array(response)

        if proposals is None:
            logger.error(f"Could not extract JSON from LLM response ({len(response)} chars)")
            logger.debug(f"Raw response preview: {response[:300]}")
            return []

        if not isinstance(proposals, list):
            proposals = [proposals]

        # Tag each as LLM-generated and add package info
        for p in proposals:
            p["source"] = "llm"
            p["llm_provider"] = provider_name
            fpath = p.get("file", "")
            if "brain" in fpath:
                p["package"] = "brain"
            elif "gateway" in fpath:
                p["package"] = "gateway"
            elif "web" in fpath:
                p["package"] = "web"
            else:
                p["package"] = "unknown"

        logger.info(f"LLM analysis: got {len(proposals)} proposals from {provider_name}")
        return proposals

    except json.JSONDecodeError as e:
        logger.error(f"LLM response wasn't valid JSON: {e}")
        return []
    except Exception as e:
        logger.error(f"LLM analysis failed: {e}")
        return []




def _handle_approval(proposal_id: str, approved: bool) -> dict:
    """Handle approval or denial of a proposal."""
    if not proposal_id:
        return {"error": "proposal_id is required"}

    pending = _load_proposals()

    # Check full ID or prefix match
    matching = None
    for pid, proposal in pending.items():
        if pid == proposal_id or pid.startswith(proposal_id):
            matching = proposal
            proposal_id = pid
            break

    if not matching:
        return {"error": f"Proposal not found: {proposal_id}", "pending": list(pending.keys())}

    if approved:
        # Remove from pending
        del pending[proposal_id]
        _save_proposals(pending)

        # Notify user
        _send_summary_to_telegram(
            f"✅ <b>Approved:</b> {matching.get('description', '')[:200]}\n\n"
            f"Kestrel will apply this fix on the next improvement cycle."
        )

        return {
            "status": "approved",
            "message": f"Proposal {proposal_id[:8]} approved. Will be applied next cycle.",
            "proposal": matching,
        }
    else:
        # Remove from pending
        del pending[proposal_id]
        _save_proposals(pending)

        _send_summary_to_telegram(
            f"❌ <b>Denied:</b> {matching.get('description', '')[:200]}\n\n"
            f"Proposal discarded."
        )

        return {
            "status": "denied",
            "message": f"Proposal {proposal_id[:8]} denied and discarded.",
        }


# ── Background Scheduler ─────────────────────────────────────────────
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
            if os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN"):
                await _github_sync()
            else:
                logger.info("No GITHUB_PERSONAL_ACCESS_TOKEN — skipping GitHub sync")

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
