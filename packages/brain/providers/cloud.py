"""
Cloud LLM provider — Unified adapter for OpenAI, Anthropic, Google Gemini.
Mirrors the streaming interface from cloud_providers.py in the monolithic app.
"""

import os
import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger("brain.providers.cloud")

# ── API Keys ──────────────────────────────────────────────────────────
PROVIDER_CONFIGS = {
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "default_model": os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5-mini"),
        "base_url": "https://api.openai.com/v1/chat/completions",
    },
    "anthropic": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "default_model": os.getenv("ANTHROPIC_DEFAULT_MODEL", "claude-haiku-4-5"),
        "base_url": "https://api.anthropic.com/v1/messages",
    },
    "google": {
        "api_key_env": "GOOGLE_API_KEY",
        "default_model": os.getenv("GOOGLE_DEFAULT_MODEL", "gemini-3.1-pro"),
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models",
    },
}

# ── Model Catalog (current as of Feb 2026) ────────────────────────────
# Use this for model discovery via CLI (`kestrel models`) or API.
MODEL_CATALOG = {
    "openai": {
        "flagship": [
            {"id": "gpt-5.2",          "ctx": "128k", "desc": "Most capable — enterprise knowledge work, complex reasoning"},
            {"id": "gpt-5.1",          "ctx": "128k", "desc": "Core dev model — conversation stability, long-context reasoning"},
            {"id": "gpt-5",            "ctx": "128k", "desc": "Developer-focused — coding + agentic tasks"},
        ],
        "efficient": [
            {"id": "gpt-5-mini",       "ctx": "128k", "desc": "Fast + cheap — well-defined tasks (default)"},
            {"id": "gpt-5-nano",       "ctx": "128k", "desc": "Ultra-fast — rapid tasks, edge deployment"},
        ],
        "coding": [
            {"id": "gpt-5.3-codex",       "ctx": "128k", "desc": "Most capable agentic coding model"},
            {"id": "gpt-5.3-codex-spark", "ctx": "128k", "desc": "Low-latency coding — real-time editing, prototyping"},
        ],
        "legacy": [
            {"id": "gpt-4.1",         "ctx": "128k", "desc": "Previous gen — still available via API"},
            {"id": "gpt-4.1-mini",    "ctx": "128k", "desc": "Previous gen compact"},
        ],
    },
    "anthropic": {
        "flagship": [
            {"id": "claude-opus-4-6",   "ctx": "200k", "desc": "Most intelligent — complex tasks, sustained agentic work"},
            {"id": "claude-sonnet-4-5", "ctx": "200k", "desc": "Best all-around — coding, agents, cost-efficient (recommended)"},
        ],
        "efficient": [
            {"id": "claude-haiku-4-5",  "ctx": "200k", "desc": "Fastest + cheapest — quick responses (default)"},
        ],
    },
    "google": {
        "flagship": [
            {"id": "gemini-3.1-pro",          "ctx": "2M",   "desc": "Multimodal flagship — deep reasoning, rich visuals (ARC-AGI-2 winner)"},
            {"id": "gemini-3-deep-think",     "ctx": "1M",   "desc": "Specialized reasoning — science, research, engineering"},
            {"id": "gemini-3-pro",            "ctx": "1M",   "desc": "Previous flagship — still highly capable"},
        ],
        "efficient": [
            {"id": "gemini-3-flash",          "ctx": "1M",   "desc": "Speed-optimized — price-performance leader"},
            {"id": "gemini-2.5-flash",        "ctx": "1M",   "desc": "Stable workhorse — high-volume, audio output"},
            {"id": "gemini-2.5-flash-lite",   "ctx": "1M",   "desc": "Ultra-cheap — high-throughput services"},
        ],
    },
}


