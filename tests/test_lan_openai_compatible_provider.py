from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module

import pytest

from nested_memvid_agent.lan_discovery_models import (
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_runtime_authority import (
    LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    LanRuntimeAuthority,
    derive_lan_runtime_endpoint_binding_digest,
    derive_lan_runtime_provider_profile_id,
    derive_lan_runtime_target_id,
)
from nested_memvid_agent.llm.base import ProviderError
from nested_memvid_agent.llm.lan_openai_compatible_provider import (
    LanOpenAICompatibleProvider,
)
from nested_memvid_agent.llm.lan_runtime_transport import (
    CancellationToken,
    LanRuntimeChatRequest,
)
from nested_memvid_agent.runtime_models import (
    ChatMessage,
    LLMOptions,
    ToolCall,
    ToolSpec,
)
from nested_memvid_agent.security_boundary import register_secret_value

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _authority(
    *,
    model_id: str = "alpha",
    interface_address: str = "192.168.50.7/24",
    network: str = "192.168.50.0/24",
    destination_address: str = "192.168.50.8",
    source_address: str = "192.168.50.7",
    material_digest: str = "sha256:" + "4" * 64,
    review_digest: str = "sha256:" + "5" * 64,
    fresh_until: str = "2026-08-01T12:05:00Z",
) -> LanRuntimeAuthority:
    interface = NetworkInterface.from_addresses(
        os_identity="darwin:en7",
        display_name="darwin:en7",
        addresses=(interface_address,),
    )
    scope = PrivateScanScope.from_request(interface, network)
    endpoint = ResolvedLanEndpoint.from_scope(scope, destination_address, 1234)
    endpoint_binding_digest = derive_lan_runtime_endpoint_binding_digest(endpoint)
    provider_profile_id = derive_lan_runtime_provider_profile_id(endpoint_binding_digest)
    reviewed_target_id = derive_lan_runtime_target_id(provider_profile_id, model_id)
    return LanRuntimeAuthority(
        scope=scope,
        endpoint=endpoint,
        source_address=source_address,
        os_interface_identity="darwin:en7",
        interface_index=7,
        provider_profile_id=provider_profile_id,
        reviewed_target_id=reviewed_target_id,
        model_id=model_id,
        api_shape="openai_compatible",
        runtime_adapter="lan-openai-compatible",
        runtime_hardening_version=LAN_OPENAI_RUNTIME_HARDENING_VERSION,
        endpoint_binding_digest=endpoint_binding_digest,
        endpoint_fingerprint="sha256:" + "3" * 64,
        reviewed_material_binding_digest=material_digest,
        review_digest=review_digest,
        fresh_until=fresh_until,
    )


def _manual_endpoint_type():
    """Resolve the Task 7A type lazily so frozen-base collection remains useful."""

    return import_module("nested_memvid_agent.lan_discovery_models").ManualLanEndpoint


def _manual_authority(
    *,
    model_id: str = "alpha",
    interface_address: str = "192.168.50.7/24",
    network: str = "192.168.50.8/32",
    destination_address: str = "192.168.50.8",
    source_address: str = "192.168.50.7",
    port: int = 5001,
    material_digest: str = "sha256:" + "4" * 64,
    review_digest: str = "sha256:" + "5" * 64,
    fresh_until: str = "2026-08-01T12:05:00Z",
) -> LanRuntimeAuthority:
    interface = NetworkInterface.from_addresses(
        os_identity="darwin:en7",
        display_name="darwin:en7",
        addresses=(interface_address,),
    )
    scope = PrivateScanScope.from_request(interface, network)
    endpoint = _manual_endpoint_type().from_exact_scope(
        scope,
        destination_address,
        port,
    )
    endpoint_binding_digest = derive_lan_runtime_endpoint_binding_digest(endpoint)
    provider_profile_id = derive_lan_runtime_provider_profile_id(endpoint_binding_digest)
    reviewed_target_id = derive_lan_runtime_target_id(provider_profile_id, model_id)
    return LanRuntimeAuthority(
        scope=scope,
        endpoint=endpoint,
        source_address=source_address,
        os_interface_identity="darwin:en7",
        interface_index=7,
        provider_profile_id=provider_profile_id,
        reviewed_target_id=reviewed_target_id,
        model_id=model_id,
        api_shape="openai_compatible",
        runtime_adapter="lan-openai-compatible",
        runtime_hardening_version=LAN_OPENAI_RUNTIME_HARDENING_VERSION,
        endpoint_binding_digest=endpoint_binding_digest,
        endpoint_fingerprint="sha256:" + "3" * 64,
        reviewed_material_binding_digest=material_digest,
        review_digest=review_digest,
        fresh_until=fresh_until,
    )


def _unchecked_authority(
    authority: LanRuntimeAuthority,
    **changes: object,
) -> LanRuntimeAuthority:
    forged = object.__new__(LanRuntimeAuthority)
    for field_name in authority.__dataclass_fields__:
        object.__setattr__(
            forged,
            field_name,
            changes.get(field_name, getattr(authority, field_name)),
        )
    return forged


class Resolver:
    def __init__(self, value: LanRuntimeAuthority | BaseException) -> None:
        self.value = value
        self.calls: list[str] = []

    def __call__(self, target_id: str) -> LanRuntimeAuthority:
        self.calls.append(target_id)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class RecordingTransport:
    def __init__(
        self,
        response: bytes = b'{"choices":[{"message":{"content":"LAN answer"}}]}',
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.calls: list[tuple[LanRuntimeAuthority, LanRuntimeChatRequest, float]] = []

    def request(
        self,
        authority: LanRuntimeAuthority,
        request: LanRuntimeChatRequest,
        *,
        timeout_seconds: float,
        cancellation: CancellationToken,
    ) -> bytes:
        assert cancellation.is_cancelled() is False
        self.calls.append((authority, request, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        return self.response


def _provider(
    *,
    authority: LanRuntimeAuthority | None = None,
    resolver: Resolver | None = None,
    transport: RecordingTransport | None = None,
    base_url: str = "http://192.168.50.8:1234/v1",
    model: str = "alpha",
) -> tuple[LanOpenAICompatibleProvider, Resolver, RecordingTransport]:
    active = authority or _authority(model_id=model)
    active_resolver = resolver or Resolver(active)
    active_transport = transport or RecordingTransport()
    provider = LanOpenAICompatibleProvider(
        model=model,
        base_url=base_url,
        authority=active,
        authority_resolver=active_resolver,
        transport=active_transport,
        timeout_seconds=60,
        temperature=0.2,
        utc_clock=lambda: NOW,
    )
    return provider, active_resolver, active_transport


def _messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="Keep the policy boundary."),
        ChatMessage(role="user", content="Answer plainly."),
        ChatMessage(role="assistant", content="Prior ordinary assistant text."),
    ]


def _tool() -> ToolSpec:
    return ToolSpec(
        name="memory.search",
        description="Search memory.",
        parameters={"type": "object", "properties": {}},
    )


def test_lan_provider_capabilities_are_generation_only_and_tool_free() -> None:
    provider, _resolver, _transport = _provider()

    capabilities = provider.capabilities

    assert capabilities.name == "lan-openai-compatible"
    assert capabilities.supports_tools is False
    assert capabilities.supports_native_tools is False
    assert capabilities.supports_json_mode is False
    assert capabilities.supports_streaming is False
    assert capabilities.supports_system_messages is True


def test_generate_canonicalizes_plain_messages_and_returns_no_raw_response() -> None:
    response = json.dumps(
        {
            "id": "response-advertised-identity-must-be-discarded",
            "model": "forged-upstream-model",
            "choices": [{"message": {"content": "LAN answer"}}],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    transport = RecordingTransport(response)
    provider, resolver, _transport = _provider(transport=transport)
    resolver.calls.clear()

    result = provider.generate(
        _messages(),
        [],
        LLMOptions(stream=False, timeout_seconds=17, max_retries=99, temperature=0.4),
    )

    assert result.content == "LAN answer"
    assert result.tool_calls == ()
    assert result.raw is None
    assert result.finish_reason is None
    assert result.usage is None
    assert resolver.calls == [provider.authority.reviewed_target_id]
    assert len(transport.calls) == 1
    authority, request, timeout = transport.calls[0]
    assert authority.reviewed_target_id == provider.authority.reviewed_target_id
    assert request.model_id == "alpha"
    assert tuple((item.role, item.content) for item in request.messages) == (
        ("system", "Keep the policy boundary."),
        ("user", "Answer plainly."),
        ("assistant", "Prior ordinary assistant text."),
    )
    assert request.temperature == 0.4
    assert timeout == 17


@pytest.mark.parametrize(
    "base_url",
    (
        "https://192.168.50.8:1234/v1",
        "http://server.local:1234/v1",
        "http://localhost:1234/v1",
        "http://127.0.0.1:1234/v1",
        "http://8.8.8.8:1234/v1",
        "http://224.0.0.1:1234/v1",
        "http://192.0.2.8:1234/v1",
        "http://192.168.50.9:1234/v1",
        "http://192.168.50.8:8000/v1",
        "http://user:pass@192.168.50.8:1234/v1",
        "http://192.168.50.8:22/v1",
        "http://192.168.50.8:1234/",
        "http://192.168.50.8:1234/v1/",
        "http://192.168.50.8:1234/v1?secret=x",
        "http://192.168.50.8:1234/v1#fragment",
        "http://fd00::8:1234/v1",
        "http://[fe80::8%25en7]:1234/v1",
    ),
)
def test_constructor_rejects_noncanonical_or_unbound_base_urls(base_url: str) -> None:
    authority = _authority()
    resolver = Resolver(authority)
    transport = RecordingTransport()

    with pytest.raises((TypeError, ValueError, ProviderError)):
        LanOpenAICompatibleProvider(
            model="alpha",
            base_url=base_url,
            authority=authority,
            authority_resolver=resolver,
            transport=transport,
            timeout_seconds=60,
            temperature=None,
            utc_clock=lambda: NOW,
        )

    assert transport.calls == []


@pytest.mark.parametrize(
    ("authority", "base_url"),
    (
        (
            _authority(
                interface_address="fd00::7/64",
                network="fd00::/64",
                destination_address="fd00::8",
                source_address="fd00::7",
            ),
            "http://[fd00::8]:1234/v1",
        ),
        (
            _authority(
                interface_address="fe80::7/64",
                network="fe80::/64",
                destination_address="fe80::8",
                source_address="fe80::7",
            ),
            "http://[fe80::8]:1234/v1",
        ),
    ),
)
def test_constructor_accepts_exact_bracketed_ula_and_link_local_authority(
    authority: LanRuntimeAuthority,
    base_url: str,
) -> None:
    provider, resolver, transport = _provider(
        authority=authority,
        resolver=Resolver(authority),
        transport=RecordingTransport(),
        base_url=base_url,
    )

    assert provider.authority is authority
    assert resolver.calls == [authority.reviewed_target_id]
    assert transport.calls == []


@pytest.mark.parametrize(
    "changes",
    (
        {"model_id": "beta"},
        {"api_shape": "ollama_compatible"},
        {"runtime_adapter": "openai-compatible"},
        {"runtime_hardening_version": "kestrel.lan.runtime.openai.v0"},
        {"source_address": "192.168.50.9"},
        {"os_interface_identity": "darwin:en8"},
        {"endpoint_binding_digest": "sha256:" + "9" * 64},
    ),
)
def test_constructor_rebuilds_and_rejects_forged_authority_fields(
    changes: dict[str, object],
) -> None:
    authority = _unchecked_authority(_authority(), **changes)
    transport = RecordingTransport()

    with pytest.raises((TypeError, ValueError, ProviderError)):
        _provider(authority=authority, resolver=Resolver(authority), transport=transport)

    assert transport.calls == []


def test_exact_python_type_without_matching_current_resolution_is_not_authority() -> None:
    assigned = _authority()
    changed = replace(
        assigned,
        reviewed_material_binding_digest="sha256:" + "8" * 64,
        review_digest="sha256:" + "9" * 64,
    )
    transport = RecordingTransport()

    with pytest.raises((ValueError, ProviderError), match="authority|binding|review"):
        _provider(authority=assigned, resolver=Resolver(changed), transport=transport)

    assert transport.calls == []


def test_generate_revalidates_current_binding_before_transport_use() -> None:
    authority = _authority()
    resolver = Resolver(authority)
    provider, _resolver, transport = _provider(authority=authority, resolver=resolver)
    resolver.calls.clear()
    resolver.value = ValueError("target disabled after assignment")

    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [], LLMOptions())

    assert raised.value.code == "lan_authority_changed"
    assert transport.calls == []
    assert resolver.calls == [authority.reviewed_target_id]


@pytest.mark.parametrize(
    "changes",
    (
        {"provider_profile_id": "lan-provider-" + "a" * 64},
        {"reviewed_target_id": "lan-target-" + "b" * 64},
        {"model_id": "beta"},
        {"api_shape": "ollama_compatible"},
        {"runtime_adapter": "openai-compatible"},
        {"runtime_hardening_version": "kestrel.lan.runtime.openai.v0"},
        {"endpoint_binding_digest": "sha256:" + "6" * 64},
        {"endpoint_fingerprint": "sha256:" + "7" * 64},
        {"reviewed_material_binding_digest": "sha256:" + "8" * 64},
        {"review_digest": "sha256:" + "9" * 64},
        {"source_address": "192.168.50.9"},
        {"os_interface_identity": "darwin:en8"},
    ),
)
def test_generate_rejects_every_resolver_returned_authority_drift(
    changes: dict[str, object],
) -> None:
    authority = _authority()
    resolver = Resolver(authority)
    provider, _resolver, transport = _provider(authority=authority, resolver=resolver)
    resolver.calls.clear()
    resolver.value = _unchecked_authority(authority, **changes)

    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [], LLMOptions())

    assert raised.value.code == "lan_authority_changed"
    assert resolver.calls == [authority.reviewed_target_id]
    assert transport.calls == []


def test_generate_rejects_valid_current_interface_index_change_from_assignment() -> None:
    authority = _authority()
    resolver = Resolver(authority)
    provider, _resolver, transport = _provider(authority=authority, resolver=resolver)
    resolver.calls.clear()
    resolver.value = replace(authority, interface_index=8)

    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [], LLMOptions())

    assert raised.value.code == "lan_authority_changed"
    assert resolver.calls == [authority.reviewed_target_id]
    assert transport.calls == []


def test_generate_allows_only_same_or_newer_freshness_without_changing_binding() -> None:
    authority = _authority(fresh_until="2026-08-01T12:05:00Z")
    resolver = Resolver(authority)
    provider, _resolver, transport = _provider(authority=authority, resolver=resolver)
    resolver.calls.clear()

    resolver.value = replace(authority, fresh_until="2026-08-01T12:04:59Z")
    with pytest.raises(ProviderError) as older:
        provider.generate(_messages(), [], LLMOptions())
    assert older.value.code == "lan_authority_changed"
    assert transport.calls == []

    resolver.value = replace(authority, fresh_until="2026-08-01T12:06:00Z")
    result = provider.generate(_messages(), [], LLMOptions())
    assert result.content == "LAN answer"
    assert len(transport.calls) == 1


def test_constructor_and_generate_reject_expired_authority() -> None:
    expired = _authority(fresh_until="2026-08-01T11:59:59Z")
    with pytest.raises((ValueError, ProviderError), match="expired|fresh"):
        _provider(authority=expired, resolver=Resolver(expired))

    authority = _authority()
    resolver = Resolver(authority)
    provider, _resolver, transport = _provider(authority=authority, resolver=resolver)
    resolver.calls.clear()
    resolver.value = replace(authority, fresh_until="2026-08-01T11:59:59Z")
    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [], LLMOptions())
    assert raised.value.code in {"lan_authority_expired", "lan_authority_changed"}
    assert transport.calls == []


