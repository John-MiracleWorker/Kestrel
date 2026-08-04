from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from nested_memvid_agent.lan_discovery_models import ResolvedLanEndpoint

DEFAULT_OLLAMA_OPENAI_BASE_URL = "http://localhost:11434/v1"


def format_numeric_http_authority(endpoint: ResolvedLanEndpoint) -> str:
    """Format a numeric HTTP authority without accepting names or userinfo.

    LAN discovery uses this only for its internally constructed ``Host`` header;
    it is deliberately not a general provider URL helper.
    """

    from nested_memvid_agent.lan_discovery_models import ResolvedLanEndpoint

    if type(endpoint) is not ResolvedLanEndpoint:
        raise TypeError("HTTP authority requires an authenticated LAN endpoint")
    address = endpoint.address
    port = endpoint.port
    if type(address) is not str or "%" in address:
        raise ValueError("LAN endpoint requires an unzoned literal IP address")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ValueError("LAN endpoint requires a literal IP address") from exc
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("LAN endpoint requires a valid numeric port")
    literal = str(parsed)
    if isinstance(parsed, ipaddress.IPv6Address):
        return f"[{literal}]:{port}"
    return f"{literal}:{port}"


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

    candidate = validate_provider_http_url(base_url or DEFAULT_OLLAMA_OPENAI_BASE_URL).rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.path in {"", "/"}:
        return urlunsplit(parsed._replace(path="/v1"))
    return candidate
