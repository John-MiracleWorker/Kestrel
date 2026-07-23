from __future__ import annotations

import hashlib
import json

from ..state_store import utc_now
from .ledger_records import RouteDecisionEntry, RouteOutcomeEntry, RoutingRevisionConflict
from .ledger_registry import RoutingRegistry
from .ledger_serialization import (
    _bounded_candidate,
    _decision_entry_from_row,
    _decision_request_identity,
    _decision_request_identity_values,
    _json,
    _outcome_entry_from_row,
    _outcome_request_identity,
    _outcome_request_identity_values,
    _validate_outcome_numbers,
    _validate_reward_components,
    _validate_route_binding,
)
from .models import RouteDecision


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
        status: str = "selected",
        router_version: str = "adaptive-flock.v1",
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
                return current
            conn.execute(
                """
                INSERT INTO routing_decisions (
                    decision_id, run_id, task_id, subagent_id, attempt, status, mode,
                    policy_id, policy_revision, contract_digest, selected_target_id,
                    selected_target_revision, selected_profile_id, selected_profile_revision,
                    selected_provider, selected_model, selection_kind, score,
                    predicted_success, estimated_cost_usd, reason_codes_json,
                    candidate_snapshot_json, actionable, router_version, created_at,
                    started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
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