@pytest.mark.parametrize(
    "messages",
    (
        [ChatMessage(role="tool", content="tool output", tool_call_id="call-1")],
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=(ToolCall(name="memory.search", arguments={}),),
            )
        ],
        [ChatMessage(role="user", content="bad\x00control")],
        [ChatMessage(role="user", content="")],
    ),
)
def test_tool_roles_tool_metadata_and_noncanonical_messages_fail_before_transport(
    messages: list[ChatMessage],
) -> None:
    provider, _resolver, transport = _provider()
    transport.calls.clear()

    with pytest.raises((ValueError, ProviderError)):
        provider.generate(messages, [], LLMOptions())

    assert transport.calls == []


def test_direct_nonempty_tools_fail_before_transport() -> None:
    provider, _resolver, transport = _provider()
    transport.calls.clear()

    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [_tool()], LLMOptions())

    assert raised.value.code == "lan_tools_unsupported"
    assert transport.calls == []


def test_stream_and_stream_option_fail_before_transport() -> None:
    provider, _resolver, transport = _provider()
    transport.calls.clear()

    with pytest.raises(ProviderError) as direct:
        list(provider.stream(_messages(), [], LLMOptions(stream=True)))
    assert direct.value.code == "lan_streaming_unsupported"

    with pytest.raises(ProviderError) as option:
        provider.generate(_messages(), [], LLMOptions(stream=True))
    assert option.value.code == "lan_streaming_unsupported"
    assert transport.calls == []


