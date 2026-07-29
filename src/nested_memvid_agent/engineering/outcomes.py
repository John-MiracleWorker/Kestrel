from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast

from ..security_boundary import redact_secrets
from ..state_store import AgentStateStore, utc_now
from .schema import ensure_engineering_schema

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_RISKS = {"low", "medium", "high", "critical"}
_BASELINES = {"live", "static_policy", "strongest_model_only", "local_only"}


@dataclass(frozen=True)
class BenchmarkCaseRecord:
    case_id: str
    project_id: str
    name: str
    task_family: str
    risk: str
    fixture: dict[str, Any]
    acceptance_criteria: tuple[str, ...]
    case_digest: str
    status: str
    actor: str
    created_at: str
    updated_at: str

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["acceptance_criteria"] = list(self.acceptance_criteria)
        return payload


@dataclass(frozen=True)
class BenchmarkReplayRecord:
    replay_id: str
    case_id: str
    run_id: str
    route_policy_id: str | None
    context_strategy: str
    baseline: str
    actor: str
    created_at: str
    run_status: str
    metrics: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class OutcomeAnalyticsService:
    """Evidence-aware outcome analytics and private benchmark cases."""

    def __init__(self, state: AgentStateStore) -> None:
        self.state = state
        ensure_engineering_schema(state)

    def report(
        self,
        *,
        project_id: str | None = None,
        task_family: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        policy_id: str | None = None,
        since: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        filters = {
            "project_id": _optional_identifier(project_id, "project_id"),
            "task_family": _optional_text(task_family, "task_family", 160),
            "provider": _optional_text(provider, "provider", 256),
            "model": _optional_text(model, "model", 512),
            "policy_id": _optional_identifier(policy_id, "policy_id"),
            "since": _optional_timestamp(since),
            "run_id": _optional_identifier(run_id, "run_id"),
        }
        with self.state._connect() as conn:
            runs = _run_rows(conn, filters)
            run_ids = tuple(str(row["run_id"]) for row in runs)
            routes = _route_rows(conn, filters, run_ids)
            approvals = _approval_rows(conn, run_ids)
            packet_calls = _packet_call_rows(conn, run_ids)
            candidate_selections = _candidate_selection_rows(conn, run_ids)
            browser_validations = _browser_validation_rows(conn, run_ids)
        groups = _route_groups(routes)
        summary = _summary(
            runs=runs,
            routes=routes,
            approvals=approvals,
            packet_calls=packet_calls,
            candidate_selections=candidate_selections,
            browser_validations=browser_validations,
        )
        report = {
            "schema": "kestrel.outcome_analytics.v1",
            "generated_at": utc_now(),
            "filters": filters,
            "summary": summary,
            "groups": groups,
            "baselines": _baselines(routes),
            "evidence_coverage": {
                "routing_outcomes": _coverage(
                    sum(bool(_load_json(row["evidence_refs_json"], [])) for row in routes),
                    len(routes),
                ),
                "provider_usage": _coverage(
                    sum(
                        row["input_tokens"] is not None
                        or row["output_tokens"] is not None
                        for row in routes
                    ),
                    len(routes),
                ),
                "cost_attribution": _coverage(
                    sum(row["actual_cost_usd"] is not None for row in routes),
                    len(routes),
                ),
                "browser_validation": _coverage(
                    sum(
                        bool(_load_json(row["evidence_refs_json"], []))
                        for row in browser_validations
                    ),
                    len(browser_validations),
                ),
            },
        }
        safe = redact_secrets(report)
        if not isinstance(safe, dict):
            raise RuntimeError("outcome analytics redaction failed")
        return safe

    def create_benchmark(
        self,
        *,
        case_id: str,
        project_id: str,
        name: str,
        task_family: str,
        risk: str,
        fixture: dict[str, Any],
        acceptance_criteria: tuple[str, ...],
        actor: str,
    ) -> BenchmarkCaseRecord:
        case_key = _identifier(case_id, "case_id")
        project_key = _identifier(project_id, "project_id")
        self.state.get_project(project_key)
        normalized_fixture = _fixture(fixture)
        normalized_criteria = _criteria(acceptance_criteria)
        risk_key = str(risk).strip().lower()
        if risk_key not in _RISKS:
            raise ValueError("benchmark risk is invalid")
        payload = {
            "schema": "kestrel.private_benchmark.v1",
            "project_id": project_key,
            "name": _text(name, "name", 256),
            "task_family": _text(task_family, "task_family", 160),
            "risk": risk_key,
            "fixture": normalized_fixture,
            "acceptance_criteria": list(normalized_criteria),
        }
        digest = _hash(payload)
        actor_text = _text(actor, "actor", 160)
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM benchmark_cases WHERE case_id = ?",
                (case_key,),
            ).fetchone()
            if existing is not None:
                current = _benchmark_case(existing)
                if current.case_digest != digest:
                    raise ValueError("benchmark_case_identity_conflict")
                return current
            conn.execute(
                """
                INSERT INTO benchmark_cases (
                    case_id, project_id, name, task_family, risk, fixture_json,
                    acceptance_criteria_json, case_digest, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    case_key,
                    project_key,
                    payload["name"],
                    payload["task_family"],
                    risk_key,
                    _json(normalized_fixture),
                    _json(list(normalized_criteria)),
                    digest,
                    actor_text,
                    now,
                    now,
                ),
            )
        return self.get_benchmark(case_key)

    def get_benchmark(self, case_id: str) -> BenchmarkCaseRecord:
        case_key = _identifier(case_id, "case_id")
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM benchmark_cases WHERE case_id = ?",
                (case_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown benchmark case: {case_key}")
        return _benchmark_case(row)

    def list_benchmarks(
        self,
        *,
        project_id: str | None = None,
    ) -> list[BenchmarkCaseRecord]:
        params: list[object] = []
        sql = "SELECT * FROM benchmark_cases"
        if project_id is not None:
            sql += " WHERE project_id = ?"
            params.append(_identifier(project_id, "project_id"))
        sql += " ORDER BY created_at ASC, case_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_benchmark_case(row) for row in rows]

    def link_replay(
        self,
        *,
        replay_id: str,
        case_id: str,
        run_id: str,
        route_policy_id: str | None,
        context_strategy: str,
        baseline: str,
        actor: str,
    ) -> BenchmarkReplayRecord:
        replay_key = _identifier(replay_id, "replay_id")
        case = self.get_benchmark(case_id)
        run_key = _identifier(run_id, "run_id")
        run = self.state.get_run(run_key)
        if run.project_id != case.project_id:
            raise ValueError("benchmark replay run belongs to a different project")
        baseline_key = str(baseline).strip().lower()
        if baseline_key not in _BASELINES:
            raise ValueError("benchmark replay baseline is invalid")
        policy_key = _optional_identifier(route_policy_id, "route_policy_id")
        context_text = _text(context_strategy, "context_strategy", 160)
        actor_text = _text(actor, "actor", 160)
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM benchmark_replays WHERE replay_id = ?",
                (replay_key,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["case_id"]),
                    str(existing["run_id"]),
                    _optional(existing["route_policy_id"]),
                    str(existing["context_strategy"]),
                    str(existing["baseline"]),
                    str(existing["actor"]),
                ) != (
                    case.case_id,
                    run_key,
                    policy_key,
                    context_text,
                    baseline_key,
                    actor_text,
                ):
                    raise ValueError("benchmark_replay_identity_conflict")
                return self.get_replay(replay_key)
            conn.execute(
                """
                INSERT INTO benchmark_replays (
                    replay_id, case_id, run_id, route_policy_id,
                    context_strategy, baseline, actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    replay_key,
                    case.case_id,
                    run_key,
                    policy_key,
                    context_text,
                    baseline_key,
                    actor_text,
                    now,
                ),
            )
        return self.get_replay(replay_key)

    def get_replay(self, replay_id: str) -> BenchmarkReplayRecord:
        replay_key = _identifier(replay_id, "replay_id")
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM benchmark_replays WHERE replay_id = ?",
                (replay_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown benchmark replay: {replay_key}")
        return self._replay(row)

    def list_replays(
        self,
        *,
        case_id: str | None = None,
    ) -> list[BenchmarkReplayRecord]:
        params: list[object] = []
        sql = "SELECT * FROM benchmark_replays"
        if case_id is not None:
            sql += " WHERE case_id = ?"
            params.append(_identifier(case_id, "case_id"))
        sql += " ORDER BY created_at ASC, replay_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._replay(row) for row in rows]

    def _replay(self, row: Any) -> BenchmarkReplayRecord:
        run_id = str(row["run_id"])
        run = self.state.get_run(run_id)
        report = self.report(run_id=run_id)
        return BenchmarkReplayRecord(
            replay_id=str(row["replay_id"]),
            case_id=str(row["case_id"]),
            run_id=run_id,
            route_policy_id=_optional(row["route_policy_id"]),
            context_strategy=str(row["context_strategy"]),
            baseline=str(row["baseline"]),
            actor=str(row["actor"]),
            created_at=str(row["created_at"]),
            run_status=run.status,
            metrics=dict(report["summary"]),
        )


def _run_rows(conn: Any, filters: dict[str, str | None]) -> list[Any]:
    clauses: list[str] = []
    params: list[object] = []
    if filters["project_id"] is not None:
        clauses.append("project_id = ?")
        params.append(filters["project_id"])
    if filters["since"] is not None:
        clauses.append("created_at >= ?")
        params.append(filters["since"])
    if filters["run_id"] is not None:
        clauses.append("run_id = ?")
        params.append(filters["run_id"])
    sql = "SELECT * FROM runs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at ASC, run_id ASC"
    return list(conn.execute(sql, params).fetchall())


def _route_rows(
    conn: Any,
    filters: dict[str, str | None],
    run_ids: tuple[str, ...],
) -> list[Any]:
    if not run_ids or not _table_exists(conn, "routing_outcomes"):
        return []
    clauses = [
        "outcome.run_id IN (" + ",".join("?" for _item in run_ids) + ")"
    ]
    params: list[object] = list(run_ids)
    mapping = {
        "task_family": "decision.task_family",
        "provider": "decision.selected_provider",
        "model": "decision.selected_model",
        "policy_id": "decision.policy_id",
    }
    for key, column in mapping.items():
        if filters[key] is not None:
            clauses.append(f"{column} = ?")
            params.append(filters[key])
    return list(
        conn.execute(
            f"""
            SELECT
                outcome.*,
                decision.project_id,
                decision.task_family,
                decision.risk,
                decision.policy_id,
                decision.mode,
                decision.selection_kind,
                decision.selected_target_id,
                decision.selected_provider,
                decision.selected_model,
                decision.predicted_success,
                target.locality,
                target.quality_tier,
                shadow.static_target_id,
                shadow.learned_target_id,
                shadow.utility_delta,
                shadow.estimated_savings_usd,
                shadow.route_regret_usd,
                shadow.activated AS shadow_activated
            FROM routing_outcomes AS outcome
            JOIN routing_decisions AS decision
              ON decision.decision_id = outcome.decision_id
            LEFT JOIN routing_model_targets AS target
              ON target.target_id = decision.selected_target_id
            LEFT JOIN routing_shadow_evaluations AS shadow
              ON shadow.decision_id = decision.decision_id
            WHERE {" AND ".join(clauses)}
            ORDER BY outcome.created_at ASC, outcome.outcome_id ASC
            """,
            params,
        ).fetchall()
    )


def _approval_rows(conn: Any, run_ids: tuple[str, ...]) -> list[Any]:
    if not run_ids:
        return []
    return list(
        conn.execute(
            """
            SELECT * FROM approval_requests
            WHERE run_id IN ("""
            + ",".join("?" for _item in run_ids)
            + ") ORDER BY created_at ASC, approval_id ASC",
            list(run_ids),
        ).fetchall()
    )


def _packet_call_rows(conn: Any, run_ids: tuple[str, ...]) -> list[Any]:
    if not run_ids or not _table_exists(conn, "approval_packet_calls"):
        return []
    return list(
        conn.execute(
            """
            SELECT * FROM approval_packet_calls
            WHERE run_id IN ("""
            + ",".join("?" for _item in run_ids)
            + ") ORDER BY created_at ASC, packet_call_id ASC",
            list(run_ids),
        ).fetchall()
    )


def _candidate_selection_rows(conn: Any, run_ids: tuple[str, ...]) -> list[Any]:
    if not run_ids or not _table_exists(conn, "candidate_selections"):
        return []
    return list(
        conn.execute(
            """
            SELECT selection.* FROM candidate_selections AS selection
            JOIN candidate_fanouts AS fanout ON fanout.fanout_id = selection.fanout_id
            WHERE fanout.run_id IN ("""
            + ",".join("?" for _item in run_ids)
            + ") ORDER BY selection.created_at ASC, selection.selection_id ASC",
            list(run_ids),
        ).fetchall()
    )


def _browser_validation_rows(conn: Any, run_ids: tuple[str, ...]) -> list[Any]:
    if not run_ids or not _table_exists(conn, "browser_validations"):
        return []
    return list(
        conn.execute(
            """
            SELECT * FROM browser_validations
            WHERE run_id IN ("""
            + ",".join("?" for _item in run_ids)
            + ") ORDER BY created_at ASC, validation_id ASC",
            list(run_ids),
        ).fetchall()
    )


def _summary(
    *,
    runs: list[Any],
    routes: list[Any],
    approvals: list[Any],
    packet_calls: list[Any],
    candidate_selections: list[Any],
    browser_validations: list[Any],
) -> dict[str, Any]:
    completed_runs = sum(str(row["status"]) == "completed" for row in runs)
    terminal_runs = sum(
        str(row["status"]) in {"completed", "failed", "cancelled"} for row in runs
    )
    validation_successes = sum(bool(row["validation_passed"]) for row in routes)
    known_costs = [
        float(row["actual_cost_usd"])
        for row in routes
        if row["actual_cost_usd"] is not None
    ]
    known_latencies = [
        float(row["latency_seconds"])
        for row in routes
        if row["latency_seconds"] is not None
    ]
    durations = [
        duration
        for row in runs
        if (duration := _duration(row["created_at"], row["updated_at"])) is not None
        and str(row["status"]) in {"completed", "failed", "cancelled"}
    ]
    wait_seconds = [
        duration
        for row in approvals
        if str(row["status"]) != "pending"
        and (duration := _duration(row["created_at"], row["updated_at"])) is not None
    ]
    wait_seconds.extend(
        duration
        for row in packet_calls
        if row["decided_at"] is not None
        and (
            duration := _duration(row["created_at"], row["decided_at"])
        )
        is not None
    )
    git_results = [
        _load_json(row["result_json"], {})
        for row in approvals
        if str(row["tool_name"]) == "git.commit" and row["result_json"] is not None
    ]
    rollback_results = [
        _load_json(row["result_json"], {})
        for row in approvals
        if str(row["tool_name"]) == "repair.rollback"
        and row["result_json"] is not None
    ]
    interventions = len(approvals) + sum(
        str(row["status"]) in {"approved", "denied", "consumed", "invalidated"}
        for row in packet_calls
    )
    total_cost = sum(known_costs) if known_costs else None
    return {
        "run_count": _metric(float(len(runs)), len(runs), len(runs)),
        "validated_completion_rate": _metric(
            _ratio(validation_successes, len(routes)),
            len(routes),
            len(routes),
        ),
        "run_completion_rate": _metric(
            _ratio(completed_runs, terminal_runs),
            terminal_runs,
            len(runs),
        ),
        "validated_success_per_dollar": _metric(
            (
                validation_successes / total_cost
                if total_cost is not None and total_cost > 0
                else None
            ),
            len(known_costs),
            len(routes),
        ),
        "validated_success_per_minute": _metric(
            (
                validation_successes / (sum(known_latencies) / 60)
                if known_latencies and sum(known_latencies) > 0
                else None
            ),
            len(known_latencies),
            len(routes),
        ),
        "actual_cost_usd": _metric(total_cost, len(known_costs), len(routes)),
        "median_run_seconds": _metric(
            _median(durations),
            len(durations),
            terminal_runs,
        ),
        "retry_count": _metric(
            float(sum(int(row["retry_count"]) for row in routes)),
            len(routes),
            len(routes),
        ),
        "human_interventions": _metric(
            float(interventions),
            len(approvals) + len(packet_calls),
            len(approvals) + len(packet_calls),
        ),
        "median_approval_wait_seconds": _metric(
            _median(wait_seconds),
            len(wait_seconds),
            len(approvals) + len(packet_calls),
        ),
        "patch_acceptance_count": _metric(
            float(sum(_result_success(item) for item in git_results)),
            len(git_results),
            len(git_results),
        ),
        "rollback_count": _metric(
            float(sum(_result_success(item) for item in rollback_results)),
            len(rollback_results),
            len(rollback_results),
        ),
        "candidate_selection_count": _metric(
            float(len(candidate_selections)),
            len(candidate_selections),
            len(candidate_selections),
        ),
        "browser_validation_pass_rate": _metric(
            _ratio(
                sum(str(row["status"]) == "passed" for row in browser_validations),
                len(browser_validations),
            ),
            len(browser_validations),
            len(browser_validations),
        ),
    }


def _route_groups(rows: list[Any]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, ...], list[Any]] = {}
    for row in rows:
        bucket_key = (
            str(row["project_id"] or ""),
            str(row["task_family"] or ""),
            str(row["risk"] or ""),
            str(row["selected_provider"]),
            str(row["selected_model"]),
            str(row["policy_id"]),
        )
        buckets.setdefault(bucket_key, []).append(row)
    groups: list[dict[str, Any]] = []
    for group_key, items in sorted(buckets.items()):
        successes = sum(bool(item["validation_passed"]) for item in items)
        costs = [
            float(item["actual_cost_usd"])
            for item in items
            if item["actual_cost_usd"] is not None
        ]
        latencies = [
            float(item["latency_seconds"])
            for item in items
            if item["latency_seconds"] is not None
        ]
        groups.append(
            {
                "project_id": group_key[0] or None,
                "task_family": group_key[1] or "unclassified",
                "risk": group_key[2] or "unknown",
                "provider": group_key[3],
                "model": group_key[4],
                "policy_id": group_key[5],
                "attempt_count": len(items),
                "validated_completion_rate": _metric(
                    _ratio(successes, len(items)),
                    len(items),
                    len(items),
                ),
                "actual_cost_usd": _metric(
                    sum(costs) if costs else None,
                    len(costs),
                    len(items),
                ),
                "median_latency_seconds": _metric(
                    _median(latencies),
                    len(latencies),
                    len(items),
                ),
                "retry_count": sum(int(item["retry_count"]) for item in items),
                "route_regret_usd": _metric(
                    _sum_optional(items, "route_regret_usd"),
                    sum(item["route_regret_usd"] is not None for item in items),
                    len(items),
                ),
                "estimated_savings_usd": _metric(
                    _sum_optional(items, "estimated_savings_usd"),
                    sum(item["estimated_savings_usd"] is not None for item in items),
                    len(items),
                ),
            }
        )
    return groups


