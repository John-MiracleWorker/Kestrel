from __future__ import annotations
"""
LM Studio LLM provider — HTTP-based adapter for LM Studio local servers.

LM Studio exposes an OpenAI-compatible API at http://localhost:1234/v1.
This provider supports streaming, tool calling, and model discovery.
"""

import json
import logging
import os
import time
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger("brain.providers.lmstudio")


class LMStudioUnavailableError(Exception):
    """Raised when LM Studio is unreachable or times out.

    Callers should catch this and failover to a cloud provider.
    """
    pass


# Configurable base URL — if set, skips network scanning
_EXPLICIT_LMSTUDIO_HOST = os.getenv("LMSTUDIO_HOST", "")
_LMSTUDIO_PORT = int(os.getenv("LMSTUDIO_PORT", "1234"))

# Default fallback URL
LMSTUDIO_HOST = _EXPLICIT_LMSTUDIO_HOST or f"http://host.docker.internal:{_LMSTUDIO_PORT}"

# Default model — LM Studio auto-selects if empty
LMSTUDIO_DEFAULT_MODEL = os.getenv("LMSTUDIO_DEFAULT_MODEL", "")

# Context length
LMSTUDIO_CONTEXT_LENGTH = int(os.getenv("LMSTUDIO_CONTEXT_LENGTH", "16384"))

# Health check cache
_health_cache: dict[str, float] = {"ready": False, "checked_at": 0}
_HEALTH_TTL = 30  # seconds


