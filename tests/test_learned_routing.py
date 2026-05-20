from __future__ import annotations

from typing import Any, cast

from nested_memvid_agent.behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    TriggerSpec,
    ValidationPlan,
)
from nested_memvid_agent.behavior_delta_ledger import (
    BehaviorDeltaActivation,
    BehaviorDeltaLedger,
    BehaviorDeltaOutcome,
)
from nested_memvid_agent.learned_routing import (
    OutcomeCalibratedRouter,
    RoutingExample,
    behavior_delta_shadow_examples_from_ledger,
    evaluate_behavior_delta_shadow_examples,
    evaluate_routing_examples,
    routing_example_from_decision,
)
from nested_memvid_agent.models import EvidenceRef, MemoryKind, MemoryLayer
from nested_memvid_agent.nested_learning import LearningSignal, NestedLearningKernel
from nested_memvid_agent.state_store import AgentStateStore


def test_shadow_router_records_counterfactual_without_changing_rule_decision() -> None:
    router = OutcomeCalibratedRouter.fit(
        (
            _example("episodic-win", target=MemoryLayer.EPISODIC, reward=0.95, outcomes=("useful",)),
            _example("semantic-loss", target=MemoryLayer.SEMANTIC, reward=-1.10, outcomes=("corrected",)),
        ),
        mode="shadow",
        confidence_threshold=0.0,
        min_examples_per_target=1,
    )
    signal = LearningSignal(
        title="Near durable fact",
        content="The workbench provider selector is easier to discover when shown inline.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=0.82,
        repeat_count=1,
    )
    kernel = NestedLearningKernel(router=router)

    decision = kernel.decide(signal)
    payload = decision.to_payload()
    learned = cast(dict[str, Any], payload["learned_routing"])

    assert decision.accepted
    assert decision.target_layer == MemoryLayer.SEMANTIC
    assert learned["target_layer"] == "episodic"
    assert learned["abstained"] is True
    assert "shadow" in str(learned["reason"])


def test_routing_example_includes_gate_margin_vector() -> None:
    signal = LearningSignal(
        title="Semantic near miss",
        content="A repeated user correction should become a stable fact.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=0.73,
        repeat_count=1,
    )
    rule_decision = NestedLearningKernel().decide(signal)

    example = routing_example_from_decision(signal, rule_decision)

    assert example.signal_features["semantic_margin"] == -0.05
    assert example.signal_features["semantic_provisional_margin"] == 0.08
    assert example.signal_features["semantic_repeat_margin"] == 0.0


def test_router_blocks_policy_without_explicit_repeat_gates() -> None:
    router = OutcomeCalibratedRouter.fit(
        (
            _example("policy-win", target=MemoryLayer.POLICY, reward=1.0, outcomes=("useful",)),
            _example("working-neutral", target=MemoryLayer.WORKING, reward=-0.02, outcomes=()),
        ),
        mode="constrained",
        confidence_threshold=0.0,
        min_examples_per_target=1,
    )
    signal = LearningSignal(
        title="One-off preference",
        content="A single repair used one command successfully.",
        kind=MemoryKind.POLICY,
        source_layer=MemoryLayer.PROCEDURAL,
        validation_score=0.99,
        repeat_count=1,
        explicit_instruction=False,
    )
    rule_decision = NestedLearningKernel().decide(signal)

    prediction = router.predict(signal, rule_decision)

    assert prediction.target_layer != MemoryLayer.POLICY
    assert any("policy" in block for block in prediction.guardrail_blocks)


def test_router_blocks_provisional_source_promotions() -> None:
    router = OutcomeCalibratedRouter.fit(
        (
            _example("procedural-win", target=MemoryLayer.PROCEDURAL, reward=1.0, outcomes=("useful",)),
            _example("reject-neutral", target=None, reward=0.0, outcomes=()),
        ),
        mode="constrained",
        confidence_threshold=0.0,
        min_examples_per_target=1,
    )
    signal = LearningSignal(
        title="Provisional recipe",
        content="A provisional record should not become a procedure.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.SEMANTIC,
        validation_score=0.95,
        repeat_count=3,
        metadata={"promotion_status": "provisional"},
    )
    rule_decision = NestedLearningKernel().decide(signal)

    prediction = router.predict(signal, rule_decision)

    assert prediction.target_layer is None
    assert any("provisional" in block for block in prediction.guardrail_blocks)