def _baselines(rows: list[Any]) -> list[dict[str, Any]]:
    definitions = {
        "static_policy": [
            row
            for row in rows
            if str(row["selection_kind"]).startswith("static")
            or row["static_target_id"] == row["selected_target_id"]
        ],
        "strongest_model_only": _strongest_target_rows(rows),
        "local_only": [row for row in rows if str(row["locality"]) == "local"],
    }
    result: list[dict[str, Any]] = []
    for name, items in definitions.items():
        successes = sum(bool(item["validation_passed"]) for item in items)
        costs = [
            float(item["actual_cost_usd"])
            for item in items
            if item["actual_cost_usd"] is not None
        ]
        result.append(
            {
                "baseline": name,
                "available": len(items) >= 3,
                "sample_count": len(items),
                "validated_completion_rate": _metric(
                    _ratio(successes, len(items)),
                    len(items),
                    len(items),
                ),
                "validated_success_per_dollar": _metric(
                    (
                        successes / sum(costs)
                        if costs and sum(costs) > 0
                        else None
                    ),
                    len(costs),
                    len(items),
                ),
                "inference": (
                    "Historical observed outcomes; no live behavior was changed."
                ),
            }
        )
    return result


def _strongest_target_rows(rows: list[Any]) -> list[Any]:
    targets: dict[str, list[Any]] = {}
    for row in rows:
        targets.setdefault(str(row["selected_target_id"]), []).append(row)
    if not targets:
        return []
    strongest = max(
        targets,
        key=lambda target: (
            max(int(item["quality_tier"] or 0) for item in targets[target]),
            max(float(item["predicted_success"] or 0.0) for item in targets[target]),
            len(targets[target]),
            target,
        ),
    )
    return targets[strongest]


