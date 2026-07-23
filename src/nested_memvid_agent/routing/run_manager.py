from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from time import monotonic
from typing import Any

from ..config import AgentConfig
from ..event_bus import RunEventBus
from ..mcp_manager import MCPManager
from ..plugin_manager import PluginManager
from ..run_manager import RunManager, _task_payload
from ..security_boundary import redact_secrets
from ..skill_manager import SkillManager
from ..state_store import AgentStateStore, TaskNodeRecord
from .coordinator import DurableRoutingAssignment, DurableRoutingCoordinator
from .router import RoutingUnavailableError

_TERMINAL_ROUTING_TASK_STATUSES = {"completed", "failed", "cancelled"}


class AdaptiveFlockRunManager(RunManager):
    """RunManager variant that routes each scheduler subagent attempt durably.

    The parent RunManager remains authoritative for claims, worktrees, approvals,
    execution, validation, cancellation, and terminal state. This subclass only
    replaces the per-attempt provider connection before delegating to the parent,
    then records a terminal routing outcome from the parent's durable evidence.
    """

    def __init__(
        self,
        *,
        routing_coordinator: DurableRoutingCoordinator,
        config: AgentConfig,
        state: AgentStateStore,
        events: RunEventBus,
        mcp: MCPManager,
        skills: SkillManager,
        plugins: PluginManager | None = None,
        secret_resolver: Callable[[str | None], str | None] | None = None,
        recover_startup_work: bool = True,
        enforce_single_owner: bool = False,
        read_only_observer: bool = False,
        auto_start: bool = True,
    ) -> None:
        self.routing_coordinator = routing_coordinator
        super().__init__(
            config=config,
            state=state,
            events=events,
            mcp=mcp,
            skills=skills,
            plugins=plugins,
            secret_resolver=secret_resolver,
            recover_startup_work=recover_startup_work,
            enforce_single_owner=enforce_single_owner,
            read_only_observer=read_only_observer,
            auto_start=auto_start,
        )

    def _run_subagent(
        self,
        thread_key: str,
        config: AgentConfig,
        subagent_id: str,
        run_id: str,
        session_id: str,
    ) -> None:
        subagent = self.state.get_subagent_run(subagent_id)
        task_id = subagent.task_id
        if task_id is None:
            super()._run_subagent(thread_key, config, subagent_id, run_id, session_id)
            return
        task = self.state.get_task_node(task_id)
        if self._is_cancelled(run_id) or self.state.get_run(run_id).status in {
            "completed",
            "failed",
            "cancelled",
        }:
            super()._run_subagent(thread_key, config, subagent_id, run_id, session_id)
            return

        attempt = max(1, task.attempt_count + 1)
        try:
            durable = self.routing_coordinator.assign(
                config,
                task,
                subagent_id=subagent_id,
                attempt=attempt,
            )
        except Exception as exc:  # noqa: BLE001 - mode determines fail-open versus fail-closed
            self._handle_pre_execution_routing_failure(
                exc,
                phase="assignment",
                thread_key=thread_key,
                config=config,
                subagent_id=subagent_id,
                run_id=run_id,
                session_id=session_id,
                task=task,
                attempt=attempt,
            )
            return

        self.events.publish(run_id, "routing.selected", _routing_decision_payload(durable))
        try:
            self.routing_coordinator.mark_started(durable)
        except Exception as exc:  # noqa: BLE001 - mode determines fail-open versus fail-closed
            self._handle_pre_execution_routing_failure(
                exc,
                phase="start",
                thread_key=thread_key,
                config=config,
                subagent_id=subagent_id,
                run_id=run_id,
                session_id=session_id,
                task=task,
                attempt=attempt,
                decision_id=durable.record.decision_id,
            )
            return
        self.events.publish(
            run_id,
            "routing.attempt_started",
            {
                "decision_id": durable.record.decision_id,
                "task_id": task_id,
                "subagent_id": subagent_id,
                "attempt": attempt,
                "selected_target_id": durable.record.selected_target_id,
                "selected_provider": durable.record.selected_provider,
                "selected_model": durable.record.selected_model,
                "actionable": durable.record.actionable,
            },
        )
        started_at = monotonic()
        try:
            super()._run_subagent(
                thread_key,
                durable.assignment.config,
                subagent_id,
                run_id,
                session_id,
            )
        finally:
            self._record_terminal_routing_outcome(
                durable,
                started_at=started_at,
                task_id=task_id,
                subagent_id=subagent_id,
                run_id=run_id,
            )

    def _handle_pre_execution_routing_failure(
        self,
        exc: Exception,
        *,
        phase: str,
        thread_key: str,
        config: AgentConfig,
        subagent_id: str,
        run_id: str,
        session_id: str,
        task: TaskNodeRecord,
        attempt: int,
        decision_id: str | None = None,
    ) -> None:
        unavailable = isinstance(exc, RoutingUnavailableError)
        reason_codes = (
            tuple(exc.reason_codes)
            if unavailable
            else (f"routing_{phase}_failed",)
        )
        error = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "subagent_id": subagent_id,
            "attempt": attempt,
            "phase": phase,
            "mode": self.routing_coordinator.mode,
            "reason_codes": list(reason_codes),
            "error": error,
        }
        if decision_id is not None:
            payload["decision_id"] = decision_id
        if self.routing_coordinator.mode == "shadow":
            event_type = (
                "routing.shadow_unavailable"
                if unavailable and phase == "assignment"
                else f"routing.{phase}_failed"
            )
            self.events.publish(run_id, event_type, payload)
            super()._run_subagent(thread_key, config, subagent_id, run_id, session_id)
            return

        category = "routing_unavailable" if unavailable else "routing_persistence_failed"
        event_type = "routing.guardrail_blocked" if unavailable else f"routing.{phase}_failed"
        self.events.publish(run_id, event_type, payload)
        self._fail_routing_assignment(
            run_id=run_id,
            task=task,
            subagent_id=subagent_id,
            error=error,
            reason_codes=reason_codes,
            category=category,
            retry_reason=(
                "No policy-admissible routing target was available."
                if unavailable
                else "The durable routing decision could not be safely persisted or started."
            ),
        )

    def _fail_routing_assignment(
        self,
        *,
        run_id: str,
        task: TaskNodeRecord,
        subagent_id: str,
        error: str,
        reason_codes: tuple[str, ...],
        category: str,
        retry_reason: str,
    ) -> None:
        failed_task, failed_subagent, applied = self.state.transition_scheduler_task_and_subagent(
            task.task_id,
            "failed",
            run_id=run_id,
            subagent_id=subagent_id,
            worker_owner=self._lease_owner,
            worker_claim_id=subagent_id,
            task_fields={
                "failure_reason": error,
                "diagnosis": {
                    "category": category,
                    "reason_codes": list(reason_codes),
                },
                "retry_strategy": {
                    "requires_changed_strategy": True,
                    "retry_allowed": False,
                    "reason": retry_reason,
                },
                "result": {
                    "error": error,
                    "routing_reason_codes": list(reason_codes),
                },
            },
            subagent_error=error,
            increment_attempt=True,
        )
        if not applied:
            return
        self.events.publish(run_id, "task.failed", _task_payload(failed_task))
        self.events.publish(run_id, "subagent.failed", asdict(failed_subagent))
        self._maybe_complete_root_task(run_id)

    def _record_terminal_routing_outcome(
        self,
        durable: DurableRoutingAssignment,
        *,
        started_at: float,
        task_id: str,
        subagent_id: str,
        run_id: str,
    ) -> None:
        task = self.state.get_task_node(task_id)
        subagent = self.state.get_subagent_run(subagent_id)
        if task.status not in _TERMINAL_ROUTING_TASK_STATUSES:
            return
        result = dict(task.result or {})
        validation_raw = result.get("acceptance_validation")
        validation = validation_raw if isinstance(validation_raw, dict) else {}
        validation_passed = task.status == "completed" and bool(validation.get("passed"))
        validation_codes = _validation_codes(validation, passed=validation_passed)
        diagnosis = task.diagnosis if isinstance(task.diagnosis, dict) else {}
        outcome_labels = (
            ("validated_success",)
            if validation_passed
            else ("cancelled",)
            if task.status == "cancelled"
            else ("acceptance_failed",)
        )
        reward = 1.0 if validation_passed else 0.0 if task.status == "cancelled" else -1.0
        try:
            outcome = self.routing_coordinator.record_outcome(
                durable,
                execution_status=subagent.status,
                validation_passed=validation_passed,
                validation_codes=validation_codes,
                failure_category=(
                    str(diagnosis.get("category")) if diagnosis.get("category") else None
                ),
                latency_seconds=max(0.0, monotonic() - started_at),
                tool_count=_non_negative_int(result.get("tool_count")),
                retry_count=max(0, task.attempt_count - 1),
                reward_components={"completion": reward},
                outcome_labels=outcome_labels,
                evidence_refs=_validation_evidence_refs(validation),
            )
        except Exception as exc:  # noqa: BLE001 - routing telemetry must not rewrite task truth
            self.events.publish(
                run_id,
                "routing.outcome_failed",
                {
                    "decision_id": durable.record.decision_id,
                    "task_id": task_id,
                    "subagent_id": subagent_id,
                    "error": str(redact_secrets(f"{type(exc).__name__}: {exc}")),
                },
            )
            return
        self.events.publish(run_id, "routing.outcome_recorded", outcome.to_payload())


