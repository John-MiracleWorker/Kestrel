from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from math import exp, log

from ..state_store import utc_now
from .ledger_records import (
    RouteDecisionEntry,
    RouteOutcomeEntry,
    RoutingRevisionConflict,
    RoutingShadowDraft,
    RoutingShadowEntry,
    TargetCalibrationEntry,
)
from .ledger_registry import RoutingRegistry
from .ledger_serialization import (
    _bounded_candidate,
    _calibration_entry_from_row,
    _decision_entry_from_row,
    _decision_request_identity,
    _decision_request_identity_values,
    _json,
    _outcome_entry_from_row,
    _outcome_request_identity,
    _outcome_request_identity_values,
    _shadow_entry_from_row,
    _validate_outcome_numbers,
    _validate_reward_components,
    _validate_route_binding,
)
from .models import AgentTaskContract, RouteDecision
from .qualification_evidence import PROVIDER_SIDE_FAILURE_CATEGORIES


class RoutingLedger(RoutingRegistry):
    """Durable Adaptive Flock decisions and outcomes over the routing registry."""

    def record_decision(
        self,
        *,
        decision_id: str,
        run_id: str,
        task_id: str,
        subagent_id: str | None,
        attempt: int,
        decision: RouteDecision,
        policy_revision: int,
        contract: AgentTaskContract | None = None,
        shadow: RoutingShadowDraft | None = None,
        status: str = "selected",
        router_version: str = "adaptive-flock.v2",
    ) -> RouteDecisionEntry:
        if isinstance(attempt, bool) or attempt < 1:
            raise ValueError("route attempt must be a positive integer")
        if status not in {"selected", "running"}:
            raise ValueError("route decision status must be selected or running")
        target_entry = self.get_model_target(decision.selected_target.target_id)
        if target_entry is None:
            raise ValueError(f"selected target is not registered: {decision.selected_target.target_id}")
        profile_entry = self.get_provider_profile(target_entry.target.provider_profile_id)
        if profile_entry is None:
            raise ValueError(
                f"selected provider profile is not registered: {target_entry.target.provider_profile_id}"
            )
        policy_entry = self.get_policy(decision.policy_id)
        if policy_entry is None:
            raise ValueError(f"route policy is not registered: {decision.policy_id}")
        if policy_entry.revision != policy_revision:
            raise RoutingRevisionConflict("route_policy", decision.policy_id, policy_entry.revision)
        candidate_snapshot = tuple(
            _bounded_candidate(item.to_payload()) for item in decision.candidates[:64]
        )
        predicted_success = target_entry.target.predicted_success
        estimated_cost = target_entry.target.estimated_cost_usd
        run = self.state.get_run(run_id)
        project_id = run.project_id
        if contract is not None:
            if contract.run_id != run_id or contract.task_id != task_id:
                raise ValueError("route contract does not match run/task")
            if contract.digest != decision.contract_digest:
                raise ValueError("route contract digest does not match decision")
            task_family = contract.task_family
            risk = contract.risk
            required_capabilities = tuple(sorted(set(contract.required_capabilities)))
        else:
            task_family = ""
            risk = ""
            required_capabilities = ()
        capability_key = capability_scope_key(required_capabilities)
        if shadow is not None:
            _validate_shadow_scope(
                shadow,
                project_id=project_id,
                task_family=task_family,
                risk=risk,
                capability_key=capability_key,
            )
        now = utc_now()
        values = (
            decision_id,
            run_id,
            task_id,
            subagent_id,
            attempt,
            status,
            decision.mode,
            decision.policy_id,
            policy_revision,
            decision.contract_digest,
            target_entry.target.target_id,
            target_entry.revision,
            profile_entry.profile.profile_id,
            profile_entry.revision,
            target_entry.target.provider,
            target_entry.target.model,
            decision.selection_kind,
            decision.score,
            predicted_success,
            estimated_cost,
            target_entry.target.input_cost_per_million_usd,
            target_entry.target.output_cost_per_million_usd,
            project_id,
            task_family,
            risk,
            _json(list(required_capabilities)),
            capability_key,
            _json(list(decision.reason_codes)),
            _json(list(candidate_snapshot)),
            1 if decision.actionable else 0,
            router_version,
            now,
            None,
            None,
        )
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _validate_route_binding(conn, run_id=run_id, task_id=task_id, subagent_id=subagent_id)
            current_target = conn.execute(
                "SELECT revision FROM routing_model_targets WHERE target_id = ?",
                (target_entry.target.target_id,),
            ).fetchone()
            current_profile = conn.execute(
                "SELECT revision FROM routing_provider_profiles WHERE profile_id = ?",
                (profile_entry.profile.profile_id,),
            ).fetchone()
            current_policy = conn.execute(
                "SELECT revision FROM routing_policies WHERE policy_id = ?",
                (decision.policy_id,),
            ).fetchone()
            if current_target is None or int(current_target["revision"]) != target_entry.revision:
                raise RoutingRevisionConflict(
                    "model_target",
                    target_entry.target.target_id,
                    0 if current_target is None else int(current_target["revision"]),
                )
            if current_profile is None or int(current_profile["revision"]) != profile_entry.revision:
                raise RoutingRevisionConflict(
                    "provider_profile",
                    profile_entry.profile.profile_id,
                    0 if current_profile is None else int(current_profile["revision"]),
                )
            if current_policy is None or int(current_policy["revision"]) != policy_revision:
                raise RoutingRevisionConflict(
                    "route_policy",
                    decision.policy_id,
                    0 if current_policy is None else int(current_policy["revision"]),
                )
            existing = conn.execute(
                "SELECT * FROM routing_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if existing is not None:
                current = _decision_entry_from_row(existing)
                if _decision_request_identity(current) != _decision_request_identity_values(values):
                    raise ValueError("route_decision_identity_conflict")
                if shadow is not None:
                    persisted_shadow = conn.execute(
                        "SELECT shadow_id FROM routing_shadow_evaluations WHERE decision_id = ?",
                        (decision_id,),
                    ).fetchone()
                    if persisted_shadow is None:
                        raise ValueError("route_shadow_missing_for_existing_decision")
                return current
            conn.execute(
                """
                INSERT INTO routing_decisions (
                    decision_id, run_id, task_id, subagent_id, attempt, status, mode,
                    policy_id, policy_revision, contract_digest, selected_target_id,
                    selected_target_revision, selected_profile_id, selected_profile_revision,
                    selected_provider, selected_model, selection_kind, score,
                    predicted_success, estimated_cost_usd, input_cost_per_million_usd,
                    output_cost_per_million_usd, project_id, task_family, risk,
                    required_capabilities_json, capability_key, reason_codes_json,
                    candidate_snapshot_json, actionable, router_version, created_at,
                    started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            if shadow is not None:
                _insert_shadow(
                    conn,
                    decision_id=decision_id,
                    shadow=shadow,
                    created_at=now,
                )
            row = conn.execute(
                "SELECT * FROM routing_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("route_decision_write_lost")
        return _decision_entry_from_row(row)

    def mark_decision_started(self, decision_id: str) -> RouteDecisionEntry:
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM routing_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown route decision: {decision_id}")
            current = _decision_entry_from_row(row)
            if current.status == "selected":
                conn.execute(
                    """
                    UPDATE routing_decisions
                    SET status = 'running', started_at = ?
                    WHERE decision_id = ? AND status = 'selected'
                    """,
                    (now, decision_id),
                )
            updated = conn.execute(
                "SELECT * FROM routing_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("route_decision_start_lost")
        return _decision_entry_from_row(updated)

    def get_decision(self, decision_id: str) -> RouteDecisionEntry | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return None if row is None else _decision_entry_from_row(row)

    def get_attempt_decision(
        self,
        *,
        run_id: str,
        task_id: str,
        subagent_id: str | None,
        attempt: int,
    ) -> RouteDecisionEntry | None:
        with self.state._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM routing_decisions
                WHERE run_id = ? AND task_id = ? AND subagent_id IS ? AND attempt = ?
                ORDER BY created_at ASC, decision_id ASC
                LIMIT 1
                """,
                (run_id, task_id, subagent_id, attempt),
            ).fetchone()
        return None if row is None else _decision_entry_from_row(row)

    def list_decisions(
        self,
        *,
        run_id: str,
        task_id: str | None = None,
    ) -> list[RouteDecisionEntry]:
        params: list[object] = [run_id]
        sql = "SELECT * FROM routing_decisions WHERE run_id = ?"
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at ASC, decision_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_decision_entry_from_row(row) for row in rows]

    def count_running_decisions(
        self,
        *,
        limit: int = 1_000,
    ) -> int:
        """Count bounded in-flight attempts without loading route snapshots."""

        bounded_limit = max(1, min(int(limit), 1_000))
        with self.state._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS item_count
                FROM (
                    SELECT 1
                    FROM routing_decisions
                    WHERE status = 'running'
                    LIMIT ?
                )
                """,
                (bounded_limit,),
            ).fetchone()
        return 0 if row is None else int(row["item_count"])

    def record_outcome(
        self,
        *,
        outcome_id: str,
        decision_id: str,
        execution_status: str,
        validation_passed: bool,
        validation_codes: tuple[str, ...] = (),
        failure_category: str | None = None,
        provider_failure_code: str | None = None,
        latency_seconds: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        actual_cost_usd: float | None = None,
        tool_count: int = 0,
        changed_file_count: int | None = None,
        retry_count: int = 0,
        escalated: bool = False,
        reward_components: dict[str, float] | None = None,
        outcome_labels: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
    ) -> RouteOutcomeEntry:
        _validate_reward_components(reward_components or {})
        _validate_outcome_numbers(
            latency_seconds=latency_seconds,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            actual_cost_usd=actual_cost_usd,
            tool_count=tool_count,
            changed_file_count=changed_file_count,
            retry_count=retry_count,
        )
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            decision_row = conn.execute(
                "SELECT * FROM routing_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if decision_row is None:
                raise KeyError(f"Unknown route decision: {decision_id}")
            decision = _decision_entry_from_row(decision_row)
            existing = conn.execute(
                "SELECT * FROM routing_outcomes WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            values = (
                outcome_id,
                decision_id,
                decision.run_id,
                decision.task_id,
                decision.subagent_id,
                decision.attempt,
                execution_status,
                1 if validation_passed else 0,
                _json(list(validation_codes)),
                failure_category,
                provider_failure_code,
                latency_seconds,
                input_tokens,
                output_tokens,
                actual_cost_usd,
                tool_count,
                changed_file_count,
                retry_count,
                1 if escalated else 0,
                _json(reward_components or {}),
                _json(list(outcome_labels)),
                _json(list(evidence_refs)),
                now,
            )
            if existing is not None:
                current = _outcome_entry_from_row(existing)
                if _outcome_request_identity(current) != _outcome_request_identity_values(values):
                    raise ValueError("route_outcome_identity_conflict")
                return current
            conn.execute(
                """
                INSERT INTO routing_outcomes (
                    outcome_id, decision_id, run_id, task_id, subagent_id, attempt,
                    execution_status, validation_passed, validation_codes_json,
                    failure_category, provider_failure_code, latency_seconds,
                    input_tokens, output_tokens, actual_cost_usd, tool_count,
                    changed_file_count, retry_count, escalated, reward_components_json,
                    outcome_labels_json, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            terminal_status = (
                "completed"
                if validation_passed
                else "cancelled"
                if execution_status == "cancelled"
                else "failed"
            )
            conn.execute(
                """
                UPDATE routing_decisions
                SET status = ?, finished_at = ?
                WHERE decision_id = ? AND status IN ('selected', 'running')
                """,
                (terminal_status, now, decision_id),
            )
            _resolve_shadow(
                conn,
                decision_id=decision_id,
                validation_passed=validation_passed,
                actual_cost_usd=actual_cost_usd,
                resolved_at=now,
            )
            _refresh_target_calibration(conn, decision=decision, updated_at=now)
            row = conn.execute(
                "SELECT * FROM routing_outcomes WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("route_outcome_write_lost")
        return _outcome_entry_from_row(row)

    def get_outcome(self, decision_id: str) -> RouteOutcomeEntry | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_outcomes WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return None if row is None else _outcome_entry_from_row(row)

    def list_outcomes(
        self,
        *,
        run_id: str,
        task_id: str | None = None,
    ) -> list[RouteOutcomeEntry]:
        params: list[object] = [run_id]
        sql = "SELECT * FROM routing_outcomes WHERE run_id = ?"
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at ASC, outcome_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_outcome_entry_from_row(row) for row in rows]

    def get_shadow(self, decision_id: str) -> RoutingShadowEntry | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routing_shadow_evaluations WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return None if row is None else _shadow_entry_from_row(row)

    def list_shadows(
        self,
        *,
        run_id: str,
        task_id: str | None = None,
    ) -> list[RoutingShadowEntry]:
        params: list[object] = [run_id]
        sql = """
            SELECT shadow.*
            FROM routing_shadow_evaluations AS shadow
            JOIN routing_decisions AS decision
              ON decision.decision_id = shadow.decision_id
            WHERE decision.run_id = ?
        """
        if task_id is not None:
            sql += " AND decision.task_id = ?"
            params.append(task_id)
        sql += " ORDER BY shadow.created_at ASC, shadow.shadow_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_shadow_entry_from_row(row) for row in rows]

    def list_calibrations(
        self,
        *,
        project_id: str | None = None,
        target_id: str | None = None,
    ) -> list[TargetCalibrationEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        sql = "SELECT * FROM routing_target_calibrations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY project_id, task_family, risk, capability_key, target_id"
        with self.state._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_calibration_entry_from_row(row) for row in rows]

    def list_learning_outcomes(
        self,
        *,
        project_id: str | None,
        task_family: str,
        risk: str,
        capability_key: str,
        eligible_target_ids: tuple[str, ...] = (),
        limit: int = 500,
    ) -> list[dict[str, object]]:
        if isinstance(limit, bool) or limit < 1 or limit > 5_000:
            raise ValueError("learning outcome limit must be between 1 and 5000")
        params: list[object] = [project_id, task_family, risk, capability_key]
        target_clause = ""
        if eligible_target_ids:
            normalized = tuple(sorted(set(eligible_target_ids)))
            target_clause = (
                " AND decision.selected_target_id IN ("
                + ",".join("?" for _item in normalized)
                + ")"
            )
            params.extend(normalized)
        params.append(limit)
        with self.state._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    decision.decision_id,
                    decision.selected_target_id,
                    decision.contract_digest,
                    decision.project_id,
                    decision.task_family,
                    decision.risk,
                    decision.capability_key,
                    outcome.validation_passed,
                    outcome.execution_status,
                    outcome.failure_category,
                    outcome.provider_failure_code,
                    outcome.actual_cost_usd,
                    outcome.latency_seconds,
                    outcome.outcome_labels_json,
                    outcome.created_at
                FROM routing_decisions AS decision
                JOIN routing_outcomes AS outcome
                  ON outcome.decision_id = decision.decision_id
                WHERE decision.project_id IS ?
                  AND decision.task_family = ?
                  AND decision.risk = ?
                  AND decision.capability_key = ?
                  AND decision.actionable = 1
                  {target_clause}
                ORDER BY outcome.created_at DESC, outcome.outcome_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        examples: list[dict[str, object]] = []
        for row in reversed(rows):
            labels = tuple(json.loads(str(row["outcome_labels_json"])))
            if not {"validated_success", "acceptance_failed"} & set(labels):
                continue
            examples.append(
                {
                    "decision_id": str(row["decision_id"]),
                    "target_id": str(row["selected_target_id"]),
                    "validation_passed": bool(row["validation_passed"]),
                    "execution_status": str(row["execution_status"]),
                    "failure_category": (
                        None
                        if row["failure_category"] is None
                        else str(row["failure_category"])
                    ),
                    "provider_failure_code": (
                        None
                        if row["provider_failure_code"] is None
                        else str(row["provider_failure_code"])
                    ),
                    "actual_cost_usd": (
                        None
                        if row["actual_cost_usd"] is None
                        else float(row["actual_cost_usd"])
                    ),
                    "latency_seconds": (
                        None
                        if row["latency_seconds"] is None
                        else float(row["latency_seconds"])
                    ),
                    "task_family": str(row["task_family"]),
                    "risk": str(row["risk"]),
                    "contract_digest": str(row["contract_digest"]),
                    "project_id": (
                        None if row["project_id"] is None else str(row["project_id"])
                    ),
                    "capability_key": str(row["capability_key"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return examples


def stable_decision_id(
    *,
    run_id: str,
    task_id: str,
    subagent_id: str | None,
    attempt: int,
    contract_digest: str,
    policy_id: str,
) -> str:
    payload = json.dumps(
        [run_id, task_id, subagent_id, attempt, contract_digest, policy_id],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "route_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def stable_outcome_id(decision_id: str) -> str:
    return "route_outcome_" + hashlib.sha256(decision_id.encode("utf-8")).hexdigest()[:40]


def capability_scope_key(required_capabilities: tuple[str, ...]) -> str:
    normalized = tuple(sorted(set(str(item) for item in required_capabilities if str(item))))
    if not normalized:
        return "none"
    encoded = json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)
    return "cap_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _validate_shadow_scope(
    shadow: RoutingShadowDraft,
    *,
    project_id: str | None,
    task_family: str,
    risk: str,
    capability_key: str,
) -> None:
    if (
        shadow.project_id != project_id
        or shadow.task_family != task_family
        or shadow.risk != risk
        or shadow.capability_key != capability_key
    ):
        raise ValueError("route shadow scope does not match decision scope")
    for name, value in (
        ("evidence_count", shadow.evidence_count),
        ("target_example_count", shadow.target_example_count),
    ):
        if isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    for name, coverage_value in (
        ("cost_coverage", shadow.cost_coverage),
        ("confidence", shadow.confidence),
    ):
        if not 0.0 <= coverage_value <= 1.0:
            raise ValueError(f"{name} must be between zero and one")
    if not shadow.static_target_id:
        raise ValueError("route shadow static_target_id is required")
    if not shadow.actual_provider or not shadow.actual_model:
        raise ValueError("route shadow actual provider and model are required")


def _insert_shadow(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    shadow: RoutingShadowDraft,
    created_at: str,
) -> None:
    shadow_id = "route_shadow_" + hashlib.sha256(
        decision_id.encode("utf-8")
    ).hexdigest()[:40]
    conn.execute(
        """
        INSERT INTO routing_shadow_evaluations (
            shadow_id, decision_id, project_id, task_family, risk, capability_key,
            static_target_id, learned_target_id, actual_target_id, actual_provider,
            actual_model, evidence_count, target_example_count, cost_coverage,
            confidence, static_utility, learned_utility, utility_delta,
            estimated_savings_usd, route_regret_usd, activated,
            abstention_reason, config_digest, created_at, resolved_at,
            actual_validation_passed, actual_cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            shadow_id,
            decision_id,
            shadow.project_id,
            shadow.task_family,
            shadow.risk,
            shadow.capability_key,
            shadow.static_target_id,
            shadow.learned_target_id,
            shadow.actual_target_id,
            shadow.actual_provider,
            shadow.actual_model,
            shadow.evidence_count,
            shadow.target_example_count,
            shadow.cost_coverage,
            shadow.confidence,
            shadow.static_utility,
            shadow.learned_utility,
            shadow.utility_delta,
            shadow.estimated_savings_usd,
            None,
            1 if shadow.activated else 0,
            shadow.abstention_reason,
            shadow.config_digest,
            created_at,
            None,
            None,
            None,
        ),
    )


def _resolve_shadow(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    validation_passed: bool,
    actual_cost_usd: float | None,
    resolved_at: str,
) -> None:
    row = conn.execute(
        "SELECT * FROM routing_shadow_evaluations WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if row is None:
        return
    learned_target_id = row["learned_target_id"]
    actual_target_id = row["actual_target_id"]
    estimated_savings = row["estimated_savings_usd"]
    if learned_target_id is not None and actual_target_id == learned_target_id:
        regret: float | None = 0.0
    elif estimated_savings is not None:
        regret = max(0.0, float(estimated_savings))
    else:
        regret = None
    conn.execute(
        """
        UPDATE routing_shadow_evaluations
        SET resolved_at = ?,
            actual_validation_passed = ?,
            actual_cost_usd = ?,
            route_regret_usd = ?
        WHERE decision_id = ?
        """,
        (
            resolved_at,
            1 if validation_passed else 0,
            actual_cost_usd,
            regret,
            decision_id,
        ),
    )


def _refresh_target_calibration(
    conn: sqlite3.Connection,
    *,
    decision: RouteDecisionEntry,
    updated_at: str,
) -> None:
    if not decision.actionable or not decision.task_family:
        return
    rows = conn.execute(
        """
        SELECT outcome.*
        FROM routing_outcomes AS outcome
        JOIN routing_decisions AS routed
          ON routed.decision_id = outcome.decision_id
        WHERE routed.project_id IS ?
          AND routed.task_family = ?
          AND routed.risk = ?
          AND routed.capability_key = ?
          AND routed.selected_target_id = ?
          AND routed.actionable = 1
        ORDER BY outcome.created_at ASC, outcome.outcome_id ASC
        """,
        (
            decision.project_id,
            decision.task_family,
            decision.risk,
            decision.capability_key,
            decision.selected_target_id,
        ),
    ).fetchall()
    weighted_quality: list[tuple[float, bool]] = []
    weighted_outages: list[tuple[float, bool]] = []
    weighted_costs: list[tuple[float, float]] = []
    weighted_latencies: list[tuple[float, float]] = []
    total_weight = 0.0
    cost_weight = 0.0
    example_count = 0
    now = _parse_timestamp(updated_at)
    for row in rows:
        labels = set(json.loads(str(row["outcome_labels_json"])))
        if not {"validated_success", "acceptance_failed"} & labels:
            continue
        weight = _decay_weight(str(row["created_at"]), now=now)
        is_outage = (
            str(row["failure_category"] or "") in PROVIDER_SIDE_FAILURE_CATEGORIES
        )
        weighted_outages.append((weight, is_outage))
        total_weight += weight
        example_count += 1
        if not is_outage:
            weighted_quality.append((weight, bool(row["validation_passed"])))
            if row["actual_cost_usd"] is not None:
                weighted_costs.append((weight, float(row["actual_cost_usd"])))
                cost_weight += weight
            if row["latency_seconds"] is not None:
                weighted_latencies.append((weight, float(row["latency_seconds"])))
    if not example_count:
        return
    quality_weight = sum(weight for weight, _value in weighted_quality)
    validation_rate = (
        sum(weight for weight, passed in weighted_quality if passed) / quality_weight
        if quality_weight
        else 0.0
    )
    outage_rate = (
        sum(weight for weight, outage in weighted_outages if outage) / total_weight
        if total_weight
        else 0.0
    )
    average_cost = _weighted_average(weighted_costs)
    average_latency = _weighted_average(weighted_latencies)
    cost_coverage = cost_weight / quality_weight if quality_weight else 0.0
    key_payload = json.dumps(
        [
            decision.project_id,
            decision.selected_target_id,
            decision.task_family,
            decision.risk,
            decision.capability_key,
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    calibration_key = "route_cal_" + hashlib.sha256(
        key_payload.encode("utf-8")
    ).hexdigest()[:40]
    conn.execute(
        """
        INSERT INTO routing_target_calibrations (
            calibration_key, project_id, target_id, task_family, risk,
            capability_key, validation_rate, recent_failure_rate,
            provider_outage_rate, average_cost_usd, average_latency_seconds,
            cost_coverage, example_count, effective_sample_size, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(calibration_key) DO UPDATE SET
            validation_rate = excluded.validation_rate,
            recent_failure_rate = excluded.recent_failure_rate,
            provider_outage_rate = excluded.provider_outage_rate,
            average_cost_usd = excluded.average_cost_usd,
            average_latency_seconds = excluded.average_latency_seconds,
            cost_coverage = excluded.cost_coverage,
            example_count = excluded.example_count,
            effective_sample_size = excluded.effective_sample_size,
            updated_at = excluded.updated_at
        """,
        (
            calibration_key,
            decision.project_id,
            decision.selected_target_id,
            decision.task_family,
            decision.risk,
            decision.capability_key,
            validation_rate,
            1.0 - validation_rate,
            outage_rate,
            average_cost,
            average_latency,
            min(1.0, cost_coverage),
            example_count,
            total_weight,
            updated_at,
        ),
    )


def _weighted_average(items: list[tuple[float, float]]) -> float | None:
    total_weight = sum(weight for weight, _value in items)
    if total_weight <= 0:
        return None
    return sum(weight * value for weight, value in items) / total_weight


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _decay_weight(created_at: str, *, now: datetime) -> float:
    observed = _parse_timestamp(created_at)
    age_days = max(0.0, (now - observed).total_seconds() / 86_400.0)
    return exp(-log(2.0) * age_days / 30.0)
