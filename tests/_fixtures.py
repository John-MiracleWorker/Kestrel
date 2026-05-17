from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.models import MemoryHit, MemoryLayer, MemoryRecord


class StubEmbedder:
    """Tiny deterministic synonym embedder for semantic-recall tests."""

    synonyms = {
        "401": "auth",
        "403": "auth",
        "credential": "auth",
        "credentials": "auth",
        "token": "auth",
        "unauthorized": "auth",
        "expired": "stale",
        "rejected": "denied",
        "renew": "refresh",
        "retrying": "retry",
        "rerun": "retry",
    }

    def concepts(self, text: str) -> set[str]:
        tokens = []
        for raw in text.lower().replace(".", " ").replace(":", " ").split():
            token = raw.strip()
            tokens.append(self.synonyms.get(token, token))
        return {token for token in tokens if token}

    def similarity(self, left: str, right: str) -> float:
        left_concepts = self.concepts(left)
        right_concepts = self.concepts(right)
        if not left_concepts or not right_concepts:
            return 0.0
        return len(left_concepts & right_concepts) / len(left_concepts | right_concepts)


class SemanticInMemoryBackend(InMemoryBackend):
    """In-memory backend that honors hybrid mode with deterministic synonym scores."""

    embedder = StubEmbedder()

    def __init__(self, path: Path, layer: MemoryLayer, **kwargs: object) -> None:
        super().__init__(path, layer, **kwargs)

    def find(
        self,
        query: str,
        k: int = 8,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        lexical = super().find(query, k=k, mode=mode, min_relevancy=min_relevancy, include_inactive=include_inactive)
        if mode != "hybrid":
            return lexical
        by_id = {hit.record.id: hit for hit in lexical}
        for record in self.iter_records(include_inactive=include_inactive):
            if record.id in by_id:
                continue
            score = self.embedder.similarity(query, _record_text(record))
            if score < max(min_relevancy, 0.12):
                continue
            by_id[record.id] = MemoryHit(
                record=record,
                score=score,
                source_backend="semantic-memory",
                frame_id=record.id,
                snippet=record.content[:220],
            )
        return sorted(by_id.values(), key=lambda hit: hit.score, reverse=True)[:k]


def _record_text(record: MemoryRecord) -> str:
    return f"{record.title} {record.content} {' '.join(record.tags.values())}"
