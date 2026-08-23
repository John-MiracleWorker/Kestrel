from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.mission_control import _payload_digest
from nested_memvid_agent.mission_proof import build_mission_proof
from nested_memvid_agent.server import create_app
from nested_memvid_agent.state_store import RunRecord

DEFAULT_TASKS: list[dict] = [
    {"task_id": "t1", "title": "Task 1", "risk": "low", "profile": "planner"}
]


def _binding(
    *,
    objective: str = "Explain this repository.",
    project_revision: int = 1,
    plan: list[dict] | None = None,
    preflight_digest: str = "a" * 64,
    override: dict | None = None,
    reseal: bool = False,
) -> dict:
    tasks = plan if plan is not None else DEFAULT_TASKS
    payload = {
        "schema": "kestrel.mission_launch_binding.v1",
        "project_id": "p1",
        "project_revision": project_revision,
        "objective_digest": hashlib.sha256(objective.strip().encode("utf-8")).hexdigest(),
        "template_id": "explain_repository",
        "config_digest": "b" * 64,
        "routing_enabled": False,
        "routing_mode": "off",
        "policy_id": "balanced",
        "policy_revision": 1,
        "inventory_digest": "c" * 64,
        "preflight_digest": preflight_digest,
        "plan_digest": _payload_digest({"tasks": tasks}),
    }
    payload["binding_digest"] = _payload_digest(payload)
    if override:
        payload.update(override)
        if reseal:
            payload["binding_digest"] = _payload_digest(
                {key: value for key, value in payload.items() if key != "binding_digest"}
            )
    return payload


def _preflight(
    *,
    objective: str = "Explain this repository.",
    tasks: list[dict] | None = None,
) -> dict:
    return {
        "schema": "kestrel.mission_preflight.v1",
        "project_id": "p1",
        "project_revision": 1,
        "objective": objective,
        "template_id": "explain_repository",
        "working_tree": {"state": "clean", "branch": "main", "summary": "clean"},
        "index": {"freshness": "current", "detail": "current", "digest": "d" * 64},
        "provider": {"status": "pass", "detail": "ok", "route_policy": "balanced"},
        "capability_ceiling": ["tool:file.read"],
        "tasks": tasks if tasks is not None else DEFAULT_TASKS,
        "can_start": True,
    }


def _run(
    *,
    objective: str = "Explain this repository.",
    binding: dict | None = None,
    preflight: dict | None = None,
    workspace: str = "/repo/p1",
    project_id: str = "p1",
) -> RunRecord:
    return RunRecord(
        run_id="run_abc",
        status="queued",
        message=objective,
        session_id="session",
        workspace=workspace,
        provider="mock",
        model="mock",
        project_id=project_id,
        mission_binding=binding,
        mission_preflight=preflight,
    )


class _FakeState:
    def __init__(
        self,
        *,
        tasks: list | None = None,
        events: list | None = None,
        approvals: list | None = None,
        project: dict | None = None,
    ) -> None:
        self.tasks = tasks or []
        self.events = events or []
        self.approvals = approvals or []
        self.project = project or {
            "project_id": "p1",
            "revision": 1,
            "repository_path": "/repo/p1",
            "allowed_paths": ["."],
        }

    def get_project(self, project_id: str) -> dict:
        if project_id != self.project["project_id"]:
            raise KeyError(project_id)
        return self.project

    def list_task_nodes(self, run_id: str) -> list:
        return self.tasks

    def list_run_steps(self, run_id: str) -> list:
        return self.events

    def list_approvals(self, *, run_id: str | None = None, expire: bool = True) -> list:
        if run_id is None:
            return self.approvals
        return [approval for approval in self.approvals if approval.get("run_id") == run_id]


class _FakeLedger:
    def __init__(self, observations: list | None = None) -> None:
        self.observations = observations or []

    def list_shadow_observations(self, *, run_id: str, task_id: str | None = None, role: str | None = None) -> list:
        return self.observations


def _observation(*, authority: str = "deterministic_static", role: str = "executor") -> SimpleNamespace:
    return SimpleNamespace(
        to_payload=lambda: {
            "observation_id": "obs_1",
            "role": role,
            "actual_authority": authority,
            "verdict": "supported",
        }
    )


