from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Thread
from time import monotonic
from typing import Any, Literal, cast

from .projects import (
    ProjectRecord,
    canonical_repository_path,
    project_routing_constraints,
    validate_project_direct_route,
)
from .repair_integrity import (
    hardened_readonly_git_command,
    hardened_readonly_git_environment,
    trusted_git_executable,
)
from .repo_index import RepositoryIndex, RepositoryIndexError
from .routing.models import RoutingMode
from .routing.router import RoutingUnavailableError
from .routing.service import AdaptiveFlockRoutingService
from .security_boundary import redact_text

MissionCheckStatus = Literal["pass", "warn", "fail", "unknown"]
WorkingTreeState = Literal["clean", "dirty", "unknown"]
IndexFreshness = Literal["current", "stale", "missing", "unknown"]

MISSION_PREFLIGHT_SCHEMA = "kestrel.mission_preflight.v1"
MISSION_LAUNCH_BINDING_SCHEMA = "kestrel.mission_launch_binding.v1"
_PLATFORM_OS: Any = os
_PLATFORM_SIGNAL: Any = signal
MISSION_TEMPLATE_IDS = frozenset(
    {
        "explain_repository",
        "fix_failing_test",
        "implement_feature",
        "safe_refactor",
        "security_review",
        "documentation",
    }
)
_MUTATING_TEMPLATES = frozenset(
    {"fix_failing_test", "implement_feature", "safe_refactor", "documentation"}
)
_MAX_GIT_PREFLIGHT_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_GIT_PREFLIGHT_UNTRACKED_BYTES = 64 * 1024 * 1024
_MAX_GIT_PREFLIGHT_UNTRACKED_FILE_BYTES = 16 * 1024 * 1024
_MAX_GIT_PREFLIGHT_UNTRACKED_FILES = 10_000


@dataclass(frozen=True)
class GitInspection:
    branch: str
    state: WorkingTreeState
    summary: str
    head_sha: str | None = None
    tree_sha: str | None = None
    worktree_digest: str | None = None


@dataclass(frozen=True)
class IndexInspection:
    freshness: IndexFreshness
    detail: str
    digest: str | None = None
    indexed_at: str | None = None


@dataclass(frozen=True)
class ProviderInspection:
    status: MissionCheckStatus
    detail: str
    route_policy: str
    estimated_cost_usd: float | None


@dataclass(frozen=True)
class _MissionRouteTask:
    task_id: str
    run_id: str
    title: str
    goal: str
    profile: str
    risk: str
    required_tools: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    plan: Mapping[str, Any] | None = None


