"""Unified memory retrieval benchmark across Kestrel and real backends.

Usage:
    python benchmarks/unified_memory_benchmark.py --output benchmark_results/unified_benchmark.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters.chroma_adapter import ChromaAdapter
from adapters.kestrel_adapter import KestrelAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.tfidf_adapter import TFIDFAdapter
from adapters.vector_rag import VectorRAG
from datasets_corpus.memory_corpus_large import build_large_memory_corpus


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


def benchmark_backend(backend, corpus, k: int = 5) -> dict[str, Any]:
    """Run a single backend through the benchmark corpus."""
    name = backend.name()
    print(f"\n=== Benchmarking {name} ===", file=sys.stderr)

    # Ingest
    t0 = time.perf_counter()
    for doc in corpus.documents:
        backend.ingest(doc["id"], doc["text"], doc.get("layer"))
    ingest_time = time.perf_counter() - t0
    print(f"  Ingest: {ingest_time:.3f}s", file=sys.stderr)

    # Retrieve
    results = []
    latencies = []
    for i, q in enumerate(corpus.queries):
        if i % 30 == 0:
            print(f"  Query {i}/{len(corpus.queries)}...", file=sys.stderr)
        t0 = time.perf_counter()
        hits = backend.retrieve(q.query, k=k, layer=q.layer)
        t1 = time.perf_counter()
        latencies.append(t1 - t0)
        ids = [h.doc_id for h in hits]
        metrics = _compute_metrics(ids, q.expected_doc_ids)
        metrics["query"] = q.query
        metrics["layer"] = q.layer
        metrics["latency_ms"] = round((t1 - t0) * 1000, 3)
        metrics["retrieved_ids"] = ids
        results.append(metrics)

    def _avg(key: str, data: list[dict[str, Any]]) -> float:
        values = [d[key] for d in data if d[key] is not None]
        return sum(values) / len(values) if values else 0.0

    # Per-layer breakdown
    layers = ["semantic", "episodic", "procedural"]
    per_layer = {}
    for layer in layers:
        layer_results = [r for r in results if r["layer"] == layer]
        per_layer[layer] = {
            "recall_at_k": round(_avg("recall_at_k", layer_results), 3),
            "precision_at_k": round(_avg("precision_at_k", layer_results), 3),
            "mrr": round(_avg("mrr", layer_results), 3),
            "avg_latency_ms": round(sum(r["latency_ms"] for r in layer_results) / len(layer_results), 3) if layer_results else 0,
            "p99_latency_ms": round(sorted(r["latency_ms"] for r in layer_results)[int(len(layer_results) * 0.99)] if layer_results else 0, 3),
        }

    overall = {
        "recall_at_k": round(_avg("recall_at_k", results), 3),
        "precision_at_k": round(_avg("precision_at_k", results), 3),
        "mrr": round(_avg("mrr", results), 3),
        "avg_latency_ms": round(sum(latencies) / len(latencies) * 1000, 3),
        "p99_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.99)] * 1000, 3) if latencies else 0,
        "ingest_time_s": round(ingest_time, 3),
        "per_layer": per_layer,
    }

    return {
        "name": name,
        "overall": overall,
        "query_details": results,
    }


def run_unified_benchmark(*, k: int = 5, seed: int = 42) -> dict[str, Any]:
    corpus = build_large_memory_corpus(seed=seed)
    print(f"Corpus: {len(corpus.documents)} docs, {len(corpus.queries)} queries", file=sys.stderr)

    backends = [
        KestrelAdapter(),
        TFIDFAdapter(),
        VectorRAG(),
        QdrantAdapter(),
        ChromaAdapter(),
    ]

    results = []
    for backend in backends:
        try:
            result = benchmark_backend(backend, corpus, k=k)
            results.append(result)
        except Exception as e:
            print(f"ERROR benchmarking {backend.name()}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            results.append({"name": backend.name(), "error": str(e)})

    # Build comparison table
    comparison = {}
    for r in results:
        if "overall" in r:
            comparison[r["name"]] = r["overall"]

    return {
        "schema": "kestrel.unified_memory_benchmark.v1",
        "config": {"k": k, "seed": seed, "total_queries": len(corpus.queries), "total_docs": len(corpus.documents)},
        "results": results,
        "comparison": comparison,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run unified memory benchmark across all backends.")
    parser.add_argument("--k", type=int, default=5, help="Top-k retrieval cutoff")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("benchmark_results/unified_benchmark.json"))
    args = parser.parse_args()

    result = run_unified_benchmark(k=args.k, seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(f"\nWrote results to {args.output}", file=sys.stderr)

    # Print summary table
    print("\n" + "=" * 100)
    print("UNIFIED MEMORY BENCHMARK RESULTS")
    print("=" * 100)
    print(f"{'Backend':<40} {'Recall@5':>10} {'Prec@5':>10} {'MRR':>10} {'Latency':>10} {'Ingest':>10}")
    print("-" * 100)
    for name, metrics in result["comparison"].items():
        print(f"{name:<40} {metrics['recall_at_k']:>10.3f} {metrics['precision_at_k']:>10.3f} {metrics['mrr']:>10.3f} {metrics['avg_latency_ms']:>8.1f}ms {metrics['ingest_time_s']:>8.2f}s")
    print("=" * 100)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
