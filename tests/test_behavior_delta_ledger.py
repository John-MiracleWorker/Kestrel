from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nested_memvid_agent.behavior_delta import (
    ActivationStats,
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    RollbackPlan,
    TriggerSpec,
    ValidationPlan,
)
from nested_memvid_agent.behavior_delta_ledger import (
    BehaviorDeltaActivation,
    BehaviorDeltaLedger,
    BehaviorDeltaOutcome,
)
from nested_memvid_agent.models import EvidenceRef, MemoryLayer
from nested_memvid_agent.state_store import SCHEMA_VERSION, AgentStateStore


def _delta(
    delta_id: str = "delta_policy_gate",
    *,
    status: BehaviorDeltaStatus = BehaviorDeltaStatus.PROPOSED,
    kind: BehaviorDeltaKind = BehaviorDeltaKind.POLICY,
    target_layer: MemoryLayer = MemoryLayer.POLICY,
    risk: BehaviorDeltaRisk = BehaviorDeltaRisk.HIGH,
) -> BehaviorDelta:
    return BehaviorDelta(
        id=delta_id,
        title="Policy gate delta",
        kind=kind,
        target_layer=target_layer,
        risk=risk,
        status=status,
        trigger=TriggerSpec(
            query_patterns=("policy", "approval"),
            task_types=("repo_modification",),
            memory_layers=(target_layer,),
            semantic_hint="Policy or approval-gate changes.",
        ),
        behavior_change="When modifying policy memory, require approval-gate tests first.",
        evidence_refs=(EvidenceRef(source="task_capsule", locator="run-1:lesson-1", quote="Gate it."),),
        validation_plan=ValidationPlan(
            required_checks=("approval_gate_tests",),
            replay_scenarios=("policy_write_requires_approval",),
            requires_human_approval=True,
            requires_exact_call_approval=True,
            min_validation_score=0.97,
            min_repeat_count=2,
        ),
        rollback_plan=RollbackPlan(can_disable=True, rollback_notes="Disable and preserve audit."),
        activation_stats=ActivationStats(activation_count=1, success_count=1),
        confidence=0.86,
        importance=0.9,
        created_from_run_id="run-1",
        created_at="2026-05-19T00:00:00+00:00",
        updated_at="2026-05-19T00:01:00+00:00",
        metadata={"explicit_instruction": True},
    )


def test_schema_migration_adds_behavior_delta_tables_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    state = AgentStateStore(db_path)
    AgentStateStore(db_path)

    assert state.schema_version() == SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list('behavior_delta_ledger')")}

    assert {
        "behavior_delta_ledger",
        "behavior_delta_activations",
        "behavior_delta_outcomes",
    } <= tables
    assert "idx_behavior_delta_ledger_status" in indexes
    assert "idx_behavior_delta_ledger_kind" in indexes


