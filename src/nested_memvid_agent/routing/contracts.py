from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .models import AgentTaskContract, PrivacyClass


class TaskLike(Protocol):
    task_id: str
    run_id: str
    title: str
    goal: str
    profile: str
    risk: str
    required_tools: Sequence[str]
    acceptance_criteria: Sequence[str]
    dependencies: Sequence[str]
    plan: Mapping[str, Any] | None


def compile_task_contract(
    task: TaskLike,
    *,
    planner_guidance: Mapping[str, Any] | None = None,
    default_privacy_class: PrivacyClass = "approved_cloud",
    local_required: bool = False,
    maximum_cost_usd: float | None = None,
) -> AgentTaskContract:
    text = f"{task.title} {task.goal}".strip()
    lowered = text.lower()
    task_family = _task_family(lowered, tuple(task.required_tools), task.profile)
    complexity = _complexity(lowered, task)
    ambiguity = _ambiguity(lowered, task)
    capabilities = set(_required_capabilities(task, lowered))
    modalities = set(_required_modalities(lowered))
    preferred_tags = set(_preferred_tags(task_family, task.profile))
    minimum_context = _minimum_context_tokens(task_family, complexity)

    guidance = planner_guidance or {}
    proposed_family = _clean_optional_string(guidance.get("task_family"))
    if proposed_family in _TASK_FAMILIES:
        task_family = proposed_family
    complexity = max(complexity, _bounded_float(guidance.get("complexity"), default=complexity))
    ambiguity = max(ambiguity, _bounded_float(guidance.get("ambiguity"), default=ambiguity))
    capabilities.update(_string_items(guidance.get("required_capabilities")))
    modalities.update(_string_items(guidance.get("required_modalities")))
    preferred_tags.update(_string_items(guidance.get("preferred_target_tags")))
    proposed_context = _positive_int_or_none(guidance.get("minimum_context_tokens"))
    if proposed_context is not None:
        minimum_context = max(minimum_context or 0, proposed_context)

    deterministic_local = local_required or default_privacy_class == "local_required"
    privacy_class: PrivacyClass = "local_required" if deterministic_local else default_privacy_class
    plan = task.plan or {}
    acceptance_modes = tuple(_string_items(plan.get("acceptance_evidence")))

    return AgentTaskContract(
        task_id=task.task_id,
        run_id=task.run_id,
        role=task.profile,
        task_family=task_family,
        objective=task.goal,
        complexity=round(complexity, 4),
        ambiguity=round(ambiguity, 4),
        risk=task.risk,
        required_tools=tuple(sorted(set(str(item) for item in task.required_tools))),
        required_capabilities=tuple(sorted(capabilities)),
        required_modalities=tuple(sorted(modalities)),
        minimum_context_tokens=minimum_context,
        structured_output_required=bool(task.acceptance_criteria or acceptance_modes),
        privacy_class=privacy_class,
        local_preferred=privacy_class == "local_preferred" or deterministic_local,
        local_required=deterministic_local,
        maximum_cost_usd=maximum_cost_usd,
        preferred_target_tags=tuple(sorted(preferred_tags)),
    )


_TASK_FAMILIES = {
    "planning",
    "architecture",
    "security_review",
    "repository_inspection",
    "frontend_design",
    "frontend_implementation",
    "backend_implementation",
    "bounded_code_change",
    "mechanical_refactor",
    "test_and_validation",
    "documentation",
    "research",
    "review",
    "recovery",
    "general",
}


