from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.task_capsule import (
    extract_learning_signals,
    summarize_run_capsule,
    write_run_capsule,
)


def test_writes_capsule_artifact_path(tmp_path: Path) -> None:
    path = write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="run_1",
        objective="Finish context packing",
        final_response="Done",
    )

    assert path == tmp_path / "runs" / "run_1" / "complete.mv2"
    assert path.exists()
    assert path.with_suffix(".memory.json").exists()


def test_extracts_learning_signals() -> None:
    signals = extract_learning_signals(
        {
            "candidate_facts": ["Kestrel stores one .mv2 file per memory layer."],
            "candidate_procedures": ["After memory tool changes, run pytest -q."],
            "candidate_corrections": ["Do not call create(path) on an existing .mv2 file."],
        },
        run_id="run_2",
    )

    assert len(signals) == 3
    assert {signal.kind.value for signal in signals} == {"fact", "procedure", "correction"}


def test_complete_mv2_is_not_a_permanent_layer(tmp_path: Path) -> None:
    path = write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="run_3",
        objective="Capsule only",
        candidate_facts=("complete.mv2 is a run artifact.",),
    )
    summary = summarize_run_capsule(runs_dir=tmp_path / "runs", run_id="run_3")

    assert path.name == "complete.mv2"
    assert path.parent.name == "run_3"
    assert summary.telemetry["is_permanent_layer"] is False
    assert all(signal.source_layer == MemoryLayer.EPISODIC for signal in summary.learning_signals)


def test_summarizes_completed_run_into_candidate_memories(tmp_path: Path) -> None:
    write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="run_4",
        objective="Capture lessons",
        final_response="Use summaries before raw chunks.",
        reusable_lessons=("Context packs should retrieve summaries first.",),
        candidate_facts=("Summaries point back to raw chunks.",),
        candidate_procedures=("Expand raw evidence only when the task needs exact details.",),
    )

    summary = summarize_run_capsule(runs_dir=tmp_path / "runs", run_id="run_4")

    assert "Objective: Capture lessons" in summary.summary
    assert len(summary.learning_signals) == 3
