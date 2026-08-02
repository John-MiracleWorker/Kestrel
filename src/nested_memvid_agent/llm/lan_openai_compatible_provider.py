"""Tool-free OpenAI-compatible generation over the direct LAN runtime boundary."""

from __future__ import annotations

import ipaddress
import json
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn, Protocol

from nested_memvid_agent.lan_discovery_models import (
    KNOWN_MODEL_SERVICE_PORTS,
    ManualLanEndpoint,
)
from nested_memvid_agent.lan_runtime_authority import (
    LanRuntimeAuthority,
    LanRuntimeAuthorityResolver,
    authenticate_lan_runtime_authority,
)
from nested_memvid_agent.runtime_models import (
    ChatMessage,
    LLMOptions,
    LLMResponse,
    LLMStreamEvent,
    ToolSpec,
)

from .base import LLMProvider, ProviderCapabilities, ProviderError
from .lan_runtime_transport import (
    MAX_LAN_RUNTIME_REQUEST_BYTES,
    MAX_LAN_RUNTIME_RESPONSE_BYTES,
    CancellationToken,
    LanRuntimeChatRequest,
)

_MAX_LAN_RUNTIME_MESSAGES = 4096
_MAX_LAN_RESPONSE_JSON_DEPTH = 64
_MAX_LAN_RESPONSE_JSON_VALUES = 100_000
_ORDINARY_MESSAGE_ROLES = frozenset({"system", "user", "assistant"})
_ALLOWED_MESSAGE_CONTROLS = frozenset({"\t", "\n", "\r"})


