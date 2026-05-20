from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

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
from nested_memvid_agent.models import EvidenceRef, MemoryLayer
from nested_memvid_agent.server_behavior_delta_routes import register_behavior_delta_routes
from nested_memvid_agent.server import create_app
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.state_store import AgentStateStore


def test_behavior_delta_review_routes_list_show_and_skill_preview(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "agent.db"))
    delta = _delta("delta_skill_candidate", kind=BehaviorDeltaKind.SKILL_CANDIDATE, status=BehaviorDeltaStatus.STAGED)
    ledger.record_delta(delta)
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-1",
            delta_id=delta.id,
            run_id="run-1",
            task_id="task-1",
            objective="make validation retries safer",
            activated_at="2026-05-20T00:00:00+00:00",
            activation_reason="test fixture",
            compiled_section="ACTIVE PROCEDURES",
        )
    )
    ledger.record_outcome(
        BehaviorDeltaOutcome(
            id="out-1",
            delta_id=delta.id,
            run_id="run-1",
            outcome="useful",
            recorded_at="2026-05-20T00:01:00+00:00",
            evidence_ref=EvidenceRef(source="test", locator="outcome"),
        )
    )
    client = _client(ledger)

    listed = client.get("/api/memory/deltas", params={"since": "all"})
    shown = client.get(f"/api/memory/deltas/{delta.id}")
    preview = client.get(f"/api/memory/deltas/{delta.id}/skill-preview")

    assert listed.status_code == 200
    list_payload = listed.json()
    assert list_payload["summary"]["total_deltas"] == 1
    assert list_payload["deltas"][0]["delta_id"] == delta.id
    assert list_payload["deltas"][0]["activation_count"] == 1
    assert list_payload["deltas"][0]["outcome_counts"]["useful"] == 1
    assert shown.status_code == 200
    show_payload = shown.json()
    assert show_payload["delta"]["id"] == delta.id
    assert show_payload["activations"][0]["id"] == "act-1"
    assert show_payload["outcomes"][0]["id"] == "out-1"
    assert show_payload["review_actions"] == {
        "can_activate": False,
        "can_reject": False,
        "can_rollback": False,
        "reason": "read_only_review_api",
    }
    assert preview.status_code == 200
    preview_payload = preview.json()
    assert preview_payload["delta_id"] == delta.id
    assert preview_payload["installable"] is False
    assert "## Trigger" in preview_payload["instructions"]


def test_behavior_delta_review_routes_are_read_only_and_return_404(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "agent.db"))
    client = _client(ledger)

    missing = client.get("/api/memory/deltas/missing")
    activate = client.post("/api/memory/deltas/missing/activate")
    rollback = client.post("/api/memory/deltas/missing/rollback")
    reject = client.post("/api/memory/deltas/missing/reject")

    assert missing.status_code == 404
    assert missing.json()["detail"] == "behavior_delta_not_found"
    assert activate.status_code == 405
    assert rollback.status_code == 405
    assert reject.status_code == 405


def test_full_server_exposes_behavior_delta_review_routes(tmp_path: Path) -> None:
    config = AgentConfig(
        state_path=tmp_path / "agent.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        secret_store_path=tmp_path / "secrets.json",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        mcp_config_path=tmp_path / "mcp.json",
        channel_config_path=tmp_path / "channels.json",
    )
    ledger = BehaviorDeltaLedger(AgentStateStore(config.state_path))
    delta = _delta("delta_full_server", kind=BehaviorDeltaKind.PROCEDURE, status=BehaviorDeltaStatus.STAGED)
    ledger.record_delta(delta)

    client = TestClient(create_app(config))
    response = client.get("/api/memory/deltas", params={"since": "all"})

    assert response.status_code == 200
    assert response.json()["deltas"][0]["delta_id"] == delta.id


def _client(ledger: BehaviorDeltaLedger) -> TestClient:
    app = FastAPI()
    register_behavior_delta_routes(app, http_exception=HTTPException, ledger=ledger)
    return TestClient(app)


def _delta(delta_id: str, *, kind: BehaviorDeltaKind, status: BehaviorDeltaStatus) -> BehaviorDelta:
    return BehaviorDelta(
        id=delta_id,
        title="Safer validation retry skill",
        kind=kind,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.LOW,
        status=status,
        trigger=TriggerSpec(
            query_patterns=("validation", "retry"),
            task_types=("repair",),
            tool_names=("pytest",),
            memory_layers=(MemoryLayer.PROCEDURAL,),
            semantic_hint="Validation retry workflow should change strategy before repeating commands.",
        ),
        behavior_change="When validation fails, inspect the prior command and change strategy before retrying the same command.",
        evidence_refs=(EvidenceRef(source="test", locator=delta_id, quote="changed strategy before retry"),),
        validation_plan=ValidationPlan(
            required_checks=("pytest",),
            replay_scenarios=("validation_retry_strategy",),
            min_validation_score=0.8,
            min_repeat_count=1,
        ),
    )
