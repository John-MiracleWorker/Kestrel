from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentConfig:
    name: str = "Nested MV2 Agent"
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
    temperature: float = 0.2
    codex_sandbox: str = "read-only"
    codex_profile: str | None = None
    codex_skip_git_repo_check: bool = False
    codex_ephemeral: bool = True
    backend: str = "memory"
    memory_dir: Path = Path(".nest/memory")
    layer_config_path: Path | None = None
    workspace: Path = Path(".")
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
    enable_agentic_cycle: bool = True
    enable_autonomous_scheduler: bool = False
    max_scheduler_tasks: int = 3
    max_scheduler_cycles: int = 5
    enable_worker_isolation: bool = False
    worker_worktree_dir: Path = Path(".nest/worktrees")
    worker_branch_prefix: str = "kestrel/worker"
    enable_task_capsules: bool = True
    enable_auto_consolidation: bool = False
    auto_consolidation_dry_run: bool = True
    enable_auto_compact: bool = False
    auto_compact_apply: bool = False
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
    tool_timeout_seconds: float = 30.0
    trusted_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1", "[::1]", "testserver")
    cors_origins: tuple[str, ...] = ()
    llm_turn_summaries: bool = False
    memory_seal_write_threshold: int = 50
    memory_seal_interval_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            provider=os.getenv("NEST_AGENT_PROVIDER", "mock"),
            model=os.getenv("NEST_AGENT_MODEL", "mock"),
            base_url=_env_str_or_none("NEST_AGENT_BASE_URL"),
            api_key_env=_env_str_or_none("NEST_AGENT_API_KEY_ENV"),
            fallback_provider=_env_str_or_none("NEST_AGENT_FALLBACK_PROVIDER"),
            fallback_model=_env_str_or_none("NEST_AGENT_FALLBACK_MODEL"),
            fallback_base_url=_env_str_or_none("NEST_AGENT_FALLBACK_BASE_URL"),
            fallback_api_key_env=_env_str_or_none("NEST_AGENT_FALLBACK_API_KEY_ENV"),
            timeout_seconds=_env_int("NEST_AGENT_TIMEOUT_SECONDS", 60),
            max_retries=_env_int("NEST_AGENT_MAX_RETRIES", 2),
            temperature=_env_float("NEST_AGENT_TEMPERATURE", 0.2),
            codex_sandbox=os.getenv("NEST_AGENT_CODEX_SANDBOX", "read-only"),
            codex_profile=_env_str_or_none("NEST_AGENT_CODEX_PROFILE"),
            codex_skip_git_repo_check=_env_bool("NEST_AGENT_CODEX_SKIP_GIT_REPO_CHECK"),
            codex_ephemeral=not _env_bool("NEST_AGENT_CODEX_PERSIST_SESSION"),
            backend=os.getenv("NEST_AGENT_BACKEND", "memory"),
            memory_dir=Path(os.getenv("NEST_AGENT_MEMORY_DIR", ".nest/memory")),
            layer_config_path=_env_path_or_none("NEST_AGENT_LAYER_CONFIG"),
            workspace=Path(os.getenv("NEST_AGENT_WORKSPACE", ".")),
            log_dir=Path(os.getenv("NEST_AGENT_LOG_DIR", ".nest/logs")),
            state_path=Path(os.getenv("NEST_AGENT_STATE_PATH", ".nest/state/agent.db")),
            secret_store_path=Path(os.getenv("NEST_AGENT_SECRET_STORE_PATH", ".nest/secrets/local_vault.json")),
            secret_backend=os.getenv("NEST_AGENT_SECRET_BACKEND", "json"),
            skills_dir=Path(os.getenv("NEST_AGENT_SKILLS_DIR", ".nest/skills")),
            plugins_dir=Path(os.getenv("NEST_AGENT_PLUGINS_DIR", ".nest/plugins")),
            mcp_config_path=Path(os.getenv("NEST_AGENT_MCP_CONFIG", ".nest/config/mcp_servers.json")),
            channel_config_path=Path(os.getenv("NEST_AGENT_CHANNEL_CONFIG", ".nest/config/channels.json")),
            enable_channel_delivery=_env_bool("NEST_AGENT_ENABLE_CHANNEL_DELIVERY"),
            channel_send_timeout_seconds=_env_int("NEST_AGENT_CHANNEL_SEND_TIMEOUT_SECONDS", 10),
            tool_timeout_seconds=_env_float("NEST_AGENT_TOOL_TIMEOUT_SECONDS", 30.0),
            allow_shell=_env_bool("NEST_AGENT_ALLOW_SHELL"),
            allow_file_write=_env_bool("NEST_AGENT_ALLOW_FILE_WRITE"),
            allow_policy_writes=_env_bool("NEST_AGENT_ALLOW_POLICY_WRITES"),
            allow_codex_cli=_env_bool("NEST_AGENT_ALLOW_CODEX_CLI"),
            allow_plugin_install=_env_bool("NEST_AGENT_ALLOW_PLUGIN_INSTALL"),
            allow_git_commit=_env_bool("NEST_AGENT_ALLOW_GIT_COMMIT"),
            allow_git_push=_env_bool("NEST_AGENT_ALLOW_GIT_PUSH"),
            allow_remote_mutation=_env_bool("NEST_AGENT_ALLOW_REMOTE_MUTATION"),
            git_write_mode=os.getenv("NEST_AGENT_GIT_WRITE_MODE", "local_branch"),
            protected_branches=_env_csv("NEST_AGENT_PROTECTED_BRANCHES", ("main", "master", "release/*")),
            allow_memory_import=_env_bool("NEST_AGENT_ALLOW_MEMORY_IMPORT"),
            allow_executable_skills=_env_bool("NEST_AGENT_ALLOW_EXECUTABLE_SKILLS"),
            allow_mcp_network_endpoints=_env_bool("NEST_AGENT_ALLOW_MCP_NETWORK_ENDPOINTS"),
            allow_web=_env_bool("NEST_AGENT_ALLOW_WEB"),
            allow_self_modification=_env_bool("NEST_AGENT_ALLOW_SELF_MODIFICATION"),
            web_backend=os.getenv("NEST_AGENT_WEB_BACKEND", "direct"),
            web_timeout_seconds=_env_int("NEST_AGENT_WEB_TIMEOUT_SECONDS", 10),
            web_max_results=_env_int("NEST_AGENT_WEB_MAX_RESULTS", 5),
            web_max_bytes=_env_int("NEST_AGENT_WEB_MAX_BYTES", 200_000),
            enable_agentic_cycle=not _env_bool("NEST_AGENT_DISABLE_AGENTIC_CYCLE"),
            enable_autonomous_scheduler=_env_bool("NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER"),
            max_scheduler_tasks=_env_int("NEST_AGENT_MAX_SCHEDULER_TASKS", 3),
            max_scheduler_cycles=_env_int("NEST_AGENT_MAX_SCHEDULER_CYCLES", 5),
            enable_worker_isolation=_env_bool("NEST_AGENT_ENABLE_WORKER_ISOLATION"),
            worker_worktree_dir=Path(os.getenv("NEST_AGENT_WORKER_WORKTREE_DIR", ".nest/worktrees")),
            worker_branch_prefix=os.getenv("NEST_AGENT_WORKER_BRANCH_PREFIX", "kestrel/worker"),
            enable_task_capsules=not _env_bool("NEST_AGENT_DISABLE_TASK_CAPSULES")
            and _env_bool_default("NEST_AGENT_ENABLE_TASK_CAPSULES", True),
            enable_auto_consolidation=_env_bool("NEST_AGENT_ENABLE_AUTO_CONSOLIDATION"),
            auto_consolidation_dry_run=_env_bool_default("NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN", True),
            enable_auto_compact=_env_bool("NEST_AGENT_ENABLE_AUTO_COMPACT"),
            auto_compact_apply=_env_bool("NEST_AGENT_AUTO_COMPACT_APPLY"),
            context_pack_token_budget=_env_int("NEST_AGENT_CONTEXT_PACK_TOKEN_BUDGET", 6000),
            context_pack_expand_raw=_env_bool("NEST_AGENT_CONTEXT_PACK_EXPAND_RAW"),
            stream=_env_bool("NEST_AGENT_STREAM"),
            require_api_auth=_env_bool("NEST_AGENT_REQUIRE_API_AUTH"),
            api_auth_token_env=os.getenv("NEST_AGENT_API_AUTH_TOKEN_ENV", "NEST_AGENT_API_TOKEN"),
            trusted_hosts=_env_csv("NEST_AGENT_TRUSTED_HOSTS", ("127.0.0.1", "localhost", "::1", "[::1]", "testserver")),
            cors_origins=_env_csv("NEST_AGENT_CORS_ORIGINS", ()),
            llm_turn_summaries=_env_bool("NEST_AGENT_LLM_TURN_SUMMARIES"),
            memory_seal_write_threshold=_env_int("NEST_AGENT_MEMORY_SEAL_WRITE_THRESHOLD", 50),
            memory_seal_interval_seconds=_env_float("NEST_AGENT_MEMORY_SEAL_INTERVAL_SECONDS", 10.0),
        )

    @classmethod
    def from_json_file(cls, path: Path) -> AgentConfig:
        raw = json.loads(path.read_text())
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> AgentConfig:
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
            elif key in {"protected_branches", "trusted_hosts", "cors_origins"} and isinstance(value, list):
                normalized[key] = tuple(str(item) for item in value)
            else:
                normalized[key] = value
        return cls(**normalized)


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_bool_default(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


def _env_str_or_none(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _env_path_or_none(name: str) -> Path | None:
    value = _env_str_or_none(name)
    return Path(value) if value else None