def _mission_task(**overrides: object) -> SimpleNamespace:
    fields = {
        "task_id": "t1",
        "title": "Task 1",
        "profile": "planner",
        "status": "queued",
        "risk": "low",
        "approved": False,
        "plan": {"source": "mission_control"},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _proof(
    *,
    run: RunRecord | None = None,
    state: _FakeState | None = None,
    ledger: _FakeLedger | None = None,
    runs_dir: str | Path = "/nonexistent/runs",
) -> dict:
    effective_run = run if run is not None else _run(
        binding=_binding(),
        preflight=_preflight(),
    )
    effective_state = state if state is not None else _FakeState(tasks=[_mission_task()])
    effective_ledger = ledger if ledger is not None else _FakeLedger()
    return build_mission_proof(
        state=effective_state,
        run=effective_run,
        routing_ledger=effective_ledger,
        runs_dir=Path(runs_dir),
    )


def test_reducer_reports_present_evidence_without_ui_inference() -> None:
    proof = _proof()
    assert proof["schema"] == "kestrel.mission_proof.v1"
    assert proof["run_id"] == "run_abc"
    sections = proof["evidence"]
    assert sections["binding"]["status"] == "present"
    assert sections["contract"]["status"] == "present"
    assert sections["roles"]["status"] == "present"
    assert sections["isolation"]["status"] == "present"
    assert sections["risks"]["status"] == "present"
    # Explicit missing evidence is reported, never invented as present.
    assert sections["routing"]["status"] == "missing"
    assert sections["approval"]["status"] == "missing"
    assert sections["change"]["status"] == "missing"
    assert sections["validation"]["status"] == "missing"
    assert sections["review"]["status"] == "missing"
    assert sections["shipping"]["status"] == "missing"
    assert sections["capsule"]["status"] == "missing"
    assert sections["learning"]["status"] == "missing"
    summary = proof["summary"]
    assert "routing" in summary["missing"]
    assert "capsule" in summary["missing"]
    assert summary["counts"]["present"] == 5
    assert summary["counts"]["missing"] == 8


def test_reducer_reports_stale_binding_when_project_moved_on() -> None:
    run = _run(binding=_binding(project_revision=1), preflight=_preflight())
    state = _FakeState(
        tasks=[_mission_task()],
        project={"project_id": "p1", "revision": 2, "repository_path": "/repo/p1", "allowed_paths": ["."]},
    )
    proof = _proof(run=run, state=state)
    assert proof["evidence"]["binding"]["status"] == "stale"
    assert proof["evidence"]["binding"]["evidence"]["admitted_project_revision"] == 1
    assert proof["evidence"]["binding"]["evidence"]["current_project_revision"] == 2
    assert "binding" in proof["summary"]["stale"]


def test_reducer_reports_mismatched_binding_digest_as_substitution() -> None:
    binding = _binding()
    binding["binding_digest"] = "f" * 64  # tamper without resealing
    run = _run(binding=binding, preflight=_preflight())
    proof = _proof(run=run)
    section = proof["evidence"]["binding"]
    assert section["status"] == "mismatched"
    assert "substituted after admission" in section["detail"]
    assert "binding" in proof["summary"]["mismatched"]


def test_reducer_reports_mismatched_objective_digest() -> None:
    # Binding resealed around a wrong objective digest: binding is internally
    # consistent, but the run objective no longer matches what was admitted.
    binding = _binding(
        override={"objective_digest": "9" * 64},
        reseal=True,
    )
    run = _run(binding=binding, preflight=_preflight())
    proof = _proof(run=run)
    assert proof["evidence"]["binding"]["status"] == "present"
    assert proof["evidence"]["contract"]["status"] == "mismatched"
    assert "contract" in proof["summary"]["mismatched"]


def test_reducer_reports_mismatched_plan_digest() -> None:
    binding = _binding(
        override={"plan_digest": "8" * 64},
        reseal=True,
    )
    run = _run(binding=binding, preflight=_preflight())
    proof = _proof(run=run)
    assert proof["evidence"]["contract"]["status"] == "mismatched"
    assert proof["evidence"]["contract"]["evidence"]["preflight_plan_digest"] != "8" * 64


def test_reducer_reports_mismatched_routing_authority() -> None:
    run = _run(binding=_binding(), preflight=_preflight())
    ledger = _FakeLedger(
        observations=[
            _observation(authority="deterministic_static", role="planner"),
            _observation(authority="adaptive_activated", role="executor"),
        ]
    )
    proof = _proof(run=run, ledger=ledger)
    section = proof["evidence"]["routing"]
    assert section["status"] == "mismatched"
    assert section["evidence"]["contradicting_roles"] == ["executor"]
    assert "routing" in proof["summary"]["mismatched"]


def test_reducer_reports_present_routing_when_observations_consistent() -> None:
    run = _run(binding=_binding(), preflight=_preflight())
    ledger = _FakeLedger(observations=[_observation(authority="deterministic_static")])
    proof = _proof(run=run, ledger=ledger)
    assert proof["evidence"]["routing"]["status"] == "present"
    assert proof["evidence"]["routing"]["evidence"]["observation_count"] == 1


def test_reducer_reports_present_evidence_families_from_durable_sources() -> None:
    state = _FakeState(
        tasks=[_mission_task()],
        events=[
            {"type": "review.completed"},
            {"type": "patch.apply"},
            {"type": "browser.validation.completed"},
            {"type": "github.change_request_prepared"},
            {"type": "lesson.created"},
            {"type": "worker.isolated"},
        ],
        approvals=[{"run_id": "run_abc", "approval_id": "appr_1", "status": "approved"}],
    )
    runs_dir = Path("/tmp/kestrel-proof-runs")
    marker_dir = runs_dir / "run_abc"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "capsule.complete.json").write_text(
        json.dumps({"format": "kestrel-task-capsule-completion", "status": "complete", "backend": "memory", "artifacts": ["complete.mv2"], "completed_at": "2026-08-22T00:00:00Z"}),
        encoding="utf-8",
    )
    try:
        proof = _proof(run=_run(binding=_binding(), preflight=_preflight()), state=state, runs_dir=runs_dir)
        for key in ("review", "change", "validation", "shipping", "learning", "isolation", "approval", "capsule"):
            assert proof["evidence"][key]["status"] == "present", key
    finally:
        import shutil

        shutil.rmtree(runs_dir, ignore_errors=True)


