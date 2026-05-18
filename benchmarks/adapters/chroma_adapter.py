"""ChromaDB adapter."""
from __future__ import annotations

import chromadb
from sentence_transformers import SentenceTransformer

from .base import RetrievalResult


class ChromaAdapter:
    """ChromaDB in-memory vector search."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", collection: str = "memory") -> None:
        self.model = SentenceTransformer(model_name)
        self.client = chromadb.Client()
        self.collection = self.client.create_collection(name=collection, metadata={"hnsw:space": "cosine"})
        self._batch_ids = []
        self._batch_texts = []
        self._batch_layers = []
        self._batch_embs = []

    def name(self) -> str:
        return "ChromaDB (in-memory)"

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        emb = self.model.encode(text, normalize_embeddings=True).tolist()
        self._batch_ids.append(doc_id)
        self._batch_texts.append(text)
        self._batch_layers.append(layer or "")
        self._batch_embs.append(emb)
        # Batch insert every 100 docs
        if len(self._batch_ids) >= 100:
            self._flush()

    def _flush(self) -> None:
        if not self._batch_ids:
            return
        self.collection.add(
            ids=self._batch_ids,
            documents=self._batch_texts,
            embeddings=self._batch_embs,
            metadatas=[{"layer": l} for l in self._batch_layers]
        )
        self._batch_ids = []
        self._batch_texts = []
        self._batch_layers = []
        self._batch_embs = []

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        self._flush()
        q_emb = self.model.encode(query, normalize_embeddings=True).tolist()
        where = None
        if layer:
            where = {"layer": layer}
        results = self.collection.query(query_embeddings=[q_emb], n_results=k, where=where)
        out = []
        for i in range(len(results["ids"][0])):
            out.append(RetrievalResult(
                doc_id=results["ids"][0][i],
                text=results["documents"][0][i] or "",
                score=results["distances"][0][i] if results["distances"] else 0.0,
                metadata={"layer": results["metadatas"][0][i].get("layer") if results["metadatas"][0][i] else None}
            ))
        return out
