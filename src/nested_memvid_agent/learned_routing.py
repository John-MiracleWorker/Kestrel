from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from .layers import DEFAULT_LAYER_SPECS, LayerSpec
from .models import MemoryKind, MemoryLayer
from .nested_learning import LearningAction, LearningDecision, LearningSignal
from .promotion_ledger import PromotionEntry, PromotionLedger, PromotionOutcome

RoutingMode = Literal["shadow", "constrained"]
RoutingFeature = float | str | bool

_ROUTE_TARGETS: tuple[MemoryLayer | None, ...] = (
    None,
    MemoryLayer.WORKING,
    MemoryLayer.EPISODIC,
    MemoryLayer.SEMANTIC,
    MemoryLayer.PROCEDURAL,
    MemoryLayer.SELF,
    MemoryLayer.POLICY,
)
_FALSE_POSITIVE_OUTCOMES = {"corrected", "contradicted"}
_OUTCOME_REWARDS = {
    "useful": 1.0,
    "confirmed_provisional": 0.4,
    "corrected": -1.0,
    "contradicted": -1.0,
    "tombstoned": -0.5,
    "never_retrieved": -0.4,
    "superseded": -0.2,
}
_LAYER_COSTS: dict[MemoryLayer | None, float] = {
    None: 0.0,
    MemoryLayer.WORKING: 0.02,
    MemoryLayer.EPISODIC: 0.05,
    MemoryLayer.SEMANTIC: 0.10,
    MemoryLayer.PROCEDURAL: 0.18,
    MemoryLayer.SELF: 0.18,
    MemoryLayer.POLICY: 0.35,
}


@dataclass(frozen=True)
class RoutingExample:
    signal_features: dict[str, RoutingFeature]
    rule_action: str
    rule_target_layer: MemoryLayer | None
    chosen_action: str
    chosen_target_layer: MemoryLayer | None
    outcome_reward: float | None
    promotion_id: str | None
    outcome_labels: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "signal_features": dict(self.signal_features),
            "rule_action": self.rule_action,
            "rule_target_layer": _target_label(self.rule_target_layer),
            "chosen_action": self.chosen_action,
            "chosen_target_layer": _target_label(self.chosen_target_layer),
            "outcome_reward": self.outcome_reward,
            "promotion_id": self.promotion_id,
            "outcome_labels": list(self.outcome_labels),
        }


@dataclass(frozen=True)
class RoutingPrediction:
    action: LearningAction
    target_layer: MemoryLayer | None
    expected_utility: float
    confidence: float
    abstained: bool
    reason: str
    guardrail_blocks: tuple[str, ...]


@dataclass(frozen=True)
class RouteStats:
    count: int
    expected_utility: float
    false_positive_rate: float
    never_retrieved_rate: float
    useful_rate: float

    def to_payload(self) -> dict[str, object]:
        return {
            "count": self.count,
            "expected_utility": round(self.expected_utility, 4),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "never_retrieved_rate": round(self.never_retrieved_rate, 4),
            "useful_rate": round(self.useful_rate, 4),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RouteStats:
        return cls(
            count=int(payload.get("count", 0)),
            expected_utility=float(payload.get("expected_utility", 0.0)),
            false_positive_rate=float(payload.get("false_positive_rate", 0.0)),
            never_retrieved_rate=float(payload.get("never_retrieved_rate", 0.0)),
            useful_rate=float(payload.get("useful_rate", 0.0)),
        )


@dataclass(frozen=True)
class RoutingEvaluation:
    baseline: dict[str, Any]
    oracle: dict[str, Any]
    improvement: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "oracle": self.oracle,
            "improvement": self.improvement,
        }