def _benchmark_case(row: Any) -> BenchmarkCaseRecord:
    fixture = _load_json(row["fixture_json"], {})
    criteria = _load_json(row["acceptance_criteria_json"], [])
    return BenchmarkCaseRecord(
        case_id=str(row["case_id"]),
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        task_family=str(row["task_family"]),
        risk=str(row["risk"]),
        fixture=dict(fixture) if isinstance(fixture, dict) else {},
        acceptance_criteria=tuple(str(item) for item in criteria),
        case_digest=str(row["case_digest"]),
        status=str(row["status"]),
        actor=str(row["actor"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _fixture(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("benchmark fixture must be an object")
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > 256_000:
        raise ValueError("benchmark fixture exceeds the 256 KiB bound")
    loaded = json.loads(encoded)
    if redact_secrets(loaded) != loaded:
        raise ValueError("benchmark fixture contains sensitive material")
    objective = loaded.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("benchmark fixture requires a non-empty objective")
    return cast(dict[str, Any], loaded)


def _criteria(values: tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(values) <= 64:
        raise ValueError("benchmark requires between one and 64 acceptance criteria")
    normalized = tuple(_text(item, "acceptance criterion", 2000) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError("benchmark acceptance criteria must be unique")
    return normalized


def _metric(value: float | None, sample_count: int, population: int) -> dict[str, Any]:
    normalized = None if value is None else round(float(value), 8)
    return {
        "value": normalized,
        "sample_count": sample_count,
        "population": population,
        "coverage": round(sample_count / population, 6) if population else 0.0,
        "missing": normalized is None,
    }


def _coverage(covered: int, total: int) -> dict[str, Any]:
    return {
        "covered": covered,
        "total": total,
        "rate": round(covered / total, 6) if total else None,
        "missing": total == 0,
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _sum_optional(rows: list[Any], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row[field] is not None]
    return sum(values) if values else None


def _duration(start: Any, end: Any) -> float | None:
    try:
        left = datetime.fromisoformat(str(start))
        right = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    if left.tzinfo is None:
        left = left.replace(tzinfo=UTC)
    if right.tzinfo is None:
        right = right.replace(tzinfo=UTC)
    value = (right - left).total_seconds()
    return max(value, 0.0) if math.isfinite(value) else None


def _result_success(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("success") is True


def _table_exists(conn: Any, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _load_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("value must be finite JSON") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"{field} has an invalid identifier")
    return text


def _optional_identifier(value: Any, field: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _identifier(value, field)


def _text(value: Any, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{field} must contain between 1 and {maximum} characters")
    if redact_secrets(text) != text:
        raise ValueError(f"{field} contains sensitive material")
    return text


def _optional_text(value: Any, field: str, maximum: int) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _text(value, field, maximum)


def _optional_timestamp(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError("since must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    return parsed.astimezone(UTC).isoformat()


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)
