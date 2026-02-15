"""
Wikipedia + Wolfram Alpha Skill
Look up encyclopaedic facts and compute mathematical/scientific answers.
Uses only stdlib (urllib + json) — zero dependencies.
"""

import json
import logging
import urllib.request
import urllib.parse
import urllib.error
import os

logger = logging.getLogger("libre_bird.skills.wikipedia")

_WIKI_API = "https://en.wikipedia.org/w/api.php"
_WOLFRAM_API = "https://api.wolframalpha.com/v1/result"


# ---------------------------------------------------------------------------
# Wikipedia helpers
# ---------------------------------------------------------------------------

def _wiki_get(params: dict) -> dict:
    """Hit the MediaWiki API and return parsed JSON."""
    params["format"] = "json"
    url = f"{_WIKI_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "LibreBird/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def tool_wikipedia_search(args: dict) -> dict:
    """Search Wikipedia for articles matching a query."""
    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    limit = min(int(args.get("limit", 5)), 10)

    try:
        data = _wiki_get({
            "action": "opensearch",
            "search": query,
            "limit": str(limit),
            "namespace": "0",
        })
        # opensearch returns [query, [titles], [descriptions], [urls]]
        titles = data[1] if len(data) > 1 else []
        descriptions = data[2] if len(data) > 2 else []
        urls = data[3] if len(data) > 3 else []

        results = []
        for i, title in enumerate(titles):
            results.append({
                "title": title,
                "description": descriptions[i] if i < len(descriptions) else "",
                "url": urls[i] if i < len(urls) else "",
            })
        return {"results": results, "count": len(results), "query": query}
    except Exception as e:
        return {"error": str(e)}


def tool_wikipedia_summary(args: dict) -> dict:
    """Get a summary of a specific Wikipedia article."""
    title = args.get("title", "").strip()
    if not title:
        return {"error": "title is required"}

    sentences = min(int(args.get("sentences", 5)), 10)

    try:
        data = _wiki_get({
            "action": "query",
            "titles": title,
            "prop": "extracts",
            "exintro": "1",
            "explaintext": "1",
            "exsentences": str(sentences),
            "redirects": "1",
        })
        pages = data.get("query", {}).get("pages", {})
        for page_id, page in pages.items():
            if page_id == "-1":
                return {"error": f"Article '{title}' not found"}
            extract = page.get("extract", "")
            return {
                "title": page.get("title", title),
                "summary": extract,
                "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(page.get('title', title).replace(' ', '_'))}",
            }
        return {"error": "No pages returned"}
    except Exception as e:
        return {"error": str(e)}


def tool_wolfram_alpha(args: dict) -> dict:
    """
    Ask Wolfram Alpha a computational question.
    Requires WOLFRAM_APP_ID env var. Returns a short text answer.
    """
    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    app_id = os.environ.get("WOLFRAM_APP_ID", "")
    if not app_id:
        return {
            "error": "WOLFRAM_APP_ID not set. Get a free API key at https://developer.wolframalpha.com/",
            "hint": "Add WOLFRAM_APP_ID=your_key to your .env file"
        }

    try:
        params = urllib.parse.urlencode({"appid": app_id, "i": query})
        url = f"{_WOLFRAM_API}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "LibreBird/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            answer = resp.read().decode()
        return {"query": query, "answer": answer}
    except urllib.error.HTTPError as e:
        if e.code == 501:
            return {"query": query, "answer": "Wolfram Alpha could not understand the query."}
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "wikipedia_search",
            "description": "Search Wikipedia for articles matching a query. Returns titles, descriptions, and URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 5, max 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wikipedia_summary",
            "description": "Get a concise summary of a specific Wikipedia article by title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Exact Wikipedia article title"},
                    "sentences": {"type": "integer", "description": "Number of sentences (default 5, max 10)"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wolfram_alpha",
            "description": "Ask Wolfram Alpha a computational, mathematical, or scientific question. Returns a short text answer. Requires WOLFRAM_APP_ID env var.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Question to ask (e.g. 'integral of x^2', 'population of France', 'distance from Earth to Mars')"},
                },
                "required": ["query"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "wikipedia_search": tool_wikipedia_search,
    "wikipedia_summary": tool_wikipedia_summary,
    "wolfram_alpha": tool_wolfram_alpha,
}