@pytest.mark.parametrize(
    "body",
    (
        b"{}",
        b'{"choices":[]}',
        b'{"choices":[{"message":{"content":"one"}},{"message":{"content":"two"}}]}',
        b'{"choices":[{"message":{}}]}',
        b'{"choices":[{"message":{"content":null}}]}',
        b'{"choices":[{"message":{"content":7}}]}',
        b'{"choices":[{"message":{"content":"ok","tool_calls":[]}}]}',
        b'{"choices":[{"message":{"content":"ok"},"delta":{"content":"x"}}]}',
        b'{"choices":[{"message":{"content":"ok"}}]} trailing',
        b"\xff\xfeHOSTILE_INVALID_UTF8_SENTINEL",
    ),
)
def test_response_requires_exact_single_string_content(body: bytes) -> None:
    provider, _resolver, transport = _provider(transport=RecordingTransport(body))
    transport.calls.clear()

    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [], LLMOptions())

    assert raised.value.code == "lan_response_invalid"
    assert len(transport.calls) == 1
    hostile_sentinel = body.decode("utf-8", "ignore")
    assert hostile_sentinel
    assert hostile_sentinel not in str(raised.value)


@pytest.mark.parametrize(
    "body",
    (
        b'{"choices":[{"message":{"content":"first","content":"second"}}]}',
        b'{"choices":[{"message":{"content":"ok"}}],"choices":[]}',
        b'{"choices":[{"message":{"content":"ok"},"message":{"content":"other"}}]}',
    ),
)
def test_duplicate_key_json_is_rejected_without_raw_echo(body: bytes) -> None:
    provider, _resolver, transport = _provider(transport=RecordingTransport(body))

    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [], LLMOptions())

    assert raised.value.code == "lan_response_invalid"
    assert "first" not in str(raised.value)
    assert "second" not in str(raised.value)


