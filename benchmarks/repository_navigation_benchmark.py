"""Deterministic multi-language repository-navigation quality gate.

The benchmark builds the production repository index over a small Python,
TypeScript, Go, Rust, Java, Kotlin, and Swift fixture. It then asks natural
language navigation questions through ``repo.context_pack`` and measures the
ranked, digest-bound evidence returned by the production tool.

Usage:
    python benchmarks/repository_navigation_benchmark.py \
      --output benchmark_results/repository_navigation.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.repo_index import RepositoryIndex
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.repo_intelligence_tools import RepoContextPackTool

_PROJECT_ID = "benchmark.repository-navigation"
_DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "repo_index"
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class NavigationCase:
    case_id: str
    language: str
    query: str
    expected_paths: tuple[str, ...]


CASES = (
    NavigationCase(
        "python_definition",
        "python",
        "Where is the Widget class defined?",
        ("src/widget.py",),
    ),
    NavigationCase(
        "typescript_definition",
        "typescript",
        "Locate the WebWidget implementation.",
        ("web/widget.ts",),
    ),
    NavigationCase(
        "go_definition",
        "go",
        "Find the BuildWidget function.",
        ("cmd/widget.go",),
    ),
    NavigationCase(
        "rust_definition",
        "rust",
        "Where is the RustWidget type defined?",
        ("crates/widget.rs",),
    ),
    NavigationCase(
        "java_definition",
        "java",
        "Show the JavaWidget class definition.",
        ("jvm/Widget.java",),
    ),
    NavigationCase(
        "kotlin_definition",
        "kotlin",
        "Locate the KotlinWidget implementation.",
        ("jvm/Widget.kt",),
    ),
    NavigationCase(
        "swift_definition",
        "swift",
        "Where is the SwiftWidget type defined?",
        ("apple/Widget.swift",),
    ),
    NavigationCase(
        "test_ownership",
        "python",
        "Which tests exercise Widget?",
        ("tests/widget_checks.py",),
    ),
    NavigationCase(
        "typescript_import",
        "typescript",
        "Where is format imported?",
        ("web/widget.ts",),
    ),
    NavigationCase(
        "swift_import",
        "swift",
        "Where is Foundation imported?",
        ("apple/Widget.swift",),
    ),
    NavigationCase(
        "cross_file_reference",
        "python",
        "Where does helper get referenced?",
        ("src/widget.py", "tests/widget_checks.py"),
    ),
)


def _copy_fixture(source: Path, target: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {".nest", "__pycache__"} or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(source, target, ignore=ignore)


def _context(base: Path, repository: Path, index_digest: str) -> ToolContext:
    memory = build_memory_system("memory", base / "memory")
    config = AgentConfig(
        workspace=repository,
        project_id=_PROJECT_ID,
        project_allowed_paths=(".",),
    )
    context = ToolContext(
        memory=memory,
        config=config,
        workspace=repository,
        project_id=_PROJECT_ID,
        allowed_paths=(".",),
    )
    context.project_revision = 1
    context.project_baseline_index_digest = index_digest
    return context


def _ranked_paths(payload: dict[str, Any], *, limit: int = 5) -> list[str]:
    paths: list[str] = []
    for raw in payload.get("evidence", []):
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        if isinstance(path, str) and path not in paths:
            paths.append(path)
        if len(paths) == limit:
            break
    return paths


def _evidence_is_bound(payload: dict[str, Any]) -> bool:
    evidence = payload.get("evidence")
    return bool(
        payload.get("authoritative") is True
        and payload.get("freshness") == "current"
        and _SHA256.fullmatch(str(payload.get("index_digest", "")))
        and isinstance(evidence, list)
        and evidence
        and all(
            isinstance(item, dict)
            and isinstance(item.get("line"), int)
            and int(item["line"]) >= 1
            and _SHA256.fullmatch(str(item.get("file_digest", "")))
            for item in evidence
        )
    )


def _evaluate_case(
    case: NavigationCase,
    *,
    tool: RepoContextPackTool,
    context: ToolContext,
) -> dict[str, Any]:
    arguments = {"query": case.query, "max_chars": 20_000, "line_window": 3}
    first = tool.run(arguments, context)
    replay = tool.run(arguments, context)
    payload = first.data
    ranked_paths = _ranked_paths(payload)
    expected = set(case.expected_paths)
    hits = expected.intersection(ranked_paths)
    reciprocal_rank = next(
        (
            1.0 / rank
            for rank, path in enumerate(ranked_paths, start=1)
            if path in expected
        ),
        0.0,
    )
    return {
        "case_id": case.case_id,
        "language": case.language,
        "query": case.query,
        "expected_paths": list(case.expected_paths),
        "ranked_paths_at_5": ranked_paths,
        "recall_at_5": len(hits) / len(expected),
        "precision_at_5": len(hits) / max(1, len(ranked_paths)),
        "reciprocal_rank": reciprocal_rank,
        "authoritative_evidence": first.success and _evidence_is_bound(payload),
        "deterministic_replay": (
            first.success == replay.success
            and first.error == replay.error
            and first.content == replay.content
            and first.data == replay.data
        ),
    }


def run_repository_navigation_benchmark(
    fixture: Path = _DEFAULT_FIXTURE,
    *,
    minimum_recall_at_5: float = 0.70,
) -> dict[str, Any]:
    if not 0.0 <= minimum_recall_at_5 <= 1.0:
        raise ValueError("minimum_recall_at_5 must be between zero and one")
    fixture = fixture.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="kestrel-repo-navigation-") as raw_tmp:
        base = Path(raw_tmp)
        repository = base / "repository"
        _copy_fixture(fixture, repository)
        build = RepositoryIndex(
            project_id=_PROJECT_ID,
            repository_root=repository,
        ).rebuild()
        context = _context(base, repository, build.aggregate_digest)
        tool = RepoContextPackTool()
        results = [
            _evaluate_case(case, tool=tool, context=context)
            for case in CASES
        ]

    recall_at_5 = sum(float(item["recall_at_5"]) for item in results) / len(results)
    precision_at_5 = sum(float(item["precision_at_5"]) for item in results) / len(results)
    mean_reciprocal_rank = sum(
        float(item["reciprocal_rank"]) for item in results
    ) / len(results)
    evidence_coverage = sum(
        1 for item in results if item["authoritative_evidence"] is True
    ) / len(results)
    deterministic = all(item["deterministic_replay"] is True for item in results)
    passed = (
        recall_at_5 >= minimum_recall_at_5
        and evidence_coverage == 1.0
        and deterministic
    )
    return {
        "schema": "kestrel.repository_navigation_benchmark.v1",
        "case_count": len(results),
        "languages": sorted({case.language for case in CASES}),
        "minimum_recall_at_5": minimum_recall_at_5,
        "metrics": {
            "recall_at_5": recall_at_5,
            "precision_at_5": precision_at_5,
            "mean_reciprocal_rank": mean_reciprocal_rank,
            "authoritative_evidence_coverage": evidence_coverage,
        },
        "deterministic_replay": deterministic,
        "cases": results,
        "passed": passed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Kestrel's deterministic repository-navigation quality gate."
    )
    parser.add_argument("--fixture", type=Path, default=_DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--minimum-recall-at-5", type=float, default=0.70)
    arguments = parser.parse_args()
    report = run_repository_navigation_benchmark(
        arguments.fixture,
        minimum_recall_at_5=arguments.minimum_recall_at_5,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
