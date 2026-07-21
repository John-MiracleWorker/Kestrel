"""Dense vector RAG baseline using sentence-transformers + cosine similarity."""
from __future__ import annotations

from typing import Any

from .base import OptionalDependencyUnavailable, RetrievalResult


class VectorRAG:
    """Flat dense vector RAG with all-MiniLM-L6-v2 embeddings."""

    BACKEND_NAME = "VectorRAG (sentence-transformers)"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        try:
            import numpy as np
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(model_name)
        except ImportError as exc:
            raise OptionalDependencyUnavailable(
                self.BACKEND_NAME,
                missing_dependency=getattr(exc, "name", None) or "sentence-transformers",
                install_hint="python -m pip install numpy sentence-transformers",
            ) from exc
        self._np = np
        self.model = model
        self.docs: list[dict[str, str | None]] = []
        self.embeddings: list[Any] = []

    def name(self) -> str:
        return f"VectorRAG ({self.model.get_sentence_embedding_dimension()}d)"

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        emb = self.model.encode(text, normalize_embeddings=True)
        self.docs.append({"id": doc_id, "text": text, "layer": layer})
        self.embeddings.append(emb)

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        if not self.embeddings:
            return []
        q_emb = self.model.encode(query, normalize_embeddings=True)
        embeddings = self._np.array(self.embeddings)
        scores = embeddings @ q_emb  # cosine similarity (already normalized)
        top_k = self._np.argsort(scores)[::-1][:k]
        results = []
        for idx in top_k:
            doc = self.docs[idx]
            if layer and doc.get("layer") != layer:
                continue
            results.append(RetrievalResult(
                doc_id=doc["id"],
                text=doc["text"],
                score=float(scores[idx]),
                metadata={"layer": doc.get("layer")}
            ))
        return results[:k]
