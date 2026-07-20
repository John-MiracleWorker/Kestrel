from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.file_lock import lock_exclusive, unlock
from nested_memvid_agent.models import MemoryHit, MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from nested_memvid_agent.task_capsule import (
    enforce_task_capsule_retention,
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
    assert (path.parent / "capsule.complete.json").exists()


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


def test_summarizes_capsule_through_backend_without_snapshot_file(tmp_path: Path) -> None:
    path = write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="run_5",
        objective="Read via backend",
        final_response="Done",
        candidate_facts=("Capsule summaries should read complete.mv2 through the backend.",),
    )
    path.with_suffix(".memory.json").unlink()

    summary = summarize_run_capsule(runs_dir=tmp_path / "runs", run_id="run_5")

    assert "Objective: Read via backend" in summary.summary
    assert summary.learning_signals


def test_capsule_root_links_to_candidate_frames(tmp_path: Path) -> None:
    path = write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="run_6",
        objective="Link capsule frames",
        errors_encountered=("Tool shell.run failed with approval_required.",),
        candidate_procedures=("Validated repeatable steps should be stored as skill cards.",),
    )

    raw = path.with_suffix(".memory.json").read_text(encoding="utf-8")

    assert '"frame_type": "task_summary"' in raw
    assert '"frame_type": "failure_note"' in raw
    assert '"frame_type": "skill_card"' in raw
    assert '"parent_ids": [' in raw
    assert '"child_ids": [' in raw


def test_capsule_redacts_secrets_from_arguments_outputs_and_candidates(tmp_path: Path) -> None:
    secret = "opaque-capsule-secret-12345"
    path = write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="run_redacted",
        objective=f"Investigate api_key={secret}",
        tool_executions=(
            ToolExecution(
                call=ToolCall(
                    name="diagnosis.classify",
                    arguments={"failure_text": f"Authorization: Bearer {secret}"},
                ),
                success=False,
                content=f"client_secret: {secret}",
                data={"token": secret},
                error="diagnostic_failure",
            ),
        ),
        errors_encountered=(f"password={secret}",),
    )

    raw = path.with_suffix(".memory.json").read_text(encoding="utf-8")
    assert secret not in raw
    assert "<redacted>" in raw


def test_summarize_prefers_exact_iter_records_before_snippet_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import nested_memvid_agent.task_capsule as task_capsule

    run_id = "run_exact_index"
    capsule_path = tmp_path / "runs" / run_id / "complete.mv2"
    capsule_path.parent.mkdir(parents=True)
    capsule_path.touch()
    payload = (
        '{"run_id":"run_exact_index","objective":"Exact index capsule",'
        '"final_assistant_response":"Done",'
        '"candidate_facts":["Exact iter_records payload should win over snippets."]}'
    )

    class FakeCapsuleBackend:
        def iter_records(self) -> list[MemoryRecord]:
            return [
                MemoryRecord(
                    id=f"capsule_{run_id}",
                    title=f"Run capsule: {run_id}",
                    content=payload,
                    layer=MemoryLayer.EPISODIC,
                    kind=MemoryKind.SUMMARY,
                )
            ]

        def find(self, query: str, k: int = 8) -> list[MemoryHit]:
            return []

        def close(self) -> None:
            return None

    monkeypatch.setattr(task_capsule, "_open_capsule_backend", lambda path, backend: FakeCapsuleBackend())

    summary = summarize_run_capsule(runs_dir=tmp_path / "runs", run_id=run_id, backend="memvid")

    assert "Objective: Exact index capsule" in summary.summary
    assert len(summary.learning_signals) == 1


