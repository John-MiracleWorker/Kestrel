from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from nested_memvid_agent.channels import ChannelManager
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.routine_limits import (
    MAX_ROUTINE_INTERVAL_SECONDS,
    MAX_ROUTINE_MISFIRE_GRACE_SECONDS,
)
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.server import create_app
from nested_memvid_agent.state_store import AgentStateStore

_ASYNC_RUN_TIMEOUT_SECONDS = 30.0


def test_server_shutdown_always_closes_dependencies_before_reporting_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def incomplete_run_shutdown(
        _manager: RunManager,
        *,
        timeout_seconds: float,
    ) -> bool:
        events.append(f"runs:{timeout_seconds}")
        return False

    def failing_channel_close(_manager: ChannelManager) -> None:
        events.append("channels")
        raise RuntimeError("channel cleanup probe")

    def mcp_shutdown(_manager: MCPManager) -> bool:
        events.append("mcp")
        return True

    monkeypatch.setattr(RunManager, "shutdown", incomplete_run_shutdown)
    monkeypatch.setattr(ChannelManager, "close", failing_channel_close)
    monkeypatch.setattr(MCPManager, "shutdown", mcp_shutdown)

    with pytest.raises(RuntimeError, match="runtime_shutdown_incomplete"):
        with TestClient(create_app(_config(tmp_path, require_api_auth=False))) as client:
            assert client.get("/api/health").status_code == 200

    assert events == ["runs:5.0", "runs:1.0", "channels", "mcp"]


def test_server_shutdown_reports_unverified_mcp_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_attempts = 0

    def incomplete_mcp_shutdown(_manager: MCPManager) -> bool:
        nonlocal shutdown_attempts
        shutdown_attempts += 1
        return False

    monkeypatch.setattr(MCPManager, "shutdown", incomplete_mcp_shutdown)

    with pytest.raises(RuntimeError, match="runtime_shutdown_incomplete"):
        with TestClient(create_app(_config(tmp_path, require_api_auth=False))) as client:
            assert client.get("/api/health").status_code == 200

    assert shutdown_attempts >= 2


def test_routine_read_routes_map_invalid_identifiers_to_client_errors(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_config(tmp_path, require_api_auth=False))) as client:
        detail = client.get("/api/routines/not!valid")
        history = client.get("/api/routines/not!valid/history")

    assert detail.status_code == 400
    assert history.status_code == 400
    assert detail.json()["detail"].startswith("routine_id may contain only")


