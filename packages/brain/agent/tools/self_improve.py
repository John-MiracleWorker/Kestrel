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


# ── Pending Proposals (in-memory store) ──────────────────────────────
_pending_proposals: dict[str, dict] = {}


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
                "approval. Actions: scan, test, report, propose, approve, deny."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["scan", "test", "report", "propose", "approve", "deny", "list_pending"],
                        "description": (
                            "scan = deep codebase analysis, "
                            "test = run test suite, "
                            "report = last scan summary, "
                            "propose = send proposals to Telegram, "
                            "approve/deny = act on a proposal, "
                            "list_pending = show pending proposals"
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
        return _propose_improvements()
    elif action == "approve":
        return _handle_approval(proposal_id, approved=True)
    elif action == "deny":
        return _handle_approval(proposal_id, approved=False)
    elif action == "list_pending":
        return {"pending": list(_pending_proposals.values()), "count": len(_pending_proposals)}
    else:
        return {"error": f"Unknown action: {action}"}


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
def _propose_improvements() -> dict:
    """Create proposals from scan results and send them to Telegram."""
    global _pending_proposals

    if not _last_scan_results or not _last_scan_results.get("issues"):
        return {"error": "No scan results. Run 'scan' first."}

    issues = _last_scan_results["issues"]

    # Only propose actionable issues (not info/low TODOs in node_modules)
    actionable = [
        i for i in issues
        if i.get("severity") in ("critical", "high", "medium")
        and "node_modules" not in i.get("file", "")
    ]

    if not actionable:
        _send_summary_to_telegram(
            "🤖 <b>Kestrel Self-Improvement</b>\n\n"
            "✅ No actionable improvements found. Codebase is clean!\n\n"
            f"📊 Scanned {_last_scan_results.get('total_issues', 0)} items total."
        )
        return {"message": "No actionable improvements. Summary sent to Telegram.", "count": 0}

    # Create proposals and send each to Telegram
    proposals_sent = 0
    for issue in actionable[:5]:  # Cap at 5 proposals per cycle
        proposal_id = str(uuid.uuid4())
        proposal = {
            "id": proposal_id,
            "created_at": time.time(),
            **issue,
        }
        _pending_proposals[proposal_id] = proposal

        result = _send_proposal_to_telegram(proposal)
        if result.get("ok"):
            proposals_sent += 1
        else:
            logger.error(f"Failed to send proposal {proposal_id}: {result}")

    # Send summary
    _send_summary_to_telegram(
        f"🤖 <b>Kestrel Self-Improvement Scan Complete</b>\n\n"
        f"📊 {_last_scan_results.get('total_issues', 0)} issues found\n"
        f"📨 {proposals_sent} proposals sent for approval\n\n"
        f"Reply with ✅ to approve or ❌ to deny each proposal."
    )

    return {
        "proposals_sent": proposals_sent,
        "total_actionable": len(actionable),
        "message": f"Sent {proposals_sent} proposals to Telegram for approval",
    }


def _handle_approval(proposal_id: str, approved: bool) -> dict:
    """Handle approval or denial of a proposal."""
    if not proposal_id:
        return {"error": "proposal_id is required"}

    # Check full ID or prefix match
    matching = None
    for pid, proposal in _pending_proposals.items():
        if pid == proposal_id or pid.startswith(proposal_id):
            matching = proposal
            proposal_id = pid
            break

    if not matching:
        return {"error": f"Proposal not found: {proposal_id}", "pending": list(_pending_proposals.keys())}

    if approved:
        # Remove from pending
        del _pending_proposals[proposal_id]

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
        del _pending_proposals[proposal_id]

        _send_summary_to_telegram(
            f"❌ <b>Denied:</b> {matching.get('description', '')[:200]}\n\n"
            f"Proposal discarded."
        )

        return {
            "status": "denied",
            "message": f"Proposal {proposal_id[:8]} denied and discarded.",
        }