def test_transport_failure_is_not_retried_or_reflected() -> None:
    transport = RecordingTransport(failure=RuntimeError("secret-upstream-body"))
    provider, _resolver, _transport = _provider(transport=transport)

    with pytest.raises(ProviderError) as raised:
        provider.generate(
            _messages(),
            [],
            LLMOptions(timeout_seconds=60, max_retries=99),
        )

    assert raised.value.code == "lan_transport_failed"
    assert raised.value.retryable is True
    assert "secret" not in str(raised.value).lower()
    assert len(transport.calls) == 1


def test_provider_never_accepts_api_key_custom_headers_or_fallback_arguments() -> None:
    authority = _authority()
    kwargs = {
        "model": "alpha",
        "base_url": "http://192.168.50.8:1234/v1",
        "authority": authority,
        "authority_resolver": Resolver(authority),
        "transport": RecordingTransport(),
        "timeout_seconds": 60,
        "temperature": None,
        "utc_clock": lambda: NOW,
    }
    for extra in (
        {"api_key": "secret"},
        {"api_key_env": "LAN_SECRET"},
        {"headers": {"X-Token": "secret"}},
        {"max_retries": 1},
        {"fallback_provider": "openai-compatible"},
    ):
        with pytest.raises(TypeError):
            LanOpenAICompatibleProvider(**kwargs, **extra)  # type: ignore[arg-type]


