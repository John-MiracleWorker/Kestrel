from __future__ import annotations

import json
import re
import socket
from contextlib import contextmanager
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..net_safety import public_url_allowed
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from .base import AgentTool, ToolContext


class WebSearchTool(AgentTool):
    spec = ToolSpec(
        name="web.search",
        description="Search the public web for read-only outside context. Disabled unless allow_web is enabled.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
        risk="medium",
        capabilities=("web", "outside-context", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        max_results = max(1, min(int(arguments.get("max_results", context.config.web_max_results)), 10))
        try:
            results = _mock_search_results(query, max_results) if context.config.web_backend == "mock" else _direct_web_search(query, context, max_results)
        except Exception as exc:  # noqa: BLE001 - web boundary
            return self._result(call, success=False, content=str(exc), error="web_search_failed")
        payload = {"query": query, "backend": context.config.web_backend, "results": results}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class WebFetchTool(AgentTool):
    spec = ToolSpec(
        name="web.fetch",
        description="Fetch a public HTTP(S) page for read-only outside context. Private and local network URLs are rejected.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 1000000},
            },
            "required": ["url"],
        },
        risk="medium",
        capabilities=("web", "outside-context", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        url = str(arguments.get("url", "")).strip()
        if not url:
            return self._result(call, success=False, content="Missing url", error="missing_url")
        max_bytes = max(1024, min(int(arguments.get("max_bytes", context.config.web_max_bytes)), 1_000_000))
        parsed = urlparse(url)
        if context.config.web_backend == "mock" and parsed.hostname == "mock.kestrel.local":
            content = _mock_fetch_content(url)
            payload = {"url": url, "backend": "mock", "bytes": len(content.encode("utf-8")), "citation": url}
            return self._result(call, success=True, content=content, data=payload)
        safe, reason = _public_web_url_allowed(url)
        if not safe:
            return self._result(call, success=False, content=reason, error="unsafe_url")
        try:
            content, final_url = _fetch_public_text(url, timeout=context.config.web_timeout_seconds, max_bytes=max_bytes)
        except Exception as exc:  # noqa: BLE001 - web boundary
            return self._result(call, success=False, content=str(exc), error="web_fetch_failed")
        payload = {"url": final_url, "backend": context.config.web_backend, "bytes": len(content.encode("utf-8")), "citation": final_url}
        return self._result(call, success=True, content=content, data=payload)


def _mock_search_results(query: str, max_results: int) -> list[dict[str, Any]]:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "query"
    return [
        {
            "title": f"Mock web result {index + 1}: {query}",
            "url": f"https://mock.kestrel.local/search/{slug}/{index + 1}",
            "snippet": f"Deterministic outside context for {query}.",
            "source": "mock",
            "citation": f"https://mock.kestrel.local/search/{slug}/{index + 1}",
        }
        for index in range(max_results)
    ]


def _mock_fetch_content(url: str) -> str:
    return f"Mock web page for Kestrel\nURL: {url}\nThis deterministic page supplies outside context without network access."


def _direct_web_search(query: str, context: ToolContext, max_results: int) -> list[dict[str, Any]]:
    search_url = "https://duckduckgo.com/html/?" + urlencode({"q": query})
    html, final_url = _fetch_public_text(search_url, timeout=context.config.web_timeout_seconds, max_bytes=context.config.web_max_bytes)
    del final_url
    results: list[dict[str, Any]] = []
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(html):
        href = unescape(match.group(1))
        title = _strip_html(match.group(2))
        url = _unwrap_duckduckgo_url(href)
        safe, _reason = _public_web_url_allowed(url)
        if not safe:
            continue
        results.append({"title": title, "url": url, "snippet": "", "source": "duckduckgo", "citation": url})
        if len(results) >= max_results:
            break
    return results


def _fetch_public_text(url: str, *, timeout: int, max_bytes: int) -> tuple[str, str]:
    vetted_addresses = _resolve_public_addresses(url)
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("URL must include a host.")
    request = Request(url, headers={"User-Agent": "Kestrel/0.1 (+local-first-agent)"})
    opener = build_opener(_NoRedirectHandler())
    with _pin_host_resolution(parsed.hostname, vetted_addresses):
        with opener.open(request, timeout=max(timeout, 1)) as response:  # nosec
            raw = response.read(max_bytes + 1)
            final_url = str(response.geturl())
            if len(raw) > max_bytes:
                raw = raw[:max_bytes]
            encoding = response.headers.get_content_charset() or "utf-8"
    safe_final, final_reason = _public_web_url_allowed(final_url)
    if not safe_final:
        raise ValueError(final_reason)
    return raw.decode(encoding, errors="replace"), final_url


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:  # noqa: ANN401
        del req, fp, code, msg, headers, newurl
        raise ValueError("Redirects are not allowed for web.fetch.")


def _resolve_public_addresses(url: str) -> set[str]:
    safe, reason = _public_web_url_allowed(url)
    if not safe:
        raise ValueError(reason)
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("URL must include a host.")
    host = parsed.hostname.lower().rstrip(".")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return set()
    return {str(info[4][0]) for info in infos if info and info[4]}


@contextmanager
def _pin_host_resolution(host: str, vetted_addresses: set[str]):
    if not vetted_addresses:
        yield
        return
    original_getaddrinfo = socket.getaddrinfo
    normalized_host = host.lower().rstrip(".")

    def _pinned_getaddrinfo(target_host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
        lowered = str(target_host).lower().rstrip(".")
        if lowered != normalized_host:
            return original_getaddrinfo(target_host, port, *args, **kwargs)
        results = original_getaddrinfo(target_host, port, *args, **kwargs)
        filtered = [info for info in results if info[4] and str(info[4][0]) in vetted_addresses]
        if not filtered:
            raise OSError(f"Host resolution changed for {target_host}.")
        return filtered

    socket.getaddrinfo = _pinned_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def _public_web_url_allowed(url: str) -> tuple[bool, str]:
    return public_url_allowed(url)


def _unwrap_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        values = parse_qs(parsed.query).get("uddg")
        if values:
            return values[0]
    return url


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", unescape(value))).strip()
