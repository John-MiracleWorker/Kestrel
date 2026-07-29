"""Learned shadow router.

Builds routing examples from verified outcome records, evaluates shadow
residual against the static policy, and gates constrained activation behind
minimum support, confidence, and utility margin thresholds.

Key safety properties:
- Sparse evidence causes abstention (min_examples threshold)
- Hard-filtered targets never become eligible through learning
- Provider outage does not become task-quality punishment
- Replayed history produces deterministic model state
- Learned residual can demonstrate utility lift on synthetic fixtures
- Policy/high-risk behavior cannot auto-change through route outcomes
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import exp, isfinite, log
from typing import Any


@dataclass(frozen=True)
class LearnedRouterConfig:
    """Configuration for the learned shadow router."""

    min_examples: int = 5
    min_target_examples: int = 3
    confidence_threshold: float = 0.70
    activation_margin: float = 0.08
    cost_coverage_threshold: float = 0.80
    decay_half_life_days: float = 30.0
    replay_gate_enabled: bool = False
    hard_filtered_targets: frozenset[str] = field(default_factory=frozenset)
    high_risk_families: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.min_examples < 1:
            raise ValueError("min_examples must be >= 1")
        if self.min_target_examples < 1:
            raise ValueError("min_target_examples must be >= 1")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0 and 1")
        if not 0.0 <= self.activation_margin <= 1.0:
            raise ValueError("activation_margin must be between 0 and 1")
        if not 0.0 <= self.cost_coverage_threshold <= 1.0:
            raise ValueError("cost_coverage_threshold must be between 0 and 1")
        if self.decay_half_life_days <= 0:
            raise ValueError("decay_half_life_days must be positive")


@dataclass(frozen=True)
class RouteExample:
    """A single routing example built from a verified outcome record."""

    decision_id: str
    target_id: str
    validation_passed: bool
    execution_status: str
    failure_category: str | None
    actual_cost_usd: float | None
    latency_seconds: float | None
    task_family: str
    risk: str
    contract_digest: str
    project_id: str | None = None
    capability_key: str = "none"
    created_at: str = ""


@dataclass(frozen=True)
class TargetScore:
    """Aggregated score for a target from learned examples."""

    target_id: str
    validation_rate: float
    avg_cost_usd: float | None
    avg_latency_seconds: float | None
    cost_coverage: float
    example_count: int
    effective_sample_size: float
    utility: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "validation_rate": round(self.validation_rate, 6),
            "avg_cost_usd": (
                None if self.avg_cost_usd is None else round(self.avg_cost_usd, 6)
            ),
            "avg_latency_seconds": (
                None
                if self.avg_latency_seconds is None
                else round(self.avg_latency_seconds, 6)
            ),
            "cost_coverage": round(self.cost_coverage, 6),
            "example_count": self.example_count,
            "effective_sample_size": round(self.effective_sample_size, 6),
            "utility": round(self.utility, 6),
        }


@dataclass(frozen=True)
class LearnedRouterState:
    """Deterministic learned router state built from examples."""

    target_scores: dict[str, TargetScore] = field(default_factory=dict)
    eligible_targets: frozenset[str] = field(default_factory=frozenset)
    task_family: str = ""
    total_examples: int = 0
    config_digest: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "target_scores": {k: v.to_payload() for k, v in self.target_scores.items()},
            "eligible_targets": sorted(self.eligible_targets),
            "task_family": self.task_family,
            "total_examples": self.total_examples,
            "config_digest": self.config_digest,
        }

    @classmethod
    def from_examples(
        cls,
        examples: list[RouteExample],
        config: LearnedRouterConfig,
    ) -> LearnedRouterState:
        """Build deterministic state from a list of routing examples.

        The state is order-independent: shuffling the input examples produces
        the same state. Provider outages (failure_category == 'provider_outage')
        do not count against task quality.
        """
        if not examples:
            return cls(config_digest=_config_digest(config))

        task_family = examples[0].task_family
        total = len(examples)
        reference_time = _reference_time(examples)

        # Group by target, excluding hard-filtered targets
        target_examples: dict[str, list[RouteExample]] = {}
        for ex in examples:
            if ex.target_id in config.hard_filtered_targets:
                continue
            target_examples.setdefault(ex.target_id, []).append(ex)

        target_scores: dict[str, TargetScore] = {}
        for target_id, target_exs in sorted(target_examples.items()):
            # Separate provider outages from task-quality outcomes
            task_quality_exs = [
                ex for ex in target_exs
                if ex.failure_category != "provider_outage"
            ]

            # Validation rate is based on task-quality outcomes only
            weighted_quality = [
                (_example_weight(ex, reference_time=reference_time, config=config), ex)
                for ex in task_quality_exs
            ]
            quality_weight = sum(weight for weight, _ex in weighted_quality)
            if quality_weight:
                validated = sum(
                    weight for weight, ex in weighted_quality if ex.validation_passed
                )
                validation_rate = validated / quality_weight
            else:
                validation_rate = 0.0

            cost_items = [
                (weight, ex.actual_cost_usd)
                for weight, ex in weighted_quality
                if ex.actual_cost_usd is not None
            ]
            latency_items = [
                (weight, ex.latency_seconds)
                for weight, ex in weighted_quality
                if ex.latency_seconds is not None
            ]
            avg_cost = _weighted_average(cost_items)
            avg_latency = _weighted_average(latency_items)
            cost_weight = sum(weight for weight, _value in cost_items)
            cost_coverage = cost_weight / quality_weight if quality_weight else 0.0

            # Missing price data is uncertainty, never a free route.
            utility = (
                validation_rate / (1.0 + avg_cost)
                if avg_cost is not None
                else validation_rate * 0.95
            )

            target_scores[target_id] = TargetScore(
                target_id=target_id,
                validation_rate=round(validation_rate, 8),
                avg_cost_usd=None if avg_cost is None else round(avg_cost, 8),
                avg_latency_seconds=(
                    None if avg_latency is None else round(avg_latency, 8)
                ),
                cost_coverage=round(min(1.0, cost_coverage), 8),
                example_count=len(target_exs),
                effective_sample_size=round(
                    sum(
                        _example_weight(ex, reference_time=reference_time, config=config)
                        for ex in target_exs
                    ),
                    8,
                ),
                utility=round(utility, 8),
            )

        eligible = frozenset(target_scores.keys())

        # Config digest for determinism
        return cls(
            target_scores=target_scores,
            eligible_targets=eligible,
            task_family=task_family,
            total_examples=total,
            config_digest=_config_digest(config),
        )


@dataclass(frozen=True)
class ShadowEvaluation:
    """Result of shadow evaluation comparing learned vs static policy."""

    static_target_id: str
    learned_target_id: str | None
    utility_improvement: float
    confidence: float
    should_activate: bool
    evidence_count: int = 0
    target_example_count: int = 0
    cost_coverage: float = 0.0
    static_utility: float | None = None
    learned_utility: float | None = None
    estimated_savings_usd: float | None = None
    abstention_reason: str | None = None
    config_digest: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "static_target_id": self.static_target_id,
            "learned_target_id": self.learned_target_id,
            "utility_improvement": round(self.utility_improvement, 8),
            "confidence": round(self.confidence, 8),
            "should_activate": self.should_activate,
            "evidence_count": self.evidence_count,
            "target_example_count": self.target_example_count,
            "cost_coverage": round(self.cost_coverage, 8),
            "static_utility": self.static_utility,
            "learned_utility": self.learned_utility,
            "estimated_savings_usd": self.estimated_savings_usd,
            "abstention_reason": self.abstention_reason,
            "config_digest": self.config_digest,
        }


def build_route_examples(
    raw_outcomes: list[dict[str, Any]],
) -> list[RouteExample]:
    """Convert raw outcome dicts (from RouteOutcomeEntry.to_payload()) into
    RouteExample objects suitable for the learned router."""
    examples: list[RouteExample] = []
    for raw in raw_outcomes:
        examples.append(RouteExample(
            decision_id=str(raw.get("decision_id", "")),
            target_id=str(raw.get("target_id", raw.get("selected_target_id", ""))),
            validation_passed=bool(raw.get("validation_passed", False)),
            execution_status=str(raw.get("execution_status", "")),
            failure_category=raw.get("failure_category"),
            actual_cost_usd=_optional_non_negative_float(raw.get("actual_cost_usd")),
            latency_seconds=_optional_non_negative_float(raw.get("latency_seconds")),
            task_family=str(raw.get("task_family", "")),
            risk=str(raw.get("risk", "low")),
            contract_digest=str(raw.get("contract_digest", "")),
            project_id=(
                None if raw.get("project_id") is None else str(raw.get("project_id"))
            ),
            capability_key=str(raw.get("capability_key", "none")),
            created_at=str(raw.get("created_at", "")),
        ))
    return examples


def evaluate_shadow(
    *,
    examples: list[RouteExample],
    static_target_id: str,
    config: LearnedRouterConfig,
) -> ShadowEvaluation:
    """Evaluate the learned policy in shadow mode against the static target.

    Returns the learned target, utility improvement, confidence, and whether
    activation should occur.
    """
    state = LearnedRouterState.from_examples(examples, config)

    if not state.target_scores:
        return ShadowEvaluation(
            static_target_id=static_target_id,
            learned_target_id=None,
            utility_improvement=0.0,
            confidence=0.0,
            should_activate=False,
            evidence_count=state.total_examples,
            abstention_reason=(
                "sparse_evidence"
                if state.total_examples < config.min_examples
                else "no_eligible_learned_target"
            ),
            config_digest=state.config_digest,
        )

    # Find the best learned target by utility
    best_target = max(
        state.target_scores.values(),
        key=_target_sort_key,
    )

    # Confidence: proportion of examples that agree with the best target
    best_count = best_target.example_count
    total = state.total_examples
    confidence = best_count / total if total > 0 else 0.0

    # Utility improvement over static
    static_score = state.target_scores.get(static_target_id)
    if static_score is not None:
        improvement = best_target.utility - static_score.utility
    else:
        improvement = best_target.utility

    # Should activate?
    abstention_reason = _activation_abstention_reason(
        examples,
        best_target=best_target,
        utility_improvement=improvement,
        config=config,
    )
    should = abstention_reason is None
    estimated_savings = (
        static_score.avg_cost_usd - best_target.avg_cost_usd
        if static_score is not None
        and static_score.avg_cost_usd is not None
        and best_target.avg_cost_usd is not None
        else None
    )

    return ShadowEvaluation(
        static_target_id=static_target_id,
        learned_target_id=best_target.target_id,
        utility_improvement=round(improvement, 8),
        confidence=round(confidence, 8),
        should_activate=should,
        evidence_count=state.total_examples,
        target_example_count=best_target.example_count,
        cost_coverage=best_target.cost_coverage,
        static_utility=None if static_score is None else static_score.utility,
        learned_utility=best_target.utility,
        estimated_savings_usd=(
            None if estimated_savings is None else round(estimated_savings, 8)
        ),
        abstention_reason=abstention_reason,
        config_digest=state.config_digest,
    )


def should_activate_learned_policy(
    examples: list[RouteExample],
    *,
    config: LearnedRouterConfig,
) -> bool:
    """Determine whether the learned policy should be activated.

    Returns False (abstain) when:
    - Fewer than min_examples examples exist
    - The task family is high-risk
    - The confidence is below the threshold
    """
    state = LearnedRouterState.from_examples(examples, config)
    if not state.target_scores:
        return False

    best_target = max(
        state.target_scores.values(),
        key=_target_sort_key,
    )
    return (
        _activation_abstention_reason(
            examples,
            best_target=best_target,
            utility_improvement=None,
            config=config,
        )
        is None
    )


def replay_history(
    raw_outcomes: list[dict[str, Any]],
    *,
    config: LearnedRouterConfig,
) -> LearnedRouterState:
    """Replay a list of raw outcome dicts and produce deterministic learned state.

    This is the replay harness for shadow evaluation. The same input always
    produces the same state, regardless of order.
    """
    examples = build_route_examples(raw_outcomes)
    return LearnedRouterState.from_examples(examples, config)


def _activation_abstention_reason(
    examples: list[RouteExample],
    *,
    best_target: TargetScore,
    utility_improvement: float | None,
    config: LearnedRouterConfig,
) -> str | None:
    if len(examples) < config.min_examples:
        return "sparse_evidence"
    if best_target.example_count < config.min_target_examples:
        return "sparse_target_evidence"
    task_families = {example.task_family for example in examples}
    if task_families & config.high_risk_families:
        return "high_risk_family"
    if any(example.risk in {"high", "critical"} for example in examples):
        return "high_risk"
    confidence = best_target.example_count / len(examples)
    if confidence < config.confidence_threshold:
        return "low_confidence"
    if best_target.cost_coverage < config.cost_coverage_threshold:
        return "insufficient_cost_coverage"
    if (
        utility_improvement is not None
        and utility_improvement < config.activation_margin
    ):
        return "utility_margin_not_met"
    return None


def _target_sort_key(score: TargetScore) -> tuple[float, float, float, str]:
    cost = score.avg_cost_usd
    return (
        score.utility,
        score.validation_rate,
        -(cost if cost is not None else float("inf")),
        score.target_id,
    )


def _weighted_average(items: list[tuple[float, float]]) -> float | None:
    total_weight = sum(weight for weight, _value in items)
    if total_weight <= 0:
        return None
    return sum(weight * value for weight, value in items) / total_weight


def _reference_time(examples: list[RouteExample]) -> datetime | None:
    observed = [
        parsed
        for example in examples
        if example.created_at
        and (parsed := _parse_timestamp(example.created_at)) is not None
    ]
    return max(observed) if observed else None


def _example_weight(
    example: RouteExample,
    *,
    reference_time: datetime | None,
    config: LearnedRouterConfig,
) -> float:
    if reference_time is None or not example.created_at:
        return 1.0
    observed = _parse_timestamp(example.created_at)
    if observed is None:
        return 1.0
    age_days = max(0.0, (reference_time - observed).total_seconds() / 86_400.0)
    return exp(-log(2.0) * age_days / config.decay_half_life_days)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _optional_non_negative_float(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if isfinite(parsed) and parsed >= 0 else None


def _config_digest(config: LearnedRouterConfig) -> str:
    config_json = json.dumps(
        {
            "min_examples": config.min_examples,
            "min_target_examples": config.min_target_examples,
            "confidence_threshold": config.confidence_threshold,
            "activation_margin": config.activation_margin,
            "cost_coverage_threshold": config.cost_coverage_threshold,
            "decay_half_life_days": config.decay_half_life_days,
            "replay_gate_enabled": config.replay_gate_enabled,
            "hard_filtered_targets": sorted(config.hard_filtered_targets),
            "high_risk_families": sorted(config.high_risk_families),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(config_json.encode("utf-8")).hexdigest()[:16]
