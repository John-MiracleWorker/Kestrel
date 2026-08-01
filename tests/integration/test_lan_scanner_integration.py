from __future__ import annotations

import os

import pytest

if os.environ.get("RUN_LAN_DISCOVERY_INTEGRATION") != "1":
    pytest.skip(
        "set RUN_LAN_DISCOVERY_INTEGRATION=1 for controlled same-host LAN evidence",
        allow_module_level=True,
    )

import http.server
import ipaddress
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from nested_memvid_agent.lan_discovery_models import KNOWN_MODEL_SERVICE_PORTS, LanScanLimits
from nested_memvid_agent.lan_discovery_scope import (
    PrivateScanScope,
    enumerate_private_interfaces,
)
from nested_memvid_agent.lan_scanner import (
    ApiShape,
    CapabilityObservationStatus,
    LanFailureCategory,
    Reachability,
    TransportSecurity,
    scan_lan_scope,
)


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    mode = "catalog"
    request_counts: dict[str, int] = {}
    counter_lock = threading.Lock()

    def _count(self) -> None:
        with type(self).counter_lock:
            mode = type(self).mode
            type(self).request_counts[mode] = type(self).request_counts.get(mode, 0) + 1

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self._count()
        if self.path != "/api/tags":
            self.send_error(404)
            return
        if type(self).mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://8.8.8.8/models")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = (
            b"x" * (256 * 1024 + 1)
            if type(self).mode == "oversize"
            else b'{"models":[{"name":"controlled-model"}]}'
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        self._count()
        if self.path != "/api/generate":
            self.send_error(404)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length))
        assert payload["model"] == "controlled-model"
        body = b'{"done":true,"response":"OK"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ExactServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def _controlled_scope() -> tuple[PrivateScanScope, str]:
    for interface in enumerate_private_interfaces():
        for value in interface.addresses:
            attached = ipaddress.ip_interface(value)
            if isinstance(attached, ipaddress.IPv4Interface) and not attached.ip.is_loopback:
                literal = str(attached.ip)
                return PrivateScanScope.from_request(interface, f"{literal}/32"), literal
    pytest.skip("no eligible non-loopback private IPv4 interface is available")


@contextmanager
def _exclusive_four_port_fixture(literal: str) -> Iterator[tuple[int, ExactServer]]:
    held: dict[int, socket.socket] = {}
    try:
        for port in KNOWN_MODEL_SERVICE_PORTS:
            descriptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                descriptor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                descriptor.bind((literal, port))
            except OSError:
                descriptor.close()
                pytest.skip(
                    "all four known ports are not exclusively available on the exact private literal"
                )
            held[port] = descriptor

        listening_port = 11434
        server = ExactServer((literal, listening_port), FixtureHandler, bind_and_activate=False)
        server.socket.close()
        server.socket = held[listening_port]
        server.server_address = server.socket.getsockname()
        server.server_name = literal
        server.server_port = listening_port
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield listening_port, server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)
            held.pop(listening_port, None)
    finally:
        for descriptor in held.values():
            descriptor.close()


def _matching_observation(scope: PrivateScanScope, port: int):
    observations = scan_lan_scope(scope, LanScanLimits())
    assert len(observations) == 4
    reachable = [item for item in observations if item.reachability is Reachability.REACHABLE]
    assert len(reachable) == 1
    assert reachable[0].endpoint.port == port
    return reachable[0]


def test_exact_private_fixture_success_redirect_and_oversize_are_fail_closed() -> None:
    scope, literal = _controlled_scope()
    FixtureHandler.request_counts = {}
    with _exclusive_four_port_fixture(literal) as (port, _server):
        FixtureHandler.mode = "catalog"
        success = _matching_observation(scope, port)
        assert success.endpoint.address == literal
        assert success.failure_category is None
        assert success.api_shape is ApiShape.OLLAMA_COMPATIBLE
        assert success.transport_security is TransportSecurity.PLAIN_HTTP
        assert success.catalog == ("controlled-model",)
        assert success.capabilities[0].status is CapabilityObservationStatus.OBSERVED_PASS
        assert FixtureHandler.request_counts["catalog"] == 2

        FixtureHandler.mode = "redirect"
        redirect = _matching_observation(scope, port)
        assert redirect.failure_category is LanFailureCategory.REDIRECT_REJECTED
        assert redirect.api_shape is None
        assert FixtureHandler.request_counts["redirect"] == 1

        FixtureHandler.mode = "oversize"
        oversize = _matching_observation(scope, port)
        assert oversize.failure_category is LanFailureCategory.RESPONSE_TOO_LARGE
        assert oversize.api_shape is None
        assert FixtureHandler.request_counts["oversize"] == 1
