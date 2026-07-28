from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nested_memvid_agent.engineering.graph_amendments import GraphAmendmentService
from nested_memvid_agent.engineering.outcomes import OutcomeAnalyticsService
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from nested_memvid_agent.server_engineering_routes import register_engineering_routes
from nested_memvid_agent.state_store import AgentStateStore


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    def publish(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.items.append((run_id, event_type, payload))


class _Registry:
    def all_specs(self) -> list[Any]:
        return []


def _app(
    tmp_path: Path,
    *,
    require_api_auth: bool = True,
) -> tuple[FastAPI, AgentStateStore, Any]:
    state = AgentStateStore(tmp_path / "state.sqlite3")
    repository = tmp_path / "repository"
    repository.mkdir()
    state.create_project(
        project_id="project_engineering",
        display_name="Engineering",
        repository_path=repository,
        active_capability_keys=(),
    )
    state.create_run(
        run_id="run_engineering",
        message="Repair",
        session_id="session",
        workspace=str(repository),
        provider="mock",
        model="mock",
        project_id="project_engineering",
    )
    state.create_task_node(
        task_id="root",
        run_id="run_engineering",
        title="Repair",
        goal="Repair safely.",
        profile="planner",
        status="running",
        approved=True,
        plan={"decomposition": "initial"},
        acceptance_criteria=("Repair is validated.",),
    )
    state.create_task_node(
        task_id="queued",
        run_id="run_engineering",
        parent_id="root",
        title="Queued task",
        goal="Perform queued work.",
        status="queued",
        acceptance_criteria=("Work is complete.",),
    )
    events = _Events()
    outcomes = OutcomeAnalyticsService(state)
    invocations: list[dict[str, Any]] = []

    def start_benchmark_replay(**kwargs: Any) -> dict[str, Any]:
        return outcomes.link_replay(
            replay_id=str(kwargs["replay_id"]),
            case_id=str(kwargs["case_id"]),
            run_id=str(kwargs.get("existing_run_id") or "run_engineering"),
            route_policy_id=kwargs.get("route_policy_id"),
            context_strategy=str(kwargs["context_strategy"]),
            baseline=str(kwargs["baseline"]),
            actor=str(kwargs["actor"]),
        ).to_payload()

    runs = SimpleNamespace(
        graph_amendments=GraphAmendmentService(state),
        outcomes=outcomes,
        start_benchmark_replay=start_benchmark_replay,
        events=events,
        build_registry=lambda: _Registry(),
        capabilities=SimpleNamespace(
            tool_decision=lambda _spec: SimpleNamespace(effective_enabled=True)
        ),
        run_scheduler_until_idle=lambda _run_id: {"stop_reason": "idle"},
        invocations=invocations,
        invoke_tool=lambda **kwargs: (
            invocations.append(kwargs)
            or ToolExecution(
                call=ToolCall(
                    name=str(kwargs["tool_name"]),
                    arguments=dict(kwargs["arguments"]),
                    id="browser_api_call",
                ),
                success=False,
                content="Exact-call approval is required.",
                error="approval_required",
            )
        ),
    )
    app = FastAPI()
    register_engineering_routes(
        app,
        active_config=SimpleNamespace(require_api_auth=require_api_auth),
        state=state,
        runs=runs,
        http_exception=HTTPException,
    )
    return app, state, runs


def test_browser_validation_api_enters_the_exact_call_tool_path(
    tmp_path: Path,
) -> None:
    app, _state, runs = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/runs/run_engineering/browser-validations",
            json={
                "task_id": "root",
                "candidate_id": None,
                "expected_candidate_digest": "a" * 64,
                "image": "playwright@sha256:" + "b" * 64,
                "start_command": ["python", "-m", "http.server", "4173"],
                "target_url": "http://127.0.0.1:4173/",
                "assertions": [],
                "interactions": [],
                "allowed_domains": [],
                "network_fixtures": {},
                "timeout_seconds": 30.0,
            },
        )

    assert response.status_code == 200
    assert response.json()["error"] == "approval_required"
    assert runs.invocations == [
        {
            "tool_name": "browser.validate",
            "arguments": {
                "task_id": "root",
                "candidate_id": None,
                "expected_candidate_digest": "a" * 64,
                "image": "playwright@sha256:" + "b" * 64,
                "start_command": ["python", "-m", "http.server", "4173"],
                "target_url": "http://127.0.0.1:4173/",
                "assertions": [],
                "interactions": [],
                "allowed_domains": [],
                "network_fixtures": {},
                "timeout_seconds": 30.0,
            },
            "session_id": "api",
            "run_id": "run_engineering",
        }
    ]


