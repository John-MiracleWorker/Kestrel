from __future__ import annotations

import os
from importlib import metadata as importlib_metadata
from typing import Any


def register_runtime_routes(app: Any, *, active_config: Any, state: Any) -> None:
    @app.get("/api/health")  # type: ignore[untyped-decorator]
    def health() -> dict[str, object]:
        return {"ok": True, "name": active_config.name}

    @app.get("/api/runtime/config")  # type: ignore[untyped-decorator]
    def runtime_config() -> dict[str, object]:
        try:
            package_version = importlib_metadata.version("nested-memvid-agent")
        except importlib_metadata.PackageNotFoundError:
            package_version = None
        provider_env = active_config.api_key_env
        fallback_env = active_config.fallback_api_key_env
        return {
            "name": active_config.name,
            "version": package_version,
            "schema_version": state.schema_version(),
            "provider": {
                "name": active_config.provider,
                "model": active_config.model,
                "base_url_configured": bool(active_config.base_url),
                "api_key_env": provider_env,
                "api_key_configured": bool(provider_env and os.getenv(provider_env)),
                "fallback_provider": active_config.fallback_provider,
                "fallback_model": active_config.fallback_model,
                "fallback_base_url_configured": bool(active_config.fallback_base_url),
                "fallback_api_key_env": fallback_env,
                "fallback_api_key_configured": bool(fallback_env and os.getenv(fallback_env)),
                "stream": active_config.stream,
                "timeout_seconds": active_config.timeout_seconds,
                "max_retries": active_config.max_retries,
            },
            "feature_flags": {
                "allow_shell": active_config.allow_shell,
                "allow_file_write": active_config.allow_file_write,
                "allow_policy_writes": active_config.allow_policy_writes,
                "allow_codex_cli": active_config.allow_codex_cli,
                "allow_plugin_install": active_config.allow_plugin_install,
                "allow_git_commit": active_config.allow_git_commit,
                "allow_git_push": active_config.allow_git_push,
                "allow_remote_mutation": active_config.allow_remote_mutation,
                "allow_memory_import": active_config.allow_memory_import,
                "allow_executable_skills": active_config.allow_executable_skills,
                "allow_mcp_network_endpoints": active_config.allow_mcp_network_endpoints,
                "allow_web": active_config.allow_web,
                "allow_self_modification": active_config.allow_self_modification,
                "require_approval_for_high_risk_tools": active_config.require_approval_for_high_risk_tools,
                "enable_agentic_cycle": active_config.enable_agentic_cycle,
                "enable_autonomous_scheduler": active_config.enable_autonomous_scheduler,
                "enable_worker_isolation": active_config.enable_worker_isolation,
                "enable_task_capsules": active_config.enable_task_capsules,
                "enable_auto_consolidation": active_config.enable_auto_consolidation,
                "auto_consolidation_dry_run": active_config.auto_consolidation_dry_run,
                "enable_channel_delivery": active_config.enable_channel_delivery,
                "require_api_auth": active_config.require_api_auth,
            },
            "git_safety": {
                "git_write_mode": active_config.git_write_mode,
                "protected_branches": list(active_config.protected_branches),
            },
            "limits": {
                "max_tool_rounds": active_config.max_tool_rounds,
                "context_budget_chars": active_config.context_budget_chars,
                "context_pack_token_budget": active_config.context_pack_token_budget,
                "max_scheduler_tasks": active_config.max_scheduler_tasks,
                "max_scheduler_cycles": active_config.max_scheduler_cycles,
                "tool_timeout_seconds": active_config.tool_timeout_seconds,
                "web_timeout_seconds": active_config.web_timeout_seconds,
                "web_max_results": active_config.web_max_results,
                "web_max_bytes": active_config.web_max_bytes,
                "web_backend": active_config.web_backend,
                "secret_backend": active_config.secret_backend,
            },
            "paths": {
                "workspace": str(active_config.workspace),
                "memory_dir": str(active_config.memory_dir),
                "state_path": str(active_config.state_path),
                "log_dir": str(active_config.log_dir),
                "skills_dir": str(active_config.skills_dir),
                "plugins_dir": str(active_config.plugins_dir),
                "mcp_config_path": str(active_config.mcp_config_path),
                "channel_config_path": str(active_config.channel_config_path),
                "worker_worktree_dir": str(active_config.worker_worktree_dir),
                "secret_store_path": str(active_config.secret_store_path),
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
