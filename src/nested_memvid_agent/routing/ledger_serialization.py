from __future__ import annotations

import json
import sqlite3
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

from .ledger_records import (
    ModelTargetEntry,
    ProviderProfileEntry,
    RouteDecisionEntry,
    RouteOutcomeEntry,
    RoutePolicyEntry,
    RoutingRevisionConflict,
)
from .models import ModelTarget, ProviderProfile, RoutePolicy

_SECRET_METADATA_KEYS = {
    "secret",
    "client_secret",
    "password",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "auth_token",
    "bearer_token",
    "id_token",
    "session_token",
}
_SECRET_METADATA_SUFFIXES = (
    "_secret",
    "_password",
    "_api_key",
    "_apikey",
)


def _validate_route_binding(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task_id: str,
    subagent_id: str | None,
) -> None:
    run_row = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run_row is None:
        raise KeyError(f"Unknown run: {run_id}")
    task_row = conn.execute(
        "SELECT run_id FROM task_nodes WHERE task_id = ?", (task_id,)
    ).fetchone()
    if task_row is None:
        raise KeyError(f"Unknown task: {task_id}")
    if str(task_row["run_id"]) != run_id:
        raise ValueError("route task does not belong to run")
    if subagent_id is None:
        return
    subagent_row = conn.execute(
        "SELECT run_id, task_id FROM subagent_runs WHERE subagent_id = ?",
        (subagent_id,),
    ).fetchone()
    if subagent_row is None:
        raise KeyError(f"Unknown subagent run: {subagent_id}")
    if str(subagent_row["run_id"]) != run_id or str(subagent_row["task_id"]) != task_id:
        raise ValueError("route subagent binding does not match run/task")


def _next_revision(
    resource: str,
    resource_id: str,
    row: sqlite3.Row | None,
    *,
    expected_revision: int | None,
    now: str,
) -> tuple[int, str]:
    if row is None:
        if expected_revision not in {None, 0}:
            raise RoutingRevisionConflict(resource, resource_id, 0)
        return 1, now
    current = int(row["revision"])
    if expected_revision is None or expected_revision != current:
        raise RoutingRevisionConflict(resource, resource_id, current)
    return current + 1, str(row["created_at"])


def _validate_secret_ref(value: str | None) -> None:
    if value is None:
        return
    if not value.startswith("secret://") or len(value) <= len("secret://"):
        raise ValueError("provider credentials must use an opaque secret:// broker reference")


def _validate_base_url(value: str | None) -> None:
    if value is None:
        return
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider base_url must not embed credentials")


