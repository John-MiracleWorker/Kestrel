"""Breadth benchmark: reproducible multi-seed / k / checkpoint / scenario matrix.

Implements BENCH-003 (measure breadth and growth reproducibly) and BENCH-004
(report credible metrics and unfavorable results honestly) for the S10 slice.

The benchmark runs a fixed matrix:

    seeds x k_values x checkpoints x scenarios

with three arms on every cell:

  1. Kestrel layered memory (in-memory benchmark backend, layer-aware
     retrieval — all eligible layers, deterministic global top-k, no oracle
     layer label, per BENCH-002)
  2. Flat TF-IDF RAG (document store, cosine similarity, no layers)
  3. Flat transcript (recency-biased chronological transcript)

Every cell emits raw per-query rows (BENCH-003: raw results) plus aggregate
statistics. The published artifact is bound to the exact fixtures and
environment by digests:

  - fixture_digest:      sha256 over the exact documents + queries of every
                          (scenario, seed, checkpoint) cell
  - environment_digest:  sha256 over the runtime snapshot (python, platform,
                          backend, package versions, seed mode)
  - methodology_digest:  sha256 over the matrix config + gate definition

Aggregate metrics (BENCH-004): Recall@k, Precision@k, MRR, p50/p95/p99
latency, and deterministic bootstrap confidence intervals (fixed bootstrap
seed over the deterministic per-query metric values, so the CIs are
reproducible). Recency and growth degradation are reported as honest deltas
for every arm.

The methodology gate is fail-closed and never requires Kestrel to win: it
passes only when every arm returns evidence for every query in every cell and
all metrics (and CI bounds) are finite. Deltas are published exactly as
measured, including when Kestrel is not ahead.

Schema: kestrel.memory_benchmark.v3

Usage:
    python benchmarks/memory_benchmark_breadth.py --output benchmark_results/memory_breadth.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

import numpy as np
from adapters.flat_transcript import FlatTranscriptMemory
from baseline_rag import BaselineRAG
from datasets_corpus.memory_corpus_breadth import (
    SCENARIOS,
    build_breadth_corpus,
    scenario_digest_inputs,
)

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery

SCHEMA = "kestrel.memory_benchmark.v3"
ARMS = ("kestrel", "tfidf", "transcript")
QUALITY_METRICS = ("recall_at_k", "precision_at_k", "mrr")
LATENCY_PERCENTILES = (50, 95, 99)

DEFAULT_SEEDS = (42, 1337, 2026)
DEFAULT_K_VALUES = (3, 5)
DEFAULT_CHECKPOINTS = (0, 1, 2)

GATE_VERSION = "kestrel.memory-breadth-methodology-gate.v1"
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_CONFIDENCE = 0.95
BOOTSTRAP_SEED = 1729


# --- canonical hashing ------------------------------------------------------

def _canonical_json(value: Any) -> str:
    """Deterministic JSON serialization (sorted keys, no spaces)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _environment_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """Deterministic runtime snapshot for the environment digest."""
    import importlib.metadata

    def _pkg_version(name: str) -> str | None:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "backend": "in_memory",
        "synthetic_fixture_seed_mode": "direct_non_promotion",
        "packages": {
            "nested-memvid-agent": _pkg_version("nested-memvid-agent"),
            "numpy": _pkg_version("numpy"),
            "sentence-transformers": _pkg_version("sentence-transformers"),
        },
        "config": config,
    }


def _methodology_definition(config: dict[str, Any]) -> dict[str, Any]:
    """Canonical methodology definition for the methodology digest."""
    return {
        "schema": SCHEMA,
        "arms": list(ARMS),
        "quality_metrics": list(QUALITY_METRICS),
        "latency_percentiles": list(LATENCY_PERCENTILES),
        "gate": {
            "version": GATE_VERSION,
            "rule": (
                "fail closed: every arm returns evidence for every query in "
                "every cell AND all per-query metrics and CI bounds are finite. "
                "No arm is required to win; deltas are reported exactly as "
                "measured, including when Kestrel is not ahead (BENCH-004)."
            ),
        },
        "bootstrap": {
            "replicates": BOOTSTRAP_REPLICATES,
            "confidence": BOOTSTRAP_CONFIDENCE,
            "seed": BOOTSTRAP_SEED,
            "note": (
                "percentile bootstrap over per-query metric values with a fixed "
                "RNG seed; deterministic because the per-query metric values "
                "are deterministic for the fixed matrix."
            ),
        },
        "config": config,
    }


