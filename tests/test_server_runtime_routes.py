from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.runtime_settings import (
    RuntimeSettings,
    RuntimeSettingsStore,
    apply_runtime_settings,
)
from nested_memvid_agent.server_runtime_routes import register_runtime_routes


class _FakeState:
    def schema_version(self) -> int:
        return 10


def test_runtime_routes_report_health_and_redacted_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KESTREL_TEST_API_KEY", "raw-secret-value")
    config = AgentConfig(
        name="Test Kestrel",
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        api_key_env="KESTREL_TEST_API_KEY",
    )
    app = FastAPI()
    register_runtime_routes(app, active_config=config, state=_FakeState())
    client = TestClient(app)

    health = client.get("/api/health")
    runtime = client.get("/api/runtime/config")

    assert health.status_code == 200
    assert health.json() == {"ok": True, "name": "Test Kestrel"}
    assert runtime.status_code == 200
    payload = runtime.json()
    assert payload["schema_version"] == 10
    assert payload["provider"]["api_key_env"] == "KESTREL_TEST_API_KEY"
    assert payload["provider"]["api_key_configured"] is True
    assert "raw-secret-value" not in runtime.text


def test_runtime_settings_save_persists_and_updates_runtime_config(tmp_path) -> None:
    config = AgentConfig(
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        mcp_config_path=tmp_path / "config" / "mcp_servers.json",
    )
    store = RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")
    active_config = config

    def get_config() -> AgentConfig:
        return active_config

    def update_config(next_config: AgentConfig) -> None:
        nonlocal active_config
        active_config = next_config

    app = FastAPI()
    register_runtime_routes(
        app,
        active_config=get_config,
        state=_FakeState(),
        settings_store=store,
        on_config_update=update_config,
        http_exception=HTTPException,
    )
    client = TestClient(app, raise_server_exceptions=False)

    saved = client.put(
        "/api/runtime/settings",
        json={
            "provider": "codex-cli",
            "model": "gpt-5.4",
            "backend": "memvid",
            "memory_dir": str(tmp_path / "mv2"),
            "workspace": str(tmp_path / "workspace"),
            "stream": True,
            "require_api_auth": False,
            "autonomy_mode": "manual",
        },
    )

    assert saved.status_code == 200
    assert store.exists()
    payload = saved.json()
    assert payload["settings"]["persisted"] is True
    assert payload["settings"]["provider"] == "codex-cli"
    assert payload["settings"]["autonomy_mode"] == "manual"
    assert active_config.provider == "codex-cli"
    assert active_config.model == "gpt-5.4"
    assert active_config.backend == "memvid"
    assert active_config.stream is True

    runtime = client.get("/api/runtime/config")
    assert runtime.status_code == 200
    runtime_payload = runtime.json()
    assert runtime_payload["provider"]["name"] == "codex-cli"
    assert runtime_payload["provider"]["model"] == "gpt-5.4"
    assert runtime_payload["provider"]["stream"] is True
    assert runtime_payload["paths"]["workspace"] == str(tmp_path / "workspace")
    assert runtime_payload["settings"]["runtime"]["persisted"] is True


def test_runtime_settings_rejects_api_auth_without_configured_token(tmp_path) -> None:
    config = AgentConfig(api_auth_token_env="KESTREL_MISSING_TOKEN")
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    app = FastAPI()
    register_runtime_routes(
        app,
        active_config=config,
        state=_FakeState(),
        settings_store=store,
        http_exception=HTTPException,
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.put("/api/runtime/settings", json={"require_api_auth": True})

    assert response.status_code == 400
    assert "api_auth_token_unconfigured:KESTREL_MISSING_TOKEN" in response.text
    assert not store.exists()


def test_runtime_settings_store_loads_saved_config_on_restart(tmp_path) -> None:
    config = AgentConfig(provider="mock", model="mock", memory_dir=tmp_path / "memory")
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    saved = store.save(
        RuntimeSettings(
            provider="codex-cli",
            model="gpt-5.4",
            backend="memvid",
            memory_dir=str(tmp_path / "mv2"),
            workspace=str(tmp_path),
            stream=True,
            require_api_auth=False,
            autonomy_mode="manual",
        )
    )

    loaded = store.load(config)
    restarted_config = apply_runtime_settings(config, loaded)

    assert loaded == saved
    assert restarted_config.provider == "codex-cli"
    assert restarted_config.model == "gpt-5.4"
    assert restarted_config.backend == "memvid"
    assert restarted_config.memory_dir == tmp_path / "mv2"
    assert restarted_config.stream is True
