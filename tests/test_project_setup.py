from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nested_memvid_agent.project_setup import build_project_setup_draft

_FIRST_MISSION_TOOLS = (
    "file.read",
    "repo.context_pack",
    "repo.dependencies",
    "repo.map",
    "repo.references",
    "repo.symbols",
    "repo.tests_for",
)


def test_project_setup_draft_inspects_branch_recipes_and_exact_local_authority(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "wildflower"
    repository.mkdir()
    (repository / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "build": "vite build",
                    "test": "vitest run",
                    "dev": "vite",
                }
            }
        ),
        encoding="utf-8",
    )
    _git(repository, "init", "-b", "trunk")
    _git(repository, "add", "package.json")
    _git(
        repository,
        "-c",
        "user.name=Kestrel Test",
        "-c",
        "user.email=kestrel@example.invalid",
        "commit",
        "-m",
        "initial",
    )

    draft = build_project_setup_draft(
        repository_path=repository,
        provider="ollama",
        model="qwen3",
        base_url=None,
        capability_catalog=_capability_catalog(),
    )

    assert draft["schema"] == "kestrel.project_setup_draft.v1"
    assert draft["inspection"]["git"] == {
        "branch": "trunk",
        "state": "clean",
        "summary": "Working tree is clean.",
    }
    assert draft["inspection"]["index"]["status"] == "not_created"
    assert draft["inspection"]["test_recipes"] == [
        {"name": "npm test", "command": "npm run test"}
    ]
    assert draft["inspection"]["build_recipes"] == [
        {"name": "npm build", "command": "npm run build"}
    ]
    assert draft["create_input"] == {
        "display_name": "wildflower",
        "repository_path": str(repository.resolve()),
        "default_branch": "trunk",
        "allowed_paths": ["."],
        "provider_policy": {
            "preset": "local_only",
            "allowed_providers": ["ollama"],
            "allowed_models": ["qwen3"],
            "direct_estimated_cost_usd": 0.0,
        },
        "cost_budget": 0.0,
        "privacy_class": "local_required",
        "test_recipes": [{"name": "npm test", "command": "npm run test"}],
        "build_recipes": [{"name": "npm build", "command": "npm run build"}],
        "capability_ceiling": [
            f"tool:{tool}" for tool in sorted(_FIRST_MISSION_TOOLS)
        ],
    }
    assert draft["first_mission"]["can_start"] is True
    assert draft["first_mission"]["blockers"] == []


def test_project_setup_draft_requires_explicit_cloud_cost_authority(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "cloud-project"
    repository.mkdir()
    (repository / "README.md").write_text("# Cloud project\n", encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c",
        "user.name=Kestrel Test",
        "-c",
        "user.email=kestrel@example.invalid",
        "commit",
        "-m",
        "initial",
    )

    blocked = build_project_setup_draft(
        repository_path=repository,
        provider="openai",
        model="gpt-5.5",
        base_url=None,
        capability_catalog=_capability_catalog(),
    )

    assert blocked["first_mission"]["can_start"] is False
    assert blocked["create_input"]["privacy_class"] == "approved_cloud"
    assert blocked["create_input"]["cost_budget"] is None
    assert blocked["first_mission"]["blockers"] == [
        "Enter the provider's estimated cost for one call.",
        "Enter a project spend limit before approving an external provider.",
    ]

    admitted = build_project_setup_draft(
        repository_path=repository,
        provider="openai",
        model="gpt-5.5",
        base_url=None,
        capability_catalog=_capability_catalog(),
        direct_estimated_cost_usd=0.25,
        cost_budget=1.0,
    )

    assert admitted["first_mission"]["can_start"] is True
    assert admitted["create_input"]["provider_policy"] == {
        "preset": "approved_cloud",
        "allowed_providers": ["openai"],
        "allowed_models": ["gpt-5.5"],
        "direct_estimated_cost_usd": 0.25,
    }
    assert admitted["create_input"]["cost_budget"] == 1.0
    assert admitted["create_input"]["privacy_class"] == "approved_cloud"


def test_project_setup_draft_reports_missing_first_mission_capabilities(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "bounded"
    repository.mkdir()
    _git(repository, "init", "-b", "main")

    draft = build_project_setup_draft(
        repository_path=repository,
        provider="mock",
        model="mock",
        base_url=None,
        capability_catalog=[],
    )

    assert draft["first_mission"]["can_start"] is False
    assert draft["create_input"]["capability_ceiling"] == []
    assert draft["first_mission"]["missing_tools"] == sorted(_FIRST_MISSION_TOOLS)
    assert draft["first_mission"]["blockers"] == [
        "Enable the required read-only capabilities before saving this first-mission profile."
    ]


def _capability_catalog() -> list[dict[str, object]]:
    return [
        {
            "key": f"tool:{tool}",
            "effective_enabled": True,
            "parent_key": None,
        }
        for tool in _FIRST_MISSION_TOOLS
    ]


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