def test_request_has_no_structured_output_or_tool_wire_fields() -> None:
    provider, _resolver, transport = _provider()
    transport.calls.clear()

    provider.generate(_messages(), [], LLMOptions())

    request = transport.calls[0][1]
    rendered = json.dumps(request.to_payload(), sort_keys=True)
    assert "response_format" not in rendered
    assert '"tools"' not in rendered
    assert "tool_choice" not in rendered
    assert '"stream": false' in rendered.lower()


def test_authority_subclass_is_rejected_exactly() -> None:
    authority = _authority()

    class AuthoritySubclass(LanRuntimeAuthority):
        pass

    provider, _resolver, _transport = _provider(authority=replace(authority))
    assert provider.capabilities.name == "lan-openai-compatible"

    forged_authority = object.__new__(AuthoritySubclass)
    for field_name in authority.__dataclass_fields__:
        object.__setattr__(forged_authority, field_name, getattr(authority, field_name))
    with pytest.raises((TypeError, ValueError, ProviderError)):
        _provider(authority=forged_authority, resolver=Resolver(forged_authority))


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("model_id", ""),
        ("model_id", " alpha"),
        ("model_id", "alpha\x00secret"),
        ("model_id", True),
        ("source_address", "server.local"),
        ("source_address", "127.0.0.1"),
        ("interface_index", False),
        ("interface_index", 0),
        ("fresh_until", "2026-08-01 12:05:00"),
    ),
)
def test_provider_independently_reconstructs_object_bypass_authority(
    field_name: str,
    value: object,
) -> None:
    valid = _authority()
    forged = _unchecked_authority(valid, **{field_name: value})
    transport = RecordingTransport()

    with pytest.raises((TypeError, ValueError, ProviderError)):
        _provider(authority=forged, resolver=Resolver(forged), transport=transport)

    assert transport.calls == []


