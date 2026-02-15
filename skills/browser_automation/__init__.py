"""
Browser Automation Skill
Full browser control via Playwright: navigate, click, type, screenshot, extract text.
Requires: pip install playwright && playwright install chromium
"""

import base64
import json
import os
import tempfile
from datetime import datetime

# Lazy-loaded browser instance
_browser = None
_page = None


def _ensure_browser():
    """Lazy-init a Playwright browser instance."""
    global _browser, _page
    if _page is not None:
        return _page

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError("Playwright not installed. Run: pip install playwright && playwright install chromium")

    pw = sync_playwright().start()
    _browser = pw.chromium.launch(headless=True)
    _page = _browser.new_page(viewport={"width": 1280, "height": 720})
    _page.set_default_timeout(15000)
    return _page


def _close_browser():
    global _browser, _page
    if _browser:
        try:
            _browser.close()
        except Exception:
            pass
    _browser = None
    _page = None


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate to a URL in a headless browser. Returns the page title and text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to navigate to"},
                    "wait_for": {"type": "string", "description": "CSS selector to wait for before returning (optional)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element on the current page by CSS selector or text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the element to click"},
                    "text": {"type": "string", "description": "Visible text of the element to click (alternative to selector)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into an input field on the current page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the input field"},
                    "text": {"type": "string", "description": "Text to type into the field"},
                    "clear_first": {"type": "boolean", "description": "Clear the field before typing (default true)"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page. Returns the path to the saved image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "full_page": {"type": "boolean", "description": "Capture the full scrollable page (default false)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_extract",
            "description": "Extract text content from the current page, optionally filtering by CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector to extract text from (optional, defaults to full page body)"},
                },
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_browser_navigate(url: str, wait_for: str = None) -> dict:
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        page = _ensure_browser()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if wait_for:
            page.wait_for_selector(wait_for, timeout=10000)
        title = page.title()
        # Get a summary of visible text (first 3000 chars)
        text = page.inner_text("body")[:3000] if page.query_selector("body") else ""
        return {
            "url": page.url, "title": title,
            "status": resp.status if resp else None,
            "text_preview": text,
        }
    except ImportError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"url": url, "error": f"Navigation failed: {str(e)}"}


def tool_browser_click(selector: str = None, text: str = None) -> dict:
    try:
        page = _ensure_browser()
        if text:
            el = page.get_by_text(text, exact=False).first
            el.click(timeout=5000)
            return {"clicked": f"text='{text}'", "status": "success"}
        elif selector:
            page.click(selector, timeout=5000)
            return {"clicked": selector, "status": "success"}
        else:
            return {"error": "Provide either 'selector' or 'text' to click."}
    except Exception as e:
        return {"error": f"Click failed: {str(e)}"}


def tool_browser_type(selector: str, text: str, clear_first: bool = True) -> dict:
    try:
        page = _ensure_browser()
        if clear_first:
            page.fill(selector, text, timeout=5000)
        else:
            page.type(selector, text, timeout=5000)
        return {"selector": selector, "typed": text[:100], "status": "success"}
    except Exception as e:
        return {"error": f"Type failed: {str(e)}"}


def tool_browser_screenshot(full_page: bool = False) -> dict:
    try:
        page = _ensure_browser()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshots_dir = os.path.expanduser("~/Desktop")
        path = os.path.join(screenshots_dir, f"libre_bird_screenshot_{timestamp}.png")
        page.screenshot(path=path, full_page=full_page)
        return {"path": path, "url": page.url, "title": page.title(), "status": "saved"}
    except Exception as e:
        return {"error": f"Screenshot failed: {str(e)}"}


def tool_browser_extract(selector: str = None) -> dict:
    try:
        page = _ensure_browser()
        if selector:
            elements = page.query_selector_all(selector)
            texts = [el.inner_text() for el in elements[:20]]
            return {
                "selector": selector, "elements": texts,
                "count": len(texts), "url": page.url,
            }
        else:
            text = page.inner_text("body")
            if len(text) > 10000:
                text = text[:10000] + "\n\n[... truncated ...]"
            return {"text": text, "char_count": len(text), "url": page.url, "title": page.title()}
    except Exception as e:
        return {"error": f"Extract failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "browser_navigate": lambda args: tool_browser_navigate(args.get("url", ""), args.get("wait_for")),
    "browser_click": lambda args: tool_browser_click(args.get("selector"), args.get("text")),
    "browser_type": lambda args: tool_browser_type(args.get("selector", ""), args.get("text", ""), args.get("clear_first", True)),
    "browser_screenshot": lambda args: tool_browser_screenshot(args.get("full_page", False)),
    "browser_extract": lambda args: tool_browser_extract(args.get("selector")),
}
