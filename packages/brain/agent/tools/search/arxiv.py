"""Arxiv search backend — academic paper search."""

from __future__ import annotations

import logging
from typing import Any

from agent.tools.search.backends import SearchBackend, SearchResult

logger = logging.getLogger("brain.agent.tools.search.arxiv")


class ArxivBackend:
    """Arxiv paper search — no API key required."""

    @property
    def name(self) -> str:
        return "arxiv"

    async def search(
        self,
        query: str,
        max_results: int = 10,
        **kwargs,
    ) -> list[SearchResult]:
        """Search Arxiv for academic papers."""
        try:
            import arxiv

            client = arxiv.Client()
            search = arxiv.Search(
                query=query,
                max_results=max_results,
                sort_by=arxiv.SortCriterion.Relevance,
            )

            results = []
            for paper in client.results(search):
                results.append(SearchResult(
                    title=paper.title,
                    url=paper.entry_id,
                    snippet=paper.summary[:500],
                    content=paper.summary,
                    metadata={
                        "authors": [a.name for a in paper.authors[:5]],
                        "published": paper.published.isoformat() if paper.published else "",
                        "categories": paper.categories,
                        "pdf_url": paper.pdf_url,
                    },
                ))
            return results

        except ImportError:
            logger.error("arxiv package not installed. Run: pip install arxiv")
            return []
        except Exception as e:
            logger.error(f"Arxiv search failed: {e}")
            raise