def test_engineering_mutations_require_owner_api_configuration(
    tmp_path: Path,
) -> None:
    app, state, runs = _app(tmp_path, require_api_auth=False)
    with TestClient(app) as client:
        graph = client.post(
            "/api/runs/run_engineering/graph/amendments",
            json={
                "amendment_id": "amend_auth",
                "operation": "cancel_task",
                "payload": {"task_id": "queued", "reason": "Owner narrowed scope."},
            },
        )
        browser = client.post(
            "/api/runs/run_engineering/browser-validations",
            json={
                "task_id": "root",
                "candidate_id": None,
                "expected_candidate_digest": "a" * 64,
                "image": "playwright@sha256:" + "b" * 64,
                "start_command": ["python", "-m", "http.server", "4173"],
                "target_url": "http://127.0.0.1:4173/",
                "assertions": [],
                "interactions": [],
                "allowed_domains": [],
                "network_fixtures": {},
                "timeout_seconds": 30.0,
            },
        )
        benchmark = client.post(
            "/api/benchmarks",
            json={
                "case_id": "case_auth",
                "project_id": "project_engineering",
                "name": "Blocked benchmark",
                "task_family": "implementation",
                "risk": "low",
                "fixture": {"objective": "Do not persist this."},
                "acceptance_criteria": ["No state is changed."],
            },
        )

    for response in (graph, browser, benchmark):
        assert response.status_code == 403
        assert response.json()["detail"] == "engineering_mutation_requires_api_auth"
    assert state.get_task_node("queued").status == "queued"
    assert runs.invocations == []
    assert runs.outcomes.list_benchmarks(project_id="project_engineering") == []


def test_graph_amendment_api_lists_applies_and_digest_binds_decisions(
    tmp_path: Path,
) -> None:
    app, state, runs = _app(tmp_path)
    with TestClient(app) as client:
        cancelled = client.post(
            "/api/runs/run_engineering/graph/amendments",
            json={
                "amendment_id": "amend_api_cancel",
                "operation": "cancel_task",
                "payload": {"task_id": "queued", "reason": "Owner narrowed scope."},
            },
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "applied"
        assert state.get_task_node("queued").status == "cancelled"

        proposed = client.post(
            "/api/runs/run_engineering/graph/amendments",
            json={
                "amendment_id": "amend_api_add",
                "operation": "add_task",
                "payload": {
                    "task": {
                        "task_id": "review",
                        "parent_id": "root",
                        "title": "Owner review",
                        "goal": "Review the repair.",
                        "risk": "high",
                        "acceptance_criteria": ["Review evidence is recorded."],
                    },
                    "estimated_budget_delta_usd": 0.2,
                },
            },
        )
        assert proposed.status_code == 200
        proposal = proposed.json()
        assert proposal["status"] == "pending_approval"

        stale = client.post(
            "/api/runs/run_engineering/graph/amendments/amend_api_add/decision",
            json={
                "approved": True,
                "expected_base_graph_digest": "0" * 64,
            },
        )
        assert stale.status_code == 409

        blocked = state.transition_run(
            "run_engineering",
            "blocked",
            stop_reason="graph_amendment_approval_required",
        )
        assert blocked.status == "blocked"
        approved = client.post(
            "/api/runs/run_engineering/graph/amendments/amend_api_add/decision",
            json={
                "approved": True,
                "expected_base_graph_digest": proposal["base_graph_digest"],
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "applied"
        assert approved.json()["scheduler_resume"] == {"stop_reason": "idle"}
        assert state.get_run("run_engineering").status == "queued"

        listed = client.get("/api/runs/run_engineering/graph/amendments")
        assert [item["amendment_id"] for item in listed.json()["items"]] == [
            "amend_api_cancel",
            "amend_api_add",
        ]
        assert [item[1] for item in runs.events.items] == [
            "graph.amendment.applied",
            "graph.amendment.requested",
            "graph.amendment.applied",
        ]


def test_outcome_and_private_benchmark_api_exposes_missing_coverage(
    tmp_path: Path,
) -> None:
    app, _state, _runs = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/benchmarks",
            json={
                "case_id": "case_api",
                "project_id": "project_engineering",
                "name": "Repair benchmark",
                "task_family": "implementation",
                "risk": "low",
                "fixture": {"objective": "Repair the parser."},
                "acceptance_criteria": ["The parser test passes."],
            },
        )
        assert created.status_code == 201
        assert created.json()["case_digest"]

        replay = client.post(
            "/api/benchmarks/case_api/replays",
            json={
                "replay_id": "replay_api",
                "launch": False,
                "existing_run_id": "run_engineering",
                "route_policy_id": "balanced",
                "context_strategy": "repository_index",
                "baseline": "static_policy",
            },
        )
        assert replay.status_code == 201
        assert replay.json()["run_id"] == "run_engineering"

        dashboard = client.get(
            "/api/outcomes",
            params={"project_id": "project_engineering"},
        )
        assert dashboard.status_code == 200
        assert dashboard.json()["summary"]["actual_cost_usd"]["missing"] is True

        exported = client.get(
            "/api/outcomes/export",
            params={"project_id": "project_engineering"},
        )
        assert exported.status_code == 200
        assert exported.json()["redacted"] is True

        detail = client.get("/api/benchmarks/case_api")
        assert detail.status_code == 200
        assert detail.json()["replays"][0]["replay_id"] == "replay_api"