# --- metrics ----------------------------------------------------------------

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


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * percentile / 100.0))
    return float(ordered[idx])


def _bootstrap_ci(values: list[float], *, rng: np.random.Generator) -> tuple[float, float]:
    """Deterministic percentile bootstrap confidence interval.

    ``values`` are the per-query metric values for one arm in one cell. The
    RNG is seeded deterministically per cell, so the resulting CI is
    reproducible for the fixed matrix (BENCH-004).
    """
    if not values:
        return (0.0, 0.0)
    arr = np.asarray(values, dtype=np.float64)
    n = arr.shape[0]
    replicates = np.empty(BOOTSTRAP_REPLICATES, dtype=np.float64)
    for i in range(BOOTSTRAP_REPLICATES):
        sample = arr[rng.integers(0, n, size=n)]
        replicates[i] = float(np.mean(sample))
    alpha = (1.0 - BOOTSTRAP_CONFIDENCE) / 2.0
    lower = float(np.quantile(replicates, alpha))
    upper = float(np.quantile(replicates, 1.0 - alpha))
    return (lower, upper)


# --- arms -------------------------------------------------------------------

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


@dataclass
class CellRow:
    arm: str
    query: str
    layer: str
    expected_ids: list[str]
    retrieved_ids: list[str]
    recall_at_k: float
    precision_at_k: float
    mrr: float
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "query": self.query,
            "layer": self.layer,
            "expected_ids": list(self.expected_ids),
            "retrieved_ids": list(self.retrieved_ids),
            "recall_at_k": round(self.recall_at_k, 6),
            "precision_at_k": round(self.precision_at_k, 6),
            "mrr": round(self.mrr, 6),
            "latency_ms": round(self.latency_ms, 3),
            "evidence": bool(self.retrieved_ids),
        }


@dataclass
class CellResult:
    scenario: str
    seed: int
    checkpoint: int
    k: int
    rows: list[CellRow] = field(default_factory=list)

    def rows_for(self, arm: str) -> list[CellRow]:
        return [r for r in self.rows if r.arm == arm]


def _run_cell(scenario: str, *, seed: int, checkpoint: int, k: int) -> CellResult:
    """Run all three arms over one (scenario, seed, checkpoint, k) cell."""
    corpus = build_breadth_corpus(scenario, seed=seed, checkpoint=checkpoint)
    result = CellResult(scenario=scenario, seed=seed, checkpoint=checkpoint, k=k)

    with tempfile.TemporaryDirectory(prefix="kestrel-breadth-") as tmpdir:
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

        for q in corpus.queries:
            t0 = time.perf_counter()
            hits = kestrel.retrieve(
                RetrievalQuery(query=q.query, k_per_layer=k, layers=tuple(MemoryLayer))
            )
            t1 = time.perf_counter()
            kestrel_ids = [hit.record.id for hit in hits[:k]]
            m = _compute_metrics(kestrel_ids, q.expected_doc_ids)
            result.rows.append(
                CellRow(
                    arm="kestrel",
                    query=q.query,
                    layer=q.layer,
                    expected_ids=q.expected_doc_ids,
                    retrieved_ids=kestrel_ids,
                    recall_at_k=m["recall_at_k"],
                    precision_at_k=m["precision_at_k"],
                    mrr=m["mrr"],
                    latency_ms=(t1 - t0) * 1000,
                )
            )

            t0 = time.perf_counter()
            tfidf_results = tfidf.retrieve(q.query, k=k)
            t1 = time.perf_counter()
            tfidf_ids = [r.doc.metadata.get("id", r.doc.id) for r in tfidf_results]
            m = _compute_metrics(tfidf_ids, q.expected_doc_ids)
            result.rows.append(
                CellRow(
                    arm="tfidf",
                    query=q.query,
                    layer=q.layer,
                    expected_ids=q.expected_doc_ids,
                    retrieved_ids=tfidf_ids,
                    recall_at_k=m["recall_at_k"],
                    precision_at_k=m["precision_at_k"],
                    mrr=m["mrr"],
                    latency_ms=(t1 - t0) * 1000,
                )
            )

            t0 = time.perf_counter()
            transcript_results = transcript.retrieve(q.query, k=k)
            t1 = time.perf_counter()
            transcript_ids = [r.doc_id for r in transcript_results]
            m = _compute_metrics(transcript_ids, q.expected_doc_ids)
            result.rows.append(
                CellRow(
                    arm="transcript",
                    query=q.query,
                    layer=q.layer,
                    expected_ids=q.expected_doc_ids,
                    retrieved_ids=transcript_ids,
                    recall_at_k=m["recall_at_k"],
                    precision_at_k=m["precision_at_k"],
                    mrr=m["mrr"],
                    latency_ms=(t1 - t0) * 1000,
                )
            )

    return result


