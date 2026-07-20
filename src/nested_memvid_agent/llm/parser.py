from __future__ import annotations

import json
import math
import re
from typing import Any

from ..runtime_models import LLMResponse, StrategyProposal, ToolCall, ToolSpec

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_CONTROL_MESSAGE_KEYS = {"message", "tool_calls", "schema_version"}


class ControlMessageError(ValueError):
    """Raised when a structured control message fails the provider boundary contract."""

    def __init__(self, message: str, *, code: str = "invalid_control_message") -> None:
        super().__init__(message)
        self.code = code


def native_tool_name(value: Any, *, location: str) -> str:
    """Validate an adapter-native function name without coercion or repair."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ControlMessageError(
            f"{location} must include a non-empty exact tool name",
            code="invalid_tool_name",
        )
    return value


def native_tool_arguments(value: Any, *, tool_name: str, location: str) -> dict[str, Any]:
    """Decode provider-native arguments as one strict JSON object.

    Native adapters must call this before constructing ``ToolCall``.  Invalid
    JSON, non-object values, and SDK values that are not JSON-compatible fail
    closed instead of being repaired to an empty argument object.
    """

    decoded = value
    if isinstance(value, str):
        if not value.strip():
            raise ControlMessageError(
                f"{location} arguments for {tool_name} must be a JSON object",
                code="invalid_tool_arguments",
            )
        try:
            decoded = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ControlMessageError(
                f"{location} arguments for {tool_name} contain invalid JSON",
                code="invalid_tool_argument_json",
            ) from exc
    if not isinstance(decoded, dict):
        raise ControlMessageError(
            f"{location} arguments for {tool_name} must be a JSON object",
            code="invalid_tool_arguments",
        )
    if not _is_json_value(decoded):
        raise ControlMessageError(
            f"{location} arguments for {tool_name} are not JSON-compatible",
            code="invalid_tool_arguments",
        )
    return dict(decoded)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _is_json_value(
    value: Any,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> bool:
    if _depth > 64:
        return False
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list | dict):
        seen = _seen if _seen is not None else set()
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        try:
            if isinstance(value, list):
                return all(
                    _is_json_value(item, _depth=_depth + 1, _seen=seen)
                    for item in value
                )
            return all(
                isinstance(key, str)
                and _is_json_value(item, _depth=_depth + 1, _seen=seen)
                for key, item in value.items()
            )
        finally:
            seen.remove(identity)
    return False


def parse_agent_response(
    text: str,
    *,
    tools: list[ToolSpec] | tuple[ToolSpec, ...] = (),
    strict: bool = False,
) -> LLMResponse:
    """Parse the agent-control JSON envelope, falling back to plain chat text.

    Expected optional envelope:
    {
      "message": "user-visible text",
      "tool_calls": [{"name": "memory.search", "arguments": {"query": "..."}}]
    }
    """
    payload = _extract_json(text)
    if not isinstance(payload, dict):
        return LLMResponse(content=text.strip())
    if strict:
        _validate_control_payload(payload)

    message = str(payload.get("message", "")).strip()
    calls_raw = payload.get("tool_calls", [])
    calls: list[ToolCall] = []
    if isinstance(calls_raw, list):
        for index, item in enumerate(calls_raw):
            if not isinstance(item, dict):
                if strict:
                    raise ControlMessageError(
                        f"tool_calls[{index}] must be an object",
                        code="invalid_tool_call",
                    )
                continue
            name = item.get("name")
            args = item.get("arguments", {})
            if isinstance(name, str) and isinstance(args, dict):
                call_id = item.get("id")
                strategy = _strategy_from_payload(item.get("strategy"), strict=strict)
                calls.append(
                    _validated_tool_call(
                        name=name,
                        arguments=dict(args),
                        tools=tools,
                        call_id=str(call_id) if isinstance(call_id, str) and call_id else None,
                        strategy=strategy,
                        strict=strict,
                    )
                )
            elif strict:
                raise ControlMessageError(
                    f"tool_calls[{index}] must include name:string and arguments:object",
                    code="invalid_tool_call",
                )
    if not message and not calls:
        return LLMResponse(content=text.strip())
    return LLMResponse(content=message, tool_calls=tuple(calls), raw=payload)


def validate_llm_response(
    response: LLMResponse,
    *,
    tools: list[ToolSpec] | tuple[ToolSpec, ...] = (),
) -> LLMResponse:
    """Validate provider-normalized tool calls against the active tool registry."""
    if not response.tool_calls:
        return response
    normalized = tuple(
        _validated_tool_call(
            name=call.name,
            arguments=dict(call.arguments),
            tools=tools,
            call_id=call.id,
            strategy=call.strategy,
            strict=True,
        )
        for call in response.tool_calls
    )
    return LLMResponse(
        content=response.content,
        tool_calls=normalized,
        raw=response.raw,
        usage=response.usage,
        finish_reason=response.finish_reason,
    )


def normalize_tool_calls(
    calls: list[ToolCall] | tuple[ToolCall, ...],
    *,
    tools: list[ToolSpec] | tuple[ToolSpec, ...] = (),
) -> tuple[ToolCall, ...]:
    """Normalize native provider tool calls through the same strict schema boundary."""
    return validate_llm_response(LLMResponse(content="", tool_calls=tuple(calls)), tools=tools).tool_calls


def _extract_json(text: str) -> Any | None:
    stripped = text.strip()
    candidates = [stripped]
    match = _JSON_BLOCK_RE.search(stripped)
    if match:
        candidates.insert(0, match.group(1))
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _validate_control_payload(payload: dict[str, Any]) -> None:
    if not (set(payload) & _CONTROL_MESSAGE_KEYS):
        return
    unknown = set(payload) - _CONTROL_MESSAGE_KEYS
    if unknown:
        raise ControlMessageError(
            f"unknown control message keys: {sorted(unknown)}",
            code="invalid_control_envelope",
        )
    if "schema_version" in payload and payload["schema_version"] not in {1, "1"}:
        raise ControlMessageError(
            "unsupported control message schema_version",
            code="invalid_control_envelope",
        )
    if "message" in payload and not isinstance(payload["message"], str):
        raise ControlMessageError("message must be a string", code="invalid_control_envelope")
    if "tool_calls" in payload and not isinstance(payload["tool_calls"], list):
        raise ControlMessageError("tool_calls must be a list", code="invalid_control_envelope")


def _validated_tool_call(
    *,
    name: str,
    arguments: dict[str, Any],
    tools: list[ToolSpec] | tuple[ToolSpec, ...],
    call_id: str | None,
    strategy: StrategyProposal | None,
    strict: bool,
) -> ToolCall:
    if not name.strip():
        raise ControlMessageError("tool call name must be non-empty", code="invalid_tool_name")
    spec = _tool_spec(name, tools)
    if spec is None:
        if strict and tools:
            raise ControlMessageError(f"unknown tool call: {name}", code="unknown_tool_call")
        return ToolCall(name=name, arguments=arguments, id=call_id or f"tool_{name}", strategy=strategy)
    _validate_schema_arguments(name, arguments, spec.parameters)
    return ToolCall(name=name, arguments=arguments, id=call_id or f"tool_{name}", strategy=strategy)


def _strategy_from_payload(value: Any, *, strict: bool) -> StrategyProposal | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        if strict:
            raise ControlMessageError(
                "tool_call.strategy must be an object",
                code="invalid_tool_strategy",
            )
        return None
    changed = value.get("changed_strategy", "")
    if not isinstance(changed, str):
        if strict:
            raise ControlMessageError(
                "tool_call.strategy.changed_strategy must be a string",
                code="invalid_tool_strategy",
            )
        return None
    optional: dict[str, str] = {}
    for key in ("why_different", "expected_signal", "fallback_if_fails"):
        item = value.get(key, "")
        if not isinstance(item, str):
            if strict:
                raise ControlMessageError(
                    f"tool_call.strategy.{key} must be a string",
                    code="invalid_tool_strategy",
                )
            item = ""
        optional[key] = item
    return StrategyProposal(changed_strategy=changed, **optional)


def _tool_spec(name: str, tools: list[ToolSpec] | tuple[ToolSpec, ...]) -> ToolSpec | None:
    for spec in tools:
        if spec.name == name:
            return spec
        if name in spec.aliases:
            return spec
    return None


def _validate_schema_arguments(tool_name: str, arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    schema_type = schema.get("type")
    if schema_type not in {None, "object"}:
        raise ControlMessageError(
            f"{tool_name} parameters schema must be an object",
            code="invalid_tool_schema",
        )
    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [key for key in required if isinstance(key, str) and key not in arguments]
        if missing:
            raise ControlMessageError(
                f"{tool_name} missing required arguments: {missing}",
                code="missing_tool_arguments",
            )
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for key, value in arguments.items():
            prop_schema = properties.get(key)
            if isinstance(prop_schema, dict):
                _validate_json_value(tool_name, key, value, prop_schema)
            elif schema.get("additionalProperties") is False:
                raise ControlMessageError(
                    f"{tool_name} unknown argument: {key}",
                    code="unknown_tool_argument",
                )


def _validate_json_value(tool_name: str, key: str, value: Any, schema: dict[str, Any]) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if any(_json_type_matches(value, item) for item in expected if isinstance(item, str)):
            return
        raise ControlMessageError(
            f"{tool_name}.{key} has invalid type",
            code="invalid_tool_argument_type",
        )
    if isinstance(expected, str) and not _json_type_matches(value, expected):
        raise ControlMessageError(
            f"{tool_name}.{key} must be {expected}",
            code="invalid_tool_argument_type",
        )
    if expected == "array":
        item_schema = schema.get("items")
        if isinstance(item_schema, dict) and isinstance(value, list):
            for index, item in enumerate(value):
                _validate_json_value(tool_name, f"{key}[{index}]", item, item_schema)


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True
