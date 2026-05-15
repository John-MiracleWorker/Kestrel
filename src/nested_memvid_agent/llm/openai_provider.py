from __future__ import annotations

import os
from importlib import import_module
from typing import Any

from ..runtime_models import ChatMessage, LLMResponse, ToolSpec
from .base import LLMProvider
from .parser import parse_agent_response


class OpenAIResponsesProvider(LLMProvider):
    """Minimal OpenAI Responses API provider.

    This provider intentionally keeps tool calls in the agent-control JSON envelope so the
    runtime remains provider-portable. Codex can later upgrade this to native function
    calling, but this version is already chat-capable when `openai` is installed.
    """

    def __init__(self, model: str, api_key: str | None = None) -> None:
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")

    def generate(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        del tools
        try:
            openai_module = import_module("openai")
        except ImportError as exc:
            raise RuntimeError("Install the OpenAI SDK with `pip install openai`.") from exc

        OpenAI = openai_module.OpenAI
        client = OpenAI(api_key=self.api_key)
        input_payload: Any = [msg.to_openai_dict() for msg in messages]
        response = client.responses.create(model=self.model, input=input_payload)
        text = getattr(response, "output_text", None)
        if text is None:
            text = str(response)
        return parse_agent_response(text)
