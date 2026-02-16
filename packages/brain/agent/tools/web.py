"""
Web tools — search the web and browse URLs.

Provides web_search (via SearXNG/DuckDuckGo API) and web_browse (URL fetching
with HTML→text conversion) capabilities.
"""

import json
import logging
import re
from typing import Optional

import httpx

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.web")


def register_web_tools(registry) -> None:
    """Register web search and browse tools."""

    registry.register(
        definition=ToolDefinition(
            name="web_search",
            description=(
                "Search the web for information. Returns a list of relevant "
                "results with titles, URLs, and snippets. Use for research, "
                "fact-checking, and finding current information."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (max 10)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=15,
            category="web",
        ),
        handler=web_search,
    )

    registry.register(
        definition=ToolDefinition(
            name="web_browse",
            description=(
                "Fetch and read the content of a web page. Returns the text "
                "content extracted from the HTML. Use for reading articles, "
                "documentation, or any web content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch and read",
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 5000)",
                        "default": 5000,
                    },
                },
                "required": ["url"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=30,
            category="web",
        ),
        handler=web_browse,
    )


async def web_search(query: str, num_results: int = 5) -> dict:
    """
    Search the web using DuckDuckGo Lite (no API key required).
    Falls back to a simple URL-based approach if the primary method fails.
    """
    num_results = min(num_results, 10)

    try:
        # Use DuckDuckGo HTML search (no API key needed)
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Kestrel/1.0)",
                },
            )
            resp.raise_for_status()

            results = _parse_ddg_html(resp.text, num_results)

            if results:
                return {
                    "query": query,
                    "results": results,
                    "count": len(results),
                }

        # Fallback: no results parsed
        return {
            "query": query,
            "results": [],
            "count": 0,
            "note": "No results found. Try a different query.",
        }

    except Exception as e:
        logger.error(f"Web search error: {e}")
        return {
            "query": query,
            "results": [],
            "error": str(e),
        }


def _parse_ddg_html(html: str, max_results: int) -> list[dict]:
    """Parse DuckDuckGo HTML results page."""
    results = []

    # Find result blocks: <a class="result__a" href="...">title</a>
    # and <a class="result__snippet">snippet</a>
    link_pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (url, title) in enumerate(links[:max_results]):
        # Clean HTML tags from title and snippet
        clean_title = re.sub(r"<[^>]+>", "", title).strip()
        clean_snippet = ""
        if i < len(snippets):
            clean_snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()

        # DuckDuckGo wraps URLs in a redirect
        if "uddg=" in url:
            from urllib.parse import unquote, urlparse, parse_qs
            parsed = parse_qs(urlparse(url).query)
            url = unquote(parsed.get("uddg", [url])[0])

        results.append({
            "title": clean_title,
            "url": url,
            "snippet": clean_snippet,
        })

    return results


async def web_browse(url: str, max_length: int = 5000) -> dict:
    """
    Fetch a web page and extract its text content.
    Strips HTML tags and returns clean text.
    """
    max_length = min(max_length, 20_000)

    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=5),
        ) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Kestrel/1.0)",
                    "Accept": "text/html,application/xhtml+xml,text/plain",
                },
            )
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")

            if "text/html" in content_type or "xhtml" in content_type:
                text = _html_to_text(resp.text)
            elif "text/" in content_type or "json" in content_type:
                text = resp.text
            else:
                return {
                    "url": url,
                    "error": f"Unsupported content type: {content_type}",
                }

            # Truncate
            if len(text) > max_length:
                text = text[:max_length] + f"\n\n... (truncated, {len(resp.text)} total)"

            return {
                "url": url,
                "title": _extract_title(resp.text) if "html" in content_type else "",
                "content": text,
                "length": len(text),
            }

    except httpx.HTTPStatusError as e:
        return {"url": url, "error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {"url": url, "error": str(e)}


def _html_to_text(html: str) -> str:
    """Simple HTML to text conversion without external dependencies."""
    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Convert block elements to newlines
    text = re.sub(r"<(?:br|p|div|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)

    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # Decode HTML entities
    import html as html_module
    text = html_module.unescape(text)

    # Clean up whitespace
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)

    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _extract_title(html: str) -> str:
    """Extract the <title> from HTML."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if match:
        import html as html_module
        return html_module.unescape(match.group(1).strip())
    return ""
