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
from typing import Any


@dataclass(frozen=True)
class LearnedRouterConfig:
    """Configuration for the learned shadow router."""

    min_examples: int = 5
    confidence_threshold: float = 0.70
    activation_margin: float = 0.08
    hard_filtered_targets: frozenset[str] = field(default_factory=frozenset)
    high_risk_families: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.min_examples < 1:
            raise ValueError("min_examples must be >= 1")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0 and 1")
        if not 0.0 <= self.activation_margin <= 1.0:
            raise ValueError("activation_margin must be between 0 and 1")


@dataclass(frozen=True)
class RouteExample:
    """A single routing example built from a verified outcome record."""

    decision_id: str
    target_id: str
    validation_passed: bool
    execution_status: str
    failure_category: str | None
    actual_cost_usd: float
    latency_seconds: float
    task_family: str
    risk: str
    contract_digest: str


@dataclass(frozen=True)
class TargetScore:
    """Aggregated score for a target from learned examples."""

    target_id: str
    validation_rate: float
    avg_cost_usd: float
    avg_latency_seconds: float
    example_count: int
    utility: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "validation_rate": round(self.validation_rate, 6),
            "avg_cost_usd": round(self.avg_cost_usd, 6),
            "avg_latency_seconds": round(self.avg_latency_seconds, 6),
            "example_count": self.example_count,
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
            return cls()

        task_family = examples[0].task_family
        total = len(examples)

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
            provider_outage_exs = [
                ex for ex in target_exs
                if ex.failure_category == "provider_outage"
            ]

            # Validation rate is based on task-quality outcomes only
            if task_quality_exs:
                validated = sum(1 for ex in task_quality_exs if ex.validation_passed)
                validation_rate = validated / len(task_quality_exs)
            else:
                validation_rate = 0.0

            # Cost and latency use all non-outage examples
            non_outage = task_quality_exs or target_exs
            if non_outage:
                avg_cost = sum(ex.actual_cost_usd for ex in non_outage) / len(non_outage)
                avg_latency = sum(ex.latency_seconds for ex in non_outage) / len(non_outage)
            else:
                avg_cost = 0.0
                avg_latency = 0.0

            # Utility: validation_rate / (1 + avg_cost) — higher is better
            utility = validation_rate / (1.0 + avg_cost) if avg_cost >= 0 else validation_rate

            target_scores[target_id] = TargetScore(
                target_id=target_id,
                validation_rate=round(validation_rate, 8),
                avg_cost_usd=round(avg_cost, 8),
                avg_latency_seconds=round(avg_latency, 8),
                example_count=len(target_exs),
                utility=round(utility, 8),
            )

        eligible = frozenset(target_scores.keys())

        # Config digest for determinism
        config_json = json.dumps(
            {
                "min_examples": config.min_examples,
                "confidence_threshold": config.confidence_threshold,
                "activation_margin": config.activation_margin,
                "hard_filtered_targets": sorted(config.hard_filtered_targets),
                "high_risk_families": sorted(config.high_risk_families),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        config_digest = hashlib.sha256(config_json.encode("utf-8")).hexdigest()[:16]

        return cls(
            target_scores=target_scores,
            eligible_targets=eligible,
            task_family=task_family,
            total_examples=total,
            config_digest=config_digest,
        )


@dataclass(frozen=True)
class ShadowEvaluation:
    """Result of shadow evaluation comparing learned vs static policy."""

    static_target_id: str
    learned_target_id: str | None
    utility_improvement: float
    confidence: float
    should_activate: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "static_target_id": self.static_target_id,
            "learned_target_id": self.learned_target_id,
            "utility_improvement": round(self.utility_improvement, 8),
            "confidence": round(self.confidence, 8),
            "should_activate": self.should_activate,
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
            actual_cost_usd=float(raw.get("actual_cost_usd", 0.0) or 0.0),
            latency_seconds=float(raw.get("latency_seconds", 0.0) or 0.0),
            task_family=str(raw.get("task_family", "")),
            risk=str(raw.get("risk", "low")),
            contract_digest=str(raw.get("contract_digest", "")),
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
        )

    # Find the best learned target by utility
    best_target = max(
        state.target_scores.values(),
        key=lambda s: (s.utility, s.validation_rate, -s.avg_cost_usd, s.target_id),
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
    should = should_activate_learned_policy(examples, config=config)
    if should:
        # Also require utility margin
        if improvement < config.activation_margin:
            should = False

    return ShadowEvaluation(
        static_target_id=static_target_id,
        learned_target_id=best_target.target_id,
        utility_improvement=round(improvement, 8),
        confidence=round(confidence, 8),
        should_activate=should,
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
    if len(examples) < config.min_examples:
        return False

    # High-risk families never auto-activate
    task_families = {ex.task_family for ex in examples}
    if task_families & config.high_risk_families:
        return False

    # Check confidence: the best target must have enough support
    state = LearnedRouterState.from_examples(examples, config)
    if not state.target_scores:
        return False

    best_target = max(
        state.target_scores.values(),
        key=lambda s: (s.utility, s.validation_rate, -s.avg_cost_usd, s.target_id),
    )
    confidence = best_target.example_count / state.total_examples if state.total_examples > 0 else 0.0

    if confidence < config.confidence_threshold:
        return False

    return True


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