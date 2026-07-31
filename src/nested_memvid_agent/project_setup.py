from __future__ import annotations

import json
import re
import stat
import tomllib
from collections.abc import Mapping, Sequence
from math import isfinite
from numbers import Real
from pathlib import Path
from typing import Any

from .mission_control import inspect_git_worktree, validated_mission_plan
from .projects import canonical_repository_path, direct_provider_is_local
from .security_boundary import redact_text

PROJECT_SETUP_DRAFT_SCHEMA = "kestrel.project_setup_draft.v1"
_MAX_MANIFEST_BYTES = 1_048_576
_FIRST_MISSION_TEMPLATE = "explain_repository"
_FIRST_MISSION_ESTIMATED_CALLS = 3


def build_project_setup_draft(
    *,
    repository_path: str | Path,
    provider: str,
    model: str,
    base_url: str | None,
    capability_catalog: Sequence[Mapping[str, object]],
    direct_estimated_cost_usd: float | None = None,
    cost_budget: float | None = None,
) -> dict[str, Any]:
    """Build a read-only, server-authoritative first-project draft.

    The draft never creates an index, changes Git, enables a capability, or
    stores provider credentials. Its `create_input` can be posted unchanged
    only after the owner has reviewed the accompanying evidence.
    """

    root = canonical_repository_path(repository_path)
    normalized_provider = _required_text(provider, "provider")
    normalized_model = _required_text(model, "model")
    reviewed_estimate = _optional_nonnegative_number(
        direct_estimated_cost_usd,
        "direct_estimated_cost_usd",
    )
    reviewed_budget = _optional_nonnegative_number(
        cost_budget,
        "cost_budget",
    )
    git = inspect_git_worktree(root)
    test_recipes, build_recipes, recipe_warnings = _discover_recipes(root)
    required_tools = _first_mission_tools()
    capability_ceiling, missing_tools = _first_mission_capability_ceiling(
        capability_catalog,
        required_tools=required_tools,
    )
    local_route = direct_provider_is_local(
        provider=normalized_provider,
        base_url=base_url,
    )
    demo_route = normalized_provider.casefold() == "mock"

    if demo_route or local_route:
        effective_estimate = 0.0
        effective_budget = 0.0 if reviewed_budget is None else reviewed_budget
        privacy_class = "local_required"
        preset = "local_only"
    else:
        effective_estimate = (
            None
            if reviewed_estimate is None
            else reviewed_estimate
        )
        effective_budget = reviewed_budget
        privacy_class = "approved_cloud"
        preset = "approved_cloud"

    provider_policy: dict[str, object] = {
        "preset": preset,
        "allowed_providers": [normalized_provider],
        "allowed_models": [normalized_model],
    }
    if effective_estimate is not None:
        provider_policy["direct_estimated_cost_usd"] = effective_estimate

    blockers: list[str] = []
    if git.state == "unknown":
        blockers.append(
            "Choose a readable Git worktree before saving this first-mission profile."
        )
    if git.branch.startswith("detached@"):
        blockers.append(
            "Check out a named Git branch before saving this first-mission profile."
        )
    if missing_tools:
        blockers.append(
            "Enable the required read-only capabilities before saving this "
            "first-mission profile."
        )
    if not (demo_route or local_route):
        if effective_estimate is None:
            blockers.append("Enter the provider's estimated cost for one call.")
        if effective_budget is None:
            blockers.append(
                "Enter a project spend limit before approving an external provider."
            )
        elif (
            effective_estimate is not None
            and effective_budget
            < effective_estimate * _FIRST_MISSION_ESTIMATED_CALLS
        ):
            blockers.append(
                "Raise the project spend limit to cover the three-call first mission estimate."
            )

    default_branch = (
        git.branch
        if git.branch
        and git.branch not in {"unknown", "not-a-git-worktree"}
        and not git.branch.startswith("detached@")
        else "unknown"
    )
    create_input = {
        "display_name": root.name or "Project",
        "repository_path": str(root),
        "default_branch": default_branch,
        "allowed_paths": ["."],
        "provider_policy": provider_policy,
        "cost_budget": effective_budget,
        "privacy_class": privacy_class,
        "test_recipes": test_recipes,
        "build_recipes": build_recipes,
        "capability_ceiling": capability_ceiling,
    }
    return {
        "schema": PROJECT_SETUP_DRAFT_SCHEMA,
        "inspection": {
            "canonical_path": str(root),
            "git": {
                "branch": git.branch,
                "state": git.state,
                "summary": git.summary,
            },
            "index": {
                "status": "not_created",
                "detail": (
                    "Preview is read-only. No repository index exists for this "
                    "unsaved profile; build it explicitly after registration."
                ),
            },
            "test_recipes": test_recipes,
            "build_recipes": build_recipes,
            "recipe_warnings": recipe_warnings,
        },
        "create_input": create_input,
        "first_mission": {
            "template_id": _FIRST_MISSION_TEMPLATE,
            "estimated_provider_calls": _FIRST_MISSION_ESTIMATED_CALLS,
            "can_start": not blockers,
            "required_tools": required_tools,
            "missing_tools": missing_tools,
            "blockers": blockers,
        },
    }


