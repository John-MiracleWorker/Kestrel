"""Tests for the memory transcript comparative benchmark."""

from __future__ import annotations

import sys
from pathlib import Path

from pytest import MonkeyPatch

from benchmarks.adapters.flat_transcript import FlatTranscriptMemory
from benchmarks.datasets_corpus.memory_corpus import build_memory_corpus
from benchmarks.memory_benchmark_transcript import (
    _RECENCY_CHATTER,
    _with_recency_chatter,
    main,
    run_memory_transcript_benchmark,
)
from nested_memvid_agent.models import MemoryLayer


def test_flat_transcript_idf_is_non_negative_at_df_total_minus_one() -> None:
    """A term present in N-1 of N documents must still return evidence.

    The pre-repair formula `log(N / (1 + df))` produced exactly 0 when
    df == N-1, and `retrieve()` discarded those entries (`lexical <= 0`),
    so a query containing only such common terms returned no evidence at
    all — biasing the flat baseline independently of recency pressure.
    """
    mem = FlatTranscriptMemory()
    for i in range(4):
        mem.ingest(f"doc_{i}", f"common_topic unique_{i}")

    # "common_topic" appears in all 4 documents; "unique_i" appears once.
    hits = mem.retrieve("common_topic", k=3)

    assert len(hits) == 3
    assert all(hit.score > 0.0 for hit in hits)


def test_flat_transcript_idf_is_positive_at_df_total() -> None:
    """A term present in every document (df == N) must still score above 0.

    The pre-repair formula `log(N / (1 + df))` went negative when
    df == N (a term in every document), so the transcript baseline could
    not return evidence for queries whose terms were ubiquitous.
    """
    mem = FlatTranscriptMemory()
    for i in range(3):
        mem.ingest(f"doc_{i}", f"ubiquitous unique_{i}")

    # "ubiquitous" appears in all 3 documents.
    hits = mem.retrieve("ubiquitous", k=2)

    assert len(hits) == 2
    assert all(hit.score > 0.0 for hit in hits)


def test_flat_transcript_idf_values_are_strictly_positive() -> None:
    """Every computed IDF value is strictly positive for all df in [1, N]."""
    mem = FlatTranscriptMemory()
    texts = [
        "alpha shared_1 unique_a",
        "alpha shared_1 shared_2 unique_b",
        "alpha shared_2 unique_c",
        "beta unique_d",
    ]
    for i, text in enumerate(texts):
        mem.ingest(f"doc_{i}", text)

    idf = mem._compute_idf()

    assert all(value > 0.0 for value in idf.values())
    # alpha appears in 3 of 4 (df == N-1), shared in 2, unique_* once.
    assert idf["alpha"] > 0.0
    assert idf["shared"] > 0.0


def test_memory_transcript_benchmark_passes_methodology_gates() -> None:
    """The acceptance gate is methodological: every arm must return evidence
    for every query and all metrics must be finite. No arm is required to win.

    The pre-repair gate required Kestrel to meet absolute floors and beat
    both flat baselines; those numbers were inflated by the oracle layer
    filter that BENCH-002 removes, so re-encoding them as pass/fail gates
    would recreate the unfair advantage. The gate instead fails closed on
    a retriever that silently returns nothing, in either phase.
    """
    report = run_memory_transcript_benchmark()

    assert report["schema"] == "kestrel.memory_transcript_benchmark.v1"
    assert report["acceptance"]["passed"] is True
    assert report["acceptance"]["query_count"] > 0
    # Honest backend naming: the configured backend is in-memory, not a
    # Memvid .mv2 file backend.
    assert report["config"]["backend"] == "in_memory"

    for phase in ("baseline_corpus", "recency_stress_corpus"):
        for arm in ("kestrel", "tfidf", "transcript"):
            details = report[phase]["query_details"][arm]
            assert details, f"{phase}/{arm}: no query rows"
            assert all(row["evidence"] for row in details), (
                f"{phase}/{arm}: a query returned no evidence"
            )


def test_recency_stress_measurably_degrades_the_flat_transcript() -> None:
    """Recent chatter must measurably degrade the recency-biased transcript.

    The honest (post-BENCH-002) comparison no longer asserts Kestrel beats
    the baselines — that advantage was manufactured by the oracle layer
    filter. What the stress scenario still demonstrates is that a burst of
    recent episodic chatter degrades a flat recency-biased transcript, and
    the benchmark records the deltas for every arm exactly as measured.
    """
    report = run_memory_transcript_benchmark()

    base = report["baseline_corpus"]["overall"]
    stressed = report["recency_stress_corpus"]["overall"]
    deltas = report["recency_stress_corpus"]["deltas"]["mrr"]

    transcript_mrr_drop = base["transcript"]["mrr"] - stressed["transcript"]["mrr"]
    assert transcript_mrr_drop > 0
    # The deltas are recorded honestly for all arms (the kestrel delta may
    # be negative after the oracle is removed — that is reported, not hidden).
    assert "kestrel_minus_transcript" in deltas
    assert "kestrel_minus_tfidf" in deltas


def test_kestrel_arm_queries_all_eligible_layers_with_no_ground_truth_layer() -> None:
    """BENCH-002: the Kestrel arm must not receive the ground-truth layer.

    The retrieval must default to the full eligible layer set (all
    MemoryLayer values) and be trimmed to the same global top-k every arm
    uses — mirroring MemorySearchTool.run, which never receives a per-query
    oracle layer.
    """
    from unittest.mock import patch

    from nested_memvid_agent.models import RetrievalQuery

    captured: list[RetrievalQuery] = []

    from nested_memvid_agent.layers import LayeredMemorySystem

    original_retrieve = LayeredMemorySystem.retrieve

    def spy_retrieve(self, query: RetrievalQuery) -> list:
        captured.append(query)
        return original_retrieve(self, query)

    with patch.object(LayeredMemorySystem, "retrieve", spy_retrieve):
        run_memory_transcript_benchmark()

    assert captured, "no RetrievalQuery captured"
    for query in captured:
        assert query.layers == tuple(
            MemoryLayer
        ), f"oracle layer filter leaked into retrieval: {query.layers}"
        # Every arm uses the same global k budget.
        assert query.k_per_layer == 5


def test_kestrel_global_top_k_is_deterministic() -> None:
    """BENCH-002: identical corpus/query/k must yield identical top-k.

    Kestrel searches all eligible layers and is trimmed deterministically
    to the global top-k, so two runs with the same seed must produce the
    exact same per-query result id sequences.
    """
    first = run_memory_transcript_benchmark()
    second = run_memory_transcript_benchmark()

    for phase in ("baseline_corpus", "recency_stress_corpus"):
        ids_first = [
            (row["query"], row["evidence"], row["recall_at_k"], row["mrr"])
            for row in first[phase]["query_details"]["kestrel"]
        ]
        ids_second = [
            (row["query"], row["evidence"], row["recall_at_k"], row["mrr"])
            for row in second[phase]["query_details"]["kestrel"]
        ]
        assert ids_first == ids_second, f"{phase}: kestrel top-k not deterministic"


def test_recency_chatter_is_appended_and_episodic() -> None:
    corpus = build_memory_corpus(seed=42)
    stressed = _with_recency_chatter(corpus)

    doc_ids = [doc["id"] for doc in stressed.documents]
    assert len(doc_ids) == len(corpus.documents) + len(_RECENCY_CHATTER)
    assert doc_ids[-1].startswith("chatter_")
    # All chatter is episodic (it never carries the semantic ground truth).
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
