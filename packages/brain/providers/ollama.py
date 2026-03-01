from __future__ import annotations
"""
Ollama LLM provider — HTTP-based adapter for locally running Ollama models.

Ollama exposes an OpenAI-compatible API at http://localhost:11434.
This provider supports streaming, tool calling, and model discovery.
"""

import json
import logging
import os
import time
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger("brain.providers.ollama")

# Configurable base URL — defaults to standard Ollama address
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")

# Default model to use when none specified
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen3:8b")

# Default context window — set higher for agent workflows
OLLAMA_CONTEXT_LENGTH = int(os.getenv("OLLAMA_CONTEXT_LENGTH", "16384"))

# Health check cache (avoid hammering Ollama every call)
_health_cache: dict[str, float] = {"ready": False, "checked_at": 0}
_HEALTH_TTL = 30  # seconds


class OllamaProvider:
    """
    Wrapper around Ollama's HTTP API for local LLM inference.

    Supports:
      - Streaming chat completions
      - Non-streaming generation
      - Tool calling (native Ollama function calling)
      - Model discovery (list installed models)
    """

    def __init__(self, base_url: str = ""):
        self._base_url = (base_url or OLLAMA_HOST).rstrip("/")
        self._last_response = ""
        self._models_cache: list[dict] = []
        self._models_cached_at = 0.0

    # ── Health / Ready ────────────────────────────────────────────────

    def is_ready(self) -> bool:
        """Check if Ollama is reachable (cached for 30s)."""
        now = time.time()
        if now - _health_cache.get("checked_at", 0) < _HEALTH_TTL:
            return bool(_health_cache.get("ready"))

        try:
            resp = httpx.get(f"{self._base_url}/api/tags", timeout=3)
            ready = resp.status_code == 200
        except Exception:
            ready = False

        _health_cache["ready"] = ready
        _health_cache["checked_at"] = now
        if not ready:
            logger.debug("Ollama health check failed")
        return ready

    @property
    def last_response(self) -> str:
        return self._last_response

    # ── Model Discovery ──────────────────────────────────────────────

    async def list_models(self) -> list[dict]:
        """Return installed Ollama models with metadata."""
        now = time.time()
        if now - self._models_cached_at < 60 and self._models_cache:
            return self._models_cache

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                models = data.get("models", [])
                self._models_cache = [
                    {
                        "id": m.get("name", ""),
                        "name": m.get("name", "").split(":")[0],
                        "size": m.get("size", 0),
                        "parameter_size": m.get("details", {}).get("parameter_size", ""),
                        "quantization": m.get("details", {}).get("quantization_level", ""),
                        "family": m.get("details", {}).get("family", ""),
                    }
                    for m in models
                ]
                self._models_cached_at = now
                return self._models_cache
        except Exception as e:
            logger.warning(f"Failed to list Ollama models: {e}")
            return self._models_cache or []

    # ── Streaming ────────────────────────────────────────────────────

    async def stream(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        api_key: str = "",  # unused, kept for interface compat
    ) -> AsyncIterator[str]:
        """Stream tokens from an Ollama model."""
        model = model or OLLAMA_DEFAULT_MODEL
        self._last_response = ""

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": OLLAMA_CONTEXT_LENGTH,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST", f"{self._base_url}/api/chat", json=payload
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                self._last_response += token
                                yield token
                            if chunk.get("done"):
                                return
                        except json.JSONDecodeError:
                            continue
        except httpx.ConnectError:
            yield f"[Error: Cannot connect to Ollama at {self._base_url}]"
        except Exception as e:
            logger.error(f"Ollama stream error: {e}")
            yield f"[Error: {e}]"

    # ── Non-Streaming Generate ───────────────────────────────────────

    async def generate(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """Non-streaming chat completion."""
        result: list[str] = []
        async for token in self.stream(messages, model, temperature, max_tokens):
            result.append(token)
        return "".join(result)

    # ── Tool Calling ─────────────────────────────────────────────────

    async def generate_with_tools(
        self,
        messages: list[dict],
        model: str = "",
        tools: list[dict] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        api_key: str = "",  # unused, kept for interface compat
    ) -> dict:
        """
        Generate a response with optional tool/function calling.

        Ollama supports native tool calling via the /api/chat endpoint.
        Tools should be in OpenAI function schema format — we convert
        them to Ollama's expected format.
        """
        model = model or OLLAMA_DEFAULT_MODEL

        # ── Sanitize messages for Ollama ─────────────────────────────
        # Strip non-standard keys that other providers add (e.g.
        # _gemini_raw_part, turn_id, _attachments) and fix content: None.
        clean_messages = []
        ALLOWED_MSG_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name", "images"}
        ALLOWED_TC_KEYS = {"id", "type", "function"}
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in ALLOWED_MSG_KEYS}
            # Ollama doesn't accept content=None (assistant with tool_calls)
            if clean.get("content") is None:
                clean["content"] = ""
            # Sanitize tool_calls within assistant messages
            if "tool_calls" in clean and clean["tool_calls"]:
                clean_tcs = []
                for tc in clean["tool_calls"]:
                    clean_tcs.append({k: v for k, v in tc.items() if k in ALLOWED_TC_KEYS})
                clean["tool_calls"] = clean_tcs
            clean_messages.append(clean)

        payload: dict = {
            "model": model,
            "messages": clean_messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": OLLAMA_CONTEXT_LENGTH,
            },
        }

        # Convert OpenAI-style tool schemas to Ollama format
        ollama_tools = []
        if tools:
            for tool in tools:
                # Handle both {type: function, function: {...}} and raw function schemas
                if isinstance(tool, dict):
                    if "type" in tool and "function" in tool:
                        ollama_tools.append(tool)
                    else:
                        ollama_tools.append({
                            "type": "function",
                            "function": tool,
                        })
            if ollama_tools:
                payload["tools"] = ollama_tools

        try:
            # Short timeout for tool calls (quick failover to cloud),
            # longer for plain chat (ollama can be slow but still cheaper).
            timeout = 30 if ollama_tools else 120
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                )
                if resp.status_code != 200:
                    try:
                        err_body = resp.text[:500]
                    except Exception:
                        err_body = "(no body)"
                    logger.error(
                        f"Ollama API {resp.status_code}: {err_body}\n"
                        f"  model={model}, messages={len(clean_messages)}, tools={len(ollama_tools)}"
                    )
                resp.raise_for_status()
                data = resp.json()

            message = data.get("message", {})
            content = message.get("content", "")
            self._last_response = content

            # Extract tool calls from Ollama response
            raw_tool_calls = message.get("tool_calls", [])
            tool_calls = []
            for i, tc in enumerate(raw_tool_calls):
                fn = tc.get("function", {})
                tool_calls.append({
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": json.dumps(fn.get("arguments", {}))
                            if isinstance(fn.get("arguments"), dict)
                            else fn.get("arguments", "{}"),
                    },
                })

            return {
                "content": content,
                "tool_calls": tool_calls,
            }

        except httpx.ConnectError:
            logger.error(f"Cannot connect to Ollama at {self._base_url}")
            raise
        except Exception as e:
            logger.error(f"Ollama generate_with_tools failed: {e}")
            raise
