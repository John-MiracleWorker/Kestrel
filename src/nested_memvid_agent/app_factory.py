from __future__ import annotations

from .agent import AgentDependencies, NestedMV2Agent
from .config import AgentConfig
from .event_log import JsonlEventLog
from .llm.factory import build_llm_provider
from .orchestrator import build_memory_system
from .tools.builtin import build_default_tools
from .tools.registry import ToolRegistry


def build_agent(config: AgentConfig, tools: ToolRegistry | None = None) -> NestedMV2Agent:
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    memory = build_memory_system(config.backend, config.memory_dir)
    llm = build_llm_provider(config)
    registry = tools or build_default_tools()
    event_log = JsonlEventLog(config.log_dir / "events.jsonl")
    return NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=registry,
            config=config,
            event_log=event_log,
        )
    )
