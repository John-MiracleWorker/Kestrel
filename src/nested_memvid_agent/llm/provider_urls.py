from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

DEFAULT_OLLAMA_OPENAI_BASE_URL = "http://localhost:11434/v1"


def validate_provider_http_url(url: str) -> str:
    """Return a provider URL only when urllib can address it over HTTP(S)."""

    candidate = url.strip()
    if not candidate:
        raise ValueError("Provider URL must be a non-empty http:// or https:// URL.")
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Provider URL is malformed.") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Provider URL must use http:// or https://.")
    if not parsed.netloc or not hostname:
        raise ValueError("Provider URL must include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Provider URL must not embed credentials.")
    return candidate


def normalize_ollama_openai_base_url(base_url: str | None) -> str:
    """Accept an Ollama host root as well as its explicit OpenAI ``/v1`` base.

    Kestrel's local ``ollama`` adapter uses Ollama's OpenAI-compatible API. A
    host-only URL is a natural operator input, but the OpenAI client appends
    ``/chat/completions`` directly and therefore needs the ``/v1`` prefix.
    Custom reverse-proxy paths are left unchanged so Kestrel does not guess at
    their routing contract.
    """

    candidate = validate_provider_http_url(
        base_url or DEFAULT_OLLAMA_OPENAI_BASE_URL
    ).rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.path in {"", "/"}:
        return urlunsplit(parsed._replace(path="/v1"))
    return candidate
