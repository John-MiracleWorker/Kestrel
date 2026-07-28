from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nested_memvid_agent.projects import (
    ProjectConflictError,
    direct_provider_is_local,
    export_project,
    import_project_document,
)
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.state_store import SCHEMA_VERSION, AgentStateStore

ACTIVE_CAPABILITIES = {
    "tool:file.read",
    "tool:file.write",
    "tool:test.run",
}


@pytest.mark.parametrize(
    ("provider", "base_url", "expected"),
    [
        ("mock", None, True),
        ("lm-studio", None, True),
        ("ollama", None, True),
        ("openai-compatible", "http://127.0.0.1:1234/v1", True),
        ("openai-compatible", "http://[::1]:1234/v1", True),
        ("openai-compatible", "https://models.example.com/v1", False),
        ("ollama", "https://ollama.com/api", False),
        ("openai", "http://127.0.0.1:1234/v1", False),
        ("local", None, False),
    ],
)
def test_direct_provider_locality_is_bound_to_the_actual_endpoint(
    provider: str,
    base_url: str | None,
    expected: bool,
) -> None:
    assert direct_provider_is_local(provider=provider, base_url=base_url) is expected


def test_project_profiles_are_isolated_and_revision_fenced(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first = state.create_project(
        project_id="project_first",
        display_name="First",
        repository_path=first_root,
        allowed_paths=("src", "tests"),
        provider_policy={"preset": "local_only"},
        cost_budget=12.5,
        privacy_class="local_required",
        test_recipes=({"name": "unit", "command": "pytest -q"},),
        build_recipes=({"name": "package", "command": "python -m build"},),
        capability_ceiling=("tool:file.read", "tool:test.run"),
        active_capability_keys=ACTIVE_CAPABILITIES,
    )
    second = state.create_project(
        project_id="project_second",
        display_name="Second",
        repository_path=second_root,
        allowed_paths=(".",),
        provider_policy={},
        privacy_class="local_required",
        capability_ceiling=("tool:file.read",),
        active_capability_keys=ACTIVE_CAPABILITIES,
    )

    updated = state.update_project(
        first.project_id,
        expected_revision=first.revision,
        active_capability_keys=ACTIVE_CAPABILITIES,
        test_recipes=({"name": "focused", "command": "pytest tests/test_one.py"},),
    )

    assert updated.revision == 2
    assert updated.test_recipes[0]["name"] == "focused"
    assert state.get_project(second.project_id) == second
    assert state.get_project(second.project_id).test_recipes == ()
    with pytest.raises(ProjectConflictError) as stale:
        state.update_project(
            first.project_id,
            expected_revision=first.revision,
            active_capability_keys=ACTIVE_CAPABILITIES,
            display_name="Stale",
        )
    assert stale.value.current == updated


@pytest.mark.parametrize(
    "field",
    ("repository_path", "test_recipes", "build_recipes"),
)
def test_project_state_rejects_null_structural_updates_without_type_errors(
    tmp_path: Path,
    field: str,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    repository = tmp_path / "repository"
    repository.mkdir()
    project = state.create_project(
        project_id=f"project_null_{field}",
        display_name="Null defense",
        repository_path=repository,
        active_capability_keys=ACTIVE_CAPABILITIES,
    )

    with pytest.raises(ValueError, match=field):
        state.update_project(
            project.project_id,
            expected_revision=project.revision,
            active_capability_keys=ACTIVE_CAPABILITIES,
            **{field: None},
        )


def test_project_repository_and_capability_boundaries_fail_closed(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(ValueError, match="absolute"):
        state.create_project(
            project_id="relative",
            display_name="Relative",
            repository_path=Path("repository"),
            active_capability_keys=ACTIVE_CAPABILITIES,
        )

    symlink = tmp_path / "repository-link"
    symlink.symlink_to(repository, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link|canonical"):
        state.create_project(
            project_id="symlinked",
            display_name="Symlinked",
            repository_path=symlink,
            active_capability_keys=ACTIVE_CAPABILITIES,
        )

    with pytest.raises(ValueError, match="active capability"):
        state.create_project(
            project_id="widened",
            display_name="Widened",
            repository_path=repository,
            capability_ceiling=("tool:shell.run",),
            active_capability_keys=ACTIVE_CAPABILITIES,
        )

    created = state.create_project(
        project_id="canonical",
        display_name="Canonical",
        repository_path=repository,
        active_capability_keys=ACTIVE_CAPABILITIES,
    )
    assert set(created.capability_ceiling) == ACTIVE_CAPABILITIES

    with pytest.raises(sqlite3.IntegrityError):
        state.create_project(
            project_id="duplicate",
            display_name="Duplicate",
            repository_path=repository,
            active_capability_keys=ACTIVE_CAPABILITIES,
        )


def test_project_export_is_redacted_reviewable_metadata_and_round_trips(
    tmp_path: Path,
) -> None:
    source = AgentStateStore(tmp_path / "source.db")
    repository = tmp_path / "repository"
    repository.mkdir()
    secret = "opaque-project-secret-value-950c"
    register_secret_value(secret)
    project = source.create_project(
        project_id="project_export",
        display_name="Exportable",
        repository_path=repository,
        remote="git@github.com:example/repository.git",
        default_branch="main",
        allowed_paths=("src", "tests"),
        provider_policy={"preset": "balanced", "token": secret},
        cost_budget=8.0,
        privacy_class="local_preferred",
        test_recipes=(
            {"name": "unit", "command": f"PROJECT_TOKEN={secret} pytest -q"},
        ),
        build_recipes=({"name": "build", "command": "python -m build"},),
        capability_ceiling=("tool:file.read", "tool:test.run"),
        active_capability_keys=ACTIVE_CAPABILITIES,
        baseline_index_digest="sha256:baseline",
    )

    document = export_project(project)
    rendered = str(document)

    assert document["format"] == "kestrel.project.v1"
    assert secret not in rendered
    assert "created_at" not in rendered
    assert "updated_at" not in rendered
    assert "archived_at" not in rendered
    assert "revision" not in rendered
    assert "memory" not in rendered
    assert "secrets" not in rendered

    target = AgentStateStore(tmp_path / "target.db")
    imported_fields = import_project_document(
        document,
        active_capability_keys=ACTIVE_CAPABILITIES,
    )
    imported = target.create_project(
        **imported_fields,
        active_capability_keys=ACTIVE_CAPABILITIES,
    )

    assert imported.project_id == project.project_id
    assert imported.repository_path == project.repository_path
    assert imported.display_name == project.display_name
    assert imported.capability_ceiling == project.capability_ceiling
    assert imported.revision == 1


def test_project_archive_is_revision_fenced_and_hidden_by_default(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    repository = tmp_path / "repository"
    repository.mkdir()
    project = state.create_project(
        project_id="project_archive",
        display_name="Archive",
        repository_path=repository,
        active_capability_keys=ACTIVE_CAPABILITIES,
    )

    archived = state.archive_project(
        project.project_id,
        expected_revision=project.revision,
    )

    assert archived.archived_at is not None
    assert archived.revision == 2
    assert state.list_projects() == []
    assert state.list_projects(include_archived=True) == [archived]
    with pytest.raises(ProjectConflictError):
        state.archive_project(
            project.project_id,
            expected_revision=project.revision,
        )


def test_new_runs_may_bind_only_to_an_active_matching_project(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    repository = tmp_path / "repository"
    other = tmp_path / "other"
    repository.mkdir()
    other.mkdir()
    project = state.create_project(
        project_id="project_run",
        display_name="Run project",
        repository_path=repository,
        active_capability_keys=ACTIVE_CAPABILITIES,
    )

    bound = state.create_run(
        run_id="bound_run",
        message="bound",
        session_id="session",
        workspace=str(repository.resolve()),
        provider="mock",
        model="mock",
        project_id=project.project_id,
    )

    assert bound.project_id == project.project_id
    with pytest.raises(ValueError, match="revision is stale"):
        state.create_run(
            run_id="stale_project_revision",
            message="stale",
            session_id="session",
            workspace=str(repository.resolve()),
            provider="mock",
            model="mock",
            project_id=project.project_id,
            expected_project_revision=project.revision + 1,
        )
    with pytest.raises(ValueError, match="workspace"):
        state.create_run(
            run_id="wrong_workspace",
            message="wrong",
            session_id="session",
            workspace=str(other.resolve()),
            provider="mock",
            model="mock",
            project_id=project.project_id,
        )
    state.archive_project(project.project_id, expected_revision=project.revision)
    with pytest.raises(ValueError, match="archived"):
        state.create_run(
            run_id="archived_run",
            message="archived",
            session_id="session",
            workspace=str(repository.resolve()),
            provider="mock",
            model="mock",
            project_id=project.project_id,
        )


def test_schema_19_migrates_projects_and_nullable_run_binding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v19.db"
    state = AgentStateStore(path)
    state.create_run(
        run_id="legacy_run",
        message="legacy",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_runs_project_id")
        connection.execute("ALTER TABLE runs DROP COLUMN project_id")
        connection.execute("DROP TABLE projects")
        connection.execute("UPDATE schema_version SET version = 19 WHERE id = 1")

    migrated = AgentStateStore(path)
    reopened = AgentStateStore(path)
    run = reopened.get_run("legacy_run")
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }

    assert migrated.schema_version() == SCHEMA_VERSION == 21
    assert reopened.schema_version() == SCHEMA_VERSION
    assert "project_id" in columns
    assert run.project_id is None
