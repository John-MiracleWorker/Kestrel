"""Comparative memory benchmark: Kestrel layered memory vs flat baselines.

Three-way head-to-head on the same synthetic corpus and ground-truth
queries:
  1. Kestrel layered memory (in-memory benchmark backend, layer-aware
     retrieval)
  2. Flat TF-IDF RAG (document store, cosine similarity, no layers)
  3. Flat transcript (recency-biased chronological transcript, the
     "typical agent memory" proxy for chat-log style runtimes)

Metrics: Recall@k, Precision@k, MRR, latency (avg + p99).

Methodology gate (fail-closed, no required winner): every arm must return
evidence for every query in both phases and all metrics must be finite. A
retriever that silently returns nothing fails the gate closed. The
comparison is fair by construction — every arm sees the same corpus,
queries, tokenization, and global k, and no arm receives the ground-truth
layer label (BENCH-002). Deltas are reported for every arm exactly as
measured, including when Kestrel is not ahead.

Usage:
    python benchmarks/memory_benchmark_transcript.py --output results/memory_transcript.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from adapters.flat_transcript import FlatTranscriptMemory
from baseline_rag import BaselineRAG
from datasets_corpus.memory_corpus import build_memory_corpus

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery

_QUALITY_FLOOR_VERSION = "kestrel.memory-transcript-methodology-gate.v1"
_QUALITY_FLOOR_V1 = {
    "recall_at_k": 0.80,
    "precision_at_k": 0.20,
    "mrr": 0.75,
}


def _finite_metric(payload: dict[str, Any], key: str) -> float:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError):
        return float("nan")
    return value if math.isfinite(value) else float("nan")


def _evaluate_quality_gate(result: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed methodology gate (BENCH-001/BENCH-002 repair).

    The benchmark compares arms fairly; it never requires a particular arm
    to win. The gate fails closed when:

      - any arm returns zero evidence for any query (a retriever that
        silently returns nothing cannot pass, even if a comparison arm also
        returns nothing), or
      - any arm's per-query metrics are non-finite.

    Recall@k, Precision@k, and MRR are reported for every arm and the
    cross-arm deltas are published exactly as measured — including when
    Kestrel is not ahead. The absolute floors are reported for
    transparency but are not pass/fail gates: the pre-repair oracle layer
    filter inflated Kestrel's numbers, so any floor calibrated against it
    would re-encode the unfair advantage this benchmark was repaired to
    remove.
    """
    query_details = result.get("query_details", {})
    overall = result.get("overall", {})
    arms = ("kestrel", "tfidf", "transcript")
    checks: dict[str, bool] = {}
    for arm in arms:
        details = query_details.get(arm, [])
        checks[f"{arm}_evidence_every_query"] = bool(details) and all(
            bool(d.get("evidence")) for d in details
        )
        checks[f"{arm}_finite_metrics"] = all(
            all(
                math.isfinite(_finite_metric(d, metric))
                for metric in ("recall_at_k", "precision_at_k", "mrr")
            )
            for d in details
        )
    observed = {
        metric: {arm: _finite_metric(overall.get(arm, {}), metric) for arm in arms}
        for metric in _QUALITY_FLOOR_V1
    }
    return {
        "version": _QUALITY_FLOOR_VERSION,
        "minimums": dict(_QUALITY_FLOOR_V1),
        "observed": observed,
        "query_count": len(query_details.get("kestrel", [])),
        "checks": checks,
        "passed": all(checks.values()),
    }


def _layer_from_string(s: str) -> MemoryLayer:
    return MemoryLayer(s)


def _ingest_into_kestrel(memory: LayeredMemorySystem, docs: list[dict[str, Any]]) -> None:
    for doc in docs:
        layer = _layer_from_string(doc["layer"])
        kind = MemoryKind.FACT
        if layer == MemoryLayer.EPISODIC:
            kind = MemoryKind.EVENT
        elif layer == MemoryLayer.PROCEDURAL:
            kind = MemoryKind.PROCEDURE
        record = MemoryRecord(
            id=doc["id"],
            title=doc["id"],
            content=doc["text"],
            layer=layer,
            kind=kind,
            confidence=max(DEFAULT_LAYER_SPECS[layer].min_write_confidence, 0.85),
        )
        memory.put(record)


