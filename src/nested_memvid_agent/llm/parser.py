from __future__ import annotations

import json
import re
from typing import Any

from ..runtime_models import LLMResponse, ToolCall

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_agent_response(text: str) -> LLMResponse:
    """Parse the agent-control JSON envelope, falling back to plain chat text.

    Expected optional envelope:
    {
      "message": "user-visible text",
      "tool_calls": [{"name": "memory.search", "arguments": {"query": "..."}}]
    }
    """
    payload = _extract_json(text)
    if not isinstance(payload, dict):
        return LLMResponse(content=text.strip())

    message = str(payload.get("message", "")).strip()
    calls_raw = payload.get("tool_calls", [])
    calls: list[ToolCall] = []
    if isinstance(calls_raw, list):
        for item in calls_raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            args = item.get("arguments", {})
            if isinstance(name, str) and isinstance(args, dict):
                calls.append(ToolCall(name=name, arguments=dict(args)))
    if not message and not calls:
        return LLMResponse(content=text.strip())
    return LLMResponse(content=message, tool_calls=tuple(calls), raw=payload)


def _extract_json(text: str) -> Any | None:
    stripped = text.strip()
    candidates = [stripped]
    match = _JSON_BLOCK_RE.search(stripped)
    if match:
        candidates.insert(0, match.group(1))
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None
