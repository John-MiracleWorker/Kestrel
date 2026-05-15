from __future__ import annotations

from ..config import AgentConfig
from .base import LLMProvider
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
    raise ValueError(f"Unsupported provider: {config.provider}")