def test_delta_records_round_trip_through_sqlite(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta()

    ledger.record_delta(delta)
    restored = ledger.get_delta(delta.id)

    assert restored == delta
    assert ledger.list_deltas(status=BehaviorDeltaStatus.PROPOSED) == [delta]
    assert ledger.list_deltas(kind=BehaviorDeltaKind.POLICY) == [delta]
    assert ledger.list_deltas(target_layer=MemoryLayer.POLICY) == [delta]


def test_update_delta_status_preserves_immutable_terminal_statuses(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta(status=BehaviorDeltaStatus.REJECTED)
    ledger.record_delta(delta)

    with pytest.raises(ValueError, match="terminal"):
        ledger.update_delta_status(delta.id, BehaviorDeltaStatus.ACTIVE, reason="changed mind")

    assert ledger.get_delta(delta.id).status == BehaviorDeltaStatus.REJECTED  # type: ignore[union-attr]


def test_status_update_records_reason_metadata(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta()
    ledger.record_delta(delta)

    updated = ledger.update_delta_status(delta.id, BehaviorDeltaStatus.STAGED, reason="awaiting replay")

    assert updated.status == BehaviorDeltaStatus.STAGED
    assert updated.metadata["status_reason"] == "awaiting replay"
    assert updated.metadata["previous_status"] == BehaviorDeltaStatus.PROPOSED.value


def test_auto_activate_low_risk_deltas_only_promotes_safe_validated_deltas(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    ready = _auto_delta(
        "delta_ready",
        metadata={"validation_score": 0.86, "repeat_count": 1},
    )
    under_validated = _auto_delta(
        "delta_under_validated",
        metadata={"validation_score": 0.2, "repeat_count": 1},
    )
    missing_validation = _auto_delta("delta_missing_validation")
    policy = _auto_delta(
        "delta_policy",
        kind=BehaviorDeltaKind.POLICY,
        target_layer=MemoryLayer.POLICY,
        metadata={"validation_score": 1.0, "repeat_count": 1},
    )
    ledger.record_delta(ready)
    ledger.record_delta(under_validated)
    ledger.record_delta(missing_validation)
    ledger.record_delta(policy)

    activated = ledger.auto_activate_low_risk_deltas(run_id="run-auto", objective="validate retries")

    assert [delta.id for delta in activated] == ["delta_ready"]
    assert ledger.get_delta("delta_ready").status == BehaviorDeltaStatus.ACTIVE  # type: ignore[union-attr]
    assert ledger.get_delta("delta_under_validated").status == BehaviorDeltaStatus.STAGED  # type: ignore[union-attr]
    assert ledger.get_delta("delta_missing_validation").status == BehaviorDeltaStatus.STAGED  # type: ignore[union-attr]
    assert ledger.get_delta("delta_policy").status == BehaviorDeltaStatus.STAGED  # type: ignore[union-attr]
    activations = ledger.list_activations("delta_ready")
    assert len(activations) == 1
    assert activations[0].activation_reason == "auto_activated_low_risk_threshold_met"
    assert activations[0].run_id == "run-auto"


def test_activation_and_outcome_records_are_append_only(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta(status=BehaviorDeltaStatus.ACTIVE)
    ledger.record_delta(delta)

    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-1",
            delta_id=delta.id,
            run_id="run-1",
            task_id="task-1",
            objective="Modify policy gates",
            activated_at="2026-05-19T01:00:00+00:00",
            activation_reason="query matched policy",
            compiled_section="ACTIVE POLICY CONSTRAINTS",
        )
    )
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-2",
            delta_id=delta.id,
            run_id="run-2",
            task_id=None,
            objective="Review policy memory",
            activated_at="2026-05-19T02:00:00+00:00",
            activation_reason="task type matched",
            compiled_section="ACTIVE POLICY CONSTRAINTS",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-1",
            delta_id=delta.id,
            run_id="run-1",
            outcome="useful",
            evidence_ref=EvidenceRef(source="test.run", locator="pytest"),
            notes="Blocked unsafe write.",
            recorded_at="2026-05-19T03:00:00+00:00",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-2",
            delta_id=delta.id,
            run_id="run-2",
            outcome="rolled_back",
            evidence_ref=None,
            notes="Operator disabled it.",
            recorded_at="2026-05-19T04:00:00+00:00",
        )
    )

    assert [item.id for item in ledger.list_activations(delta.id)] == ["act-1", "act-2"]
    assert [item.id for item in ledger.list_outcomes(delta.id)] == ["out-1", "out-2"]


def test_summary_reports_useful_failure_rollback_and_never_activated_rates(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    active = _delta("delta_active", status=BehaviorDeltaStatus.ACTIVE)
    never = _delta(
        "delta_never",
        status=BehaviorDeltaStatus.ACTIVE,
        kind=BehaviorDeltaKind.TOOL_HEURISTIC,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.MEDIUM,
    )
    ledger.record_delta(active)
    ledger.record_delta(never)
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-1",
            delta_id=active.id,
            run_id="run-1",
            task_id=None,
            objective="Policy task",
            activated_at="2026-05-19T01:00:00+00:00",
            activation_reason="matched",
            compiled_section="ACTIVE POLICY CONSTRAINTS",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-1",
            delta_id=active.id,
            run_id="run-1",
            outcome="useful",
            evidence_ref=None,
            recorded_at="2026-05-19T02:00:00+00:00",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-2",
            delta_id=active.id,
            run_id="run-2",
            outcome="caused_failure",
            evidence_ref=None,
            recorded_at="2026-05-19T03:00:00+00:00",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-3",
            delta_id=active.id,
            run_id="run-3",
            outcome="rolled_back",
            evidence_ref=None,
            recorded_at="2026-05-19T04:00:00+00:00",
        )
    )

    summary = ledger.summarize_deltas()
    payload = summary.to_payload()

    assert payload["total_deltas"] == 2
    assert payload["useful_rate"] == 0.5
    assert payload["failure_rate"] == 0.5
    assert payload["rollback_rate"] == 0.5
    assert payload["never_activated_rate"] == 0.5
    assert payload["outcomes"]["useful"] == 1
    assert payload["outcomes"]["caused_failure"] == 1
    assert payload["outcomes"]["rolled_back"] == 1


def _auto_delta(
    delta_id: str,
    *,
    kind: BehaviorDeltaKind = BehaviorDeltaKind.PROCEDURE,
    target_layer: MemoryLayer = MemoryLayer.PROCEDURAL,
    metadata: dict[str, object] | None = None,
) -> BehaviorDelta:
    return BehaviorDelta(
        id=delta_id,
        title="Validated low-risk delta",
        kind=kind,
        target_layer=target_layer,
        risk=BehaviorDeltaRisk.LOW,
        status=BehaviorDeltaStatus.STAGED,
        trigger=TriggerSpec(query_patterns=("validate",), task_types=("debugging",)),
        behavior_change="When validation fails, inspect the prior command before retrying.",
        evidence_refs=(EvidenceRef(source="test", locator=delta_id, quote="validated low-risk lesson"),),
        validation_plan=ValidationPlan(
            required_checks=("behavior_delta_review",),
            min_validation_score=0.6,
            min_repeat_count=1,
        ),
        rollback_plan=RollbackPlan(can_disable=True),
        confidence=0.72,
        importance=0.7,
        metadata=metadata or {},
    )
