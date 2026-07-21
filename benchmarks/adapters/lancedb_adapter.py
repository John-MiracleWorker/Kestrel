"""LanceDB adapter."""
from __future__ import annotations

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

from .base import RetrievalResult


class LanceDBAdapter:
    """LanceDB in-memory vector search."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", table_name: str = "memory") -> None:
        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        self.db = lancedb.connect("/tmp/kestrel-lancedb-bench")
        self.table_name = table_name
        self._data = []
        self._table = None

    def name(self) -> str:
        return "LanceDB"

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        emb = self.model.encode(text, normalize_embeddings=True).tolist()
        self._data.append({"doc_id": doc_id, "text": text, "layer": layer, "vector": emb})

    def _ensure_table(self) -> None:
        if self._table is None and self._data:
            schema = pa.schema([
                ("doc_id", pa.string()),
                ("text", pa.string()),
                ("layer", pa.string()),
                ("vector", pa.list_(pa.float32(), self.dim)),
            ])
            self._table = self.db.create_table(self.table_name, data=self._data, schema=schema, mode="overwrite")
            self._table.create_index(metric="cosine", vector_column_name="vector")

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        self._ensure_table()
        if self._table is None:
            return []
        q_emb = self.model.encode(query, normalize_embeddings=True).tolist()
        results = self._table.search(q_emb).metric("cosine").limit(k)
        if layer:
            results = results.where(f'layer = "{layer}"')
        results = results.to_list()
        return [
            RetrievalResult(
                doc_id=r["doc_id"],
                text=r["text"],
                score=1.0 - r.get("_distance", 0),  # lancedb returns distance, convert to similarity
                metadata={"layer": r.get("layer")}
            )
            for r in results
        ]
