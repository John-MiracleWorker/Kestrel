from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.repo_index import RepositoryIndex
from nested_memvid_agent.server import create_app


def test_project_setup_draft_route_returns_reviewable_server_evidence(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "project-draft-owner-token-2d90"
    monkeypatch.setenv("KESTREL_PROJECT_DRAFT_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "draft-repository"
    repository.mkdir()
    (repository / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "-b", "trunk"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "package.json"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Kestrel Test",
            "-c",
            "user.email=kestrel@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    with TestClient(
        create_app(_config(tmp_path, token_env="KESTREL_PROJECT_DRAFT_TOKEN"))
    ) as client:
        assert (
            client.post(
                "/api/projects/setup-draft",
                json={"repository_path": str(repository.resolve())},
            ).status_code
            == 401
        )
        response = client.post(
            "/api/projects/setup-draft",
            headers=headers,
            json={"repository_path": str(repository.resolve())},
        )

    assert response.status_code == 200
    draft = response.json()
    assert draft["inspection"]["git"]["branch"] == "trunk"
    assert draft["inspection"]["test_recipes"] == [
        {"name": "npm test", "command": "npm run test"}
    ]
    assert draft["inspection"]["build_recipes"] == [
        {"name": "npm build", "command": "npm run build"}
    ]
    assert draft["create_input"]["provider_policy"] == {
        "preset": "local_only",
        "allowed_providers": ["mock"],
        "allowed_models": ["mock"],
        "direct_estimated_cost_usd": 0.0,
    }
    assert draft["first_mission"]["can_start"] is True
    assert not (repository / ".nest").exists()


@pytest.mark.parametrize(
    (
        "provider",
        "model",
        "draft_costs",
        "expected_privacy",
    ),
    (
        ("ollama", "qwen3", {}, "local_required"),
        (
            "openai",
            "gpt-5.5",
            {
                "direct_estimated_cost_usd": 0.1,
                "cost_budget": 0.5,
            },
            "approved_cloud",
        ),
    ),
)
def test_reviewed_setup_draft_admits_the_real_first_mission_preflight(
    tmp_path: Path,
    monkeypatch: object,
    provider: str,
    model: str,
    draft_costs: dict[str, float],
    expected_privacy: str,
) -> None:
    token = f"project-live-draft-token-{provider}"
    token_env = f"KESTREL_PROJECT_DRAFT_{provider.upper()}_TOKEN".replace(
        "-",
        "_",
    )
    monkeypatch.setenv(token_env, token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / f"{provider}-repository"
    repository.mkdir()
    (repository / "README.md").write_text(
        f"# {provider} project\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Kestrel Test",
            "-c",
            "user.email=kestrel@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    config = replace(
        _config(tmp_path, token_env=token_env),
        provider=provider,
        model=model,
    )

    with TestClient(create_app(config)) as client:
        draft_response = client.post(
            "/api/projects/setup-draft",
            headers=headers,
            json={
                "repository_path": str(repository.resolve()),
                **draft_costs,
            },
        )
        assert draft_response.status_code == 200
        draft = draft_response.json()
        assert draft["first_mission"]["can_start"] is True
        assert draft["create_input"]["privacy_class"] == expected_privacy

        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": f"project_{provider.replace('-', '_')}",
                **draft["create_input"],
            },
        )
        assert created.status_code == 201
        preflight = client.post(
            f"/api/projects/{created.json()['project_id']}/mission/preflight",
            headers=headers,
            json={
                "objective": "Explain this repository.",
                "template_id": "explain_repository",
            },
        )

    assert preflight.status_code == 200
    payload = preflight.json()
    assert payload["can_start"] is True
    assert payload["blockers"] == []


def test_project_api_create_update_export_import_and_archive(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "project-api-owner-token-f82a"
    monkeypatch.setenv("KESTREL_PROJECT_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    with TestClient(create_app(_config(tmp_path, token_env="KESTREL_PROJECT_TEST_TOKEN"))) as client:
        capabilities = client.get("/api/capabilities", headers=headers).json()["items"]
        active = [item["key"] for item in capabilities if item["effective_enabled"]]
        assert active
        create_payload = {
            "project_id": "project_api",
            "display_name": "API project",
            "repository_path": str(first_root.resolve()),
            "allowed_paths": ["src", "tests"],
            "provider_policy": {"preset": "local_only"},
            "cost_budget": 4.0,
            "privacy_class": "local_required",
            "test_recipes": [{"name": "unit", "command": "pytest -q"}],
            "build_recipes": [],
            "capability_ceiling": active[:1],
        }

        assert client.post("/api/projects", json=create_payload).status_code == 401
        created_response = client.post(
            "/api/projects",
            headers=headers,
            json=create_payload,
        )
        assert created_response.status_code == 201
        created = created_response.json()
        assert created["revision"] == 1
        assert created["repository_path"] == str(first_root.resolve())

        stale = client.put(
            "/api/projects/project_api",
            headers=headers,
            json={"expected_revision": 99, "display_name": "stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["error"] == "project_revision_conflict"
        assert stale.json()["detail"]["current"]["revision"] == 1

        updated_response = client.put(
            "/api/projects/project_api",
            headers=headers,
            json={
                "expected_revision": 1,
                "display_name": "Updated API project",
                "test_recipes": [
                    {"name": "focused", "command": "pytest tests/test_projects.py"}
                ],
            },
        )
        assert updated_response.status_code == 200
        assert updated_response.json()["revision"] == 2

        exported = client.get(
            "/api/projects/project_api/export",
            headers=headers,
        )
        assert exported.status_code == 200
        document = exported.json()
        document["project"]["project_id"] = "project_imported"
        document["project"]["repository_path"] = str(second_root.resolve())
        imported = client.post(
            "/api/projects/import",
            headers=headers,
            json={"document": document},
        )
        assert imported.status_code == 201
        assert imported.json()["project_id"] == "project_imported"

        listed = client.get("/api/projects", headers=headers)
        assert [item["project_id"] for item in listed.json()["items"]] == [
            "project_api",
            "project_imported",
        ]

        archived = client.delete(
            "/api/projects/project_api",
            headers=headers,
            params={"expected_revision": 2},
        )
        assert archived.status_code == 200
        assert archived.json()["archived_at"] is not None
        assert client.get("/api/projects/project_api", headers=headers).status_code == 200
        assert [
            item["project_id"]
            for item in client.get("/api/projects", headers=headers).json()["items"]
        ] == ["project_imported"]


def test_project_api_rejects_extra_fields_unknown_capabilities(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "project-api-boundary-token-116b"
    monkeypatch.setenv("KESTREL_PROJECT_BOUNDARY_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"Authorization": f"Bearer {token}"}
    repository = tmp_path / "repository"
    repository.mkdir()

    with TestClient(create_app(_config(tmp_path, token_env="KESTREL_PROJECT_BOUNDARY_TOKEN"))) as client:
        extra = client.post(
            "/api/projects",
            headers=headers,
            json={
                "display_name": "Extra",
                "repository_path": str(repository.resolve()),
                "unexpected": True,
            },
        )
        unknown = client.post(
            "/api/projects",
            headers=headers,
            json={
                "display_name": "Unknown",
                "repository_path": str(repository.resolve()),
                "capability_ceiling": ["tool:not.real"],
            },
        )

    assert extra.status_code == 422
    assert unknown.status_code == 400
    assert "active capability" in unknown.json()["detail"]


@pytest.mark.parametrize(
    "field",
    ("repository_path", "test_recipes", "build_recipes"),
)
def test_project_api_rejects_null_structural_updates(
    tmp_path: Path,
    monkeypatch: object,
    field: str,
) -> None:
    token = "project-api-null-token-c645"
    monkeypatch.setenv("KESTREL_PROJECT_NULL_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "repository"
    repository.mkdir()

    with TestClient(create_app(_config(tmp_path, token_env="KESTREL_PROJECT_NULL_TOKEN"))) as client:
        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "project_null_api",
                "display_name": "Null API",
                "repository_path": str(repository.resolve()),
            },
        )
        assert created.status_code == 201

        rejected = client.put(
            "/api/projects/project_null_api",
            headers=headers,
            json={"expected_revision": 1, field: None},
        )

    assert rejected.status_code == 422


def test_project_api_preserves_explicit_clearing_for_nullable_metadata(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "project-api-clear-token-a756"
    monkeypatch.setenv("KESTREL_PROJECT_CLEAR_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "repository"
    repository.mkdir()

    with TestClient(create_app(_config(tmp_path, token_env="KESTREL_PROJECT_CLEAR_TOKEN"))) as client:
        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "project_clear_api",
                "display_name": "Clear API",
                "repository_path": str(repository.resolve()),
                "remote": "git@github.com:example/repository.git",
                "cost_budget": 5.0,
                "provider_policy": {"preset": "local_only"},
                "baseline_index_digest": "sha256:baseline",
                "test_recipes": [{"name": "unit", "command": "pytest -q"}],
                "build_recipes": [{"name": "build", "command": "python -m build"}],
            },
        )
        assert created.status_code == 201

        cleared = client.put(
            "/api/projects/project_clear_api",
            headers=headers,
            json={
                "expected_revision": 1,
                "remote": None,
                "cost_budget": None,
                "provider_policy": None,
                "baseline_index_digest": None,
                "test_recipes": [],
                "build_recipes": [],
            },
        )

    assert cleared.status_code == 200
    payload = cleared.json()
    assert payload["remote"] is None
    assert payload["cost_budget"] is None
    assert payload["provider_policy"] == {}
    assert payload["baseline_index_digest"] is None
    assert payload["test_recipes"] == []
    assert payload["build_recipes"] == []


def test_project_index_status_and_explicit_revision_bound_rebuild(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "project-index-owner-token-20d7"
    monkeypatch.setenv("KESTREL_PROJECT_INDEX_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "indexed-repository"
    source = repository / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def answer() -> int:\n    return 42\n",
        encoding="utf-8",
    )

    with TestClient(
        create_app(_config(tmp_path, token_env="KESTREL_PROJECT_INDEX_TOKEN"))
    ) as client:
        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "project_index_api",
                "display_name": "Indexed API",
                "repository_path": str(repository.resolve()),
            },
        )
        assert created.status_code == 201

        missing = client.get(
            "/api/projects/project_index_api/index",
            headers=headers,
        )
        assert missing.status_code == 200
        assert missing.json()["freshness"] == "missing"
        assert not (repository / ".nest").exists()

        assert client.post(
            "/api/projects/project_index_api/index/rebuild",
            json={"expected_project_revision": 1},
        ).status_code == 401
        stale = client.post(
            "/api/projects/project_index_api/index/rebuild",
            headers=headers,
            json={"expected_project_revision": 2},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["error"] == "project_revision_conflict"
        assert not (repository / ".nest").exists()

        rebuilt = client.post(
            "/api/projects/project_index_api/index/rebuild",
            headers=headers,
            json={"expected_project_revision": 1},
        )
        assert rebuilt.status_code == 200
        payload = rebuilt.json()
        assert payload["report"]["indexed_files"] == 1
        assert payload["project"]["revision"] == 2
        assert (
            payload["project"]["baseline_index_digest"]
            == payload["report"]["aggregate_digest"]
        )

        current = client.get(
            "/api/projects/project_index_api/index",
            headers=headers,
        ).json()
        assert current["status"] == "ready"
        assert current["freshness"] == "current"
        assert current["aggregate_digest"] == payload["report"]["aggregate_digest"]

        source.write_text(
            "def answer() -> int:\n    return 43\n",
            encoding="utf-8",
        )
        changed = client.get(
            "/api/projects/project_index_api/index",
            headers=headers,
        ).json()
        assert changed["freshness"] == "stale"

        RepositoryIndex(
            project_id="project_index_api",
            repository_root=repository,
        ).rebuild()
        unbound = client.get(
            "/api/projects/project_index_api/index",
            headers=headers,
        ).json()
        assert unbound["status"] == "rebuild_required"
        assert unbound["freshness"] == "unknown"
        assert "not bound" in unbound["detail"]


def _config(tmp_path: Path, *, token_env: str) -> AgentConfig:
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
        api_auth_token_env=token_env,
    )