def test_replay_eval_can_show_oracle_utility_lift_on_synthetic_history() -> None:
    examples = (
        _example("semantic-corrected-1", target=MemoryLayer.SEMANTIC, reward=-1.10, outcomes=("corrected",)),
        _example("semantic-corrected-2", target=MemoryLayer.SEMANTIC, reward=-1.10, outcomes=("contradicted",)),
        _example("episodic-useful-1", target=MemoryLayer.EPISODIC, reward=0.95, outcomes=("useful",)),
        _example("episodic-useful-2", target=MemoryLayer.EPISODIC, reward=0.95, outcomes=("useful",)),
    )
    router = OutcomeCalibratedRouter.fit(
        examples,
        mode="constrained",
        confidence_threshold=0.0,
        min_examples_per_target=1,
    )

    report = evaluate_routing_examples(examples, router)
    payload = report.to_payload()

    assert payload["oracle"]["gate_violations"] == 0
    assert payload["oracle"]["abstention_rate"] < 1.0
    assert payload["improvement"]["expected_utility_delta"] > 0.15
    assert payload["improvement"]["passes"] is True


def test_behavior_delta_shadow_examples_include_outcome_features_without_authority(tmp_path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "agent.db"))
    delta = _behavior_delta(
        "delta_policy_guard",
        kind=BehaviorDeltaKind.POLICY,
        target_layer=MemoryLayer.POLICY,
        risk=BehaviorDeltaRisk.HIGH,
        status=BehaviorDeltaStatus.ACTIVE,
    )
    ledger.record_delta(delta)
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-1",
            delta_id=delta.id,
            run_id="run-1",
            task_id="task-1",
            objective="modify policy memory",
            activated_at="2026-05-20T00:00:00+00:00",
            activation_reason="matched policy task",
            compiled_section="ACTIVE POLICY CONSTRAINTS",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-1",
            delta_id=delta.id,
            run_id="run-1",
            outcome="useful",
            recorded_at="2026-05-20T00:01:00+00:00",
            evidence_ref=EvidenceRef(source="replay", locator="policy_write_requires_approval"),
        )
    )

    examples = behavior_delta_shadow_examples_from_ledger(ledger)
    report = evaluate_behavior_delta_shadow_examples(examples)
    payload = report.to_payload()

    assert len(examples) == 1
    features = examples[0].features
    assert features["delta_kind"] == "policy"
    assert features["target_layer"] == "policy"
    assert features["risk"] == "high"
    assert features["activation_count"] == 1.0
    assert features["useful_rate"] == 1.0
    assert features["failure_rate"] == 0.0
    assert features["trigger_specificity"] > 0.0
    assert examples[0].recommendation in {"keep_active", "monitor"}
    assert examples[0].authority == "shadow_only"
    assert payload["authority"] == "shadow_only"
    assert payload["gate_authority"] == "mutation_gate"
    assert payload["policy_write_authority"] is False
    assert payload["counterfactuals"][0]["decision"] in {"keep_active", "monitor"}


