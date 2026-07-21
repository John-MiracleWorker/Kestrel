from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nested_memvid_agent.worker_isolation import prepare_git_worktree


def test_existing_worker_worktree_is_reused_only_when_repo_and_branch_match(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    worktree_root = tmp_path / "worktrees"
    first = prepare_git_worktree(
        workspace=repo,
        worktree_root=worktree_root,
        branch_prefix="kestrel/worker",
        run_id="run-1",
        worker_id="worker-1",
    )
    (first.workspace / "worker.txt").write_text("persistent\n", encoding="utf-8")

    second = prepare_git_worktree(
        workspace=repo,
        worktree_root=worktree_root,
        branch_prefix="kestrel/worker",
        run_id="run-1",
        worker_id="worker-1",
    )

    assert second == first
    assert (second.workspace / "worker.txt").read_text(encoding="utf-8") == "persistent\n"


def test_existing_worker_target_from_another_repository_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    target = tmp_path / "worktrees" / "run-2" / "worker-2"
    target.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "kestrel/worker/run-2/worker-2", str(target)],
        cwd=other,
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(RuntimeError, match="repository mismatch"):
        prepare_git_worktree(
            workspace=repo,
            worktree_root=tmp_path / "worktrees",
            branch_prefix="kestrel/worker",
            run_id="run-2",
            worker_id="worker-2",
        )


def test_existing_worker_worktree_on_unexpected_branch_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    worktree_root = tmp_path / "worktrees"
    isolation = prepare_git_worktree(
        workspace=repo,
        worktree_root=worktree_root,
        branch_prefix="kestrel/worker",
        run_id="run-3",
        worker_id="worker-3",
    )
    subprocess.run(
        ["git", "switch", "-c", "unexpected/branch"],
        cwd=isolation.workspace,
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(RuntimeError, match="branch mismatch"):
        prepare_git_worktree(
            workspace=repo,
            worktree_root=worktree_root,
            branch_prefix="kestrel/worker",
            run_id="run-3",
            worker_id="worker-3",
        )


def test_non_git_worker_target_is_never_reused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    target = tmp_path / "worktrees" / "run-4" / "worker-4"
    target.mkdir(parents=True)
    (target / "user-data.txt").write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="exists without git metadata"):
        prepare_git_worktree(
            workspace=repo,
            worktree_root=tmp_path / "worktrees",
            branch_prefix="kestrel/worker",
            run_id="run-4",
            worker_id="worker-4",
        )
    assert (target / "user-data.txt").read_text(encoding="utf-8") == "do not overwrite\n"


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "kestrel@example.test"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Kestrel Test"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return path
