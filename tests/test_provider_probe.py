from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.model_catalog import (
    MAX_MODEL_CATALOG_BYTES,
    MAX_MODEL_ID_CHARS,
    MAX_PROVIDER_HTTP_REQUEST_BYTES,
    ProviderModelCatalog,
    _encode_provider_http_request,
    _fetch_json,
    model_catalog_for_provider,
)
from nested_memvid_agent.llm.provider_http_worker import (
    MAX_PROVIDER_HTTP_REQUEST_BYTES as WORKER_MAX_PROVIDER_HTTP_REQUEST_BYTES,
)
from nested_memvid_agent.llm.provider_http_worker import (
    _read_request_bytes,
)
from nested_memvid_agent.provider_probe import (
    CapabilityEvidence,
    ModelProbeObservation,
    ProviderProbeService,
    _post_bytes,
    _valid_generation,
    _valid_stream,
    _valid_structured_output,
    _valid_tool_call,
    routing_constraint_presets,
)
from nested_memvid_agent.routing.models import ProviderProfile


def _profile(*, secret_ref: str | None = None) -> ProviderProfile:
    return ProviderProfile(
        profile_id="local",
        display_name="Local models",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        secret_ref=secret_ref,
        locality="local",
    )


def test_probe_service_combines_catalog_declarations_with_observed_evidence() -> None:
    catalog = ProviderModelCatalog(
        provider="openai-compatible",
        models=("qwen-coder",),
        fallback_models=("local-model",),
        source="provider",
        ok=True,
        fetchable=True,
        fetched_at="2026-07-28T12:00:00+00:00",
        catalog_complete=True,
        declared_capabilities={"qwen-coder": ("vision",)},
    )

    class Backend:
        def probe(
            self,
            profile: ProviderProfile,
            model: str,
            *,
            timeout_seconds: float,
        ) -> ModelProbeObservation:
            assert profile.profile_id == "local"
            assert model == "qwen-coder"
            assert timeout_seconds == 2.0
            return ModelProbeObservation(
                model=model,
                model_identity="qwen-coder-v2",
                latency_ms=18.25,
                capabilities=(
                    CapabilityEvidence.observed_pass("generation"),
                    CapabilityEvidence.observed_pass("streaming"),
                    CapabilityEvidence.observed_pass("structured_output"),
                    CapabilityEvidence.observed_pass("tools"),
                ),
            )

    service = ProviderProbeService(
        catalog_loader=lambda _profile, _timeout: catalog,
        probe_backend=Backend(),
        clock=lambda: datetime(2026, 7, 28, 12, 1, tzinfo=UTC),
    )

    result = service.discover(
        _profile(),
        max_models=2,
        timeout_seconds=2.0,
        probe_capabilities=True,
    )

    assert result.catalog_digest == catalog.digest
    assert result.catalog_fetched_at == "2026-07-28T12:00:00+00:00"
    assert result.probed_at == "2026-07-28T12:01:00+00:00"
    assert len(result.models) == 1
    model = result.models[0]
    assert model.model == "qwen-coder"
    assert model.model_identity == "qwen-coder-v2"
    assert model.latency_ms == 18.25
    evidence = {item.capability: item for item in model.capabilities}
    assert evidence["generation"].provenance == "observed"
    assert evidence["generation"].status == "pass"
    assert evidence["vision"].supported is True
    assert evidence["vision"].provenance == "provider_declared"


def test_probe_service_does_not_treat_fallback_catalog_as_discovery() -> None:
    catalog = ProviderModelCatalog(
        provider="openai-compatible",
        models=("hard-coded-fallback",),
        fallback_models=("hard-coded-fallback",),
        source="fallback",
        ok=False,
        fetchable=True,
        error="request failed with key=raw-secret-value",
    )
    service = ProviderProbeService(
        catalog_loader=lambda _profile, _timeout: catalog,
        secret_resolver=lambda _ref: "raw-secret-value",
    )

    result = service.discover(
        _profile(secret_ref="secret://local"),
        max_models=1,
        timeout_seconds=1.0,
        probe_capabilities=False,
    )

    assert result.models == ()
    assert result.catalog_ok is False
    assert "raw-secret-value" not in str(result.to_public_payload())
    assert "<redacted>" in str(result.to_public_payload())


