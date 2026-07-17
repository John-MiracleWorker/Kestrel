from __future__ import annotations

from collections.abc import Callable

from .agent import AgentDependencies, NestedMV2Agent
from .config import AgentConfig
from .event_log import JsonlEventLog
from .layers import load_layer_specs
from .llm.factory import build_llm_provider
from .orchestrator import build_memory_system
from .promotion_ledger import PromotionLedger
from .security_boundary import register_secret_env_names
from .state_store import AgentStateStore
from .tools.builtin import build_default_tools
from .tools.registry import RetryingRegistry, ToolRegistry


def build_agent(
    config: AgentConfig,
    tools: ToolRegistry | None = None,
    *,
    state: AgentStateStore | None = None,
    secret_resolver: Callable[[str | None], str | None] | None = None,
) -> NestedMV2Agent:
    register_secret_env_names(
        {config.api_key_env, config.fallback_api_key_env, config.api_auth_token_env}
    )
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    specs = load_layer_specs(config.layer_config_path) if config.layer_config_path else None
    active_state = state or AgentStateStore(config.state_path)
    memory = build_memory_system(
        config.backend,
        config.memory_dir,
        specs=specs,
        ledger=PromotionLedger(active_state),
        max_file_bytes=config.memory_max_layer_bytes,
    )
    llm = build_llm_provider(config, secret_resolver=secret_resolver)
    base_registry: ToolRegistry = tools or build_default_tools(config.enabled_tools)
    # Wrap with transparent retry layer for transient failures
    registry: ToolRegistry
    if config.tool_retry_max_attempts > 0:
        registry = RetryingRegistry(
            base_registry,
            max_attempts=config.tool_retry_max_attempts,
            backoff_base_seconds=config.tool_retry_backoff_base_seconds,
        )
    else:
        registry = base_registry
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
