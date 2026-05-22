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
        "can_activate": True,
        "can_reject": True,
        "can_rollback": False,
        "requires_exact_call_approval": True,
        "authority": "mutation_gate",
    }
    assert preview.status_code == 200
    preview_payload = preview.json()
    assert preview_payload["delta_id"] == delta.id
    assert preview_payload["installable"] is False
    assert "## Trigger" in preview_payload["instructions"]


def test_behavior_delta_review_routes_return_404_for_missing_delta(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "agent.db"))
    client = _client(ledger)

    missing = client.get("/api/memory/deltas/missing")
    activate = client.post("/api/memory/deltas/missing/activate", json={"exact_call_approved": True})
    rollback = client.post("/api/memory/deltas/missing/rollback", json={"exact_call_approved": True})
    reject = client.post("/api/memory/deltas/missing/reject", json={"exact_call_approved": True})

    assert missing.status_code == 404
    assert missing.json()["detail"] == "behavior_delta_not_found"
    assert activate.status_code == 404
    assert rollback.status_code == 404
    assert reject.status_code == 404


def test_behavior_delta_review_actions_require_exact_call_approval(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "agent.db"))
    delta = _delta("delta_action_gate", kind=BehaviorDeltaKind.PROCEDURE, status=BehaviorDeltaStatus.STAGED)
    ledger.record_delta(delta)
    client = _client(ledger)

    for action in ("activate", "reject", "rollback"):
        response = client.post(f"/api/memory/deltas/{delta.id}/{action}", json={"reason": "operator review"})
        assert response.status_code == 403
        assert response.json()["detail"] == "exact_call_approval_required"
    assert ledger.get_delta(delta.id).status == BehaviorDeltaStatus.STAGED


def test_behavior_delta_review_actions_reject_and_rollback_with_audit_outcomes(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "agent.db"))
    staged = _delta("delta_reject", kind=BehaviorDeltaKind.PROCEDURE, status=BehaviorDeltaStatus.STAGED)
    active = _delta("delta_rollback", kind=BehaviorDeltaKind.PROCEDURE, status=BehaviorDeltaStatus.ACTIVE)
    ledger.record_delta(staged)
    ledger.record_delta(active)
    client = _client(ledger)

    reject = client.post(f"/api/memory/deltas/{staged.id}/reject", json={"reason": "operator rejected vague scope", "exact_call_approved": True})
    rollback = client.post(f"/api/memory/deltas/{active.id}/rollback", json={"reason": "operator saw regression", "exact_call_approved": True})

    assert reject.status_code == 200
    assert reject.json()["delta"]["status"] == "rejected"
    assert rollback.status_code == 200
    assert rollback.json()["delta"]["status"] == "rolled_back"
    outcomes = ledger.list_outcomes(active.id)
    assert outcomes[-1].outcome == "rolled_back"
    assert outcomes[-1].notes == "operator saw regression"


def test_behavior_delta_activate_uses_mutation_gate_and_records_decision(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "agent.db"))
    delta = _delta("delta_medium_activate", kind=BehaviorDeltaKind.TOOL_HEURISTIC, status=BehaviorDeltaStatus.STAGED)
    delta = BehaviorDelta(
        **{
            **delta.__dict__,
            "risk": BehaviorDeltaRisk.MEDIUM,
            "validation_plan": ValidationPlan(
                required_checks=("pytest",), replay_scenarios=("validation_retry_strategy",), min_validation_score=0.8, min_repeat_count=1
            ),
        }
    )
    ledger.record_delta(delta)
    client = _client(ledger)

    blocked = client.post(
        f"/api/memory/deltas/{delta.id}/activate",
        json={"reason": "operator review", "exact_call_approved": True, "validation_score": 0.5, "repeat_count": 1},
    )
    assert blocked.status_code == 409
    assert blocked.json()["decision"]["status"] == "staged"
    assert "validation_score_below_threshold" in blocked.json()["decision"]["blocked_by"]
    assert ledger.get_delta(delta.id).status == BehaviorDeltaStatus.STAGED

    activated = client.post(
        f"/api/memory/deltas/{delta.id}/activate",
        json={
            "reason": "operator review",
            "exact_call_approved": True,
            "validation_score": 0.95,
            "repeat_count": 1,
            "replay_passed": True,
        },
    )
    assert activated.status_code == 200
    assert activated.json()["delta"]["status"] == "active"
    assert activated.json()["decision"]["status"] == "active"


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


