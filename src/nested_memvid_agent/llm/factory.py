from __future__ import annotations

from ..config import AgentConfig
from .anthropic_provider import AnthropicMessagesProvider
from .base import FallbackLLMProvider, LLMProvider
from .codex_cli_provider import CodexCLIProvider
from .gemini_provider import GeminiProvider
from .mock import MockLLMProvider
from .ollama_provider import OllamaNativeProvider
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
    if provider == "openrouter":
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://openrouter.ai/api/v1",
            api_key_env=api_key_env or "OPENROUTER_API_KEY",
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="openrouter",
        )
    if provider == "deepseek":
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://api.deepseek.com",
            api_key_env=api_key_env or "DEEPSEEK_API_KEY",
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="deepseek",
        )
    if provider == "kimi":
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://api.moonshot.ai/v1",
            api_key_env=api_key_env or "MOONSHOT_API_KEY",
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="kimi",
        )
    if provider == "ollama":
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "http://localhost:11434/v1",
            api_key="ollama",
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="ollama",
        )
    if provider == "ollama-cloud":
        return OllamaNativeProvider(
            model=model,
            base_url=base_url or "https://ollama.com/api",
            api_key_env=api_key_env or "OLLAMA_API_KEY",
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "anthropic":
        return AnthropicMessagesProvider(
            model=model,
            api_key_env=api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "gemini":
        return GeminiProvider(
            model=model,
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