# --- aggregation ------------------------------------------------------------

def _cell_aggregate(cell: CellResult) -> dict[str, Any]:
    """Per-arm aggregate statistics for one cell (BENCH-004)."""
    aggregate: dict[str, Any] = {}
    for arm in ARMS:
        rows = cell.rows_for(arm)
        latencies = [r.latency_ms for r in rows]
        aggregate[arm] = {
            "recall_at_k": round(sum(r.recall_at_k for r in rows) / len(rows), 6) if rows else 0.0,
            "precision_at_k": round(sum(r.precision_at_k for r in rows) / len(rows), 6) if rows else 0.0,
            "mrr": round(sum(r.mrr for r in rows) / len(rows), 6) if rows else 0.0,
            "latency_ms": {
                f"p{p}": round(_percentile(latencies, p), 3) for p in LATENCY_PERCENTILES
            },
            "avg_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        }
    return aggregate


def _cell_confidence_intervals(cell: CellResult, *, seed: int) -> dict[str, Any]:
    """Deterministic bootstrap CIs per arm/metric for one cell."""
    rng = np.random.default_rng(BOOTSTRAP_SEED + seed * 7919 + cell.checkpoint * 104729 + cell.k * 15485863)
    cis: dict[str, Any] = {}
    for arm in ARMS:
        rows = cell.rows_for(arm)
        cis[arm] = {}
        for metric in QUALITY_METRICS:
            values = [getattr(r, metric) for r in rows]
            lower, upper = _bootstrap_ci(values, rng=rng)
            cis[arm][metric] = {
                "lower": round(lower, 6),
                "upper": round(upper, 6),
                "replicates": BOOTSTRAP_REPLICATES,
                "confidence": BOOTSTRAP_CONFIDENCE,
            }
    return cis


# --- methodology gate -------------------------------------------------------

def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _evaluate_gate(
    cells: list[CellResult],
    confidence_intervals: dict[tuple[int, int, int], dict[str, Any]],
) -> dict[str, Any]:
    """Fail-closed methodology gate with no required winner (BENCH-004).

    Fails closed when any arm returns zero evidence for any query in any cell,
    or when any per-query metric or CI bound is non-finite. Never compares arms
    against each other.
    """
    checks: dict[str, bool] = {}
    for cell in cells:
        key = f"{cell.scenario}/cp{cell.checkpoint}/s{cell.seed}/k{cell.k}"
        for arm in ARMS:
            rows = cell.rows_for(arm)
            checks[f"{key}:{arm}_evidence_every_query"] = bool(rows) and all(
                bool(r.retrieved_ids) for r in rows
            )
            checks[f"{key}:{arm}_finite_metrics"] = all(
                _finite(getattr(r, metric))
                for r in rows
                for metric in QUALITY_METRICS
            )
        cis = confidence_intervals.get((cell.seed, cell.checkpoint, cell.k), {})
        for arm in ARMS:
            for metric in QUALITY_METRICS:
                bound = cis.get(arm, {}).get(metric, {})
                checks[f"{key}:{arm}:{metric}_finite_ci"] = (
                    _finite(bound.get("lower")) and _finite(bound.get("upper"))
                )

    return {
        "version": GATE_VERSION,
        "rule": (
            "fail closed: every arm returns evidence for every query in every "
            "cell AND all per-query metrics and CI bounds are finite. No arm "
            "is required to win (BENCH-004)."
        ),
        "cell_count": len(cells),
        "check_count": len(checks),
        "checks": checks,
        "passed": bool(checks) and all(checks.values()),
    }


# --- degradation ------------------------------------------------------------

def _mean_metric(aggregates: dict[int, dict[str, Any]], metric: str, arm: str) -> float:
    """Mean of a metric across all seeds/checkpoints/k in an aggregates dict."""
    values = [
        agg[arm][metric]
        for agg in aggregates.values()
        if arm in agg and metric in agg[arm]
    ]
    return sum(values) / len(values) if values else 0.0


def _recency_degradation(
    scenario_aggregates: dict[str, dict[tuple[int, int, int], dict[str, Any]]],
) -> dict[str, Any]:
    """Delta between baseline and recency scenarios for each arm/metric.

    Reported for every arm exactly as measured; a negative delta means the
    recency scenario degraded that arm relative to baseline.
    """
    baseline = scenario_aggregates.get("baseline", {})
    recency = scenario_aggregates.get("recency", {})
    common_keys = set(baseline) & set(recency)
    degradation: dict[str, Any] = {}
    for metric in QUALITY_METRICS:
        degradation[metric] = {}
        for arm in ARMS:
            base_vals = [baseline[key][arm][metric] for key in sorted(common_keys) if arm in baseline[key]]
            rec_vals = [recency[key][arm][metric] for key in sorted(common_keys) if arm in recency[key]]
            if base_vals and rec_vals:
                degradation[metric][arm] = {
                    "baseline": round(sum(base_vals) / len(base_vals), 6),
                    "recency": round(sum(rec_vals) / len(rec_vals), 6),
                    "delta": round(sum(rec_vals) / len(rec_vals) - sum(base_vals) / len(base_vals), 6),
                }
    return degradation


def _growth_degradation(
    scenario_aggregates: dict[str, dict[tuple[int, int, int], dict[str, Any]]],
    checkpoints: list[int],
) -> dict[str, Any]:
    """Delta between the smallest and largest growth checkpoint per scenario.

    cp0 is the scenario corpus with no growth filler; the largest checkpoint
    has the most filler. The delta is reported per arm/metric as
    (largest - smallest); a negative delta means growth degraded that arm.
    """
    if len(checkpoints) < 2:
        return {}
    first_cp = min(checkpoints)
    last_cp = max(checkpoints)
    degradation: dict[str, Any] = {}
    for scenario, aggregates in scenario_aggregates.items():
        # Keys are (seed, checkpoint, k); growth compares the same (seed, k)
        # pair across the smallest and largest checkpoint.
        first = {key: agg for key, agg in aggregates.items() if key[1] == first_cp}
        last = {key: agg for key, agg in aggregates.items() if key[1] == last_cp}
        common_sk = set((key[0], key[2]) for key in first) & set(
            (key[0], key[2]) for key in last
        )
        if not common_sk:
            continue
        degradation[scenario] = {}
        for metric in QUALITY_METRICS:
            degradation[scenario][metric] = {}
            for arm in ARMS:
                f_vals = [
                    first[key][arm][metric]
                    for key in first
                    if (key[0], key[2]) in common_sk and arm in first[key]
                ]
                l_vals = [
                    last[key][arm][metric]
                    for key in last
                    if (key[0], key[2]) in common_sk and arm in last[key]
                ]
                if f_vals and l_vals:
                    f_mean = sum(f_vals) / len(f_vals)
                    l_mean = sum(l_vals) / len(l_vals)
                    degradation[scenario][metric][arm] = {
                        f"checkpoint_{first_cp}": round(f_mean, 6),
                        f"checkpoint_{last_cp}": round(l_mean, 6),
                        "delta": round(l_mean - f_mean, 6),
                    }
    return degradation


# --- top-level runner -------------------------------------------------------

def run_breadth_benchmark(
    *,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
    checkpoints: tuple[int, ...] = DEFAULT_CHECKPOINTS,
    scenarios: tuple[str, ...] = SCENARIOS,
) -> dict[str, Any]:
    """Run the full fixed breadth matrix and return the v3 report.

    Deterministic quality metrics and CIs; latency percentiles are measured
    wall-clock values (environment-sensitive) and are reported as measured.
    """
    config = {
        "seeds": [int(s) for s in seeds],
        "k_values": [int(k) for k in k_values],
        "checkpoints": [int(c) for c in checkpoints],
        "scenarios": list(scenarios),
        "arms": list(ARMS),
        "backend": "in_memory",
        "synthetic_fixture_seed_mode": "direct_non_promotion",
        "growth_step": 6,
        "latency_percentiles": list(LATENCY_PERCENTILES),
    }

    cells: list[CellResult] = []
    scenario_aggregates: dict[str, dict[tuple[int, int, int], dict[str, Any]]] = {}
    confidence_intervals: dict[tuple[int, int, int], dict[str, Any]] = {}
    scenario_ci: dict[str, dict[tuple[int, int, int], dict[str, Any]]] = {}

    for scenario in scenarios:
        scenario_aggregates[scenario] = {}
        scenario_ci[scenario] = {}
        for seed in seeds:
            for checkpoint in checkpoints:
                for k in k_values:
                    cell = _run_cell(scenario, seed=seed, checkpoint=checkpoint, k=k)
                    cells.append(cell)
                    key = (seed, checkpoint, k)
                    scenario_aggregates[scenario][key] = _cell_aggregate(cell)
                    cis = _cell_confidence_intervals(cell, seed=seed)
                    confidence_intervals[key] = cis
                    scenario_ci[scenario][key] = cis

    # Explicit cell map: scenario -> seed -> checkpoint -> k -> data.
    cells_out: dict[str, Any] = {}
    for cell in cells:
        s_block = cells_out.setdefault(cell.scenario, {})
        seed_block = s_block.setdefault(str(cell.seed), {})
        cp_block = seed_block.setdefault(str(cell.checkpoint), {})
        k_block = cp_block.setdefault(str(cell.k), {})
        k_block["query_count"] = len(cell.rows) // len(ARMS)
        k_block["aggregate"] = scenario_aggregates[cell.scenario][(cell.seed, cell.checkpoint, cell.k)]
        k_block["confidence_intervals"] = scenario_ci[cell.scenario][(cell.seed, cell.checkpoint, cell.k)]
        k_block["raw_rows"] = [row.to_dict() for row in cell.rows]

    # Digests.
    fixture_digest_inputs: dict[str, Any] = {}
    for scenario in scenarios:
        fixture_digest_inputs[scenario] = scenario_digest_inputs(scenario, seeds=list(seeds), checkpoints=list(checkpoints))
    fixture_digest = _sha256(_canonical_json(fixture_digest_inputs))

    environment = _environment_snapshot(config)
    environment_digest = _sha256(_canonical_json(environment))

    methodology = _methodology_definition(config)
    methodology_digest = _sha256(_canonical_json(methodology))

    degradation = {
        "recency": _recency_degradation(scenario_aggregates),
        "growth": _growth_degradation(scenario_aggregates, list(checkpoints)),
    }

    gate = _evaluate_gate(cells, confidence_intervals)

    return {
        "schema": SCHEMA,
        "config": config,
        "digests": {
            "algorithm": "sha256",
            "fixture_digest": fixture_digest,
            "environment_digest": environment_digest,
            "methodology_digest": methodology_digest,
        },
        "environment": environment,
        "methodology": methodology,
        "cells": cells_out,
        "degradation": degradation,
        "acceptance": gate,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed breadth benchmark matrix (BENCH-003/BENCH-004)."
    )
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--k-values", type=int, nargs="+", default=list(DEFAULT_K_VALUES))
    parser.add_argument("--checkpoints", type=int, nargs="+", default=list(DEFAULT_CHECKPOINTS))
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS))
    parser.add_argument("--output", type=Path, help="JSON output path")
    args = parser.parse_args()

    report = run_breadth_benchmark(
        seeds=tuple(args.seeds),
        k_values=tuple(args.k_values),
        checkpoints=tuple(args.checkpoints),
        scenarios=tuple(args.scenarios),
    )
    print(json.dumps(report, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(f"\nWrote breadth benchmark report to {args.output}", file=sys.stderr)
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
