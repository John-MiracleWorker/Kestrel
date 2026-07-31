from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.runtime_settings import RuntimeSettingsStore
from nested_memvid_agent.server_settings_routes import register_settings_routes


def _config(tmp_path: Path, **overrides: object) -> AgentConfig:
    base = AgentConfig(
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state" / "agent.db",
        log_dir=tmp_path / "logs",
        workspace=tmp_path / "workspace",
    )
    return replace(base, **overrides)


def _app(
    config: AgentConfig,
    store: RuntimeSettingsStore,
    *,
    capabilities: tuple[dict[str, object], ...] = (),
    on_config_update=None,
) -> FastAPI:
    app = FastAPI()
    register_settings_routes(
        app,
        active_config=lambda: config,
        settings_store=store,
        capabilities=lambda: capabilities,
        on_config_update=on_config_update,
        http_exception=HTTPException,
    )
    return app


def test_get_settings_returns_projection_with_required_fields(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")
    app = _app(config, store)

    with TestClient(app) as client:
        response = client.get("/api/settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "kestrel.effective_settings.v1"
    assert payload["revision"] == store.load(config).revision
    assert payload["categories"] == [
        "General",
        "Models and providers",
        "Safety and permissions",
        "Storage and memory",
        "Containment",
        "Appearance",
        "Notifications",
        "Updates",
        "Diagnostics",
        "Advanced",
    ]
    items = payload["items"]
    assert items
    by_id = {item["id"]: item for item in items}
    assert len(by_id) == len(items)
    required = {
        "id",
        "category",
        "type",
        "configured_value",
        "effective_value",
        "blockers",
        "authority_impact",
        "privacy_impact",
        "applies",
        "revision",
        "provenance",
        "undo_available",
        "restart_required",
    }
    for item in items:
        assert required <= set(item), item["id"]
        assert item["revision"] == payload["revision"]
        assert isinstance(item["blockers"], list)
    assert "allowed_values" in by_id["models.provider"]
    assert "allowed_range" in by_id["models.temperature"]


def test_get_settings_reflects_capability_blockers(tmp_path: Path) -> None:
    config = _config(tmp_path, allow_web=True)
    store = RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")
    app = _app(
        config,
        store,
        capabilities=({"key": "network", "effective_enabled": False},),
    )

    with TestClient(app) as client:
        response = client.get("/api/settings")

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}
    web_search = items["tools.web_search.enabled"]
    assert web_search["configured_value"] is True
    assert web_search["effective_value"] is False
    assert web_search["blockers"] == ["capability:network_disabled"]
    assert web_search["applies"] == "new_runs"


def test_put_setting_commits_and_returns_fresh_projection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")
    active = [config]

    app = _app(
        config,
        store,
        on_config_update=lambda candidate: active.__setitem__(0, candidate),
    )

    with TestClient(app) as client:
        initial = client.get("/api/settings").json()
        revision = initial["items_by_id"]["model"]["revision"]
        response = client.put(
            "/api/settings/model",
            json={"value": "mock-v2", "expected_revision": revision},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["setting"]["id"] == "model"
    assert payload["setting"]["configured_value"] == "mock-v2"
    assert payload["setting"]["effective_value"] == "mock-v2"
    assert payload["setting"]["revision"] != revision
    assert payload["revision"] == payload["setting"]["revision"]
    assert payload["revoked_approvals"] == 0
    assert isinstance(payload["authority_changes"], list)
    assert store.load(config).model == "mock-v2"
    assert active[0].model == "mock-v2"


def test_put_setting_requires_expected_revision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")
    app = _app(config, store)

    with TestClient(app) as client:
        response = client.put("/api/settings/model", json={"value": "mock-v2"})

    assert response.status_code == 400
    assert response.json()["detail"] == "expected_revision_is_required"
    assert store.load(config).model == "mock"


def test_put_setting_conflict_returns_current_projection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")
    app = _app(config, store)

    with TestClient(app) as client:
        first = client.get("/api/settings").json()
        revision = first["items_by_id"]["model"]["revision"]
        committed = client.put(
            "/api/settings/model",
            json={"value": "mock-v2", "expected_revision": revision},
        )
        assert committed.status_code == 200
        stale = client.put(
            "/api/settings/model",
            json={"value": "mock-v3", "expected_revision": revision},
        )

    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "setting_revision_conflict"
    current = detail["current"]
    assert current["id"] == "model"
    assert current["configured_value"] == "mock-v2"
    assert current["effective_value"] == "mock-v2"
    assert current["revision"] == committed.json()["setting"]["revision"]
    assert current["revision"] != revision
    assert store.load(config).model == "mock-v2"


def test_put_setting_rejects_unknown_setting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")
    app = _app(config, store)

    with TestClient(app) as client:
        revision = client.get("/api/settings").json()["revision"]
        response = client.put(
            "/api/settings/does.not.exist",
            json={"value": True, "expected_revision": revision},
        )

    assert response.status_code == 404


def test_put_setting_rejects_read_only_and_invalid_values(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")
    app = _app(config, store)

    with TestClient(app) as client:
        revision = client.get("/api/settings").json()["revision"]
        read_only = client.put(
            "/api/settings/server.require_api_auth",
            json={"value": True, "expected_revision": revision},
        )
        invalid = client.put(
            "/api/settings/models.temperature",
            json={"value": 9.5, "expected_revision": revision},
        )

    assert read_only.status_code == 400
    assert invalid.status_code == 400
    assert store.load(config).temperature is None


def test_put_setting_without_store_returns_503(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = FastAPI()
    register_settings_routes(
        app,
        active_config=lambda: config,
        settings_store=None,
        http_exception=HTTPException,
    )

    with TestClient(app) as client:
        listed = client.get("/api/settings")
        revision_mutation = client.put(
            "/api/settings/model",
            json={"value": "mock-v2", "expected_revision": "x"},
        )

    assert listed.status_code == 503
    assert revision_mutation.status_code == 503
