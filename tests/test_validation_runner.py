from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.extension_runner import (
    ContainerExecutionRequest,
    ContainerExecutionResult,
)
from nested_memvid_agent.validation_runner import (
    ValidationIsolationError,
    run_isolated_validation,
)

PINNED_IMAGE = "example.invalid/kestrel-validation@sha256:" + "a" * 64


class _InspectingRunner:
    def __init__(self, *, mutate: Path | None = None) -> None:
        self.mutate = mutate
        self.requests: list[ContainerExecutionRequest] = []

    def run(self, request: ContainerExecutionRequest) -> ContainerExecutionResult:
        self.requests.append(request)
        snapshot = request.source_dir
        assert (snapshot / "tracked.py").read_text(encoding="utf-8") == "print('tracked')\n"
        assert (snapshot / "new.txt").read_text(encoding="utf-8") == "untracked\n"
        assert not (snapshot / ".git").exists()
        assert not (snapshot / ".nest").exists()
        assert request.scopes.network == "none"
        assert request.scopes.secrets == ()
        if self.mutate is not None:
            self.mutate.write_text("raced\n", encoding="utf-8")
        return ContainerExecutionResult(
            success=True,
            stdout="isolated\n",
            stderr="",
            returncode=0,
            content="Container execution completed.",
            tree_digest=request.expected_tree_digest,
            scope_digest=request.scopes.digest(),
        )


def test_validation_runner_uses_private_exact_candidate_and_attests_isolation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    private = repo / ".nest"
    private.mkdir()
    (private / "repair_receipt_signing.v2.key").write_bytes(b"x" * 32)
    runner = _InspectingRunner()

    result = run_isolated_validation(
        workspace=repo,
        image=PINNED_IMAGE,
        command=["python", "tracked.py"],
        timeout_seconds=5,
        runner=runner,
    )

    assert result.returncode == 0
    assert result.stdout == "isolated\n"
    assert result.isolation["mode"] == "oci_snapshot_v1"
    assert result.isolation["host_fallback"] is False
    assert result.isolation["image"] == PINNED_IMAGE
    assert result.isolation["source_tree_digest"].startswith("sha256:")
    assert len(runner.requests) == 1
    assert not runner.requests[0].source_dir.exists()


def test_validation_runner_has_no_host_fallback_without_pinned_image(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    runner = _InspectingRunner()

    with pytest.raises(ValidationIsolationError) as raised:
        run_isolated_validation(
            workspace=repo,
            image=None,
            command=["python", "tracked.py"],
            timeout_seconds=5,
            runner=runner,
        )

    assert raised.value.code == "validation_container_required"
    assert runner.requests == []


def test_validation_image_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_VALIDATION_CONTAINER_IMAGE", PINNED_IMAGE)

    assert AgentConfig.from_env().validation_container_image == PINNED_IMAGE


def test_validation_runner_rejects_candidate_race_after_container(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    candidate = repo / "tracked.py"
    runner = _InspectingRunner(mutate=candidate)

    with pytest.raises(ValidationIsolationError) as raised:
        run_isolated_validation(
            workspace=repo,
            image=PINNED_IMAGE,
            command=["python", "tracked.py"],
            timeout_seconds=5,
            runner=runner,
        )

    assert raised.value.code == "validation_candidate_changed"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "fix/validation-container"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Kestrel Test"],
        cwd=repo,
        check=True,
    )
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (repo / "tracked.py").write_text("print('tracked')\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", "tracked.py"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"],
        cwd=repo,
        check=True,
    )
    return repo
