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
    workspace: Path = Path(".")
    max_tool_rounds: int = 6
    context_budget_chars: int = 18_000
    allow_shell: bool = False
    allow_file_write: bool = False
    allow_policy_writes: bool = False
    allow_codex_cli: bool = False
    require_approval_for_high_risk_tools: bool = True
    enable_task_capsules: bool = True
    enable_auto_consolidation: bool = False
    auto_consolidation_dry_run: bool = True
    context_pack_token_budget: int = 6000
    context_pack_expand_raw: bool = False
    stream: bool = False
    log_dir: Path = Path(".nest/logs")
    state_path: Path = Path(".nest/state/agent.db")
    skills_dir: Path = Path(".nest/skills")
    mcp_config_path: Path = Path(".nest/config/mcp_servers.json")
    channel_config_path: Path = Path(".nest/config/channels.json")
    enable_channel_delivery: bool = False
    channel_send_timeout_seconds: int = 10
    tool_timeout_seconds: float = 30.0

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
            workspace=Path(os.getenv("NEST_AGENT_WORKSPACE", ".")),
            log_dir=Path(os.getenv("NEST_AGENT_LOG_DIR", ".nest/logs")),
            state_path=Path(os.getenv("NEST_AGENT_STATE_PATH", ".nest/state/agent.db")),
            skills_dir=Path(os.getenv("NEST_AGENT_SKILLS_DIR", ".nest/skills")),
            mcp_config_path=Path(os.getenv("NEST_AGENT_MCP_CONFIG", ".nest/config/mcp_servers.json")),
            channel_config_path=Path(os.getenv("NEST_AGENT_CHANNEL_CONFIG", ".nest/config/channels.json")),
            enable_channel_delivery=_env_bool("NEST_AGENT_ENABLE_CHANNEL_DELIVERY"),
            channel_send_timeout_seconds=_env_int("NEST_AGENT_CHANNEL_SEND_TIMEOUT_SECONDS", 10),
            tool_timeout_seconds=_env_float("NEST_AGENT_TOOL_TIMEOUT_SECONDS", 30.0),
            allow_shell=_env_bool("NEST_AGENT_ALLOW_SHELL"),
            allow_file_write=_env_bool("NEST_AGENT_ALLOW_FILE_WRITE"),
            allow_policy_writes=_env_bool("NEST_AGENT_ALLOW_POLICY_WRITES"),
            allow_codex_cli=_env_bool("NEST_AGENT_ALLOW_CODEX_CLI"),
            enable_task_capsules=not _env_bool("NEST_AGENT_DISABLE_TASK_CAPSULES")
            and _env_bool_default("NEST_AGENT_ENABLE_TASK_CAPSULES", True),
            enable_auto_consolidation=_env_bool("NEST_AGENT_ENABLE_AUTO_CONSOLIDATION"),
            auto_consolidation_dry_run=_env_bool_default("NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN", True),
            context_pack_token_budget=_env_int("NEST_AGENT_CONTEXT_PACK_TOKEN_BUDGET", 6000),
            context_pack_expand_raw=_env_bool("NEST_AGENT_CONTEXT_PACK_EXPAND_RAW"),
            stream=_env_bool("NEST_AGENT_STREAM"),
        )

    @classmethod
    def from_json_file(cls, path: Path) -> AgentConfig:
        raw = json.loads(path.read_text())
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> AgentConfig:
        path_fields = {
            "memory_dir",
            "workspace",
            "log_dir",
            "state_path",
            "skills_dir",
            "mcp_config_path",
            "channel_config_path",
        }
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            normalized[key] = Path(value) if key in path_fields and value is not None else value
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


def _env_str_or_none(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None