def _task_family(text: str, tools: tuple[str, ...], profile: str) -> str:
    tool_set = set(tools)
    if profile == "reviewer":
        return "review"
    if profile == "planner":
        return "planning"
    if any(term in text for term in ("security", "vulnerability", "threat model", "auth boundary")):
        return "security_review"
    if any(term in text for term in ("architecture", "system design", "data model", "concurrency design")):
        return "architecture"
    if any(term in text for term in ("figma", "mockup", "visual design", "ux", "user experience")):
        return "frontend_design"
    if any(term in text for term in ("frontend", "react", "css", "component", "page", "accessibility")):
        return "frontend_implementation"
    if any(term in text for term in ("backend", "api", "database", "migration", "service")):
        return "backend_implementation"
    if tool_set & {"test.run", "lint.run", "repair.validate", "repair.orchestrate_validate"}:
        return "test_and_validation"
    if tool_set and tool_set <= {"repo.search", "repo.map", "memory.search", "context.pack", "file.read", "file.list"}:
        return "repository_inspection"
    if any(term in text for term in ("rename", "replace", "mechanical", "repetitive", "all occurrences")):
        return "mechanical_refactor"
    if any(term in text for term in ("document", "readme", "docs", "changelog", "guide")):
        return "documentation"
    if any(term in text for term in ("research", "investigate", "compare", "find sources")):
        return "research"
    if any(term in text for term in ("fix", "patch", "implement", "update", "change")):
        return "bounded_code_change"
    return "general"


def _complexity(text: str, task: TaskLike) -> float:
    value = 0.22
    value += min(len(tuple(task.required_tools)), 6) * 0.055
    value += min(len(tuple(task.dependencies)), 4) * 0.035
    value += min(len(tuple(task.acceptance_criteria)), 4) * 0.04
    value += {"low": 0.0, "medium": 0.10, "high": 0.22, "critical": 0.34}.get(task.risk, 0.08)
    if any(term in text for term in ("architecture", "security", "concurrency", "race condition", "redesign", "migration")):
        value += 0.22
    if any(term in text for term in ("repository-wide", "entire repository", "multi-file", "across the codebase")):
        value += 0.15
    return min(1.0, value)


def _ambiguity(text: str, task: TaskLike) -> float:
    value = 0.22
    if any(term in text for term in ("design", "best", "improve", "investigate", "determine", "figure out", "architecture")):
        value += 0.30
    if any(term in text for term in ("only", "exactly", "rename", "replace", "specified", "targeted")):
        value -= 0.16
    if task.acceptance_criteria:
        value -= min(len(tuple(task.acceptance_criteria)), 3) * 0.04
    if task.required_tools:
        value -= 0.04
    return max(0.0, min(1.0, value))


def _required_capabilities(task: TaskLike, text: str) -> tuple[str, ...]:
    capabilities: set[str] = set()
    if task.required_tools:
        capabilities.add("tools")
    if task.acceptance_criteria:
        capabilities.add("structured_output")
    if task.profile in {"planner", "reviewer"} or any(term in text for term in ("architecture", "security", "reason")):
        capabilities.add("reasoning")
    if _required_modalities(text):
        capabilities.add("vision")
    return tuple(sorted(capabilities))


def _required_modalities(text: str) -> tuple[str, ...]:
    return ("image",) if any(term in text for term in ("screenshot", "image", "figma", "mockup", "visual reference")) else ()


def _preferred_tags(task_family: str, role: str) -> tuple[str, ...]:
    tags = {role, task_family}
    if task_family in {"frontend_design", "frontend_implementation"}:
        tags.add("frontend")
    if task_family in {"bounded_code_change", "mechanical_refactor", "backend_implementation"}:
        tags.add("coding")
    if task_family == "repository_inspection":
        tags.add("scout")
    return tuple(sorted(tags))


def _minimum_context_tokens(task_family: str, complexity: float) -> int:
    baseline = 16_000
    if task_family in {"architecture", "security_review", "planning"}:
        baseline = 64_000
    elif task_family in {"frontend_implementation", "backend_implementation", "bounded_code_change"}:
        baseline = 32_000
    if complexity >= 0.8:
        baseline = max(baseline, 96_000)
    return baseline


def _string_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))


def _clean_optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bounded_float(value: object, *, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))


def _positive_int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value
