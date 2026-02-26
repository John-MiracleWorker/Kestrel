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

logger = logging.getLogger("brain.agent.tools.self_improve")

PROJECT_ROOT = "/project"

INACTIVITY_THRESHOLD_SECONDS = 30 * 60

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

def _extract_json_array(text: str) -> Optional[list]:
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

_PROPOSALS_FILE = os.path.join(PROJECT_ROOT, ".kestrel", "cache", "kestrel_proposals.json")

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

_last_scan_results: dict = {}

_PERSISTENT_DIR = os.path.join(PROJECT_ROOT, ".kestrel", "cache")
_SCAN_CACHE_FILE = os.path.join(_PERSISTENT_DIR, "self_improve_scan.json")
_SCAN_RESULTS_FILE = os.path.join(_PERSISTENT_DIR, "self_improve_last_results.json")

def _load_scan_cache() -> dict:
    try:
        with open(_SCAN_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"files": {}}

def _save_scan_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_SCAN_CACHE_FILE), exist_ok=True)
        with open(_SCAN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError as e:
        logger.debug(f"Failed to persist scan cache: {e}")

def _file_signature(path: str) -> str:
    stat = os.stat(path)
    return f"{stat.st_mtime_ns}:{stat.st_size}"

def _persist_scan_results(results: dict) -> None:
    """Write scan results to disk so they survive service restarts."""
    try:
        os.makedirs(os.path.dirname(_SCAN_RESULTS_FILE), exist_ok=True)
        with open(_SCAN_RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f)
    except OSError as e:
        logger.debug(f"Failed to persist scan results: {e}")


def _restore_scan_results() -> dict:
    """Load the most recent scan results from disk (cold-start recovery)."""
    try:
        with open(_SCAN_RESULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# Restore last scan results on import so 'report' works after restart
_last_scan_results = _restore_scan_results()

_codebase_overview_cache: dict = {}

PACKAGES = {
    "brain": {"path": "packages/brain", "lang": "python", "ext": ".py"},
    "gateway": {"path": "packages/gateway/src", "lang": "typescript", "ext": ".ts"},
    "web": {"path": "packages/web/src", "lang": "typescript", "ext": ".tsx"},
    "hands": {"path": "packages/hands", "lang": "python", "ext": ".py"},
}