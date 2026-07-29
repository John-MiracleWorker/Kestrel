"""Versioned machine-report contract for Kestrel's everyday golden evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

GOLDEN_REPORT_SCHEMA = "kestrel.golden_eval_report.v2"
GOLDEN_CASE_CATEGORIES: Mapping[str, str] = MappingProxyType(
    {
        "remember_correction_across_sessions": "memory_precision_recall",
        "retrieve_prior_failure": "memory_precision_recall",
        "use_procedural_recipe_after_repeats": "memory_precision_recall",
        "refuse_path_escape": "approval_correctness",
        "block_shell_without_enablement": "approval_correctness",
        "verify_mv2_files": "repo_regression",
        "compile_context_under_budget": "memory_precision_recall",
        "summary_first_expand_raw_on_demand": "memory_precision_recall",
        "flag_conflicting_facts": "memory_precision_recall",
        "create_capsule_and_consolidate_validated_lessons": "memory_precision_recall",
        "mv2_not_sqlite_or_vector_db_substrate": "repo_regression",
        "avoid_policy_from_ordinary_event": "approval_correctness",
        "explain_memory_promotion_gates": "approval_correctness",
        "map_repository": "repo_regression",
        "apply_patch_and_run_tests": "repair_success_rate",
        "report_test_failure_honestly": "hallucinated_success_rate",
        "no_success_claim_without_evidence": "hallucinated_success_rate",
        "tool_call_accuracy_search": "tool_call_accuracy",
        "approval_requires_exact_call": "approval_correctness",
        "durable_plan_completion": "plan_completion_rate",
        "repo_regression_guard": "repo_regression",
    }
)

_TOP_LEVEL_FIELDS = {
    "schema",
    "configuration",
    "results",
    "summary",
    "acceptance",
    "passed",
}
_CONFIGURATION_FIELDS = {
    "backend",
    "provider",
    "model",
    "seed",
    "max_case_latency_ms",
}
_SUMMARY_FIELDS = {
    "pass_count",
    "fail_count",
    "latency_ms_max",
    "context_chars_max",
    "tool_count_total",
    "cost_estimate_usd_total",
    "categories",
    "acceptance",
    "promotion_precision",
    "false_promotion_count",
}
_RESULT_REQUIRED_FIELDS = {
    "name",
    "category",
    "passed",
    "score",
    "latency_ms",
    "memory_hits",
    "context_chars",
    "tool_count",
    "cost_estimate_usd",
}
_LATENCY_FIELDS = {
    "measurement_status",
    "gate_configured",
    "required",
    "threshold_max_case_latency_ms",
    "latency_ms_max",
    "passed",
}
_COST_FIELDS = {
    "measurement_status",
    "gate_configured",
    "required",
    "measured_case_count",
    "unmeasured_case_count",
    "cost_estimate_usd_total",
    "passed",
    "residual",
}


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_exact_fields(
    value: object,
    expected: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} has unexpected fields: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def validate_golden_report(
    report: dict[str, object],
    *,
    expected_case_categories: Mapping[str, str] = GOLDEN_CASE_CATEGORIES,
    expected_seed: int | None = None,
) -> bool:
    """Validate the exact aggregate contract and derive acceptance independently."""

    _require_exact_fields(report, _TOP_LEVEL_FIELDS, label="golden report top-level fields")
    if report.get("schema") != GOLDEN_REPORT_SCHEMA:
        raise ValueError(f"golden report schema must be {GOLDEN_REPORT_SCHEMA!r}")

    configuration = _require_exact_fields(
        report.get("configuration"),
        _CONFIGURATION_FIELDS,
        label="golden report configuration",
    )
    if configuration["backend"] != "memory":
        raise ValueError("determinism golden report backend must be 'memory'")
    if configuration["provider"] != "mock" or configuration["model"] != "mock":
        raise ValueError("determinism golden report must use the mock provider and model")
    seed = configuration["seed"]
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("golden report seed must be an integer")
    if expected_seed is not None and seed != expected_seed:
        raise ValueError(f"golden report seed mismatch: {seed!r} != {expected_seed!r}")
    threshold = configuration["max_case_latency_ms"]
    if threshold is not None and (not _is_number(threshold) or float(threshold) <= 0):
        raise ValueError("golden report max_case_latency_ms must be null or positive")

    raw_results = report.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("golden report results must be a list")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise ValueError("golden report result must be an object")
        missing = _RESULT_REQUIRED_FIELDS - set(raw)
        if missing:
            raise ValueError(f"golden report result is missing fields: {sorted(missing)}")
        name = raw["name"]
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError(f"golden report case name is invalid or duplicated: {name!r}")
        expected_category = expected_case_categories.get(name)
        if expected_category is None:
            raise ValueError(f"golden report contains unexpected case: {name!r}")
        if raw["category"] != expected_category:
            raise ValueError(
                f"golden report category mismatch for {name!r}: "
                f"{raw['category']!r} != {expected_category!r}"
            )
        if not isinstance(raw["passed"], bool):
            raise ValueError(f"golden report passed value must be boolean for {name!r}")
        if not _is_number(raw["score"]):
            raise ValueError(f"golden report score must be numeric for {name!r}")
        expected_score = 1.0 if raw["passed"] is True else 0.0
        if float(raw["score"]) != expected_score:
            raise ValueError(f"golden report score does not match pass state for {name!r}")
        if not _is_number(raw["latency_ms"]) or float(raw["latency_ms"]) < 0:
            raise ValueError(f"golden report latency must be non-negative for {name!r}")
        for key in ("memory_hits", "context_chars", "tool_count"):
            if not isinstance(raw[key], int) or isinstance(raw[key], bool) or int(raw[key]) < 0:
                raise ValueError(f"golden report {key} must be non-negative for {name!r}")
        cost = raw["cost_estimate_usd"]
        if cost is not None and (not _is_number(cost) or float(cost) < 0):
            raise ValueError(f"golden report cost must be null or non-negative for {name!r}")
        seen.add(name)
        results.append(raw)
    expected_names = set(expected_case_categories)
    if seen != expected_names:
        raise ValueError(
            "golden report case set mismatch: "
            f"missing={sorted(expected_names - seen)}, extra={sorted(seen - expected_names)}"
        )

    summary = _require_exact_fields(
        report.get("summary"),
        _SUMMARY_FIELDS,
        label="golden report summary",
    )
    passed_count = sum(item["passed"] is True for item in results)
    failed_count = len(results) - passed_count
    if summary["pass_count"] != passed_count or summary["fail_count"] != failed_count:
        raise ValueError("golden report summary pass/fail counts do not match results")
    if not isinstance(summary["categories"], dict):
        raise ValueError("golden report summary categories must be an object")
    observed_latency = max(float(item["latency_ms"]) for item in results)
    if summary["latency_ms_max"] != observed_latency:
        raise ValueError("golden report summary maximum latency does not match results")
    if summary["context_chars_max"] != max(int(item["context_chars"]) for item in results):
        raise ValueError("golden report summary maximum context does not match results")
    if summary["tool_count_total"] != sum(int(item["tool_count"]) for item in results):
        raise ValueError("golden report summary tool count does not match results")

    summary_acceptance = _require_exact_fields(
        summary["acceptance"],
        {"latency", "cost"},
        label="golden report summary acceptance",
    )
    acceptance = _require_exact_fields(
        report.get("acceptance"),
        {"functional", "latency", "cost"},
        label="golden report acceptance",
    )
    functional = _require_exact_fields(
        acceptance["functional"],
        {"required", "passed"},
        label="golden report functional acceptance",
    )
    derived_functional = failed_count == 0
    if functional != {"required": True, "passed": derived_functional}:
        raise ValueError("golden report functional acceptance does not match results")

    latency = _require_exact_fields(
        acceptance["latency"],
        _LATENCY_FIELDS,
        label="golden report latency acceptance",
    )
    if summary_acceptance["latency"] != latency:
        raise ValueError("golden report latency acceptance copies do not match")
    latency_configured = threshold is not None
    derived_latency_passed = observed_latency <= float(threshold) if threshold is not None else None
    if (
        latency["measurement_status"] != "measured"
        or latency["gate_configured"] is not latency_configured
        or latency["required"] is not latency_configured
        or latency["threshold_max_case_latency_ms"] != threshold
        or latency["latency_ms_max"] != observed_latency
        or latency["passed"] is not derived_latency_passed
    ):
        raise ValueError("golden report latency acceptance does not match results")

    cost = _require_exact_fields(
        acceptance["cost"],
        _COST_FIELDS,
        label="golden report cost acceptance",
    )
    if summary_acceptance["cost"] != cost:
        raise ValueError("golden report cost acceptance copies do not match")
    measured_costs = [
        float(item["cost_estimate_usd"])
        for item in results
        if item["cost_estimate_usd"] is not None
    ]
    measured_count = len(measured_costs)
    expected_cost_total = round(sum(measured_costs), 6) if measured_costs else None
    if (
        cost["gate_configured"] is not False
        or cost["required"] is not False
        or cost["passed"] is not None
        or cost["measured_case_count"] != measured_count
        or cost["unmeasured_case_count"] != len(results) - measured_count
        or cost["cost_estimate_usd_total"] != expected_cost_total
        or summary["cost_estimate_usd_total"] != expected_cost_total
    ):
        raise ValueError("golden report cost measurement does not match results")
    expected_cost_status = (
        "unmeasured"
        if measured_count == 0
        else "measured"
        if measured_count == len(results)
        else "partially_measured"
    )
    if cost["measurement_status"] != expected_cost_status:
        raise ValueError("golden report cost measurement status does not match results")
    if not isinstance(cost["residual"], str) or not cost["residual"]:
        raise ValueError("golden report cost residual must be a non-empty string")
    if not isinstance(report.get("passed"), bool):
        raise ValueError("golden report top-level passed value must be boolean")
    derived_passed = derived_functional and (
        derived_latency_passed is True if latency_configured else True
    )
    return derived_passed and report["passed"] is derived_passed
