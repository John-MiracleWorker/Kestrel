"""Simple flat TF-IDF RAG baseline for comparative benchmarking.

This is intentionally primitive: no layers, no promotion gates, no context packing,
no trust ordering — just flat document storage with cosine-similarity retrieval.
It represents a typical "naive RAG" implementation.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]{2,}", text.lower())


@dataclass
class Document:
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    doc: Document
    score: float


class SimpleTFIDF:
    """In-memory TF-IDF with cosine similarity. Pure Python, no numpy."""

    def __init__(self) -> None:
        self._docs: list[Document] = []
        self._idf: dict[str, float] = {}
        self._vectors: list[dict[str, float]] = []
        self._doc_count = 0

    def add(self, doc: Document) -> None:
        self._docs.append(doc)
        tokens = _tokenize(doc.text)
        tf = Counter(tokens)
        # Initial pass: store raw term frequencies; IDF updated in batch for simplicity
        self._vectors.append(dict(tf))
        self._doc_count += 1
        # Update IDF incrementally (approximate but fine for benchmarking)
        unique_terms = set(tokens)
        for term in unique_terms:
            self._idf[term] = math.log(self._doc_count / (1 + sum(1 for d in self._docs if term in _tokenize(d.text))))
        # Recompute all vectors with current IDF
        self._recompute_vectors()

    def _recompute_vectors(self) -> None:
        self._vectors = []
        for doc in self._docs:
            tf = Counter(_tokenize(doc.text))
            vec = {term: count * self._idf.get(term, 0.0) for term, count in tf.items()}
            self._vectors.append(vec)

    def search(self, query: str, k: int = 5) -> list[SearchResult]:
        q_tokens = _tokenize(query)
        q_tf = Counter(q_tokens)
        q_vec = {term: count * self._idf.get(term, 0.0) for term, count in q_tf.items()}
        q_norm = math.sqrt(sum(v * v for v in q_vec.values()))
        if q_norm == 0:
            return []

        scores: list[tuple[int, float]] = []
        for idx, vec in enumerate(self._vectors):
            dot = sum(q_vec.get(term, 0.0) * vec.get(term, 0.0) for term in set(q_vec) | set(vec))
            d_norm = math.sqrt(sum(v * v for v in vec.values()))
            if d_norm == 0:
                continue
            scores.append((idx, dot / (q_norm * d_norm)))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [SearchResult(doc=self._docs[idx], score=score) for idx, score in scores[:k]]


class BaselineRAG:
    """Flat RAG with no memory layers, gates, or packing."""

    def __init__(self) -> None:
        self.index = SimpleTFIDF()
        self._id_counter = 0

    def ingest(self, text: str, metadata: dict[str, Any] | None = None) -> str:
        self._id_counter += 1
        doc_id = f"rag_doc_{self._id_counter}"
        self.index.add(Document(id=doc_id, text=text, metadata=metadata or {}))
        return doc_id

    def retrieve(self, query: str, k: int = 5) -> list[SearchResult]:
        return self.index.search(query, k=k)