def test_reducer_reports_mismatched_capsule_marker() -> None:
    runs_dir = Path("/tmp/kestrel-proof-runs-bad")
    marker_dir = runs_dir / "run_abc"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "capsule.complete.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8"
    )
    try:
        proof = _proof(run=_run(binding=_binding(), preflight=_preflight()), runs_dir=runs_dir)
        section = proof["evidence"]["capsule"]
        assert section["status"] == "mismatched"
        assert section["evidence"]["marker_status"] == "failed"
    finally:
        import shutil

        shutil.rmtree(runs_dir, ignore_errors=True)


def test_reducer_reports_mismatched_isolation_boundary() -> None:
    run = _run(
        binding=_binding(),
        preflight=_preflight(),
        workspace="/other/workspace",
    )
    proof = _proof(run=run)
    section = proof["evidence"]["isolation"]
    assert section["status"] == "mismatched"
    assert section["evidence"]["run_workspace"] == "/other/workspace"


def test_reducer_reports_missing_contract_without_binding() -> None:
    run = _run(binding=None, preflight=None)
    proof = _proof(run=run)
    assert proof["evidence"]["binding"]["status"] == "missing"
    assert proof["evidence"]["contract"]["status"] == "missing"


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        workspace=tmp_path,
        state_path=tmp_path / "agent.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        secret_store_path=tmp_path / "secrets.json",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        mcp_config_path=tmp_path / "mcp.json",
        channel_config_path=tmp_path / "channels.json",
        require_api_auth=True,
        api_auth_token_env="KESTREL_MISSION_PROOF_TEST_TOKEN",
        enable_agentic_cycle=False,
        allow_file_write=True,
        allow_shell=True,
    )


