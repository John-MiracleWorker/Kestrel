from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.engineering.github_workflow import (
    GitHubWorkflowService,
    _github_url,
)
from nested_memvid_agent.repair_integrity import (
    repair_snapshot,
    write_repair_artifact,
    write_validation_receipt,
)
from nested_memvid_agent.state_store import AgentStateStore


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[AgentStateStore, Path, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "kestrel@example.test")
    _git(repo, "config", "user.name", "Kestrel Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "switch", "-c", "kestrel/worker/parser")
    (repo / "README.md").write_text("reviewed candidate\n", encoding="utf-8")
    snapshot = repair_snapshot(repo)
    validation = write_validation_receipt(
        repo,
        tool_name="repair.validate",
        command=["pytest", "-q"],
        success=True,
        returncode=0,
        content="1 passed",
        validation_evidence={"test_refs": ["pytest"]},
        snapshot=snapshot,
        started_at="2026-07-27T12:00:00+00:00",
        isolation_attestation={
            "schema_version": 1,
            "mode": "oci_snapshot_v1",
            "image": "example.invalid/kestrel-validation@sha256:" + "a" * 64,
            "network": "none",
            "workspace_mount": "private_read_only_snapshot",
            "host_fallback": False,
            "source_tree_digest": "sha256:" + "b" * 64,
            "repair_diff_digest": snapshot["diff_digest"],
            "repair_head_sha": snapshot["head_sha"],
            "repair_branch": snapshot["branch"],
        },
    )
    review_id = "repair_review_" + "c" * 24
    write_repair_artifact(
        repo,
        "repair_reviews",
        review_id,
        {
            "schema_version": 2,
            "review_id": review_id,
            "validation_id": validation["validation_id"],
            "branch": snapshot["branch"],
            "head_sha": snapshot["head_sha"],
            "diff_hash": snapshot["diff_digest"],
            "diff_digest": snapshot["diff_digest"],
            "changed_files": snapshot["changed_files"],
            "repair_snapshot": snapshot,
            "summary": "Repair the parser contract.",
            "risks": ["Parser callers should be monitored."],
            "validation": {
                "validation_id": validation["validation_id"],
                "tool": "repair.validate",
                "success": True,
                "returncode": 0,
            },
            "commit_gate": {
                "commit_allowed": True,
                "approval_required_before_commit": True,
            },
        },
    )
    state = AgentStateStore(tmp_path / "state.sqlite3")
    state.create_project(
        project_id="project_repo",
        display_name="Repository",
        repository_path=repo,
        active_capability_keys=(),
    )
    state.create_run(
        run_id="run_repair",
        message="Repair the parser without changing its public contract.",
        session_id="session",
        workspace=str(repo),
        provider="mock",
        model="mock",
        project_id="project_repo",
    )
    return state, repo, review_id


class _GitHubRunner:
    def __init__(self, head_sha: str) -> None:
        self.head_sha = head_sha
        self.commands: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        self.commands.append(command)
        names = [Path(item).name if index == 0 else item for index, item in enumerate(command)]
        if names[0] == "git" and names[1:4] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, "https://github.com/acme/repo.git\n", "")
        if names[0] == "git" and names[1] == "push":
            return subprocess.CompletedProcess(command, 0, "pushed\n", "")
        if names[0] == "gh" and "statusCheckRollup" in names[-1]:
            payload: dict[str, Any] = {
                "number": 42,
                "url": "https://github.com/acme/repo/pull/42",
                "state": "OPEN",
                "reviewDecision": "",
                "headRefOid": self.head_sha,
                "statusCheckRollup": [
                    {"name": "test", "conclusion": "FAILURE"},
                ],
                "comments": [{"author": {"login": "ci"}, "body": "Tests failed."}],
                "reviews": [],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if names[0] == "gh" and names[1:3] == ["pr", "view"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/acme/repo/pull/42",
                        "state": "OPEN",
                        "headRefOid": self.head_sha,
                    }
                ),
                "",
            )
        raise AssertionError(f"Unexpected command: {names}")


def test_pr_workflow_binds_reviewed_commit_and_ingests_failed_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nested_memvid_agent.engineering.github_workflow.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" else "/usr/bin/gh",
    )
    state, repo, review_id = _fixture(tmp_path)
    service = GitHubWorkflowService(state)

    uncommitted = service.prepare(
        request_id="github_uncommitted",
        run_id="run_repair",
        review_id=review_id,
        title="Repair parser",
        base_branch="main",
        head_branch=None,
        actor="owner",
    )
    assert uncommitted.reviewed_commit_sha is None
    with pytest.raises(ValueError, match="committed locally"):
        service.publish(
            uncommitted.request_id,
            expected_request_digest=uncommitted.request_digest,
        )

    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "repair parser")
    commit_sha = _git(repo, "rev-parse", "HEAD")
    runner = _GitHubRunner(commit_sha)
    service = GitHubWorkflowService(state, runner=runner)
    request = service.prepare(
        request_id="github_parser",
        run_id="run_repair",
        review_id=review_id,
        title="Repair parser",
        base_branch="main",
        head_branch="kestrel/worker/parser",
        actor="owner",
    )

    assert request.reviewed_commit_sha == commit_sha
    assert request.to_payload()["publish_tool_request"]["requires_exact_call_approval"] is True
    assert "Signed validation" in request.body
    with pytest.raises(ValueError, match="digest changed"):
        service.publish(
            request.request_id,
            expected_request_digest="0" * 64,
        )

    published = service.publish(
        request.request_id,
        expected_request_digest=request.request_digest,
    )
    assert published.status == "published"
    assert published.external_number == 42
    assert published.external_url == "https://github.com/acme/repo/pull/42"
    assert any(Path(command[0]).name == "git" and "push" in command for command in runner.commands)

    synced = service.sync(
        request.request_id,
        expected_request_digest=request.request_digest,
    )
    assert synced.status == "ci_failed"
    assert synced.feedback[-1].status == "ci_failed"
    assert synced.feedback[-1].payload["comments"][0]["body"] == "Tests failed."

    state.create_run(
        run_id="run_ci_recovery",
        message="Recover failed CI.",
        session_id="recovery",
        workspace=str(repo),
        provider="mock",
        model="mock",
        project_id="project_repo",
    )
    recovered = service.bind_recovery_run(
        request.request_id,
        recovery_run_id="run_ci_recovery",
    )
    assert recovered.recovery_run_id == "run_ci_recovery"


def test_pr_preparation_rejects_a_commit_that_expands_reviewed_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nested_memvid_agent.engineering.github_workflow.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" else "/usr/bin/gh",
    )
    state, repo, review_id = _fixture(tmp_path)
    (repo / "extra.txt").write_text("scope expansion\n", encoding="utf-8")
    _git(repo, "add", "README.md", "extra.txt")
    _git(repo, "commit", "-m", "expanded repair")

    with pytest.raises(ValueError, match="changed-file set"):
        GitHubWorkflowService(state).prepare(
            request_id="github_expanded",
            run_id="run_repair",
            review_id=review_id,
            title="Expanded repair",
            base_branch="main",
            head_branch=None,
            actor="owner",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@github.com/acme/repo/pull/42",
        "https://github.com/acme/repo/pull/42?token=redacted",
        "https://github.com/acme/repo/issues/42",
        "https://github.com/acme/repo/pull/0",
    ],
)
def test_github_pull_request_url_rejects_credentials_and_non_pr_shapes(
    url: str,
) -> None:
    with pytest.raises(ValueError, match="invalid pull request URL"):
        _github_url(url)