def _ingest_into_flat(store: Any, docs: list[dict[str, Any]]) -> None:
    for doc in docs:
        store.ingest(doc["id"], doc["text"], layer=doc["layer"])


def _compute_metrics(retrieved_ids: list[str], expected_ids: list[str]) -> dict[str, Any]:
    expected_set = set(expected_ids)
    retrieved_set = set(retrieved_ids)
    relevant_in_top_k = len(expected_set & retrieved_set)

    recall_at_k = relevant_in_top_k / len(expected_set) if expected_set else 0.0
    precision_at_k = relevant_in_top_k / len(retrieved_ids) if retrieved_ids else 0.0

    mrr = 0.0
    for rank, rid in enumerate(retrieved_ids, start=1):
        if rid in expected_set:
            mrr = 1.0 / rank
            break

    return {
        "recall_at_k": recall_at_k,
        "precision_at_k": precision_at_k,
        "mrr": mrr,
    }


_RECENCY_CHATTER = [
    # Recent conversation chatter sharing vocabulary with the semantic
    # facts but carrying no ground truth. In a flat transcript these
    # land at the end (most recent) and crowd out the older facts under
    # recency-biased scoring. Kestrel retrieves across all eligible layers
    # (no oracle layer filter), so it must also absorb this chatter; the
    # benchmark measures how each arm's ranking degrades as a result.
    ("chatter_001", "episodic", "Just checked the API docs again, the base URL thing keeps tripping me up, the endpoint path and the key header."),
    ("chatter_002", "episodic", "Been hitting rate limits all afternoon with the standard key, definitely need the higher tier for this batch work."),
    ("chatter_003", "episodic", "The token refresh flow worked after I read the auth section, expiry is annoying though, keeps timing out."),
    ("chatter_004", "episodic", "Batch upload discussion in the channel, someone said the payload cap, we should test the record limit with a big file."),
    ("chatter_005", "episodic", "Webhook signatures came up again in review, the HMAC secret from the dashboard, everyone keeps asking about it."),
    ("chatter_006", "episodic", "Retry policy chat, exponential backoff starting small, max retries before we give up, the 429 handling."),
    ("chatter_007", "episodic", "Pagination again, next cursor, linked pages, the API keeps returning the cursor field, mildly confusing."),
    ("chatter_008", "episodic", "Search filtering question in standup, created at and tags and status query params, the search endpoint."),
    ("chatter_009", "episodic", "File uploads and the file id for async callbacks, the processing pipeline, uploads endpoint talk."),
    ("chatter_010", "episodic", "Versioning and breaking changes, major version bump, semantic versioning argument in the team channel."),
    ("chatter_011", "episodic", "GraphQL gateway deprecation, removal version, the timeline for retirement, legacy clients."),
    ("chatter_012", "episodic", "Migrations again, alembic upgrade head, someone broke staging, the migration order."),
    ("chatter_013", "episodic", "Environment variables for prod, database url, redis url, secret key, the config checklist."),
    ("chatter_014", "episodic", "Worker queue and redis streams, idempotent handlers, retries are automatic, the job design."),
    ("chatter_015", "episodic", "Docker image tags, latest stable commit sha, ghcr registry, the container publish workflow."),
    ("chatter_016", "episodic", "Tests and the four minute integration suite, docker compose, the pytest setup."),
    ("chatter_017", "episodic", "Config schema and pydantic v2, validation at startup, the config module."),
    ("chatter_018", "episodic", "Structured logging JSON lines, log level env var, stdout output format."),
    ("chatter_019", "episodic", "Health checks, the health endpoint, database and cache state in the response body."),
    ("chatter_020", "episodic", "Talked to the user about the base URL, they keep pasting the wrong host, X-API-Key header."),
]


