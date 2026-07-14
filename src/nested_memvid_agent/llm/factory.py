from __future__ import annotations

from collections.abc import Callable

from ..config import AgentConfig
from .anthropic_provider import AnthropicMessagesProvider
from .base import FallbackLLMProvider, LLMProvider
from .codex_cli_provider import CodexCLIProvider
from .gemini_provider import GeminiProvider
from .mock import MockLLMProvider
from .ollama_provider import OllamaNativeProvider
from .openai_compatible_provider import OpenAICompatibleProvider
from .openai_provider import OpenAIResponsesProvider

SecretResolver = Callable[[str | None], str | None]


def build_llm_provider(config: AgentConfig, *, secret_resolver: SecretResolver | None = None) -> LLMProvider:
    provider = _build_single_provider(
        config,
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        api_key_env=config.api_key_env,
        secret_resolver=secret_resolver,
    )
    if config.fallback_provider:
        fallback = _build_single_provider(
            config,
            provider=config.fallback_provider,
            model=config.fallback_model or config.model,
            base_url=config.fallback_base_url,
            api_key_env=config.fallback_api_key_env or config.api_key_env,
            secret_resolver=secret_resolver,
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
    secret_resolver: SecretResolver | None = None,
) -> LLMProvider:
    if provider == "mock":
        return MockLLMProvider()
    if provider == "openai":
        active_api_key_env = api_key_env or "OPENAI_API_KEY"
        return OpenAIResponsesProvider(
            model=model,
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "lm-studio":
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "http://localhost:1234/v1",
            api_key="lm-studio",
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="lm-studio",
        )
    if provider == "openai-compatible":
        if not base_url:
            raise ValueError("openai-compatible provider requires base_url")
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url,
            api_key=_resolve_secret(secret_resolver, api_key_env),
            api_key_env=api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "openrouter":
        active_api_key_env = api_key_env or "OPENROUTER_API_KEY"
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://openrouter.ai/api/v1",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="openrouter",
        )
    if provider == "deepseek":
        active_api_key_env = api_key_env or "DEEPSEEK_API_KEY"
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://api.deepseek.com",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="deepseek",
        )
    if provider == "kimi":
        active_api_key_env = api_key_env or "MOONSHOT_API_KEY"
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://api.moonshot.ai/v1",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
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
        active_api_key_env = api_key_env or "OLLAMA_API_KEY"
        return OllamaNativeProvider(
            model=model,
            base_url=base_url or "https://ollama.com/api",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "anthropic":
        active_api_key_env = api_key_env or "ANTHROPIC_API_KEY"
        return AnthropicMessagesProvider(
            model=model,
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "grok":
        active_api_key_env = api_key_env or "XAI_API_KEY"
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://api.x.ai/v1",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="grok",
        )
    if provider == "gemini":
        active_api_key_env = api_key_env or "GEMINI_API_KEY"
        return GeminiProvider(
            model=model,
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
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


def _resolve_secret(secret_resolver: SecretResolver | None, name_or_ref: str | None) -> str | None:
    if secret_resolver is None or not name_or_ref:
        return None
    return secret_resolver(name_or_ref)
