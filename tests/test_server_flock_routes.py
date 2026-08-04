"""HTTP contract tests for the owner-facing Flock routes (Adaptive Flock plan, Task 17).

The routes are thin adapters: business logic stays in the preview, runner, and
activation services.  These tests pin the API security contract -- mutation
schemas forbid extras and reject raw secrets, mutations require expected
revisions, activation is owner-only, and error responses use stable codes
(409 conflicts/drift, 422 schema, 400 invalid scope/corpus/cap, 403 non-owner,
404 unknown IDs).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from nested_memvid_agent.routing.activation_service import ActivationService
from nested_memvid_agent.routing.learned_router import LearnedRouterState
from nested_memvid_agent.routing.qualification_evaluator import ScopeQualificationResult
from nested_memvid_agent.routing.qualification_ledger import QualificationLedger
from nested_memvid_agent.routing.qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    QualificationScope,
    QualificationThresholds,
)
from nested_memvid_agent.routing.qualification_preview import (
    QualificationPreviewService,
    TargetInventory,
)
from nested_memvid_agent.routing.qualification_receipt import build_terminal_receipt
from nested_memvid_agent.routing.qualification_records import (
    QualificationReceipt,
    QualificationRunDraft,
)
from nested_memvid_agent.routing.qualification_replay import ReplayResult
from nested_memvid_agent.routing.qualification_runner import QualificationRunner
from nested_memvid_agent.server_flock_routes import register_flock_routes
from nested_memvid_agent.state_store import AgentStateStore

OWNER = "owner@example.test"
AGENT_PRINCIPAL = "agent:flock-worker:v1"


# --- service-level fixtures (mirrors tests/test_flock_activation_service.py) --------


def run_scope(capabilities: tuple[str, ...] = ("repository_inspection",)) -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk="low",
        capabilities=capabilities,
        policy_id="balanced",
        policy_revision=1,
        target_ids=("target_a", "target_b"),
        target_inventory_digest="1" * 64,
        price_digest="2" * 64,
        learned_config_digest="3" * 64,
        project_authority_digest="4" * 64,
    )


def corpus_manifest() -> CorpusManifest:
    return CorpusManifest(
        schema_version=1,
        items=(
            CorpusItem(
                item_id="corpus_item_1",
                task_family="repository_inspection",
                risk="low",
                capabilities=("repository_inspection",),
                task_contract_digest="a" * 64,
                acceptance_plan_digest="b" * 64,
                evidence_kind="real_project",
            ),
        ),
    )


def run_draft(run_id: str = "qual_routes") -> QualificationRunDraft:
    scope = run_scope()
    return QualificationRunDraft(
        run_id=run_id,
        owner_principal=OWNER,
        scope=scope,
        corpus=corpus_manifest(),
        thresholds=QualificationThresholds(),
        target_snapshot={"targets": list(scope.target_ids)},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
        project_authority={"principal": OWNER},
        build={"version": "0.5.0", "git": "bd2c182"},
        max_spend=MoneyMicros.from_usd_text("50.00"),
        effective_stop_cap=MoneyMicros.from_usd_text("50.00"),
        attempt_ceiling=MoneyMicros.from_usd_text("5.00"),
    )


def qualified_scope() -> ScopeQualificationResult:
    scope = run_scope()
    return ScopeQualificationResult(
        scope_digest=scope.digest,
        state="qualified",
        static_target_id="target_a",
        selected_target_id="target_b",
        total_support=10,
        selected_target_support=5,
        confidence=0.9,
        static_utility=0.5,
        learned_utility=0.7,
        utility_delta=0.2,
        cost_coverage=0.9,
        estimated_savings_usd=0.001,
        estimated_regret_usd=None,
        guardrail_violations=0,
        evaluated_target_ids=("target_a", "target_b"),
        reasons=(),
        router_state=LearnedRouterState(config_digest="6" * 64),
        thresholds_digest=QualificationThresholds().digest,
    )


def completed_qualified_receipt(ledger: QualificationLedger) -> QualificationReceipt:
    run = ledger.create_run(run_draft())
    digests = ("c" * 64,) * 20
    replay = ReplayResult(
        repeats=20,
        completed_repeats=20,
        successes_required=20,
        projection_digests=digests,
        results=(qualified_scope(),),
        reasons=(),
    )
    payload = build_terminal_receipt(
        status="completed",
        run=run,
        terminal_reason="matrix_exhausted",
        scopes=replay.results,
        replay=replay,
    )
    ledger.finalize_run_terminal(
        run.run_id,
        expected_revision=run.revision,
        terminal_status="completed",
        terminal_reason="matrix_exhausted",
        actual_spend=run.actual_spend,
        receipt_payload=payload,
    )
    receipts = ledger.list_receipts(run.run_id)
    assert len(receipts) == 1
    return receipts[0]


def activation_body(receipt: QualificationReceipt) -> dict[str, Any]:
    payload = receipt.payload
    return {
        "receipt_id": receipt.receipt_id,
        "scope_digests": [qualified_scope().scope_digest],
        "expected_receipt_digest": str(payload["payload_digest"]),
        "expected_run_revision": int(payload["run"]["revision"]),
        "bindings": {
            "project_authority": {"principal": OWNER},
            "target_snapshot": {"targets": ["target_a", "target_b"]},
            "price_snapshot": {"source": "operator_verified"},
            "policy_payload": {"policy_id": "balanced", "revision": 1},
            "learned_payload": {"state": "disabled"},
        },
    }


# --- HTTP request bodies -------------------------------------------------------------


def corpus_item_payload() -> dict[str, Any]:
    return {
        "item_id": "corpus_item_1",
        "task_family": "repository_inspection",
        "risk": "low",
        "capabilities": ["repository_inspection"],
        "task_contract_digest": "a" * 64,
        "acceptance_plan_digest": "b" * 64,
        "evidence_kind": "real_project",
    }


def qualification_preview_request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "project_id": "project-alpha",
        "task_families": ["repository_inspection"],
        "corpus": [corpus_item_payload()],
    }
    body.update(overrides)
    return body


def qualification_create_request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scope": run_scope().to_payload(),
        "corpus": [corpus_item_payload()],
        "target_snapshot": {"targets": ["target_a", "target_b"]},
        "price_snapshot": {"source": "operator_verified"},
        "policy_payload": {"policy_id": "balanced", "revision": 1},
        "learned_payload": {"state": "disabled"},
        "project_authority": {"principal": OWNER},
        "build": {"version": "0.5.0", "git": "bd2c182"},
        "maximum_spend_usd": "50.00",
        "attempt_ceiling_usd": "5.00",
    }
    body.update(overrides)
    return body


# --- app fixture ----------------------------------------------------------------------


@dataclass
class FlockTestApp:
    client: TestClient
    state: AgentStateStore
    ledger: QualificationLedger
    runner: QualificationRunner
    service: ActivationService


def _build_app(
    state: AgentStateStore,
    *,
    owner_principal: str = OWNER,
    owner_authorized: bool = True,
) -> FlockTestApp:
    ledger = QualificationLedger(state)
    runner = QualificationRunner(state, ledger)
    service = ActivationService(ledger)
    preview = QualificationPreviewService(
        inventory=lambda: TargetInventory(profiles=(), targets=())
    )
    app = FastAPI()
    register_flock_routes(
        app,
        qualification_runner=runner,
        activation_service=service,
        preview_service=preview,
        ledger=ledger,
        http_exception=HTTPException,
        streaming_response=StreamingResponse,
        owner_principal=owner_principal,
        owner_authorized=lambda: owner_authorized,
    )
    return FlockTestApp(
        client=TestClient(app),
        state=state,
        ledger=ledger,
        runner=runner,
        service=service,
    )


@pytest.fixture
def state(tmp_path: Path) -> AgentStateStore:
    return AgentStateStore(tmp_path / "state" / "agent.db")


@pytest.fixture
def flock(state: AgentStateStore) -> FlockTestApp:
    return _build_app(state)


@pytest.fixture
def master_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "adaptive")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")


def create_run(client: TestClient, **overrides: Any) -> str:
    response = client.post("/api/flock/qualifications", json=qualification_create_request(**overrides))
    assert response.status_code == 201, response.text
    return str(response.json()["run_id"])


def current_revision(client: TestClient, run_id: str) -> int:
    response = client.get(f"/api/flock/qualifications/{run_id}")
    assert response.status_code == 200, response.text
    return int(response.json()["revision"])


def lower_cap(client: TestClient, run_id: str, usd: str) -> Any:
    return client.post(
        f"/api/flock/qualifications/{run_id}/lower-cap",
        json={
            "maximum_spend_usd": usd,
            "expected_revision": current_revision(client, run_id),
        },
    )


# --- preview ---------------------------------------------------------------------------


def test_preview_defaults_to_editable_fifty_dollar_cap(flock: FlockTestApp) -> None:
    response = flock.client.post(
        "/api/flock/qualifications/preview",
        json=qualification_preview_request(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["budget"]["maximum_spend_micros"] == 50_000_000
    assert response.json()["budget"]["maximum_spend_usd"] == "50.00"


def test_preview_honors_owner_edited_cap(flock: FlockTestApp) -> None:
    response = flock.client.post(
        "/api/flock/qualifications/preview",
        json=qualification_preview_request(maximum_spend_usd="37.25"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["budget"]["maximum_spend_micros"] == 37_250_000
    assert response.json()["budget"]["maximum_spend_usd"] == "37.25"


def test_preview_rejects_extra_fields(flock: FlockTestApp) -> None:
    response = flock.client.post(
        "/api/flock/qualifications/preview",
        json=qualification_preview_request(unexpected="nope"),
    )
    assert response.status_code == 422


def test_preview_rejects_invalid_corpus_risk(flock: FlockTestApp) -> None:
    item = corpus_item_payload()
    item["risk"] = "extreme"
    response = flock.client.post(
        "/api/flock/qualifications/preview",
        json=qualification_preview_request(corpus=[item]),
    )
    assert response.status_code == 422


def test_preview_rejects_invalid_cap_text(flock: FlockTestApp) -> None:
    response = flock.client.post(
        "/api/flock/qualifications/preview",
        json=qualification_preview_request(maximum_spend_usd="not-money"),
    )
    assert response.status_code == 422


def test_mutation_rejects_raw_secrets(flock: FlockTestApp) -> None:
    item = corpus_item_payload()
    item["item_id"] = "sk-" + "a" * 20
    response = flock.client.post(
        "/api/flock/qualifications",
        json=qualification_create_request(corpus=[item]),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "flock_raw_secret_rejected"
    assert "sk-" + "a" * 20 not in response.text


# --- run CRUD and lifecycle ------------------------------------------------------------


def test_create_get_and_list_runs(flock: FlockTestApp) -> None:
    run_id = create_run(flock.client)
    created = flock.client.get(f"/api/flock/qualifications/{run_id}")
    assert created.status_code == 200
    payload = created.json()
    assert payload["status"] == "draft"
    assert payload["owner_principal"] == OWNER
    assert payload["caps"]["max_spend_micros"] == 50_000_000
    assert payload["caps"]["max_spend_usd"] == "50.00"
    assert payload["caps"]["effective_stop_cap_usd"] == "50.00"
    listing = flock.client.get("/api/flock/qualifications")
    assert listing.status_code == 200
    assert [run["run_id"] for run in listing.json()["runs"]] == [run_id]


def test_create_rejects_stop_cap_above_max(flock: FlockTestApp) -> None:
    response = flock.client.post(
        "/api/flock/qualifications",
        json=qualification_create_request(effective_stop_cap_usd="60.00"),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "flock_cap_invalid"


def test_unknown_run_ids_return_404(flock: FlockTestApp) -> None:
    for path in (
        "/api/flock/qualifications/qual_missing",
        "/api/flock/qualifications/qual_missing/receipt",
        "/api/flock/qualifications/qual_missing/events",
    ):
        response = flock.client.get(path)
        assert response.status_code == 404, path
        assert response.json()["detail"]["code"] == "flock_qualification_not_found"


def test_running_cap_can_lower_but_not_raise(flock: FlockTestApp) -> None:
    run_id = create_run(flock.client)
    assert lower_cap(flock.client, run_id, "40.00").status_code == 200
    response = lower_cap(flock.client, run_id, "50.00")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "qualification_cap_cannot_increase"


def test_lower_cap_with_stale_revision_conflicts(flock: FlockTestApp) -> None:
    run_id = create_run(flock.client)
    assert lower_cap(flock.client, run_id, "40.00").status_code == 200
    response = flock.client.post(
        f"/api/flock/qualifications/{run_id}/lower-cap",
        json={"maximum_spend_usd": "30.00", "expected_revision": 1},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "flock_revision_conflict"
    assert detail["current_revision"] == current_revision(flock.client, run_id)


def test_start_without_execution_config_conflicts(flock: FlockTestApp) -> None:
    run_id = create_run(flock.client)
    response = flock.client.post(
        f"/api/flock/qualifications/{run_id}/start",
        json={"expected_revision": current_revision(flock.client, run_id)},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "flock_execution_unavailable"


def test_pause_and_resume_require_a_running_run(flock: FlockTestApp) -> None:
    run_id = create_run(flock.client)
    for action in ("pause", "resume"):
        response = flock.client.post(
            f"/api/flock/qualifications/{run_id}/{action}",
            json={"expected_revision": current_revision(flock.client, run_id)},
        )
        assert response.status_code == 409, action
        assert response.json()["detail"]["code"] == "flock_run_state_conflict"


def test_mutations_require_owner_authorization(state: AgentStateStore) -> None:
    unauthorized = _build_app(state, owner_authorized=False)
    run_id = create_run(flock_client := _build_app(state).client)
    del flock_client
    responses = [
        unauthorized.client.post(
            "/api/flock/qualifications", json=qualification_create_request()
        ),
        unauthorized.client.post(
            f"/api/flock/qualifications/{run_id}/cancel", json={"expected_revision": 1}
        ),
        unauthorized.client.post(
            f"/api/flock/qualifications/{run_id}/lower-cap",
            json={"maximum_spend_usd": "40.00", "expected_revision": 1},
        ),
        unauthorized.client.post(
            "/api/flock/activations",
            json={
                "receipt_id": "rcpt_x",
                "scope_digests": ["d" * 64],
                "expected_receipt_digest": "e" * 64,
                "expected_run_revision": 1,
                "bindings": {
                    "project_authority": {},
                    "target_snapshot": {},
                    "price_snapshot": {},
                    "policy_payload": {},
                    "learned_payload": {},
                },
            },
        ),
        unauthorized.client.post(
            "/api/flock/activations/grant_x/revoke", json={"expected_revision": 1}
        ),
    ]
    for response in responses:
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "flock_mutation_requires_api_auth"


# --- receipt and SSE events ------------------------------------------------------------


def test_receipt_is_404_until_the_run_is_terminal(flock: FlockTestApp) -> None:
    run_id = create_run(flock.client)
    response = flock.client.get(f"/api/flock/qualifications/{run_id}/receipt")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "flock_receipt_not_found"


def test_cancel_records_receipt_and_replayable_events(flock: FlockTestApp) -> None:
    run_id = create_run(flock.client)
    cancelled = flock.client.post(
        f"/api/flock/qualifications/{run_id}/cancel",
        json={"expected_revision": current_revision(flock.client, run_id)},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    receipt = flock.client.get(f"/api/flock/qualifications/{run_id}/receipt")
    assert receipt.status_code == 200
    assert receipt.json()["payload"]["status"] == "cancelled"

    events = flock.client.get(f"/api/flock/qualifications/{run_id}/events")
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert events.headers["cache-control"] == "no-store, no-transform"
    assert events.headers["x-accel-buffering"] == "no"
    assert "id: 1\n" in events.text
    assert "event: run_cancelled\n" in events.text

    replayed = flock.client.get(
        f"/api/flock/qualifications/{run_id}/events",
        headers={"Last-Event-ID": "1"},
    )
    assert replayed.status_code == 200
    assert "event:" not in replayed.text


def test_events_reject_invalid_cursor(flock: FlockTestApp) -> None:
    run_id = create_run(flock.client)
    for cursor in ("abc", "0", "-1"):
        response = flock.client.get(
            f"/api/flock/qualifications/{run_id}/events",
            headers={"Last-Event-ID": cursor},
        )
        assert response.status_code == 400, cursor
        assert response.json()["detail"]["code"] == "flock_event_cursor_invalid"


# --- activations ------------------------------------------------------------------------


def test_activation_preview_shows_owner_and_scopes(flock: FlockTestApp) -> None:
    receipt = completed_qualified_receipt(flock.ledger)
    response = flock.client.post(
        "/api/flock/activations/preview",
        json={
            "receipt_id": receipt.receipt_id,
            "scope_digests": [qualified_scope().scope_digest],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["owner_principal"] == OWNER
    assert payload["receipt_id"] == receipt.receipt_id
    assert payload["scopes"][0]["scope_digest"] == qualified_scope().scope_digest
    assert payload["scopes"][0]["qualified"] is True


def test_activation_preview_unknown_receipt_is_404(flock: FlockTestApp) -> None:
    response = flock.client.post(
        "/api/flock/activations/preview",
        json={"receipt_id": "rcpt_missing", "scope_digests": ["d" * 64]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "flock_activation_not_found"


def test_activation_preview_unknown_scope_conflicts(flock: FlockTestApp) -> None:
    receipt = completed_qualified_receipt(flock.ledger)
    response = flock.client.post(
        "/api/flock/activations/preview",
        json={"receipt_id": receipt.receipt_id, "scope_digests": ["d" * 64]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "scope_not_in_receipt"


def test_agent_principal_cannot_activate(state: AgentStateStore) -> None:
    owner_app = _build_app(state)
    receipt = completed_qualified_receipt(owner_app.ledger)
    client_as_agent = _build_app(state, owner_principal=AGENT_PRINCIPAL).client
    response = client_as_agent.post(
        "/api/flock/activations",
        json=activation_body(receipt),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "flock_activation_requires_owner"


def test_owner_activation_creates_and_lists_grants(
    flock: FlockTestApp,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(flock.ledger)
    response = flock.client.post("/api/flock/activations", json=activation_body(receipt))
    assert response.status_code == 201, response.text
    grants = response.json()["grants"]
    assert len(grants) == 1
    assert grants[0]["scope_digest"] == qualified_scope().scope_digest
    assert grants[0]["created_by"] == OWNER

    listing = flock.client.get("/api/flock/activations")
    assert listing.status_code == 200
    assert [grant["grant_id"] for grant in listing.json()["grants"]] == [
        grants[0]["grant_id"]
    ]


def test_activation_with_stale_revision_conflicts(flock: FlockTestApp) -> None:
    receipt = completed_qualified_receipt(flock.ledger)
    body = activation_body(receipt)
    body["expected_run_revision"] = int(body["expected_run_revision"]) + 1
    response = flock.client.post("/api/flock/activations", json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "receipt_revision_changed"


def test_evaluate_reports_grant_effectiveness(
    flock: FlockTestApp,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(flock.ledger)
    created = flock.client.post("/api/flock/activations", json=activation_body(receipt))
    assert created.status_code == 201, created.text
    grant_id = created.json()["grants"][0]["grant_id"]

    evaluation = flock.client.get(f"/api/flock/activations/{grant_id}/evaluate")
    assert evaluation.status_code == 200
    payload = evaluation.json()
    assert payload["status"] == "active"
    assert payload["effective"] is True
    assert payload["receipt_authenticates"] is True
    assert payload["reason_codes"] == []

    missing = flock.client.get("/api/flock/activations/grant_missing/evaluate")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "flock_grant_not_found"


def test_revoke_is_terminal_and_revision_checked(
    flock: FlockTestApp,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(flock.ledger)
    created = flock.client.post("/api/flock/activations", json=activation_body(receipt))
    assert created.status_code == 201, created.text
    grant_id = created.json()["grants"][0]["grant_id"]

    stale = flock.client.post(
        f"/api/flock/activations/{grant_id}/revoke",
        json={"expected_revision": 7},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "flock_revision_conflict"

    revoked = flock.client.post(
        f"/api/flock/activations/{grant_id}/revoke",
        json={"expected_revision": 1},
    )
    assert revoked.status_code == 200
    assert revoked.json()["transition_type"] == "revoked"

    evaluation = flock.client.get(f"/api/flock/activations/{grant_id}/evaluate")
    assert evaluation.status_code == 200
    payload = evaluation.json()
    assert payload["status"] == "revoked"
    assert payload["effective"] is False
    assert "grant_revoked" in payload["reason_codes"]

    again = flock.client.post(
        f"/api/flock/activations/{grant_id}/revoke",
        json={"expected_revision": 2},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "flock_grant_already_revoked"

    missing = flock.client.post(
        "/api/flock/activations/grant_missing/revoke",
        json={"expected_revision": 1},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "flock_grant_not_found"