class LMStudioProvider:
    """
    Wrapper around LM Studio's OpenAI-compatible HTTP API for local LLM inference.

    Supports:
      - Streaming chat completions
      - Non-streaming generation
      - Tool calling (OpenAI function calling format)
      - Model discovery (list loaded models)
    """

    provider = "lmstudio"  # provider identifier for the registry

    def __init__(self, base_url: str = ""):
        self._explicit_url = base_url or _EXPLICIT_LMSTUDIO_HOST
        self._base_url = (self._explicit_url or LMSTUDIO_HOST).rstrip("/")
        self._last_response = ""
        self._models_cache: list[dict] = []
        self._models_cached_at = 0.0

    @classmethod
    def start_discovery(cls) -> None:
        """Start background network scanning for LM Studio instances.

        Call this once at application startup. If LMSTUDIO_HOST is explicitly
        set, scanning is skipped. Otherwise kestrel will probe the LAN and
        pick the most capable LM Studio instance automatically.
        """
        try:
            from providers.lmstudio_discovery import lmstudio_discovery
            lmstudio_discovery.start_background_scanning()
        except Exception as e:
            logger.warning(f"Failed to start LM Studio discovery: {e}")

    async def _resolve_url(self) -> str:
        """Return the best LM Studio URL, using discovery if no explicit host set."""
        if self._explicit_url:
            return self._explicit_url.rstrip("/")
        try:
            from providers.lmstudio_discovery import lmstudio_discovery
            best = await lmstudio_discovery.get_best_host()
            if best and best != self._base_url:
                logger.info(f"LM Studio discovery: switching to best host {best}")
                self._base_url = best.rstrip("/")
        except Exception as e:
            logger.debug(f"LM Studio discovery resolution failed (using cached): {e}")
        return self._base_url

    # ── Health / Ready ────────────────────────────────────────────────

    def is_ready(self) -> bool:
        """Check if the currently selected LM Studio host is reachable (cached 30s)."""
        now = time.time()
        if now - _health_cache.get("checked_at", 0) < _HEALTH_TTL:
            return bool(_health_cache.get("ready"))

        try:
            from providers.lmstudio_discovery import lmstudio_discovery
            cached = lmstudio_discovery.get_cached_hosts()
            if cached and not self._explicit_url:
                check_url = cached[0]["url"]
            else:
                check_url = self._base_url
        except Exception:
            check_url = self._base_url

        try:
            resp = httpx.get(f"{check_url}/v1/models", timeout=3)
            ready = resp.status_code == 200
            if ready:
                data = resp.json()
                # LM Studio returns {"data": [...]} — need at least one model loaded
                ready = bool(data.get("data"))
            if ready and check_url != self._base_url:
                self._base_url = check_url
        except Exception:
            ready = False

        _health_cache["ready"] = ready
        _health_cache["checked_at"] = now
        if not ready:
            logger.debug(f"LM Studio health check failed for {check_url}")
        return ready

    @staticmethod
    def invalidate_health() -> None:
        """Force-clear the health cache so next call re-checks."""
        _health_cache["ready"] = False
        _health_cache["checked_at"] = 0

    @property
    def last_response(self) -> str:
        return self._last_response

    # ── Model Discovery ──────────────────────────────────────────────

    async def list_models(self) -> list[dict]:
        """Return loaded LM Studio models with metadata."""
        now = time.time()
        if now - self._models_cached_at < 60 and self._models_cache:
            return self._models_cache

        base_url = await self._resolve_url()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{base_url}/v1/models")
                resp.raise_for_status()
                data = resp.json()
                # OpenAI format: {"data": [{"id": "model-name", ...}, ...]}
                models = data.get("data", [])
                self._models_cache = [
                    {
                        "id": m.get("id", ""),
                        "name": m.get("id", "").split("/")[-1] if "/" in m.get("id", "") else m.get("id", ""),
                        "owned_by": m.get("owned_by", "lmstudio"),
                    }
                    for m in models
                ]
                self._models_cached_at = now
                return self._models_cache
        except Exception as e:
            logger.warning(f"Failed to list LM Studio models: {e}")
            return self._models_cache or []

    # ── Streaming ────────────────────────────────────────────────────

    async def stream(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        api_key: str = "",  # unused, kept for interface compat
    ) -> AsyncIterator[str]:
        """Stream tokens from an LM Studio model."""
        model = model or LMSTUDIO_DEFAULT_MODEL
        base_url = await self._resolve_url()
        self._last_response = ""

        payload = {
            "model": model or None,  # None = LM Studio auto-selects
            "messages": self._sanitize_messages(messages),
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST", f"{base_url}/v1/chat/completions", json=payload
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip() or not line.startswith("data: "):
                            continue
                        data_str = line[6:]  # strip "data: " prefix
                        if data_str == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                self._last_response += token
                                yield token
                        except (json.JSONDecodeError, IndexError):
                            continue
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.error(f"LM Studio rate limited (429): {e}")
                raise
            logger.error(f"LM Studio stream error: {e}")
            self.invalidate_health()
            raise LMStudioUnavailableError(f"LM Studio HTTP error: {e}") from e
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            logger.error(f"Cannot connect to LM Studio at {base_url}: {e}")
            self.invalidate_health()
            raise LMStudioUnavailableError(
                f"Cannot connect to LM Studio at {base_url}"
            ) from e
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            logger.error(f"LM Studio stream timeout for model {model}: {e}")
            self.invalidate_health()
            raise LMStudioUnavailableError(
                f"LM Studio stream timeout for model {model}"
            ) from e
        except LMStudioUnavailableError:
            raise
        except Exception as e:
            logger.error(f"LM Studio stream error: {e}")
            self.invalidate_health()
            raise LMStudioUnavailableError(f"LM Studio stream error: {e}") from e

    # ── Non-Streaming Generate ───────────────────────────────────────

    async def generate(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
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

        LM Studio supports OpenAI-compatible tool calling via
        /v1/chat/completions. Tools should be in OpenAI function schema format.
        """
        model = model or LMSTUDIO_DEFAULT_MODEL
        base_url = await self._resolve_url()

        clean_messages = self._sanitize_messages(messages)

        payload: dict = {
            "model": model or None,
            "messages": clean_messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Add tools in OpenAI format
        openai_tools = []
        if tools:
            for tool in tools:
                if isinstance(tool, dict):
                    if "type" in tool and "function" in tool:
                        openai_tools.append(tool)
                    else:
                        openai_tools.append({
                            "type": "function",
                            "function": tool,
                        })
            if openai_tools:
                payload["tools"] = openai_tools

        try:
            timeout = int(os.getenv("LMSTUDIO_TIMEOUT", "600"))
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base_url}/v1/chat/completions",
                    json=payload,
                )
                if resp.status_code != 200:
                    try:
                        err_body = resp.text[:500]
                    except Exception:
                        err_body = "(no body)"
                    logger.error(
                        f"LM Studio API {resp.status_code}: {err_body}\n"
                        f"  model={model}, messages={len(clean_messages)}, tools={len(openai_tools)}"
                    )
                resp.raise_for_status()
                data = resp.json()

            # Parse OpenAI-format response
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "") or ""
            self._last_response = content

            # Extract tool calls (already in OpenAI format)
            raw_tool_calls = message.get("tool_calls", [])
            tool_calls = []
            for tc in raw_tool_calls:
                fn = tc.get("function", {})
                tool_calls.append({
                    "id": tc.get("id", f"call_{len(tool_calls)}"),
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    },
                })

            return {
                "content": content,
                "tool_calls": tool_calls,
            }

        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            logger.error(f"Cannot connect to LM Studio at {base_url}")
            self.invalidate_health()
            raise LMStudioUnavailableError(
                f"Cannot connect to LM Studio at {base_url}"
            ) from e
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            logger.error(
                f"LM Studio timeout after {timeout}s "
                f"(model={model}, messages={len(clean_messages)}, tools={len(openai_tools)})"
            )
            self.invalidate_health()
            raise LMStudioUnavailableError(
                f"LM Studio timeout after {timeout}s for model {model}"
            ) from e
        except httpx.HTTPStatusError as e:
            logger.error(
                f"LM Studio generate_with_tools HTTP error: {e}\n"
                f"  model={model}, messages={len(clean_messages)}, tools={len(openai_tools)}"
            )
            if e.response.status_code == 429:
                raise
            self.invalidate_health()
            raise LMStudioUnavailableError(
                f"LM Studio HTTP {e.response.status_code} for model {model}"
            ) from e
        except LMStudioUnavailableError:
            raise
        except Exception as e:
            logger.error(
                f"LM Studio generate_with_tools failed: {type(e).__name__}: {e}\n"
                f"  model={model}, messages={len(clean_messages)}, tools={len(openai_tools)}"
            )
            self.invalidate_health()
            raise LMStudioUnavailableError(
                f"LM Studio failed: {type(e).__name__}: {e}"
            ) from e

    # ── Message Sanitization ─────────────────────────────────────────

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Strip non-standard keys for OpenAI-compatible API."""
        ALLOWED_MSG_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}
        clean_messages = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in ALLOWED_MSG_KEYS}
            # Ensure content is always a string (never None)
            if clean.get("content") is None:
                clean["content"] = ""
            clean_messages.append(clean)
        return clean_messages
