from __future__ import annotations

import json
import sys
from pathlib import Path

from pytest import MonkeyPatch

from benchmarks.repository_navigation_benchmark import (
    main,
    run_repository_navigation_benchmark,
)


def test_repository_navigation_benchmark_clears_quality_and_evidence_gates() -> None:
    report = run_repository_navigation_benchmark()

    assert report["passed"] is True
    assert report["case_count"] == 11
    assert report["languages"] == [
        "go",
        "java",
        "kotlin",
        "python",
        "rust",
        "swift",
        "typescript",
    ]
    assert report["metrics"]["recall_at_5"] >= 0.70
    assert report["metrics"]["authoritative_evidence_coverage"] == 1.0
    assert report["deterministic_replay"] is True
    assert all(case["ranked_paths_at_5"] for case in report["cases"])


def test_repository_navigation_cli_writes_machine_readable_report(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    output = tmp_path / "repository-navigation.json"
    monkeypatch.setattr(sys, "argv", ["benchmark", "--output", str(output)])

    main()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema"] == "kestrel.repository_navigation_benchmark.v1"
    assert report["passed"] is True
