from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.server import create_app


def test_mission_preflight_is_read_only_and_plan_becomes_project_bound_graph(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "mission-owner-token-8c5e5b66"
    monkeypatch.setenv("KESTREL_MISSION_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    (repository / "README.md").write_text("# Repository\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "-c", "user.name=Kestrel", "-c", "user.email=k@example.invalid", "commit", "-m", "baseline")
    config = _config(tmp_path)

    with TestClient(create_app(config)) as client:
        capabilities = client.get("/api/capabilities", headers=headers).json()["items"]
        active = [item["key"] for item in capabilities if item["effective_enabled"]]
        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "mission_project",
                "display_name": "Mission project",
                "repository_path": str(repository.resolve()),
                "privacy_class": "local_required",
                "provider_policy": {"preset": "local_only"},
                "test_recipes": [{"name": "unit", "command": "pytest -q"}],
                "capability_ceiling": active,
            },
        )
        assert created.status_code == 201
        assert not (repository / ".nest").exists()
        assert client.get("/api/runs", headers=headers).json() == []

        response = client.post(
            "/api/projects/mission_project/mission/preflight",
            headers=headers,
            json={
                "objective": "Explain this repository's architecture and entry points.",
                "template_id": "explain_repository",
            },
        )
        assert response.status_code == 200
        preflight = response.json()
        assert preflight["schema"] == "kestrel.mission_preflight.v1"
        assert preflight["project_id"] == "mission_project"
        assert preflight["working_tree"]["state"] == "clean"
        assert preflight["index"]["freshness"] == "missing"
        assert preflight["can_start"] is True
        assert not (repository / ".nest").exists()
        assert client.get("/api/runs", headers=headers).json() == []
        edited_tasks = [dict(task) for task in preflight["tasks"]]
        edited_tasks[0] = {
            **edited_tasks[0],
            "title": "Map the repository precisely",
            "acceptance_criteria": [
                "Repository boundaries, entry points, and unknowns are cited."
            ],
        }

        launched = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": preflight["objective"],
                "project_id": "mission_project",
                "autonomy_mode": "manual",
                "mission_plan": edited_tasks,
                "project_revision": preflight["project_revision"],
                "mission_template_id": preflight["template_id"],
                "mission_binding": preflight["launch_binding"],
            },
        )
        assert launched.status_code == 200
        run = launched.json()
        assert run["project_id"] == "mission_project"
        assert run["workspace"] == str(repository.resolve())

        graph = client.get(
            f"/api/runs/{run['run_id']}/task-graph",
            headers=headers,
        )
        assert graph.status_code == 200
        tasks = graph.json()["tasks"]
        mission_tasks = [
            task for task in tasks if task.get("plan", {}).get("source") == "mission_control"
        ]
        assert [task["title"] for task in mission_tasks] == [
            item["title"] for item in edited_tasks
        ]
        assert all(task["run_id"] == run["run_id"] for task in mission_tasks)

        (repository / "README.md").write_text(
            "# Repository changed after preflight\n",
            encoding="utf-8",
        )
        stale_repository = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": preflight["objective"],
                "project_id": "mission_project",
                "autonomy_mode": "manual",
                "mission_plan": edited_tasks,
                "project_revision": preflight["project_revision"],
                "mission_template_id": preflight["template_id"],
                "mission_binding": preflight["launch_binding"],
            },
        )
        assert stale_repository.status_code == 409
        assert stale_repository.json()["detail"] == "mission_preflight_binding_stale"

        mismatch = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": "Wrong root",
                "project_id": "mission_project",
                "workspace": str(tmp_path.resolve()),
                "autonomy_mode": "manual",
            },
        )
        assert mismatch.status_code == 400

        unknown = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": "Unknown project",
                "project_id": "missing",
                "autonomy_mode": "manual",
            },
        )
        assert unknown.status_code == 404


def test_mission_launch_rejects_client_risk_downgrade(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "mission-risk-token-1e8c4c8d"
    monkeypatch.setenv("KESTREL_MISSION_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "risk-repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    config = _config(tmp_path)

    with TestClient(create_app(config)) as client:
        capabilities = client.get("/api/capabilities", headers=headers).json()["items"]
        active = [item["key"] for item in capabilities if item["effective_enabled"]]
        assert client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "risk_project",
                "display_name": "Risk project",
                "repository_path": str(repository.resolve()),
                "capability_ceiling": active,
            },
        ).status_code == 201
        preflight = client.post(
            "/api/projects/risk_project/mission/preflight",
            headers=headers,
            json={
                "objective": "Attempt a downgraded repair",
                "template_id": "fix_failing_test",
            },
        ).json()
        assert preflight["can_start"] is True, preflight["blockers"]
        downgraded_plan = [dict(task) for task in preflight["tasks"]]
        downgraded_plan[1] = {
            **downgraded_plan[1],
            "risk": "low",
        }

        response = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": "Attempt a downgraded repair",
                "project_id": "risk_project",
                "autonomy_mode": "manual",
                "project_revision": 1,
                "mission_template_id": "fix_failing_test",
                "mission_binding": preflight["launch_binding"],
                "mission_plan": downgraded_plan,
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "mission_plan_scope_changed_since_preflight"


