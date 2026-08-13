"""Tests for the memory transcript comparative benchmark."""

from __future__ import annotations

import sys
from pathlib import Path

from pytest import MonkeyPatch

from benchmarks.datasets_corpus.memory_corpus import build_memory_corpus
from benchmarks.memory_benchmark_transcript import (
    _RECENCY_CHATTER,
    _with_recency_chatter,
    main,
    run_memory_transcript_benchmark,
)


def test_memory_transcript_benchmark_passes_quality_and_comparative_gates() -> None:
    report = run_memory_transcript_benchmark()

    assert report["schema"] == "kestrel.memory_transcript_benchmark.v1"
    assert report["acceptance"]["passed"] is True
    assert report["acceptance"]["query_count"] > 0
    assert report["baseline_corpus"]["overall"]["kestrel"]["recall_at_k"] >= 0.80
    assert report["baseline_corpus"]["overall"]["kestrel"]["mrr"] >= 0.75

    # Kestrel must not be below either flat baseline in either phase.
    for phase in ("baseline_corpus", "recency_stress_corpus"):
        overall = report[phase]["overall"]
        kestrel = overall["kestrel"]
        for arm in ("tfidf", "transcript"):
            assert kestrel["recall_at_k"] >= overall[arm]["recall_at_k"]
            assert kestrel["mrr"] >= overall[arm]["mrr"]


def test_recency_stress_degrades_flat_transcript_more_than_kestrel() -> None:
    """Recent chatter must hurt the recency-biased transcript harder than
    Kestrel's layer-aware retrieval (the headline comparison)."""
    report = run_memory_transcript_benchmark()

    base = report["baseline_corpus"]["overall"]
    stressed = report["recency_stress_corpus"]["overall"]

    kestrel_mrr_drop = base["kestrel"]["mrr"] - stressed["kestrel"]["mrr"]
    transcript_mrr_drop = base["transcript"]["mrr"] - stressed["transcript"]["mrr"]

    assert transcript_mrr_drop > 0
    assert kestrel_mrr_drop <= transcript_mrr_drop


def test_recency_chatter_is_appended_and_episodic() -> None:
    corpus = build_memory_corpus(seed=42)
    stressed = _with_recency_chatter(corpus)

    doc_ids = [doc["id"] for doc in stressed.documents]
    assert len(doc_ids) == len(corpus.documents) + len(_RECENCY_CHATTER)
    assert doc_ids[-1].startswith("chatter_")
    # All chatter is episodic (lands outside the semantic layer Kestrel queries).
    chatter = [doc for doc in stressed.documents if doc["id"].startswith("chatter_")]
    assert all(doc["layer"] == "episodic" for doc in chatter)


def test_memory_transcript_cli_writes_machine_readable_report(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    output = tmp_path / "memory-transcript.json"
    monkeypatch.setattr(sys, "argv", ["benchmark", "--output", str(output)])

    result = main()

    assert result == 0
    assert output.exists()
    payload = output.read_text(encoding="utf-8")
    assert '"schema": "kestrel.memory_transcript_benchmark.v1"' in payload
    assert '"recency_stress_corpus"' in payload
