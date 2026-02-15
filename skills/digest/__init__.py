"""
Daily Digest Skill
RSS feed reader + article fetch. Subscribe to feeds and get latest articles.
Uses only stdlib (xml.etree.ElementTree + urllib) — no extra dependencies.
"""

import json
import os
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime

# ---------------------------------------------------------------------------
# Feed storage (simple JSON file)
# ---------------------------------------------------------------------------

_FEEDS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "rss_feeds.json")


def _load_feeds() -> list[dict]:
    if os.path.isfile(_FEEDS_FILE):
        try:
            with open(_FEEDS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_feeds(feeds: list[dict]):
    os.makedirs(os.path.dirname(_FEEDS_FILE), exist_ok=True)
    with open(_FEEDS_FILE, "w") as f:
        json.dump(feeds, f, indent=2)


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "digest_add_feed",
            "description": "Subscribe to an RSS or Atom feed. Provide the feed URL and an optional label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The RSS/Atom feed URL"},
                    "label": {"type": "string", "description": "A friendly label for this feed (optional)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "digest_list_feeds",
            "description": "List all subscribed RSS/Atom feeds.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "digest_fetch",
            "description": "Fetch the latest articles from all subscribed feeds (or a specific feed). Returns headlines, summaries, and links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "feed_url": {"type": "string", "description": "Fetch only from this feed URL (optional, omit for all feeds)"},
                    "limit": {"type": "integer", "description": "Max articles per feed (default 5)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "digest_remove_feed",
            "description": "Unsubscribe from an RSS/Atom feed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The feed URL to unsubscribe from"},
                },
                "required": ["url"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_feed(url: str, limit: int = 5) -> dict:
    """Fetch and parse an RSS/Atom feed."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LibreBird/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read()

        root = ET.fromstring(xml_data)

        # Detect feed type & namespace
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        articles = []

        # RSS 2.0
        channel = root.find("channel")
        if channel is not None:
            feed_title = (channel.findtext("title") or "").strip()
            for item in channel.findall("item")[:limit]:
                articles.append({
                    "title": (item.findtext("title") or "").strip(),
                    "link": (item.findtext("link") or "").strip(),
                    "summary": (item.findtext("description") or "").strip()[:300],
                    "published": (item.findtext("pubDate") or "").strip(),
                })
            return {"feed_title": feed_title, "url": url, "articles": articles}

        # Atom
        if root.tag == "{http://www.w3.org/2005/Atom}feed" or root.tag == "feed":
            feed_title = ""
            title_el = root.find("atom:title", ns) or root.find("title")
            if title_el is not None:
                feed_title = (title_el.text or "").strip()
            entries = root.findall("atom:entry", ns) or root.findall("entry")
            for entry in entries[:limit]:
                title = ""
                title_el = entry.find("atom:title", ns) or entry.find("title")
                if title_el is not None:
                    title = (title_el.text or "").strip()
                link = ""
                link_el = entry.find("atom:link", ns) or entry.find("link")
                if link_el is not None:
                    link = link_el.get("href", "")
                summary = ""
                summary_el = entry.find("atom:summary", ns) or entry.find("summary") or entry.find("atom:content", ns) or entry.find("content")
                if summary_el is not None:
                    summary = (summary_el.text or "").strip()[:300]
                articles.append({"title": title, "link": link, "summary": summary})
            return {"feed_title": feed_title, "url": url, "articles": articles}

        return {"url": url, "error": "Could not parse feed — unrecognized format"}
    except ET.ParseError as e:
        return {"url": url, "error": f"XML parse error: {str(e)}"}
    except Exception as e:
        return {"url": url, "error": f"Failed to fetch feed: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_digest_add_feed(url: str, label: str = None) -> dict:
    feeds = _load_feeds()
    for f in feeds:
        if f["url"] == url:
            return {"url": url, "status": "already_subscribed", "label": f.get("label", "")}
    # Validate by trying to fetch
    result = _parse_feed(url, limit=1)
    if "error" in result:
        return {"url": url, "error": f"Could not validate feed: {result['error']}"}
    entry = {
        "url": url,
        "label": label or result.get("feed_title", url),
        "added": datetime.now().isoformat(),
    }
    feeds.append(entry)
    _save_feeds(feeds)
    return {"status": "subscribed", "feed": entry, "feed_title": result.get("feed_title")}


def tool_digest_list_feeds() -> dict:
    feeds = _load_feeds()
    if not feeds:
        return {"feeds": [], "count": 0, "message": "No feeds subscribed yet. Use digest_add_feed to add one."}
    return {"feeds": feeds, "count": len(feeds)}


def tool_digest_fetch(feed_url: str = None, limit: int = 5) -> dict:
    feeds = _load_feeds()
    if feed_url:
        feeds = [f for f in feeds if f["url"] == feed_url]
        if not feeds:
            # Still try to fetch the url directly
            return _parse_feed(feed_url, limit)
    if not feeds:
        return {"error": "No feeds to fetch. Subscribe to some feeds first with digest_add_feed."}
    all_results = []
    for f in feeds:
        result = _parse_feed(f["url"], limit)
        result["label"] = f.get("label", "")
        all_results.append(result)
    total_articles = sum(len(r.get("articles", [])) for r in all_results)
    return {"feeds": all_results, "total_articles": total_articles}


def tool_digest_remove_feed(url: str) -> dict:
    feeds = _load_feeds()
    original_count = len(feeds)
    feeds = [f for f in feeds if f["url"] != url]
    if len(feeds) == original_count:
        return {"url": url, "status": "not_found", "message": "Feed URL not found in subscriptions."}
    _save_feeds(feeds)
    return {"url": url, "status": "unsubscribed"}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "digest_add_feed": lambda args: tool_digest_add_feed(args.get("url", ""), args.get("label")),
    "digest_list_feeds": lambda args: tool_digest_list_feeds(),
    "digest_fetch": lambda args: tool_digest_fetch(args.get("feed_url"), args.get("limit", 5)),
    "digest_remove_feed": lambda args: tool_digest_remove_feed(args.get("url", "")),
}
