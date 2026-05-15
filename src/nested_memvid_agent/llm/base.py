from __future__ import annotations

from abc import ABC, abstractmethod

from ..runtime_models import ChatMessage, LLMResponse, ToolSpec


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        raise NotImplementedError
