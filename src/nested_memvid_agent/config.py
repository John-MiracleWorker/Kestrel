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
