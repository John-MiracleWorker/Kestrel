"""Qdrant vector DB adapter."""
from __future__ import annotations

from typing import Any

from .base import OptionalDependencyUnavailable, RetrievalResult


class QdrantAdapter:
    """Qdrant in-memory vector search."""

    BACKEND_NAME = "Qdrant (in-memory)"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", collection: str = "memory") -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, PointStruct, VectorParams
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(model_name)
            dim = model.get_sentence_embedding_dimension()
            client = QdrantClient(":memory:")
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        except ImportError as exc:
            raise OptionalDependencyUnavailable(
                self.BACKEND_NAME,
                missing_dependency=getattr(exc, "name", None) or "qdrant-client",
                install_hint="python -m pip install qdrant-client sentence-transformers",
            ) from exc
        self._point_struct: Any = PointStruct
        self.model = model
        self.dim = dim
        self.client = client
        self.collection = collection
        self._counter = 0

    def name(self) -> str:
        return self.BACKEND_NAME

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        emb = self.model.encode(text, normalize_embeddings=True).tolist()
        self._counter += 1
        self.client.upsert(
            collection_name=self.collection,
            points=[
                self._point_struct(
                    id=self._counter,
                    vector=emb,
                    payload={"doc_id": doc_id, "text": text, "layer": layer},
                )
            ],
        )

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        q_emb = self.model.encode(query, normalize_embeddings=True).tolist()
        filter_ = None
        if layer:
            from qdrant_client.models import FieldCondition, Filter, MatchValue
            filter_ = Filter(must=[FieldCondition(key="layer", match=MatchValue(value=layer))])
        results = self.client.search(collection_name=self.collection, query_vector=q_emb, limit=k, query_filter=filter_)
        return [
            RetrievalResult(
                doc_id=r.payload["doc_id"],
                text=r.payload["text"],
                score=r.score,
                metadata={"layer": r.payload.get("layer")}
            )
            for r in results
        ]
