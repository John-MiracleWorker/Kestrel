from __future__ import annotations

from ..config import AgentConfig
from .base import FallbackLLMProvider, LLMProvider
from .codex_cli_provider import CodexCLIProvider
from .mock import MockLLMProvider
from .openai_compatible_provider import OpenAICompatibleProvider
from .openai_provider import OpenAIResponsesProvider


def build_llm_provider(config: AgentConfig) -> LLMProvider:
    provider = _build_single_provider(config, provider=config.provider, model=config.model, base_url=config.base_url, api_key_env=config.api_key_env)
    if config.fallback_provider:
        fallback = _build_single_provider(
            config,
            provider=config.fallback_provider,
            model=config.fallback_model or config.model,
            base_url=config.fallback_base_url,
            api_key_env=config.fallback_api_key_env or config.api_key_env,
        )
        return FallbackLLMProvider(provider, fallback)
    return provider


def _build_single_provider(
    config: AgentConfig,
    *,
    provider: str,
    model: str,
    base_url: str | None,
    api_key_env: str | None,
) -> LLMProvider:
    if provider == "mock":
        return MockLLMProvider()
    if provider == "openai":
        return OpenAIResponsesProvider(
            model=model,
            api_key_env=api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "openai-compatible":
        if not base_url:
            raise ValueError("openai-compatible provider requires base_url")
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "codex-cli":
        return CodexCLIProvider(
            model=model,
            workspace=config.workspace,
            sandbox=config.codex_sandbox,
            profile=config.codex_profile,
            skip_git_repo_check=config.codex_skip_git_repo_check,
            ephemeral=config.codex_ephemeral,
        )
    raise ValueError(f"Unsupported provider: {provider}")