def test_behavior_delta_shadow_recommends_review_for_failed_active_delta(tmp_path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "agent.db"))
    delta = _behavior_delta(
        "delta_bad_tool_rule",
        kind=BehaviorDeltaKind.TOOL_HEURISTIC,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.MEDIUM,
        status=BehaviorDeltaStatus.ACTIVE,
    )
    ledger.record_delta(delta)
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-1",
            delta_id=delta.id,
            run_id="run-1",
            task_id="task-1",
            objective="retry validation",
            activated_at="2026-05-20T00:00:00+00:00",
            activation_reason="matched validation retry",
            compiled_section="ACTIVE TOOL HEURISTICS",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(id="out-1", delta_id=delta.id, run_id="run-1", outcome="caused_failure", recorded_at="2026-05-20T00:01:00+00:00")
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(id="out-2", delta_id=delta.id, run_id="run-2", outcome="contradicted", recorded_at="2026-05-20T00:02:00+00:00")
    )

    report = evaluate_behavior_delta_shadow_examples(behavior_delta_shadow_examples_from_ledger(ledger)).to_payload()

    assert report["counterfactuals"][0]["decision"] == "review_or_rollback"
    assert report["counterfactuals"][0]["actionable"] is False
    assert report["counterfactuals"][0]["authority"] == "shadow_only"
    assert report["summary"]["review_or_rollback"] == 1


def test_router_model_state_round_trips_through_payload() -> None:
    examples = (
        _example("episodic-win", target=MemoryLayer.EPISODIC, reward=0.95, outcomes=("useful",)),
        _example("semantic-loss", target=MemoryLayer.SEMANTIC, reward=-1.10, outcomes=("corrected",)),
    )
    router = OutcomeCalibratedRouter.fit(
        examples,
        mode="constrained",
        confidence_threshold=0.0,
        min_examples_per_target=1,
    )

    restored = OutcomeCalibratedRouter.from_payload(router.to_payload())

    before = router.predict_example(examples[1])
    after = restored.predict_example(examples[1])
    assert after.target_layer == before.target_layer
    assert after.expected_utility == before.expected_utility
    assert after.guardrail_blocks == before.guardrail_blocks


def _behavior_delta(
    delta_id: str,
    *,
    kind: BehaviorDeltaKind,
    target_layer: MemoryLayer,
    risk: BehaviorDeltaRisk,
    status: BehaviorDeltaStatus,
) -> BehaviorDelta:
    return BehaviorDelta(
        id=delta_id,
        title=f"Shadow delta {delta_id}",
        kind=kind,
        target_layer=target_layer,
        risk=risk,
        status=status,
        trigger=TriggerSpec(
            query_patterns=("policy", "validation", "retry"),
            task_types=("repair",),
            tool_names=("shell.run",),
            memory_layers=(target_layer,),
            risk_tags=(risk.value,),
            semantic_hint="Use for behavior-delta ORACLE shadow evaluation fixtures.",
        ),
        behavior_change="When the trigger matches, apply the staged behavior change under existing gates.",
        evidence_refs=(EvidenceRef(source="test", locator=delta_id),),
        validation_plan=ValidationPlan(
            required_checks=("unit",),
            replay_scenarios=("shadow_fixture",),
            min_validation_score=0.8,
            min_repeat_count=1,
        ),
    )


def _example(
    promotion_id: str,
    *,
    target: MemoryLayer | None,
    reward: float,
    outcomes: tuple[str, ...],
) -> RoutingExample:
    target_name = "" if target is None else target.value
    return RoutingExample(
        signal_features={
            "source_layer": MemoryLayer.EPISODIC.value,
            "memory_kind": MemoryKind.FACT.value,
            "requested_target_layer": "",
            "validation_score": 0.76,
            "repeat_count": 1,
            "explicit_instruction": False,
            "confidence": 0.65,
            "importance": 0.5,
            "promotion_status": "confirmed",
            "rule_target_layer": target_name,
            "semantic_margin": -0.02,
            "semantic_provisional_margin": 0.11,
            "semantic_repeat_margin": 0.0,
            "episodic_margin": 0.11,
            "episodic_provisional_margin": 0.24,
            "episodic_repeat_margin": 0.0,
        },
        rule_action="reject" if target is None else "promote",
        rule_target_layer=target,
        chosen_action="reject" if target is None else "promote",
        chosen_target_layer=target,
        outcome_reward=reward,
        promotion_id=promotion_id,
        outcome_labels=outcomes,
    )
