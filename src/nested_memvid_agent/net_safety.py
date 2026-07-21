from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock
from typing import Any, cast
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request

_LOCAL_HOST_SUFFIXES = (".localhost", ".local")
_METADATA_HOSTS = {
    "metadata",
    "metadata.google.internal",
}
_DNS_PIN_LOCK = RLock()


def public_url_allowed(url: str, *, require_https: bool = False) -> tuple[bool, str]:
    """Return whether a URL is safe for outbound public-network requests."""
    try:
        resolve_public_addresses(url, require_https=require_https)
    except ValueError as exc:
        return False, str(exc)
    return True, ""


def resolve_public_addresses(
    url: str,
    *,
    require_https: bool = False,
) -> frozenset[str]:
    """Resolve and validate every address used by a public-network request."""

    parsed = urlparse(url)
    if require_https:
        if parsed.scheme != "https":
            raise ValueError("Only https:// URLs are allowed.")
    elif parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http:// and https:// URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with credentials are not allowed.")
    try:
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"Invalid URL host: {exc}") from exc
    if not host:
        raise ValueError("URL must include a host.")
    lowered = _normalized_host(host)
    if lowered == "localhost" or lowered.endswith(_LOCAL_HOST_SUFFIXES):
        raise ValueError("Local hostnames are not allowed.")
    if lowered in _METADATA_HOSTS:
        raise ValueError("Cloud metadata hostnames are not allowed.")
    try:
        literal_ip = ipaddress.ip_address(lowered)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        allowed, reason = public_ip_allowed(literal_ip)
        if not allowed:
            raise ValueError(reason)
        return frozenset({str(literal_ip)})
    try:
        infos = socket.getaddrinfo(
            lowered,
            None,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise ValueError(f"Unable to resolve host: {exc}") from exc
    addresses: set[str] = set()
    for info in infos:
        if not info or not info[4]:
            continue
        address = str(info[4][0])
        try:
            normalized = str(ipaddress.ip_address(address))
        except ValueError as exc:
            raise ValueError(f"Resolved invalid IP address: {address}") from exc
        allowed, reason = public_ip_allowed(ipaddress.ip_address(normalized))
        if not allowed:
            raise ValueError(reason)
        addresses.add(normalized)
    if not addresses:
        raise ValueError("Unable to resolve host to a public IP address.")
    return frozenset(addresses)


@contextmanager
def pin_host_resolution(host: str, vetted_addresses: frozenset[str]) -> Iterator[None]:
    """Keep a connection on the exact public addresses vetted before opening it."""

    if not vetted_addresses:
        raise ValueError("At least one vetted public IP address is required.")
    normalized_host = _normalized_host(host)
    with _DNS_PIN_LOCK:
        original_getaddrinfo = socket.getaddrinfo

        def pinned_getaddrinfo(
            target_host: str,
            port: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            lowered = _normalized_host(str(target_host))
            results = original_getaddrinfo(target_host, port, *args, **kwargs)
            if lowered != normalized_host:
                return results
            filtered = []
            for info in results:
                if not info or not info[4]:
                    continue
                try:
                    address = str(ipaddress.ip_address(str(info[4][0])))
                except ValueError:
                    continue
                if address in vetted_addresses:
                    filtered.append(info)
            if not filtered:
                raise OSError(f"Host resolution changed for {target_host}.")
            return filtered

        socket.getaddrinfo = cast(Any, pinned_getaddrinfo)
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


class NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects before a vetted outbound request can change targets."""

    def __init__(self, message: str = "Redirects are not allowed.") -> None:
        super().__init__()
        self.message = message

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        raise ValueError(self.message)


def _normalized_host(host: str) -> str:
    normalized = host.lower().rstrip(".")
    if not normalized:
        raise ValueError("URL must include a valid host.")
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("URL host has invalid internationalized encoding.") from exc


def public_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str]:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False, "Private, local, link-local, multicast, reserved, and unspecified addresses are not allowed."
    return True, ""
