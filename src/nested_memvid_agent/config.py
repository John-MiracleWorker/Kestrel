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
    timeout_seconds: int = 60
    max_retries: int = 2
    temperature: float = 0.2
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
    stream: bool = False
    log_dir: Path = Path(".nest/logs")
    state_path: Path = Path(".nest/state/agent.db")
    skills_dir: Path = Path(".nest/skills")
    mcp_config_path: Path = Path(".nest/config/mcp_servers.json")

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            provider=os.getenv("NEST_AGENT_PROVIDER", "mock"),
            model=os.getenv("NEST_AGENT_MODEL", "mock"),
            base_url=_env_str_or_none("NEST_AGENT_BASE_URL"),
            api_key_env=_env_str_or_none("NEST_AGENT_API_KEY_ENV"),
            timeout_seconds=_env_int("NEST_AGENT_TIMEOUT_SECONDS", 60),
            max_retries=_env_int("NEST_AGENT_MAX_RETRIES", 2),
            temperature=_env_float("NEST_AGENT_TEMPERATURE", 0.2),
            backend=os.getenv("NEST_AGENT_BACKEND", "memory"),
            memory_dir=Path(os.getenv("NEST_AGENT_MEMORY_DIR", ".nest/memory")),
            workspace=Path(os.getenv("NEST_AGENT_WORKSPACE", ".")),
            log_dir=Path(os.getenv("NEST_AGENT_LOG_DIR", ".nest/logs")),
            state_path=Path(os.getenv("NEST_AGENT_STATE_PATH", ".nest/state/agent.db")),
            skills_dir=Path(os.getenv("NEST_AGENT_SKILLS_DIR", ".nest/skills")),
            mcp_config_path=Path(os.getenv("NEST_AGENT_MCP_CONFIG", ".nest/config/mcp_servers.json")),
            allow_shell=_env_bool("NEST_AGENT_ALLOW_SHELL"),
            allow_file_write=_env_bool("NEST_AGENT_ALLOW_FILE_WRITE"),
            allow_policy_writes=_env_bool("NEST_AGENT_ALLOW_POLICY_WRITES"),
            allow_codex_cli=_env_bool("NEST_AGENT_ALLOW_CODEX_CLI"),
            stream=_env_bool("NEST_AGENT_STREAM"),
        )

    @classmethod
    def from_json_file(cls, path: Path) -> AgentConfig:
        raw = json.loads(path.read_text())
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> AgentConfig:
        path_fields = {"memory_dir", "workspace", "log_dir", "state_path", "skills_dir", "mcp_config_path"}
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            normalized[key] = Path(value) if key in path_fields and value is not None else value
        return cls(**normalized)


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


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
