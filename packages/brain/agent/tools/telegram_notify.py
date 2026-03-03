"""
Telegram Notify Tool — send messages directly to Telegram from agent tasks.

This tool allows the agent to send notifications, summaries, and updates
directly to Telegram via the Bot API. Used by:
  - Cron jobs (Gmail summary, AI news briefing)
  - Automated tasks that need to report results
  - Any agent workflow that needs to push info to the user

Uses TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
"""

import json
import logging
import os
import re
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.telegram_notify")


# ── Telegram Bot API ───────────────────────────────────────────────


def _telegram_api(method: str, payload: dict) -> dict:
    """Call Telegram Bot API directly."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}

    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (URLError, Exception) as e:
        logger.error(f"Telegram API error: {e}")
        return {"ok": False, "error": str(e)}


def _md_to_html(text: str) -> str:
    """
    Convert common markdown patterns to Telegram HTML.
    Telegram supports: <b>, <i>, <code>, <pre>, <a>, <s>, <u>.
    """
    html = text
    # Bold: **text** or __text__ → <b>text</b>
    html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', html)
    html = re.sub(r'__(.+?)__', r'<b>\1</b>', html)
    # Italic: *text* → <i>text</i>  (but not inside <b> tags)
    html = re.sub(r'(?<!\*)(\*)(?!\*)(.+?)(?<!\*)\1(?!\*)', r'<i>\2</i>', html)
    # Code blocks: ```text``` → <pre>text</pre>
    html = re.sub(r'```(?:\w+)?\n?(.*?)```', r'<pre>\1</pre>', html, flags=re.DOTALL)
    # Inline code: `text` → <code>text</code>
    html = re.sub(r'`([^`]+)`', r'<code>\1</code>', html)
    # Headers: ## text → <b>text</b>
    html = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', html, flags=re.MULTILINE)
    # Bullet lists: - item or * item → • item
    html = re.sub(r'^[\s]*[-*]\s+', '• ', html, flags=re.MULTILINE)
    # Escape remaining angle brackets (not part of our tags)
    html = re.sub(r'<(?!/?(b|i|code|pre|a|s|u)[ >])', '&lt;', html)
    return html


def _chunk_message(text: str, max_len: int = 4000) -> list[str]:
    """Split a message into Telegram-safe chunks."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Split at paragraph or sentence boundary
        split_at = remaining.rfind('\n\n', 0, max_len)
        if split_at < max_len // 3:
            split_at = remaining.rfind('\n', 0, max_len)
        if split_at < max_len // 3:
            split_at = remaining.rfind('. ', 0, max_len)
        if split_at < max_len // 3:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()

    return chunks


# ── Tool handler ───────────────────────────────────────────────────


async def telegram_notify_handler(
    message: str = "",
    chat_id: str = "",
    parse_mode: str = "auto",
    silent: bool = False,
    **kwargs,
) -> dict:
    """
    Send a message to Telegram.

    Args:
        message: The text to send. Supports markdown formatting.
        chat_id: Target chat ID. Defaults to TELEGRAM_CHAT_ID env var.
        parse_mode: 'html', 'markdown', or 'auto' (converts markdown to html).
        silent: If true, sends without notification sound.
    """
    if not message:
        return {"success": False, "error": "Message text is required"}

    target_chat = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not target_chat:
        return {
            "success": False,
            "error": "No chat_id provided and TELEGRAM_CHAT_ID not configured",
        }

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {
            "success": False,
            "error": "TELEGRAM_BOT_TOKEN not configured. Set the environment variable.",
        }

    # Format message
    if parse_mode == "auto":
        formatted = _md_to_html(message)
        tg_parse_mode = "HTML"
    elif parse_mode == "html":
        formatted = message
        tg_parse_mode = "HTML"
    elif parse_mode == "markdown":
        formatted = message
        tg_parse_mode = "Markdown"
    else:
        formatted = message
        tg_parse_mode = None

    # Send in chunks if too long
    chunks = _chunk_message(formatted)
    results = []
    success = True

    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": int(target_chat) if target_chat.lstrip("-").isdigit() else target_chat,
            "text": chunk,
            "disable_notification": silent,
            "disable_web_page_preview": True,
        }
        if tg_parse_mode:
            payload["parse_mode"] = tg_parse_mode

        result = _telegram_api("sendMessage", payload)

        if not result.get("ok"):
            # Retry without formatting if parse fails
            if tg_parse_mode:
                payload.pop("parse_mode", None)
                payload["text"] = message if i == 0 else chunk
                result = _telegram_api("sendMessage", payload)

        results.append(result)
        if not result.get("ok"):
            success = False
            logger.error(f"Telegram send failed: {result.get('error', 'unknown')}")

    if success:
        return {
            "success": True,
            "message": f"Sent to Telegram ({len(chunks)} message{'s' if len(chunks) > 1 else ''})",
            "chat_id": target_chat,
        }
    else:
        errors = [r.get("error", "unknown") for r in results if not r.get("ok")]
        return {
            "success": False,
            "error": f"Some messages failed: {'; '.join(errors)}",
            "partial_results": results,
        }


# ── Tool Registration ──────────────────────────────────────────────


TELEGRAM_NOTIFY_TOOL = ToolDefinition(
    name="telegram_send",
    description=(
        "Send a message directly to Telegram. Use this to deliver notifications, "
        "summaries, briefings, and updates to the user's Telegram chat. "
        "Supports markdown formatting (auto-converted to Telegram HTML). "
        "The default chat_id is loaded from TELEGRAM_CHAT_ID env var. "
        "Use this when asked to 'send to Telegram', 'notify on Telegram', "
        "or when completing cron jobs that should report results via Telegram."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to send. Supports markdown formatting (**bold**, `code`, ## headers, - lists).",
            },
            "chat_id": {
                "type": "string",
                "description": "Target Telegram chat ID. Leave empty to use the default configured chat.",
            },
            "parse_mode": {
                "type": "string",
                "enum": ["auto", "html", "markdown"],
                "description": "Message formatting: 'auto' converts markdown to HTML (recommended), 'html' for raw HTML, 'markdown' for Telegram markdown.",
            },
            "silent": {
                "type": "boolean",
                "description": "Send without notification sound. Useful for routine updates.",
            },
        },
        "required": ["message"],
    },
    risk_level=RiskLevel.LOW,
)


def register_telegram_tools(registry) -> None:
    """Register Telegram notification tools in the agent tool registry."""
    registry.register(
        definition=TELEGRAM_NOTIFY_TOOL,
        handler=telegram_notify_handler,
    )
