from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from threading import Lock
from time import monotonic
from typing import Any, cast

from ..agent import NestedMV2Agent
from ..config import AgentConfig
from ..event_bus import RunEventBus
from ..graph_runtime import GraphRunState
from ..mcp_manager import MCPManager
from ..plugin_manager import PluginManager
from ..projects import project_routing_constraints
from ..run_manager import RunManager, _task_payload
from ..security_boundary import redact_secrets
from ..skill_manager import SkillManager
from ..state_store import (
    AgentStateStore,
    RunRecord,
    SubagentRunRecord,
    TaskNodeRecord,
)
from .coordinator import DurableRoutingAssignment, DurableRoutingCoordinator
from .models import AgentTaskContract, ModelTarget, ProviderProfile, RoutingMode
from .qualification_evidence import classify_failure_code, normalize_provider_attempt
from .role_resolver import GraphRoleAssignment, RoleAssignmentResolver
from .router import ReviewDiversityContext, RoutingUnavailableError
from .service import AdaptiveFlockRoutingService

_TERMINAL_ROUTING_TASK_STATUSES = {"completed", "failed", "cancelled"}


def _graph_role_contracts(
    ctx: GraphRunState,
    *,
    state: AgentStateStore,
) -> tuple[AgentTaskContract, AgentTaskContract, AgentTaskContract]:
    """Build executor/planner/reviewer contracts for the primary graph turn.

    The graph runtime models planner/executor/reviewer as graph *nodes* over a
    single root planner task, so there are no per-role task records to compile
    from.  We synthesise three role contracts from the root task's durable
    metadata (objective, risk, required tools) so the role resolver can route
    each role independently.
    """

    tasks = state.list_task_nodes(ctx.run_id)
    root = next(
        (task for task in tasks if task.parent_id is None and task.profile == "planner"),
        None,
    )
    objective = ctx.message.strip() or (root.goal if root is not None else "Run objective")
    risk = root.risk if root is not None else "low"
    required_tools = tuple(root.required_tools) if root is not None else ()
    task_id = root.task_id if root is not None else ctx.run_id

    constraints = _graph_role_project_constraints(ctx, state=state)

    def make(role: str, *, task_family: str, structured_output: bool) -> AgentTaskContract:
        return AgentTaskContract(
            task_id=task_id,
            run_id=ctx.run_id,
            role=role,
            task_family=task_family,
            objective=objective,
            complexity=0.5,
            ambiguity=0.4,
            risk=risk,
            required_tools=required_tools,
            required_capabilities=("reasoning",) if role in {"planner", "reviewer"} else (),
            structured_output_required=structured_output,
            privacy_class=constraints["privacy_class"],
            local_required=constraints["local_required"],
            maximum_cost_usd=constraints["maximum_cost_usd"],
            allowed_target_ids=constraints["allowed_target_ids"],
            forbidden_target_ids=constraints["forbidden_target_ids"],
            allowed_provider_profiles=constraints["allowed_provider_profiles"],
            forbidden_provider_profiles=constraints["forbidden_provider_profiles"],
        )

    return (
        make("executor", task_family="general", structured_output=False),
        make("planner", task_family="planning", structured_output=True),
        make("reviewer", task_family="review", structured_output=True),
    )


def _graph_role_project_constraints(
    ctx: GraphRunState,
    *,
    state: AgentStateStore,
) -> dict[str, Any]:
    """Compile the run's project routing policy into graph role constraints.

    The graph roles are synthesized from the root task, but they must still
    honour the run's project routing policy — otherwise a local-required or
    provider-restricted project's separate reviewer could be built for a
    cloud/prohibited target and receive the objective/response.  When the run
    is not project-bound, the contracts keep their default (unrestricted)
    constraint fields.
    """

    run = state.get_run(ctx.run_id)
    if run.project_id is None:
        return {
            "privacy_class": "approved_cloud",
            "local_required": False,
            "maximum_cost_usd": None,
            "allowed_target_ids": (),
            "forbidden_target_ids": (),
            "allowed_provider_profiles": (),
            "forbidden_provider_profiles": (),
        }
    project = state.get_project(run.project_id)
    compiled = project_routing_constraints(project)
    return {
        "privacy_class": compiled["default_privacy_class"],
        "local_required": bool(compiled["local_required"]),
        "maximum_cost_usd": project.cost_budget,
        "allowed_target_ids": compiled["allowed_target_ids"],
        "forbidden_target_ids": compiled["forbidden_target_ids"],
        "allowed_provider_profiles": compiled["allowed_provider_profiles"],
        "forbidden_provider_profiles": compiled["forbidden_provider_profiles"],
    }


