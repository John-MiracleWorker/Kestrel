from __future__ import annotations

import os
from dataclasses import replace
from importlib import metadata as importlib_metadata
from typing import Any, NoReturn, cast

from .capability_policy import enablement_flag_for_tool
from .config import AgentConfig
from .llm.factory import provider_health_id
from .llm.model_catalog import all_model_catalogs, default_api_key_env, model_catalog_for_provider
from .llm.resilience import global_provider_health_registry
from .operational_metrics import readiness_snapshot
from .runtime_settings import (
    RuntimeSettingsConflict,
    RuntimeSettingsStore,
    apply_runtime_settings,
    merge_runtime_settings,
)


def register_runtime_routes(
    app: Any,
    *,
    active_config: Any,
    state: Any,
    settings_store: RuntimeSettingsStore | None = None,
    on_config_update: Any | None = None,
    secret_broker: Any | None = None,
    http_exception: Any | None = None,
    runs: Any | None = None,
    provider_probe: Any | None = None,
) -> None:
    @app.get("/api/health")  # type: ignore[untyped-decorator]
    def health() -> dict[str, object]:
        config = _active_config(active_config)
        return {"ok": True, "name": config.name}

    @app.get("/api/health/live")  # type: ignore[untyped-decorator]
    def liveness() -> dict[str, object]:
        return {"ok": True, "status": "live"}

    @app.get("/api/health/ready")  # type: ignore[untyped-decorator]
    def readiness() -> dict[str, object]:
        snapshot = readiness_snapshot(
            config=_active_config(active_config),
            state=state,
            runs=runs,
        )
        if not snapshot["ok"] and http_exception is not None:
            raise http_exception(status_code=503, detail=snapshot)
        return snapshot

    @app.post("/api/runtime/provider/probe")  # type: ignore[untyped-decorator]
    def probe_provider() -> dict[str, object]:
        if provider_probe is None:
            _raise(http_exception, 501, "provider_probe_unavailable")
        return cast(dict[str, object], provider_probe())

    @app.get("/api/runtime/config")  # type: ignore[untyped-decorator]
    def runtime_config() -> dict[str, object]:
        config = _active_config(active_config)
        try:
            package_version = importlib_metadata.version("nested-memvid-agent")
        except importlib_metadata.PackageNotFoundError:
            package_version = None
        provider_env = default_api_key_env(config.provider, config.api_key_env)
        fallback_env = default_api_key_env(config.fallback_provider or "", config.fallback_api_key_env)
        saved_settings = settings_store.load(config) if settings_store is not None else None
        return {
            "name": config.name,
            "version": package_version,
            "schema_version": state.schema_version(),
            "provider": {
                "name": config.provider,
                "model": config.model,
                "base_url_configured": bool(config.base_url),
                "api_key_env": provider_env,
                "api_key_configured": _secret_configured(secret_broker, provider_env),
                "fallback_provider": config.fallback_provider,
                "fallback_model": config.fallback_model,
                "fallback_base_url_configured": bool(config.fallback_base_url),
                "fallback_api_key_env": fallback_env,
                "fallback_api_key_configured": _secret_configured(secret_broker, fallback_env),
                "stream": config.stream,
                "temperature": config.temperature,
                "timeout_seconds": config.timeout_seconds,
                "max_retries": config.max_retries,
                "operational_health": global_provider_health_registry.snapshot(provider_health_id(config)),
                "fallback_operational_health": global_provider_health_registry.snapshot(
                    provider_health_id(
                        replace(
                            config,
                            provider=config.fallback_provider,
                            model=config.fallback_model or config.model,
                            base_url=config.fallback_base_url,
                            api_key_env=config.fallback_api_key_env,
                        )
                    )
                )
                if config.fallback_provider
                else None,
            },
            "feature_flags": {
                "allow_shell": config.allow_shell,
                "allow_file_write": config.allow_file_write,
                "allow_policy_writes": config.allow_policy_writes,
                "allow_codex_cli": config.allow_codex_cli,
                "allow_plugin_install": config.allow_plugin_install,
                "allow_git_commit": config.allow_git_commit,
                "allow_git_push": config.allow_git_push,
                "allow_remote_mutation": config.allow_remote_mutation,
                "allow_memory_import": config.allow_memory_import,
                "allow_executable_skills": config.allow_executable_skills,
                "allow_mcp_network_endpoints": config.allow_mcp_network_endpoints,
                "allow_web": config.allow_web,
                "allow_self_modification": config.allow_self_modification,
                "enable_auto_activate_low_risk_deltas": config.enable_auto_activate_low_risk_deltas,
                "enable_auto_skill_materialization": config.enable_auto_skill_materialization,
                "enable_auto_consolidation_shadow": config.enable_auto_consolidation_shadow,
                "enable_auto_consolidation_apply": config.enable_auto_consolidation_apply,
                "enable_diagnosis_to_patch": config.enable_diagnosis_to_patch,
                "require_approval_for_high_risk_tools": config.require_approval_for_high_risk_tools,
                "enable_agentic_cycle": config.enable_agentic_cycle,
                "enable_autonomous_scheduler": config.enable_autonomous_scheduler,
                "enable_worker_isolation": config.enable_worker_isolation,
                "enable_task_capsules": config.enable_task_capsules,
                "enable_auto_consolidation": config.enable_auto_consolidation,
                "auto_consolidation_dry_run": config.auto_consolidation_dry_run,
                "enable_channel_delivery": config.enable_channel_delivery,
                "require_api_auth": config.require_api_auth,
            },
            "git_safety": {
                "git_write_mode": config.git_write_mode,
                "protected_branches": list(config.protected_branches),
            },
            "limits": {
                "max_tool_rounds": config.max_tool_rounds,
                "context_budget_chars": config.context_budget_chars,
                "context_pack_token_budget": config.context_pack_token_budget,
                "max_scheduler_tasks": config.max_scheduler_tasks,
                "max_scheduler_cycles": config.max_scheduler_cycles,
                "tool_timeout_seconds": config.tool_timeout_seconds,
                "approval_ttl_seconds": config.approval_ttl_seconds,
                "web_timeout_seconds": config.web_timeout_seconds,
                "web_max_results": config.web_max_results,
                "web_max_bytes": config.web_max_bytes,
                "web_backend": config.web_backend,
                "secret_backend": config.secret_backend,
            },
            "paths": {
                "workspace": str(config.workspace),
                "memory_dir": str(config.memory_dir),
                "state_path": str(config.state_path),
                "log_dir": str(config.log_dir),
                "skills_dir": str(config.skills_dir),
                "plugins_dir": str(config.plugins_dir),
                "mcp_config_path": str(config.mcp_config_path),
                "channel_config_path": str(config.channel_config_path),
                "worker_worktree_dir": str(config.worker_worktree_dir),
                "secret_store_path": str(config.secret_store_path),
            },
            "settings": {
                "runtime": saved_settings.to_public_dict(path=settings_store.path, persisted=settings_store.exists())
                if settings_store is not None and saved_settings is not None
                else None,
            },
            "validation_commands": [
                "python -m compileall -q src tests scripts",
                "python -m pytest -q",
                "python scripts/run_golden_evals.py --backend memory --provider mock",
                'PYTHONPATH=src python -m nested_memvid_agent.cli chat --backend memory --provider mock --message "hello"',
                "npm run test --prefix web",
                "npm run build --prefix web",
            ],
        }

    @app.get("/api/runtime/models")  # type: ignore[untyped-decorator]
    def runtime_models(provider: str | None = None) -> dict[str, object]:
        config = _active_config(active_config)
        secret_resolver = getattr(secret_broker, "resolve", None)
        if provider:
            catalog = model_catalog_for_provider(config, provider, secret_resolver=secret_resolver)
            return catalog.to_public_dict()
        return {"providers": [catalog.to_public_dict() for catalog in all_model_catalogs(config, secret_resolver=secret_resolver)]}

    @app.get("/api/runtime/settings")  # type: ignore[untyped-decorator]
    def get_runtime_settings() -> dict[str, object]:
        store = _require_settings_store(settings_store, http_exception)
        config = _active_config(active_config)
        settings = store.load(config)
        return {"settings": settings.to_public_dict(path=store.path, persisted=store.exists())}

    @app.put("/api/runtime/settings")  # type: ignore[untyped-decorator]
    def save_runtime_settings(request: dict[str, Any]) -> dict[str, object]:
        store = _require_settings_store(settings_store, http_exception)
        config = _active_config(active_config)
        try:
            current = store.load(config)
            if "require_api_auth" in request and _request_bool(request["require_api_auth"]) != config.require_api_auth:
                _raise(http_exception, 400, "require_api_auth_is_launch_controlled")
            settings = merge_runtime_settings(config, current, request)
        except ValueError as exc:
            _raise(http_exception, 400, str(exc))
        try:
            saved = store.save(settings, expected_revision=current.revision)
        except RuntimeSettingsConflict as exc:
            _raise(http_exception, 409, str(exc))
        next_config = apply_runtime_settings(config, saved)
        if on_config_update is not None:
            on_config_update(next_config)
        revoked_approvals = 0
        if runs is not None:
            registry = runs.build_registry()
            specs = getattr(registry, "all_specs", registry.specs)()
            disabled_flags = {
                flag
                for spec in specs
                if (flag := enablement_flag_for_tool(spec)) is not None
                and bool(getattr(config, flag, False))
                and not bool(getattr(next_config, flag, False))
            }
            affected = {
                name
                for spec in specs
                if enablement_flag_for_tool(spec) in disabled_flags
                for name in (spec.name, *spec.aliases)
            }
            if affected:
                revoked_approvals = runs.revoke_pending_approvals_for_tools(
                    affected,
                    reason="global_capability_disabled",
                )
        return {
            "settings": saved.to_public_dict(path=store.path, persisted=True),
            "runtime": {
                "provider": next_config.provider,
                "model": next_config.model,
                "base_url": next_config.base_url,
                "api_key_env": next_config.api_key_env,
                "backend": next_config.backend,
                "memory_dir": str(next_config.memory_dir),
                "workspace": str(next_config.workspace),
                "max_tool_rounds": next_config.max_tool_rounds,
                "stream": next_config.stream,
                "require_api_auth": next_config.require_api_auth,
                "allow_shell": next_config.allow_shell,
                "allow_file_write": next_config.allow_file_write,
                "allow_codex_cli": next_config.allow_codex_cli,
                "allow_plugin_install": next_config.allow_plugin_install,
                "allow_git_commit": next_config.allow_git_commit,
                "allow_memory_import": next_config.allow_memory_import,
                "allow_executable_skills": next_config.allow_executable_skills,
                "allow_web": next_config.allow_web,
                "allow_self_modification": next_config.allow_self_modification,
            },
            "revoked_approvals": revoked_approvals,
        }


def _active_config(active_config: Any) -> AgentConfig:
    config = active_config() if callable(active_config) else active_config
    return cast(AgentConfig, config)


def _require_settings_store(store: RuntimeSettingsStore | None, http_exception: Any | None) -> RuntimeSettingsStore:
    if store is None:
        _raise(http_exception, 503, "runtime_settings_store_unavailable")
    return store


def _secret_configured(secret_broker: Any | None, name_or_ref: str | None) -> bool:
    if not name_or_ref:
        return False
    if secret_broker is not None and hasattr(secret_broker, "status"):
        try:
            return bool(secret_broker.status(name_or_ref).get("configured"))
        except Exception:  # noqa: BLE001 - status should never break runtime config
            return bool(os.getenv(name_or_ref))
    return bool(os.getenv(name_or_ref))


def _raise(http_exception: Any | None, status_code: int, detail: str) -> NoReturn:
    if http_exception is not None:
        raise http_exception(status_code=status_code, detail=detail)
    raise RuntimeError(detail)


def _request_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