def test_full_server_behavior_delta_review_action_cycle_is_audited(tmp_path: Path) -> None:
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
    delta = _delta("delta_full_cycle", kind=BehaviorDeltaKind.TOOL_HEURISTIC, status=BehaviorDeltaStatus.STAGED)
    delta = BehaviorDelta(
        **{
            **delta.__dict__,
            "risk": BehaviorDeltaRisk.MEDIUM,
            "validation_plan": ValidationPlan(
                required_checks=("pytest",),
                replay_scenarios=("validation_retry_strategy",),
                min_validation_score=0.8,
                min_repeat_count=1,
            ),
        }
    )
    ledger.record_delta(delta)
    client = TestClient(create_app(config))

    listed_before = client.get("/api/memory/deltas", params={"since": "all"})
    blocked = client.post(
        f"/api/memory/deltas/{delta.id}/activate",
        json={"reason": "operator review", "exact_call_approved": True, "validation_score": 0.2, "repeat_count": 1},
    )
    activated = client.post(
        f"/api/memory/deltas/{delta.id}/activate",
        json={
            "reason": "operator replay passed",
            "exact_call_approved": True,
            "validation_score": 0.91,
            "repeat_count": 1,
            "replay_passed": True,
        },
    )
    shown_active = client.get(f"/api/memory/deltas/{delta.id}")
    rolled_back = client.post(
        f"/api/memory/deltas/{delta.id}/rollback",
        json={"reason": "operator deterministic e2e rollback", "exact_call_approved": True, "run_id": "run-e2e"},
    )
    listed_after = client.get("/api/memory/deltas", params={"since": "all"})

    assert listed_before.status_code == 200
    assert listed_before.json()["deltas"][0]["status"] == "staged"
    assert blocked.status_code == 409
    assert blocked.json()["decision"]["status"] == "staged"
    assert activated.status_code == 200
    assert activated.json()["delta"]["status"] == "active"
    assert shown_active.status_code == 200
    assert shown_active.json()["review_actions"]["can_rollback"] is True
    assert rolled_back.status_code == 200
    assert rolled_back.json()["delta"]["status"] == "rolled_back"
    assert listed_after.status_code == 200
    assert listed_after.json()["deltas"][0]["status"] == "rolled_back"
    assert listed_after.json()["deltas"][0]["outcome_counts"]["rolled_back"] == 1


def test_full_server_learning_dashboard_reports_empty_and_auth_required(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEST_AGENT_API_TOKEN", "test-token")
    config = AgentConfig(
        state_path=tmp_path / "agent.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        secret_store_path=tmp_path / "secrets.json",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        mcp_config_path=tmp_path / "mcp.json",
        channel_config_path=tmp_path / "channels.json",
        require_api_auth=True,
    )
    client = TestClient(create_app(config))

    unauthorized = client.get("/api/learning/dashboard")
    authorized = client.get("/api/learning/dashboard", headers={"X-Kestrel-API-Key": "test-token"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["headline"]["auto_activations"] == 0
    assert authorized.json()["layers"] == []


def test_full_server_learning_dashboard_reports_behavior_delta_activity(tmp_path: Path) -> None:
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
    delta = _delta("delta_dashboard", kind=BehaviorDeltaKind.PROCEDURE, status=BehaviorDeltaStatus.ACTIVE)
    ledger.record_delta(delta)
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-dashboard",
            delta_id=delta.id,
            run_id="run-dashboard",
            task_id=None,
            objective="debug",
            activated_at="2026-05-21T00:00:00+00:00",
            activation_reason="auto_activated_low_risk_threshold_met",
            compiled_section="ACTIVE PROCEDURES",
        )
    )
    client = TestClient(create_app(config))

    response = client.get("/api/learning/dashboard", params={"since": "all"})

    assert response.status_code == 200
    assert response.json()["headline"]["auto_activations"] == 1
    assert response.json()["layers"][0]["layer"] == "procedural"


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


def test_full_server_product_readiness_route_reports_product_gap(tmp_path: Path) -> None:
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
    client = TestClient(create_app(config))

    response = client.get("/api/product/readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "kestrel.product_readiness.v1"
    assert payload["headline"]["product_ready"] is False
    assert any(category["category_id"] == "production_auth_workspaces" for category in payload["categories"])
