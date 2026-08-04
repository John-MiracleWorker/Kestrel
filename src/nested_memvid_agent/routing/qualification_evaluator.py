"""Per-scope qualification metrics and exact evaluation (Adaptive Flock plan, Task 11).

Consumes the exact ordered terminal attempt evidence for one qualification
scope plus the snapshotted thresholds/config, and produces a transparent
``ScopeQualificationResult``: qualified / abstained / deterministic-only
state, selected learned target, static target, support, confidence, utility
components and delta, cost coverage, savings/regret, guardrail counts, and
explicit machine-readable reasons.

Invariants:

- Target utility math, ranking, and decay semantics are never re-implemented
  here: evidence is projected into ``RouteExample`` records and evaluated
  through ``LearnedRouterState.from_examples`` / ``evaluate_shadow`` with the
  snapshotted ``QualificationThresholds`` mapped exactly onto
  ``LearnedRouterConfig``.
- Gates are never compressed into a single score; every failed gate appends
  its own explicit reason.
- Evidence must bind exactly to the scope (scope digest, project, task
  family, risk, snapshotted eligible targets); foreign evidence is rejected.
- Evaluation is deterministic and independent of input ordering: attempts
  are canonicalized once by ``(case_id, target_id, attempt_ordinal,
  attempt_id)`` before any metric is computed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Literal

from .learned_router import (
    LearnedRouterConfig,
    LearnedRouterState,
    RouteExample,
    evaluate_shadow,
)
from .qualification_digest import canonical_digest
from .qualification_evidence import FAILURE_CATEGORIES
from .qualification_models import (
    EvidenceKind,
    QualificationScope,
    QualificationThresholds,
)

__all__ = [
    "SCOPE_QUALIFICATION_STATES",
    "ScopeAttemptEvidence",
    "ScopeEvaluationInput",
    "ScopeQualificationResult",
    "evaluate_scope",
]

ScopeQualificationState = Literal["qualified", "abstained", "deterministic_only"]

SCOPE_QUALIFICATION_STATES: tuple[str, ...] = (
    "qualified",
    "abstained",
    "deterministic_only",
)

GUARDRAIL_STATES: tuple[str, ...] = ("clear", "violated")

_DETERMINISTIC_ONLY_RISKS: tuple[str, ...] = ("high", "critical")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _require_digest(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")


def _require_optional_non_negative(value: float | None, name: str) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative number or None")


@dataclass(frozen=True)
class ScopeAttemptEvidence:
    """One terminal attempt evidence entry bound exactly to a scope."""

    attempt_id: str
    case_id: str
    scope_digest: str
    target_id: str
    attempt_ordinal: int
    validation_passed: bool
    execution_status: str
    failure_category: str | None
    actual_cost_usd: float | None
    latency_seconds: float | None
    guardrail_state: str
    evidence_kind: EvidenceKind
    trusted_acceptance: bool
    task_family: str
    risk: str
    contract_digest: str
    project_id: str | None = None
    capability_key: str = "none"
    created_at: str = ""

    def __post_init__(self) -> None:
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.case_id, "case_id")
        _require_digest(self.scope_digest, "scope_digest")
        _require_text(self.target_id, "target_id")
        if (
            isinstance(self.attempt_ordinal, bool)
            or not isinstance(self.attempt_ordinal, int)
            or self.attempt_ordinal < 1
        ):
            raise ValueError("attempt_ordinal must be a positive integer")
        if not isinstance(self.validation_passed, bool):
            raise ValueError("validation_passed must be a boolean")
        if self.failure_category is not None and self.failure_category not in FAILURE_CATEGORIES:
            raise ValueError(
                "failure_category must be one of " + ", ".join(sorted(FAILURE_CATEGORIES))
            )
        _require_optional_non_negative(self.actual_cost_usd, "actual_cost_usd")
        _require_optional_non_negative(self.latency_seconds, "latency_seconds")
        if self.guardrail_state not in GUARDRAIL_STATES:
            raise ValueError("guardrail_state must be 'clear' or 'violated'")
        if self.evidence_kind not in ("synthetic", "real_project"):
            raise ValueError("evidence_kind must be 'synthetic' or 'real_project'")
        if not isinstance(self.trusted_acceptance, bool):
            raise ValueError("trusted_acceptance must be a boolean")
        _require_text(self.task_family, "task_family")
        _require_text(self.risk, "risk")
        _require_digest(self.contract_digest, "contract_digest")

    def to_route_example(self) -> RouteExample:
        """Project this attempt into the learned router's example shape."""
        return RouteExample(
            decision_id=self.attempt_id,
            target_id=self.target_id,
            validation_passed=self.validation_passed,
            execution_status=self.execution_status,
            failure_category=self.failure_category,
            actual_cost_usd=self.actual_cost_usd,
            latency_seconds=self.latency_seconds,
            task_family=self.task_family,
            risk=self.risk,
            contract_digest=self.contract_digest,
            project_id=self.project_id,
            capability_key=self.capability_key,
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class ScopeEvaluationInput:
    """Exact ordered terminal evidence for one scope plus snapshotted config.

    Attempts are canonicalized once by ``(case_id, target_id,
    attempt_ordinal, attempt_id)`` so evaluation never depends on database
    query order. Every attempt must bind exactly to the scope.
    """

    scope: QualificationScope
    static_target_id: str
    thresholds: QualificationThresholds
    attempts: tuple[ScopeAttemptEvidence, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, QualificationScope):
            raise ValueError("scope must be a QualificationScope")
        if not isinstance(self.thresholds, QualificationThresholds):
            raise ValueError("thresholds must be QualificationThresholds")
        if self.static_target_id not in self.scope.target_ids:
            raise ValueError("static target must be a snapshotted eligible target")
        for attempt in self.attempts:
            if not isinstance(attempt, ScopeAttemptEvidence):
                raise ValueError("attempts must be ScopeAttemptEvidence entries")
            if (
                attempt.scope_digest != self.scope.digest
                or attempt.project_id != self.scope.project_id
                or attempt.task_family != self.scope.task_family
                or attempt.risk != self.scope.risk
            ):
                raise ValueError("attempt evidence does not bind to the qualification scope")
            if attempt.target_id not in self.scope.target_ids:
                raise ValueError("attempt target is not a snapshotted eligible target")
        object.__setattr__(
            self,
            "attempts",
            tuple(
                sorted(
                    self.attempts,
                    key=lambda attempt: (
                        attempt.case_id,
                        attempt.target_id,
                        attempt.attempt_ordinal,
                        attempt.attempt_id,
                    ),
                )
            ),
        )


@dataclass(frozen=True)
class ScopeQualificationResult:
    """Transparent per-scope qualification outcome with explicit reasons."""

    scope_digest: str
    state: ScopeQualificationState
    static_target_id: str
    selected_target_id: str | None
    total_support: int
    selected_target_support: int
    confidence: float
    static_utility: float | None
    learned_utility: float | None
    utility_delta: float
    cost_coverage: float
    estimated_savings_usd: float | None
    estimated_regret_usd: float | None
    guardrail_violations: int
    evaluated_target_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    router_state: LearnedRouterState
    thresholds_digest: str

    def __post_init__(self) -> None:
        _require_digest(self.scope_digest, "scope_digest")
        if self.state not in SCOPE_QUALIFICATION_STATES:
            raise ValueError(
                "state must be one of " + ", ".join(SCOPE_QUALIFICATION_STATES)
            )
        if self.state != "qualified" and self.selected_target_id is not None:
            raise ValueError("only a qualified scope may select a learned target")
        _require_digest(self.thresholds_digest, "thresholds_digest")

    @property
    def qualified(self) -> bool:
        return self.state == "qualified"

    def to_payload(self) -> dict[str, Any]:
        return {
            "scope_digest": self.scope_digest,
            "state": self.state,
            "qualified": self.qualified,
            "static_target_id": self.static_target_id,
            "selected_target_id": self.selected_target_id,
            "total_support": self.total_support,
            "selected_target_support": self.selected_target_support,
            "confidence": round(self.confidence, 8),
            "static_utility": self.static_utility,
            "learned_utility": self.learned_utility,
            "utility_delta": round(self.utility_delta, 8),
            "cost_coverage": round(self.cost_coverage, 8),
            "estimated_savings_usd": self.estimated_savings_usd,
            "estimated_regret_usd": self.estimated_regret_usd,
            "guardrail_violations": self.guardrail_violations,
            "evaluated_target_ids": list(self.evaluated_target_ids),
            "reasons": list(self.reasons),
            "router_state": self.router_state.to_payload(),
            "thresholds_digest": self.thresholds_digest,
        }

    @property
    def projection_digest(self) -> str:
        """Deterministic digest of the full metric projection (replay input)."""
        return canonical_digest(self.to_payload())


def evaluate_scope(bundle: ScopeEvaluationInput) -> ScopeQualificationResult:
    """Evaluate one scope exactly against its snapshotted thresholds.

    Every gate is applied independently and contributes its own explicit
    reason; the gates are never compressed into a single score. A scope
    qualifies only when no gate fails.
    """
    scope = bundle.scope
    thresholds = bundle.thresholds
    attempts = bundle.attempts

    reasons: list[str] = []

    # 1. Risk gate: high/critical scopes are deterministic-only forever.
    deterministic_only = scope.risk in _DETERMINISTIC_ONLY_RISKS
    if deterministic_only:
        reasons.append("high_risk_deterministic_only")

    # 2. Evidence binding: real project evidence with trusted acceptance.
    if any(attempt.evidence_kind != "real_project" for attempt in attempts):
        reasons.append("real_project_evidence_required")
    if not any(
        attempt.evidence_kind == "real_project" and attempt.trusted_acceptance
        for attempt in attempts
    ):
        reasons.append("trusted_acceptance_missing")

    # 3. Comparative coverage: every snapshotted eligible target must have
    #    been actually evaluated, and at least two real targets compared.
    evaluated_target_ids = tuple(sorted({attempt.target_id for attempt in attempts}))
    covered = set(evaluated_target_ids)
    if len(covered) < 2 or any(
        target_id not in covered for target_id in scope.target_ids
    ):
        reasons.append("comparative_target_coverage_missing")

    # 4. Guardrails: zero hard violations by default.
    guardrail_violations = sum(
        1 for attempt in attempts if attempt.guardrail_state == "violated"
    )
    if guardrail_violations > thresholds.max_guardrail_violations:
        reasons.append("guardrail_violation")

    # 5. Learned evaluation reusing existing utility math and decay semantics.
    config = LearnedRouterConfig.from_qualification_thresholds(thresholds)
    examples = [attempt.to_route_example() for attempt in attempts]
    router_state = LearnedRouterState.from_examples(examples, config)
    shadow = evaluate_shadow(
        examples=examples,
        static_target_id=bundle.static_target_id,
        config=config,
    )

    # 6. Support, confidence, utility margin, and live cost coverage gates.
    total_support = router_state.total_examples
    if total_support < thresholds.min_examples_per_scope:
        reasons.append("sparse_evidence")
    selected_target_support = shadow.target_example_count
    if selected_target_support < thresholds.min_examples_per_target:
        reasons.append("sparse_target_evidence")
    if shadow.confidence < thresholds.confidence_threshold:
        reasons.append("low_confidence")
    if shadow.utility_improvement < thresholds.utility_margin:
        reasons.append("insufficient_utility_margin")
    if shadow.cost_coverage < thresholds.cost_coverage_threshold:
        reasons.append("insufficient_cost_coverage")

    unique_reasons = tuple(dict.fromkeys(reasons))
    qualified = not deterministic_only and not unique_reasons
    state: ScopeQualificationState = (
        "deterministic_only"
        if deterministic_only
        else ("qualified" if qualified else "abstained")
    )

    savings = shadow.estimated_savings_usd
    regret = None if savings is None else round(max(0.0, -savings), 8)

    return ScopeQualificationResult(
        scope_digest=scope.digest,
        state=state,
        static_target_id=bundle.static_target_id,
        selected_target_id=shadow.learned_target_id if qualified else None,
        total_support=total_support,
        selected_target_support=selected_target_support,
        confidence=shadow.confidence,
        static_utility=shadow.static_utility,
        learned_utility=shadow.learned_utility,
        utility_delta=shadow.utility_improvement,
        cost_coverage=shadow.cost_coverage,
        estimated_savings_usd=savings,
        estimated_regret_usd=regret,
        guardrail_violations=guardrail_violations,
        evaluated_target_ids=evaluated_target_ids,
        reasons=unique_reasons,
        router_state=router_state,
        thresholds_digest=thresholds.digest,
    )
