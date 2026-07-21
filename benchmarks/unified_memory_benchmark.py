"""Unified memory retrieval benchmark across Kestrel and real backends.

Usage:
    python benchmarks/unified_memory_benchmark.py --output benchmark_results/unified_benchmark.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if __package__:
    from .adapters.base import MemoryBackend, OptionalDependencyUnavailable
    from .adapters.chroma_adapter import ChromaAdapter
    from .adapters.kestrel_adapter import KestrelAdapter
    from .adapters.qdrant_adapter import QdrantAdapter
    from .adapters.tfidf_adapter import TFIDFAdapter
    from .adapters.vector_rag import VectorRAG
    from .datasets_corpus.memory_corpus_large import build_large_memory_corpus
else:
    from adapters.base import MemoryBackend, OptionalDependencyUnavailable
    from adapters.chroma_adapter import ChromaAdapter
    from adapters.kestrel_adapter import KestrelAdapter
    from adapters.qdrant_adapter import QdrantAdapter
    from adapters.tfidf_adapter import TFIDFAdapter
    from adapters.vector_rag import VectorRAG
    from datasets_corpus.memory_corpus_large import build_large_memory_corpus


@dataclass(frozen=True, slots=True)
class _QualityFloor:
    version: str
    min_recall_at_k: float
    min_precision_at_k: float
    min_mrr: float

    def __post_init__(self) -> None:
        values = (self.min_recall_at_k, self.min_precision_at_k, self.min_mrr)
        if not self.version.strip():
            raise ValueError("Quality-floor version must be nonempty")
        if any(not math.isfinite(value) or value <= 0.0 or value > 1.0 for value in values):
            raise ValueError("All retrieval quality floors must be finite and in (0, 1]")

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "minimums": {
                "recall_at_k": self.min_recall_at_k,
                "precision_at_k": self.min_precision_at_k,
                "mrr": self.min_mrr,
            },
        }


_QUALITY_FLOOR_VERSION = "kestrel.unified-memory-quality-floor.v1"
_NONZERO_QUALITY_FLOOR_V1 = _QualityFloor(
    version=_QUALITY_FLOOR_VERSION,
    min_recall_at_k=0.01,
    min_precision_at_k=0.01,
    min_mrr=0.01,
)


@dataclass(frozen=True, slots=True)
class _BackendSpec:
    name: str
    factory: Callable[[], MemoryBackend]
    required: bool
    quality_floor: _QualityFloor = _NONZERO_QUALITY_FLOOR_V1


def _backend_specs() -> tuple[_BackendSpec, ...]:
    """Return required built-ins first, followed by optional comparison backends."""

    return (
        _BackendSpec(
            name=KestrelAdapter.LEXICAL_BACKEND_NAME,
            factory=lambda: KestrelAdapter(hybrid=False),
            required=True,
            quality_floor=_NONZERO_QUALITY_FLOOR_V1,
        ),
        _BackendSpec(
            name=TFIDFAdapter.BACKEND_NAME,
            factory=TFIDFAdapter,
            required=True,
            quality_floor=_NONZERO_QUALITY_FLOOR_V1,
        ),
        _BackendSpec(
            name=VectorRAG.BACKEND_NAME,
            factory=VectorRAG,
            required=False,
            quality_floor=_NONZERO_QUALITY_FLOOR_V1,
        ),
        _BackendSpec(
            name=QdrantAdapter.BACKEND_NAME,
            factory=QdrantAdapter,
            required=False,
            quality_floor=_NONZERO_QUALITY_FLOOR_V1,
        ),
        _BackendSpec(
            name=ChromaAdapter.BACKEND_NAME,
            factory=ChromaAdapter,
            required=False,
            quality_floor=_NONZERO_QUALITY_FLOOR_V1,
        ),
    )


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


def benchmark_backend(
    backend: MemoryBackend,
    corpus: Any,
    k: int = 5,
) -> dict[str, Any]:
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
            "avg_latency_ms": round(
                sum(r["latency_ms"] for r in layer_results) / len(layer_results), 3
            )
            if layer_results
            else 0,
            "p99_latency_ms": round(
                sorted(r["latency_ms"] for r in layer_results)[int(len(layer_results) * 0.99)]
                if layer_results
                else 0,
                3,
            ),
        }

    overall = {
        "recall_at_k": round(_avg("recall_at_k", results), 3),
        "precision_at_k": round(_avg("precision_at_k", results), 3),
        "mrr": round(_avg("mrr", results), 3),
        "avg_latency_ms": round(sum(latencies) / len(latencies) * 1000, 3),
        "p99_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.99)] * 1000, 3)
        if latencies
        else 0,
        "ingest_time_s": round(ingest_time, 3),
        "per_layer": per_layer,
    }

    return {
        "name": name,
        "overall": overall,
        "query_details": results,
    }


def _evaluate_quality_floor(
    result: dict[str, Any],
    floor: _QualityFloor,
) -> dict[str, Any]:
    overall = result.get("overall", {})
    query_count = len(result.get("query_details", []))
    minimums = floor.to_payload()["minimums"]
    observed = {metric: float(overall.get(metric, 0.0)) for metric in minimums}
    checks = {
        "nonempty_query_set": query_count > 0,
        **{
            f"{metric}_at_or_above_floor": math.isfinite(observed[metric])
            and observed[metric] >= minimum
            for metric, minimum in minimums.items()
        },
    }
    return {
        "version": floor.version,
        "minimums": minimums,
        "observed": observed,
        "query_count": query_count,
        "checks": checks,
        "passed": all(checks.values()),
    }


def run_unified_benchmark(*, k: int = 5, seed: int = 42) -> dict[str, Any]:
    corpus = build_large_memory_corpus(seed=seed)
    print(f"Corpus: {len(corpus.documents)} docs, {len(corpus.queries)} queries", file=sys.stderr)

    results: list[dict[str, Any]] = []
    for spec in _backend_specs():
        try:
            backend = spec.factory()
            result = benchmark_backend(backend, corpus, k=k)
            quality_gate = _evaluate_quality_floor(result, spec.quality_floor)
            result.update(
                {
                    "status": "passed" if quality_gate["passed"] else "failed",
                    "required": spec.required,
                    "quality_gate": quality_gate,
                }
            )
            if not quality_gate["passed"]:
                result.update(
                    {
                        "stage": "quality_gate",
                        "error": (
                            "Retrieval quality did not meet the versioned absolute floor "
                            f"{spec.quality_floor.version}."
                        ),
                    }
                )
            results.append(result)
        except OptionalDependencyUnavailable as exc:
            if spec.required:
                print(f"ERROR initializing required backend {spec.name}: {exc}", file=sys.stderr)
                results.append(
                    {
                        "name": spec.name,
                        "status": "failed",
                        "required": True,
                        "stage": "initialization",
                        "error": str(exc),
                    }
                )
                continue
            print(str(exc), file=sys.stderr)
            results.append(
                {
                    "name": spec.name,
                    "status": "skipped",
                    "required": False,
                    "skip_reason": "missing_optional_dependency",
                    "missing_dependency": exc.missing_dependency,
                    "install_hint": exc.install_hint,
                }
            )
        except Exception as exc:  # noqa: BLE001 - failures belong in the report
            print(f"ERROR benchmarking {spec.name}: {exc}", file=sys.stderr)
            traceback.print_exc()
            results.append(
                {
                    "name": spec.name,
                    "status": "failed",
                    "required": spec.required,
                    "error": str(exc),
                }
            )

    # Build comparison table
    comparison = {}
    for r in results:
        if r.get("status") == "passed" and "overall" in r:
            comparison[r["name"]] = r["overall"]

    passed = sum(result.get("status") == "passed" for result in results)
    skipped = sum(result.get("status") == "skipped" for result in results)
    failed = sum(result.get("status") == "failed" for result in results)
    required_failed = sum(
        result.get("status") != "passed" and result.get("required") is True for result in results
    )

    return {
        "schema": "kestrel.unified_memory_benchmark.v1",
        "config": {
            "k": k,
            "seed": seed,
            "total_queries": len(corpus.queries),
            "total_docs": len(corpus.documents),
            "quality_floor_version": _QUALITY_FLOOR_VERSION,
        },
        "results": results,
        "comparison": comparison,
        "summary": {
            "passed": passed,
            "skipped": skipped,
            "failed": failed,
            "required_failed": required_failed,
            "success": passed > 0 and failed == 0 and required_failed == 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run unified memory benchmark across all backends."
    )
    parser.add_argument("--k", type=int, default=5, help="Top-k retrieval cutoff")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark_results/unified_benchmark.json")
    )
    args = parser.parse_args()

    result = run_unified_benchmark(k=args.k, seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(f"\nWrote results to {args.output}", file=sys.stderr)

    # Print summary table
    print("\n" + "=" * 100)
    print("UNIFIED MEMORY BENCHMARK RESULTS")
    print("=" * 100)
    print(
        f"{'Backend':<40} {f'Recall@{args.k}':>10} {f'Prec@{args.k}':>10} "
        f"{'MRR':>10} {'Latency':>10} {'Ingest':>10}"
    )
    print("-" * 100)
    for name, metrics in result["comparison"].items():
        print(
            f"{name:<40} {metrics['recall_at_k']:>10.3f} {metrics['precision_at_k']:>10.3f} {metrics['mrr']:>10.3f} {metrics['avg_latency_ms']:>8.1f}ms {metrics['ingest_time_s']:>8.2f}s"
        )
    for backend_result in result["results"]:
        status = backend_result.get("status")
        if status == "skipped":
            print(
                f"{backend_result['name']:<40} {'SKIPPED':>10}  "
                f"missing {backend_result['missing_dependency']}"
            )
        elif status == "failed":
            print(f"{backend_result['name']:<40} {'FAILED':>10}  {backend_result['error']}")
    print("=" * 100)

    return 0 if result["summary"]["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