def test_routine_api_requires_authenticated_owner_and_revision_cas(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "routine-api-token-4b30"
    monkeypatch.setenv("KESTREL_ROUTINE_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    config = _config(tmp_path, require_api_auth=True)
    start = datetime.now(UTC) + timedelta(hours=1)

    with TestClient(create_app(config)) as client:
        request = {
            "routine_id": "morning-review",
            "name": "Morning review",
            "prompt": "Review the pending local work",
            "schedule_kind": "once",
            "start_at": start.isoformat(),
        }
        assert client.post("/api/routines", json=request).status_code == 401

        created_response = client.post(
            "/api/routines",
            headers=headers,
            json=request,
        )
        assert created_response.status_code == 200
        created = created_response.json()
        assert created["enabled"] is False
        assert created["revision"] == 1

        null_name = client.put(
            "/api/routines/morning-review",
            headers=headers,
            json={"expected_revision": 1, "name": None},
        )
        assert null_name.status_code == 400

        stale = client.put(
            "/api/routines/morning-review",
            headers=headers,
            json={"expected_revision": 99, "prompt": "stale update"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["current"]["revision"] == 1

        updated = client.put(
            "/api/routines/morning-review",
            headers=headers,
            json={"expected_revision": 1, "prompt": "Review only local pending work"},
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2

        enabled = client.put(
            "/api/routines/morning-review/enabled",
            headers=headers,
            json={"expected_revision": 2, "enabled": True},
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.json()["revision"] == 3

        deleted = client.delete(
            "/api/routines/morning-review?expected_revision=3",
            headers=headers,
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted_at"] is not None

        recreated = client.post(
            "/api/routines",
            headers=headers,
            json=request,
        )
        assert recreated.status_code == 409


def test_routine_api_rejects_coercible_and_unbounded_integer_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "routine-strict-input-token-b541"
    monkeypatch.setenv("KESTREL_ROUTINE_TEST_TOKEN", token)
    headers = {"X-Kestrel-API-Key": token}
    config = _config(tmp_path, require_api_auth=True)
    create_base = {
        "name": "Strict routine",
        "prompt": "Reject coerced owner input",
        "schedule_kind": "interval",
        "start_at": "2030-01-01T00:00:00+00:00",
        "interval_seconds": 60,
    }
    requests = [
        (
            "put",
            "/api/routines/missing",
            {"expected_revision": True, "name": "invalid"},
        ),
        (
            "put",
            "/api/routines/missing",
            {"expected_revision": "1", "name": "invalid"},
        ),
        (
            "put",
            "/api/routines/missing/enabled",
            {"expected_revision": 1, "enabled": "false"},
        ),
        (
            "put",
            "/api/routines/missing/enabled",
            {"expected_revision": 1, "enabled": 1},
        ),
        (
            "post",
            "/api/routines",
            {**create_base, "interval_seconds": "60"},
        ),
        (
            "post",
            "/api/routines",
            {**create_base, "interval_seconds": True},
        ),
        (
            "post",
            "/api/routines",
            {**create_base, "interval_seconds": 59},
        ),
        (
            "post",
            "/api/routines",
            {
                **create_base,
                "interval_seconds": MAX_ROUTINE_INTERVAL_SECONDS + 1,
            },
        ),
        (
            "post",
            "/api/routines",
            {**create_base, "misfire_grace_seconds": "60"},
        ),
        (
            "post",
            "/api/routines",
            {**create_base, "misfire_grace_seconds": -1},
        ),
        (
            "put",
            "/api/routines/missing",
            {"expected_revision": 1, "misfire_grace_seconds": False},
        ),
        (
            "put",
            "/api/routines/missing",
            {
                "expected_revision": 1,
                "misfire_grace_seconds": MAX_ROUTINE_MISFIRE_GRACE_SECONDS + 1,
            },
        ),
    ]

    with TestClient(create_app(config)) as client:
        for method, path, payload in requests:
            response = client.request(method, path, headers=headers, json=payload)
            assert response.status_code == 422, (method, path, payload, response.text)


def test_routine_api_dispatches_internal_run_and_reports_history(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "routine-dispatch-token-07e4"
    monkeypatch.setenv("KESTREL_ROUTINE_TEST_TOKEN", token)  # type: ignore[attr-defined]
    headers = {"X-Kestrel-API-Key": token}
    config = _config(
        tmp_path,
        require_api_auth=True,
        enable_proactive_routines=True,
    )
    due = datetime.now(UTC) - timedelta(seconds=1)

    with TestClient(create_app(config)) as client:
        ready = client.get("/api/health/ready", headers=headers)
        assert ready.status_code == 200
        assert ready.json()["proactive_routines"]["status"] == "healthy"

        created = client.post(
            "/api/routines",
            headers=headers,
            json={
                "routine_id": "durable-once",
                "name": "Durable once",
                "prompt": "Give a deterministic mock response",
                "schedule_kind": "once",
                "start_at": due.isoformat(),
                "misfire_grace_seconds": 300,
            },
        ).json()
        enabled = client.put(
            "/api/routines/durable-once/enabled",
            headers=headers,
            json={"expected_revision": created["revision"], "enabled": True},
        ).json()

        tick = client.post(
            "/api/routines/actions/tick",
            headers=headers,
        )
        assert tick.status_code == 200
        tick_payload = tick.json()
        assert tick_payload["claimed"] == 1
        assert len(tick_payload["dispatches"]) == 1
        run_id = tick_payload["dispatches"][0]["run_id"]

        run = _wait_for_api_run(client, run_id, headers)
        assert run["turn_origin"] == "scheduled_routine"
        assert run["transcript_scope"] == "internal"
        assert run["turn_source"] is None

        reconciled = client.post(
            "/api/routines/actions/tick",
            headers=headers,
        )
        assert reconciled.status_code == 200

        history = client.get(
            "/api/routines/durable-once/history",
            headers=headers,
        )
        assert history.status_code == 200
        occurrences = history.json()
        assert len(occurrences) == 1
        assert occurrences[0]["routine_revision"] == enabled["revision"]
        assert occurrences[0]["run_id"] == run_id
        assert occurrences[0]["status"] == "completed"

        future = client.post(
            "/api/routines",
            headers=headers,
            json={
                "routine_id": "future-once",
                "name": "Future once",
                "prompt": "Do not consume this schedule early",
                "schedule_kind": "once",
                "start_at": "2099-01-01T00:00:00+00:00",
            },
        ).json()
        client.put(
            "/api/routines/future-once/enabled",
            headers=headers,
            json={"expected_revision": future["revision"], "enabled": True},
        )
        no_time_travel = client.post(
            "/api/routines/actions/tick",
            headers=headers,
            json={"now": "2099-01-02T00:00:00+00:00"},
        )
        assert no_time_travel.status_code == 200
        assert no_time_travel.json()["claimed"] == 0
        assert (
            client.get(
                "/api/routines/future-once/history",
                headers=headers,
            ).json()
            == []
        )

    persisted_run = AgentStateStore(config.state_path).get_run(run_id)
    provenance = persisted_run.config_snapshot["routine_provenance"]
    assert provenance["routine_id"] == "durable-once"
    assert provenance["routine_revision"] == enabled["revision"]
    assert provenance["occurrence_id"] == occurrences[0]["occurrence_id"]


def test_routine_run_now_requires_owner_gate_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "routine-run-now-token-a28f"
    monkeypatch.setenv("KESTREL_ROUTINE_TEST_TOKEN", token)
    headers = {"X-Kestrel-API-Key": token}
    config = _config(
        tmp_path,
        require_api_auth=True,
        enable_proactive_routines=True,
    )
    key = "routine-api-run-now-key-0001"

    with TestClient(create_app(config)) as client:
        created = client.post(
            "/api/routines",
            headers=headers,
            json={
                "routine_id": "api-run-now",
                "name": "API run now",
                "prompt": "Give a deterministic mock response",
                "schedule_kind": "once",
                "start_at": "2099-01-01T00:00:00+00:00",
            },
        ).json()
        enabled = client.put(
            "/api/routines/api-run-now/enabled",
            headers=headers,
            json={"expected_revision": created["revision"], "enabled": True},
        ).json()
        path = "/api/routines/api-run-now/actions/run-now"
        body = {
            "expected_revision": enabled["revision"],
            "idempotency_key": key,
        }

        assert client.post(path, json=body).status_code == 401
        malformed = client.post(
            path,
            headers=headers,
            json={**body, "idempotency_key": "short"},
        )
        assert malformed.status_code == 422
        extra_field = client.post(
            path,
            headers=headers,
            json={**body, "workspace": "/tmp/client-controlled"},
        )
        assert extra_field.status_code == 422

        first_response = client.post(path, headers=headers, json=body)
        assert first_response.status_code == 200
        first = first_response.json()
        run_id = first["occurrence"]["run_id"]
        assert first["idempotent_replay"] is False
        assert first["occurrence"]["trigger_kind"] == "manual"
        assert key not in first_response.text
        _wait_for_api_run(client, run_id, headers)

        reconciled_response = client.post(
            "/api/routines/actions/tick",
            headers=headers,
        )
        assert reconciled_response.status_code == 200
        reconciled = reconciled_response.json()
        assert first["occurrence"]["occurrence_id"] in reconciled["reconciled"]
        history = client.get(
            "/api/routines/api-run-now/history",
            headers=headers,
        )
        assert history.status_code == 200
        assert history.json()[0]["status"] == "completed"

        replay_response = client.post(path, headers=headers, json=body)
        assert replay_response.status_code == 200
        replay = replay_response.json()
        assert replay["idempotent_replay"] is True
        assert replay["occurrence"]["run_id"] == run_id
        assert replay["occurrence"]["status"] == "completed"
        assert replay["dispatch"] is None

        reused = client.post(
            path,
            headers=headers,
            json={**body, "expected_revision": enabled["revision"] + 1},
        )
        assert reused.status_code == 409
        assert reused.json()["detail"]["error"] == "routine_idempotency_key_reused"

        routine = client.get("/api/routines/api-run-now", headers=headers).json()
        assert routine["next_run_at"] == "2099-01-01T00:00:00+00:00"


def test_routine_run_now_fails_closed_when_proactive_dispatch_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "routine-run-now-disabled-token-f27e"
    monkeypatch.setenv("KESTREL_ROUTINE_TEST_TOKEN", token)
    headers = {"X-Kestrel-API-Key": token}
    config = _config(tmp_path, require_api_auth=True)

    with TestClient(create_app(config)) as client:
        created = client.post(
            "/api/routines",
            headers=headers,
            json={
                "routine_id": "disabled-run-now",
                "name": "Disabled dispatch",
                "prompt": "Do not dispatch",
                "schedule_kind": "once",
                "start_at": "2099-01-01T00:00:00+00:00",
            },
        ).json()
        enabled = client.put(
            "/api/routines/disabled-run-now/enabled",
            headers=headers,
            json={"expected_revision": created["revision"], "enabled": True},
        ).json()
        response = client.post(
            "/api/routines/disabled-run-now/actions/run-now",
            headers=headers,
            json={
                "expected_revision": enabled["revision"],
                "idempotency_key": "disabled-dispatch-key-0001",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "proactive_routines_disabled"
    assert AgentStateStore(config.state_path).list_routine_occurrences("disabled-run-now") == []


def test_routine_mutation_is_closed_when_api_auth_is_not_configured(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, require_api_auth=False)
    with TestClient(create_app(config)) as client:
        response = client.post(
            "/api/routines",
            json={
                "name": "Blocked",
                "prompt": "This must remain local",
                "schedule_kind": "once",
                "start_at": datetime.now(UTC).isoformat(),
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "routine_mutation_requires_api_auth"


def test_routine_api_rejects_registered_raw_secret_without_echo(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    token = "routine-secret-auth-3b9d"
    raw_secret = "routine-definition-secret-cc475d"  # gitleaks:allow -- synthetic fixture
    register_secret_value(raw_secret)
    monkeypatch.setenv("KESTREL_ROUTINE_TEST_TOKEN", token)  # type: ignore[attr-defined]
    config = _config(tmp_path, require_api_auth=True)

    with TestClient(create_app(config)) as client:
        response = client.post(
            "/api/routines",
            headers={"X-Kestrel-API-Key": token},
            json={
                "name": "Unsafe",
                "prompt": f"Send {raw_secret}",
                "schedule_kind": "once",
                "start_at": datetime.now(UTC).isoformat(),
            },
        )

    assert response.status_code == 400
    assert "raw secrets" in response.text
    assert raw_secret not in response.text

    with TestClient(create_app(_config(tmp_path / "time", require_api_auth=True))) as client:
        malformed_time = client.post(
            "/api/routines",
            headers={"X-Kestrel-API-Key": token},
            json={
                "name": "Unsafe time",
                "prompt": "Keep parser errors redacted",
                "schedule_kind": "once",
                "start_at": raw_secret,
            },
        )

    assert malformed_time.status_code == 400
    assert raw_secret not in malformed_time.text


def _config(
    tmp_path: Path,
    *,
    require_api_auth: bool,
    enable_proactive_routines: bool = False,
) -> AgentConfig:
    return AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state" / "agent.db",
        secret_store_path=tmp_path / "secrets" / "vault.json",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        mcp_config_path=tmp_path / "config" / "mcp.json",
        channel_config_path=tmp_path / "config" / "channels.json",
        workspace=tmp_path,
        stream=False,
        require_api_auth=require_api_auth,
        api_auth_token_env="KESTREL_ROUTINE_TEST_TOKEN",
        enable_proactive_routines=enable_proactive_routines,
        routine_poll_interval_seconds=3600,
    )


def _wait_for_api_run(
    client: TestClient,
    run_id: str,
    headers: dict[str, str],
) -> dict[str, object]:
    deadline = monotonic() + _ASYNC_RUN_TIMEOUT_SECONDS
    last_status: object = "not_observed"
    while monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}", headers=headers)
        assert response.status_code == 200
        run = response.json()
        last_status = run.get("status", "missing")
        if run["status"] in {"completed", "failed", "cancelled"}:
            assert run["status"] == "completed", run.get("error")
            return run
        sleep(0.02)
    raise AssertionError(
        f"run {run_id} did not finish within {_ASYNC_RUN_TIMEOUT_SECONDS:.0f}s "
        f"(last_status={last_status!r})"
    )
