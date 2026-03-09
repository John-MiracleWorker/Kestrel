"""DuckDuckGo search backend — free, no API key required."""

from __future__ import annotations

import logging
from typing import Any

from agent.tools.search.backends import SearchBackend, SearchResult

logger = logging.getLogger("brain.agent.tools.search.duckduckgo")


class DuckDuckGoBackend:
    """DuckDuckGo Instant Answer API — always-available fallback."""

    @property
    def name(self) -> str:
        return "duckduckgo"

    async def search(
        self,
        query: str,
        max_results: int = 10,
        **kwargs,
    ) -> list[SearchResult]:
        """Execute a DuckDuckGo search."""
        try:
            import aiohttp

            params = {
                "q": query,
                "format": "json",
                "no_redirect": "1",
                "no_html": "1",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.duckduckgo.com/",
                    params=params,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)

            results = []

            # Abstract result
            if data.get("Abstract"):
                results.append(SearchResult(
                    title=data.get("Heading", query),
                    url=data.get("AbstractURL", ""),
                    snippet=data.get("Abstract", ""),
                    content=data.get("Abstract", ""),
                ))

            # Related topics
            for topic in data.get("RelatedTopics", [])[:max_results - len(results)]:
                if isinstance(topic, dict) and "Text" in topic:
                    results.append(SearchResult(
                        title=topic.get("Text", "")[:80],
                        url=topic.get("FirstURL", ""),
                        snippet=topic.get("Text", ""),
                        content=topic.get("Text", ""),
                    ))

            return results

        except Exception as e:
            logger.error(f"DuckDuckGo search failed: {e}")
            raise