def test_probe_backend_failure_is_redacted_and_keeps_capabilities_unknown() -> None:
    catalog = ProviderModelCatalog(
        provider="openai-compatible",
        models=("qwen-coder",),
        fallback_models=(),
        source="provider",
        ok=True,
        fetchable=True,
        fetched_at="2026-07-28T12:00:00+00:00",
        catalog_complete=True,
    )

    class Backend:
        def probe(
            self,
            profile: ProviderProfile,
            model: str,
            *,
            timeout_seconds: float,
        ) -> ModelProbeObservation:
            del profile, model, timeout_seconds
            raise RuntimeError("authorization raw-secret-value rejected")

    service = ProviderProbeService(
        catalog_loader=lambda _profile, _timeout: catalog,
        probe_backend=Backend(),
        secret_resolver=lambda _ref: "raw-secret-value",
    )
    result = service.discover(
        _profile(secret_ref="secret://local"),
        max_models=1,
        timeout_seconds=1.0,
        probe_capabilities=True,
    )

    payload = result.to_public_payload()
    assert "raw-secret-value" not in str(payload)
    assert payload["models"][0]["capabilities"][0]["status"] == "fail"
    assert payload["models"][0]["capabilities"][0]["supported"] is None


def test_routing_presets_are_filter_or_rank_only() -> None:
    presets = routing_constraint_presets()

    assert {preset.preset_id for preset in presets} == {
        "balanced",
        "cheapest-validated",
        "fastest",
        "frontier-review",
        "local-only",
        "privacy-first",
    }
    assert all(preset.effect == "filter_or_rank_only" for preset in presets)
    assert all(preset.can_enable_targets is False for preset in presets)
    assert all(preset.can_change_trust is False for preset in presets)


def test_operator_supplied_capability_is_explicitly_provenanced() -> None:
    evidence = CapabilityEvidence.operator_supplied("tools", supported=True)

    assert evidence.supported is True
    assert evidence.provenance == "operator_supplied"
    assert evidence.status == "pass"


def test_live_catalog_preserves_explicit_provider_capability_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_json(_url: str, **_kwargs: Any) -> Any:
        return {
            "data": [
                {
                    "id": "vision-coder",
                    "capabilities": ["streaming", "tools"],
                    "architecture": {"input_modalities": ["text", "image"]},
                    "supported_parameters": ["response_format"],
                }
            ]
        }

    monkeypatch.setattr("nested_memvid_agent.llm.model_catalog._fetch_json", fake_fetch_json)

    catalog = model_catalog_for_provider(
        AgentConfig(
            provider="openai-compatible",
            model="vision-coder",
            base_url="http://127.0.0.1:1234/v1",
        ),
        "openai-compatible",
    )

    assert catalog.declared_capabilities["vision-coder"] == (
        "streaming",
        "structured_output",
        "tools",
        "vision",
    )


def test_catalog_response_body_is_bounded() -> None:
    class OversizedBodyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"x" * (MAX_MODEL_CATALOG_BYTES + 1)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                return

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    with _serve(OversizedBodyHandler) as base_url:
        with pytest.raises(ValueError, match="byte limit"):
            _fetch_json(
                f"{base_url}/models",
                timeout_seconds=2,
                api_key=None,
            )


def test_live_catalog_drops_unbounded_or_control_character_model_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_json(_url: str, **_kwargs: Any) -> Any:
        return {
            "data": [
                {"id": "valid/model:1"},
                {"id": "bad\nmodel"},
                {"id": "x" * (MAX_MODEL_ID_CHARS + 1)},
            ]
        }

    monkeypatch.setattr("nested_memvid_agent.llm.model_catalog._fetch_json", fake_fetch_json)
    catalog = model_catalog_for_provider(
        AgentConfig(
            provider="openai-compatible",
            model="valid/model:1",
            base_url="http://127.0.0.1:1234/v1",
        ),
        "openai-compatible",
    )

    assert catalog.models == ("valid/model:1",)
    assert catalog.catalog_complete is False
    assert catalog.catalog_truncated is True