def build_mission_preflight(
    *,
    project: ProjectRecord,
    objective: str,
    template_id: str,
    git: GitInspection,
    index: IndexInspection,
    provider: ProviderInspection,
    capability_catalog: Sequence[Mapping[str, object]],
    mission_plan: Sequence[Mapping[str, Any]] | None = None,
    launch_binding: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one deterministic, read-only task projection from current evidence."""

    normalized_objective = objective.strip()
    if not normalized_objective:
        raise ValueError("objective must not be empty")
    if template_id not in MISSION_TEMPLATE_IDS:
        raise ValueError(f"unsupported mission template: {template_id}")
    if project.archived_at is not None:
        raise ValueError("cannot preflight an archived project")

    tasks = validated_mission_plan(template_id, mission_plan)
    required_tools = sorted(
        {
            str(tool)
            for task in tasks
            for tool in task["required_tools"]
        }
    )
    allowed_capabilities = set(project.capability_ceiling)
    catalog_by_key = {
        str(item.get("key", "")): item
        for item in capability_catalog
        if str(item.get("key", ""))
    }
    effective_items = []
    for item in capability_catalog:
        key = str(item.get("key", ""))
        parent_key = str(item.get("parent_key") or "")
        parent = catalog_by_key.get(parent_key) if parent_key else None
        if (
            bool(item.get("effective_enabled"))
            and key in allowed_capabilities
            and (
                not parent_key
                or (
                    parent_key in allowed_capabilities
                    and parent is not None
                    and bool(parent.get("effective_enabled"))
                )
            )
        ):
            effective_items.append(item)
    effective_capabilities = sorted(str(item["key"]) for item in effective_items)
    effective_tools = {
        str(item["key"])[len("tool:") :]
        for item in effective_items
        if str(item.get("key", "")).startswith("tool:")
    }
    missing_tools = sorted(set(required_tools) - effective_tools)
    likely_approvals = sorted(
        str(item["key"])
        for item in effective_items
        if str(item.get("key", "")).startswith("tool:")
        and str(item["key"])[len("tool:") :] in required_tools
        and bool(item.get("requires_approval"))
    )

    warnings: list[str] = []
    blockers: list[str] = []
    checks: list[dict[str, str | None]] = []

    repository_status: MissionCheckStatus = (
        "pass" if git.state in {"clean", "dirty"} else "fail"
    )
    checks.append(
        _check(
            "repository",
            "Repository",
            repository_status,
            git.summary,
            (
                None
                if git.state != "unknown"
                else "Verify that the project path is a readable Git worktree."
            ),
        )
    )
    if git.state == "dirty":
        warnings.append(
            "The working tree has local changes; any mutation must stay in an isolated "
            "repair worktree so those changes remain untouched."
        )
    elif git.state == "unknown":
        repository_blocker = (
            "Git working-tree state could not be established; the repository cannot be "
            "safely admitted."
        )
        blockers.append(repository_blocker)

    index_status: MissionCheckStatus = "pass" if index.freshness == "current" else "warn"
    if index.freshness == "unknown":
        index_status = "unknown"
    checks.append(
        _check(
            "index",
            "Repository index",
            index_status,
            index.detail,
            (
                None
                if index.freshness == "current"
                else "Explicitly rebuild the project index before relying on structural evidence."
            ),
        )
    )
    if index.freshness == "missing":
        warnings.append(
            "The repository index is missing; bounded file evidence remains available, "
            "but structural answers cannot claim indexed authority."
        )
    elif index.freshness == "stale":
        warnings.append(
            "The repository index is stale and cannot be treated as authoritative until rebuilt."
        )
    elif index.freshness == "unknown":
        warnings.append(
            "Repository-index freshness is unknown and cannot be treated as authoritative."
        )

    checks.append(
        _check(
            "route",
            "Route",
            provider.status,
            f"{provider.route_policy}: {provider.detail}",
            (
                "Discover or validate an eligible provider target."
                if provider.status == "fail"
                else None
            ),
        )
    )
    if provider.status == "fail":
        blockers.append(provider.detail)
    elif provider.status in {"warn", "unknown"}:
        warnings.append(provider.detail)

    if missing_tools:
        missing = ", ".join(missing_tools)
        capability_detail = f"Missing required capabilities: {missing}."
        checks.append(
            _check(
                "capabilities",
                "Permissions",
                "fail",
                capability_detail,
                "Enable only the required tools globally, then include them in the project ceiling.",
            )
        )
        blockers.append(capability_detail)
    else:
        checks.append(
            _check(
                "capabilities",
                "Permissions",
                "pass",
                (
                    f"{len(required_tools)} required tools are enabled inside the "
                    f"{len(effective_capabilities)}-capability project ceiling."
                ),
                None,
            )
        )

    budget_status, budget_detail = _budget_check(
        limit=project.cost_budget,
        estimate=provider.estimated_cost_usd,
    )
    checks.append(
        _check(
            "budget",
            "Budget",
            budget_status,
            budget_detail,
            (
                "Raise the project cap or choose a cheaper validated route."
                if budget_status == "fail"
                else None
            ),
        )
    )
    if budget_status == "fail":
        blockers.append(budget_detail)
    elif budget_status in {"warn", "unknown"}:
        warnings.append(budget_detail)

    validation_recipes = _validation_recipes(project)
    if template_id in _MUTATING_TEMPLATES and not validation_recipes:
        validation_status: MissionCheckStatus = "warn"
        validation_detail = (
            "No project validation recipe is configured; Kestrel must propose a bounded "
            "command for explicit review."
        )
        warnings.append(validation_detail)
    elif validation_recipes:
        validation_status = "pass"
        validation_detail = f"{len(validation_recipes)} project validation recipes are available."
    else:
        validation_status = "pass"
        validation_detail = "This review task does not plan a repository mutation."
    checks.append(
        _check(
            "validation",
            "Validation",
            validation_status,
            validation_detail,
            (
                "Add a project test or build recipe."
                if validation_status == "warn"
                else None
            ),
        )
    )

    rollback = (
        "Mutations remain in an isolated repair worktree until a current review artifact "
        "authorizes an exact acceptance action."
        if template_id in _MUTATING_TEMPLATES
        else "No repository mutation is planned; cancelling the run leaves the project unchanged."
    )
    checks.append(
        _check(
            "rollback",
            "Rollback",
            "pass",
            rollback,
            None,
        )
    )

    created = generated_at or datetime.now(UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {
        "schema": MISSION_PREFLIGHT_SCHEMA,
        "project_id": project.project_id,
        "project_revision": project.revision,
        "project_name": project.display_name,
        "repository_path": project.repository_path,
        "objective": normalized_objective,
        "template_id": template_id,
        "branch": git.branch or project.default_branch,
        "working_tree": {
            "state": git.state,
            "summary": git.summary,
            "head_sha": git.head_sha,
            "tree_sha": git.tree_sha,
            "digest": git.worktree_digest,
        },
        "route_policy": provider.route_policy,
        "budget": {
            "currency": "USD",
            "limit": project.cost_budget,
            "estimate": provider.estimated_cost_usd,
        },
        "effective_capabilities": effective_capabilities,
        "likely_approvals": likely_approvals,
        "validation_recipes": validation_recipes,
        "rollback": rollback,
        "index": {
            "freshness": index.freshness,
            "digest": index.digest,
            "indexed_at": index.indexed_at,
            "detail": index.detail,
        },
        "provider": {
            "status": provider.status,
            "detail": provider.detail,
        },
        "launch_binding": dict(launch_binding or {}),
        "checks": checks,
        "tasks": tasks,
        "warnings": _deduplicate(warnings),
        "blockers": _deduplicate(blockers),
        "can_start": not blockers,
        "generated_at": created.astimezone(UTC).isoformat(),
    }


def build_mission_launch_binding(
    *,
    project: ProjectRecord,
    objective: str,
    template_id: str,
    config: Any,
    routing_config: Any,
    provider_profiles: Sequence[Any],
    model_targets: Sequence[Any],
    route_policies: Sequence[Any],
    preflight_digest: str,
    mission_plan: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind Mission launch to the live project, provider, and routing revisions."""

    normalized_objective = objective.strip()
    if not normalized_objective:
        raise ValueError("objective must not be empty")
    if template_id not in MISSION_TEMPLATE_IDS:
        raise ValueError(f"unsupported mission template: {template_id}")
    policy_id = str(
        project.provider_policy.get("policy_id")
        or getattr(routing_config, "policy_id", "balanced")
    )
    policy_entry = next(
        (
            entry
            for entry in route_policies
            if str(entry.policy.policy_id) == policy_id
        ),
        None,
    )
    config_digest = _payload_digest(
        {
            "provider": str(getattr(config, "provider", "")),
            "model": str(getattr(config, "model", "")),
            "base_url": str(getattr(config, "base_url", "") or ""),
            "api_key_env_digest": _credential_reference_digest(
                str(getattr(config, "api_key_env", "") or ""),
                purpose="primary",
            ),
            "fallback_provider": str(
                getattr(config, "fallback_provider", "") or ""
            ),
            "fallback_model": str(getattr(config, "fallback_model", "") or ""),
            "fallback_base_url": str(
                getattr(config, "fallback_base_url", "") or ""
            ),
            "fallback_api_key_env_digest": _credential_reference_digest(
                str(getattr(config, "fallback_api_key_env", "") or ""),
                purpose="fallback",
            ),
            "timeout_seconds": int(getattr(config, "timeout_seconds", 0)),
            "max_retries": int(getattr(config, "max_retries", 0)),
        }
    )
    inventory_digest = _payload_digest(
        {
            "provider_profiles": [
                {
                    "id": str(entry.profile.profile_id),
                    "revision": int(entry.revision),
                }
                for entry in sorted(
                    provider_profiles,
                    key=lambda item: str(item.profile.profile_id),
                )
            ],
            "model_targets": [
                {
                    "id": str(entry.target.target_id),
                    "revision": int(entry.revision),
                }
                for entry in sorted(
                    model_targets,
                    key=lambda item: str(item.target.target_id),
                )
            ],
        }
    )
    payload: dict[str, Any] = {
        "schema": MISSION_LAUNCH_BINDING_SCHEMA,
        "project_id": project.project_id,
        "project_revision": project.revision,
        "objective_digest": hashlib.sha256(
            normalized_objective.encode("utf-8")
        ).hexdigest(),
        "template_id": template_id,
        "config_digest": config_digest,
        "routing_enabled": bool(getattr(routing_config, "enabled", False)),
        "routing_mode": str(getattr(routing_config, "mode", "off")),
        "policy_id": policy_id,
        "policy_revision": (
            None if policy_entry is None else int(policy_entry.revision)
        ),
        "inventory_digest": inventory_digest,
        "preflight_digest": preflight_digest,
        "plan_digest": _payload_digest({"tasks": list(mission_plan)}),
    }
    payload["binding_digest"] = _payload_digest(payload)
    return payload


def build_mission_preflight_digest(
    *,
    git: GitInspection,
    index: IndexInspection,
    provider: ProviderInspection,
    capability_catalog: Sequence[Mapping[str, object]],
) -> str:
    """Commit the launch binding to every safety-relevant preflight projection."""

    capabilities = [
        {
            "key": str(item.get("key", "")),
            "effective_enabled": bool(item.get("effective_enabled")),
            "requires_approval": bool(item.get("requires_approval")),
            "risk": str(item.get("risk", "")),
            "parent_key": str(item.get("parent_key") or ""),
            "blocked_by": item.get("blocked_by"),
        }
        for item in sorted(
            capability_catalog,
            key=lambda candidate: str(candidate.get("key", "")),
        )
    ]
    return _payload_digest(
        {
            "git": {
                "branch": git.branch,
                "state": git.state,
                "summary": git.summary,
                "head_sha": git.head_sha,
                "tree_sha": git.tree_sha,
                "worktree_digest": git.worktree_digest,
            },
            "index": {
                "freshness": index.freshness,
                "detail": index.detail,
                "digest": index.digest,
                "indexed_at": index.indexed_at,
            },
            "provider": {
                "status": provider.status,
                "detail": provider.detail,
                "route_policy": provider.route_policy,
                "estimated_cost_usd": provider.estimated_cost_usd,
            },
            "capabilities": capabilities,
        }
    )


def mission_launch_binding_matches(
    provided: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    """Compare a client-returned preflight binding to the live server projection."""

    try:
        provided_digest = str(provided.get("binding_digest", ""))
        expected_digest = str(expected.get("binding_digest", ""))
        provided_canonical = json.dumps(
            dict(provided),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        expected_canonical = json.dumps(
            dict(expected),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(provided_digest, expected_digest) and hmac.compare_digest(
        provided_canonical,
        expected_canonical,
    )


def mission_plan_scope_matches(
    provided: Sequence[Mapping[str, Any]],
    expected: Sequence[Mapping[str, Any]],
) -> bool:
    """Require the launched plan to exactly match the newly bound preflight plan."""

    if len(provided) != len(expected):
        return False
    try:
        return json.dumps(
            list(provided),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ) == json.dumps(
            list(expected),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return False


def validated_mission_plan(
    template_id: str,
    proposed: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Accept bounded prose edits while preserving the server-authored task structure."""

    expected = _plan_for(template_id)
    if proposed is None:
        return expected
    if len(proposed) != len(expected):
        raise ValueError("mission plan cannot add or remove preflight tasks")
    structural_fields = ("task_id", "dependencies", "required_tools", "risk")
    prose_fields = ("title", "rationale", "acceptance_criteria")
    normalized: list[dict[str, Any]] = []
    for index, (candidate, template) in enumerate(
        zip(proposed, expected, strict=True),
    ):
        if set(candidate) != set(template):
            raise ValueError(
                f"mission task {index + 1} fields do not match the template"
            )
        if any(candidate.get(field) != template[field] for field in structural_fields):
            raise ValueError(
                f"mission task {index + 1} cannot change dependencies, tools, or risk"
            )
        title = candidate.get("title")
        rationale = candidate.get("rationale")
        acceptance = candidate.get("acceptance_criteria")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 512
            or not isinstance(rationale, str)
            or not rationale.strip()
            or len(rationale) > 4_096
            or not isinstance(acceptance, list)
            or not 1 <= len(acceptance) <= 32
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > 4_096
                for item in acceptance
            )
        ):
            raise ValueError(
                f"mission task {index + 1} has invalid title, rationale, or acceptance criteria"
            )
        normalized.append(
            {
                field: (
                    list(candidate[field])
                    if field in {"dependencies", "acceptance_criteria", "required_tools"}
                    else candidate[field]
                )
                for field in (*structural_fields[:1], *prose_fields, *structural_fields[1:])
            }
        )
    return normalized


def inspect_git_worktree(
    repository_root: Path,
    *,
    timeout_seconds: float = 3.0,
) -> GitInspection:
    """Inspect Git without optional locks, hooks, index refresh, or unbounded output."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        root = canonical_repository_path(repository_root)
    except (PermissionError, ValueError) as exc:
        return GitInspection(
            branch="unknown",
            state="unknown",
            summary=f"Repository identity validation failed: {redact_text(str(exc))}",
        )
    try:
        trusted_git_executable()
    except RuntimeError:
        return GitInspection(
            branch="unknown",
            state="unknown",
            summary="Git is not installed or is not available on PATH.",
        )
    deadline = monotonic() + timeout_seconds
    try:
        inside, inside_return_code = _git_first_line(
            root,
            ("rev-parse", "--is-inside-work-tree"),
            deadline=deadline,
        )
        if inside_return_code != 0:
            raise OSError(
                f"git rev-parse failed with status {inside_return_code}"
            )
        if inside.strip() != "true":
            return GitInspection(
                branch="not-a-git-worktree",
                state="unknown",
                summary="The project directory is not a Git worktree.",
            )
        branch, branch_return_code = _git_first_line(
            root,
            ("symbolic-ref", "--short", "-q", "HEAD"),
            deadline=deadline,
        )
        if branch_return_code not in {0, 1}:
            raise OSError(
                f"git symbolic-ref failed with status {branch_return_code}"
            )
        if not branch:
            if branch_return_code != 1:
                raise OSError("git symbolic-ref returned an empty branch")
            detached, detached_return_code = _git_first_line(
                root,
                ("rev-parse", "--short", "HEAD"),
                deadline=deadline,
            )
            if detached_return_code != 0 or not detached:
                raise OSError(
                    f"git detached-HEAD inspection failed with status {detached_return_code}"
                )
            branch = f"detached@{detached}" if detached else "detached"
        tracked, tracked_return_code = _git_first_line(
            root,
            ("status", "--porcelain=v1", "--untracked-files=no"),
            deadline=deadline,
        )
        if tracked_return_code != 0:
            raise OSError(f"git status failed with status {tracked_return_code}")
        untracked_output, untracked_return_code = _git_output_bytes(
            root,
            (
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
            ),
            deadline=deadline,
            maximum_bytes=_MAX_GIT_PREFLIGHT_OUTPUT_BYTES,
        )
        if untracked_return_code != 0:
            raise OSError(
                f"git untracked-file inspection failed with status {untracked_return_code}"
            )
        head_sha, head_return_code = _git_first_line(
            root,
            ("rev-parse", "--verify", "HEAD"),
            deadline=deadline,
        )
        if head_return_code == 0 and not _is_git_object_id(head_sha):
            raise OSError(
                f"git HEAD inspection failed with status {head_return_code}"
            )
        if head_return_code == 0:
            tree_sha, tree_return_code = _git_first_line(
                root,
                ("rev-parse", "--verify", "HEAD^{tree}"),
                deadline=deadline,
            )
            if tree_return_code != 0 or not _is_git_object_id(tree_sha):
                raise OSError(
                    f"git tree inspection failed with status {tree_return_code}"
                )
            tracked_diff, diff_return_code = _git_output_bytes(
                root,
                (
                    "diff",
                    "--binary",
                    "--full-index",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "HEAD",
                    "--",
                ),
                deadline=deadline,
                maximum_bytes=_MAX_GIT_PREFLIGHT_OUTPUT_BYTES,
            )
            if diff_return_code != 0:
                raise OSError(
                    f"git worktree-diff inspection failed with status {diff_return_code}"
                )
        else:
            head_sha = ""
            tree_sha = ""
            staged_diff, staged_return_code = _git_output_bytes(
                root,
                (
                    "diff",
                    "--cached",
                    "--binary",
                    "--full-index",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                ),
                deadline=deadline,
                maximum_bytes=_MAX_GIT_PREFLIGHT_OUTPUT_BYTES,
            )
            unstaged_diff, unstaged_return_code = _git_output_bytes(
                root,
                (
                    "diff",
                    "--binary",
                    "--full-index",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                ),
                deadline=deadline,
                maximum_bytes=_MAX_GIT_PREFLIGHT_OUTPUT_BYTES,
            )
            if staged_return_code != 0 or unstaged_return_code != 0:
                raise OSError("git unborn-worktree inspection failed")
            tracked_diff = staged_diff + b"\0kestrel-index-boundary\0" + unstaged_diff
        untracked_manifest = _untracked_content_manifest(
            root,
            untracked_output,
            deadline=deadline,
        )
        worktree_digest = _payload_digest(
            {
                "head_sha": head_sha,
                "tree_sha": tree_sha,
                "tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest(),
                "untracked": untracked_manifest,
            }
        )
    except TimeoutError:
        return GitInspection(
            branch="unknown",
            state="unknown",
            summary="Git inspection exceeded its bounded deadline.",
        )
    except OSError as exc:
        return GitInspection(
            branch="unknown",
            state="unknown",
            summary=f"Git inspection failed: {redact_text(str(exc))}",
        )

    dirty_kinds = []
    if tracked:
        dirty_kinds.append("tracked changes")
    if untracked_output:
        dirty_kinds.append("untracked files")
    if not dirty_kinds:
        return GitInspection(
            branch=branch,
            state="clean",
            summary="Working tree is clean.",
            head_sha=head_sha or None,
            tree_sha=tree_sha or None,
            worktree_digest=worktree_digest,
        )
    return GitInspection(
        branch=branch,
        state="dirty",
        summary=f"{' and '.join(dirty_kinds).capitalize()} are present.",
        head_sha=head_sha or None,
        tree_sha=tree_sha or None,
        worktree_digest=worktree_digest,
    )


def inspect_index_without_mutation(project: ProjectRecord) -> IndexInspection:
    """Read a current-schema index without creating, migrating, or publishing it."""

    sidecar = (
        Path(project.repository_path)
        / ".nest"
        / "repo-index"
        / f"{project.project_id}.sqlite"
    )
    try:
        metadata = sidecar.lstat()
    except FileNotFoundError:
        return IndexInspection(
            freshness="missing",
            digest=project.baseline_index_digest,
            detail="No repository-index sidecar exists for this project.",
        )
    except OSError as exc:
        return IndexInspection(
            freshness="unknown",
            digest=project.baseline_index_digest,
            detail=f"Repository-index metadata is unreadable: {redact_text(str(exc))}",
        )
    if sidecar.is_symlink() or not sidecar.is_file():
        return IndexInspection(
            freshness="unknown",
            digest=project.baseline_index_digest,
            detail="Repository-index sidecar is not a trusted regular file.",
        )
    try:
        status = RepositoryIndex(
            project_id=project.project_id,
            repository_root=Path(project.repository_path),
            create=False,
        ).status()
    except (OSError, RepositoryIndexError, ValueError) as exc:
        return IndexInspection(
            freshness="unknown",
            digest=project.baseline_index_digest,
            detail=(
                "Repository-index authority could not be established without a rebuild: "
                f"{redact_text(str(exc))}"
            ),
        )
    if (
        project.baseline_index_digest is None
        or project.baseline_index_digest != status.aggregate_digest
    ):
        return IndexInspection(
            freshness="unknown",
            digest=status.aggregate_digest,
            indexed_at=status.indexed_at,
            detail=(
                "The valid index generation is not bound to the current project profile "
                f"({metadata.st_size} bytes); rebuild it through Project setup."
            ),
        )
    freshness: IndexFreshness = (
        "current" if status.freshness.value == "current" else "stale"
    )
    return IndexInspection(
        freshness=freshness,
        digest=status.aggregate_digest,
        indexed_at=status.indexed_at,
        detail=(
            "Repository index matches the current repository snapshot."
            if freshness == "current"
            else "Repository contents changed after the last index build."
        ),
    )


def inspect_provider_readiness(
    *,
    project: ProjectRecord,
    config: Any,
    routing_config: Any,
    provider_profiles: Sequence[Any],
    model_targets: Sequence[Any],
    route_policies: Sequence[Any],
    template_id: str,
) -> ProviderInspection:
    """Evaluate provider readiness from durable target state without making a network call."""

    active_policy_id = str(
        project.provider_policy.get("policy_id")
        or getattr(routing_config, "policy_id", "balanced")
    )
    direct_policy_label = str(
        project.provider_policy.get("preset")
        or project.provider_policy.get("policy_id")
        or "direct"
    )
    routing_enabled = bool(getattr(routing_config, "enabled", False))
    routing_mode = str(getattr(routing_config, "mode", "off"))
    routing_actionable = routing_enabled and routing_mode in {
        "constrained",
        "adaptive",
    }
    estimated_calls = _estimated_provider_calls(template_id)
    provider = str(getattr(config, "provider", "")).strip()
    model = str(getattr(config, "model", "")).strip()
    if not routing_actionable:
        try:
            direct_cost = validate_project_direct_route(
                project,
                provider=provider,
                model=model,
                base_url=getattr(config, "base_url", None),
            )
        except ValueError as exc:
            return ProviderInspection(
                status="fail",
                detail=redact_text(str(exc)),
                route_policy=direct_policy_label,
                estimated_cost_usd=None,
            )
        direct_estimate = (
            None if direct_cost is None else direct_cost * estimated_calls
        )
        if provider.casefold() == "mock":
            return ProviderInspection(
                status="warn",
                detail=(
                    "The deterministic mock provider is ready for demos and tests, not "
                    "real engineering completion."
                ),
                route_policy=direct_policy_label,
                estimated_cost_usd=direct_estimate,
            )
        direct_health = str(
            project.provider_policy.get("direct_health", "")
        ).strip().casefold()
        if direct_health != "healthy":
            return ProviderInspection(
                status="warn",
                detail=(
                    f"Direct provider {provider} with model {model} is configured, but "
                    "no current healthy probe evidence is bound to the project."
                ),
                route_policy=(
                    f"{active_policy_id} shadow / {direct_policy_label} actual"
                    if routing_enabled
                    else direct_policy_label
                ),
                estimated_cost_usd=direct_estimate,
            )
        return ProviderInspection(
            status="pass",
            detail=(
                f"Direct provider {provider} with model {model} has project-bound "
                "healthy probe evidence."
            ),
            route_policy=(
                f"{active_policy_id} shadow / {direct_policy_label} actual"
                if routing_enabled
                else direct_policy_label
            ),
            estimated_cost_usd=direct_estimate,
        )

    policy_entry = next(
        (
            entry
            for entry in route_policies
            if str(entry.policy.policy_id) == active_policy_id and bool(entry.enabled)
        ),
        None,
    )
    if policy_entry is None:
        return ProviderInspection(
            status="fail",
            detail=f"Route policy {active_policy_id!r} is unavailable.",
            route_policy=active_policy_id,
            estimated_cost_usd=None,
        )
    try:
        service = AdaptiveFlockRoutingService(
            profiles=[entry.profile for entry in provider_profiles],
            targets=[entry.target for entry in model_targets],
            policy=policy_entry.policy,
            mode=cast(RoutingMode, routing_mode),
        )
        constraints = project_routing_constraints(project)
        remaining_budget = project.cost_budget
        selected_targets: list[Any] = []
        selected_costs: list[float] = []
        missing_cost = False
        for raw_task in _plan_for(template_id):
            task_id = str(raw_task["task_id"])
            task = _MissionRouteTask(
                task_id=f"preflight-{task_id}",
                run_id=f"preflight-{project.project_id}",
                title=str(raw_task["title"]),
                goal=str(raw_task["rationale"]),
                profile=_mission_task_profile(task_id),
                risk=str(raw_task["risk"]),
                required_tools=tuple(str(item) for item in raw_task["required_tools"]),
                acceptance_criteria=tuple(
                    str(item) for item in raw_task["acceptance_criteria"]
                ),
                dependencies=tuple(str(item) for item in raw_task["dependencies"]),
            )
            _contract, decision = service.preview(
                task,
                default_privacy_class=constraints["default_privacy_class"],
                local_required=bool(constraints["local_required"]),
                maximum_cost_usd=remaining_budget,
                allowed_target_ids=constraints["allowed_target_ids"],
                forbidden_target_ids=constraints["forbidden_target_ids"],
                allowed_provider_profiles=constraints["allowed_provider_profiles"],
                forbidden_provider_profiles=constraints[
                    "forbidden_provider_profiles"
                ],
            )
            target = decision.selected_target
            selected_targets.append(target)
            if target.estimated_cost_usd is None:
                missing_cost = True
            else:
                selected_costs.append(float(target.estimated_cost_usd))
                if remaining_budget is not None:
                    remaining_budget = max(
                        0.0,
                        remaining_budget - float(target.estimated_cost_usd),
                    )
    except (RoutingUnavailableError, ValueError) as exc:
        return ProviderInspection(
            status="fail",
            detail=redact_text(f"No production-equivalent route satisfies the project: {exc}"),
            route_policy=active_policy_id,
            estimated_cost_usd=None,
        )
    health = {str(target.health) for target in selected_targets}
    status: MissionCheckStatus = (
        "pass" if health <= {"healthy"} and not missing_cost else "warn"
    )
    estimate = None if missing_cost else sum(selected_costs)
    selected_ids = ", ".join(str(target.target_id) for target in selected_targets)
    return ProviderInspection(
        status=status,
        detail=(
            "Production-equivalent task contracts selected "
            f"{selected_ids}; health states: {', '.join(sorted(health))}. "
            + (
                "At least one selected target has no cost estimate."
                if missing_cost
                else "Every selected target has cost attribution."
            )
        ),
        route_policy=active_policy_id,
        estimated_cost_usd=estimate,
    )


def _git_first_line(
    repository_root: Path,
    arguments: tuple[str, ...],
    *,
    deadline: float,
    maximum_bytes: int = 8_192,
) -> tuple[str, int]:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    environment = hardened_readonly_git_environment()
    environment["LC_ALL"] = "C"
    command = hardened_readonly_git_command(
        ["-C", str(repository_root), *arguments],
    )
    process = subprocess.Popen(  # noqa: S603 - verified Git executable and structured argv
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=os.name != "nt",
    )
    stream = process.stdout
    if stream is None:
        _stop_process(process)
        raise OSError("Git preflight stdout pipe was not created")
    first_line = bytearray()
    first_line_complete = False
    first_line_too_large = False
    reader_error: list[OSError] = []

    def drain_output() -> None:
        nonlocal first_line_complete, first_line_too_large
        try:
            while chunk := stream.read(8_192):
                if first_line_complete:
                    continue
                newline = chunk.find(b"\n")
                fragment = chunk if newline < 0 else chunk[:newline]
                remaining_capacity = maximum_bytes + 1 - len(first_line)
                if remaining_capacity > 0:
                    first_line.extend(fragment[:remaining_capacity])
                if len(first_line) > maximum_bytes or (
                    newline < 0 and len(fragment) > remaining_capacity
                ):
                    first_line_too_large = True
                if newline >= 0:
                    first_line_complete = True
        except OSError as exc:
            reader_error.append(exc)

    reader = Thread(
        target=drain_output,
        name="kestrel-git-preflight-reader",
        daemon=True,
    )
    reader.start()
    reader.join(timeout=remaining)
    if reader.is_alive():
        _stop_process(process)
        reader.join(timeout=0.25)
        raise TimeoutError
    remaining = deadline - monotonic()
    if remaining <= 0:
        _stop_process(process)
        raise TimeoutError
    try:
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        raise TimeoutError from None
    if reader_error:
        raise OSError(f"Git preflight output could not be read: {reader_error[0]}")
    if first_line_too_large:
        raise OSError("Git preflight output exceeded its bounded line envelope")
    return (
        redact_text(bytes(first_line).decode("utf-8", errors="replace").strip()),
        return_code,
    )


def _git_output_bytes(
    repository_root: Path,
    arguments: tuple[str, ...],
    *,
    deadline: float,
    maximum_bytes: int,
) -> tuple[bytes, int]:
    """Read one Git result with a hard time and memory envelope."""

    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    environment = hardened_readonly_git_environment()
    environment["LC_ALL"] = "C"
    command = hardened_readonly_git_command(
        ["-C", str(repository_root), *arguments],
    )
    process = subprocess.Popen(  # noqa: S603 - verified Git executable and structured argv
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=os.name != "nt",
    )
    stream = process.stdout
    if stream is None:
        _stop_process(process)
        raise OSError("Git preflight stdout pipe was not created")
    output = bytearray()
    output_too_large = False
    reader_error: list[OSError] = []

    def drain_output() -> None:
        nonlocal output_too_large
        try:
            while chunk := stream.read(64 * 1024):
                capacity = maximum_bytes + 1 - len(output)
                if capacity > 0:
                    output.extend(chunk[:capacity])
                if len(output) > maximum_bytes or len(chunk) > capacity:
                    output_too_large = True
                    _stop_process(process)
                    return
        except OSError as exc:
            reader_error.append(exc)

    reader = Thread(
        target=drain_output,
        name="kestrel-git-preflight-output-reader",
        daemon=True,
    )
    reader.start()
    reader.join(timeout=remaining)
    if reader.is_alive():
        _stop_process(process)
        reader.join(timeout=0.25)
        raise TimeoutError
    remaining = deadline - monotonic()
    if remaining <= 0:
        _stop_process(process)
        raise TimeoutError
    try:
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        raise TimeoutError from None
    if reader_error:
        raise OSError(f"Git preflight output could not be read: {reader_error[0]}")
    if output_too_large:
        raise OSError(
            f"Git preflight output exceeded its {maximum_bytes}-byte envelope"
        )
    return bytes(output), return_code


def _untracked_content_manifest(
    repository_root: Path,
    encoded_paths: bytes,
    *,
    deadline: float,
) -> list[dict[str, object]]:
    raw_paths = [item for item in encoded_paths.split(b"\0") if item]
    if len(raw_paths) > _MAX_GIT_PREFLIGHT_UNTRACKED_FILES:
        raise OSError(
            "Git preflight found too many untracked files to bind safely"
        )
    manifest: list[dict[str, object]] = []
    total_bytes = 0
    for raw_path in sorted(set(raw_paths)):
        if monotonic() > deadline:
            raise TimeoutError
        relative = os.fsdecode(raw_path)
        pure_path = PurePosixPath(relative)
        if (
            not relative
            or pure_path.is_absolute()
            or any(part in {"", ".", ".."} for part in pure_path.parts)
        ):
            raise OSError("Git reported an unsafe untracked path")
        candidate = repository_root.joinpath(*pure_path.parts)
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise OSError("An untracked path changed during Git preflight") from exc
        if stat.S_ISLNK(metadata.st_mode):
            target = os.fsencode(os.readlink(candidate))
            total_bytes += len(target)
            if total_bytes > _MAX_GIT_PREFLIGHT_UNTRACKED_BYTES:
                raise OSError(
                    "Untracked content exceeds the Git preflight envelope"
                )
            manifest.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "size": len(target),
                    "sha256": hashlib.sha256(target).hexdigest(),
                }
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Git reported an unsupported untracked path type")
        if metadata.st_size > _MAX_GIT_PREFLIGHT_UNTRACKED_FILE_BYTES:
            raise OSError(
                "An untracked file exceeds the Git preflight per-file envelope"
            )
        total_bytes += metadata.st_size
        if total_bytes > _MAX_GIT_PREFLIGHT_UNTRACKED_BYTES:
            raise OSError("Untracked content exceeds the Git preflight envelope")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = _PLATFORM_OS.open(candidate, flags)
        try:
            opened = _PLATFORM_OS.fstat(descriptor)
            digest = hashlib.sha256()
            read_bytes = 0
            while chunk := _PLATFORM_OS.read(descriptor, 64 * 1024):
                if monotonic() > deadline:
                    raise TimeoutError
                read_bytes += len(chunk)
                if read_bytes > _MAX_GIT_PREFLIGHT_UNTRACKED_FILE_BYTES:
                    raise OSError(
                        "An untracked file exceeded the Git preflight envelope"
                    )
                digest.update(chunk)
            after = _PLATFORM_OS.fstat(descriptor)
            try:
                visible_after = candidate.lstat()
            except OSError as exc:
                raise OSError(
                    "An untracked path changed during Git preflight"
                ) from exc
            if _untracked_file_changed(
                visible_before=metadata,
                opened_before=opened,
                opened_after=after,
                visible_after=visible_after,
                read_bytes=read_bytes,
            ):
                raise OSError("An untracked file changed during Git preflight")
        finally:
            _PLATFORM_OS.close(descriptor)
        manifest.append(
            {
                "path": relative,
                "kind": "file",
                "mode": _regular_worktree_mode(metadata),
                "size": opened.st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return manifest


def _untracked_file_changed(
    *,
    visible_before: os.stat_result,
    opened_before: os.stat_result,
    opened_after: os.stat_result,
    visible_after: os.stat_result,
    read_bytes: int,
) -> bool:
    if (
        not stat.S_ISREG(opened_before.st_mode)
        or not stat.S_ISREG(opened_after.st_mode)
        or not stat.S_ISREG(visible_after.st_mode)
        or _descriptor_file_snapshot(opened_before)
        != _descriptor_file_snapshot(opened_after)
        or read_bytes != opened_before.st_size
    ):
        return True
    if getattr(_PLATFORM_OS, "name", os.name) == "nt":
        return (
            _path_file_snapshot(visible_before)
            != _path_file_snapshot(visible_after)
            or opened_before.st_size != visible_before.st_size
        )
    return (
        not os.path.samestat(visible_before, opened_before)
        or not os.path.samestat(opened_after, visible_after)
        or opened_before.st_mtime_ns != visible_before.st_mtime_ns
        or opened_after.st_mtime_ns != visible_after.st_mtime_ns
    )


def _descriptor_file_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _path_file_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
        int(getattr(metadata, "st_file_attributes", 0)),
        int(getattr(metadata, "st_reparse_tag", 0)),
    )


def _regular_worktree_mode(metadata: os.stat_result) -> int:
    if getattr(_PLATFORM_OS, "name", os.name) == "nt":
        return 0o644
    return stat.S_IMODE(metadata.st_mode)


def _is_git_object_id(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            _PLATFORM_OS.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                _PLATFORM_OS.killpg(process.pid, _PLATFORM_SIGNAL.SIGKILL)
            except ProcessLookupError:
                return
        else:
            process.kill()
        process.wait(timeout=0.2)


def _plan_for(template_id: str) -> list[dict[str, Any]]:
    if template_id in {"explain_repository", "security_review"}:
        final_title = (
            "Produce evidence-backed findings"
            if template_id == "security_review"
            else "Explain the repository"
        )
        return [
            _task(
                "map",
                "Map the repository",
                "Identify bounded entry points, build surfaces, and important directories.",
                (),
                ("Repository boundaries and entry points are cited.",),
                ("repo.map", "repo.symbols", "repo.dependencies"),
                "low",
            ),
            _task(
                "trace",
                "Trace important relationships",
                "Follow definitions, references, and configuration with exact file evidence.",
                ("map",),
                ("Important execution relationships have file and line evidence.",),
                ("repo.references", "repo.tests_for", "repo.context_pack", "file.read"),
                "low",
            ),
            _task(
                "synthesize",
                final_title,
                "Separate confirmed evidence, inference, risk, and unknowns.",
                ("trace",),
                ("The objective is answered without treating stale evidence as authority.",),
                ("file.read",),
                "low",
            ),
        ]
    if template_id == "documentation":
        return [
            _task(
                "understand",
                "Establish repository evidence",
                "Map the documented surface before drafting prose.",
                (),
                ("Relevant entry points and current behavior are cited.",),
                ("repo.map", "repo.dependencies", "repo.context_pack", "file.read"),
                "low",
            ),
            _task(
                "draft",
                "Draft the documentation change",
                "Write the smallest documentation change grounded in current code.",
                ("understand",),
                ("Documentation matches the repository evidence.",),
                ("file.write",),
                "high",
            ),
            _task(
                "review",
                "Review the documentation patch",
                "Inspect the literal diff and validate referenced commands where configured.",
                ("draft",),
                ("The diff is reviewable and validation evidence is attached.",),
                ("git.diff",),
                "low",
            ),
        ]
    return [
        _task(
            "understand",
            "Reproduce and map the objective",
            "Establish current behavior and the smallest relevant code surface.",
            (),
            ("Current behavior or failure is reproduced with exact evidence.",),
            (
                "repo.map",
                "repo.symbols",
                "repo.impact",
                "repo.tests_for",
                "repo.context_pack",
                "file.read",
            ),
            "low",
        ),
        _task(
            "repair",
            "Implement in isolation",
            "Prepare an isolated repair worktree and apply the smallest compatible change.",
            ("understand",),
            ("The candidate change is isolated from the user's working tree.",),
            ("repair.prepare", "repair.apply_patch"),
            "high",
        ),
        _task(
            "validate",
            "Prove the acceptance criteria",
            "Run targeted validation first, then the configured broader evidence.",
            ("repair",),
            ("Every acceptance criterion maps to passing or explicitly failed evidence.",),
            ("repair.validate",),
            "high",
        ),
        _task(
            "review",
            "Review risk and rollback",
            "Bind the exact candidate tree, validation, risks, and rollback into review evidence.",
            ("validate",),
            ("A current review artifact explains patch, proof, risk, and rollback.",),
            ("repair.review",),
            "high",
        ),
    ]


def _task(
    task_id: str,
    title: str,
    rationale: str,
    dependencies: tuple[str, ...],
    acceptance_criteria: tuple[str, ...],
    required_tools: tuple[str, ...],
    risk: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "title": title,
        "rationale": rationale,
        "dependencies": list(dependencies),
        "acceptance_criteria": list(acceptance_criteria),
        "required_tools": list(required_tools),
        "risk": risk,
    }


def _validation_recipes(project: ProjectRecord) -> list[str]:
    recipes: list[str] = []
    for recipe in (*project.test_recipes, *project.build_recipes):
        name = redact_text(str(recipe.get("name", "validation")))
        command = redact_text(str(recipe.get("command", "")))
        recipes.append(f"{name}: {command}")
    return recipes


def _budget_check(
    *,
    limit: float | None,
    estimate: float | None,
) -> tuple[MissionCheckStatus, str]:
    if limit is None and estimate is None:
        return (
            "warn",
            "No project cost cap or provider cost estimate is available; missing data is not zero.",
        )
    if estimate is None:
        return (
            "warn",
            (
                f"The project cap is ${limit:.2f}, but the selected route has no cost estimate."
                if limit is not None
                else "The selected route has no cost estimate."
            ),
        )
    if limit is None:
        return (
            "warn",
            f"The estimated route cost is ${estimate:.2f}, but the project has no cost cap.",
        )
    if estimate > limit:
        return (
            "fail",
            f"The estimated route cost ${estimate:.2f} exceeds the ${limit:.2f} project budget.",
        )
    return (
        "pass",
        f"The estimated route cost ${estimate:.2f} is within the ${limit:.2f} project budget.",
    )


def _check(
    check_id: str,
    title: str,
    status: MissionCheckStatus,
    detail: str,
    recovery: str | None,
) -> dict[str, str | None]:
    return {
        "check_id": check_id,
        "title": title,
        "status": status,
        "detail": detail,
        "recovery": recovery,
    }


def _estimated_provider_calls(template_id: str) -> int:
    if template_id in {"explain_repository", "security_review"}:
        return 3
    if template_id == "documentation":
        return 3
    return 4


def _mission_task_profile(source_task_id: str) -> str:
    if source_task_id in {"review", "synthesize"}:
        return "reviewer"
    if source_task_id in {"map", "trace", "understand"}:
        return "planner"
    return "worker"


def _deduplicate(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _credential_reference_digest(reference: str, *, purpose: str) -> str:
    normalized = reference.strip()
    if not normalized:
        return ""
    salt = f"kestrel:mission-launch:{purpose}:credential-reference:v1".encode()
    return hashlib.scrypt(
        normalized.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    ).hex()


def _payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha3_256(encoded).hexdigest()
