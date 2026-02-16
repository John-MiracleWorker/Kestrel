"""
Libre Bird — Cloud LLM Providers
Optional integration with Gemini, Claude, and OpenAI APIs.

⚠️  PRIVACY WARNING: Using cloud providers sends your messages to external servers.
This bypasses Libre Bird's privacy-first design. Use only if you understand the tradeoff.
"""

import json
import logging
import os
from typing import AsyncIterator, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger("libre_bird.cloud")


# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------

PROVIDERS = {
    "gemini": {
        "name": "gemini",
        "display_name": "Google Gemini",
        "icon": "✨",
        "models": [
            "gemini-3-flash-preview",
            "gemini-3-pro-preview",
            "gemini-2.5-flash-lite",
        ],
        "default_model": "gemini-3-flash-preview",
        "env_key": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
    "openai": {
        "name": "openai",
        "display_name": "OpenAI",
        "icon": "🤖",
        "models": [
            "gpt-5-nano",
            "gpt-5-mini",
            "gpt-5.2",
            "gpt-5.2-pro",
        ],
        "default_model": "gpt-5-nano",
        "env_key": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
    },
    "claude": {
        "name": "claude",
        "display_name": "Anthropic Claude",
        "icon": "🧠",
        "models": [
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-opus-4-5",
            "claude-opus-4-6",
        ],
        "default_model": "claude-haiku-4-5",
        "env_key": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com/v1",
    },
}


# ---------------------------------------------------------------------------
# API key management (stored in .env alongside the project)
# ---------------------------------------------------------------------------

_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def _load_env():
    """Load .env file into a dict."""
    env = {}
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _save_env(env: dict):
    """Write env dict back to .env, preserving comments."""
    lines = []
    existing_keys = set()

    # Preserve comments and update existing keys
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    existing_keys.add(key)
                    if key in env:
                        lines.append(f"{key}={env[key]}\n")
                    else:
                        lines.append(line)
                else:
                    lines.append(line)

    # Add new keys
    for key, value in env.items():
        if key not in existing_keys:
            lines.append(f"{key}={value}\n")

    with open(_ENV_PATH, "w") as f:
        f.writelines(lines)


def get_api_key(provider: str) -> Optional[str]:
    """Get API key for a provider, checking env vars then .env file."""
    provider_info = PROVIDERS.get(provider)
    if not provider_info:
        return None

    env_key = provider_info["env_key"]

    # Check environment variable first
    key = os.environ.get(env_key)
    if key:
        return key

    # Check .env file
    env = _load_env()
    return env.get(env_key)


def set_api_key(provider: str, api_key: str):
    """Save an API key for a provider to the .env file."""
    provider_info = PROVIDERS.get(provider)
    if not provider_info:
        raise ValueError(f"Unknown provider: {provider}")

    env_key = provider_info["env_key"]

    # Update .env file
    env = _load_env()
    if api_key:
        env[env_key] = api_key
        os.environ[env_key] = api_key
    else:
        env.pop(env_key, None)
        os.environ.pop(env_key, None)

    _save_env(env)
    logger.info(f"API key {'set' if api_key else 'removed'} for {provider}")


def remove_api_key(provider: str):
    """Remove an API key for a provider."""
    set_api_key(provider, "")


def list_providers() -> list:
    """List all providers with their configured status."""
    result = []
    for name, info in PROVIDERS.items():
        key = get_api_key(name)
        result.append({
            "name": name,
            "display_name": info["display_name"],
            "icon": info["icon"],
            "models": info["models"],
            "default_model": info["default_model"],
            "configured": bool(key),
            "key_preview": f"...{key[-4:]}" if key and len(key) > 4 else None,
        })
    return result


# ---------------------------------------------------------------------------
# Chat completion via cloud providers
# ---------------------------------------------------------------------------

def _http_json(url: str, data: dict, headers: dict, timeout: int = 60) -> dict:
    """Make an HTTP POST request and return JSON response."""
    body = json.dumps(data).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Cloud API error {e.code}: {error_body}")
        raise RuntimeError(f"API error ({e.code}): {error_body[:200]}")
    except URLError as e:
        raise RuntimeError(f"Connection failed: {e.reason}")


async def cloud_chat(provider: str, model: str, messages: list,
                     temperature: float = 0.7, max_tokens: int = 2048) -> str:
    """Send a chat completion request to a cloud provider. Returns full text."""
    full = []
    async for token in cloud_chat_stream(provider, model, messages, temperature, max_tokens):
        full.append(token)
    return "".join(full)


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

def _http_stream(url: str, data: dict, headers: dict, timeout: int = 120):
    """Make an HTTP POST and yield raw bytes line-by-line (for SSE / streaming)."""
    body = json.dumps(data).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urlopen(req, timeout=timeout)
        for line in resp:
            yield line
        resp.close()
    except HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Cloud API stream error {e.code}: {error_body}")
        raise RuntimeError(f"API error ({e.code}): {error_body[:200]}")
    except URLError as e:
        raise RuntimeError(f"Connection failed: {e.reason}")


def _gemini_stream(api_key: str, model: str, messages: list,
                   temperature: float, max_tokens: int):
    """Gemini streaming — yields text tokens."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:streamGenerateContent?alt=sse&key={api_key}")

    contents = []
    system_instruction = None
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_instruction = msg["content"]
            continue
        gemini_role = "user" if role == "user" else "model"
        contents.append({
            "role": gemini_role,
            "parts": [{"text": msg["content"]}]
        })

    data = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
    }
    if system_instruction:
        data["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    headers = {"Content-Type": "application/json"}

    for line in _http_stream(url, data, headers):
        text = line.decode("utf-8", errors="replace").strip()
        if not text or not text.startswith("data: "):
            continue
        payload = text[6:]
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            parts = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            for part in parts:
                t = part.get("text", "")
                if t:
                    yield t
        except (json.JSONDecodeError, KeyError, IndexError):
            continue


def _openai_stream(api_key: str, model: str, messages: list,
                   temperature: float, max_tokens: int):
    """OpenAI streaming — yields text tokens."""
    url = "https://api.openai.com/v1/chat/completions"

    data = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for line in _http_stream(url, data, headers):
        text = line.decode("utf-8", errors="replace").strip()
        if not text or not text.startswith("data: "):
            continue
        payload = text[6:]
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            delta = chunk["choices"][0].get("delta", {})
            content = delta.get("content", "")
            if content:
                yield content
        except (json.JSONDecodeError, KeyError, IndexError):
            continue


def _claude_stream(api_key: str, model: str, messages: list,
                   temperature: float, max_tokens: int):
    """Claude streaming — yields text tokens."""
    url = "https://api.anthropic.com/v1/messages"

    system_text = ""
    chat_messages = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        else:
            chat_messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })

    data = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": chat_messages,
        "stream": True,
    }
    if system_text:
        data["system"] = system_text

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    for line in _http_stream(url, data, headers):
        text = line.decode("utf-8", errors="replace").strip()
        if not text or not text.startswith("data: "):
            continue
        payload = text[6:]
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            if chunk.get("type") == "content_block_delta":
                delta = chunk.get("delta", {})
                t = delta.get("text", "")
                if t:
                    yield t
        except (json.JSONDecodeError, KeyError):
            continue


async def cloud_chat_stream(provider: str, model: str, messages: list,
                            temperature: float = 0.7, max_tokens: int = 2048):
    """Async generator that yields text tokens from a cloud provider."""
    import asyncio
    import queue
    import threading

    api_key = get_api_key(provider)
    if not api_key:
        raise RuntimeError(
            f"No API key configured for {provider}. "
            "Add it in Settings → Cloud Providers."
        )

    providers_map = {
        "gemini": _gemini_stream,
        "openai": _openai_stream,
        "claude": _claude_stream,
    }

    stream_fn = providers_map.get(provider)
    if not stream_fn:
        raise RuntimeError(f"Unknown provider: {provider}")

    # Run the synchronous streaming generator in a thread and relay tokens
    # via an asyncio-safe queue so we don't block the event loop.
    q: queue.Queue = queue.Queue()
    _SENTINEL = object()

    def _run():
        try:
            for token in stream_fn(api_key, model, messages, temperature, max_tokens):
                q.put(token)
        except Exception as exc:
            q.put(exc)
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    loop = asyncio.get_event_loop()
    while True:
        item = await loop.run_in_executor(None, q.get)
        if item is _SENTINEL:
            break
        if isinstance(item, Exception):
            raise item
        yield item

