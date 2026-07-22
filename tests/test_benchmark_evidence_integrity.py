from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from benchmarks import agent_benchmark
from benchmarks import error_recovery_benchmark as recovery_benchmark
from benchmarks import unified_memory_benchmark as unified


class _CannedAnswerAgent:
    """Answers every prompt correctly without ever executing a tool."""

    def __init__(self) -> None:
        self._turn = 0
        self.memory = SimpleNamespace()

    def chat(self, _prompt: str, session_id: str, **_kwargs: Any) -> Any:
        self._turn += 1
        answers = {
            "bench_memory_write": "Saved your favorite color, teal.",
            "bench_memory_recall": "Your favorite color is teal.",
            "bench_file_read": "The secret code is 42.",
            "bench_repo_search": "fruits.txt contains banana.",
            "bench_git_status": "new_feature.py is untracked.",
        }
        return SimpleNamespace(
            session_id=session_id,
            assistant_message=answers[session_id],
            tool_executions=(),
            context_prompt="User's favorite color is teal.",
        )


@pytest.mark.parametrize(
    "task",
    (
        agent_benchmark._task_memory_persistence,
        agent_benchmark._task_file_read_qa,
        agent_benchmark._task_repo_search,
        agent_benchmark._task_git_status,
    ),
)
def test_agent_task_rejects_canned_answer_without_expected_tool(
    tmp_path: Path,
    task: Any,
) -> None:
    result = task(_CannedAnswerAgent(), tmp_path)

    assert result["success"] is False


@pytest.mark.parametrize(
    "module",
    (agent_benchmark, recovery_benchmark),
)
@pytest.mark.parametrize(
    ("rows", "total_tasks", "success_count"),
    (
        ([], 0, 0),
        ([{"task": "failed", "success": False}], 1, 0),
        ([{"task": "failed", "success": False}], 1, 1),
    ),
)
def test_agent_style_benchmark_main_rejects_empty_failed_or_inconsistent_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    rows: list[dict[str, Any]],
    total_tasks: int,
    success_count: int,
) -> None:
    result = {
        "schema": "test",
        "config": {},
        "summary": {
            "total_tasks": total_tasks,
            "success_count": success_count,
        },
        "results": rows,
    }
    runner_name = (
        "run_agent_benchmark" if module is agent_benchmark else "run_error_recovery_benchmark"
    )
    monkeypatch.setattr(module, runner_name, lambda **_kwargs: result)
    output = tmp_path / f"{module.__name__.rsplit('.', 1)[-1]}.json"
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--output", str(output)])

    assert module.main() == 1
    assert output.exists()


def test_deterministic_agent_benchmark_proves_exact_tools_and_fresh_recall() -> None:
    result = agent_benchmark.run_agent_benchmark(provider="mock")

    assert result["summary"]["passed"] is True
    rows = {row["task"]: row for row in result["results"]}
    assert rows["memory_persistence"]["current_tool_record"] is True
    assert rows["memory_persistence"]["fresh_session"] is True
    assert rows["memory_persistence"]["record_surfaced_in_recall_tool"] is True
    assert all(row["expected_tool_succeeded"] for row in rows.values())


def test_deterministic_recovery_benchmark_proves_injected_failures_and_bounds() -> None:
    result = recovery_benchmark.run_error_recovery_benchmark(provider="mock")

    assert result["summary"]["passed"] is True
    rows = {row["task"]: row for row in result["results"]}
    assert rows["transient_file_read"]["recovered_after_injection"] is True
    assert rows["strategy_retry"]["retry_allowed"] is True
    assert rows["strategy_retry"]["retry_decision"]["retry_allowed"] is True
    assert rows["max_retries_exceeded"]["nonzero_bounded_failures"] is True
    assert 0 < rows["max_retries_exceeded"]["file_read_attempts"] <= 5
    assert result["summary"]["total_errors_injected"] == 9


class _EmptyRetriever:
    def name(self) -> str:
        return "empty-retriever"

    def ingest(self, _doc_id: str, _text: str, _layer: str | None = None) -> None:
        return None

    def retrieve(self, _query: str, k: int = 5, layer: str | None = None) -> list[Any]:
        del k, layer
        return []


@pytest.mark.parametrize("required", (True, False))
def test_unified_benchmark_fails_executing_empty_retriever(
    monkeypatch: pytest.MonkeyPatch,
    required: bool,
) -> None:
    corpus = SimpleNamespace(
        documents=[{"id": "expected", "text": "durable memory", "layer": "semantic"}],
        queries=[
            SimpleNamespace(
                query="durable memory",
                layer="semantic",
                expected_doc_ids=["expected"],
            )
        ],
    )
    monkeypatch.setattr(unified, "build_large_memory_corpus", lambda *, seed: corpus)
    monkeypatch.setattr(
        unified,
        "_backend_specs",
        lambda: (unified._BackendSpec("empty-retriever", _EmptyRetriever, required),),
    )

    result = unified.run_unified_benchmark(k=1, seed=7)

    row = result["results"][0]
    assert row["status"] == "failed"
    assert row["stage"] == "quality_gate"
    assert row["quality_gate"]["passed"] is False
    assert row["quality_gate"]["observed"] == {
        "recall_at_k": 0.0,
        "precision_at_k": 0.0,
        "mrr": 0.0,
    }
    assert result["summary"]["success"] is False


def test_unified_backend_specs_use_versioned_nonzero_quality_floors() -> None:
    specs = unified._backend_specs()

    assert specs
    assert {spec.quality_floor.version for spec in specs} == {
        "kestrel.unified-memory-quality-floor.v1"
    }
    for spec in specs:
        assert spec.quality_floor.min_recall_at_k > 0
        assert spec.quality_floor.min_precision_at_k > 0
        assert spec.quality_floor.min_mrr > 0