def test_task_capsule_retention_keeps_latest_and_explicitly_preserved_run(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    run_paths = {
        run_id: write_run_capsule(
            runs_dir=runs_dir,
            run_id=run_id,
            objective=f"Complete {run_id}",
        )
        for run_id in ("oldest", "middle", "newest")
    }
    for index, run_id in enumerate(("oldest", "middle", "newest"), start=1):
        run_dir = run_paths[run_id].parent
        data_timestamp = 1_700_000_000 + index * 10
        for artifact in run_dir.iterdir():
            if artifact.name != "capsule.complete.json":
                os.utime(artifact, (data_timestamp, data_timestamp))
        marker_timestamp = data_timestamp + 1
        os.utime(
            run_dir / "capsule.complete.json",
            (marker_timestamp, marker_timestamp),
        )
    root_lock = runs_dir / ".middle.kestrel-memory.lock"
    root_lock.touch(mode=0o600)

    report = enforce_task_capsule_retention(
        runs_dir=runs_dir,
        retention_count=2,
        preserve_run_ids=("oldest",),
    )

    assert report.completed_run_count == 3
    assert report.retained_run_ids == ("newest", "oldest")
    assert report.deleted_run_ids == ("middle",)
    assert report.deleted_artifact_count == 5
    assert report.reclaimed_bytes > 0
    assert run_paths["oldest"].parent.exists()
    assert not run_paths["middle"].parent.exists()
    assert run_paths["newest"].parent.exists()
    assert not root_lock.exists()
    assert report.to_payload()["deleted_run_ids"] == ["middle"]


def test_task_capsule_retention_accepts_a_sealed_legacy_memory_snapshot(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    legacy = write_run_capsule(
        runs_dir=runs_dir,
        run_id="legacy",
        objective="Legacy sealed snapshot",
        candidate_facts=("The child frame must also be sealed.",),
    )
    latest = write_run_capsule(
        runs_dir=runs_dir,
        run_id="latest",
        objective="Latest marked capsule",
    )
    (legacy.parent / "capsule.complete.json").unlink()
    for artifact in legacy.parent.iterdir():
        os.utime(artifact, (1_600_000_000, 1_600_000_000))

    report = enforce_task_capsule_retention(runs_dir=runs_dir, retention_count=1)

    assert report.deleted_run_ids == ("legacy",)
    assert report.retained_run_ids == ("latest",)
    assert not legacy.parent.exists()
    assert latest.parent.exists()


def test_concurrent_task_capsule_retention_passes_treat_disappearing_candidates_as_skips(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    for index in range(16):
        write_run_capsule(
            runs_dir=runs_dir,
            run_id=f"run-{index:02d}",
            objective=f"Concurrent retention {index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        reports = tuple(
            pool.map(
                lambda _index: enforce_task_capsule_retention(
                    runs_dir=runs_dir,
                    retention_count=5,
                ),
                range(8),
            )
        )

    retained_directories = tuple(path for path in runs_dir.iterdir() if path.is_dir())
    assert len(retained_directories) == 5
    assert all(report.retention_count == 5 for report in reports)


@pytest.mark.skipif(os.name == "nt", reason="POSIX link and advisory-lock safety coverage")
def test_task_capsule_retention_skips_active_partial_unknown_and_linked_artifacts(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    write_run_capsule(
        runs_dir=runs_dir,
        run_id="latest-safe",
        objective="Retain latest",
    )
    active = write_run_capsule(
        runs_dir=runs_dir,
        run_id="active-run",
        objective="Do not delete while open",
    )
    unknown = write_run_capsule(
        runs_dir=runs_dir,
        run_id="unknown-run",
        objective="Do not delete unknown files",
    )
    (unknown.parent / "operator-note.txt").write_text("preserve me", encoding="utf-8")
    hardlinked = write_run_capsule(
        runs_dir=runs_dir,
        run_id="hardlinked-run",
        objective="Do not delete hardlinks",
    )
    outside_hardlink = tmp_path / "outside-hardlink.json"
    os.link(hardlinked.with_suffix(".memory.json"), outside_hardlink)
    partial = runs_dir / "partial-run"
    partial.mkdir(mode=0o700)
    (partial / "complete.mv2").touch(mode=0o600)
    (partial / ".complete.mv2.kestrel.lock").touch(mode=0o600)
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    (runs_dir / "symlink-run").symlink_to(outside_directory, target_is_directory=True)

    active_lock_path = active.parent / ".complete.mv2.kestrel.lock"
    with active_lock_path.open("r+", encoding="utf-8") as active_lock:
        lock_exclusive(active_lock)
        try:
            report = enforce_task_capsule_retention(runs_dir=runs_dir, retention_count=1)
        finally:
            unlock(active_lock)

    reasons = {item.run_id: item.reason for item in report.skipped}
    assert reasons == {
        "active-run": "active_capsule",
        "hardlinked-run": "unsafe_capsule_artifact",
        "partial-run": "partial_capsule",
        "symlink-run": "unsafe_run_directory",
        "unknown-run": "unknown_capsule_artifact",
    }
    assert report.deleted_run_ids == ()
    assert report.retained_run_ids == ("latest-safe",)
    assert active.parent.exists()
    assert unknown.parent.exists()
    assert hardlinked.parent.exists()
    assert partial.exists()
    assert (runs_dir / "symlink-run").is_symlink()
    assert outside_directory.exists()
    assert outside_hardlink.exists()


def test_task_capsule_retention_config_defaults_validates_and_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert AgentConfig().task_capsule_retention_count == 1_000
    monkeypatch.setenv("NEST_AGENT_TASK_CAPSULE_RETENTION_COUNT", "37")
    assert AgentConfig.from_env().task_capsule_retention_count == 37
    with pytest.raises(ValueError, match="task_capsule_retention_count"):
        AgentConfig(task_capsule_retention_count=0)
    with pytest.raises(ValueError, match="task_capsule_retention_count"):
        AgentConfig(task_capsule_retention_count=True)
