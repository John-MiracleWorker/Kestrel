from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest import MonkeyPatch

from benchmarks import memory_benchmark, memory_benchmark_large
from benchmarks import unified_memory_benchmark as unified
from benchmarks.adapters.base import OptionalDependencyUnavailable
from benchmarks.adapters.chroma_adapter import ChromaAdapter
from benchmarks.adapters.kestrel_adapter import KestrelAdapter
from benchmarks.adapters.qdrant_adapter import QdrantAdapter
from benchmarks.adapters.tfidf_adapter import TFIDFAdapter
from benchmarks.adapters.vector_rag import VectorRAG


@pytest.mark.parametrize(
    "script_name",
    ("memory_benchmark.py", "memory_benchmark_large.py", "unified_memory_benchmark.py"),
)
def test_benchmark_entrypoint_help_runs_outside_repository(
    tmp_path: Path,
    script_name: str,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(repository / "benchmarks" / script_name), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.lower()
    assert "ModuleNotFoundError" not in completed.stderr


@pytest.mark.parametrize(
    ("factory", "missing_dependency", "backend_name"),
    (
        (VectorRAG, "sentence_transformers", VectorRAG.BACKEND_NAME),
        (QdrantAdapter, "qdrant_client", QdrantAdapter.BACKEND_NAME),
        (ChromaAdapter, "chromadb", ChromaAdapter.BACKEND_NAME),
    ),
)
def test_optional_adapter_reports_missing_dependency_at_construction(
    monkeypatch: MonkeyPatch,
    factory: type[Any],
    missing_dependency: str,
    backend_name: str,
) -> None:
    real_import = builtins.__import__

    def controlled_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == missing_dependency or name.startswith(f"{missing_dependency}."):
            raise ModuleNotFoundError(
                f"No module named {missing_dependency!r}",
                name=missing_dependency,
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", controlled_import)

    with pytest.raises(OptionalDependencyUnavailable) as captured:
        factory()

    assert captured.value.backend_name == backend_name
    assert captured.value.missing_dependency == missing_dependency
    assert "pip install" in captured.value.install_hint


def test_unified_benchmark_runs_required_backends_and_skips_missing_optional(
    monkeypatch: MonkeyPatch,
) -> None:
    corpus = SimpleNamespace(
        documents=[
            {
                "id": "semantic_1",
                "text": "Kestrel stores layered memory locally.",
                "layer": "semantic",
            }
        ],
        queries=[
            SimpleNamespace(
                query="Where does Kestrel store memory?",
                layer="semantic",
                expected_doc_ids=["semantic_1"],
            )
        ],
    )

    def unavailable(name: str, dependency: str) -> Any:
        def factory() -> Any:
            raise OptionalDependencyUnavailable(
                name,
                missing_dependency=dependency,
                install_hint=f"python -m pip install {dependency}",
            )

        return factory

    specs = (
        unified._BackendSpec(
            KestrelAdapter.LEXICAL_BACKEND_NAME,
            lambda: KestrelAdapter(hybrid=False),
            True,
        ),
        unified._BackendSpec(TFIDFAdapter.BACKEND_NAME, TFIDFAdapter, True),
        unified._BackendSpec(
            VectorRAG.BACKEND_NAME,
            unavailable(VectorRAG.BACKEND_NAME, "sentence_transformers"),
            False,
        ),
        unified._BackendSpec(
            QdrantAdapter.BACKEND_NAME,
            unavailable(QdrantAdapter.BACKEND_NAME, "qdrant_client"),
            False,
        ),
        unified._BackendSpec(
            ChromaAdapter.BACKEND_NAME,
            unavailable(ChromaAdapter.BACKEND_NAME, "chromadb"),
            False,
        ),
    )
    monkeypatch.setattr(unified, "build_large_memory_corpus", lambda *, seed: corpus)
    monkeypatch.setattr(unified, "_backend_specs", lambda: specs)

    result = unified.run_unified_benchmark(k=1, seed=7)

    assert result["summary"] == {
        "passed": 2,
        "skipped": 3,
        "failed": 0,
        "required_failed": 0,
        "success": True,
    }
    assert set(result["comparison"]) == {
        KestrelAdapter.LEXICAL_BACKEND_NAME,
        TFIDFAdapter.BACKEND_NAME,
    }
    statuses = {row["name"]: row["status"] for row in result["results"]}
    assert statuses[KestrelAdapter.LEXICAL_BACKEND_NAME] == "passed"
    assert statuses[TFIDFAdapter.BACKEND_NAME] == "passed"
    assert statuses[VectorRAG.BACKEND_NAME] == "skipped"
    assert statuses[QdrantAdapter.BACKEND_NAME] == "skipped"
    assert statuses[ChromaAdapter.BACKEND_NAME] == "skipped"


@pytest.mark.parametrize(("success", "expected_exit"), ((True, 0), (False, 1)))
def test_unified_main_exit_status_tracks_benchmark_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    success: bool,
    expected_exit: int,
) -> None:
    status = "skipped" if success else "failed"
    result = {
        "schema": "test",
        "config": {},
        "comparison": {},
        "results": [
            {
                "name": "optional",
                "status": status,
                "required": False,
                **(
                    {
                        "missing_dependency": "optional-package",
                        "skip_reason": "missing_optional_dependency",
                    }
                    if success
                    else {"error": "benchmark failed"}
                ),
            }
        ],
        "summary": {
            "passed": 2 if success else 1,
            "skipped": 1 if success else 0,
            "failed": 0 if success else 1,
            "required_failed": 0,
            "success": success,
        },
    }
    output = tmp_path / f"unified-{success}.json"
    monkeypatch.setattr(unified, "run_unified_benchmark", lambda **_kwargs: result)
    monkeypatch.setattr(
        sys,
        "argv",
        ["unified_memory_benchmark.py", "--output", str(output)],
    )

    assert unified.main() == expected_exit
    assert output.exists()


def _zero_quality_memory_result() -> dict[str, Any]:
    zero_metrics = {
        "recall_at_k": 0.0,
        "precision_at_k": 0.0,
        "mrr": 0.0,
        "avg_latency_ms": 0.0,
        "p99_latency_ms": 0.0,
    }
    return {
        "schema": "test",
        "config": {"total_queries": 1},
        "overall": {
            "kestrel": dict(zero_metrics),
            "baseline": dict(zero_metrics),
        },
        "query_details": {
            "kestrel": [
                {
                    "recall_at_k": 0.0,
                    "precision_at_k": 0.0,
                    "mrr": 0.0,
                    "retrieved_ids": [],
                }
            ],
            "baseline": [],
        },
    }


@pytest.mark.parametrize("module", (memory_benchmark, memory_benchmark_large))
def test_memory_benchmark_main_rejects_zero_quality_results(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    module: Any,
) -> None:
    output = tmp_path / f"{module.__name__.rsplit('.', 1)[-1]}.json"
    monkeypatch.setattr(
        module,
        "run_memory_benchmark",
        lambda **_kwargs: _zero_quality_memory_result(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [f"{module.__name__}.py", "--output", str(output)],
    )

    assert module.main() == 1
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["acceptance"]["passed"] is False
    assert all(value > 0 for value in written["acceptance"]["minimums"].values())


@pytest.mark.parametrize("module", (memory_benchmark, memory_benchmark_large))
def test_memory_quality_gate_rejects_empty_query_evidence_even_with_high_metrics(
    module: Any,
) -> None:
    result = _zero_quality_memory_result()
    result["overall"]["kestrel"].update({"recall_at_k": 1.0, "precision_at_k": 1.0, "mrr": 1.0})
    result["overall"]["baseline"].update({"recall_at_k": 1.0, "precision_at_k": 1.0, "mrr": 1.0})
    result["query_details"]["kestrel"] = []

    gate = module._evaluate_quality_gate(result)

    assert gate["checks"]["nonempty_query_evidence"] is False
    assert gate["passed"] is False
