from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.mission_control import mission_launch_binding_matches
from nested_memvid_agent.server import create_app
from nested_memvid_agent.state_store import AgentStateStore


def _test_token() -> str:
    return "mission-binding-token-7f3a1c99"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        workspace=tmp_path,
        state_path=tmp_path / "agent.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        secret_store_path=tmp_path / "secrets.json",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        mcp_config_path=tmp_path / "mcp.json",
        channel_config_path=tmp_path / "channels.json",
        require_api_auth=True,
        api_auth_token_env="KESTREL_MISSION_BINDING_TEST_TOKEN",
        enable_agentic_cycle=False,
        allow_file_write=True,
        allow_shell=True,
    )


def _admit_mission_run(
    tmp_path: Path,
    headers: dict[str, str],
) -> tuple[dict, dict, dict]:
    """Preflight and launch one mission run; return (preflight, launch_binding, run)."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("# Repository\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c",
        "user.name=Kestrel",
        "-c",
        "user.email=k@example.invalid",
        "commit",
        "-m",
        "baseline",
    )
    with TestClient(create_app(_config(tmp_path))) as client:
        capabilities = client.get("/api/capabilities", headers=headers).json()["items"]
        active = [item["key"] for item in capabilities if item["effective_enabled"]]
        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "binding_project",
                "display_name": "Binding project",
                "repository_path": str(repository.resolve()),
                "privacy_class": "local_required",
                "provider_policy": {"preset": "local_only"},
                "test_recipes": [{"name": "unit", "command": "pytest -q"}],
                "capability_ceiling": active,
            },
        )
        assert created.status_code == 201
        preflight = client.post(
            "/api/projects/binding_project/mission/preflight",
            headers=headers,
            json={
                "objective": "Explain this repository's architecture and entry points.",
                "template_id": "explain_repository",
            },
        )
        assert preflight.status_code == 200
        preflight_payload = preflight.json()
        assert preflight_payload["can_start"] is True
        binding = preflight_payload["launch_binding"]
        launched = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": preflight_payload["objective"],
                "project_id": "binding_project",
                "autonomy_mode": "manual",
                "mission_plan": preflight_payload["tasks"],
                "project_revision": preflight_payload["project_revision"],
                "mission_template_id": preflight_payload["template_id"],
                "mission_binding": binding,
            },
        )
        assert launched.status_code == 200
        run = launched.json()
        assert run["mission_binding"] == binding
        assert run["mission_preflight"] is not None
    return preflight_payload, binding, run


def test_mission_binding_and_preflight_persist_with_the_admitted_run(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = _test_token()
    monkeypatch.setenv("KESTREL_MISSION_BINDING_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    _preflight, binding, run = _admit_mission_run(tmp_path, headers)

    # Reload the state database in a fresh process-like handle: the accepted
    # binding and preflight must be durably persisted with the run row.
    reloaded = AgentStateStore(tmp_path / "agent.db")
    assert reloaded.schema_version() == 22
    persisted_run = reloaded.get_run(run["run_id"])
    assert persisted_run.mission_binding == binding
    assert persisted_run.mission_preflight is not None
    # The accepted preflight carried the accepted binding (no substitution).
    assert (
        persisted_run.mission_preflight["launch_binding"] == persisted_run.mission_binding
    )


def test_persisted_binding_cannot_be_substituted_after_admission(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = _test_token()
    monkeypatch.setenv("KESTREL_MISSION_BINDING_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    _preflight, binding, run = _admit_mission_run(tmp_path, headers)

    reloaded = AgentStateStore(tmp_path / "agent.db")
    persisted_binding = reloaded.get_run(run["run_id"]).mission_binding
    assert persisted_binding is not None
    assert mission_launch_binding_matches(binding, persisted_binding) is True

    def substituted(**changes: object) -> dict:
        candidate = dict(persisted_binding)
        candidate.update(changes)
        return candidate

    # Project revision cannot be substituted.
    assert (
        mission_launch_binding_matches(
            substituted(project_revision=int(persisted_binding["project_revision"]) + 1),
            persisted_binding,
        )
        is False
    )
    # Objective digest cannot be substituted.
    assert (
        mission_launch_binding_matches(
            substituted(objective_digest="0" * 64),
            persisted_binding,
        )
        is False
    )
    # Plan digest cannot be substituted.
    assert (
        mission_launch_binding_matches(
            substituted(plan_digest="1" * 64),
            persisted_binding,
        )
        is False
    )
    # Preflight digest cannot be substituted.
    assert (
        mission_launch_binding_matches(
            substituted(preflight_digest="2" * 64),
            persisted_binding,
        )
        is False
    )
    # The binding digest itself cannot be substituted while fields stay equal.
    assert (
        mission_launch_binding_matches(
            substituted(binding_digest="3" * 64),
            persisted_binding,
        )
        is False
    )
    # Reopening the database again returns the same immutable binding.
    second_reload = AgentStateStore(tmp_path / "agent.db")
    assert second_reload.get_run(run["run_id"]).mission_binding == persisted_binding
