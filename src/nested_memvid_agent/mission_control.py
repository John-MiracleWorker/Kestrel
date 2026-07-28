from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Any, Literal

from .projects import ProjectRecord, canonical_repository_path
from .repair_integrity import (
    hardened_readonly_git_command,
    hardened_readonly_git_environment,
    trusted_git_executable,
)
from .security_boundary import redact_text

MissionCheckStatus = Literal["pass", "warn", "fail", "unknown"]
WorkingTreeState = Literal["clean", "dirty", "unknown"]
IndexFreshness = Literal["current", "stale", "missing", "unknown"]

MISSION_PREFLIGHT_SCHEMA = "kestrel.mission_preflight.v1"
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
_LOCAL_PROVIDERS = frozenset(
    {"mock", "ollama", "lmstudio", "lm-studio", "local", "llamacpp", "llama.cpp"}
)
_UNAVAILABLE_HEALTH = frozenset({"open", "unavailable"})


@dataclass(frozen=True)
class GitInspection:
    branch: str
    state: WorkingTreeState
    summary: str


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


def build_mission_preflight(
    *,
    project: ProjectRecord,
    objective: str,
    template_id: str,
    git: GitInspection,
    index: IndexInspection,
    provider: ProviderInspection,
    capability_catalog: Sequence[Mapping[str, object]],
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

    tasks = _plan_for(template_id)
    required_tools = sorted(
        {
            str(tool)
            for task in tasks
            for tool in task["required_tools"]
        }
    )
    allowed_capabilities = set(project.capability_ceiling)
    effective_items = [
        item
        for item in capability_catalog
        if bool(item.get("effective_enabled"))
        and str(item.get("key", "")) in allowed_capabilities
    ]
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
        "project_name": project.display_name,
        "repository_path": project.repository_path,
        "objective": normalized_objective,
        "template_id": template_id,
        "branch": git.branch or project.default_branch,
        "working_tree": {
            "state": git.state,
            "summary": git.summary,
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
        "checks": checks,
        "tasks": tasks,
        "warnings": _deduplicate(warnings),
        "blockers": _deduplicate(blockers),
        "can_start": not blockers,
        "generated_at": created.astimezone(UTC).isoformat(),
    }


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
        inside, _ = _git_first_line(
            root,
            ("rev-parse", "--is-inside-work-tree"),
            deadline=deadline,
        )
        if inside.strip() != "true":
            return GitInspection(
                branch="not-a-git-worktree",
                state="unknown",
                summary="The project directory is not a Git worktree.",
            )
        branch, _ = _git_first_line(
            root,
            ("symbolic-ref", "--short", "-q", "HEAD"),
            deadline=deadline,
        )
        if not branch:
            detached, _ = _git_first_line(
                root,
                ("rev-parse", "--short", "HEAD"),
                deadline=deadline,
            )
            branch = f"detached@{detached}" if detached else "detached"
        tracked, _ = _git_first_line(
            root,
            ("status", "--porcelain=v1", "--untracked-files=no"),
            deadline=deadline,
        )
        untracked, _ = _git_first_line(
            root,
            (
                "ls-files",
                "--others",
                "--exclude-standard",
                "--directory",
                "--no-empty-directory",
            ),
            deadline=deadline,
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
    if untracked:
        dirty_kinds.append("untracked files")
    if not dirty_kinds:
        return GitInspection(
            branch=branch,
            state="clean",
            summary="Working tree is clean.",
        )
    return GitInspection(
        branch=branch,
        state="dirty",
        summary=f"{' and '.join(dirty_kinds).capitalize()} are present.",
    )


def inspect_index_without_mutation(project: ProjectRecord) -> IndexInspection:
    """Describe an existing sidecar without creating, migrating, or opening it."""

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
    return IndexInspection(
        freshness="unknown",
        digest=project.baseline_index_digest,
        detail=(
            "A repository-index sidecar exists, but this read-only preflight cannot bind "
            f"its generation and freshness ({metadata.st_size} bytes)."
        ),
    )


def inspect_provider_readiness(
    *,
    project: ProjectRecord,
    config: Any,
    routing_config: Any,
    provider_profiles: Sequence[Any],
    model_targets: Sequence[Any],
    template_id: str,
) -> ProviderInspection:
    """Evaluate provider readiness from durable target state without making a network call."""

    policy = str(
        project.provider_policy.get("policy_id")
        or project.provider_policy.get("preset")
        or getattr(routing_config, "policy_id", "direct")
    )
    routing_enabled = bool(getattr(routing_config, "enabled", False))
    estimated_calls = _estimated_provider_calls(template_id)
    if not routing_enabled:
        provider = str(getattr(config, "provider", "")).strip()
        model = str(getattr(config, "model", "")).strip()
        if not provider or not model:
            return ProviderInspection(
                status="fail",
                detail="No direct provider and model are configured.",
                route_policy=policy,
                estimated_cost_usd=None,
            )
        if project.privacy_class == "local_required" and provider.casefold() not in _LOCAL_PROVIDERS:
            return ProviderInspection(
                status="fail",
                detail=(
                    "The project requires local execution, but the direct provider has no "
                    "locality evidence."
                ),
                route_policy=policy,
                estimated_cost_usd=None,
            )
        if provider.casefold() == "mock":
            return ProviderInspection(
                status="warn",
                detail=(
                    "The deterministic mock provider is ready for demos and tests, not "
                    "real engineering completion."
                ),
                route_policy=policy,
                estimated_cost_usd=0.0,
            )
        return ProviderInspection(
            status="pass",
            detail=f"Direct provider {provider} with model {model} is configured.",
            route_policy=policy,
            estimated_cost_usd=None,
        )

    enabled_profiles = {
        str(entry.profile.profile_id)
        for entry in provider_profiles
        if bool(entry.profile.enabled)
    }
    targets = [
        entry.target
        for entry in model_targets
        if bool(entry.target.enabled)
        and str(entry.target.provider_profile_id) in enabled_profiles
        and str(entry.target.health) not in _UNAVAILABLE_HEALTH
        and _target_matches_project(entry.target, project)
    ]
    if not targets:
        return ProviderInspection(
            status="fail",
            detail="No policy-eligible durable routing target is currently available.",
            route_policy=policy,
            estimated_cost_usd=None,
        )
    health = {str(target.health) for target in targets}
    status: MissionCheckStatus = (
        "pass" if health <= {"healthy"} else "warn"
    )
    costs = [
        float(target.estimated_cost_usd)
        for target in targets
        if target.estimated_cost_usd is not None
    ]
    estimate = min(costs) * estimated_calls if costs else None
    return ProviderInspection(
        status=status,
        detail=(
            f"{len(targets)} eligible target{'s are' if len(targets) != 1 else ' is'} "
            f"available with health states: {', '.join(sorted(health))}."
        ),
        route_policy=policy,
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
    captured: list[bytes] = []

    def read_one() -> None:
        captured.append(stream.readline(maximum_bytes + 1))

    reader = Thread(target=read_one, name="kestrel-git-preflight-reader", daemon=True)
    reader.start()
    reader.join(timeout=remaining)
    if reader.is_alive():
        _stop_process(process)
        raise TimeoutError
    if process.poll() is None:
        _stop_process(process)
    return_code = process.wait(timeout=0.25)
    line = captured[0] if captured else b""
    if len(line) > maximum_bytes:
        return "", return_code
    return redact_text(line.decode("utf-8", errors="replace").strip()), return_code


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
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
                ("repo.map", "repo.search"),
                "low",
            ),
            _task(
                "trace",
                "Trace important relationships",
                "Follow definitions, references, and configuration with exact file evidence.",
                ("map",),
                ("Important execution relationships have file and line evidence.",),
                ("repo.search", "file.read"),
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
                ("repo.map", "repo.search", "file.read"),
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
            ("repo.map", "repo.search", "file.read"),
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


def _target_matches_project(target: Any, project: ProjectRecord) -> bool:
    policy = project.provider_policy
    target_id = str(target.target_id)
    profile_id = str(target.provider_profile_id)
    allowed_targets = _string_set(policy.get("allowed_targets"))
    forbidden_targets = _string_set(policy.get("forbidden_targets"))
    allowed_profiles = _string_set(policy.get("allowed_profiles"))
    forbidden_profiles = _string_set(policy.get("forbidden_profiles"))
    if allowed_targets and target_id not in allowed_targets:
        return False
    if target_id in forbidden_targets:
        return False
    if allowed_profiles and profile_id not in allowed_profiles:
        return False
    if profile_id in forbidden_profiles:
        return False
    local_only = str(policy.get("preset", "")).casefold() in {
        "local_only",
        "local only",
        "privacy_first",
        "privacy-first",
    }
    if (local_only or project.privacy_class == "local_required") and str(
        target.locality
    ) not in {"local", "hybrid"}:
        return False
    return True


def _string_set(value: object) -> frozenset[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return frozenset()
    return frozenset(str(item) for item in value)


def _estimated_provider_calls(template_id: str) -> int:
    if template_id in {"explain_repository", "security_review"}:
        return 3
    if template_id == "documentation":
        return 3
    return 4


def _deduplicate(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))