def _routing_decision_payload(durable: DurableRoutingAssignment) -> dict[str, Any]:
    record = durable.record
    return {
        "decision_id": record.decision_id,
        "task_id": record.task_id,
        "subagent_id": record.subagent_id,
        "attempt": record.attempt,
        "mode": record.mode,
        "policy_id": record.policy_id,
        "selected_target_id": record.selected_target_id,
        "selected_provider": record.selected_provider,
        "selected_model": record.selected_model,
        "selection_kind": record.selection_kind,
        "score": record.score,
        "reason_codes": list(record.reason_codes),
        "actionable": record.actionable,
        "reused": durable.reused,
    }


def _validation_codes(validation: dict[str, Any], *, passed: bool) -> tuple[str, ...]:
    raw = validation.get("failure_codes")
    if isinstance(raw, list):
        codes = tuple(str(item) for item in raw if str(item))
        if codes:
            return codes
    return ("accepted",) if passed else ("acceptance_not_proven",)


def _validation_evidence_refs(validation: dict[str, Any]) -> tuple[str, ...]:
    refs: set[str] = set()
    raw_criteria = validation.get("criteria")
    if isinstance(raw_criteria, list):
        for criterion in raw_criteria:
            if not isinstance(criterion, dict):
                continue
            raw_refs = criterion.get("evidence_refs")
            if isinstance(raw_refs, list):
                refs.update(str(item) for item in raw_refs if str(item))
    return tuple(sorted(refs))


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)
