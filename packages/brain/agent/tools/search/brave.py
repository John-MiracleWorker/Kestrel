"""Brave search backend — privacy-focused web search."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from agent.tools.search.backends import SearchBackend, SearchResult

logger = logging.getLogger("brain.agent.tools.search.brave")


class BraveBackend:
    """Brave Search API."""

    _BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str):
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "brave"

    async def search(
        self,
        query: str,
        max_results: int = 10,
        **kwargs,
    ) -> list[SearchResult]:
        """Execute a Brave search."""
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        params = {
            "q": query,
            "count": min(max_results, 20),
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._BASE_URL,
                    headers=headers,
                    params=params,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            results = []
            for item in data.get("web", {}).get("results", []):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                    content=item.get("description", ""),
                ))
            return results

        except Exception as e:
            logger.error(f"Brave search failed: {e}")
            raise
