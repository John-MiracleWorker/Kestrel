from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .llm.factory import provider_health_id
from .llm.resilience import global_provider_health_registry


class SetupReadinessStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class SetupReadinessCheck:
    check_id: str
    title: str
    status: SetupReadinessStatus
    detail: str
    recovery: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "title": self.title,
            "status": self.status.value,
            "detail": self.detail,
            "recovery": self.recovery,
        }


@dataclass(frozen=True)
class SetupReadinessReport:
    schema: str
    ready: bool
    pass_count: int
    warn_count: int
    fail_count: int
    checks: tuple[SetupReadinessCheck, ...]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "ready": self.ready,
            "pass_count": self.pass_count,
            "warn_count": self.warn_count,
            "fail_count": self.fail_count,
            "checks": [check.to_dict() for check in self.checks],
            "next_action": self.next_action,
        }


SecretResolver = Callable[[str | None], str | None]


def build_setup_readiness_report(
    config: AgentConfig,
    *,
    secret_resolver: SecretResolver | None = None,
) -> SetupReadinessReport:
    """Inspect non-secret first-run prerequisites for the local product surface.

    The report is deliberately safe for API/UI display: it checks only paths,
    provider metadata, environment-variable presence, and safety-gate flags. It
    never returns secret values and never creates or mutates files/directories.
    """
    checks = (
        _provider_check(config, secret_resolver=secret_resolver),
        _provider_operational_check(config),
        _workspace_check(config.workspace),
        _directory_check(
            "memory_storage",
            "Memory storage",
            config.memory_dir,
            missing_detail="Memory directory is not present yet.",
            missing_recovery="Run `nest-agent init` or start a local run so Kestrel can initialize memory layers.",
        ),
        _parent_path_check(
            "state_storage",
            "State database path",
            config.state_path,
            missing_recovery="Create the state parent directory or choose a writable `--state-path`.",
        ),
        _parent_path_check(
            "log_storage",
            "Log directory path",
            config.log_dir,
            missing_recovery="Create the log directory or choose a writable `--log-dir` before relying on diagnostics.",
            allow_directory_target=True,
        ),
        _permission_gate_check(config),
        _repair_isolation_check(config),
        _proactive_routines_check(config),
        _api_auth_check(config),
    )
    pass_count = sum(1 for check in checks if check.status == SetupReadinessStatus.PASS)
    warn_count = sum(1 for check in checks if check.status == SetupReadinessStatus.WARN)
    fail_count = sum(1 for check in checks if check.status == SetupReadinessStatus.FAIL)
    ready = fail_count == 0
    if fail_count:
        next_action = "Fix failing setup checks before starting the golden local workflow."
    elif warn_count:
        next_action = "You can run locally now; resolve warnings before treating this setup as product-ready."
    else:
        next_action = "Setup prerequisites are ready for the local golden workflow."
    return SetupReadinessReport(
        schema="kestrel.setup_readiness.v1",
        ready=ready,
        pass_count=pass_count,
        warn_count=warn_count,
        fail_count=fail_count,
        checks=checks,
        next_action=next_action,
    )


def _provider_check(config: AgentConfig, *, secret_resolver: SecretResolver | None = None) -> SetupReadinessCheck:
    provider = config.provider.strip() or "mock"
    if provider == "mock":
        return SetupReadinessCheck(
            "provider_configuration",
            "Provider configuration",
            SetupReadinessStatus.PASS,
            "Mock provider is selected, so deterministic first-run smoke tests can run without credentials.",
            "Choose a live provider later and rerun setup readiness before claiming provider support.",
        )
    if provider in {"openai-compatible", "lm-studio", "ollama"} and config.base_url:
        return SetupReadinessCheck(
            "provider_configuration",
            "Provider configuration",
            SetupReadinessStatus.PASS,
            f"{provider} has a configured base URL and model `{config.model}`.",
            "If the provider requires credentials, store the provider key in Settings or set the configured environment variable before live validation.",
        )
    if config.api_key_env and _secret_configured(config.api_key_env, secret_resolver=secret_resolver):
        return SetupReadinessCheck(
            "provider_configuration",
            "Provider configuration",
            SetupReadinessStatus.PASS,
            f"{provider} has model `{config.model}` and provider key `{config.api_key_env}` is available.",
            "Run the provider smoke/golden eval before release certification.",
        )
    if config.api_key_env:
        return SetupReadinessCheck(
            "provider_configuration",
            "Provider configuration",
            SetupReadinessStatus.FAIL,
            f"{provider} expects provider key `{config.api_key_env}`, but it is not stored in Settings or set in the environment.",
            f"Store `{config.api_key_env}` in Settings, set it in the environment, or switch to `--provider mock` for deterministic first-run checks.",
        )
    return SetupReadinessCheck(
        "provider_configuration",
        "Provider configuration",
        SetupReadinessStatus.WARN,
        f"{provider} is selected with model `{config.model}`, but no provider key name or local base URL is configured.",
        "Choose a provider key name in Settings, set `--base-url`, or use `--provider mock` until live provider validation is ready.",
    )