def _executor_diversity_anchor(
    config: AgentConfig,
    *,
    targets: tuple[ModelTarget, ...],
    profiles: tuple[ProviderProfile, ...],
) -> ReviewDiversityContext | None:
    """Derive the reviewer diversity anchor from the *executed* config.

    The graph executor runs ``ctx.config`` directly (it is not routed through
    the ledger), so the ledger's synthetic executor decision is not the config
    that actually executed.  Anchor reviewer diversity to the ledger target
    matching ``ctx.config``'s provider+model so a reviewer routed to the same
    provider/model as the real executor is never mis-labeled ``independent``.

    Returns ``None`` when the executed config matches no ledger target (a
    direct provider outside the ledger), in which case the resolver falls back
    to the synthetic executor decision.
    """

    profile_by_id = {profile.profile_id: profile for profile in profiles}
    for target in targets:
        profile = profile_by_id.get(target.provider_profile_id)
        if profile is None:
            continue
        if profile.adapter == config.provider and target.model == config.model:
            family = str(target.metadata.get("model_family", "")).strip()
            return ReviewDiversityContext(
                target_id=target.target_id,
                provider_profile_id=target.provider_profile_id,
                model_family=family or None,
            )
    return None


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
        self._routing_assignment_lock = Lock()
        self._scheduler_routing_lock = Lock()
        self._scheduler_routing_attempts: dict[
            tuple[str, str], tuple[DurableRoutingAssignment, float]
        ] = {}
        super().__init__(
            config=config,
            state=state,
            events=events,
            mcp=mcp,
            skills=skills,
            plugins=plugins,
            secret_resolver=secret_resolver,
            lan_runtime_authority_resolver=(
                routing_coordinator.lan_runtime_authority_resolver
            ),
            lan_runtime_utc_clock=routing_coordinator.clock,
            recover_startup_work=recover_startup_work,
            enforce_single_owner=enforce_single_owner,
            read_only_observer=read_only_observer,
            auto_start=auto_start,
        )

    def _review_authority_resolver(self) -> Callable[[GraphRunState], GraphRoleAssignment | None]:
        coordinator = self.routing_coordinator

        def resolve(ctx: GraphRunState) -> GraphRoleAssignment | None:
            policy_entry = coordinator.ledger.get_policy(coordinator.policy_id)
            if policy_entry is None or not policy_entry.enabled:
                return None
            profiles = tuple(
                entry.profile for entry in coordinator.ledger.list_provider_profiles()
            )
            targets = tuple(entry.target for entry in coordinator.ledger.list_model_targets())
            resolver = RoleAssignmentResolver(
                profiles=profiles,
                targets=targets,
                policy=policy_entry.policy,
                mode=cast(RoutingMode, coordinator.mode),
            )
            executor_contract, planner_contract, reviewer_contract = _graph_role_contracts(
                ctx,
                state=self.state,
            )
            executor_diversity_context = _executor_diversity_anchor(
                ctx.config,
                targets=targets,
                profiles=profiles,
            )
            return resolver.resolve(
                executor_contract,
                planner_contract,
                reviewer_contract,
                executor_diversity_context=executor_diversity_context,
            )

        return resolve

    def _build_reviewer_agent(
        self,
    ) -> Callable[[GraphRunState, Any], NestedMV2Agent | None] | None:
        coordinator = self.routing_coordinator

        def build(ctx: GraphRunState, assignment: Any) -> NestedMV2Agent | None:
            reviewer_decision = getattr(assignment, "reviewer_decision", None)
            if reviewer_decision is None:
                return None
            # Shadow mode observes the independent reviewer target but must NOT
            # execute the alternate provider call (SHADOW-001: no alternate
            # provider call). Only constrained/adaptive decisions are actionable.
            if not getattr(reviewer_decision, "actionable", False):
                return None
            policy_entry = coordinator.ledger.get_policy(coordinator.policy_id)
            if policy_entry is None or not policy_entry.enabled:
                return None
            service = AdaptiveFlockRoutingService(
                profiles=[
                    entry.profile for entry in coordinator.ledger.list_provider_profiles()
                ],
                targets=[entry.target for entry in coordinator.ledger.list_model_targets()],
                policy=policy_entry.policy,
                mode=cast(RoutingMode, coordinator.mode),
                lan_runtime_authority_resolver=coordinator.lan_runtime_authority_resolver,
                clock=coordinator.clock,
            )
            reviewer_config = service.apply_decision(ctx.config, reviewer_decision)
            return self._build_agent(reviewer_config)

        return build

    def _prepare_scheduler_task_config(
        self,
        config: AgentConfig,
        *,
        run: RunRecord,
        task: TaskNodeRecord,
        subagent: SubagentRunRecord,
    ) -> AgentConfig:
        attempt = max(1, task.attempt_count + 1)
        durable: DurableRoutingAssignment | None = None
        try:
            durable = self._assign_with_project_policy(
                config,
                task,
                run=run,
                subagent_id=subagent.subagent_id,
                attempt=attempt,
            )
            self.events.publish(
                run.run_id,
                "routing.selected",
                _routing_decision_payload(durable),
            )
            self.routing_coordinator.mark_started(durable)
        except Exception as exc:  # noqa: BLE001 - mode selects fail-open or fail-closed
            return self._handle_scheduler_routing_failure(
                exc,
                config=config,
                run=run,
                task=task,
                subagent=subagent,
                attempt=attempt,
                durable=durable,
            )
        self.events.publish(
            run.run_id,
            "routing.attempt_started",
            {
                "decision_id": durable.record.decision_id,
                "task_id": task.task_id,
                "subagent_id": subagent.subagent_id,
                "attempt": attempt,
                "selected_target_id": durable.record.selected_target_id,
                "selected_provider": durable.record.selected_provider,
                "selected_model": durable.record.selected_model,
                "actionable": durable.record.actionable,
                "scheduler": True,
            },
        )
        with self._scheduler_routing_lock:
            self._scheduler_routing_attempts[(run.run_id, task.task_id)] = (
                durable,
                monotonic(),
            )
        return durable.assignment.config

    def _execute_ready_task(self, run: RunRecord, task: TaskNodeRecord) -> dict[str, Any]:
        try:
            return super()._execute_ready_task(run, task)
        finally:
            with self._scheduler_routing_lock:
                context = self._scheduler_routing_attempts.pop(
                    (run.run_id, task.task_id),
                    None,
                )
            if context is not None:
                durable, started_at = context
                subagent_id = durable.record.subagent_id
                if subagent_id is not None:
                    self._record_terminal_routing_outcome(
                        durable,
                        started_at=started_at,
                        task_id=task.task_id,
                        subagent_id=subagent_id,
                        run_id=run.run_id,
                    )

    def _handle_scheduler_routing_failure(
        self,
        exc: Exception,
        *,
        config: AgentConfig,
        run: RunRecord,
        task: TaskNodeRecord,
        subagent: SubagentRunRecord,
        attempt: int,
        durable: DurableRoutingAssignment | None,
    ) -> AgentConfig:
        phase = "assignment" if durable is None else "start"
        if isinstance(exc, RoutingUnavailableError):
            unavailable = True
            reason_codes = tuple(exc.reason_codes)
        else:
            unavailable = False
            reason_codes = (f"routing_{phase}_failed",)
        category = "routing_unavailable" if unavailable else "routing_persistence_failed"
        error = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "subagent_id": subagent.subagent_id,
            "attempt": attempt,
            "phase": phase,
            "mode": self.routing_coordinator.mode,
            "reason_codes": list(reason_codes),
            "error": error,
            "scheduler": True,
        }
        if durable is not None:
            payload["decision_id"] = durable.record.decision_id
            try:
                outcome = self.routing_coordinator.record_outcome(
                    durable,
                    execution_status=f"routing_{phase}_failed",
                    validation_passed=False,
                    validation_codes=reason_codes,
                    failure_category=category,
                    reward_components={"completion": -1.0},
                    outcome_labels=(f"routing_{phase}_failed",),
                )
                self.events.publish(
                    run.run_id,
                    "routing.outcome_recorded",
                    outcome.to_payload(),
                )
            except Exception as outcome_exc:  # noqa: BLE001 - retain root failure
                self.events.publish(
                    run.run_id,
                    "routing.outcome_failed",
                    {
                        "decision_id": durable.record.decision_id,
                        "task_id": task.task_id,
                        "subagent_id": subagent.subagent_id,
                        "error": str(
                            redact_secrets(
                                f"{type(outcome_exc).__name__}: {outcome_exc}"
                            )
                        ),
                    },
                )
        if self.routing_coordinator.mode == "shadow":
            event_type = (
                "routing.shadow_unavailable"
                if unavailable and phase == "assignment"
                else f"routing.{phase}_failed"
            )
            self.events.publish(run.run_id, event_type, payload)
            return replace(config, lan_runtime_authority=None)
        event_type = (
            "routing.guardrail_blocked"
            if unavailable
            else f"routing.{phase}_failed"
        )
        self.events.publish(run.run_id, event_type, payload)
        raise RuntimeError(
            f"{category}:{','.join(reason_codes)}:{error}"
        ) from exc

    def _uses_actionable_project_routing(self) -> bool:
        return self.routing_coordinator.mode in {"constrained", "adaptive"}

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
            super()._run_subagent(
                thread_key,
                replace(config, lan_runtime_authority=None),
                subagent_id,
                run_id,
                session_id,
            )
            return
        task = self.state.get_task_node(task_id)
        run = self.state.get_run(run_id)
        if self._is_cancelled(run_id) or run.status in {
            "completed",
            "failed",
            "cancelled",
        }:
            super()._run_subagent(
                thread_key,
                replace(config, lan_runtime_authority=None),
                subagent_id,
                run_id,
                session_id,
            )
            return

        attempt = max(1, task.attempt_count + 1)
        try:
            durable = self._assign_with_project_policy(
                config,
                task,
                run=run,
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

    def _assign_with_project_policy(
        self,
        config: AgentConfig,
        task: TaskNodeRecord,
        *,
        run: RunRecord,
        subagent_id: str | None,
        attempt: int,
    ) -> DurableRoutingAssignment:
        def assign() -> DurableRoutingAssignment:
            if run.project_id is None:
                return self.routing_coordinator.assign(
                    config,
                    task,
                    subagent_id=subagent_id,
                    attempt=attempt,
                )
            project = self.state.get_project(run.project_id)
            if project.archived_at is not None:
                raise RoutingUnavailableError(
                    "project-bound route references an archived project",
                    reason_codes=("project_archived",),
                )
            configured_policy_id = str(
                project.provider_policy.get("policy_id", "")
            ).strip()
            if (
                configured_policy_id
                and configured_policy_id != self.routing_coordinator.policy_id
            ):
                raise RoutingUnavailableError(
                    "project routing policy does not match the active durable coordinator",
                    reason_codes=("project_route_policy_mismatch",),
                )
            constraints = project_routing_constraints(project)
            maximum_cost_usd = project.cost_budget
            if maximum_cost_usd is not None:
                existing = self.routing_coordinator.ledger.get_attempt_decision(
                    run_id=run.run_id,
                    task_id=task.task_id,
                    subagent_id=subagent_id,
                    attempt=attempt,
                )
                spent = 0.0
                for decision in self.routing_coordinator.ledger.list_decisions(
                    run_id=run.run_id
                ):
                    if existing is not None and decision.decision_id == existing.decision_id:
                        continue
                    if not decision.actionable:
                        continue
                    if decision.estimated_cost_usd is None:
                        raise RoutingUnavailableError(
                            "project route budget has unattributed prior estimated cost",
                            reason_codes=("project_route_cost_unknown",),
                        )
                    spent += decision.estimated_cost_usd
                maximum_cost_usd = max(0.0, maximum_cost_usd - spent)
            return self.routing_coordinator.assign(
                config,
                task,
                subagent_id=subagent_id,
                attempt=attempt,
                default_privacy_class=constraints["default_privacy_class"],
                local_required=bool(constraints["local_required"]),
                maximum_cost_usd=maximum_cost_usd,
                allowed_target_ids=constraints["allowed_target_ids"],
                forbidden_target_ids=constraints["forbidden_target_ids"],
                allowed_provider_profiles=constraints["allowed_provider_profiles"],
                forbidden_provider_profiles=constraints[
                    "forbidden_provider_profiles"
                ],
            )

        lock = getattr(self, "_routing_assignment_lock", None)
        if lock is None:
            return assign()
        with lock:
            return assign()

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
        if isinstance(exc, RoutingUnavailableError):
            unavailable = True
            reason_codes = tuple(exc.reason_codes)
        else:
            unavailable = False
            reason_codes = (f"routing_{phase}_failed",)
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
            super()._run_subagent(
                thread_key,
                replace(config, lan_runtime_authority=None),
                subagent_id,
                run_id,
                session_id,
            )
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
        provider_usage_raw = result.get("provider_usage")
        provider_usage = (
            provider_usage_raw if isinstance(provider_usage_raw, dict) else {}
        )
        input_tokens = _optional_non_negative_int(provider_usage.get("input_tokens"))
        output_tokens = _optional_non_negative_int(provider_usage.get("output_tokens"))
        failure_codes_raw = provider_usage.get("provider_failure_codes")
        failure_codes = (
            sorted(str(item) for item in failure_codes_raw if str(item))
            if isinstance(failure_codes_raw, list)
            else []
        )
        provider_failure_code = failure_codes[0] if failure_codes else None
        fallback_count = _non_negative_int(provider_usage.get("fallback_count"))
        provider_error_count = _non_negative_int(
            provider_usage.get("provider_error_count")
        )
        diagnosis_category = (
            str(diagnosis.get("category")) if diagnosis.get("category") else None
        )
        failure_category = _provider_failure_category(
            provider_failure_code,
            default=diagnosis_category,
        )
        evidence = normalize_provider_attempt(
            {
                "subject_id": durable.record.decision_id,
                "run_id": run_id,
                "task_id": task_id,
                "attempt": max(1, task.attempt_count),
                "target_id": str(durable.record.selected_target_id),
                "provider": str(durable.record.selected_provider),
                "profile_id": str(
                    getattr(durable.record, "selected_profile_id", "") or ""
                ),
                "model": str(durable.record.selected_model),
                "request_id": provider_usage.get("provider_request_id")
                or provider_usage.get("request_id"),
                "status": task.status,
                "execution_status": subagent.status,
                "error_code": provider_failure_code,
                "validation_passed": validation_passed,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": _optional_non_negative_int(
                    provider_usage.get("cached_tokens")
                ),
                "reasoning_tokens": _optional_non_negative_int(
                    provider_usage.get("reasoning_tokens")
                ),
                "latency_seconds": max(0.0, monotonic() - started_at),
                "input_cost_per_million_usd": getattr(
                    durable.record, "input_cost_per_million_usd", None
                ),
                "output_cost_per_million_usd": getattr(
                    durable.record, "output_cost_per_million_usd", None
                ),
            }
        )
        evidence_refs = _validation_evidence_refs(validation)
        if evidence.request_id_digest is not None:
            evidence_refs = (
                *evidence_refs,
                f"provider_request:{evidence.request_id_digest}",
            )
        outcome_labels: tuple[str, ...] = (
            ("validated_success",)
            if validation_passed
            else ("cancelled",)
            if task.status == "cancelled"
            else ("acceptance_failed",)
        )
        if provider_usage:
            if provider_usage.get("complete") is True:
                outcome_labels = (*outcome_labels, "usage_complete")
            elif _non_negative_int(provider_usage.get("reported_call_count")):
                outcome_labels = (*outcome_labels, "usage_partial")
        reward = 1.0 if validation_passed else 0.0 if task.status == "cancelled" else -1.0
        try:
            outcome = self.routing_coordinator.record_outcome(
                durable,
                execution_status=subagent.status,
                validation_passed=validation_passed,
                validation_codes=validation_codes,
                failure_category=failure_category,
                provider_failure_code=provider_failure_code,
                latency_seconds=max(0.0, monotonic() - started_at),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_count=_non_negative_int(result.get("tool_count")),
                changed_file_count=_changed_file_count(result),
                retry_count=max(
                    0,
                    task.attempt_count - 1,
                    fallback_count + provider_error_count,
                ),
                escalated=fallback_count > 0,
                reward_components={"completion": reward},
                outcome_labels=outcome_labels,
                evidence_refs=evidence_refs,
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
        "activation_grant_id": getattr(record, "activation_grant_id", None),
        "activation_receipt_id": getattr(record, "activation_receipt_id", None),
        "activation_effective": bool(getattr(record, "activation_effective", False)),
        "activation_reason": getattr(record, "activation_reason", None),
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


def _optional_non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _provider_failure_category(
    provider_failure_code: str | None,
    *,
    default: str | None,
) -> str | None:
    """Classify a typed provider failure code via the shared taxonomy.

    Unrecognized codes fall back to the task diagnosis category (legacy
    precedence) and then to ``"unknown"`` — never to task-quality blame.
    """
    if provider_failure_code is None:
        return default
    category = classify_failure_code(provider_failure_code)
    if category is None or category == "unknown":
        return default or category
    return category


def _changed_file_count(result: dict[str, Any]) -> int | None:
    candidates: list[object] = [
        result.get("changed_files"),
        result.get("files_changed"),
    ]
    for key in ("repair_artifact", "review", "patch_review"):
        nested = result.get(key)
        if isinstance(nested, dict):
            candidates.extend(
                (
                    nested.get("changed_files"),
                    nested.get("files_changed"),
                )
            )
    for candidate in candidates:
        if isinstance(candidate, list):
            return len({str(item) for item in candidate if str(item)})
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    return None
