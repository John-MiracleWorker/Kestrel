"""Jina crawler backend — web page content extraction."""

from __future__ import annotations

import logging

import aiohttp

from agent.tools.search.backends import SearchBackend, SearchResult

logger = logging.getLogger("brain.agent.tools.search.jina_crawler")


class JinaCrawlerBackend:
    """Jina Reader API — extracts clean content from web pages."""

    _BASE_URL = "https://r.jina.ai"

    def __init__(self, api_key: str):
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "jina"

    async def search(
        self,
        query: str,
        max_results: int = 5,
        **kwargs,
    ) -> list[SearchResult]:
        """Crawl a URL via Jina and extract clean content.

        If query looks like a URL, crawl it directly.
        Otherwise, use Jina's search endpoint.
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

        try:
            if query.startswith(("http://", "https://")):
                # Direct URL crawling
                url = f"{self._BASE_URL}/{query}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                return [SearchResult(
                    title=data.get("title", ""),
                    url=query,
                    snippet=data.get("content", "")[:500],
                    content=data.get("content", ""),
                )]
            else:
                # Search mode
                search_url = f"https://s.jina.ai/{query}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(search_url, headers=headers) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                results = []
                for item in data.get("results", [])[:max_results]:
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("content", "")[:500],
                        content=item.get("content", ""),
                    ))
                return results

        except Exception as e:
            logger.error(f"Jina crawl failed: {e}")
            raise
