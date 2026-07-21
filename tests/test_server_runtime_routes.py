from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.runtime_settings import (
    RuntimeSettings,
    RuntimeSettingsConflict,
    RuntimeSettingsStore,
    apply_runtime_settings,
    default_runtime_settings_path,
)
from nested_memvid_agent.server import create_app
from nested_memvid_agent.server_runtime_routes import register_runtime_routes


class _FakeState:
    def schema_version(self) -> int:
        return 10


class _FakeSecretBroker:
    def __init__(self, configured: set[str] | None = None) -> None:
        self.configured = configured or set()

    def status(self, name_or_ref: str | None) -> dict[str, object]:
        return {"configured": bool(name_or_ref in self.configured), "source_env": name_or_ref}

    def resolve(self, name_or_ref: str | None) -> str | None:
        return "broker-secret" if name_or_ref in self.configured else None


def test_runtime_routes_report_health_and_redacted_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KESTREL_TEST_API_KEY", "raw-secret-value")
    config = AgentConfig(
        name="Test Kestrel",
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        api_key_env="KESTREL_TEST_API_KEY",
        enable_semantic_orchestration=True,
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
    assert payload["feature_flags"]["enable_semantic_orchestration"] is True
    assert payload["feature_flags"]["enable_proactive_routines"] is False
    assert payload["limits"]["max_routines_per_tick"] == 3
    assert "raw-secret-value" not in runtime.text


def test_runtime_provider_probe_exposes_explicit_operational_check(tmp_path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory", state_path=tmp_path / "state.db")
    app = FastAPI()
    register_runtime_routes(
        app,
        active_config=config,
        state=_FakeState(),
        http_exception=HTTPException,
        provider_probe=lambda: {"operational": True, "state": "healthy", "total_successes": 1},
    )
    client = TestClient(app)

    response = client.post("/api/runtime/provider/probe")

    assert response.status_code == 200
    assert response.json()["operational"] is True
    assert response.json()["state"] == "healthy"


def test_runtime_models_route_returns_static_and_dynamic_catalogs(tmp_path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory", state_path=tmp_path / "state.db")
    app = FastAPI()
    register_runtime_routes(app, active_config=config, state=_FakeState())
    client = TestClient(app)

    mock_catalog = client.get("/api/runtime/models?provider=mock")
    cloud_catalog = client.get("/api/runtime/models?provider=ollama-cloud")
    deepseek_catalog = client.get("/api/runtime/models?provider=deepseek")
    kimi_catalog = client.get("/api/runtime/models?provider=kimi")
    all_catalogs = client.get("/api/runtime/models")

    assert mock_catalog.status_code == 200
    assert mock_catalog.json()["models"] == ["mock"]
    assert cloud_catalog.status_code == 200
    assert cloud_catalog.json()["api_key_env"] == "OLLAMA_API_KEY"
    assert cloud_catalog.json()["models"] == ["gpt-oss:120b", "gpt-oss:20b"]
    assert deepseek_catalog.status_code == 200
    assert deepseek_catalog.json()["api_key_env"] == "DEEPSEEK_API_KEY"
    assert deepseek_catalog.json()["models"] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert kimi_catalog.status_code == 200
    assert kimi_catalog.json()["api_key_env"] == "MOONSHOT_API_KEY"
    assert kimi_catalog.json()["models"] == ["kimi-k2.6", "kimi-k2.5"]
    assert all_catalogs.status_code == 200
    providers = {item["provider"] for item in all_catalogs.json()["providers"]}
    assert "ollama-cloud" in providers
    assert "deepseek" in providers
    assert "kimi" in providers


def test_runtime_models_route_includes_local_and_grok_provider_choices(tmp_path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory", state_path=tmp_path / "state.db")
    app = FastAPI()
    register_runtime_routes(app, active_config=config, state=_FakeState())
    client = TestClient(app)

    lm_studio_catalog = client.get("/api/runtime/models?provider=lm-studio")
    grok_catalog = client.get("/api/runtime/models?provider=grok")
    all_catalogs = client.get("/api/runtime/models")

    assert lm_studio_catalog.status_code == 200
    assert lm_studio_catalog.json()["fallback_models"] == ["local-model"]
    assert lm_studio_catalog.json()["base_url_configured"] is True
    assert lm_studio_catalog.json()["api_key_env"] is None
    assert grok_catalog.status_code == 200
    assert grok_catalog.json()["models"] == ["grok-4.3", "grok-build-0.1", "grok-4.20"]
    assert grok_catalog.json()["api_key_env"] == "XAI_API_KEY"
    assert grok_catalog.json()["base_url_configured"] is True
    providers = {item["provider"] for item in all_catalogs.json()["providers"]}
    assert {"lm-studio", "ollama", "ollama-cloud", "openai", "anthropic", "grok", "gemini"} <= providers


def test_runtime_routes_use_broker_status_for_provider_key_without_leaking_value(tmp_path) -> None:
    config = AgentConfig(
        provider="grok",
        model="grok-4.3",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
    )
    app = FastAPI()
    register_runtime_routes(
        app,
        active_config=config,
        state=_FakeState(),
        secret_broker=_FakeSecretBroker({"XAI_API_KEY"}),
    )
    client = TestClient(app)

    runtime = client.get("/api/runtime/config")
    catalog = client.get("/api/runtime/models?provider=grok")

    assert runtime.status_code == 200
    assert runtime.json()["provider"]["api_key_configured"] is True
    assert "broker-secret" not in runtime.text
    assert catalog.status_code == 200
    assert catalog.json()["api_key_configured"] is True
    assert "broker-secret" not in catalog.text


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
            "expected_revision": store.load(config).revision,
            "provider": "codex-cli",
            "model": "gpt-5.4",
            "temperature": 0.7,
            "backend": "memvid",
            "memory_dir": str(tmp_path / "mv2"),
            "workspace": str(tmp_path / "workspace"),
            "max_tool_rounds": 12,
            "stream": True,
            "require_api_auth": False,
            "autonomy_mode": "manual",
            "allow_shell": True,
            "allow_file_write": True,
            "allow_codex_cli": True,
            "allow_plugin_install": True,
            "allow_git_commit": True,
            "allow_memory_import": True,
            "allow_executable_skills": True,
            "allow_web": True,
            "allow_self_modification": True,
            "enable_semantic_orchestration": True,
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
    assert active_config.temperature == 0.7
    assert active_config.backend == "memvid"
    assert active_config.max_tool_rounds == 12
    assert active_config.stream is True
    assert active_config.require_api_auth is config.require_api_auth
    assert active_config.allow_shell is True
    assert active_config.allow_file_write is True
    assert active_config.allow_codex_cli is True
    assert active_config.allow_plugin_install is True
    assert active_config.allow_git_commit is True
    assert active_config.allow_memory_import is True
    assert active_config.enable_semantic_orchestration is True
    assert active_config.allow_executable_skills is True
    assert active_config.allow_web is True
    assert active_config.allow_self_modification is True

    runtime = client.get("/api/runtime/config")
    assert runtime.status_code == 200
    runtime_payload = runtime.json()
    assert runtime_payload["provider"]["name"] == "codex-cli"
    assert runtime_payload["provider"]["model"] == "gpt-5.4"
    assert runtime_payload["provider"]["temperature"] == 0.7
    assert runtime_payload["provider"]["stream"] is True
    assert runtime_payload["limits"]["max_tool_rounds"] == 12
    assert runtime_payload["paths"]["workspace"] == str(tmp_path / "workspace")
    assert runtime_payload["settings"]["runtime"]["persisted"] is True
    assert runtime_payload["settings"]["runtime"]["temperature"] == 0.7
    assert runtime_payload["settings"]["runtime"]["max_tool_rounds"] == 12
    assert runtime_payload["settings"]["runtime"]["allow_shell"] is True
    assert runtime_payload["feature_flags"]["allow_shell"] is True
    assert runtime_payload["feature_flags"]["enable_semantic_orchestration"] is True


def test_runtime_settings_save_persists_provider_endpoint_and_key_env(tmp_path) -> None:
    config = AgentConfig(
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
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
            "expected_revision": store.load(config).revision,
            "provider": "ollama-cloud",
            "model": "gpt-oss:120b",
            "base_url": "https://ollama.com/api",
            "api_key_env": "OLLAMA_API_KEY",
        },
    )

    assert saved.status_code == 200
    payload = saved.json()
    assert payload["settings"]["base_url"] == "https://ollama.com/api"
    assert payload["settings"]["api_key_env"] == "OLLAMA_API_KEY"
    assert active_config.provider == "ollama-cloud"
    assert active_config.model == "gpt-oss:120b"
    assert active_config.base_url == "https://ollama.com/api"
    assert active_config.api_key_env == "OLLAMA_API_KEY"

    runtime = client.get("/api/runtime/config")
    runtime_payload = runtime.json()
    assert runtime_payload["provider"]["base_url_configured"] is True
    assert runtime_payload["provider"]["api_key_env"] == "OLLAMA_API_KEY"


def test_runtime_settings_rejects_launch_controlled_api_auth_toggle(tmp_path) -> None:
    config = AgentConfig(require_api_auth=False, api_auth_token_env="KESTREL_MISSING_TOKEN")
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

    response = client.put(
        "/api/runtime/settings",
        json={
            "expected_revision": store.load(config).revision,
            "require_api_auth": True,
        },
    )

    assert response.status_code == 400
    assert "require_api_auth_is_launch_controlled" in response.text
    assert not store.exists()


def test_runtime_settings_requires_expected_revision(tmp_path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory", state_path=tmp_path / "state.db")
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    app = FastAPI()
    register_runtime_routes(
        app,
        active_config=config,
        state=_FakeState(),
        settings_store=store,
        http_exception=HTTPException,
    )

    with TestClient(app) as client:
        response = client.put("/api/runtime/settings", json={"model": "stale-model"})

    assert response.status_code == 400
    assert response.json()["detail"] == "expected_revision_is_required"
    assert not store.exists()


def test_runtime_settings_rejects_stale_client_revision_without_reenabling_capability(
    tmp_path,
) -> None:
    config = AgentConfig(
        provider="mock",
        model="mock",
        allow_shell=True,
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
    )
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    initial = store.save(RuntimeSettings.from_config(config))
    active = [config]
    app = FastAPI()
    register_runtime_routes(
        app,
        active_config=lambda: active[0],
        state=_FakeState(),
        settings_store=store,
        on_config_update=lambda candidate: active.__setitem__(0, candidate),
        http_exception=HTTPException,
    )

    with TestClient(app) as client:
        disabled = client.put(
            "/api/runtime/settings",
            json={"expected_revision": initial.revision, "allow_shell": False},
        )
        stale = client.put(
            "/api/runtime/settings",
            json={
                "expected_revision": initial.revision,
                "model": "new-model-from-stale-tab",
                "allow_shell": True,
            },
        )

    assert disabled.status_code == 200
    assert stale.status_code == 409
    assert store.load(config).allow_shell is False
    assert active[0].allow_shell is False


def test_runtime_settings_serializes_persistence_and_activation(tmp_path) -> None:
    config = AgentConfig(
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
    )
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    initial = store.save(RuntimeSettings.from_config(config))
    active = [config]
    activation_started = Event()
    release_activation = Event()
    load_started = Event()
    load_finished = Event()

    def activate(candidate: AgentConfig) -> None:
        activation_started.set()
        assert release_activation.wait(5)
        active[0] = candidate

    app = FastAPI()
    register_runtime_routes(
        app,
        active_config=lambda: active[0],
        state=_FakeState(),
        settings_store=store,
        on_config_update=activate,
        http_exception=HTTPException,
    )
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/runtime/settings"
        and "PUT" in getattr(route, "methods", set())
    )
    update_result: dict[str, object] = {}

    def update() -> None:
        update_result.update(
            endpoint(
                {
                    "expected_revision": initial.revision,
                    "model": "serialized-model",
                }
            )
        )

    def load() -> None:
        load_started.set()
        store.load(config)
        load_finished.set()

    update_thread = Thread(target=update)
    update_thread.start()
    assert activation_started.wait(5)
    load_thread = Thread(target=load)
    load_thread.start()
    assert load_started.wait(5)
    assert not load_finished.wait(0.05)
    release_activation.set()
    update_thread.join(5)
    load_thread.join(5)

    assert not update_thread.is_alive()
    assert not load_thread.is_alive()
    assert load_finished.is_set()
    assert active[0].model == "serialized-model"
    assert store.load(config).model == "serialized-model"
    runtime = update_result.get("runtime")
    assert isinstance(runtime, dict)
    assert runtime["model"] == "serialized-model"


def test_runtime_settings_activation_failure_restores_persisted_and_live_config(
    tmp_path,
) -> None:
    config = AgentConfig(
        provider="mock",
        model="mock",
        allow_shell=True,
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
    )
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    initial = store.save(RuntimeSettings.from_config(config))
    initial_payload = store.path.read_bytes()
    active = [config]

    class FailingRevocationRuns:
        class Registry:
            @staticmethod
            def specs() -> list[SimpleNamespace]:
                return [
                    SimpleNamespace(
                        name="shell.run",
                        source="builtin",
                        aliases=(),
                    )
                ]

            all_specs = specs

        def build_registry(self) -> Registry:
            return self.Registry()

        def revoke_pending_approvals_for_tools(
            self,
            _tool_names: set[str],
            *,
            reason: str,
        ) -> int:
            assert reason == "global_capability_disabled"
            raise RuntimeError("simulated approval revocation failure")

    app = FastAPI()
    register_runtime_routes(
        app,
        active_config=lambda: active[0],
        state=_FakeState(),
        settings_store=store,
        on_config_update=lambda candidate: active.__setitem__(0, candidate),
        http_exception=HTTPException,
        runs=FailingRevocationRuns(),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put(
            "/api/runtime/settings",
            json={
                "expected_revision": initial.revision,
                "allow_shell": False,
            },
        )

    assert response.status_code == 500
    assert active[0].allow_shell is True
    assert store.path.read_bytes() == initial_payload
    assert store.load(config).revision == initial.revision


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink path validation")
def test_runtime_settings_rejects_invalid_memory_path_before_persisting(
    tmp_path,
) -> None:
    runtime_root = tmp_path / "runtime"
    config = AgentConfig(
        memory_dir=runtime_root / "memory",
        state_path=runtime_root / "state" / "agent.db",
        log_dir=runtime_root / "logs",
        secret_store_path=runtime_root / "secrets" / "local_vault.json",
        skills_dir=runtime_root / "skills",
        plugins_dir=runtime_root / "plugins",
        mcp_config_path=runtime_root / "config" / "mcp_servers.json",
        channel_config_path=runtime_root / "config" / "channels.json",
        worker_worktree_dir=runtime_root / "worktrees",
        workspace=tmp_path / "workspace",
    )
    store = RuntimeSettingsStore(default_runtime_settings_path(config))
    original = store.save(RuntimeSettings.from_config(config))
    original_payload = store.path.read_bytes()
    outside = tmp_path / "outside-memory"
    outside.mkdir(mode=0o755)
    linked_memory = runtime_root / "linked-memory"
    linked_memory.parent.mkdir(parents=True, exist_ok=True)
    linked_memory.symlink_to(outside, target_is_directory=True)

    with TestClient(create_app(config), raise_server_exceptions=False) as client:
        response = client.put(
            "/api/runtime/settings",
            json={
                "expected_revision": original.revision,
                "memory_dir": str(linked_memory),
            },
        )

    assert response.status_code == 400
    assert store.path.read_bytes() == original_payload
    persisted = store.load(config)
    assert persisted.revision == original.revision
    assert persisted.memory_dir == str(config.memory_dir)
    assert outside.stat().st_mode & 0o777 == 0o755

    with TestClient(create_app(config)) as restarted:
        health = restarted.get("/api/health")
    assert health.status_code == 200


@pytest.mark.skipif(os.name == "nt", reason="POSIX artifact alias validation")
def test_runtime_settings_store_rejects_file_and_lock_aliases(tmp_path: Path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory")
    outside = tmp_path / "outside-settings.json"
    outside_store = RuntimeSettingsStore(outside)
    outside_store.save(RuntimeSettings.from_config(replace(config, allow_shell=True)))
    os.chmod(outside, 0o644)

    symlink_store = RuntimeSettingsStore(tmp_path / "symlink" / "runtime_settings.json")
    symlink_store.path.parent.mkdir()
    symlink_store.path.symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic links"):
        symlink_store.load(config)
    assert outside.stat().st_mode & 0o777 == 0o644

    hardlink_store = RuntimeSettingsStore(tmp_path / "hardlink" / "runtime_settings.json")
    hardlink_store.path.parent.mkdir()
    os.link(outside, hardlink_store.path)
    outside_payload = outside.read_bytes()
    with pytest.raises(ValueError, match="hard-linked"):
        hardlink_store.save(RuntimeSettings.from_config(config))
    assert outside.read_bytes() == outside_payload
    assert outside.stat().st_mode & 0o777 == 0o644

    lock_store = RuntimeSettingsStore(tmp_path / "lock-alias" / "runtime_settings.json")
    lock_store.path.parent.mkdir()
    lock_target = tmp_path / "outside-lock-target"
    lock_target.write_text("unchanged", encoding="utf-8")
    os.chmod(lock_target, 0o644)
    lock_store.path.with_name(".runtime_settings.json.lock").symlink_to(lock_target)
    with pytest.raises(ValueError, match="symbolic links"):
        lock_store.save(RuntimeSettings.from_config(config))
    assert lock_target.read_text(encoding="utf-8") == "unchanged"
    assert lock_target.stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX artifact ownership and modes")
def test_runtime_settings_store_hardens_mode_and_rejects_non_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory")
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    store.save(RuntimeSettings.from_config(config))
    os.chmod(store.path, 0o644)

    store.load(config)

    assert store.path.stat().st_mode & 0o777 == 0o600
    import nested_memvid_agent.private_artifacts as private_artifacts

    current_owner = os.geteuid()
    monkeypatch.setattr(private_artifacts.os, "geteuid", lambda: current_owner + 1)
    with pytest.raises(PermissionError, match="owned by the current user"):
        store.exists()


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
            temperature=0.7,
            max_tool_rounds=11,
            stream=True,
            require_api_auth=False,
            autonomy_mode="manual",
            allow_shell=True,
            allow_web=True,
        )
    )

    loaded = store.load(config)
    restarted_config = apply_runtime_settings(config, loaded)

    assert loaded == saved
    assert restarted_config.provider == "codex-cli"
    assert restarted_config.model == "gpt-5.4"
    assert restarted_config.temperature == 0.7
    assert restarted_config.backend == "memvid"
    assert restarted_config.memory_dir == tmp_path / "mv2"
    assert restarted_config.max_tool_rounds == 11
    assert restarted_config.stream is True
    assert restarted_config.require_api_auth is config.require_api_auth
    assert restarted_config.allow_shell is True
    assert restarted_config.allow_web is True


def test_runtime_settings_store_accepts_verified_older_schema_shape(tmp_path) -> None:
    config = AgentConfig(provider="mock", model="mock", memory_dir=tmp_path / "memory")
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    store.save(RuntimeSettings.from_config(config))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload.pop("provider_startup_probe")
    payload["sources"].pop("provider_startup_probe")
    payload["revision"] = None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload["revision"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load(config)
    updated = store.save(
        replace(loaded, model="updated-after-migration"),
        expected_revision=loaded.revision,
    )

    assert loaded.provider_startup_probe is False
    assert updated.model == "updated-after-migration"
    assert store.load(config).model == "updated-after-migration"


def test_runtime_settings_save_rejects_stale_revision(tmp_path) -> None:
    config = AgentConfig(provider="mock", model="mock", memory_dir=tmp_path / "memory")
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    first = store.save(RuntimeSettings.from_config(config))
    assert first.revision is not None
    second = store.save(replace(first, model="mock-v2"), expected_revision=first.revision)

    with pytest.raises(RuntimeSettingsConflict, match="revision_conflict"):
        store.save(replace(first, model="stale-write"), expected_revision=first.revision)

    assert store.load(config).revision == second.revision
    assert store.load(config).model == "mock-v2"


def test_runtime_settings_store_versions_hashes_and_protects_effective_settings(tmp_path) -> None:
    config = AgentConfig(provider="mock", model="mock", memory_dir=tmp_path / "memory")
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")

    saved = store.save(RuntimeSettings.from_config(config))
    payload = json.loads(store.path.read_text(encoding="utf-8"))

    assert saved.schema_version == 1
    assert saved.revision is not None and len(saved.revision) == 64
    assert payload["revision"] == saved.revision
    assert saved.sources["provider"] == "persisted"
    assert saved.sources["require_api_auth"] == "launch"
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_runtime_settings_store_rejects_unknown_future_schema(tmp_path) -> None:
    config = AgentConfig()
    store = RuntimeSettingsStore(tmp_path / "runtime_settings.json")
    store.path.write_text('{"schema_version": 999}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported runtime settings schema"):
        store.load(config)
