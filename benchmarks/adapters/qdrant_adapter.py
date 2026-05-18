"""Qdrant vector DB adapter."""
from __future__ import annotations

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sentence_transformers import SentenceTransformer

from .base import RetrievalResult


class QdrantAdapter:
    """Qdrant in-memory vector search."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", collection: str = "memory") -> None:
        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        self.client = QdrantClient(":memory:")
        self.collection = collection
        self.client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )
        self._counter = 0

    def name(self) -> str:
        return "Qdrant (in-memory)"

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        emb = self.model.encode(text, normalize_embeddings=True).tolist()
        self._counter += 1
        self.client.upsert(
            collection_name=self.collection,
            points=[PointStruct(id=self._counter, vector=emb, payload={"doc_id": doc_id, "text": text, "layer": layer})]
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
