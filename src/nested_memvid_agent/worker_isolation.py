from __future__ import annotations

import re
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerIsolation:
    workspace: Path
    branch: str
    worker_id: str
    mode: str = "git-worktree"

    def to_payload(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "workspace": str(self.workspace),
            "branch": self.branch,
            "worker_id": self.worker_id,
        }


def prepare_git_worktree(
    *,
    workspace: Path,
    worktree_root: Path,
    branch_prefix: str,
    run_id: str,
    worker_id: str,
) -> WorkerIsolation:
    """Create a persistent branch/worktree for an isolated worker."""
    repo_root = Path(_git_output(workspace, "rev-parse", "--show-toplevel")).resolve()
    base_sha = _git_output(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    common_dir = _resolved_git_path(repo_root, "rev-parse", "--git-common-dir")
    safe_run = _safe_ref_part(run_id)
    safe_worker = _safe_ref_part(worker_id)
    safe_prefix = "/".join(_safe_ref_part(part) for part in branch_prefix.split("/") if part)
    branch = (
        f"{safe_prefix}/{safe_run}/{safe_worker}"
        if safe_prefix
        else f"kestrel-worker/{safe_run}/{safe_worker}"
    )
    _git_output(repo_root, "check-ref-format", "--branch", branch)
    resolved_worktree_root = worktree_root.resolve()
    target = (resolved_worktree_root / safe_run / safe_worker).resolve()
    if resolved_worktree_root not in target.parents:
        raise RuntimeError("worker worktree target escapes configured worktree root")
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / ".git").exists():
        return _verified_existing_worktree(
            target=target,
            expected_branch=branch,
            expected_common_dir=common_dir,
            worker_id=worker_id,
        )
    if target.exists():
        raise RuntimeError(f"worker worktree target exists without git metadata: {target}")
    _git_output(repo_root, "worktree", "add", "-b", branch, str(target), base_sha)
    return _verified_existing_worktree(
        target=target,
        expected_branch=branch,
        expected_common_dir=common_dir,
        worker_id=worker_id,
    )


def _verified_existing_worktree(
    *,
    target: Path,
    expected_branch: str,
    expected_common_dir: Path,
    worker_id: str,
) -> WorkerIsolation:
    actual_root = Path(_git_output(target, "rev-parse", "--show-toplevel")).resolve()
    if actual_root != target:
        raise RuntimeError(f"worker worktree root mismatch: expected {target}, found {actual_root}")
    actual_common_dir = _resolved_git_path(target, "rev-parse", "--git-common-dir")
    if actual_common_dir != expected_common_dir:
        raise RuntimeError(
            "worker worktree repository mismatch: "
            f"expected {expected_common_dir}, found {actual_common_dir}"
        )
    actual_branch = _git_output(target, "symbolic-ref", "--quiet", "--short", "HEAD")
    if actual_branch != expected_branch:
        raise RuntimeError(
            f"worker worktree branch mismatch: expected {expected_branch}, found {actual_branch}"
        )
    _git_output(target, "rev-parse", "--verify", "HEAD^{commit}")
    return WorkerIsolation(
        workspace=target,
        branch=expected_branch,
        worker_id=worker_id,
    )


def _resolved_git_path(cwd: Path, *args: str) -> Path:
    raw = Path(_git_output(cwd, *args))
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def _git_output(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed executable with argument vector  # nosec
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"exit_code={completed.returncode}"
        )
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _safe_ref_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("./-")
    cleaned = cleaned.replace("..", ".")
    return cleaned or "worker"
