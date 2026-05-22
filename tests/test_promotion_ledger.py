from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.promotion_ledger import PromotionEntry, PromotionLedger, PromotionOutcome
from nested_memvid_agent.state_store import AgentStateStore


def test_promotion_ledger_records_promotion_outcome_and_summary(tmp_path: Path) -> None:
    ledger = PromotionLedger(AgentStateStore(tmp_path / "state.db"))
    entry = _entry("promotion-1", target_layer=MemoryLayer.PROCEDURAL)
    ledger.record_promotion(entry)
    ledger.record_outcome(
        PromotionOutcome(
            promotion_id=entry.promotion_id,
            outcome="useful",
            evidence_record_id="evidence-1",
            notes="used by a later repair",
            recorded_at=datetime.now(UTC).isoformat(),
        )
    )

    summary = ledger.summarize()

    assert summary.rows[0].promoted == 1
    assert summary.rows[0].outcome_counts["useful"] == 1
    assert ledger.get_promotion("promotion-1") == entry


def test_promotion_ledger_allows_multiple_outcomes(tmp_path: Path) -> None:
    ledger = PromotionLedger(AgentStateStore(tmp_path / "state.db"))
    entry = _entry("promotion-1")
    ledger.record_promotion(entry)
    ledger.record_outcome(
        PromotionOutcome(
            promotion_id=entry.promotion_id,
            outcome="useful",
            evidence_record_id="run-1",
            notes="initially helped",
            recorded_at=datetime.now(UTC).isoformat(),
        )
    )
    ledger.record_outcome(
        PromotionOutcome(
            promotion_id=entry.promotion_id,
            outcome="corrected",
            evidence_record_id="correction-1",
            notes="later corrected",
            recorded_at=datetime.now(UTC).isoformat(),
        )
    )

    row = ledger.summarize().rows[0]

    assert row.outcome_counts["useful"] == 1
    assert row.outcome_counts["corrected"] == 1
    assert row.false_positive_rate == 1.0