def _provider_operational_check(config: AgentConfig) -> SetupReadinessCheck:
    if config.provider == "mock":
        return SetupReadinessCheck(
            "provider_operational",
            "Provider operational health",
            SetupReadinessStatus.PASS,
            "Deterministic mock provider is operational for local validation.",
            "Use a live provider smoke test before claiming external provider readiness.",
        )
    health = global_provider_health_registry.snapshot(provider_health_id(config))
    state = str(health["state"])
    if state == "healthy":
        return SetupReadinessCheck(
            "provider_operational",
            "Provider operational health",
            SetupReadinessStatus.PASS,
            "The configured provider completed a live request successfully in this process.",
            "No recovery needed.",
        )
    if state in {"open", "degraded"}:
        failure_class = str(health.get("failure_class") or "provider_error")
        return SetupReadinessCheck(
            "provider_operational",
            "Provider operational health",
            SetupReadinessStatus.FAIL,
            f"The configured provider is {state}; last failure class: {failure_class}.",
            "Resolve credentials, rate limits, endpoint availability, or model configuration, then run a live smoke request.",
        )
    return SetupReadinessCheck(
        "provider_operational",
        "Provider operational health",
        SetupReadinessStatus.WARN,
        "Provider configuration exists, but this process has not completed a live request yet.",
        "Run a minimal live provider smoke request before treating the deployment as ready.",
    )


def _secret_configured(name_or_ref: str | None, *, secret_resolver: SecretResolver | None = None) -> bool:
    if not name_or_ref:
        return False
    if secret_resolver is not None:
        resolved = secret_resolver(name_or_ref)
        if resolved:
            return True
    return bool(os.getenv(name_or_ref))


def _workspace_check(path: Path) -> SetupReadinessCheck:
    resolved = path.expanduser()
    if not resolved.exists():
        return SetupReadinessCheck(
            "workspace",
            "Workspace",
            SetupReadinessStatus.FAIL,
            f"Workspace `{resolved}` does not exist.",
            "Create the workspace or pass `--workspace` pointing at the repo/project Kestrel should operate on.",
        )
    if not resolved.is_dir():
        return SetupReadinessCheck(
            "workspace",
            "Workspace",
            SetupReadinessStatus.FAIL,
            f"Workspace `{resolved}` is not a directory.",
            "Choose a directory workspace before starting local runs.",
        )
    if not os.access(resolved, os.R_OK | os.W_OK):
        return SetupReadinessCheck(
            "workspace",
            "Workspace",
            SetupReadinessStatus.FAIL,
            f"Workspace `{resolved}` is not readable and writable by this process.",
            "Fix filesystem permissions or choose a writable workspace.",
        )
    git_dir = resolved / ".git"
    status = SetupReadinessStatus.PASS if git_dir.exists() else SetupReadinessStatus.WARN
    detail = f"Workspace `{resolved}` is readable and writable."
    if status == SetupReadinessStatus.WARN:
        detail += " It is not currently a git repository."
    return SetupReadinessCheck(
        "workspace",
        "Workspace",
        status,
        detail,
        "Connect a git repository before running the golden repair workflow." if status == SetupReadinessStatus.WARN else "No recovery needed.",
    )


def _directory_check(
    check_id: str,
    title: str,
    path: Path,
    *,
    missing_detail: str,
    missing_recovery: str,
) -> SetupReadinessCheck:
    resolved = path.expanduser()
    if not resolved.exists():
        return SetupReadinessCheck(check_id, title, SetupReadinessStatus.WARN, f"{missing_detail} Path: `{resolved}`.", missing_recovery)
    if not resolved.is_dir():
        return SetupReadinessCheck(check_id, title, SetupReadinessStatus.FAIL, f"`{resolved}` exists but is not a directory.", "Choose a directory path or remove the blocking file.")
    if not os.access(resolved, os.R_OK | os.W_OK):
        return SetupReadinessCheck(check_id, title, SetupReadinessStatus.FAIL, f"`{resolved}` is not readable and writable.", "Fix directory permissions before running Kestrel.")
    return SetupReadinessCheck(check_id, title, SetupReadinessStatus.PASS, f"`{resolved}` exists and is readable/writable.", "No recovery needed.")


def _parent_path_check(
    check_id: str,
    title: str,
    path: Path,
    *,
    missing_recovery: str,
    allow_directory_target: bool = False,
) -> SetupReadinessCheck:
    resolved = path.expanduser()
    target = resolved if allow_directory_target else resolved.parent
    if resolved.exists() and not allow_directory_target and resolved.is_dir():
        return SetupReadinessCheck(check_id, title, SetupReadinessStatus.FAIL, f"`{resolved}` is a directory, not a file path.", "Choose a database file path such as `.nest/state/agent.db`.")
    if resolved.exists() and allow_directory_target:
        target = resolved
    if not target.exists():
        return SetupReadinessCheck(check_id, title, SetupReadinessStatus.WARN, f"Parent directory `{target}` does not exist yet.", missing_recovery)
    if not target.is_dir():
        return SetupReadinessCheck(check_id, title, SetupReadinessStatus.FAIL, f"Parent path `{target}` is not a directory.", "Choose a path with a valid writable parent directory.")
    if not os.access(target, os.R_OK | os.W_OK):
        return SetupReadinessCheck(check_id, title, SetupReadinessStatus.FAIL, f"Parent directory `{target}` is not readable and writable.", "Fix filesystem permissions before starting the server.")
    return SetupReadinessCheck(check_id, title, SetupReadinessStatus.PASS, f"Parent directory `{target}` is ready.", "No recovery needed.")


