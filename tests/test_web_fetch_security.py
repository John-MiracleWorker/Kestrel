from __future__ import annotations

import pytest

from nested_memvid_agent.tools import web_tools


class _FakeHeaders:
    def get_content_charset(self) -> str:
        return "utf-8"


class _FakeResponse:
    def __init__(self, body: bytes, final_url: str) -> None:
        self._body = body
        self._final_url = final_url
        self.headers = _FakeHeaders()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def read(self, _size: int) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._final_url


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def open(self, _request: object, timeout: int) -> _FakeResponse:
        del timeout
        web_tools.socket.getaddrinfo("example.com", 443)
        return self._response


def test_fetch_rejects_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_tools, "_resolve_public_addresses", lambda _url: {"93.184.216.34"})
    monkeypatch.setattr(web_tools, "build_opener", lambda *_: (_ for _ in ()).throw(ValueError("Redirects are not allowed for web.fetch.")))

    with pytest.raises(ValueError, match="Redirects are not allowed"):
        web_tools._fetch_public_text("https://example.com", timeout=2, max_bytes=1024)


def test_fetch_pins_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_tools, "_resolve_public_addresses", lambda _url: {"93.184.216.34"})
    monkeypatch.setattr(web_tools, "_public_web_url_allowed", lambda _url: (True, ""))
    monkeypatch.setattr(web_tools, "build_opener", lambda *_: _FakeOpener(_FakeResponse(b"ok", "https://example.com")))

    calls: list[str] = []

    def tracking_getaddrinfo(host: str, port: object, *args: object, **kwargs: object):
        del args, kwargs
        calls.append(host)
        return [
            (
                web_tools.socket.AF_INET,
                web_tools.socket.SOCK_STREAM,
                web_tools.socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", int(port or 443)),
            )
        ]

    monkeypatch.setattr(web_tools.socket, "getaddrinfo", tracking_getaddrinfo)

    text, final_url = web_tools._fetch_public_text("https://example.com", timeout=2, max_bytes=1024)

    assert text == "ok"
    assert final_url == "https://example.com"
    assert "example.com" in calls