def _validate_metadata(value: object, *, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if _metadata_key_is_secret_bearing(normalized):
                raise ValueError(f"{path} contains a secret-bearing key: {key}")
            _validate_metadata(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_metadata(item, path=f"{path}[{index}]")


def _metadata_key_is_secret_bearing(normalized: str) -> bool:
    return normalized in _SECRET_METADATA_KEYS or any(
        normalized.endswith(suffix) for suffix in _SECRET_METADATA_SUFFIXES
    )


def _target_values(
    target: ModelTarget,
    *,
    revision: int,
    created_at: str,
    updated_at: str,
) -> tuple[object, ...]:
    return (
        target.target_id,
        target.provider_profile_id,
        target.provider,
        target.model,
        1 if target.enabled else 0,
        target.locality,
        target.trust_class,
        _json(list(target.capability_tags)),
        _json(list(target.role_affinities)),
        _json(list(target.task_family_affinities)),
        target.max_context_tokens,
        1 if target.supports_tools else 0,
        1 if target.supports_json else 0,
        1 if target.supports_vision else 0,
        1 if target.supports_reasoning else 0,
        1 if target.supports_streaming else 0,
        target.quality_tier,
        target.latency_tier,
        target.operator_priority,
        target.estimated_cost_usd,
        target.health,
        target.recent_failure_rate,
        target.predicted_success,
        _json(target.metadata),
        revision,
        created_at,
        updated_at,
    )


def _profile_entry_from_row(row: sqlite3.Row) -> ProviderProfileEntry:
    return ProviderProfileEntry(
        profile=ProviderProfile(
            profile_id=str(row["profile_id"]),
            display_name=str(row["display_name"]),
            adapter=str(row["adapter"]),
            base_url=_optional_str(row["base_url"]),
            secret_ref=_optional_str(row["secret_ref"]),
            enabled=bool(row["enabled"]),
            locality=str(row["locality"]),  # type: ignore[arg-type]
            trust_class=str(row["trust_class"]),
            max_concurrency=int(row["max_concurrency"]),
            metadata=_json_dict(row["metadata_json"]),
        ),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _target_entry_from_row(row: sqlite3.Row) -> ModelTargetEntry:
    return ModelTargetEntry(
        target=ModelTarget(
            target_id=str(row["target_id"]),
            provider_profile_id=str(row["provider_profile_id"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            enabled=bool(row["enabled"]),
            locality=str(row["locality"]),  # type: ignore[arg-type]
            trust_class=str(row["trust_class"]),
            capability_tags=_json_tuple(row["capability_tags_json"]),
            role_affinities=_json_tuple(row["role_affinities_json"]),
            task_family_affinities=_json_tuple(row["task_family_affinities_json"]),
            max_context_tokens=_optional_int(row["max_context_tokens"]),
            supports_tools=bool(row["supports_tools"]),
            supports_json=bool(row["supports_json"]),
            supports_vision=bool(row["supports_vision"]),
            supports_reasoning=bool(row["supports_reasoning"]),
            supports_streaming=bool(row["supports_streaming"]),
            quality_tier=int(row["quality_tier"]),
            latency_tier=int(row["latency_tier"]),
            operator_priority=int(row["operator_priority"]),
            estimated_cost_usd=_optional_float(row["estimated_cost_usd"]),
            health=str(row["health"]),  # type: ignore[arg-type]
            recent_failure_rate=float(row["recent_failure_rate"]),
            predicted_success=_optional_float(row["predicted_success"]),
            metadata=_json_dict(row["metadata_json"]),
        ),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _policy_entry_from_row(row: sqlite3.Row) -> RoutePolicyEntry:
    payload = _json_dict(row["payload_json"])
    return RoutePolicyEntry(
        policy=RoutePolicy(**payload),
        enabled=bool(row["enabled"]),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _decision_entry_from_row(row: sqlite3.Row) -> RouteDecisionEntry:
    candidates = json.loads(str(row["candidate_snapshot_json"]))
    return RouteDecisionEntry(
        decision_id=str(row["decision_id"]),
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]),
        subagent_id=_optional_str(row["subagent_id"]),
        attempt=int(row["attempt"]),
        status=str(row["status"]),
        mode=str(row["mode"]),
        policy_id=str(row["policy_id"]),
        policy_revision=int(row["policy_revision"]),
        contract_digest=str(row["contract_digest"]),
        selected_target_id=str(row["selected_target_id"]),
        selected_target_revision=int(row["selected_target_revision"]),
        selected_profile_id=str(row["selected_profile_id"]),
        selected_profile_revision=int(row["selected_profile_revision"]),
        selected_provider=str(row["selected_provider"]),
        selected_model=str(row["selected_model"]),
        selection_kind=str(row["selection_kind"]),
        score=float(row["score"]),
        predicted_success=_optional_float(row["predicted_success"]),
        estimated_cost_usd=_optional_float(row["estimated_cost_usd"]),
        reason_codes=_json_tuple(row["reason_codes_json"]),
        candidate_snapshot=tuple(dict(item) for item in candidates if isinstance(item, dict)),
        actionable=bool(row["actionable"]),
        router_version=str(row["router_version"]),
        created_at=str(row["created_at"]),
        started_at=_optional_str(row["started_at"]),
        finished_at=_optional_str(row["finished_at"]),
    )


def _outcome_entry_from_row(row: sqlite3.Row) -> RouteOutcomeEntry:
    return RouteOutcomeEntry(
        outcome_id=str(row["outcome_id"]),
        decision_id=str(row["decision_id"]),
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]),
        subagent_id=_optional_str(row["subagent_id"]),
        attempt=int(row["attempt"]),
        execution_status=str(row["execution_status"]),
        validation_passed=bool(row["validation_passed"]),
        validation_codes=_json_tuple(row["validation_codes_json"]),
        failure_category=_optional_str(row["failure_category"]),
        provider_failure_code=_optional_str(row["provider_failure_code"]),
        latency_seconds=_optional_float(row["latency_seconds"]),
        input_tokens=_optional_int(row["input_tokens"]),
        output_tokens=_optional_int(row["output_tokens"]),
        actual_cost_usd=_optional_float(row["actual_cost_usd"]),
        tool_count=int(row["tool_count"]),
        changed_file_count=_optional_int(row["changed_file_count"]),
        retry_count=int(row["retry_count"]),
        escalated=bool(row["escalated"]),
        reward_components={
            str(key): float(value)
            for key, value in _json_dict(row["reward_components_json"]).items()
        },
        outcome_labels=_json_tuple(row["outcome_labels_json"]),
        evidence_refs=_json_tuple(row["evidence_refs_json"]),
        created_at=str(row["created_at"]),
    )


def _bounded_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_id": str(payload.get("target_id", ""))[:256],
        "provider_profile_id": str(payload.get("provider_profile_id", ""))[:256],
        "provider": str(payload.get("provider", ""))[:128],
        "model": str(payload.get("model", ""))[:256],
        "eligible": bool(payload.get("eligible")),
        "score": payload.get("score"),
        "reason_codes": [str(item)[:128] for item in list(payload.get("reason_codes", []))[:32]],
        "components": {
            str(key)[:128]: float(value)
            for key, value in dict(payload.get("components", {})).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    }


def _decision_request_identity(entry: RouteDecisionEntry) -> tuple[object, ...]:
    return (
        entry.decision_id,
        entry.run_id,
        entry.task_id,
        entry.subagent_id,
        entry.attempt,
        entry.mode,
        entry.policy_id,
        entry.policy_revision,
        entry.contract_digest,
        entry.selected_target_id,
        entry.selected_target_revision,
        entry.selected_profile_id,
        entry.selected_profile_revision,
        entry.selected_provider,
        entry.selected_model,
        entry.selection_kind,
        entry.score,
        entry.predicted_success,
        entry.estimated_cost_usd,
        _json(list(entry.reason_codes)),
        _json(list(entry.candidate_snapshot)),
        1 if entry.actionable else 0,
        entry.router_version,
    )


def _decision_request_identity_values(values: tuple[object, ...]) -> tuple[object, ...]:
    return (
        values[0],
        values[1],
        values[2],
        values[3],
        values[4],
        values[6],
        values[7],
        values[8],
        values[9],
        values[10],
        values[11],
        values[12],
        values[13],
        values[14],
        values[15],
        values[16],
        values[17],
        values[18],
        values[19],
        values[20],
        values[21],
        values[22],
        values[23],
    )


def _outcome_request_identity(entry: RouteOutcomeEntry) -> tuple[object, ...]:
    return (
        entry.outcome_id,
        entry.decision_id,
        entry.run_id,
        entry.task_id,
        entry.subagent_id,
        entry.attempt,
        entry.execution_status,
        entry.validation_passed,
        _json(list(entry.validation_codes)),
        entry.failure_category,
        entry.provider_failure_code,
        entry.latency_seconds,
        entry.input_tokens,
        entry.output_tokens,
        entry.actual_cost_usd,
        entry.tool_count,
        entry.changed_file_count,
        entry.retry_count,
        entry.escalated,
        _json(entry.reward_components),
        _json(list(entry.outcome_labels)),
        _json(list(entry.evidence_refs)),
    )


def _outcome_request_identity_values(values: tuple[object, ...]) -> tuple[object, ...]:
    return (
        values[0],
        values[1],
        values[2],
        values[3],
        values[4],
        values[5],
        values[6],
        bool(values[7]),
        values[8],
        values[9],
        values[10],
        values[11],
        values[12],
        values[13],
        values[14],
        values[15],
        values[16],
        values[17],
        bool(values[18]),
        values[19],
        values[20],
        values[21],
    )


def _validate_reward_components(components: dict[str, float]) -> None:
    for key, value in components.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"reward component {key} must be a finite number")


def _validate_outcome_numbers(
    *,
    latency_seconds: float | None,
    input_tokens: int | None,
    output_tokens: int | None,
    actual_cost_usd: float | None,
    tool_count: int,
    changed_file_count: int | None,
    retry_count: int,
) -> None:
    for name, value in (
        ("latency_seconds", latency_seconds),
        ("actual_cost_usd", actual_cost_usd),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be finite and non-negative")
    for name, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("tool_count", tool_count),
        ("changed_file_count", changed_file_count),
        ("retry_count", retry_count),
    ):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{name} must be a non-negative integer")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_dict(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_tuple(value: object) -> tuple[str, ...]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str, bytes, bytearray)):
        raise ValueError("SQLite integer value has an unsupported type")
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes, bytearray)):
        raise ValueError("SQLite floating-point value has an unsupported type")
    return float(value)