def test_conflict_metadata_records_contradicted_outcome(tmp_path: Path) -> None:
    ledger = PromotionLedger(AgentStateStore(tmp_path / "state.db"))
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend, ledger=ledger)
    promoted = _promoted_record(
        promotion_id="promotion-contradicted",
        title="Provider setting",
        content="Provider setting is enabled.",
    )
    memory.put(promoted)
    memory.put(
        MemoryRecord(
            title="Provider setting",
            content="Provider setting is not enabled.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )

    row = ledger.summarize().rows[0]

    assert row.outcome_counts["contradicted"] == 1


def test_tombstoning_promoted_record_records_tombstoned_outcome(tmp_path: Path) -> None:
    ledger = PromotionLedger(AgentStateStore(tmp_path / "state.db"))
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend, ledger=ledger)
    promoted = _promoted_record(promotion_id="promotion-tombstoned")
    record_id = memory.put(promoted)

    assert memory.tombstone(MemoryLayer.SEMANTIC, record_id, reason="test cleanup")

    row = ledger.summarize().rows[0]
    assert row.outcome_counts["tombstoned"] == 1


def test_ledger_summary_respects_since_layer_and_outcome_filters(tmp_path: Path) -> None:
    ledger = PromotionLedger(AgentStateStore(tmp_path / "state.db"))
    old = _entry(
        "old",
        target_layer=MemoryLayer.SEMANTIC,
        promoted_at=(datetime.now(UTC) - timedelta(days=40)).isoformat(),
    )
    recent = _entry("recent", target_layer=MemoryLayer.PROCEDURAL)
    ledger.record_promotion(old)
    ledger.record_promotion(recent)
    ledger.record_outcome(
        PromotionOutcome(
            promotion_id=recent.promotion_id,
            outcome="corrected",
            evidence_record_id="correction",
            notes="test",
            recorded_at=datetime.now(UTC).isoformat(),
        )
    )

    summary = ledger.summarize(
        since=datetime.now(UTC) - timedelta(days=7),
        target_layer=MemoryLayer.PROCEDURAL,
        outcome="corrected",
    )

    assert len(summary.rows) == 1
    assert summary.rows[0].target_layer == MemoryLayer.PROCEDURAL
    assert summary.rows[0].promoted == 1
    assert summary.rows[0].outcome_counts["corrected"] == 1


def test_learning_dashboard_aggregates_behavior_delta_outcomes_by_layer(tmp_path: Path) -> None:
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

    state = AgentStateStore(tmp_path / "state.db")
    delta_ledger = BehaviorDeltaLedger(state)
    now = datetime.now(UTC)
    auto_delta = BehaviorDelta(
        id="delta-auto",
        title="Retry safer",
        kind=BehaviorDeltaKind.PROCEDURE,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.LOW,
        status=BehaviorDeltaStatus.ACTIVE,
        trigger=TriggerSpec(task_types=("debugging",)),
        behavior_change="Change strategy before retrying validation.",
        evidence_refs=(EvidenceRef(source="test", locator="fixture"),),
        validation_plan=ValidationPlan(),
        metadata={"draft": True},
        created_at=(now - timedelta(hours=3)).isoformat(),
        updated_at=(now - timedelta(hours=3)).isoformat(),
    )
    rolled_back_delta = BehaviorDelta(
        id="delta-rolled",
        title="Bad hint",
        kind=BehaviorDeltaKind.PROCEDURE,
        target_layer=MemoryLayer.SEMANTIC,
        risk=BehaviorDeltaRisk.LOW,
        status=BehaviorDeltaStatus.ROLLED_BACK,
        trigger=TriggerSpec(task_types=("debugging",)),
        behavior_change="Prefer a bad hint.",
        evidence_refs=(EvidenceRef(source="test", locator="fixture"),),
        validation_plan=ValidationPlan(),
        metadata={"draft": True},
        created_at=(now - timedelta(hours=2)).isoformat(),
        updated_at=(now - timedelta(hours=2)).isoformat(),
    )
    delta_ledger.record_delta(auto_delta)
    delta_ledger.record_delta(rolled_back_delta)
    delta_ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-auto",
            delta_id=auto_delta.id,
            run_id="run-1",
            task_id=None,
            objective="debug",
            activated_at=(now - timedelta(hours=2)).isoformat(),
            activation_reason="auto_activated_low_risk_threshold_met",
            compiled_section="ACTIVE PROCEDURES",
        )
    )
    delta_ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-rolled",
            delta_id=rolled_back_delta.id,
            run_id="run-2",
            task_id=None,
            objective="debug",
            activated_at=(now - timedelta(hours=1)).isoformat(),
            activation_reason="operator activated",
            compiled_section="ACTIVE PROCEDURES",
        )
    )
    delta_ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-rolled",
            delta_id=rolled_back_delta.id,
            run_id="run-2",
            outcome="rolled_back",
            notes="operator rollback",
            recorded_at=now.isoformat(),
        )
    )
    delta_ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-failure",
            delta_id=rolled_back_delta.id,
            run_id="run-2",
            outcome="caused_failure",
            notes="false positive",
            recorded_at=now.isoformat(),
        )
    )

    dashboard = PromotionLedger(state).learning_dashboard(since=now - timedelta(days=1))

    assert dashboard.headline.auto_activations == 1
    assert dashboard.headline.rollbacks == 1
    assert dashboard.headline.activations_then_rolled_back == 1
    assert dashboard.headline.false_positive_rate == 0.5
    assert dashboard.headline.average_time_to_rollback_hours == 1.0
    by_layer = {row.layer: row for row in dashboard.layers}
    assert by_layer[MemoryLayer.PROCEDURAL].auto_activations == 1
    assert by_layer[MemoryLayer.SEMANTIC].rollbacks == 1
    assert by_layer[MemoryLayer.SEMANTIC].false_positive_rate == 1.0


def _entry(
    promotion_id: str,
    *,
    target_layer: MemoryLayer = MemoryLayer.SEMANTIC,
    promoted_at: str | None = None,
) -> PromotionEntry:
    return PromotionEntry(
        promotion_id=promotion_id,
        record_id=f"record-{promotion_id}",
        source_layer=MemoryLayer.EPISODIC,
        target_layer=target_layer,
        decision_reason="test promotion",
        validation_score=0.9,
        repeat_count=2,
        explicit_instruction=False,
        optimizer_trace={"validation_score": 0.9},
        promoted_at=promoted_at or datetime.now(UTC).isoformat(),
    )


def _promoted_record(
    *,
    promotion_id: str,
    title: str = "Promoted fact",
    content: str = "Promoted fact is enabled.",
) -> MemoryRecord:
    return MemoryRecord(
        title=title,
        content=content,
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.9,
        metadata={
            "promotion_id": promotion_id,
            "promotion_status": "confirmed",
            "source_layer": MemoryLayer.EPISODIC.value,
            "validation_score": 0.9,
            "repeat_count": 2,
            "explicit_instruction": False,
            "nested_learning": {
                "context_flow": {"source_layers": [MemoryLayer.EPISODIC.value]},
                "decision": {"reason": "test promotion"},
                "optimizer_trace": {"validation_score": 0.9},
            },
        },
    )
