from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import benchmarks.learning_benchmark as learning_benchmark


def test_aggregate_memory_benchmark_runs_with_truthful_fixture_seed_mode(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "benchmark-report.json"

    completed = subprocess.run(
        [
            sys.executable,
            "benchmarks/run_all.py",
            "--memory-only",
            "--output",
            str(output),
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    config = report["memory"]["config"]
    assert config["backend"] == "in_memory"
    assert config["synthetic_fixture_seed_mode"] == "direct_non_promotion"
    assert report["memory"]["overall"]["kestrel"]["recall_at_k"] >= 0
    quality_gate = report["memory_quality_gate"]
    assert quality_gate["version"] == "kestrel.aggregate-memory-quality-floor.v1"
    assert all(minimum > 0 for minimum in quality_gate["minimums"].values())
    assert quality_gate["passed"] is True
    assert all(
        report["acceptance"]["assertions"][name]
        for name in (
            "memory_recall_at_k_at_or_above_absolute_floor",
            "memory_precision_at_k_at_or_above_absolute_floor",
            "memory_mrr_at_or_above_absolute_floor",
        )
    )
    assert report["acceptance"]["passed"] is True


def test_learning_benchmark_uses_validated_sink_and_passes_all_gates() -> None:
    report = learning_benchmark.run_learning_benchmark(seed=42)

    assert report["passed"] is True
    assert all(report["assertions"].values())
    dimensions = {dimension["name"]: dimension for dimension in report["dimensions"]}
    assert dimensions["promotion_gate_conformance"]["evidence_mode"] == (
        "synthetic_declared_gate_conformance_no_memory_write"
    )
    assert dimensions["promotion_gate_conformance"]["expected_label_source"] == (
        "declared_kernel_contract_not_independent_outcomes"
    )
    router = dimensions["router_calibration"]
    assert router["evaluation_mode"] == "synthetic_held_out_projected_utility"
    assert router["counterfactual_outcome_ground_truth"] is False
    assert set(router["training_promotion_ids"]).isdisjoint(router["evaluation_promotion_ids"])
    assert router["evaluation_examples"] > 0
    assert "expected_utility_delta" not in router
    assert "promotion_accuracy" not in dimensions
    assert dimensions["procedural_consolidation"]["evidence_mode"] == (
        "authenticated_runtime_receipts_and_validated_sink"
    )
    assert dimensions["procedural_consolidation"]["formation_rate"] == 1.0
    assert dimensions["procedural_consolidation"]["retrieval_rate"] == 1.0


def test_learning_benchmark_cli_exits_nonzero_when_a_gate_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "failed-learning.json"
    monkeypatch.setattr(
        learning_benchmark,
        "run_learning_benchmark",
        lambda *, seed: {
            "schema": "kestrel.learning_benchmark.v1",
            "config": {"seed": seed},
            "dimensions": [],
            "assertions": {"injected_failure": False},
            "passed": False,
            "summary": {},
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["learning_benchmark.py", "--output", str(output)],
    )

    assert learning_benchmark.main() == 1
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is False