def test_mission_proof_endpoint_is_read_only_and_aggregates_evidence(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "mission-proof-token-2b9d5a77"
    monkeypatch.setenv("KESTREL_MISSION_PROOF_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("# Repository\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c",
        "user.name=Kestrel",
        "-c",
        "user.email=k@example.invalid",
        "commit",
        "-m",
        "baseline",
    )
    with TestClient(create_app(_config(tmp_path))) as client:
        capabilities = client.get("/api/capabilities", headers=headers).json()["items"]
        active = [item["key"] for item in capabilities if item["effective_enabled"]]
        created = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "proof_project",
                "display_name": "Proof project",
                "repository_path": str(repository.resolve()),
                "privacy_class": "local_required",
                "provider_policy": {"preset": "local_only"},
                "test_recipes": [{"name": "unit", "command": "pytest -q"}],
                "capability_ceiling": active,
            },
        )
        assert created.status_code == 201
        preflight = client.post(
            "/api/projects/proof_project/mission/preflight",
            headers=headers,
            json={
                "objective": "Explain this repository's architecture and entry points.",
                "template_id": "explain_repository",
            },
        ).json()
        assert preflight["can_start"] is True
        launched = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": preflight["objective"],
                "project_id": "proof_project",
                "autonomy_mode": "manual",
                "mission_plan": preflight["tasks"],
                "project_revision": preflight["project_revision"],
                "mission_template_id": preflight["template_id"],
                "mission_binding": preflight["launch_binding"],
            },
        )
        assert launched.status_code == 200
        run = launched.json()

        response = client.get(
            f"/api/runs/{run['run_id']}/mission-proof",
            headers=headers,
        )
        assert response.status_code == 200
        proof = response.json()
        assert proof["schema"] == "kestrel.mission_proof.v1"
        assert proof["run_id"] == run["run_id"]
        assert proof["binding"]["persisted"] is True
        assert proof["binding"]["preflight_persisted"] is True
        sections = proof["evidence"]
        assert sections["binding"]["status"] == "present"
        assert sections["contract"]["status"] == "present"
        assert sections["roles"]["status"] == "present"
        assert sections["isolation"]["status"] == "present"
        assert sections["risks"]["status"] == "present"
        # No shadow observations, approvals, or capsule were produced: the
        # projection must say so explicitly rather than infer presence.
        assert sections["routing"]["status"] == "missing"
        assert sections["approval"]["status"] == "missing"
        assert sections["capsule"]["status"] == "missing"
        assert proof["summary"]["counts"]["present"] >= 5

        # The projection is read-only: repeated reads are stable (only the
        # generated_at timestamp advances) and nothing was mutated by reading.
        again = client.get(
            f"/api/runs/{run['run_id']}/mission-proof",
            headers=headers,
        ).json()
        assert again["evidence"] == proof["evidence"]
        assert again["summary"] == proof["summary"]
        assert again["binding"] == proof["binding"]
        unknown = client.get("/api/runs/run_missing/mission-proof", headers=headers)
        assert unknown.status_code == 404


def test_mission_proof_reports_stale_binding_after_project_update(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "mission-proof-stale-token-5c1e0b22"
    monkeypatch.setenv("KESTREL_MISSION_PROOF_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("# Repository\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c",
        "user.name=Kestrel",
        "-c",
        "user.email=k@example.invalid",
        "commit",
        "-m",
        "baseline",
    )
    with TestClient(create_app(_config(tmp_path))) as client:
        capabilities = client.get("/api/capabilities", headers=headers).json()["items"]
        active = [item["key"] for item in capabilities if item["effective_enabled"]]
        assert client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "stale_project",
                "display_name": "Stale project",
                "repository_path": str(repository.resolve()),
                "privacy_class": "local_required",
                "provider_policy": {"preset": "local_only"},
                "capability_ceiling": active,
            },
        ).status_code == 201
        preflight = client.post(
            "/api/projects/stale_project/mission/preflight",
            headers=headers,
            json={
                "objective": "Explain the repository.",
                "template_id": "explain_repository",
            },
        ).json()
        launched = client.post(
            "/api/runs",
            headers=headers,
            json={
                "message": preflight["objective"],
                "project_id": "stale_project",
                "autonomy_mode": "manual",
                "mission_plan": preflight["tasks"],
                "project_revision": preflight["project_revision"],
                "mission_template_id": preflight["template_id"],
                "mission_binding": preflight["launch_binding"],
            },
        ).json()
        # Bump the project revision after admission (allowed; the run's
        # admitted binding must then be reported stale, never rewritten).
        updated = client.put(
            "/api/projects/stale_project",
            headers=headers,
            json={"expected_revision": 1, "display_name": "Stale project updated"},
        )
        assert updated.status_code == 200

        proof = client.get(
            f"/api/runs/{launched['run_id']}/mission-proof",
            headers=headers,
        ).json()
        binding_section = proof["evidence"]["binding"]
        assert binding_section["status"] == "stale"
        assert binding_section["evidence"]["admitted_project_revision"] == 1
        assert binding_section["evidence"]["current_project_revision"] == 2
        assert "binding" in proof["summary"]["stale"]
        # The persisted binding itself is immutable.
        assert launched["mission_binding"]["project_revision"] == 1
