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
    repo_root = _git_output(workspace, "rev-parse", "--show-toplevel")
    _git_output(Path(repo_root), "rev-parse", "--verify", "HEAD")
    safe_run = _safe_ref_part(run_id)
    safe_worker = _safe_ref_part(worker_id)
    safe_prefix = "/".join(_safe_ref_part(part) for part in branch_prefix.split("/") if part)
    branch = f"{safe_prefix}/{safe_run}/{safe_worker}" if safe_prefix else f"kestrel-worker/{safe_run}/{safe_worker}"
    target = (worktree_root / safe_run / safe_worker).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / ".git").exists():
        return WorkerIsolation(workspace=target, branch=branch, worker_id=worker_id)
    _git_output(Path(repo_root), "worktree", "add", "-b", branch, str(target), "HEAD")
    return WorkerIsolation(workspace=target, branch=branch, worker_id=worker_id)


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
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit_code={completed.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _safe_ref_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", value.strip())
    cleaned = cleaned.strip("./-")
    cleaned = cleaned.replace("..", ".")
    return cleaned or "worker"
