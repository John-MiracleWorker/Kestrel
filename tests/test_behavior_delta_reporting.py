from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pytest import MonkeyPatch

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
from nested_memvid_agent.cli import main
from nested_memvid_agent.models import EvidenceRef, MemoryLayer
from nested_memvid_agent.state_store import AgentStateStore


def _delta(
    delta_id: str,
    *,
    status: BehaviorDeltaStatus = BehaviorDeltaStatus.ACTIVE,
    kind: BehaviorDeltaKind = BehaviorDeltaKind.PROCEDURE,
    target_layer: MemoryLayer = MemoryLayer.PROCEDURAL,
    created_at: str = "2026-05-10T00:00:00+00:00",
) -> BehaviorDelta:
    return BehaviorDelta(
        id=delta_id,
        title=f"Delta {delta_id}",
        kind=kind,
        target_layer=target_layer,
        risk=BehaviorDeltaRisk.MEDIUM,
        status=status,
        trigger=TriggerSpec(query_patterns=("validation",), memory_layers=(target_layer,)),
        behavior_change="When validation fails, require a changed strategy before retrying.",
        evidence_refs=(EvidenceRef(source="test", locator=delta_id),),
        validation_plan=ValidationPlan(required_checks=("replay",), min_validation_score=0.8),
        created_at=created_at,
        updated_at=created_at,
    )


def test_behavior_delta_report_includes_per_delta_rates_and_advisory_recommendations(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    useful = _delta("delta_useful")
    failing = _delta("delta_failing")
    quiet = _delta("delta_never")
    for delta in (useful, failing, quiet):
        ledger.record_delta(delta)
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-useful",
            delta_id=useful.id,
            run_id="run-1",
            task_id=None,
            objective="repair validation",
            activated_at="2026-05-11T00:00:00+00:00",
            activation_reason="matched validation",
            compiled_section="ACTIVE PROCEDURES",
        )
    )
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-failing",
            delta_id=failing.id,
            run_id="run-2",
            task_id=None,
            objective="repair validation",
            activated_at="2026-05-12T00:00:00+00:00",
            activation_reason="matched validation",
            compiled_section="ACTIVE PROCEDURES",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-useful",
            delta_id=useful.id,
            run_id="run-1",
            outcome="useful",
            recorded_at="2026-05-11T01:00:00+00:00",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-failure",
            delta_id=failing.id,
            run_id="run-2",
            outcome="caused_failure",
            recorded_at="2026-05-12T01:00:00+00:00",
        )
    )

    report = ledger.report_deltas()
    payload = report.to_payload()

    assert payload["summary"]["total_deltas"] == 3
    assert payload["summary"]["useful_rate"] == pytest.approx(1 / 3, rel=0.001)
    assert payload["summary"]["failure_rate"] == pytest.approx(1 / 3, rel=0.001)
    assert payload["summary"]["never_activated_rate"] == pytest.approx(1 / 3, rel=0.001)
    rows = {row["delta_id"]: row for row in payload["deltas"]}
    assert rows["delta_useful"]["activation_count"] == 1
    assert rows["delta_useful"]["outcome_counts"]["useful"] == 1
    assert rows["delta_failing"]["failure_rate"] == 1.0
    assert rows["delta_never"]["never_activated"] is True
    assert any("delta_failing" in item and "review" in item.lower() for item in payload["recommendations"])
    assert all("auto" not in item.lower() for item in payload["recommendations"])
    assert ledger.get_delta(failing.id).status == BehaviorDeltaStatus.ACTIVE  # type: ignore[union-attr]


def test_behavior_delta_report_since_filters_old_activity(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    old = _delta("delta_old", created_at="2026-01-01T00:00:00+00:00")
    recent = _delta("delta_recent", created_at="2026-05-19T00:00:00+00:00")
    ledger.record_delta(old)
    ledger.record_delta(recent)
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-old",
            delta_id=old.id,
            run_id="run-old",
            outcome="caused_failure",
            recorded_at="2026-01-02T00:00:00+00:00",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-recent",
            delta_id=recent.id,
            run_id="run-recent",
            outcome="useful",
            recorded_at="2026-05-20T00:00:00+00:00",
        )
    )

    report = ledger.report_deltas(since="2026-05-01T00:00:00+00:00")
    payload = report.to_payload()

    assert [row["delta_id"] for row in payload["deltas"]] == ["delta_recent"]
    assert payload["summary"]["total_deltas"] == 1
    assert payload["summary"]["useful_rate"] == 1.0
    assert payload["summary"]["failure_rate"] == 0.0


def test_cli_memory_deltas_ledger_outputs_json_report(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    state_path = tmp_path / "state.db"
    ledger = BehaviorDeltaLedger(AgentStateStore(state_path))
    delta = _delta("delta_cli")
    ledger.record_delta(delta)
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-cli",
            delta_id=delta.id,
            run_id="run-cli",
            task_id=None,
            objective="repair validation",
            activated_at="2026-05-19T00:00:00+00:00",
            activation_reason="matched validation",
            compiled_section="ACTIVE PROCEDURES",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-cli",
            delta_id=delta.id,
            run_id="run-cli",
            outcome="useful",
            recorded_at="2026-05-19T01:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "deltas",
            "ledger",
            "--state-path",
            str(state_path),
            "--since",
            "all",
            "--json",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total_deltas"] == 1
    assert payload["summary"]["useful_rate"] == 1.0
    assert payload["deltas"][0]["delta_id"] == "delta_cli"
    assert payload["deltas"][0]["activation_count"] == 1
    assert payload["recommendations"] == []