class CloudProvider:
    """Unified cloud LLM provider with streaming support."""

    def __init__(self, provider_name: str):
        self.provider = provider_name
        self._config = PROVIDER_CONFIGS.get(provider_name)
        if not self._config:
            raise ValueError(f"Unknown cloud provider: {provider_name}")

        self._api_key = os.getenv(self._config["api_key_env"], "")
        self._last_response = ""

    def is_ready(self) -> bool:
        return bool(self._api_key)

    @property
    def last_response(self) -> str:
        return self._last_response

    async def stream(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        api_key: str = "",
    ) -> AsyncIterator[str]:
        """Stream tokens from the cloud provider."""
        model = model or self._config["default_model"]
        self._last_response = ""

        # Use provided key or fallback to env var
        request_key = api_key or self._api_key
        if not request_key:
             yield f"[Error: No API key found for {self.provider}]"
             return

        if self.provider == "openai":
            async for token in self._stream_openai(messages, model, temperature, max_tokens, request_key):
                self._last_response += token
                yield token
        elif self.provider == "anthropic":
            async for token in self._stream_anthropic(messages, model, temperature, max_tokens, request_key):
                self._last_response += token
                yield token
        elif self.provider == "google":
            async for token in self._stream_google(messages, model, temperature, max_tokens, request_key):
                self._last_response += token
                yield token

    async def _stream_openai(self, messages, model, temperature, max_tokens, api_key: str) -> AsyncIterator[str]:
        """OpenAI-compatible streaming."""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", self._config["base_url"], json=payload, headers=headers
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        token = delta.get("content", "")
                        if token:
                            yield token
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def _stream_anthropic(self, messages, model, temperature, max_tokens, api_key: str) -> AsyncIterator[str]:
        """Anthropic Claude streaming."""
        # Extract system message
        system = ""
        chat_msgs = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                chat_msgs.append(msg)

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_msgs,
            "stream": True,
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", self._config["base_url"], json=payload, headers=headers
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    try:
                        chunk = json.loads(data)
                        if chunk.get("type") == "content_block_delta":
                            token = chunk.get("delta", {}).get("text", "")
                            if token:
                                yield token
                    except (json.JSONDecodeError, KeyError):
                        continue

    async def _stream_google(self, messages, model, temperature, max_tokens, api_key: str) -> AsyncIterator[str]:
        """Google Gemini streaming."""
        # Convert messages to Gemini format
        contents = []
        system_instruction = ""
        for msg in messages:
            if msg["role"] == "system":
                system_instruction = msg["content"]
            else:
                role = "user" if msg["role"] == "user" else "model"
                contents.append({
                    "role": role,
                    "parts": [{"text": msg["content"]}],
                })

        url = f"{self._config['base_url']}/{model}:streamGenerateContent?key={api_key}&alt=sse"
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                   error_body = await resp.aread()
                   logger.error(f"Google API Error: {error_body.decode('utf-8')}")
                   resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    try:
                        chunk = json.loads(data)
                        parts = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        for part in parts:
                            token = part.get("text", "")
                            if token:
                                yield token
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def list_models(self, api_key: str = "") -> list[dict]:
        """List available models from the provider."""
        request_key = api_key or self._api_key
        if not request_key:
            return []

        if self.provider == "openai":
            return await self._list_openai_models(request_key)
        elif self.provider == "anthropic":
            return self._list_anthropic_models() # Anthropic has no public list endpoint yet
        elif self.provider == "google":
            return await self._list_google_models(request_key)
        return []

    async def _list_openai_models(self, api_key: str) -> list[dict]:
        headers = {
            "Authorization": f"Bearer {api_key}",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get("https://api.openai.com/v1/models", headers=headers)
                resp.raise_for_status()
                data = resp.json()
                models = []
                for m in data.get("data", []):
                    # Filter for chat models to avoid clutter
                    if "gpt" in m["id"] or "o1" in m["id"]:
                        models.append({
                            "id": m["id"],
                            "name": m["id"],
                            "context_window": "128k" # Placeholder, strict context not in list endpoint
                        })
                return sorted(models, key=lambda x: x["id"], reverse=True)
            except Exception as e:
                logger.error(f"Failed to list OpenAI models: {e}")
                return []

    def _list_anthropic_models(self) -> list[dict]:
        # Return hardcoded list as Anthropic doesn't have a simple list endpoint purely for models
        # Updated for 2026 availability
        return [
            {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "context_window": "200k"},
            {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5", "context_window": "200k"},
            {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "context_window": "200k"},
        ]

    async def _list_google_models(self, api_key: str) -> list[dict]:
        if not api_key:
            return []
        
        # Hardcoded list of 2026 Gemini models to ensure availability
        # The API might be versioned or restricted, so we prioritize these
        common_models = [
            {"id": "gemini-3.1-pro", "name": "Gemini 3.1 Pro", "context_window": "2M"},
            {"id": "gemini-3-deep-think", "name": "Gemini 3 Deep Think", "context_window": "1M"},
            {"id": "gemini-3-pro", "name": "Gemini 3 Pro", "context_window": "1M"},
            {"id": "gemini-3-flash", "name": "Gemini 3 Flash", "context_window": "1M"},
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "context_window": "1M"},
            {"id": "gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash Lite", "context_window": "1M"},
        ]

        try:
            # We still try to fetch from API to get any new/custom ones, but we'll merge
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    api_models = []
                    for m in data.get("models", []):
                        # Filter for generation models
                        if "generateContent" in m.get("supportedGenerationMethods", []):
                            name_id = m["name"].split("/")[-1]
                            # Avoid duplicates from hardcoded list
                            if not any(cm["id"] == name_id for cm in common_models):
                                api_models.append({
                                    "id": name_id,
                                    "name": m.get("displayName", name_id),
                                    "context_window": str(m.get("inputTokenLimit", "Unknown"))
                                })
                    return common_models + api_models
                else:
                    logging.warning(f"Failed to fetch Google models: {resp.status_code} - {resp.text}")
                    return common_models
        except Exception as e:
            logging.error(f"Error fetching Google models: {e}")
            return common_models

    async def generate(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        result = []
        async for token in self.stream(messages, model, temperature, max_tokens):
            result.append(token)
        return "".join(result)

    async def generate_with_tools(
        self,
        messages: list[dict],
        model: str = "",
        tools: list[dict] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        api_key: str = "",
    ) -> dict:
        """
        Call the LLM with function/tool calling support.
        Returns a dict with 'content' (str) and/or 'tool_calls' (list).
        """
        model = model or self._config["default_model"]
        request_key = api_key or self._api_key
        if not request_key:
            return {"content": f"[Error: No API key for {self.provider}]", "tool_calls": []}

        if self.provider == "openai":
            return await self._generate_with_tools_openai(messages, model, tools or [], temperature, max_tokens, request_key)
        elif self.provider == "anthropic":
            return await self._generate_with_tools_anthropic(messages, model, tools or [], temperature, max_tokens, request_key)
        elif self.provider == "google":
            return await self._generate_with_tools_google(messages, model, tools or [], temperature, max_tokens, request_key)
        return {"content": "", "tool_calls": []}

    async def _generate_with_tools_openai(self, messages, model, tools, temperature, max_tokens, api_key):
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(self._config["base_url"], json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            choice = data.get("choices", [{}])[0].get("message", {})
            return {
                "content": choice.get("content") or "",
                "tool_calls": choice.get("tool_calls") or [],
            }

    async def _generate_with_tools_anthropic(self, messages, model, tools, temperature, max_tokens, api_key):
        system = ""
        chat_msgs = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                chat_msgs.append(msg)

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        # Convert OpenAI tool schema to Anthropic format
        anthropic_tools = [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_msgs,
        }
        if system:
            payload["system"] = system
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(self._config["base_url"], json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Convert Anthropic response to OpenAI-style format
        tool_calls = []
        content_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                content_text += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", "call_1"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        return {"content": content_text, "tool_calls": tool_calls}

    async def _generate_with_tools_google(self, messages, model, tools, temperature, max_tokens, api_key):
        contents = []
        system_instruction = ""
        for msg in messages:
            if msg["role"] == "system":
                system_instruction = msg["content"]
            elif msg.get("tool_calls"):
                parts = [
                    {"functionCall": {"name": tc["function"]["name"], "args": json.loads(tc["function"]["arguments"])}}
                    for tc in msg["tool_calls"]
                ]
                contents.append({"role": "model", "parts": parts})
            elif msg["role"] == "tool":
                contents.append({"role": "user", "parts": [{"functionResponse": {
                    "name": "tool_result",
                    "response": {"result": msg.get("content", "")},
                }}]})
            else:
                role = "user" if msg["role"] == "user" else "model"
                contents.append({"role": role, "parts": [{"text": msg.get("content") or ""}]})

        url = f"{self._config['base_url']}/{model}:generateContent?key={api_key}"
        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t.get("name"), "description": t.get("description", ""),
                 "parameters": t.get("parameters", {})}
                for t in tools
            ]}]

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        content_text = ""
        tool_calls = []
        for part in parts:
            if "text" in part:
                content_text += part["text"]
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append({
                    "id": f"call_{fc.get('name', '')}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {})),
                    },
                })

        return {"content": content_text, "tool_calls": tool_calls}
