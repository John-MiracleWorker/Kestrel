from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nested_memvid_agent import mission_control as mission_control_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.mission_control import (
    GitInspection,
    IndexInspection,
    ProviderInspection,
    build_mission_preflight,
    inspect_git_worktree,
    inspect_provider_readiness,
)
from nested_memvid_agent.projects import ProjectRecord
from nested_memvid_agent.routing.ledger_records import (
    ModelTargetEntry,
    ProviderProfileEntry,
    RoutePolicyEntry,
)
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.routing.runtime import AdaptiveFlockRuntimeConfig
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import ToolSpec


def test_preflight_is_deterministic_and_truthful_about_warnings() -> None:
    project = _project()
    capabilities = _capabilities(
        "repo.map",
        "repo.symbols",
        "repo.impact",
        "repo.tests_for",
        "repo.context_pack",
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


def test_git_inspection_fails_closed_when_status_command_fails(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    responses = iter(
        [
            ("true", 0),
            ("main", 0),
            ("", 128),
        ]
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        mission_control_module,
        "_git_first_line",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        mission_control_module,
        "trusted_git_executable",
        lambda: Path("/usr/bin/git"),
    )

    inspection = inspect_git_worktree(repository)

    assert inspection.state == "unknown"
    assert "status 128" in inspection.summary


def test_git_probe_drains_bounded_output_until_the_process_finishes(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    script = (
        "import sys, time; "
        "print('first', flush=True); "
        "sys.stdout.write('discarded-output\\n' * 10000); "
        "sys.stdout.flush(); "
        "time.sleep(0.35)"
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        mission_control_module,
        "hardened_readonly_git_command",
        lambda _arguments: [sys.executable, "-c", script],
    )

    line, return_code = mission_control_module._git_first_line(
        tmp_path,
        ("status",),
        deadline=mission_control_module.monotonic() + 2.0,
    )

    assert line == "first"
    assert return_code == 0


def test_preflight_requires_parent_capability_for_mcp_tool() -> None:
    project = _project()
    project = ProjectRecord(
        **{
            **project.__dict__,
            "capability_ceiling": ("tool:mcp.demo.read",),
        }
    )
    result = build_mission_preflight(
        project=project,
        objective="Explain the repository.",
        template_id="explain_repository",
        git=GitInspection(branch="main", state="clean", summary="Working tree is clean."),
        index=IndexInspection(freshness="current", detail="Current."),
        provider=ProviderInspection(
            status="pass",
            detail="Ready.",
            route_policy="local",
            estimated_cost_usd=0.0,
        ),
        capability_catalog=[
            {
                "key": "tool:repo.map",
                "effective_enabled": True,
                "parent_key": None,
            },
            {
                "key": "tool:repo.symbols",
                "effective_enabled": True,
                "parent_key": None,
            },
            {
                "key": "tool:repo.dependencies",
                "effective_enabled": True,
                "parent_key": None,
            },
            {
                "key": "tool:repo.references",
                "effective_enabled": True,
                "parent_key": None,
            },
            {
                "key": "tool:repo.tests_for",
                "effective_enabled": True,
                "parent_key": None,
            },
            {
                "key": "tool:repo.context_pack",
                "effective_enabled": True,
                "parent_key": None,
            },
            {
                "key": "tool:file.read",
                "effective_enabled": True,
                "parent_key": None,
            },
            {
                "key": "tool:mcp.demo.read",
                "effective_enabled": True,
                "parent_key": "mcp_server:demo",
            },
            {
                "key": "mcp_server:demo",
                "effective_enabled": True,
                "parent_key": None,
            },
        ],
        generated_at=datetime(2026, 7, 27, 16, 30, tzinfo=UTC),
    )

    assert "tool:mcp.demo.read" not in result["effective_capabilities"]


def test_live_project_gate_requires_mcp_parent_capability(tmp_path: Path) -> None:
    project = ProjectRecord(
        **{
            **_project().__dict__,
            "repository_path": str(tmp_path.resolve()),
            "capability_ceiling": ("tool:mcp.demo.read",),
        }
    )
    manager = object.__new__(RunManager)
    manager.capabilities = SimpleNamespace(
        tool_decision=lambda _spec: SimpleNamespace(
            effective_enabled=True,
            blocked_by=(),
        )
    )
    manager.state = SimpleNamespace(get_project=lambda _project_id: project)
    manager.config = AgentConfig(
        workspace=tmp_path,
        project_id=project.project_id,
    )
    spec = ToolSpec(
        name="mcp.demo.read",
        description="Read through demo MCP",
        parameters={"type": "object"},
        source="mcp",
        server_id="demo",
    )

    allowed, detail = manager._capability_gate(spec)

    assert allowed is False
    assert "mcp_server:demo" in detail


def test_routed_preflight_uses_production_contracts_and_selected_cost() -> None:
    project = ProjectRecord(
        **{
            **_project(cost_budget=0.1).__dict__,
            "provider_policy": {
                "policy_id": "balanced",
                "direct_estimated_cost_usd": 0.0,
            },
        }
    )
    profile = ProviderProfile(
        profile_id="local",
        display_name="Local",
        adapter="mock",
        locality="local",
    )
    cheap = ModelTarget(
        target_id="cheap",
        provider_profile_id="local",
        provider="mock",
        model="cheap",
        locality="local",
        supports_tools=True,
        supports_reasoning=True,
        supports_json=True,
        max_context_tokens=64_000,
        quality_tier=1,
        estimated_cost_usd=0.01,
        health="healthy",
    )
    strong = ModelTarget(
        target_id="strong",
        provider_profile_id="local",
        provider="mock",
        model="strong",
        locality="local",
        supports_tools=True,
        supports_reasoning=True,
        supports_json=True,
        max_context_tokens=64_000,
        quality_tier=5,
        estimated_cost_usd=1.0,
        health="healthy",
    )

    inspection = inspect_provider_readiness(
        project=project,
        config=AgentConfig(provider="mock", model="mock"),
        routing_config=AdaptiveFlockRuntimeConfig(
            enabled=True,
            mode="constrained",
            policy_id="balanced",
        ),
        provider_profiles=[
            ProviderProfileEntry(
                profile=profile,
                revision=1,
                created_at="2026-07-27T00:00:00+00:00",
                updated_at="2026-07-27T00:00:00+00:00",
            )
        ],
        model_targets=[
            ModelTargetEntry(
                target=cheap,
                revision=1,
                created_at="2026-07-27T00:00:00+00:00",
                updated_at="2026-07-27T00:00:00+00:00",
            ),
            ModelTargetEntry(
                target=strong,
                revision=1,
                created_at="2026-07-27T00:00:00+00:00",
                updated_at="2026-07-27T00:00:00+00:00",
            ),
        ],
        route_policies=[
            RoutePolicyEntry(
                policy=RoutePolicy(policy_id="balanced"),
                enabled=True,
                revision=1,
                created_at="2026-07-27T00:00:00+00:00",
                updated_at="2026-07-27T00:00:00+00:00",
            )
        ],
        template_id="fix_failing_test",
    )

    assert inspection.status == "fail"
    assert inspection.estimated_cost_usd is None
    assert "production-equivalent route" in inspection.detail.lower()


def test_actionable_routing_does_not_require_the_direct_provider_to_be_eligible() -> None:
    project = ProjectRecord(
        **{
            **_project(cost_budget=0.1).__dict__,
            "provider_policy": {
                "policy_id": "balanced",
                "preset": "local_only",
            },
        }
    )
    profile = ProviderProfile(
        profile_id="local",
        display_name="Local",
        adapter="mock",
        locality="local",
    )
    target = ModelTarget(
        target_id="local-validated",
        provider_profile_id="local",
        provider="mock",
        model="validated",
        locality="local",
        supports_tools=True,
        supports_reasoning=True,
        supports_json=True,
        max_context_tokens=64_000,
        quality_tier=5,
        estimated_cost_usd=0.01,
        health="healthy",
    )

    inspection = inspect_provider_readiness(
        project=project,
        config=AgentConfig(provider="openai", model="cloud-model"),
        routing_config=AdaptiveFlockRuntimeConfig(
            enabled=True,
            mode="constrained",
            policy_id="balanced",
        ),
        provider_profiles=[
            ProviderProfileEntry(
                profile=profile,
                revision=1,
                created_at="2026-07-27T00:00:00+00:00",
                updated_at="2026-07-27T00:00:00+00:00",
            )
        ],
        model_targets=[
            ModelTargetEntry(
                target=target,
                revision=1,
                created_at="2026-07-27T00:00:00+00:00",
                updated_at="2026-07-27T00:00:00+00:00",
            )
        ],
        route_policies=[
            RoutePolicyEntry(
                policy=RoutePolicy(policy_id="balanced"),
                enabled=True,
                revision=1,
                created_at="2026-07-27T00:00:00+00:00",
                updated_at="2026-07-27T00:00:00+00:00",
            )
        ],
        template_id="explain_repository",
    )

    assert inspection.status == "pass"
    assert inspection.estimated_cost_usd == pytest.approx(0.03)
    assert inspection.route_policy == "balanced"
    assert "local-validated" in inspection.detail


def _project(*, cost_budget: float | None = 2.0) -> ProjectRecord:
    capability_ceiling = tuple(
        f"tool:{name}"
        for name in (
            "repo.map",
            "repo.symbols",
            "repo.impact",
            "repo.tests_for",
            "repo.context_pack",
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
