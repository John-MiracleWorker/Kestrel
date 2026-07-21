"""Large-scale memory retrieval benchmark: Kestrel layered vs flat TF-IDF RAG.

Usage:
    python benchmarks/memory_benchmark_large.py --output results/memory_benchmark_large.json
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

if __package__:
    from .baseline_rag import BaselineRAG
    from .datasets_corpus.memory_corpus_large import build_large_memory_corpus
else:
    from baseline_rag import BaselineRAG
    from datasets_corpus.memory_corpus_large import build_large_memory_corpus

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery

_QUALITY_FLOOR_VERSION = "kestrel.large-memory-quality-floor.v1"
_QUALITY_FLOOR_V1 = {
    "recall_at_k": 0.30,
    "precision_at_k": 0.06,
    "mrr": 0.15,
}


def _finite_metric(payload: dict[str, Any], key: str) -> float:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError):
        return float("nan")
    return value if math.isfinite(value) else float("nan")


def _evaluate_quality_gate(result: dict[str, Any]) -> dict[str, Any]:
    overall = result.get("overall", {})
    kestrel = overall.get("kestrel", {})
    baseline = overall.get("baseline", {})
    query_details = result.get("query_details", {}).get("kestrel", [])
    observed = {metric: _finite_metric(kestrel, metric) for metric in _QUALITY_FLOOR_V1}
    checks = {
        "nonempty_query_evidence": bool(query_details),
        **{
            f"kestrel_{metric}_at_or_above_floor": (
                math.isfinite(observed[metric]) and observed[metric] >= minimum
            )
            for metric, minimum in _QUALITY_FLOOR_V1.items()
        },
        "kestrel_recall_not_below_baseline": (
            _finite_metric(kestrel, "recall_at_k") >= _finite_metric(baseline, "recall_at_k")
        ),
        "kestrel_precision_not_below_baseline": (
            _finite_metric(kestrel, "precision_at_k") >= _finite_metric(baseline, "precision_at_k")
        ),
        "kestrel_mrr_not_below_baseline": (
            _finite_metric(kestrel, "mrr") >= _finite_metric(baseline, "mrr")
        ),
    }
    return {
        "version": _QUALITY_FLOOR_VERSION,
        "minimums": dict(_QUALITY_FLOOR_V1),
        "observed": observed,
        "query_count": len(query_details),
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


def _ingest_into_baseline(rag: BaselineRAG, docs: list[dict[str, Any]]) -> None:
    for doc in docs:
        rag.ingest(doc["text"], metadata={"id": doc["id"], "layer": doc["layer"]})


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


def run_memory_benchmark(*, k: int = 5, seed: int = 42) -> dict[str, Any]:
    corpus = build_large_memory_corpus(seed=seed)

    print(f"Corpus: {len(corpus.documents)} docs, {len(corpus.queries)} queries", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="kestrel-bench-") as tmpdir:
        mem_dir = Path(tmpdir) / "memory"
        mem_dir.mkdir()

        # --- Setup Kestrel ---
        print("Ingesting into Kestrel...", file=sys.stderr)
        t0 = time.perf_counter()
        # Synthetic retrieval fixtures are direct layer seeds, not learned
        # promotions. Keep that benchmark-only exception explicit and visible
        # in the emitted configuration rather than forging validation evidence.
        kestrel = LayeredMemorySystem.from_backend_factory(
            mem_dir,
            InMemoryBackend,
            enforce_stable_write_integrity=False,
        )
        _ingest_into_kestrel(kestrel, corpus.documents)
        kestrel_ingest_time = time.perf_counter() - t0
        print(f"  Kestrel ingest: {kestrel_ingest_time:.2f}s", file=sys.stderr)

        # --- Setup Baseline ---
        print("Ingesting into Baseline...", file=sys.stderr)
        t0 = time.perf_counter()
        baseline = BaselineRAG()
        _ingest_into_baseline(baseline, corpus.documents)
        baseline_ingest_time = time.perf_counter() - t0
        print(f"  Baseline ingest: {baseline_ingest_time:.2f}s", file=sys.stderr)

        kestrel_results = []
        baseline_results = []
        kestrel_latencies = []
        baseline_latencies = []

        for i, q in enumerate(corpus.queries):
            if i % 20 == 0:
                print(f"  Query {i}/{len(corpus.queries)}...", file=sys.stderr)

            layer = _layer_from_string(q.layer)
            t0 = time.perf_counter()
            hits = kestrel.retrieve(RetrievalQuery(query=q.query, k_per_layer=k, layers=(layer,)))
            t1 = time.perf_counter()
            kestrel_latencies.append(t1 - t0)
            kestrel_ids = [hit.record.id for hit in hits]
            kestrel_metrics = _compute_metrics(kestrel_ids, q.expected_doc_ids)
            kestrel_metrics["query"] = q.query
            kestrel_metrics["layer"] = q.layer
            kestrel_metrics["latency_ms"] = round((t1 - t0) * 1000, 3)
            kestrel_metrics["retrieved_ids"] = kestrel_ids
            kestrel_results.append(kestrel_metrics)

            t0 = time.perf_counter()
            results = baseline.retrieve(q.query, k=k)
            t1 = time.perf_counter()
            baseline_latencies.append(t1 - t0)
            baseline_ids = [r.doc.metadata.get("id", r.doc.id) for r in results]
            baseline_metrics = _compute_metrics(baseline_ids, q.expected_doc_ids)
            baseline_metrics["query"] = q.query
            baseline_metrics["layer"] = q.layer
            baseline_metrics["latency_ms"] = round((t1 - t0) * 1000, 3)
            baseline_metrics["retrieved_ids"] = baseline_ids
            baseline_results.append(baseline_metrics)

        def _avg(key: str, data: list[dict[str, Any]]) -> float:
            values = [d[key] for d in data if d[key] is not None]
            return sum(values) / len(values) if values else 0.0

        layers = ["semantic", "episodic", "procedural"]
        layer_comparison = {}
        for layer in layers:
            k_layer = [r for r in kestrel_results if r["layer"] == layer]
            b_layer = [r for r in baseline_results if r["layer"] == layer]
            layer_comparison[layer] = {
                "kestrel": {
                    "recall_at_k": round(_avg("recall_at_k", k_layer), 3),
                    "precision_at_k": round(_avg("precision_at_k", k_layer), 3),
                    "mrr": round(_avg("mrr", k_layer), 3),
                    "avg_latency_ms": round(sum(r["latency_ms"] for r in k_layer) / len(k_layer), 3)
                    if k_layer
                    else 0,
                    "p99_latency_ms": round(
                        sorted(r["latency_ms"] for r in k_layer)[int(len(k_layer) * 0.99)]
                        if k_layer
                        else 0,
                        3,
                    ),
                },
                "baseline": {
                    "recall_at_k": round(_avg("recall_at_k", b_layer), 3),
                    "precision_at_k": round(_avg("precision_at_k", b_layer), 3),
                    "mrr": round(_avg("mrr", b_layer), 3),
                    "avg_latency_ms": round(sum(r["latency_ms"] for r in b_layer) / len(b_layer), 3)
                    if b_layer
                    else 0,
                    "p99_latency_ms": round(
                        sorted(r["latency_ms"] for r in b_layer)[int(len(b_layer) * 0.99)]
                        if b_layer
                        else 0,
                        3,
                    ),
                },
            }

        overall = {
            "kestrel": {
                "recall_at_k": round(_avg("recall_at_k", kestrel_results), 3),
                "precision_at_k": round(_avg("precision_at_k", kestrel_results), 3),
                "mrr": round(_avg("mrr", kestrel_results), 3),
                "avg_latency_ms": round(sum(kestrel_latencies) / len(kestrel_latencies) * 1000, 3),
                "p99_latency_ms": round(
                    sorted(kestrel_latencies)[int(len(kestrel_latencies) * 0.99)] * 1000, 3
                )
                if kestrel_latencies
                else 0,
                "ingest_time_s": round(kestrel_ingest_time, 3),
            },
            "baseline": {
                "recall_at_k": round(_avg("recall_at_k", baseline_results), 3),
                "precision_at_k": round(_avg("precision_at_k", baseline_results), 3),
                "mrr": round(_avg("mrr", baseline_results), 3),
                "avg_latency_ms": round(
                    sum(baseline_latencies) / len(baseline_latencies) * 1000, 3
                ),
                "p99_latency_ms": round(
                    sorted(baseline_latencies)[int(len(baseline_latencies) * 0.99)] * 1000, 3
                )
                if baseline_latencies
                else 0,
                "ingest_time_s": round(baseline_ingest_time, 3),
            },
        }

        deltas = {}
        for metric in ["recall_at_k", "precision_at_k", "mrr"]:
            deltas[metric] = round(overall["kestrel"][metric] - overall["baseline"][metric], 3)

        speedup = (
            round(overall["baseline"]["avg_latency_ms"] / overall["kestrel"]["avg_latency_ms"], 2)
            if overall["kestrel"]["avg_latency_ms"] > 0
            else 0
        )

        result = {
            "schema": "kestrel.memory_benchmark_large.v1",
            "config": {
                "k": k,
                "seed": seed,
                "total_queries": len(corpus.queries),
                "total_docs": len(corpus.documents),
                "backend": "in_memory",
                "synthetic_fixture_seed_mode": "direct_non_promotion",
            },
            "overall": overall,
            "deltas": deltas,
            "speedup": speedup,
            "per_layer": layer_comparison,
            "query_details": {
                "kestrel": kestrel_results,
                "baseline": baseline_results,
            },
        }
        result["acceptance"] = _evaluate_quality_gate(result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Kestrel vs baseline RAG large memory benchmark."
    )
    parser.add_argument("--k", type=int, default=5, help="Top-k retrieval cutoff")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, help="JSON output path")
    args = parser.parse_args()

    result = run_memory_benchmark(k=args.k, seed=args.seed)
    result["acceptance"] = _evaluate_quality_gate(result)
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2))
        print(f"\nWrote results to {args.output}", file=sys.stderr)
    return 0 if result["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
