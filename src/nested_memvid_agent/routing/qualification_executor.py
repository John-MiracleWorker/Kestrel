"""Isolated Flock qualification executor (Adaptive Flock plan, Task 8).

``QualificationExecutor.execute(AttemptLease) -> AttemptEvidence`` runs one
leased qualification attempt through the *existing* governed routing path:

1. the attempt lease and its digests are verified;
2. read-only or isolated-worktree project state is staged (fail-closed:
   candidate code never falls back to the host when containment is missing);
3. the matrix target is routed through normal eligibility with
   ``direct_target_id`` — never a parallel approximation — and the route
   decision/lease is persisted *before* provider execution;
4. the provider/tool loop runs under task capability ceilings;
5. trusted validators decide acceptance (the candidate model's self-report
   is never reused as validation);
6. bounded evidence with receipt references is recorded;
7. the attempt workspace is left in place for receipt-bound cleanup/review.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..config import AgentConfig
from .contracts import TaskLike, compile_task_contract
from .coordinator import DurableRoutingAssignment, DurableRoutingCoordinator
from .qualification_models import MoneyMicros
from .qualification_workspace import (
    CONTAINMENT_MODES,
    ContainmentMode,
    QualificationAttemptBlocked,
    QualificationWorkspace,
)
from .router import RoutingUnavailableError
from .service import RoutingAssignment

__all__ = [
    "AttemptEvidence",
    "AttemptLease",
    "AttemptValidator",
    "ExecutorRouteDecision",
    "ProviderAttempt",
    "ProviderRequest",
    "QualificationAttemptBlocked",
    "QualificationExecutor",
    "QualificationProvider",
]

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

DEFAULT_MAX_INPUT_TOKENS = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 16_000


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _require_digest(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")


def _require_tokens(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class AttemptLease:
    """Exact, digest-bound lease for one qualification attempt.

    Binds run/case/target, the exact task contract, project/tree digests,
    target/price/policy/config digests, the budget reservation, the
    containment mode allowed by the corpus item, and the idempotency key.
    """

    lease_id: str
    run_id: str
    case_id: str
    attempt_id: str
    attempt_number: int
    target_id: str
    task: TaskLike
    task_contract_digest: str
    project_digest: str
    tree_digest: str
    target_digest: str
    price_digest: str
    policy_digest: str
    config_digest: str
    reservation: MoneyMicros
    containment: ContainmentMode

    def __post_init__(self) -> None:
        _require_text(self.lease_id, "lease_id")
        _require_text(self.run_id, "run_id")
        _require_text(self.case_id, "case_id")
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.target_id, "target_id")
        if isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise ValueError("attempt_number must be a positive integer")
        if self.task.run_id != self.run_id:
            raise ValueError("run_id must match the leased task run")
        _require_text(self.task.task_id, "task_id")
        for name in (
            "task_contract_digest",
            "project_digest",
            "tree_digest",
            "target_digest",
            "price_digest",
            "policy_digest",
            "config_digest",
        ):
            _require_digest(getattr(self, name), name)
        if not isinstance(self.reservation, MoneyMicros):
            raise ValueError("reservation must be a MoneyMicros value")
        if self.containment not in CONTAINMENT_MODES:
            raise ValueError(f"containment must be one of {', '.join(CONTAINMENT_MODES)}")


@dataclass(frozen=True)
class ProviderRequest:
    """One ceiling-bound provider invocation for a leased attempt."""

    idempotency_key: str
    attempt_id: str
    target_id: str
    provider: str
    model: str
    objective: str
    required_capabilities: tuple[str, ...]
    max_input_tokens: int
    max_output_tokens: int
    containment: ContainmentMode
    workspace_ref: str

    def __post_init__(self) -> None:
        _require_text(self.idempotency_key, "idempotency_key")
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.target_id, "target_id")
        _require_text(self.provider, "provider")
        _require_text(self.model, "model")
        _require_text(self.workspace_ref, "workspace_ref")
        for name in ("max_input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ProviderAttempt:
    """Bounded evidence of one provider attempt."""

    target_id: str
    provider: str
    model: str
    output: str
    input_tokens: int
    output_tokens: int
    latency_seconds: float
    failure_category: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.target_id, "target_id")
        _require_text(self.provider, "provider")
        _require_text(self.model, "model")
        _require_tokens(self.input_tokens, "input_tokens")
        _require_tokens(self.output_tokens, "output_tokens")
        if (
            isinstance(self.latency_seconds, bool)
            or not isinstance(self.latency_seconds, (int, float))
            or self.latency_seconds < 0
        ):
            raise ValueError("latency_seconds must be a non-negative number")


class QualificationProvider(Protocol):
    """Provider/tool loop invoked by the executor under capability ceilings."""

    def execute(self, request: ProviderRequest) -> ProviderAttempt: ...


AttemptValidator = Callable[[AttemptLease, ProviderAttempt], tuple[bool, tuple[str, ...]]]


@dataclass(frozen=True)
class ExecutorRouteDecision:
    """Bounded route-decision evidence for one executed attempt."""

    decision_id: str
    selected_target_id: str
    selection_kind: str
    actionable: bool
    reason_codes: tuple[str, ...]
    hard_filter_reasons: tuple[str, ...]


@dataclass(frozen=True)
class AttemptEvidence:
    """Bounded, receipt-referenced evidence of one executed attempt."""

    lease_id: str
    attempt_id: str
    run_id: str
    case_id: str
    actual_target_id: str
    containment: ContainmentMode
    workspace_ref: str
    route_decision: ExecutorRouteDecision
    provider_attempt: ProviderAttempt
    validation_passed: bool
    validation_codes: tuple[str, ...]
    failure_category: str | None
    evidence_refs: tuple[str, ...]


def _hard_filter_reasons(assignment: RoutingAssignment, selected_target_id: str) -> tuple[str, ...]:
    """Hard-filter reasons reported by normal eligibility for the pinned target.

    An eligible pin yields ``()``; the executor never executes an ineligible
    pin, so any reasons surface only on the blocked path.
    """

    candidate = next(
        (
            item
            for item in assignment.decision.candidates
            if item.target.target_id == selected_target_id
        ),
        None,
    )
    if candidate is None or candidate.eligible:
        return ()
    return tuple(candidate.reason_codes)


def _default_validator(
    lease: AttemptLease, attempt: ProviderAttempt
) -> tuple[bool, tuple[str, ...]]:
    """Trusted default acceptance check; ignores any model self-report."""

    if attempt.failure_category is not None:
        return False, ("provider_failure",)
    if not attempt.output.strip():
        return False, ("empty_output",)
    return True, ("accepted",)


class QualificationExecutor:
    """Execute leased qualification attempts through governed routing.

    Execution is isolated from production routing decisions: every attempt is
    pinned to its matrix target via ``direct_target_id`` and the executor
    verifies the pin survives routing; learned-route activation can never
    substitute a different target for a leased attempt.
    """

    def __init__(
        self,
        coordinator: DurableRoutingCoordinator,
        *,
        base_config: AgentConfig,
        workspace: QualificationWorkspace,
        provider: QualificationProvider,
        validator: AttemptValidator | None = None,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if not isinstance(coordinator, DurableRoutingCoordinator):
            raise ValueError("coordinator must be a DurableRoutingCoordinator")
        if coordinator.mode not in ("constrained", "adaptive"):
            raise ValueError(
                "qualification execution requires an executing routing mode "
                "('constrained' or 'adaptive'); shadow mode never executes"
            )
        if not isinstance(workspace, QualificationWorkspace):
            raise ValueError("workspace must be a QualificationWorkspace")
        if not hasattr(provider, "execute"):
            raise ValueError("provider must implement execute(ProviderRequest)")
        if validator is not None and not callable(validator):
            raise ValueError("validator must be callable")
        for name, value in (
            ("max_input_tokens", max_input_tokens),
            ("max_output_tokens", max_output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._coordinator = coordinator
        self._base_config = base_config
        self._workspace = workspace
        self.provider = provider
        self._validator: AttemptValidator = validator or _default_validator
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens
        self._evidence: dict[str, AttemptEvidence] = {}

    @property
    def workspace(self) -> QualificationWorkspace:
        return self._workspace

    def execute(self, lease: AttemptLease) -> AttemptEvidence:
        """Execute one leased attempt and return its bounded evidence.

        Idempotent by ``lease.lease_id``: a repeated lease returns the
        originally recorded evidence without re-contacting the provider.
        """

        if not isinstance(lease, AttemptLease):
            raise ValueError("lease must be an AttemptLease")
        recorded = self._evidence.get(lease.lease_id)
        if recorded is not None:
            return recorded

        # 1. Verify the attempt lease binds the exact compiled task contract.
        contract = compile_task_contract(lease.task)
        if contract.digest != lease.task_contract_digest:
            raise QualificationAttemptBlocked(
                "lease_contract_mismatch",
                "the leased task contract digest does not match the task",
            )

        # 2. Stage containment before any routing lease or provider contact.
        attempt_workspace = self._workspace.stage(
            lease_id=lease.lease_id,
            containment=lease.containment,
            tree_digest=lease.tree_digest,
        )

        # 3. Route the pinned matrix target through normal eligibility and
        #    persist the route decision/lease before provider execution.
        durable = self._route(lease)
        assignment = durable.assignment
        selected = assignment.decision.selected_target
        if (
            not assignment.executes_selected_target
            or not assignment.decision.actionable
            or selected.target_id != lease.target_id
        ):
            raise QualificationAttemptBlocked(
                "route_target_mismatch",
                f"routing did not pin the leased matrix target {lease.target_id!r}",
            )
        self._coordinator.mark_started(durable)

        # 4. Invoke the provider/tool loop under task capability ceilings.
        request = ProviderRequest(
            idempotency_key=lease.lease_id,
            attempt_id=lease.attempt_id,
            target_id=selected.target_id,
            provider=selected.provider,
            model=selected.model,
            objective=contract.objective,
            required_capabilities=contract.required_capabilities,
            max_input_tokens=self._max_input_tokens,
            max_output_tokens=self._max_output_tokens,
            containment=lease.containment,
            workspace_ref=attempt_workspace.receipt_ref,
        )
        attempt = self.provider.execute(request)
        if not isinstance(attempt, ProviderAttempt):
            raise ValueError("provider must return a ProviderAttempt")

        # 5. Trusted validation; the candidate's self-report is never used.
        validation_passed, validation_codes = self._validator(lease, attempt)
        validation_codes = tuple(sorted(set(validation_codes)))

        # 6. Record bounded evidence in the durable routing ledger.
        outcome = self._coordinator.record_outcome(
            durable,
            execution_status=("completed" if attempt.failure_category is None else "failed"),
            validation_passed=bool(validation_passed),
            validation_codes=validation_codes,
            failure_category=attempt.failure_category,
            latency_seconds=float(attempt.latency_seconds),
            input_tokens=attempt.input_tokens,
            output_tokens=attempt.output_tokens,
            evidence_refs=(attempt_workspace.receipt_ref,),
        )

        # 7. Leave the workspace for receipt-bound cleanup/review.
        self._workspace.finalize(attempt_workspace)

        evidence = AttemptEvidence(
            lease_id=lease.lease_id,
            attempt_id=lease.attempt_id,
            run_id=lease.run_id,
            case_id=lease.case_id,
            actual_target_id=selected.target_id,
            containment=lease.containment,
            workspace_ref=attempt_workspace.receipt_ref,
            route_decision=ExecutorRouteDecision(
                decision_id=durable.record.decision_id,
                selected_target_id=selected.target_id,
                selection_kind=assignment.decision.selection_kind,
                actionable=assignment.decision.actionable,
                reason_codes=tuple(assignment.decision.reason_codes),
                hard_filter_reasons=_hard_filter_reasons(assignment, selected.target_id),
            ),
            provider_attempt=attempt,
            validation_passed=bool(validation_passed),
            validation_codes=validation_codes,
            failure_category=attempt.failure_category,
            evidence_refs=(
                attempt_workspace.receipt_ref,
                f"routing_decision:{durable.record.decision_id}",
                f"routing_outcome:{outcome.outcome_id}",
            ),
        )
        self._evidence[lease.lease_id] = evidence
        return evidence

    def _route(self, lease: AttemptLease) -> DurableRoutingAssignment:
        try:
            return self._coordinator.assign(
                self._base_config,
                lease.task,
                subagent_id=None,
                attempt=lease.attempt_number,
                direct_target_id=lease.target_id,
            )
        except RoutingUnavailableError as exc:
            reason = (
                "route_target_unknown"
                if "direct_target_unknown" in exc.reason_codes
                else "route_target_ineligible"
            )
            raise QualificationAttemptBlocked(
                reason,
                f"{exc} ({', '.join(exc.reason_codes)})",
            ) from exc