def test_project_launch_rejects_cloud_override_and_stale_preflight_revision(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "mission-route-token-4af6fb0d"
    monkeypatch.setenv("KESTREL_MISSION_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "route-repository"
    repository.mkdir()

    with TestClient(create_app(_config(tmp_path))) as client:
        capabilities = client.get("/api/capabilities", headers=headers).json()["items"]
        active = [item["key"] for item in capabilities if item["effective_enabled"]]
        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "local_project",
                "display_name": "Local project",
                "repository_path": str(repository.resolve()),
                "privacy_class": "local_required",
                "provider_policy": {"preset": "local_only"},
                "capability_ceiling": active,
            },
        )
        assert created.status_code == 201
        preflight = client.post(
            "/api/projects/local_project/mission/preflight",
            headers=headers,
            json={
                "objective": "Launch a stale mission",
                "template_id": "explain_repository",
            },
        ).json()
        updated = client.put(
            "/api/projects/local_project",
            headers=headers,
            json={"expected_revision": 1, "display_name": "Updated local project"},
        )
        assert updated.status_code == 200

        cloud = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": "Do not send this repository to a cloud provider",
                "project_id": "local_project",
                "provider": "openai",
                "model": "gpt-cloud",
                "autonomy_mode": "manual",
            },
        )
        stale = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": "Launch a stale mission",
                "project_id": "local_project",
                "project_revision": 1,
                "autonomy_mode": "manual",
                "mission_template_id": "explain_repository",
                "mission_binding": preflight["launch_binding"],
                "mission_plan": [
                    {
                        "task_id": "inspect",
                        "title": "Inspect",
                        "rationale": "Read one allowed file.",
                        "dependencies": [],
                        "acceptance_criteria": ["Evidence is cited."],
                        "required_tools": ["file.read"],
                        "risk": "low",
                    }
                ],
            },
        )

    assert cloud.status_code == 400
    assert "local provider" in cloud.json()["detail"]
    assert stale.status_code == 409
    assert stale.json()["detail"] == "mission_preflight_binding_stale"


def test_project_scope_live_gates_tools_and_allowed_paths(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "mission-scope-token-38c75ca1"
    monkeypatch.setenv("KESTREL_MISSION_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "scoped-repository"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "inside.txt").write_text("inside\n", encoding="utf-8")
    (repository / "outside.txt").write_text("outside\n", encoding="utf-8")

    with TestClient(create_app(_config(tmp_path))) as client:
        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "scoped_project",
                "display_name": "Scoped project",
                "repository_path": str(repository.resolve()),
                "allowed_paths": ["src"],
                "capability_ceiling": ["tool:file.read"],
            },
        )
        assert created.status_code == 201
        run = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": "Inspect the allowed project path",
                "project_id": "scoped_project",
                "autonomy_mode": "manual",
            },
        ).json()
        tool_payload = {"run_id": run["run_id"], "session_id": "scope-test"}

        inside = client.post(
            "/api/tools/file.read/invoke",
            headers=headers,
            json={
                **tool_payload,
                "arguments": {"path": "src/inside.txt"},
            },
        ).json()
        outside = client.post(
            "/api/tools/file.read/invoke",
            headers=headers,
            json={
                **tool_payload,
                "arguments": {"path": "outside.txt"},
            },
        ).json()
        beyond_ceiling = client.post(
            "/api/tools/repo.map/invoke",
            headers=headers,
            json={
                **tool_payload,
                "arguments": {"path": "src"},
            },
        ).json()

        assert inside["success"] is True
        assert outside["success"] is False
        assert "allowed paths" in outside["content"]
        assert beyond_ceiling["success"] is False
        assert beyond_ceiling["error"] == "tool_disabled"

        narrowed = client.put(
            "/api/projects/scoped_project",
            headers=headers,
            json={"expected_revision": 1, "capability_ceiling": []},
        )
        assert narrowed.status_code == 200
        revoked_live = client.post(
            "/api/tools/file.read/invoke",
            headers=headers,
            json={
                **tool_payload,
                "arguments": {"path": "src/inside.txt"},
            },
        ).json()

    assert revoked_live["success"] is False
    assert revoked_live["error"] == "tool_disabled"
    assert "project capability ceiling" in revoked_live["content"]


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
        api_auth_token_env="KESTREL_MISSION_TEST_TOKEN",
        enable_agentic_cycle=False,
        allow_file_write=True,
        allow_shell=True,
    )


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
