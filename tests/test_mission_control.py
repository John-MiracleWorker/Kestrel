from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from nested_memvid_agent.mission_control import (
    GitInspection,
    IndexInspection,
    ProviderInspection,
    build_mission_preflight,
    inspect_git_worktree,
)
from nested_memvid_agent.projects import ProjectRecord


def test_preflight_is_deterministic_and_truthful_about_warnings() -> None:
    project = _project()
    capabilities = _capabilities(
        "repo.map",
        "repo.search",
        "file.read",
        "repair.prepare",
        "repair.apply_patch",
        "repair.validate",
        "repair.review",
    )

    first = build_mission_preflight(
        project=project,
        objective="Fix the failing authentication test without changing the public API.",
        template_id="fix_failing_test",
        git=GitInspection(
            branch="feature/local",
            state="dirty",
            summary="Tracked changes and untracked files are present.",
        ),
        index=IndexInspection(
            freshness="stale",
            digest="sha256:old",
            indexed_at="2026-07-26T10:00:00+00:00",
            detail="The repository changed after the last index build.",
        ),
        provider=ProviderInspection(
            status="pass",
            detail="One healthy local target is eligible.",
            route_policy="privacy-first",
            estimated_cost_usd=0.25,
        ),
        capability_catalog=capabilities,
        generated_at=datetime(2026, 7, 27, 16, 30, tzinfo=UTC),
    )
    second = build_mission_preflight(
        project=project,
        objective="Fix the failing authentication test without changing the public API.",
        template_id="fix_failing_test",
        git=GitInspection(
            branch="feature/local",
            state="dirty",
            summary="Tracked changes and untracked files are present.",
        ),
        index=IndexInspection(
            freshness="stale",
            digest="sha256:old",
            indexed_at="2026-07-26T10:00:00+00:00",
            detail="The repository changed after the last index build.",
        ),
        provider=ProviderInspection(
            status="pass",
            detail="One healthy local target is eligible.",
            route_policy="privacy-first",
            estimated_cost_usd=0.25,
        ),
        capability_catalog=capabilities,
        generated_at=datetime(2026, 7, 27, 16, 30, tzinfo=UTC),
    )

    assert first == second
    assert first["schema"] == "kestrel.mission_preflight.v1"
    assert first["working_tree"]["state"] == "dirty"
    assert first["index"]["freshness"] == "stale"
    assert first["budget"] == {"currency": "USD", "limit": 2.0, "estimate": 0.25}
    assert first["can_start"] is True
    assert first["blockers"] == []
    assert first["likely_approvals"] == [
        "tool:repair.apply_patch",
        "tool:repair.prepare",
        "tool:repair.review",
        "tool:repair.validate",
    ]
    assert [task["task_id"] for task in first["tasks"]] == [
        "understand",
        "repair",
        "validate",
        "review",
    ]
    assert first["tasks"][1]["dependencies"] == ["understand"]
    assert any("working tree" in item.lower() for item in first["warnings"])
    assert any("index" in item.lower() for item in first["warnings"])


def test_preflight_blocks_missing_required_capability_and_budget_overrun() -> None:
    project = _project(cost_budget=0.1)
    capabilities = _capabilities("repo.map", "repo.search", "file.read")

    result = build_mission_preflight(
        project=project,
        objective="Implement the requested feature.",
        template_id="implement_feature",
        git=GitInspection(branch="main", state="clean", summary="Working tree is clean."),
        index=IndexInspection(
            freshness="current",
            digest="sha256:index",
            indexed_at="2026-07-27T15:00:00+00:00",
            detail="Index matches the current repository snapshot.",
        ),
        provider=ProviderInspection(
            status="pass",
            detail="Validated target ready.",
            route_policy="balanced",
            estimated_cost_usd=0.5,
        ),
        capability_catalog=capabilities,
        generated_at=datetime(2026, 7, 27, 16, 30, tzinfo=UTC),
    )

    assert result["can_start"] is False
    assert result["budget"]["estimate"] == 0.5
    assert any("budget" in item.lower() for item in result["blockers"])
    assert any("required capabilities" in item.lower() for item in result["blockers"])
    capability_check = next(
        check for check in result["checks"] if check["check_id"] == "capabilities"
    )
    assert capability_check["status"] == "fail"


def test_git_inspection_detects_dirty_state_without_refreshing_index(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "kestrel@example.invalid")
    _git(repository, "config", "user.name", "Kestrel Test")
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "baseline")
    index_path = repository / ".git" / "index"
    before = index_path.stat()

    (repository / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")
    inspection = inspect_git_worktree(repository, timeout_seconds=2.0)
    after = index_path.stat()

    assert inspection.branch == "main"
    assert inspection.state == "dirty"
    assert "tracked changes" in inspection.summary.lower()
    assert "untracked files" in inspection.summary.lower()
    assert (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) == (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_size,
    )


def _project(*, cost_budget: float | None = 2.0) -> ProjectRecord:
    capability_ceiling = tuple(
        f"tool:{name}"
        for name in (
            "repo.map",
            "repo.search",
            "file.read",
            "repair.prepare",
            "repair.apply_patch",
            "repair.validate",
            "repair.review",
        )
    )
    return ProjectRecord(
        project_id="project_kestrel",
        display_name="Kestrel",
        repository_path="/tmp/kestrel",
        remote=None,
        default_branch="main",
        allowed_paths=(".",),
        provider_policy={"preset": "privacy-first"},
        cost_budget=cost_budget,
        privacy_class="local_required",
        test_recipes=({"name": "unit", "command": "pytest -q"},),
        build_recipes=(),
        capability_ceiling=capability_ceiling,
        baseline_index_digest="sha256:old",
        revision=1,
        archived_at=None,
        created_at="2026-07-27T12:00:00+00:00",
        updated_at="2026-07-27T12:00:00+00:00",
    )


def _capabilities(*tool_names: str) -> list[dict[str, object]]:
    return [
        {
            "key": f"tool:{name}",
            "name": name,
            "effective_enabled": True,
            "requires_approval": name.startswith("repair."),
            "risk": "high" if name == "repair.apply_patch" else "medium",
        }
        for name in tool_names
    ]


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