class _LanRuntimeTransport(Protocol):
    def request(
        self,
        authority: LanRuntimeAuthority,
        request: LanRuntimeChatRequest,
        *,
        timeout_seconds: float,
        cancellation: CancellationToken,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


_NEVER_CANCELLED = _NeverCancelled()


class _InvalidLanResponse(ValueError):
    """Internal sentinel whose text never contains untrusted response material."""


class LanOpenAICompatibleProvider(LLMProvider):
    """Generation-only provider bound to one reviewed private-LAN target."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        authority: LanRuntimeAuthority,
        authority_resolver: LanRuntimeAuthorityResolver,
        transport: _LanRuntimeTransport,
        timeout_seconds: float,
        temperature: float | None,
        utc_clock: Callable[[], datetime],
    ) -> None:
        assigned = authenticate_lan_runtime_authority(authority)
        _require_bound_model(model, assigned)
        _require_exact_base_url(base_url, assigned)
        configured_timeout = _require_positive_finite_number(
            timeout_seconds,
            field_name="timeout",
        )
        configured_temperature = _require_optional_finite_number(
            temperature,
            field_name="temperature",
        )
        if not callable(authority_resolver):
            raise TypeError("LAN authority resolver must be callable")
        if not callable(getattr(transport, "request", None)):
            raise TypeError("LAN runtime transport must provide request()")
        if not callable(utc_clock):
            raise TypeError("LAN UTC clock must be callable")

        now = _read_utc_clock(utc_clock)
        _require_fresh(assigned, now)
        current = _resolve_current_authority(authority_resolver, assigned.reviewed_target_id)
        _require_same_binding(assigned, current)
        _require_not_older_freshness(assigned, current)
        _require_fresh(current, now)

        self.model = model
        self.base_url = base_url
        self.authority = assigned
        self._initial_binding = _authority_binding(assigned)
        self._initial_fresh_until = assigned.fresh_until_datetime
        self._authority_resolver = authority_resolver
        self._transport = transport
        self._timeout_seconds = configured_timeout
        self._temperature = configured_temperature
        self._utc_clock = utc_clock

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="lan-openai-compatible",
            supports_tools=False,
            supports_native_tools=False,
            supports_streaming=False,
            supports_json_mode=False,
            supports_system_messages=True,
        )

    @property
    def authority_resolver(self) -> LanRuntimeAuthorityResolver:
        """Expose the exact injected resolver for composition identity checks."""

        return self._authority_resolver

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        canonical_messages = _canonical_messages(messages)
        _require_empty_tools(tools)
        timeout_seconds, temperature = self._active_options(options)

        assigned = authenticate_lan_runtime_authority(self.authority)
        _require_bound_model(self.model, assigned)
        _require_exact_base_url(self.base_url, assigned)
        if _authority_binding(assigned) != self._initial_binding:
            raise ProviderError(
                "LAN runtime authority binding changed.",
                code="lan_authority_changed",
            )
        if assigned.fresh_until_datetime < self._initial_fresh_until:
            raise ProviderError(
                "LAN runtime authority freshness changed.",
                code="lan_authority_changed",
            )

        current = _resolve_current_authority(
            self._authority_resolver,
            assigned.reviewed_target_id,
        )
        _require_same_binding(assigned, current)
        _require_not_older_freshness(assigned, current)
        _require_fresh(current, _read_utc_clock(self._utc_clock))

        request = LanRuntimeChatRequest(
            model_id=current.model_id,
            messages=canonical_messages,
            temperature=temperature,
        )
        try:
            response_body = self._transport.request(
                current,
                request,
                timeout_seconds=timeout_seconds,
                cancellation=_NEVER_CANCELLED,
            )
        except Exception:
            raise ProviderError(
                "LAN runtime transport failed.",
                code="lan_transport_failed",
                retryable=True,
            ) from None

        content = _response_content(response_body)
        return LLMResponse(
            content=content,
            tool_calls=(),
            raw=None,
            usage=None,
            finish_reason=None,
        )

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> Iterator[LLMStreamEvent]:
        del messages, tools, options
        raise ProviderError(
            "LAN runtime streaming is unsupported.",
            code="lan_streaming_unsupported",
        )

    def _active_options(self, options: LLMOptions | None) -> tuple[float, float | None]:
        if options is None:
            return self._timeout_seconds, self._temperature
        if type(options) is not LLMOptions:
            raise TypeError("LAN runtime options must use the exact options type")
        if type(options.stream) is not bool:
            raise TypeError("LAN runtime stream option must be Boolean")
        if options.stream:
            raise ProviderError(
                "LAN runtime streaming is unsupported.",
                code="lan_streaming_unsupported",
            )
        if (
            isinstance(options.max_retries, bool)
            or not isinstance(options.max_retries, int)
            or options.max_retries < 0
        ):
            raise TypeError("LAN runtime retry option must be a nonnegative integer")
        timeout_seconds = _require_positive_finite_number(
            options.timeout_seconds,
            field_name="timeout",
        )
        temperature = _require_optional_finite_number(
            options.temperature,
            field_name="temperature",
        )
        return timeout_seconds, temperature


def _require_bound_model(model: object, authority: LanRuntimeAuthority) -> None:
    if type(model) is not str or model != authority.model_id:
        raise ValueError("LAN provider model does not match its authority binding")


def _require_exact_base_url(base_url: object, authority: LanRuntimeAuthority) -> None:
    if type(base_url) is not str:
        raise TypeError("LAN provider base URL must be a string")
    address = authority.endpoint.address
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError("LAN provider authority has an invalid literal endpoint") from None
    if str(parsed) != address or (
        type(authority.endpoint) is not ManualLanEndpoint
        and authority.endpoint.port not in KNOWN_MODEL_SERVICE_PORTS
    ):
        raise ValueError("LAN provider authority has a noncanonical endpoint")
    numeric_authority = (
        f"[{address}]:{authority.endpoint.port}"
        if isinstance(parsed, ipaddress.IPv6Address)
        else f"{address}:{authority.endpoint.port}"
    )
    if base_url != f"http://{numeric_authority}/v1":
        raise ValueError("LAN provider base URL does not match its authority binding")


def _require_positive_finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"LAN runtime {field_name} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"LAN runtime {field_name} must be positive and finite")
    return normalized


def _require_optional_finite_number(
    value: object,
    *,
    field_name: str,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"LAN runtime {field_name} must be numeric or null")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"LAN runtime {field_name} must be finite")
    return normalized


def _read_utc_clock(clock: Callable[[], datetime]) -> datetime:
    try:
        now = clock()
    except Exception:
        raise ProviderError("LAN runtime clock failed.", code="lan_clock_invalid") from None
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ProviderError("LAN runtime clock is invalid.", code="lan_clock_invalid")
    return now.astimezone(UTC)


def _resolve_current_authority(
    resolver: LanRuntimeAuthorityResolver,
    reviewed_target_id: str,
) -> LanRuntimeAuthority:
    try:
        current = resolver(reviewed_target_id)
        return authenticate_lan_runtime_authority(current)
    except Exception:
        raise ProviderError(
            "LAN runtime authority binding changed.",
            code="lan_authority_changed",
        ) from None


def _authority_binding(authority: LanRuntimeAuthority) -> tuple[object, ...]:
    return (
        authority.scope,
        authority.endpoint,
        authority.source_address,
        authority.os_interface_identity,
        authority.interface_index,
        authority.provider_profile_id,
        authority.reviewed_target_id,
        authority.model_id,
        authority.api_shape,
        authority.runtime_adapter,
        authority.runtime_hardening_version,
        authority.endpoint_binding_digest,
        authority.endpoint_fingerprint,
        authority.reviewed_material_binding_digest,
        authority.review_digest,
    )


def _require_same_binding(
    assigned: LanRuntimeAuthority,
    current: LanRuntimeAuthority,
) -> None:
    if _authority_binding(current) != _authority_binding(assigned):
        raise ProviderError(
            "LAN runtime authority binding changed.",
            code="lan_authority_changed",
        )


def _require_not_older_freshness(
    assigned: LanRuntimeAuthority,
    current: LanRuntimeAuthority,
) -> None:
    if current.fresh_until_datetime < assigned.fresh_until_datetime:
        raise ProviderError(
            "LAN runtime authority freshness changed.",
            code="lan_authority_changed",
        )


def _require_fresh(authority: LanRuntimeAuthority, now: datetime) -> None:
    if authority.fresh_until_datetime <= now:
        raise ProviderError(
            "LAN runtime authority expired.",
            code="lan_authority_expired",
        )


def _canonical_messages(messages: object) -> tuple[ChatMessage, ...]:
    if type(messages) is not list:
        raise TypeError("LAN runtime messages must use a list")
    if not messages or len(messages) > _MAX_LAN_RUNTIME_MESSAGES:
        raise ValueError("LAN runtime messages are empty or exceed the count limit")

    total_content_bytes = 0
    canonical: list[ChatMessage] = []
    for message in messages:
        if type(message) is not ChatMessage:
            raise TypeError("LAN runtime messages must use the exact message type")
        if type(message.role) is not str or message.role not in _ORDINARY_MESSAGE_ROLES:
            raise ValueError("LAN runtime accepts only ordinary message roles")
        content_bytes = _canonical_message_content(message.content)
        total_content_bytes += len(content_bytes)
        if total_content_bytes > MAX_LAN_RUNTIME_REQUEST_BYTES:
            raise ValueError("LAN runtime message content exceeds the byte limit")
        if message.name is not None or message.tool_call_id is not None:
            raise ValueError("LAN runtime messages cannot contain tool metadata")
        if type(message.tool_calls) is not tuple or message.tool_calls:
            raise ValueError("LAN runtime messages cannot contain tool calls")
        canonical.append(ChatMessage(role=message.role, content=message.content))
    return tuple(canonical)


def _canonical_message_content(content: object) -> bytes:
    if type(content) is not str or not content:
        raise ValueError("LAN runtime message content must be a nonempty string")
    if any(
        (ord(character) < 32 and character not in _ALLOWED_MESSAGE_CONTROLS)
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in content
    ):
        raise ValueError("LAN runtime message content is not canonical text")
    try:
        return content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ValueError("LAN runtime message content is not canonical UTF-8") from None


def _require_empty_tools(tools: object) -> None:
    if type(tools) is not list:
        raise TypeError("LAN runtime tools must use a list")
    if tools:
        raise ProviderError(
            "LAN runtime tools are unsupported.",
            code="lan_tools_unsupported",
        )


def _response_content(response_body: object) -> str:
    try:
        if type(response_body) is not bytes:
            raise _InvalidLanResponse
        if not response_body or len(response_body) > MAX_LAN_RUNTIME_RESPONSE_BYTES:
            raise _InvalidLanResponse
        decoded = response_body.decode("utf-8", errors="strict")
        if decoded.startswith("\ufeff"):
            raise _InvalidLanResponse
        parsed = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_int=_parse_bounded_json_int,
        )
        _require_bounded_json_tree(parsed)
        if type(parsed) is not dict:
            raise _InvalidLanResponse
        choices = parsed.get("choices")
        if type(choices) is not list or len(choices) != 1:
            raise _InvalidLanResponse
        choice = choices[0]
        if type(choice) is not dict or "delta" in choice:
            raise _InvalidLanResponse
        message = choice.get("message")
        if type(message) is not dict:
            raise _InvalidLanResponse
        if any(key in message for key in ("tool_calls", "function_call", "audio")):
            raise _InvalidLanResponse
        if "role" in message and message["role"] != "assistant":
            raise _InvalidLanResponse
        content = message.get("content")
        if type(content) is not str or any(
            (ord(character) < 32 and character not in _ALLOWED_MESSAGE_CONTROLS)
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in content
        ):
            raise _InvalidLanResponse
        return content
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        ValueError,
        _InvalidLanResponse,
    ):
        raise ProviderError(
            "LAN runtime response was invalid.",
            code="lan_response_invalid",
        ) from None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidLanResponse
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise _InvalidLanResponse


def _parse_bounded_json_int(value: str) -> int:
    if len(value) > 128:
        raise _InvalidLanResponse
    return int(value)


def _require_bounded_json_tree(root: object) -> None:
    stack: list[tuple[object, int]] = [(root, 1)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if depth > _MAX_LAN_RESPONSE_JSON_DEPTH or visited > _MAX_LAN_RESPONSE_JSON_VALUES:
            raise _InvalidLanResponse
        if type(value) is dict:
            stack.extend((child, depth + 1) for child in value.values())
        elif type(value) is list:
            stack.extend((child, depth + 1) for child in value)
        elif type(value) is float and not math.isfinite(value):
            raise _InvalidLanResponse
