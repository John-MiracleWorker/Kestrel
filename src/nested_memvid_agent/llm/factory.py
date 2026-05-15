from __future__ import annotations

from ..config import AgentConfig
from .base import LLMProvider
from .mock import MockLLMProvider
from .openai_provider import OpenAIResponsesProvider


def build_llm_provider(config: AgentConfig) -> LLMProvider:
    if config.provider == "mock":
        return MockLLMProvider()
    if config.provider == "openai":
        return OpenAIResponsesProvider(model=config.model)
    raise ValueError(f"Unsupported provider: {config.provider}")
