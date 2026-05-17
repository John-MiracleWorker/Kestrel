from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_LOCAL_HOST_SUFFIXES = (".localhost", ".local")
_METADATA_HOSTS = {
    "metadata",
    "metadata.google.internal",
}


def public_url_allowed(url: str, *, require_https: bool = False) -> tuple[bool, str]:
    """Return whether a URL is safe for outbound public-network requests."""
    parsed = urlparse(url)
    if require_https:
        if parsed.scheme != "https":
            return False, "Only https:// URLs are allowed."
    elif parsed.scheme not in {"http", "https"}:
        return False, "Only http:// and https:// URLs are allowed."
    if parsed.username or parsed.password:
        return False, "URLs with credentials are not allowed."
    host = parsed.hostname
    if not host:
        return False, "URL must include a host."
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(_LOCAL_HOST_SUFFIXES):
        return False, "Local hostnames are not allowed."
    if lowered in _METADATA_HOSTS:
        return False, "Cloud metadata hostnames are not allowed."
    try:
        ip = ipaddress.ip_address(lowered)
        return public_ip_allowed(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(lowered, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return False, f"Unable to resolve host: {exc}"
    for info in infos:
        address = info[4][0]
        try:
            allowed, reason = public_ip_allowed(ipaddress.ip_address(address))
        except ValueError:
            return False, f"Resolved invalid IP address: {address}"
        if not allowed:
            return False, reason
    return True, ""


def public_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str]:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False, "Private, local, link-local, multicast, reserved, and unspecified addresses are not allowed."
    return True, ""