@pytest.mark.parametrize(
    "model_id",
    (
        "xoxb-123456789012",
        "github_pat_ABCDEFGHIJKL",
        "123456:abcdefghijklmnopqrst",
    ),
)
def test_authority_model_canonicality_matches_task4_token_redaction(model_id: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        _authority(model_id=model_id)


def test_authority_model_canonicality_rejects_registered_secret_values() -> None:
    registered = "QuartzFalconModelValue8C19B2"
    register_secret_value(registered)

    with pytest.raises((TypeError, ValueError)):
        _authority(model_id=registered)


@pytest.mark.parametrize(
    "message",
    (
        ChatMessage(role="user", content="ok", name="caller-name"),
        ChatMessage(role="assistant", content="ok", tool_call_id="call-1"),
        ChatMessage(role="user", content="bad\ud800surrogate"),
    ),
)
def test_message_name_tool_metadata_and_surrogates_are_rejected(
    message: ChatMessage,
) -> None:
    provider, _resolver, transport = _provider()
    with pytest.raises((TypeError, ValueError, ProviderError)):
        provider.generate([message], [], LLMOptions())
    assert transport.calls == []


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("timeout_seconds", False),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
        ("temperature", False),
        ("temperature", float("nan")),
        ("temperature", float("inf")),
    ),
)
def test_invalid_options_fail_before_resolver_or_transport(
    field_name: str,
    value: object,
) -> None:
    provider, resolver, transport = _provider()
    resolver.calls.clear()
    options = LLMOptions()
    object.__setattr__(options, field_name, value)

    with pytest.raises((TypeError, ValueError, ProviderError)):
        provider.generate(_messages(), [], options)

    assert resolver.calls == []
    assert transport.calls == []