def _permission_gate_check(config: AgentConfig) -> SetupReadinessCheck:
    risky_enabled = [
        name
        for name, enabled in (
            ("shell", config.allow_shell),
            ("file_write", config.allow_file_write),
            ("git_commit", config.allow_git_commit),
            ("git_push", config.allow_git_push),
            ("remote_mutation", config.allow_remote_mutation),
            ("policy_writes", config.allow_policy_writes),
        )
        if enabled
    ]
    if not risky_enabled:
        return SetupReadinessCheck(
            "permission_gates",
            "Dangerous-action permission gates",
            SetupReadinessStatus.PASS,
            "High-risk mutation and publishing capabilities are disabled by default.",
            "Enable only the narrow capability needed for a reviewed workflow.",
        )
    if config.require_approval_for_high_risk_tools:
        return SetupReadinessCheck(
            "permission_gates",
            "Dangerous-action permission gates",
            SetupReadinessStatus.WARN,
            "Some high-risk capabilities are enabled but still require exact-call approval: " + ", ".join(risky_enabled) + ".",
            "Keep exact-call approvals enabled and disable capabilities not needed for first run.",
        )
    return SetupReadinessCheck(
        "permission_gates",
        "Dangerous-action permission gates",
        SetupReadinessStatus.FAIL,
        "High-risk capabilities are enabled without exact-call approvals: " + ", ".join(risky_enabled) + ".",
        "Re-enable exact-call approvals or disable high-risk capabilities before product use.",
    )


def _repair_isolation_check(config: AgentConfig) -> SetupReadinessCheck:
    if config.enable_worker_isolation:
        return SetupReadinessCheck(
            "repair_isolation",
            "Repair isolation",
            SetupReadinessStatus.PASS,
            f"Worker isolation is enabled; worktrees will use `{config.worker_worktree_dir}`.",
            "Verify the worker worktree root stays outside protected production paths.",
        )
    return SetupReadinessCheck(
        "repair_isolation",
        "Repair isolation",
        SetupReadinessStatus.WARN,
        "Worker/worktree isolation is not enabled, so this setup is not yet ready for the golden repair workflow.",
        "Set `NEST_AGENT_ENABLE_WORKER_ISOLATION=1` or pass `--enable-worker-isolation` before repair runs.",
    )


def _api_auth_check(config: AgentConfig) -> SetupReadinessCheck:
    if not config.require_api_auth:
        return SetupReadinessCheck(
            "api_auth",
            "Local API auth",
            SetupReadinessStatus.WARN,
            "Control-plane API auth is disabled. This is acceptable only for trusted local development.",
            "Set `NEST_AGENT_REQUIRE_API_AUTH=1` and provide `NEST_AGENT_API_TOKEN` before exposing the server beyond local use.",
        )
    if not os.getenv(config.api_auth_token_env):
        return SetupReadinessCheck(
            "api_auth",
            "Local API auth",
            SetupReadinessStatus.FAIL,
            f"API auth is required but `{config.api_auth_token_env}` is not set.",
            f"Set `{config.api_auth_token_env}` before starting the server.",
        )
    return SetupReadinessCheck(
        "api_auth",
        "Local API auth",
        SetupReadinessStatus.PASS,
        f"API auth is required and `{config.api_auth_token_env}` is present.",
        "No recovery needed.",
    )


def _proactive_routines_check(config: AgentConfig) -> SetupReadinessCheck:
    if not config.enable_proactive_routines:
        return SetupReadinessCheck(
            "proactive_routines",
            "Proactive routines",
            SetupReadinessStatus.PASS,
            "Time-based proactive execution is disabled by default.",
            "Enable it only after creating and reviewing disabled routine drafts through the local CLI or an authenticated API.",
        )
    if not config.require_api_auth:
        return SetupReadinessCheck(
            "proactive_routines",
            "Proactive routines",
            SetupReadinessStatus.WARN,
            "Proactive execution is enabled, but API authentication is disabled.",
            "Keep the server strictly loopback-only or enable API authentication before managing routines through the web API.",
        )
    return SetupReadinessCheck(
        "proactive_routines",
        "Proactive routines",
        SetupReadinessStatus.PASS,
        "Proactive execution is enabled behind authenticated owner controls; individual routines still default disabled.",
        "Review each routine revision and keep exact-call tool approvals enabled.",
    )
