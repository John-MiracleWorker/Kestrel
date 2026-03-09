"""
Search backend abstraction — unified interface for multiple search providers.

Inspired by DeerFlow 2.0's multi-backend search architecture.
Supports Tavily, Brave, DuckDuckGo, Arxiv, and Jina web crawling.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("brain.agent.tools.search.backends")


@dataclass
class SearchResult:
    """Normalized search result across all backends."""
    title: str
    url: str
    snippet: str
    content: str = ""           # Full page content (if crawled)
    source: str = ""            # Backend that produced this result
    score: float = 0.0          # Relevance score (0-1)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SearchBackend(Protocol):
    """Protocol for search backends."""

    @property
    def name(self) -> str: ...

    async def search(
        self,
        query: str,
        max_results: int = 10,
        **kwargs,
    ) -> list[SearchResult]: ...


class SearchRouter:
    """Routes queries to appropriate search backends based on content type.

    Query routing rules:
    - Academic/paper queries → Arxiv
    - Deep content queries → Jina crawler
    - General queries → configured default (Tavily, Brave, or DuckDuckGo)
    - Fallback chain on failure
    """

    def __init__(self):
        self._backends: dict[str, SearchBackend] = {}
        self._default = os.getenv("SEARCH_API", "tavily")
        self._init_backends()

    def _init_backends(self):
        """Initialize available backends based on env config."""
        # Tavily
        tavily_key = os.getenv("TAVILY_API_KEY")
        if tavily_key:
            from agent.tools.search.tavily import TavilyBackend
            self._backends["tavily"] = TavilyBackend(api_key=tavily_key)

        # Brave
        brave_key = os.getenv("BRAVE_API_KEY")
        if brave_key:
            from agent.tools.search.brave import BraveBackend
            self._backends["brave"] = BraveBackend(api_key=brave_key)

        # Arxiv (no API key needed)
        from agent.tools.search.arxiv import ArxivBackend
        self._backends["arxiv"] = ArxivBackend()

        # Jina crawler
        jina_key = os.getenv("JINA_API_KEY")
        if jina_key:
            from agent.tools.search.jina_crawler import JinaCrawlerBackend
            self._backends["jina"] = JinaCrawlerBackend(api_key=jina_key)

        # DuckDuckGo (no API key, always available as fallback)
        from agent.tools.search.duckduckgo import DuckDuckGoBackend
        self._backends["duckduckgo"] = DuckDuckGoBackend()

        logger.info(f"Search backends initialized: {list(self._backends.keys())}")

    def route(self, query: str) -> SearchBackend:
        """Select the best backend for a query."""
        q = query.lower()

        # Academic queries → Arxiv
        if any(kw in q for kw in ("arxiv", "paper", "research paper", "journal", "citation")):
            if "arxiv" in self._backends:
                return self._backends["arxiv"]

        # Use configured default
        if self._default in self._backends:
            return self._backends[self._default]

        # Fallback to first available
        if self._backends:
            return next(iter(self._backends.values()))

        raise RuntimeError("No search backends available. Set TAVILY_API_KEY or BRAVE_API_KEY.")

    async def search(
        self,
        query: str,
        max_results: int = 10,
        backend: Optional[str] = None,
    ) -> list[SearchResult]:
        """Search with automatic routing and fallback."""
        if backend and backend in self._backends:
            target = self._backends[backend]
        else:
            target = self.route(query)

        try:
            results = await target.search(query, max_results=max_results)
            for r in results:
                r.source = target.name
            return results
        except Exception as e:
            logger.warning(f"Search failed on {target.name}: {e}")
            # Try fallback backends
            for name, b in self._backends.items():
                if b is not target:
                    try:
                        results = await b.search(query, max_results=max_results)
                        for r in results:
                            r.source = name
                        return results
                    except Exception:
                        continue
            logger.error("All search backends failed")
            return []

    def available_backends(self) -> list[str]:
        """List available backend names."""
        return list(self._backends.keys())
