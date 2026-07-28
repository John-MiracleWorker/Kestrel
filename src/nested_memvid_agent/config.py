from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .routine_limits import (
    validate_routine_claim_ttl,
    validate_routine_poll_interval,
    validate_routines_per_tick,
)

MAX_TOOL_TIMEOUT_SECONDS = 3_600.0
MAX_TOOL_RETRY_BACKOFF_SECONDS = 300.0


@dataclass(frozen=True)
class _EnvironmentReader:
    values: Mapping[str, str]

    def get(self, name: str, default: str) -> str:
        return self.values.get(name, default)

    def as_bool(self, name: str) -> bool:
        return self.values.get(name, "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def as_bool_default(self, name: str, default: bool) -> bool:
        raw = self.values.get(name)
        if raw is None or not raw.strip():
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def as_int(self, name: str, default: int) -> int:
        raw = self.values.get(name)
        if raw is None or not raw.strip():
            return default
        return int(raw)

    def as_float(self, name: str, default: float) -> float:
        raw = self.values.get(name)
        if raw is None or not raw.strip():
            return default
        return float(raw)

    def as_float_or_none(self, name: str) -> float | None:
        raw = self.values.get(name)
        if raw is None or not raw.strip():
            return None
        return float(raw)

    def as_csv(self, name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = self.values.get(name)
        if raw is None or not raw.strip():
            return default
        values = tuple(part.strip() for part in raw.split(",") if part.strip())
        return values or default

    def as_str_or_none(self, name: str) -> str | None:
        raw = self.values.get(name)
        if raw is None:
            return None
        stripped = raw.strip()
        return stripped or None

    def as_path_or_none(self, name: str) -> Path | None:
        value = self.as_str_or_none(name)
        return Path(value) if value else None


@dataclass(frozen=True)
class AgentConfig:
    name: str = "Kestrel"
    provider: str = "mock"
    model: str = "mock"
    base_url: str | None = None
    api_key_env: str | None = None
    fallback_provider: str | None = None
    fallback_model: str | None = None
    fallback_base_url: str | None = None
    fallback_api_key_env: str | None = None
    timeout_seconds: int = 60
    max_retries: int = 2
    provider_circuit_failure_threshold: int = 3
    provider_circuit_cooldown_seconds: float = 30.0
    provider_startup_probe: bool = False
    run_lease_ttl_seconds: float = 30.0
    run_heartbeat_interval_seconds: float = 10.0
    max_concurrent_runs: int = 4
    max_queued_runs: int = 100
    temperature: float | None = None
    codex_sandbox: str = "read-only"
    codex_profile: str | None = None
    codex_skip_git_repo_check: bool = False
    codex_ephemeral: bool = True
    backend: str = "memory"
    memory_dir: Path = Path(".nest/memory")
    memory_max_layer_bytes: int = 1_073_741_824
    layer_config_path: Path | None = None
    workspace: Path = Path(".")
    project_id: str | None = None
    project_revision: int | None = None
    project_baseline_index_digest: str | None = None
    project_allowed_paths: tuple[str, ...] = (".",)
    max_tool_rounds: int = 6
    context_budget_chars: int = 18_000
    allow_shell: bool = False
    allow_file_write: bool = False
    allow_policy_writes: bool = False
    allow_codex_cli: bool = False
    allow_plugin_install: bool = False
    allow_git_commit: bool = False
    allow_git_push: bool = False
    allow_remote_mutation: bool = False
    git_write_mode: str = "local_branch"
    protected_branches: tuple[str, ...] = ("main", "master", "release/*")
    allow_memory_import: bool = False
    allow_executable_skills: bool = False
    allow_mcp_network_endpoints: bool = False
    allow_web: bool = False
    allow_self_modification: bool = False
    web_backend: str = "direct"
    web_timeout_seconds: int = 10
    web_max_results: int = 5
    web_max_bytes: int = 200_000
    require_approval_for_high_risk_tools: bool = True
    approval_ttl_seconds: float = 900.0
    enable_agentic_cycle: bool = True
    enable_semantic_orchestration: bool = False
    enable_autonomous_scheduler: bool = False
    max_scheduler_tasks: int = 3
    max_scheduler_cycles: int = 5
    enable_proactive_routines: bool = False
    routine_poll_interval_seconds: float = 30.0
    routine_claim_ttl_seconds: float = 120.0
    max_routines_per_tick: int = 3
    enable_worker_isolation: bool = False
    worker_worktree_dir: Path = Path(".nest/worktrees")
    worker_branch_prefix: str = "kestrel/worker"
    enable_task_capsules: bool = True
    task_capsule_retention_count: int = 1_000
    enable_auto_consolidation: bool = False
    auto_consolidation_dry_run: bool = True
    enable_auto_compact: bool = False
    auto_compact_apply: bool = False
    enable_behavior_deltas: bool = False
    enable_auto_activate_low_risk_deltas: bool = False
    enable_auto_skill_materialization: bool = False
    enable_auto_consolidation_shadow: bool = False
    enable_auto_consolidation_apply: bool = False
    enable_diagnosis_to_patch: bool = False
    max_active_deltas_per_run: int = 8
    context_pack_token_budget: int = 6000
    context_pack_expand_raw: bool = False
    stream: bool = False
    log_dir: Path = Path(".nest/logs")
    state_path: Path = Path(".nest/state/agent.db")
    secret_store_path: Path = Path(".nest/secrets/local_vault.json")
    secret_backend: str = "json"
    skills_dir: Path = Path(".nest/skills")
    plugins_dir: Path = Path(".nest/plugins")
    mcp_config_path: Path = Path(".nest/config/mcp_servers.json")
    channel_config_path: Path = Path(".nest/config/channels.json")
    enable_channel_delivery: bool = False
    channel_send_timeout_seconds: int = 10
    require_api_auth: bool = False
    api_auth_token_env: str = "NEST_AGENT_API_TOKEN"
    api_rate_limit_requests: int = 120
    api_rate_limit_window_seconds: float = 60.0
    api_rate_limit_max_clients: int = 2048
    max_request_body_bytes: int = 1_000_000
    tool_timeout_seconds: float = 30.0
    validation_container_image: str | None = None
    tool_retry_max_attempts: int = 3
    tool_retry_backoff_base_seconds: float = 1.0
    trusted_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1", "[::1]", "testserver")
    cors_origins: tuple[str, ...] = ()
    llm_turn_summaries: bool = False
    memory_seal_write_threshold: int = 50
    memory_seal_interval_seconds: float = 10.0
    enabled_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Retain the legacy field for snapshot compatibility, but never permit a
        # configuration document to disable exact-call approval for tools that
        # declare it. The registry enforces the same invariant independently.
        if not self.require_approval_for_high_risk_tools:
            object.__setattr__(self, "require_approval_for_high_risk_tools", True)
        if self.approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be greater than zero")
        object.__setattr__(
            self,
            "tool_timeout_seconds",
            _finite_seconds(
                "tool_timeout_seconds",
                self.tool_timeout_seconds,
                minimum=0.001,
                maximum=MAX_TOOL_TIMEOUT_SECONDS,
            ),
        )
        object.__setattr__(
            self,
            "tool_retry_backoff_base_seconds",
            _finite_seconds(
                "tool_retry_backoff_base_seconds",
                self.tool_retry_backoff_base_seconds,
                minimum=0.0,
                maximum=MAX_TOOL_RETRY_BACKOFF_SECONDS,
            ),
        )
        if (
            isinstance(self.task_capsule_retention_count, bool)
            or self.task_capsule_retention_count < 1
        ):
            raise ValueError(
                "task_capsule_retention_count must be an integer greater than or equal to 1"
            )
        object.__setattr__(
            self,
            "routine_poll_interval_seconds",
            validate_routine_poll_interval(self.routine_poll_interval_seconds),
        )
        object.__setattr__(
            self,
            "routine_claim_ttl_seconds",
            validate_routine_claim_ttl(self.routine_claim_ttl_seconds),
        )
        object.__setattr__(
            self,
            "max_routines_per_tick",
            validate_routines_per_tick(self.max_routines_per_tick),
        )
        for field_name in ("base_url", "fallback_base_url"):
            value = getattr(self, field_name)
            if not value:
                continue
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(
                    f"{field_name} must not embed credentials; use an environment reference"
                )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AgentConfig:
        environment = _EnvironmentReader(
            os.environ if environ is None else environ
        )
        return cls(
            provider=environment.get("NEST_AGENT_PROVIDER", "mock"),
            model=environment.get("NEST_AGENT_MODEL", "mock"),
            base_url=environment.as_str_or_none("NEST_AGENT_BASE_URL"),
            api_key_env=environment.as_str_or_none("NEST_AGENT_API_KEY_ENV"),
            fallback_provider=environment.as_str_or_none("NEST_AGENT_FALLBACK_PROVIDER"),
            fallback_model=environment.as_str_or_none("NEST_AGENT_FALLBACK_MODEL"),
            fallback_base_url=environment.as_str_or_none("NEST_AGENT_FALLBACK_BASE_URL"),
            fallback_api_key_env=environment.as_str_or_none("NEST_AGENT_FALLBACK_API_KEY_ENV"),
            timeout_seconds=environment.as_int("NEST_AGENT_TIMEOUT_SECONDS", 60),
            max_retries=environment.as_int("NEST_AGENT_MAX_RETRIES", 2),
            provider_circuit_failure_threshold=environment.as_int(
                "NEST_AGENT_PROVIDER_CIRCUIT_FAILURE_THRESHOLD", 3
            ),
            provider_circuit_cooldown_seconds=environment.as_float(
                "NEST_AGENT_PROVIDER_CIRCUIT_COOLDOWN_SECONDS", 30.0
            ),
            provider_startup_probe=environment.as_bool("NEST_AGENT_PROVIDER_STARTUP_PROBE"),
            run_lease_ttl_seconds=environment.as_float("NEST_AGENT_RUN_LEASE_TTL_SECONDS", 30.0),
            run_heartbeat_interval_seconds=environment.as_float(
                "NEST_AGENT_RUN_HEARTBEAT_INTERVAL_SECONDS", 10.0
            ),
            max_concurrent_runs=environment.as_int("NEST_AGENT_MAX_CONCURRENT_RUNS", 4),
            max_queued_runs=environment.as_int("NEST_AGENT_MAX_QUEUED_RUNS", 100),
            temperature=environment.as_float_or_none("NEST_AGENT_TEMPERATURE"),
            codex_sandbox=environment.get("NEST_AGENT_CODEX_SANDBOX", "read-only"),
            codex_profile=environment.as_str_or_none("NEST_AGENT_CODEX_PROFILE"),
            codex_skip_git_repo_check=environment.as_bool("NEST_AGENT_CODEX_SKIP_GIT_REPO_CHECK"),
            codex_ephemeral=not environment.as_bool("NEST_AGENT_CODEX_PERSIST_SESSION"),
            backend=environment.get("NEST_AGENT_BACKEND", "memory"),
            memory_dir=Path(environment.get("NEST_AGENT_MEMORY_DIR", ".nest/memory")),
            memory_max_layer_bytes=environment.as_int("NEST_AGENT_MEMORY_MAX_LAYER_BYTES", 1_073_741_824),
            layer_config_path=environment.as_path_or_none("NEST_AGENT_LAYER_CONFIG"),
            workspace=Path(environment.get("NEST_AGENT_WORKSPACE", ".")),
            max_tool_rounds=environment.as_int("NEST_AGENT_MAX_TOOL_ROUNDS", 6),
            context_budget_chars=environment.as_int("NEST_AGENT_CONTEXT_BUDGET_CHARS", 18_000),
            log_dir=Path(environment.get("NEST_AGENT_LOG_DIR", ".nest/logs")),
            state_path=Path(environment.get("NEST_AGENT_STATE_PATH", ".nest/state/agent.db")),
            secret_store_path=Path(
                environment.get("NEST_AGENT_SECRET_STORE_PATH", ".nest/secrets/local_vault.json")
            ),
            secret_backend=environment.get("NEST_AGENT_SECRET_BACKEND", "json"),
            skills_dir=Path(environment.get("NEST_AGENT_SKILLS_DIR", ".nest/skills")),
            plugins_dir=Path(environment.get("NEST_AGENT_PLUGINS_DIR", ".nest/plugins")),
            mcp_config_path=Path(
                environment.get("NEST_AGENT_MCP_CONFIG", ".nest/config/mcp_servers.json")
            ),
            channel_config_path=Path(
                environment.get("NEST_AGENT_CHANNEL_CONFIG", ".nest/config/channels.json")
            ),
            enable_channel_delivery=environment.as_bool("NEST_AGENT_ENABLE_CHANNEL_DELIVERY"),
            channel_send_timeout_seconds=environment.as_int("NEST_AGENT_CHANNEL_SEND_TIMEOUT_SECONDS", 10),
            api_rate_limit_requests=environment.as_int("NEST_AGENT_API_RATE_LIMIT_REQUESTS", 120),
            api_rate_limit_window_seconds=environment.as_float(
                "NEST_AGENT_API_RATE_LIMIT_WINDOW_SECONDS", 60.0
            ),
            api_rate_limit_max_clients=environment.as_int("NEST_AGENT_API_RATE_LIMIT_MAX_CLIENTS", 2048),
            max_request_body_bytes=environment.as_int("NEST_AGENT_MAX_REQUEST_BODY_BYTES", 1_000_000),
            tool_timeout_seconds=environment.as_float("NEST_AGENT_TOOL_TIMEOUT_SECONDS", 30.0),
            validation_container_image=environment.as_str_or_none("NEST_AGENT_VALIDATION_CONTAINER_IMAGE"),
            tool_retry_max_attempts=environment.as_int("NEST_AGENT_TOOL_RETRY_MAX_ATTEMPTS", 3),
            tool_retry_backoff_base_seconds=environment.as_float(
                "NEST_AGENT_TOOL_RETRY_BACKOFF_BASE_SECONDS", 1.0
            ),
            approval_ttl_seconds=environment.as_float("NEST_AGENT_APPROVAL_TTL_SECONDS", 900.0),
            allow_shell=environment.as_bool("NEST_AGENT_ALLOW_SHELL"),
            allow_file_write=environment.as_bool("NEST_AGENT_ALLOW_FILE_WRITE"),
            allow_policy_writes=environment.as_bool("NEST_AGENT_ALLOW_POLICY_WRITES"),
            allow_codex_cli=environment.as_bool("NEST_AGENT_ALLOW_CODEX_CLI"),
            allow_plugin_install=environment.as_bool("NEST_AGENT_ALLOW_PLUGIN_INSTALL"),
            allow_git_commit=environment.as_bool("NEST_AGENT_ALLOW_GIT_COMMIT"),
            allow_git_push=environment.as_bool("NEST_AGENT_ALLOW_GIT_PUSH"),
            allow_remote_mutation=environment.as_bool("NEST_AGENT_ALLOW_REMOTE_MUTATION"),
            git_write_mode=environment.get("NEST_AGENT_GIT_WRITE_MODE", "local_branch"),
            protected_branches=environment.as_csv(
                "NEST_AGENT_PROTECTED_BRANCHES", ("main", "master", "release/*")
            ),
            allow_memory_import=environment.as_bool("NEST_AGENT_ALLOW_MEMORY_IMPORT"),
            allow_executable_skills=environment.as_bool("NEST_AGENT_ALLOW_EXECUTABLE_SKILLS"),
            allow_mcp_network_endpoints=environment.as_bool("NEST_AGENT_ALLOW_MCP_NETWORK_ENDPOINTS"),
            allow_web=environment.as_bool("NEST_AGENT_ALLOW_WEB"),
            allow_self_modification=environment.as_bool("NEST_AGENT_ALLOW_SELF_MODIFICATION"),
            web_backend=environment.get("NEST_AGENT_WEB_BACKEND", "direct"),
            web_timeout_seconds=environment.as_int("NEST_AGENT_WEB_TIMEOUT_SECONDS", 10),
            web_max_results=environment.as_int("NEST_AGENT_WEB_MAX_RESULTS", 5),
            web_max_bytes=environment.as_int("NEST_AGENT_WEB_MAX_BYTES", 200_000),
            enable_agentic_cycle=not environment.as_bool("NEST_AGENT_DISABLE_AGENTIC_CYCLE"),
            enable_semantic_orchestration=environment.as_bool("NEST_AGENT_ENABLE_SEMANTIC_ORCHESTRATION"),
            enable_autonomous_scheduler=environment.as_bool("NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER"),
            max_scheduler_tasks=environment.as_int("NEST_AGENT_MAX_SCHEDULER_TASKS", 3),
            max_scheduler_cycles=environment.as_int("NEST_AGENT_MAX_SCHEDULER_CYCLES", 5),
            enable_proactive_routines=environment.as_bool("NEST_AGENT_ENABLE_PROACTIVE_ROUTINES"),
            routine_poll_interval_seconds=environment.as_float(
                "NEST_AGENT_ROUTINE_POLL_INTERVAL_SECONDS", 30.0
            ),
            routine_claim_ttl_seconds=environment.as_float("NEST_AGENT_ROUTINE_CLAIM_TTL_SECONDS", 120.0),
            max_routines_per_tick=environment.as_int("NEST_AGENT_MAX_ROUTINES_PER_TICK", 3),
            enable_worker_isolation=environment.as_bool("NEST_AGENT_ENABLE_WORKER_ISOLATION"),
            worker_worktree_dir=Path(
                environment.get("NEST_AGENT_WORKER_WORKTREE_DIR", ".nest/worktrees")
            ),
            worker_branch_prefix=environment.get("NEST_AGENT_WORKER_BRANCH_PREFIX", "kestrel/worker"),
            enable_task_capsules=not environment.as_bool("NEST_AGENT_DISABLE_TASK_CAPSULES")
            and environment.as_bool_default("NEST_AGENT_ENABLE_TASK_CAPSULES", True),
            task_capsule_retention_count=environment.as_int("NEST_AGENT_TASK_CAPSULE_RETENTION_COUNT", 1_000),
            enable_auto_consolidation=environment.as_bool("NEST_AGENT_ENABLE_AUTO_CONSOLIDATION"),
            auto_consolidation_dry_run=environment.as_bool_default(
                "NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN", True
            ),
            enable_auto_compact=environment.as_bool("NEST_AGENT_ENABLE_AUTO_COMPACT"),
            auto_compact_apply=environment.as_bool("NEST_AGENT_AUTO_COMPACT_APPLY"),
            enable_behavior_deltas=environment.as_bool("NEST_AGENT_ENABLE_BEHAVIOR_DELTAS"),
            enable_auto_activate_low_risk_deltas=environment.as_bool(
                "NEST_AGENT_ENABLE_AUTO_ACTIVATE_LOW_RISK_DELTAS"
            ),
            enable_auto_skill_materialization=environment.as_bool(
                "NEST_AGENT_ENABLE_AUTO_SKILL_MATERIALIZATION"
            ),
            enable_auto_consolidation_shadow=environment.as_bool(
                "NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_SHADOW"
            ),
            enable_auto_consolidation_apply=environment.as_bool("NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_APPLY"),
            enable_diagnosis_to_patch=environment.as_bool("NEST_AGENT_ENABLE_DIAGNOSIS_TO_PATCH"),
            max_active_deltas_per_run=environment.as_int("NEST_AGENT_MAX_ACTIVE_DELTAS_PER_RUN", 8),
            context_pack_token_budget=environment.as_int("NEST_AGENT_CONTEXT_PACK_TOKEN_BUDGET", 6000),
            context_pack_expand_raw=environment.as_bool("NEST_AGENT_CONTEXT_PACK_EXPAND_RAW"),
            stream=environment.as_bool("NEST_AGENT_STREAM"),
            require_api_auth=environment.as_bool("NEST_AGENT_REQUIRE_API_AUTH"),
            api_auth_token_env=environment.get("NEST_AGENT_API_AUTH_TOKEN_ENV", "NEST_AGENT_API_TOKEN"),
            trusted_hosts=environment.as_csv(
                "NEST_AGENT_TRUSTED_HOSTS", ("127.0.0.1", "localhost", "::1", "[::1]", "testserver")
            ),
            cors_origins=environment.as_csv("NEST_AGENT_CORS_ORIGINS", ()),
            llm_turn_summaries=environment.as_bool("NEST_AGENT_LLM_TURN_SUMMARIES"),
            memory_seal_write_threshold=environment.as_int("NEST_AGENT_MEMORY_SEAL_WRITE_THRESHOLD", 50),
            memory_seal_interval_seconds=environment.as_float(
                "NEST_AGENT_MEMORY_SEAL_INTERVAL_SECONDS", 10.0
            ),
            enabled_tools=environment.as_csv("NEST_AGENT_ENABLED_TOOLS", ()),
        )

    @classmethod
    def from_json_file(cls, path: Path) -> AgentConfig:
        raw = json.loads(path.read_text())
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> AgentConfig:
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"unsupported agent configuration fields: {', '.join(unknown)}")
        path_fields = {
            "memory_dir",
            "layer_config_path",
            "workspace",
            "log_dir",
            "state_path",
            "secret_store_path",
            "skills_dir",
            "plugins_dir",
            "mcp_config_path",
            "channel_config_path",
            "worker_worktree_dir",
        }
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            if key in path_fields and value is not None:
                normalized[key] = Path(value)
            elif key in {
                "protected_branches",
                "trusted_hosts",
                "cors_origins",
                "enabled_tools",
                "project_allowed_paths",
            } and isinstance(value, list):
                normalized[key] = tuple(str(item) for item in value)
            else:
                normalized[key] = value
        return cls(**normalized)

    def to_mapping(self) -> dict[str, Any]:
        rendered: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, Path):
                rendered[item.name] = str(value)
            elif isinstance(value, tuple):
                rendered[item.name] = list(value)
            else:
                rendered[item.name] = value
        return rendered


def _finite_seconds(
    name: str,
    value: object,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite")
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum} seconds")
    return normalized
