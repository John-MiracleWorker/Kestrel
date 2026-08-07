"""Flat-transcript memory baseline: the "typical agent memory" proxy.

Models how mainstream agent runtimes (chat-log / persistent-transcript
style memory such as Hermes or OpenClaw) actually remember: every
observation is appended to ONE chronological transcript, retrieval is
keyword overlap plus a recency bonus, and there is no layering, no
promotion, no trust ordering, no deduplication, and no conflict
handling. A query searches the whole transcript and recent entries win
ties.

This baseline intentionally has none of Kestrel's architectural
properties so the benchmark isolates what layered memory adds:
  - no per-layer retrieval (semantic facts compete with episodic noise)
  - no promotion or evidence gates (old but important facts decay)
  - no trust ordering (retrieved text is not classified by authority)
  - recency bias (the transcript's last entries dominate scoring)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .base import RetrievalResult


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]{2,}", text.lower())


@dataclass
class _TranscriptEntry:
    doc_id: str
    text: str
    layer: str | None
    seq: int  # monotonically increasing insertion order


class FlatTranscriptMemory:
    """Single flat transcript with lexical + recency retrieval."""

    BACKEND_NAME = "Flat Transcript Baseline (recency-biased)"

    def __init__(self, recency_beta: float = 0.005) -> None:
        """recency_beta controls how strongly recent entries dominate.

        score = lexical_score + recency_beta * seq. Higher beta models a
        runtime whose context is dominated by the latest transcript
        turns (short-context agents); lower beta models one that
        keyword-searches the whole history with mild recency preference.
        """
        self._entries: list[_TranscriptEntry] = []
        self._seq = 0
        self._recency_beta = recency_beta

    def name(self) -> str:
        return self.BACKEND_NAME

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        self._entries.append(
            _TranscriptEntry(doc_id=doc_id, text=text, layer=layer, seq=self._seq)
        )
        self._seq += 1

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        q_tokens = set(_tokenize(query))
        if not q_tokens:
            return []
        idf = self._compute_idf()
        scored: list[tuple[float, int]] = []
        for entry in self._entries:
            e_tokens = _tokenize(entry.text)
            if not e_tokens:
                continue
            overlap = q_tokens & set(e_tokens)
            lexical = sum(idf.get(term, 0.0) for term in overlap)
            if lexical <= 0.0:
                continue
            recency = self._recency_beta * entry.seq
            scored.append((lexical + recency, entry.seq))
        scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
        results: list[RetrievalResult] = []
        for score, seq in scored[:k]:
            entry = self._entries[seq]
            results.append(
                RetrievalResult(
                    doc_id=entry.doc_id,
                    text=entry.text,
                    score=round(score, 6),
                    metadata={"layer": entry.layer},
                )
            )
        return results

    def _compute_idf(self) -> dict[str, float]:
        total = len(self._entries)
        if total == 0:
            return {}
        doc_freq: dict[str, int] = {}
        for entry in self._entries:
            for term in set(_tokenize(entry.text)):
                doc_freq[term] = doc_freq.get(term, 0) + 1
        return {
            term: math.log(total / (1 + freq))
            for term, freq in doc_freq.items()
        }
