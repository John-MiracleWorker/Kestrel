"""Tests for the breadth benchmark (BENCH-003/BENCH-004, S10)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

from benchmarks.datasets_corpus.memory_corpus_breadth import (
    SCENARIOS,
    build_breadth_corpus,
    scenario_digest_inputs,
)
from benchmarks.memory_benchmark_breadth import (
    ARMS,
    LATENCY_PERCENTILES,
    QUALITY_METRICS,
    SCHEMA,
    main,
    run_breadth_benchmark,
)

# A fast-but-covering matrix for tests (multi-seed, multi-k, multi-checkpoint,
# all scenario families present).
T_SEEDS: tuple[int, ...] = (42, 1337)
T_K: tuple[int, ...] = (3, 5)
T_CPS: tuple[int, ...] = (0, 1)
T_SCENARIOS: tuple[str, ...] = SCENARIOS


def _test_matrix() -> dict[str, Any]:
    return dict(seeds=T_SEEDS, k_values=T_K, checkpoints=T_CPS, scenarios=T_SCENARIOS)


def test_schema_and_fixed_matrix() -> None:
    report = run_breadth_benchmark(**_test_matrix())

    assert report["schema"] == SCHEMA == "kestrel.memory_benchmark.v3"
    # Fixed multi-seed matrix, k values, corpus checkpoints, scenarios
    # (BENCH-003).
    assert report["config"]["seeds"] == [42, 1337]
    assert report["config"]["k_values"] == [3, 5]
    assert report["config"]["checkpoints"] == [0, 1]
    assert set(report["config"]["scenarios"]) == set(SCENARIOS)
    # All scenario families must be present as top-level cell keys.
    assert set(report["cells"].keys()) == set(SCENARIOS)
    # Every (seed, checkpoint, k) cell exists for every scenario.
    for scenario in SCENARIOS:
        for seed in ("42", "1337"):
            assert seed in report["cells"][scenario]
            for cp in ("0", "1"):
                assert cp in report["cells"][scenario][seed]
                for k in ("3", "5"):
                    assert k in report["cells"][scenario][seed][cp]


def test_methodology_gate_passes_without_requiring_a_winner() -> None:
    """BENCH-004: the gate is methodological and does not require Kestrel to win.

    It fails closed only when an arm returns no evidence for a query or a
    metric/CI bound is non-finite. A negative Kestrel delta must not fail the
    gate.
    """
    report = run_breadth_benchmark(**_test_matrix())

    acceptance = report["acceptance"]
    assert acceptance["version"] == "kestrel.memory-breadth-methodology-gate.v1"
    assert acceptance["passed"] is True
    assert acceptance["cell_count"] == 2 * 2 * 2 * len(SCENARIOS)
    assert acceptance["check_count"] > 0
    for name, passed in acceptance["checks"].items():
        assert passed, f"gate check failed: {name}"

    # The gate must not encode any arm-beats-arm requirement.
    for arm in ARMS:
        assert f"{arm}_not_below" not in "".join(acceptance["checks"])


def test_raw_rows_and_metrics_are_present() -> None:
    """BENCH-003/004: raw per-query rows + Recall@k/Precision@k/MRR/p50-95-99."""
    report = run_breadth_benchmark(**_test_matrix())

    cell = report["cells"]["conflict"]["42"]["0"]["3"]
    assert cell["query_count"] > 0
    rows = cell["raw_rows"]
    assert len(rows) == cell["query_count"] * len(ARMS)
    arms_seen = {row["arm"] for row in rows}
    assert arms_seen == set(ARMS)

    for row in rows:
        for key in ("query", "layer", "expected_ids", "retrieved_ids", "evidence"):
            assert key in row, f"raw row missing {key}: {row}"
        for metric in QUALITY_METRICS:
            assert metric in row

    aggregate = cell["aggregate"]
    for arm in ARMS:
        for metric in QUALITY_METRICS:
            assert aggregate[arm][metric] >= 0.0
        latency = aggregate[arm]["latency_ms"]
        for p in LATENCY_PERCENTILES:
            assert f"p{p}" in latency
            assert latency[f"p{p}"] >= 0.0


def test_deterministic_confidence_intervals() -> None:
    """BENCH-004: bootstrap CIs are deterministic for the fixed matrix."""
    first = run_breadth_benchmark(**_test_matrix())
    second = run_breadth_benchmark(**_test_matrix())

    for scenario in SCENARIOS:
        for seed in ("42", "1337"):
            for cp in ("0", "1"):
                for k in ("3", "5"):
                    a = first["cells"][scenario][seed][cp][k]["confidence_intervals"]
                    b = second["cells"][scenario][seed][cp][k]["confidence_intervals"]
                    assert a == b, f"{scenario}/{seed}/{cp}/{k} CI not deterministic"
                    for arm in ARMS:
                        for metric in QUALITY_METRICS:
                            ci = a[arm][metric]
                            assert ci["replicates"] > 0
                            assert 0.0 <= ci["lower"] <= ci["upper"] <= 1.0


def test_digests_are_present_and_reproducible() -> None:
    """BENCH-003: manifest/environment/fixture digests, stable across runs."""
    first = run_breadth_benchmark(**_test_matrix())
    second = run_breadth_benchmark(**_test_matrix())

    for key in ("fixture_digest", "environment_digest", "methodology_digest"):
        digest = first["digests"][key]
        assert len(digest) == 64
        assert int(digest, 16) >= 0  # valid hex
        assert first["digests"][key] == second["digests"][key], (
            f"{key} not reproducible"
        )

    # Fixture digest must differ from the raw single-scenario serialization
    # shape (the benchmark digests the full scenario matrix, not one corpus).
    single = scenario_digest_inputs("conflict", seeds=[42, 1337], checkpoints=[0, 1])
    assert first["digests"]["fixture_digest"] != hashlib.sha256(
        json.dumps(single, sort_keys=True).encode()
    ).hexdigest()


def test_fixture_digest_binds_scenario_matrix() -> None:
    """The fixture digest covers every (scenario, seed, checkpoint) cell."""
    inputs = {
        scenario: scenario_digest_inputs(
            scenario, seeds=list(_test_matrix()["seeds"]), checkpoints=list(_test_matrix()["checkpoints"])
        )
        for scenario in SCENARIOS
    }
    digest = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report = run_breadth_benchmark(**_test_matrix())
    assert report["digests"]["fixture_digest"] == digest


def test_recency_and_growth_degradation_reported_for_every_arm() -> None:
    """BENCH-004: recency + growth degradation deltas for every arm."""
    report = run_breadth_benchmark(**_test_matrix())

    degradation = report["degradation"]
    assert "recency" in degradation
    assert "growth" in degradation

    # Recency degradation covers every arm and every quality metric.
    for metric in QUALITY_METRICS:
        for arm in ARMS:
            entry = degradation["recency"].get(metric, {}).get(arm)
            assert entry is not None, f"recency degradation missing {metric}/{arm}"
            assert "baseline" in entry and "recency" in entry and "delta" in entry

    # Growth degradation exists for at least the baseline scenario and every arm.
    baseline_growth = degradation["growth"].get("baseline", {})
    assert baseline_growth, "growth degradation missing baseline"
    for metric in QUALITY_METRICS:
        for arm in ARMS:
            entry = baseline_growth.get(metric, {}).get(arm)
            assert entry is not None, f"growth degradation missing {metric}/{arm}"
            assert "checkpoint_0" in entry and "delta" in entry


def test_recency_stress_degrades_the_flat_transcript_honestly() -> None:
    """The recency scenario measurably degrades the recency-biased transcript,
    and the deltas are reported for every arm exactly as measured (including
    Kestrel when it is not ahead).
    """
    report = run_breadth_benchmark(seeds=(42,), k_values=(5,), checkpoints=(0,), scenarios=("baseline", "recency"))

    base = report["cells"]["baseline"]["42"]["0"]["5"]["aggregate"]
    stressed = report["cells"]["recency"]["42"]["0"]["5"]["aggregate"]

    transcript_mrr_drop = base["transcript"]["mrr"] - stressed["transcript"]["mrr"]
    assert transcript_mrr_drop > 0

    # The report must include Kestrel's delta even when it is negative (honest
    # unfavorable result, BENCH-004).
    deltas = report["degradation"]["recency"]["mrr"]
    assert "kestrel" in deltas
    assert "delta" in deltas["kestrel"]


def test_kestrel_arm_queries_all_eligible_layers_no_oracle() -> None:
    """BENCH-002 carried forward: the breadth Kestrel arm never receives the
    ground-truth layer label."""
    from unittest.mock import patch

    from nested_memvid_agent.layers import LayeredMemorySystem
    from nested_memvid_agent.models import MemoryLayer, RetrievalQuery

    captured: list[RetrievalQuery] = []

    original_retrieve = LayeredMemorySystem.retrieve

    def spy_retrieve(self, query: RetrievalQuery) -> list:
        captured.append(query)
        return original_retrieve(self, query)

    with patch.object(LayeredMemorySystem, "retrieve", spy_retrieve):
        run_breadth_benchmark(seeds=(42,), k_values=(3,), checkpoints=(0,), scenarios=("conflict", "update"))

    assert captured, "no RetrievalQuery captured"
    for query in captured:
        assert query.layers == tuple(MemoryLayer), f"oracle layer leaked: {query.layers}"
        assert query.k_per_layer == 3


def test_breadth_cli_writes_machine_readable_report(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    output = tmp_path / "memory-breadth.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_benchmark_breadth.py",
            "--seeds",
            "42",
            "--k-values",
            "3",
            "--checkpoints",
            "0",
            "--scenarios",
            "baseline",
            "common_term",
            "--output",
            str(output),
        ],
    )

    result = main()

    assert result == 0
    assert output.exists()
    payload = output.read_text(encoding="utf-8")
    assert '"schema": "kestrel.memory_benchmark.v3"' in payload
    assert '"fixture_digest"' in payload
    assert '"raw_rows"' in payload
    assert json.loads(payload)["acceptance"]["passed"] is True


def test_corpus_scenarios_are_deterministic_and_grounded() -> None:
    """Every scenario corpus is deterministic and every expected id exists."""
    for scenario in SCENARIOS:
        a = build_breadth_corpus(scenario, seed=42, checkpoint=2)
        b = build_breadth_corpus(scenario, seed=42, checkpoint=2)
        assert [d["id"] for d in a.documents] == [d["id"] for d in b.documents]
        assert [q.query for q in a.queries] == [q.query for q in b.queries]
        doc_ids = {d["id"] for d in a.documents}
        for q in a.queries:
            for expected in q.expected_doc_ids:
                assert expected in doc_ids, (
                    f"{scenario}: expected id {expected} not in corpus for {q.query}"
                )
        # Growth checkpoints actually grow the corpus.
        c0 = build_breadth_corpus(scenario, seed=42, checkpoint=0)
        assert len(build_breadth_corpus(scenario, seed=42, checkpoint=1).documents) > len(
            c0.documents
        )
        assert len(build_breadth_corpus(scenario, seed=42, checkpoint=2).documents) > len(
            build_breadth_corpus(scenario, seed=42, checkpoint=1).documents
        )


def test_common_term_scenario_returns_evidence_for_ubiquitous_terms() -> None:
    """A query made of ubiquitous terms must still return evidence for every
    arm (the smoothed non-negative IDF repair is carried into breadth)."""
    report = run_breadth_benchmark(
        seeds=(42,), k_values=(3,), checkpoints=(0,), scenarios=("common_term",)
    )
    cell = report["cells"]["common_term"]["42"]["0"]["3"]
    for row in cell["raw_rows"]:
        if "rate limit" in row["query"] or "webhook" in row["query"]:
            assert row["evidence"] is True, f"no evidence for ubiquitous-term query: {row['query']}"
            assert row["recall_at_k"] > 0.0