def _first_mission_tools() -> list[str]:
    plan = validated_mission_plan(_FIRST_MISSION_TEMPLATE, None)
    return sorted(
        {
            str(tool)
            for task in plan
            for tool in task["required_tools"]
        }
    )


def _first_mission_capability_ceiling(
    catalog: Sequence[Mapping[str, object]],
    *,
    required_tools: Sequence[str],
) -> tuple[list[str], list[str]]:
    by_key = {
        str(item.get("key", "")): item
        for item in catalog
        if str(item.get("key", ""))
    }
    admitted: set[str] = set()
    missing: list[str] = []
    for tool in required_tools:
        key = f"tool:{tool}"
        item = by_key.get(key)
        if item is None or not bool(item.get("effective_enabled")):
            missing.append(tool)
            continue
        parent_key = str(item.get("parent_key") or "")
        if parent_key:
            parent = by_key.get(parent_key)
            if parent is None or not bool(parent.get("effective_enabled")):
                missing.append(tool)
                continue
            admitted.add(parent_key)
        admitted.add(key)
    return sorted(admitted), sorted(missing)


def _discover_recipes(
    root: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    tests: list[dict[str, str]] = []
    builds: list[dict[str, str]] = []
    warnings: list[str] = []

    package_text = _bounded_manifest(root / "package.json", warnings)
    if package_text is not None:
        try:
            payload = json.loads(package_text)
            scripts = payload.get("scripts", {}) if isinstance(payload, dict) else {}
            if isinstance(scripts, dict):
                for name in sorted(scripts):
                    if not isinstance(name, str) or not isinstance(scripts[name], str):
                        continue
                    if name == "test" or name.startswith(("test:", "lint", "check", "typecheck")):
                        _append_recipe(tests, f"npm {name}", f"npm run {name}")
                    elif name == "build" or name.startswith("build:"):
                        _append_recipe(builds, f"npm {name}", f"npm run {name}")
        except (json.JSONDecodeError, UnicodeError) as exc:
            warnings.append(f"package.json was not parsed: {redact_text(str(exc))}")

    pyproject_text = _bounded_manifest(root / "pyproject.toml", warnings)
    if pyproject_text is not None:
        try:
            payload = tomllib.loads(pyproject_text)
            tools = payload.get("tool", {}) if isinstance(payload, dict) else {}
            if not isinstance(tools, dict):
                tools = {}
            if "pytest" in tools:
                _append_recipe(tests, "pytest", "pytest -q")
            if "ruff" in tools:
                _append_recipe(tests, "ruff", "ruff check .")
            if "mypy" in tools:
                _append_recipe(tests, "mypy", "mypy src")
            if isinstance(payload, dict) and "build-system" in payload:
                _append_recipe(builds, "python build", "python -m build")
        except (tomllib.TOMLDecodeError, UnicodeError) as exc:
            warnings.append(f"pyproject.toml was not parsed: {redact_text(str(exc))}")

    makefile_text = _bounded_manifest(root / "Makefile", warnings)
    if makefile_text is not None:
        for line in makefile_text.splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+):(?:\s|$)", line)
            if match is None:
                continue
            target = match.group(1)
            if target in {"test", "tests", "check", "lint", "typecheck"}:
                _append_recipe(tests, f"make {target}", f"make {target}")
            elif target == "build":
                _append_recipe(builds, "make build", "make build")

    return tests[:64], builds[:64], warnings


def _bounded_manifest(path: Path, warnings: list[str]) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        warnings.append(f"{path.name} could not be inspected: {redact_text(str(exc))}")
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        warnings.append(f"{path.name} is not a trusted regular file.")
        return None
    if metadata.st_size > _MAX_MANIFEST_BYTES:
        warnings.append(f"{path.name} exceeds the 1 MiB setup inspection limit.")
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        warnings.append(f"{path.name} could not be read: {redact_text(str(exc))}")
        return None


def _append_recipe(
    recipes: list[dict[str, str]],
    name: str,
    command: str,
) -> None:
    if (
        not name.strip()
        or len(name.strip()) > 128
        or not command.strip()
        or len(command.strip()) > 8_192
    ):
        return
    if any(recipe["command"] == command for recipe in recipes):
        return
    recipes.append(
        {"name": name.strip(), "command": command.strip()}
    )


def _required_text(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > 512:
        raise ValueError(f"{field} exceeds 512 characters")
    return normalized


def _optional_nonnegative_number(
    value: float | None,
    field: str,
) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{field} must be a finite non-negative number")
    return float(value)