def test_options_and_message_containers_require_exact_types() -> None:
    provider, resolver, transport = _provider()
    resolver.calls.clear()

    class OptionsSubclass(LLMOptions):
        pass

    class MessageSubclass(ChatMessage):
        pass

    options = object.__new__(OptionsSubclass)
    for field_name in LLMOptions.__dataclass_fields__:
        object.__setattr__(options, field_name, getattr(LLMOptions(), field_name))
    message = object.__new__(MessageSubclass)
    valid_message = ChatMessage(role="user", content="hello")
    for field_name in ChatMessage.__dataclass_fields__:
        object.__setattr__(message, field_name, getattr(valid_message, field_name))

    with pytest.raises((TypeError, ValueError, ProviderError)):
        provider.generate([valid_message], [], options)
    with pytest.raises((TypeError, ValueError, ProviderError)):
        provider.generate([message], [], LLMOptions())
    with pytest.raises((TypeError, ValueError, ProviderError)):
        provider.generate((valid_message,), [], LLMOptions())  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError, ProviderError)):
        provider.generate([valid_message], (), LLMOptions())  # type: ignore[arg-type]
    assert resolver.calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    "body",
    (
        b'\xef\xbb\xbf{"choices":[{"message":{"content":"ok"}}]}',
        b'{"choices":[{"message":{"content":NaN}}]}',
        b'{"choices":[{"message":{"content":Infinity}}]}',
        b'[{"choices":[{"message":{"content":"ok"}}]}]',
        (b'{"x":' * 128) + b"0" + (b"}" * 128),
    ),
)
def test_bom_nonfinite_wrong_root_and_excessive_depth_are_closed(body: bytes) -> None:
    provider, _resolver, transport = _provider(transport=RecordingTransport(body))

    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [], LLMOptions())

    assert raised.value.code == "lan_response_invalid"
    assert "choices" not in str(raised.value)
    assert "Infinity" not in str(raised.value)
    assert len(transport.calls) == 1


def test_unrelated_oversized_json_integer_is_a_closed_provider_error() -> None:
    body = b'{"unrelated":' + (b"9" * 5000) + b',"choices":[{"message":{"content":"ok"}}]}'
    provider, _resolver, transport = _provider(transport=RecordingTransport(body))

    with pytest.raises(ProviderError) as raised:
        provider.generate(_messages(), [], LLMOptions())

    assert raised.value.code == "lan_response_invalid"
    assert "999999" not in str(raised.value)
    assert len(transport.calls) == 1


