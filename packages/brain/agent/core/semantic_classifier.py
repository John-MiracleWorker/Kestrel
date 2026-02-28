"""
Semantic Complexity Classifier — embedding-based text classification for
routing proposals and steps to the right category.

Falls back to keyword matching when embeddings are unavailable, ensuring
the system works without a vector store while benefiting from one when present.
"""

import logging
import math
from typing import Optional

logger = logging.getLogger("brain.agent.core.semantic_classifier")


# Pre-defined category exemplar texts. Each category has representative
# descriptions that anchor the embedding space.
CATEGORY_EXEMPLARS: dict[str, list[str]] = {
    "security": [
        "validate authentication tokens and verify signatures",
        "check for SQL injection and XSS vulnerabilities",
        "enforce access control and RBAC permissions",
        "encrypt sensitive user data at rest and in transit",
        "audit credential storage and secret management",
        "review OAuth flow and token refresh handling",
    ],
    "architecture": [
        "refactor module dependencies and reduce coupling",
        "design service communication patterns and APIs",
        "implement clean separation of concerns between layers",
        "plan database schema migration strategy",
        "evaluate microservice vs monolith trade-offs",
        "redesign component boundaries for scalability",
    ],
    "performance": [
        "optimize database query performance and reduce latency",
        "implement caching layer for frequently accessed data",
        "profile memory usage and fix resource leaks",
        "batch API requests to reduce network overhead",
        "add connection pooling and request throttling",
        "resolve N+1 query patterns in ORM usage",
    ],
    "ux": [
        "improve user onboarding flow and reduce friction",
        "redesign navigation for better discoverability",
        "add accessibility features and screen reader support",
        "simplify error messages for non-technical users",
        "implement responsive design for mobile devices",
        "reduce page load time for better user experience",
    ],
}


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticClassifier:
    """Optional embedding-based text classifier.

    Uses pre-computed category exemplar embeddings to classify text into
    the best-matching category via cosine similarity. Falls back gracefully
    when embeddings are unavailable (returns empty string to signal caller
    should use keyword fallback).
    """

    def __init__(self, vector_store=None):
        self._vector_store = vector_store
        self._exemplar_embeddings: dict[str, list[list[float]]] = {}
        self._initialized = False

    async def _ensure_initialized(self) -> bool:
        """Lazily compute exemplar embeddings on first use."""
        if self._initialized:
            return bool(self._exemplar_embeddings)

        self._initialized = True
        if not self._vector_store or not hasattr(self._vector_store, "embed"):
            return False

        try:
            for category, exemplars in CATEGORY_EXEMPLARS.items():
                embeddings = []
                for text in exemplars:
                    emb = await self._vector_store.embed(text)
                    if emb:
                        embeddings.append(emb)
                if embeddings:
                    self._exemplar_embeddings[category] = embeddings
            logger.info(
                f"Semantic classifier initialized with {len(self._exemplar_embeddings)} categories"
            )
            return bool(self._exemplar_embeddings)
        except Exception as e:
            logger.debug(f"Semantic classifier initialization failed: {e}")
            return False

    async def classify(self, text: str, min_confidence: float = 0.4) -> str:
        """Classify text into the best-matching category.

        Returns the category name if confidence exceeds min_confidence,
        or empty string to signal the caller should use keyword fallback.
        """
        if not await self._ensure_initialized():
            return ""

        try:
            query_embedding = await self._vector_store.embed(text)
            if not query_embedding:
                return ""

            best_category = ""
            best_score = -1.0

            for category, exemplar_embeddings in self._exemplar_embeddings.items():
                # Average similarity across all exemplars for this category
                scores = [
                    _cosine_similarity(query_embedding, emb)
                    for emb in exemplar_embeddings
                ]
                avg_score = sum(scores) / len(scores) if scores else 0.0

                if avg_score > best_score:
                    best_score = avg_score
                    best_category = category

            if best_score >= min_confidence:
                logger.debug(
                    f"Semantic classification: '{text[:60]}...' → {best_category} "
                    f"(score={best_score:.3f})"
                )
                return best_category

            return ""

        except Exception as e:
            logger.debug(f"Semantic classification failed: {e}")
            return ""