class OutcomeCalibratedRouter:
    """Small residual memory router trained from promotion outcomes.

    The router is intentionally boring: it learns average utility per target layer
    from ledger outcomes, scores only guardrail-admissible targets, and abstains
    unless a candidate has enough support and margin over the rule decision.
    """

    def __init__(
        self,
        *,
        mode: RoutingMode = "shadow",
        stats_by_target: dict[MemoryLayer | None, RouteStats] | None = None,
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        confidence_threshold: float = 0.65,
        activation_margin: float = 0.05,
        min_examples_per_target: int = 3,
    ) -> None:
        self.mode = mode
        self.stats_by_target = stats_by_target or {}
        self.specs = specs or DEFAULT_LAYER_SPECS
        self.confidence_threshold = confidence_threshold
        self.activation_margin = activation_margin
        self.min_examples_per_target = min_examples_per_target

    @classmethod
    def shadow(
        cls,
        examples: Iterable[RoutingExample] = (),
        *,
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        confidence_threshold: float = 0.65,
        activation_margin: float = 0.05,
        min_examples_per_target: int = 3,
    ) -> OutcomeCalibratedRouter:
        return cls.fit(
            examples,
            mode="shadow",
            specs=specs,
            confidence_threshold=confidence_threshold,
            activation_margin=activation_margin,
            min_examples_per_target=min_examples_per_target,
        )

    @classmethod
    def fit(
        cls,
        examples: Iterable[RoutingExample],
        *,
        mode: RoutingMode = "shadow",
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        confidence_threshold: float = 0.65,
        activation_margin: float = 0.05,
        min_examples_per_target: int = 3,
    ) -> OutcomeCalibratedRouter:
        example_tuple = tuple(examples)
        return cls(
            mode=mode,
            stats_by_target=_stats_by_target(example_tuple),
            specs=specs,
            confidence_threshold=confidence_threshold,
            activation_margin=activation_margin,
            min_examples_per_target=min_examples_per_target,
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        specs: dict[MemoryLayer, LayerSpec] | None = None,
    ) -> OutcomeCalibratedRouter:
        raw_stats = payload.get("target_stats", {})
        stats_by_target: dict[MemoryLayer | None, RouteStats] = {}
        if isinstance(raw_stats, dict):
            for target_label, raw_value in raw_stats.items():
                if isinstance(raw_value, dict):
                    stats_by_target[_target_from_label(str(target_label))] = RouteStats.from_payload(raw_value)
        return cls(
            mode="constrained" if payload.get("mode") == "constrained" else "shadow",
            stats_by_target=stats_by_target,
            specs=specs,
            confidence_threshold=float(payload.get("confidence_threshold", 0.65)),
            activation_margin=float(payload.get("activation_margin", 0.05)),
            min_examples_per_target=int(payload.get("min_examples_per_target", 3)),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "confidence_threshold": self.confidence_threshold,
            "activation_margin": self.activation_margin,
            "min_examples_per_target": self.min_examples_per_target,
            "target_stats": {
                _target_label(target): stats.to_payload()
                for target, stats in sorted(self.stats_by_target.items(), key=lambda item: _target_label(item[0]))
            },
        }

    def predict(self, signal: LearningSignal, rule_decision: LearningDecision) -> RoutingPrediction:
        example = routing_example_from_decision(signal, rule_decision, specs=self.specs)
        return self.predict_example(example)

    def predict_example(self, example: RoutingExample) -> RoutingPrediction:
        allowed_targets, guardrail_blocks = _allowed_targets(example.signal_features, self.specs)
        scores = [
            (target, self._expected_utility(target, example.signal_features))
            for target in _ROUTE_TARGETS
            if target in allowed_targets
        ]
        if not scores:
            scores = [(None, 0.0)]
        best_target, best_utility = max(scores, key=lambda item: item[1])
        rule_utility = self._expected_utility(example.rule_target_layer, example.signal_features)
        utility_delta = best_utility - rule_utility
        confidence = self._confidence(best_target, utility_delta)
        abstain_reasons = self._abstain_reasons(best_target, example.rule_target_layer, utility_delta, confidence)
        return RoutingPrediction(
            action=_action_for_target(best_target, example.signal_features, self.specs),
            target_layer=best_target,
            expected_utility=round(best_utility, 4),
            confidence=round(confidence, 4),
            abstained=bool(abstain_reasons),
            reason="; ".join(abstain_reasons) or "constrained router selected an admissible route",
            guardrail_blocks=guardrail_blocks,
        )

    def explain(self, prediction: RoutingPrediction) -> dict[str, object]:
        return {
            "action": prediction.action,
            "target_layer": _target_label(prediction.target_layer),
            "expected_utility": prediction.expected_utility,
            "confidence": prediction.confidence,
            "abstained": prediction.abstained,
            "reason": prediction.reason,
            "guardrail_blocks": list(prediction.guardrail_blocks),
        }

    def _expected_utility(self, target: MemoryLayer | None, features: dict[str, RoutingFeature]) -> float:
        stats = self.stats_by_target.get(target)
        base = stats.expected_utility if stats is not None else -_layer_cost(target)
        if target is None:
            return base
        margin = _feature_float(features, f"{target.value}_margin", 0.0)
        repeat_margin = _feature_float(features, f"{target.value}_repeat_margin", 0.0)
        residual = max(min(margin, 0.25), -0.25) * 0.2
        residual += max(min(repeat_margin, 2.0), -2.0) * 0.02
        return base + residual

    def _confidence(self, target: MemoryLayer | None, utility_delta: float) -> float:
        stats = self.stats_by_target.get(target)
        support = 0.0 if stats is None else min(stats.count / max(self.min_examples_per_target, 1), 1.0)
        spread = min(abs(utility_delta), 1.0)
        return max(0.0, min(0.99, 0.35 + 0.45 * support + 0.20 * spread))

    def _abstain_reasons(
        self,
        target: MemoryLayer | None,
        rule_target: MemoryLayer | None,
        utility_delta: float,
        confidence: float,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        stats = self.stats_by_target.get(target)
        if self.mode == "shadow":
            reasons.append("shadow mode records counterfactual routing only")
        if target != rule_target:
            if stats is None or stats.count < self.min_examples_per_target:
                reasons.append(
                    f"insufficient outcome support for {_target_label(target)} "
                    f"({0 if stats is None else stats.count} < {self.min_examples_per_target})"
                )
            if confidence < self.confidence_threshold:
                reasons.append(
                    f"confidence {confidence:.2f} below activation threshold {self.confidence_threshold:.2f}"
                )
            if utility_delta < self.activation_margin:
                reasons.append(
                    f"utility delta {utility_delta:.2f} below activation margin {self.activation_margin:.2f}"
                )
        return tuple(reasons)


def routing_example_from_decision(
    signal: LearningSignal,
    rule_decision: LearningDecision,
    *,
    specs: dict[MemoryLayer, LayerSpec] | None = None,
) -> RoutingExample:
    features = signal_features(signal, rule_decision, specs=specs)
    target = rule_decision.target_layer
    return RoutingExample(
        signal_features=features,
        rule_action=rule_decision.action,
        rule_target_layer=target,
        chosen_action=rule_decision.action,
        chosen_target_layer=target,
        outcome_reward=None,
        promotion_id=None,
        outcome_labels=(),
    )


def signal_features(
    signal: LearningSignal,
    rule_decision: LearningDecision,
    *,
    specs: dict[MemoryLayer, LayerSpec] | None = None,
) -> dict[str, RoutingFeature]:
    layer_specs = specs or DEFAULT_LAYER_SPECS
    trace = rule_decision.optimizer_trace
    features: dict[str, RoutingFeature] = {
        "source_layer": signal.source_layer.value,
        "memory_kind": signal.kind.value,
        "requested_target_layer": _target_label(signal.requested_target_layer),
        "validation_score": signal.computed_validation_score,
        "repeat_count": float(signal.repeat_count),
        "explicit_instruction": signal.explicit_instruction,
        "confidence": signal.confidence,
        "importance": signal.importance,
        "promotion_status": str((signal.metadata or {}).get("promotion_status", "confirmed")),
        "rule_action": rule_decision.action,
        "rule_target_layer": _target_label(rule_decision.target_layer),
        "surprise": trace.surprise,
        "compression_ratio": -1.0 if trace.compression_ratio is None else trace.compression_ratio,
        "confidence_delta": trace.confidence_delta,
        "effective_confidence": trace.effective_confidence,
    }
    self_schema = (signal.metadata or {}).get("self_schema")
    if self_schema is not None:
        features["self_schema"] = str(self_schema)
    features.update(_gate_margin_features(signal.computed_validation_score, signal.repeat_count, layer_specs))
    return features


def routing_examples_from_ledger(ledger: PromotionLedger) -> tuple[RoutingExample, ...]:
    entries = ledger._entries(since=None, target_layer=None, outcome=None)
    outcomes = ledger._outcomes_for_entries([entry.promotion_id for entry in entries], outcome=None)
    return tuple(_routing_example_from_entry(entry, outcomes.get(entry.promotion_id, [])) for entry in entries)


def outcome_reward(target_layer: MemoryLayer | None, outcomes: Iterable[PromotionOutcome | str]) -> float:
    reward = -_layer_cost(target_layer)
    for item in outcomes:
        if isinstance(item, str):
            label = item
            notes = ""
        else:
            label = item.outcome
            notes = item.notes
        reward += _OUTCOME_REWARDS.get(label, 0.0)
        lowered_notes = notes.lower()
        if label == "useful" and "provisional" in lowered_notes and "confirmed" in lowered_notes:
            reward += _OUTCOME_REWARDS["confirmed_provisional"]
    return round(reward, 4)


def evaluate_routing_examples(
    examples: Iterable[RoutingExample],
    router: OutcomeCalibratedRouter,
    *,
    min_utility_delta: float = 0.15,
) -> RoutingEvaluation:
    example_tuple = tuple(examples)
    predictions = tuple(router.predict_example(example) for example in example_tuple)
    baseline_utility = sum(_example_reward(example) for example in example_tuple)
    oracle_utility = 0.0
    gate_violations = 0
    oracle_targets: list[MemoryLayer | None] = []
    for example, prediction in zip(example_tuple, predictions, strict=True):
        if prediction.abstained:
            oracle_utility += _example_reward(example)
            oracle_targets.append(example.rule_target_layer)
            continue
        oracle_utility += prediction.expected_utility
        oracle_targets.append(prediction.target_layer)
        allowed, _ = _allowed_targets(example.signal_features, router.specs)
        if prediction.target_layer not in allowed:
            gate_violations += 1

    baseline_rates = _observed_rates(example_tuple)
    oracle_rates = _predicted_rates(oracle_targets, router.stats_by_target)
    abstention_rate = (sum(1 for item in predictions if item.abstained) / len(predictions)) if predictions else 0.0
    utility_delta = _relative_delta(oracle_utility, baseline_utility)
    baseline: dict[str, Any] = {
        "examples": len(example_tuple),
        "expected_utility": round(baseline_utility, 4),
        **baseline_rates,
    }
    oracle: dict[str, Any] = {
        "examples": len(example_tuple),
        "expected_utility": round(oracle_utility, 4),
        **oracle_rates,
        "abstention_rate": round(abstention_rate, 4),
        "gate_violations": gate_violations,
    }
    improvement: dict[str, Any] = {
        "expected_utility_delta": round(utility_delta, 4),
        "passes": (
            utility_delta >= min_utility_delta
            and float(oracle["false_positive_rate"]) <= float(baseline["false_positive_rate"])
            and gate_violations == 0
        ),
    }
    return RoutingEvaluation(baseline=baseline, oracle=oracle, improvement=improvement)


def _routing_example_from_entry(entry: PromotionEntry, outcomes: list[PromotionOutcome]) -> RoutingExample:
    labels = tuple(item.outcome for item in outcomes)
    features = _entry_features(entry)
    return RoutingExample(
        signal_features=features,
        rule_action="promote",
        rule_target_layer=entry.target_layer,
        chosen_action="promote",
        chosen_target_layer=entry.target_layer,
        outcome_reward=outcome_reward(entry.target_layer, outcomes),
        promotion_id=entry.promotion_id,
        outcome_labels=labels,
    )


def _entry_features(entry: PromotionEntry) -> dict[str, RoutingFeature]:
    features: dict[str, RoutingFeature] = {
        "source_layer": entry.source_layer.value,
        "memory_kind": _memory_kind_for_target(entry.target_layer).value,
        "requested_target_layer": "",
        "validation_score": entry.validation_score,
        "repeat_count": float(entry.repeat_count),
        "explicit_instruction": entry.explicit_instruction,
        "confidence": 0.0,
        "importance": 0.0,
        "promotion_status": "confirmed",
        "rule_action": "promote",
        "rule_target_layer": entry.target_layer.value,
    }
    for key in ("surprise", "compression_ratio", "confidence_delta", "effective_confidence"):
        value = entry.optimizer_trace.get(key)
        if isinstance(value, int | float):
            features[key] = float(value)
    features.update(_gate_margin_features(entry.validation_score, entry.repeat_count, DEFAULT_LAYER_SPECS))
    return features


def _stats_by_target(examples: tuple[RoutingExample, ...]) -> dict[MemoryLayer | None, RouteStats]:
    grouped: dict[MemoryLayer | None, list[RoutingExample]] = defaultdict(list)
    for example in examples:
        grouped[example.chosen_target_layer].append(example)
    return {target: _route_stats(group) for target, group in grouped.items()}


def _route_stats(examples: list[RoutingExample]) -> RouteStats:
    rewards = [_example_reward(example) for example in examples]
    counts = Counter(label for example in examples for label in example.outcome_labels)
    promoted = len(examples)
    return RouteStats(
        count=promoted,
        expected_utility=sum(rewards) / promoted if promoted else 0.0,
        false_positive_rate=(
            sum(counts.get(label, 0) for label in _FALSE_POSITIVE_OUTCOMES) / promoted if promoted else 0.0
        ),
        never_retrieved_rate=counts.get("never_retrieved", 0) / promoted if promoted else 0.0,
        useful_rate=counts.get("useful", 0) / promoted if promoted else 0.0,
    )


def _observed_rates(examples: tuple[RoutingExample, ...]) -> dict[str, float]:
    if not examples:
        return {"false_positive_rate": 0.0, "never_retrieved_rate": 0.0, "useful_rate": 0.0}
    counts = Counter(label for example in examples for label in example.outcome_labels)
    promoted = len(examples)
    return {
        "false_positive_rate": round(sum(counts.get(label, 0) for label in _FALSE_POSITIVE_OUTCOMES) / promoted, 4),
        "never_retrieved_rate": round(counts.get("never_retrieved", 0) / promoted, 4),
        "useful_rate": round(counts.get("useful", 0) / promoted, 4),
    }


def _predicted_rates(
    targets: list[MemoryLayer | None],
    stats_by_target: dict[MemoryLayer | None, RouteStats],
) -> dict[str, float]:
    if not targets:
        return {"false_positive_rate": 0.0, "never_retrieved_rate": 0.0, "useful_rate": 0.0}
    false_positive = 0.0
    never_retrieved = 0.0
    useful = 0.0
    for target in targets:
        stats = stats_by_target.get(target)
        if stats is None:
            continue
        false_positive += stats.false_positive_rate
        never_retrieved += stats.never_retrieved_rate
        useful += stats.useful_rate
    total = len(targets)
    return {
        "false_positive_rate": round(false_positive / total, 4),
        "never_retrieved_rate": round(never_retrieved / total, 4),
        "useful_rate": round(useful / total, 4),
    }


def _allowed_targets(
    features: dict[str, RoutingFeature],
    specs: dict[MemoryLayer, LayerSpec],
) -> tuple[set[MemoryLayer | None], tuple[str, ...]]:
    source = _feature_layer(features, "source_layer")
    kind = _feature_kind(features)
    score = _feature_float(features, "validation_score", 0.0)
    repeat_count = int(_feature_float(features, "repeat_count", 0.0))
    explicit_instruction = _feature_bool(features, "explicit_instruction")
    promotion_status = str(features.get("promotion_status", "confirmed"))
    if promotion_status == "provisional" and source != MemoryLayer.WORKING:
        return {None}, ("provisional source records cannot be promoted further",)

    allowed: set[MemoryLayer | None] = {None, MemoryLayer.WORKING}
    blocks: list[str] = []
    if source == MemoryLayer.WORKING:
        allowed.add(MemoryLayer.EPISODIC)
    elif source == MemoryLayer.EPISODIC:
        allowed.update({MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC})
        _allow_procedural(allowed, blocks, kind, repeat_count, specs)
        _allow_self(allowed, blocks, score, repeat_count, features, specs)
    elif source == MemoryLayer.SEMANTIC:
        allowed.add(MemoryLayer.SEMANTIC)
        _allow_procedural(allowed, blocks, kind, repeat_count, specs)
    elif source == MemoryLayer.PROCEDURAL:
        allowed.add(MemoryLayer.PROCEDURAL)
        _allow_policy(allowed, blocks, score, repeat_count, explicit_instruction, specs)
    elif source == MemoryLayer.SELF:
        allowed.add(MemoryLayer.SELF)
    elif source == MemoryLayer.POLICY:
        allowed.add(MemoryLayer.POLICY)
    return allowed, tuple(blocks)


def _allow_procedural(
    allowed: set[MemoryLayer | None],
    blocks: list[str],
    kind: MemoryKind | None,
    repeat_count: int,
    specs: dict[MemoryLayer, LayerSpec],
) -> None:
    spec = specs[MemoryLayer.PROCEDURAL]
    if kind in {MemoryKind.FAILURE, MemoryKind.PROCEDURE} and repeat_count >= spec.min_repeat_count_for_promotion:
        allowed.add(MemoryLayer.PROCEDURAL)
        return
    blocks.append(
        "procedural blocked: requires failure/procedure kind and "
        f"repeat_count >= {spec.min_repeat_count_for_promotion}"
    )


def _allow_self(
    allowed: set[MemoryLayer | None],
    blocks: list[str],
    score: float,
    repeat_count: int,
    features: dict[str, RoutingFeature],
    specs: dict[MemoryLayer, LayerSpec],
) -> None:
    spec = specs[MemoryLayer.SELF]
    has_schema = bool(str(features.get("self_schema", "")).strip())
    if has_schema and score >= _provisional_threshold(spec) and repeat_count >= spec.min_repeat_count_for_promotion:
        allowed.add(MemoryLayer.SELF)
        return
    blocks.append("self blocked: requires self_schema evidence and self-memory provisional gate")


def _allow_policy(
    allowed: set[MemoryLayer | None],
    blocks: list[str],
    score: float,
    repeat_count: int,
    explicit_instruction: bool,
    specs: dict[MemoryLayer, LayerSpec],
) -> None:
    spec = specs[MemoryLayer.POLICY]
    if (
        explicit_instruction
        and repeat_count >= spec.min_repeat_count_for_promotion
        and score >= _provisional_threshold(spec)
    ):
        allowed.add(MemoryLayer.POLICY)
        return
    blocks.append(
        "policy blocked: requires explicit_instruction, "
        f"repeat_count >= {spec.min_repeat_count_for_promotion}, and validation >= {_provisional_threshold(spec):.2f}"
    )


def _gate_margin_features(
    validation_score: float,
    repeat_count: int,
    specs: dict[MemoryLayer, LayerSpec],
) -> dict[str, RoutingFeature]:
    features: dict[str, RoutingFeature] = {}
    for layer, spec in specs.items():
        name = layer.value
        provisional = _provisional_threshold(spec)
        features[f"{name}_threshold"] = spec.promotion_threshold
        features[f"{name}_provisional_threshold"] = provisional
        features[f"{name}_margin"] = round(validation_score - spec.promotion_threshold, 4)
        features[f"{name}_provisional_margin"] = round(validation_score - provisional, 4)
        features[f"{name}_repeat_margin"] = float(repeat_count - spec.min_repeat_count_for_promotion)
    return features


def _action_for_target(
    target: MemoryLayer | None,
    features: dict[str, RoutingFeature],
    specs: dict[MemoryLayer, LayerSpec],
) -> LearningAction:
    if target is None:
        return "reject"
    source = _feature_layer(features, "source_layer")
    if target == MemoryLayer.WORKING or target == source:
        return "write"
    score = _feature_float(features, "validation_score", 0.0)
    spec = specs[target]
    if score < spec.promotion_threshold and score >= _provisional_threshold(spec):
        return "promote_provisional"
    return "promote"


def _feature_layer(features: dict[str, RoutingFeature], key: str) -> MemoryLayer | None:
    value = str(features.get(key, "")).strip()
    if not value:
        return None
    try:
        return MemoryLayer(value)
    except ValueError:
        return None


def _feature_kind(features: dict[str, RoutingFeature]) -> MemoryKind | None:
    value = str(features.get("memory_kind", "")).strip()
    if not value:
        return None
    try:
        return MemoryKind(value)
    except ValueError:
        return None


def _feature_float(features: dict[str, RoutingFeature], key: str, default: float) -> float:
    value = features.get(key)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _feature_bool(features: dict[str, RoutingFeature], key: str) -> bool:
    value = features.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, int | float):
        return value != 0
    return False


def _memory_kind_for_target(target: MemoryLayer) -> MemoryKind:
    if target == MemoryLayer.POLICY:
        return MemoryKind.POLICY
    if target == MemoryLayer.PROCEDURAL:
        return MemoryKind.PROCEDURE
    if target == MemoryLayer.EPISODIC:
        return MemoryKind.EVENT
    return MemoryKind.FACT


def _example_reward(example: RoutingExample) -> float:
    if example.outcome_reward is not None:
        return example.outcome_reward
    return -_layer_cost(example.chosen_target_layer)


def _layer_cost(target: MemoryLayer | None) -> float:
    return _LAYER_COSTS.get(target, 0.0)


def _relative_delta(candidate: float, baseline: float) -> float:
    denominator = max(abs(baseline), 0.01)
    return (candidate - baseline) / denominator


def _target_label(target: MemoryLayer | None) -> str:
    return "" if target is None else target.value


def _target_from_label(label: str) -> MemoryLayer | None:
    value = label.strip()
    if not value:
        return None
    return MemoryLayer(value)


def _provisional_threshold(spec: LayerSpec) -> float:
    if spec.provisional_threshold is not None:
        return spec.provisional_threshold
    return max(0.0, spec.promotion_threshold - 0.13)