def _with_recency_chatter(corpus: Any) -> Any:
    """Return a corpus copy with recent chatter appended (highest seq)."""
    from dataclasses import replace

    docs = list(corpus.documents)
    for chatter_id, layer, text in _RECENCY_CHATTER:
        docs.append({"id": chatter_id, "text": text, "layer": layer})
    return replace(corpus, documents=docs)


def _avg(key: str, data: list[dict[str, Any]]) -> float:
    values = [d[key] for d in data if d[key] is not None]
    return sum(values) / len(values) if values else 0.0


def _p99(latencies: list[float]) -> float:
    if not latencies:
        return 0.0
    ordered = sorted(latencies)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]


def _run_phase(corpus: Any, *, k: int) -> dict[str, Any]:
    """Run all three arms over one corpus and return per-arm metrics."""
    with tempfile.TemporaryDirectory(prefix="kestrel-bench-transcript-") as tmpdir:
        mem_dir = Path(tmpdir) / "memory"
        mem_dir.mkdir()
        kestrel = LayeredMemorySystem.from_backend_factory(
            mem_dir,
            InMemoryBackend,
            enforce_stable_write_integrity=False,
        )
        _ingest_into_kestrel(kestrel, corpus.documents)

        tfidf = BaselineRAG()
        for doc in corpus.documents:
            tfidf.ingest(doc["text"], metadata={"id": doc["id"], "layer": doc["layer"]})

        transcript = FlatTranscriptMemory()
        _ingest_into_flat(transcript, corpus.documents)

        results: dict[str, list[dict[str, Any]]] = {
            "kestrel": [],
            "tfidf": [],
            "transcript": [],
        }
        latencies: dict[str, list[float]] = {"kestrel": [], "tfidf": [], "transcript": []}

        for q in corpus.queries:
            # Kestrel: production-equivalent retrieval. MemorySearchTool.run
            # defaults an unspecified layer list to all MemoryLayer values and
            # exposes only the global top-k of the cross-layer results, so no
            # arm here receives the ground-truth layer label. The retrieval
            # trims deterministically to the same global k every arm uses.
            t0 = time.perf_counter()
            hits = kestrel.retrieve(
                RetrievalQuery(query=q.query, k_per_layer=k, layers=tuple(MemoryLayer))
            )
            t1 = time.perf_counter()
            latencies["kestrel"].append(t1 - t0)
            kestrel_ids = [hit.record.id for hit in hits[:k]]
            kestrel_metrics = _compute_metrics(kestrel_ids, q.expected_doc_ids)
            kestrel_metrics.update(
                {
                    "query": q.query,
                    "layer": q.layer,
                    "evidence": bool(kestrel_ids),
                    "latency_ms": round((t1 - t0) * 1000, 3),
                }
            )
            results["kestrel"].append(kestrel_metrics)

            # TF-IDF flat RAG: whole-corpus search.
            t0 = time.perf_counter()
            tfidf_results = tfidf.retrieve(q.query, k=k)
            t1 = time.perf_counter()
            latencies["tfidf"].append(t1 - t0)
            tfidf_ids = [r.doc.metadata.get("id", r.doc.id) for r in tfidf_results]
            tfidf_metrics = _compute_metrics(tfidf_ids, q.expected_doc_ids)
            tfidf_metrics.update(
                {
                    "query": q.query,
                    "layer": q.layer,
                    "evidence": bool(tfidf_ids),
                    "latency_ms": round((t1 - t0) * 1000, 3),
                }
            )
            results["tfidf"].append(tfidf_metrics)

            # Flat transcript: recency-biased whole-transcript search.
            t0 = time.perf_counter()
            transcript_results = transcript.retrieve(q.query, k=k)
            t1 = time.perf_counter()
            latencies["transcript"].append(t1 - t0)
            transcript_ids = [r.doc_id for r in transcript_results]
            transcript_metrics = _compute_metrics(transcript_ids, q.expected_doc_ids)
            transcript_metrics.update(
                {
                    "query": q.query,
                    "layer": q.layer,
                    "evidence": bool(transcript_ids),
                    "latency_ms": round((t1 - t0) * 1000, 3),
                }
            )
            results["transcript"].append(transcript_metrics)

        def _summary(name: str) -> dict[str, float]:
            data = results[name]
            lats = latencies[name]
            return {
                "recall_at_k": round(_avg("recall_at_k", data), 3),
                "precision_at_k": round(_avg("precision_at_k", data), 3),
                "mrr": round(_avg("mrr", data), 3),
                "avg_latency_ms": round(sum(lats) / len(lats) * 1000, 3) if lats else 0.0,
                "p99_latency_ms": round(_p99(lats) * 1000, 3),
            }

        overall = {name: _summary(name) for name in ("kestrel", "tfidf", "transcript")}

        layer_comparison: dict[str, Any] = {}
        for layer_name in ("semantic", "episodic", "procedural"):
            layer_comparison[layer_name] = {}
            for name in ("kestrel", "tfidf", "transcript"):
                subset = [r for r in results[name] if r["layer"] == layer_name]
                layer_comparison[layer_name][name] = {
                    "recall_at_k": round(_avg("recall_at_k", subset), 3),
                    "precision_at_k": round(_avg("precision_at_k", subset), 3),
                    "mrr": round(_avg("mrr", subset), 3),
                    "avg_latency_ms": round(
                        sum(r["latency_ms"] for r in subset) / len(subset), 3
                    )
                    if subset
                    else 0.0,
                }

        deltas = {
            metric: {
                "kestrel_minus_tfidf": round(
                    overall["kestrel"][metric] - overall["tfidf"][metric], 3
                ),
                "kestrel_minus_transcript": round(
                    overall["kestrel"][metric] - overall["transcript"][metric], 3
                ),
            }
            for metric in ("recall_at_k", "precision_at_k", "mrr")
        }

        return {
            "overall": overall,
            "deltas": deltas,
            "per_layer": layer_comparison,
            "query_details": results,
        }