def test_provider_never_logs_raw_response_identity_body_transport_or_resolver_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    identity = "HOSTILE_UPSTREAM_IDENTITY_51D0C9"
    body_token = "HOSTILE_UPSTREAM_BODY_7A4E31"
    response = json.dumps(
        {
            "id": identity,
            "model": body_token,
            "choices": [{"message": {"content": "safe answer"}}],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    provider, _resolver, _transport = _provider(transport=RecordingTransport(response))

    assert provider.generate(_messages(), [], LLMOptions()).content == "safe answer"

    transport_token = "HOSTILE_TRANSPORT_EXCEPTION_9B20A7"
    failed_transport = RecordingTransport(failure=RuntimeError(transport_token))
    failed, _resolver, _transport = _provider(transport=failed_transport)
    with pytest.raises(ProviderError) as transport_error:
        failed.generate(_messages(), [], LLMOptions())
    assert transport_error.value.code == "lan_transport_failed"

    authority = _authority()
    resolver_token = "HOSTILE_RESOLVER_EXCEPTION_4FC821"
    resolver = Resolver(ValueError(resolver_token))
    with pytest.raises((ValueError, ProviderError)):
        _provider(authority=authority, resolver=resolver)

    rendered = caplog.text
    for token in (identity, body_token, transport_token, resolver_token):
        assert token not in rendered


def test_task7b_provider_accepts_exact_manual_unusual_port_authority() -> None:
    authority = _manual_authority(port=5001)
    provider, resolver, transport = _provider(
        authority=authority,
        resolver=Resolver(authority),
        transport=RecordingTransport(),
        base_url="http://192.168.50.8:5001/v1",
    )
    resolver.calls.clear()

    result = provider.generate(
        _messages(),
        [],
        LLMOptions(timeout_seconds=17, max_retries=99, temperature=0.4),
    )

    assert result.content == "LAN answer"
    assert result.raw is None
    assert resolver.calls == [authority.reviewed_target_id]
    assert len(transport.calls) == 1
    resolved, request, timeout = transport.calls[0]
    assert resolved is authority
    assert type(resolved.endpoint) is _manual_endpoint_type()
    assert resolved.endpoint.kind == "manual"
    assert resolved.endpoint.port == 5001
    assert request.model_id == "alpha"
    assert timeout == 17


def test_task7b_provider_rejects_automatic_kind_on_unusual_manual_port() -> None:
    manual = _manual_authority(port=5001)
    automatic_endpoint = object.__new__(ResolvedLanEndpoint)
    for field_name in ("interface_id", "address", "port"):
        object.__setattr__(
            automatic_endpoint,
            field_name,
            getattr(manual.endpoint, field_name),
        )
    forged = _unchecked_authority(manual, endpoint=automatic_endpoint)
    resolver = Resolver(forged)
    transport = RecordingTransport()

    with pytest.raises((TypeError, ValueError, ProviderError)):
        _provider(
            authority=forged,
            resolver=resolver,
            transport=transport,
            base_url="http://192.168.50.8:5001/v1",
        )

    assert resolver.calls == []
    assert transport.calls == []


def test_task7b_manual_provider_failure_is_retry_free_and_secret_free() -> None:
    authority = _manual_authority(port=5001)
    transport = RecordingTransport(failure=RuntimeError("secret-upstream-body"))
    provider, resolver, _transport = _provider(
        authority=authority,
        resolver=Resolver(authority),
        transport=transport,
        base_url="http://192.168.50.8:5001/v1",
    )
    resolver.calls.clear()

    with pytest.raises(ProviderError) as raised:
        provider.generate(
            _messages(),
            [],
            LLMOptions(timeout_seconds=60, max_retries=99),
        )

    assert raised.value.code == "lan_transport_failed"
    assert "secret" not in str(raised.value).lower()
    assert resolver.calls == [authority.reviewed_target_id]
    assert len(transport.calls) == 1


def test_task7b_manual_provider_rejects_credentials_headers_retries_and_fallbacks() -> None:
    authority = _manual_authority(port=5001)
    kwargs = {
        "model": "alpha",
        "base_url": "http://192.168.50.8:5001/v1",
        "authority": authority,
        "authority_resolver": Resolver(authority),
        "transport": RecordingTransport(),
        "timeout_seconds": 60,
        "temperature": None,
        "utc_clock": lambda: NOW,
    }
    for extra in (
        {"api_key": "secret"},
        {"api_key_env": "LAN_SECRET"},
        {"headers": {"X-Token": "secret"}},
        {"max_retries": 1},
        {"fallback_provider": "openai-compatible"},
    ):
        with pytest.raises(TypeError):
            LanOpenAICompatibleProvider(**kwargs, **extra)  # type: ignore[arg-type]
