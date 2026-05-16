from __future__ import annotations

from ..config import AgentConfig
from .base import LLMProvider
from .codex_cli_provider import CodexCLIProvider
from .mock import MockLLMProvider
from .openai_compatible_provider import OpenAICompatibleProvider
from .openai_provider import OpenAIResponsesProvider


def build_llm_provider(config: AgentConfig) -> LLMProvider:
    if config.provider == "mock":
        return MockLLMProvider()
    if config.provider == "openai":
        return OpenAIResponsesProvider(
            model=config.model,
            api_key_env=config.api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if config.provider == "openai-compatible":
        if not config.base_url:
            raise ValueError("openai-compatible provider requires base_url")
        return OpenAICompatibleProvider(
            model=config.model,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if config.provider == "codex-cli":
        return CodexCLIProvider(
            model=config.model,
            workspace=config.workspace,
            sandbox=config.codex_sandbox,
            profile=config.codex_profile,
            skip_git_repo_check=config.codex_skip_git_repo_check,
            ephemeral=config.codex_ephemeral,
        )
    raise ValueError(f"Unsupported provider: {config.provider}")