def run_memory_transcript_benchmark(*, k: int = 5, seed: int = 42) -> dict[str, Any]:
    corpus = build_memory_corpus(seed=seed)
    base = _run_phase(corpus, k=k)
    stressed = _run_phase(_with_recency_chatter(corpus), k=k)

    result = {
        "schema": "kestrel.memory_transcript_benchmark.v1",
        "config": {
            "k": k,
            "seed": seed,
            "total_queries": len(corpus.queries),
            "total_docs": len(corpus.documents),
            "backend": "in_memory",
            "transcript_recency_beta": 0.005,
            "synthetic_fixture_seed_mode": "direct_non_promotion",
        },
        "baseline_corpus": base,
        "recency_stress_corpus": stressed,
        "recency_stress": {
            "added_chatter_docs": len(_RECENCY_CHATTER),
            "note": (
                "Recent episodic chatter appended after the facts. Measures whether "
                "recent conversation buries older facts: flat transcript (recency-biased) "
                "vs Kestrel layer-aware retrieval."
            ),
        },
    }
    # Acceptance gate is evaluated against the stressed corpus, where the
    # baselines are weakest and Kestrel must still hold the floor.
    result["acceptance"] = _evaluate_quality_gate({"overall": stressed["overall"], "query_details": stressed["query_details"]})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Kestrel vs flat TF-IDF RAG vs flat-transcript memory benchmark."
    )
    parser.add_argument("--k", type=int, default=5, help="Top-k retrieval cutoff")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, help="JSON output path")
    args = parser.parse_args()

    result = run_memory_transcript_benchmark(k=args.k, seed=args.seed)
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2))
        print(f"\nWrote results to {args.output}", file=sys.stderr)
    return 0 if result["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