def test_catalog_marks_paginated_or_capped_inventory_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_json(_url: str, **_kwargs: Any) -> Any:
        return {
            "data": [{"id": f"model-{index}"} for index in range(2049)],
            "has_more": True,
            "total": 3000,
        }

    monkeypatch.setattr("nested_memvid_agent.llm.model_catalog._fetch_json", fake_fetch_json)
    catalog = model_catalog_for_provider(
        AgentConfig(
            provider="openai-compatible",
            model="model-0",
            base_url="http://127.0.0.1:1234/v1",
        ),
        "openai-compatible",
    )

    assert len(catalog.models) == 2048
    assert catalog.catalog_complete is False
    assert catalog.catalog_truncated is True
    assert catalog.reported_model_count == 3000
    assert catalog.to_public_dict()["catalog_complete"] is False


def test_capability_validators_require_exact_meaningful_evidence() -> None:
    assert _valid_generation({"choices": [{"message": {"content": " OK "}}]}) is True
    assert _valid_generation({"choices": [{"message": {"content": "  "}}]}) is False
    assert _valid_generation({"choices": [{"message": {"content": None}}]}) is False

    valid_sse = (
        b'data: {"choices":[{"delta":{"content":"O"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    assert _valid_stream(valid_sse) is True
    assert _valid_stream(b'data: {"error":{"message":"denied"}}\n\n') is False
    assert _valid_stream(b"data: [DONE]\n\n") is False
    assert _valid_stream(b"data: not-json\n\n") is False

    assert (
        _valid_structured_output(
            {"choices": [{"message": {"content": '{"ok": true}'}}]}
        )
        is True
    )
    assert (
        _valid_structured_output(
            {"choices": [{"message": {"content": '{"different": true}'}}]}
        )
        is False
    )

    valid_tool = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "kestrel_probe",
                                "arguments": '{"value":"ok"}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    assert _valid_tool_call(valid_tool) is True
    wrong_name = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "other_tool",
                                "arguments": '{"value":"ok"}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    assert _valid_tool_call(wrong_name) is False
    invalid_arguments = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "kestrel_probe",
                                "arguments": '{"value":"wrong"}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    assert _valid_tool_call(invalid_arguments) is False


def _slow_body_handler(*, stop: Event) -> type[BaseHTTPRequestHandler]:
    class SlowBodyHandler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                while not stop.wait(timeout=0.01):
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except OSError:
                return

        def do_GET(self) -> None:  # noqa: N802
            self._respond()

        def do_POST(self) -> None:  # noqa: N802
            self._respond()

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    return SlowBodyHandler


def test_catalog_read_enforces_monotonic_deadline_during_slow_drip() -> None:
    stop = Event()
    try:
        with _serve(_slow_body_handler(stop=stop)) as base_url:
            with pytest.raises(TimeoutError, match="deadline"):
                _fetch_json(
                    f"{base_url}/models",
                    timeout_seconds=0.25,
                    api_key=None,
                )
    finally:
        stop.set()


def test_probe_read_enforces_monotonic_deadline_during_slow_drip() -> None:
    stop = Event()
    try:
        with _serve(_slow_body_handler(stop=stop)) as base_url:
            with pytest.raises(TimeoutError, match="deadline"):
                _post_bytes(
                    f"{base_url}/chat/completions",
                    {"model": "local"},
                    timeout_seconds=0.25,
                    secret=None,
                )
    finally:
        stop.set()


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _SlowHeaderHandler(BaseHTTPRequestHandler):
    def _respond(self) -> None:
        try:
            self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Kestrel-Slow: ")
            for value in b"deadline-must-cover-headers":
                self.connection.sendall(bytes((value,)))
                sleep(0.03)
            self.connection.sendall(
                b"\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
            )
        except OSError:
            # The bounded client is expected to close the connection while this
            # adversarial peer is still trickling headers.
            return

    def do_GET(self) -> None:  # noqa: N802
        self._respond()

    def do_POST(self) -> None:  # noqa: N802
        self._respond()

    def log_message(self, _format: str, *_args: object) -> None:
        return None


def _saturating_slow_header_handler(
    *,
    stop: Event,
    started: Event,
) -> type[BaseHTTPRequestHandler]:
    class SaturatingSlowHeaderHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthy":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"recovered":true}')
                return
            started.set()
            try:
                self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Kestrel-Slow: ")
                while not stop.wait(timeout=0.01):
                    self.connection.sendall(b"x")
            except OSError:
                return

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    return SaturatingSlowHeaderHandler


def test_slow_header_timeouts_release_transport_capacity_for_recovery() -> None:
    stop = Event()
    started = Event()
    handler = _saturating_slow_header_handler(stop=stop, started=started)
    try:
        with _serve(handler) as base_url:
            def slow_request(_index: int) -> str:
                try:
                    _fetch_json(
                        f"{base_url}/slow",
                        timeout_seconds=1.0,
                        api_key=None,
                    )
                except TimeoutError as exc:
                    return str(exc)
                return "unexpected_success"

            with ThreadPoolExecutor(max_workers=16) as executor:
                outcomes = list(executor.map(slow_request, range(16)))

            assert started.wait(timeout=2.0)
            assert outcomes == ["provider response deadline exceeded"] * 16
            assert _fetch_json(
                f"{base_url}/healthy",
                timeout_seconds=2.0,
                api_key=None,
            ) == {"recovered": True}
    finally:
        stop.set()


def test_provider_transport_request_envelope_is_capped_on_both_sides() -> None:
    from io import BytesIO
    from urllib.request import Request

    assert WORKER_MAX_PROVIDER_HTTP_REQUEST_BYTES == MAX_PROVIDER_HTTP_REQUEST_BYTES
    oversized = Request(
        "http://127.0.0.1:1234/v1/chat/completions",
        data=b"x" * MAX_PROVIDER_HTTP_REQUEST_BYTES,
        method="POST",
    )
    with pytest.raises(ValueError, match="request exceeds"):
        _encode_provider_http_request(
            oversized,
            timeout_seconds=1.0,
            max_bytes=1024,
            error_max_bytes=1024,
        )
    with pytest.raises(ValueError, match="request exceeds"):
        _read_request_bytes(BytesIO(b"x" * (MAX_PROVIDER_HTTP_REQUEST_BYTES + 1)))


def test_catalog_wall_deadline_covers_slow_response_headers() -> None:
    with _serve(_SlowHeaderHandler) as base_url:
        started = monotonic()
        with pytest.raises(TimeoutError, match="deadline"):
            _fetch_json(
                f"{base_url}/models",
                timeout_seconds=0.25,
                api_key=None,
            )
        elapsed = monotonic() - started

    assert elapsed < 0.75


def test_probe_wall_deadline_covers_slow_response_headers() -> None:
    with _serve(_SlowHeaderHandler) as base_url:
        started = monotonic()
        with pytest.raises(TimeoutError, match="deadline"):
            _post_bytes(
                f"{base_url}/chat/completions",
                {"model": "local"},
                timeout_seconds=0.25,
                secret=None,
            )
        elapsed = monotonic() - started

    assert elapsed < 0.75


def test_catalog_redirect_never_forwards_authorization_cross_host() -> None:
    captured_authorization: list[str | None] = []

    class CaptureHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            captured_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"stolen"}]}')

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    with _serve(CaptureHandler) as capture_url:
        cross_host_url = capture_url.replace("127.0.0.1", "localhost")

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"{cross_host_url}/models")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return None

        with _serve(RedirectHandler) as redirect_url:
            with pytest.raises(RuntimeError, match="HTTP 302") as raised:
                _fetch_json(
                    f"{redirect_url}/models",
                    timeout_seconds=1,
                    api_key="redirect-secret-value",
                )

    assert captured_authorization == []
    assert "redirect-secret-value" not in str(raised.value)


def test_probe_redirect_never_forwards_authorization_cross_host() -> None:
    captured_authorization: list[str | None] = []

    class CaptureHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            captured_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"stolen"}}]}')

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    with _serve(CaptureHandler) as capture_url:
        cross_host_url = capture_url.replace("127.0.0.1", "localhost")

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.send_response(307)
                self.send_header("Location", f"{cross_host_url}/chat/completions")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return None

        with _serve(RedirectHandler) as redirect_url:
            with pytest.raises(RuntimeError, match="HTTP 307") as raised:
                _post_bytes(
                    f"{redirect_url}/chat/completions",
                    {"model": "local"},
                    timeout_seconds=1,
                    secret="redirect-secret-value",
                )

    assert captured_authorization == []
    assert "redirect-secret-value" not in str(raised.value)
