"""Tavily search backend — AI-optimized web search."""

from __future__ import annotations

import logging
from typing import Any

from agent.tools.search.backends import SearchBackend, SearchResult

logger = logging.getLogger("brain.agent.tools.search.tavily")


class TavilyBackend:
    """Tavily AI search — optimized for LLM consumption."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "tavily"

    async def search(
        self,
        query: str,
        max_results: int = 10,
        **kwargs,
    ) -> list[SearchResult]:
        """Execute a Tavily search."""
        try:
            from tavily import AsyncTavilyClient

            client = AsyncTavilyClient(api_key=self._api_key)
            search_depth = kwargs.get("search_depth", "advanced")
            response = await client.search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
                include_answer=True,
            )

            results = []
            for item in response.get("results", []):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", "")[:500],
                    content=item.get("content", ""),
                    score=item.get("score", 0.0),
                ))
            return results

        except ImportError:
            logger.error("tavily-python not installed. Run: pip install tavily-python")
            return []
        except Exception as e:
            logger.error(f"Tavily search failed: {e}")
            raise
