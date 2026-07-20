from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.llm.anthropic_provider import _anthropic_response_to_llm_response
from nested_memvid_agent.llm.base import ProviderError
from nested_memvid_agent.llm.gemini_provider import _gemini_tool_calls
from nested_memvid_agent.llm.ollama_provider import _ollama_tool_calls
from nested_memvid_agent.llm.openai_compatible_provider import (
    OpenAICompatibleProvider,
    _accumulate_delta_tool_calls,
    _buffered_tool_calls,
    _chat_completion_tool_calls,
)
from nested_memvid_agent.llm.openai_provider import _response_tool_calls
from nested_memvid_agent.llm.parser import (
    ControlMessageError,
    native_tool_arguments,
    native_tool_name,
)
from nested_memvid_agent.llm.resilience import ProviderHealthRegistry, ResilientLLMProvider
from nested_memvid_agent.runtime_models import ChatMessage


def _assert_control_code(exc_info: pytest.ExceptionInfo[ControlMessageError], code: str) -> None:
    assert exc_info.value.code == code


@pytest.mark.parametrize("value", [None, "", " memory.search", "memory.search ", 7])
def test_native_tool_name_rejects_missing_or_inexact_values(value: Any) -> None:
    with pytest.raises(ControlMessageError) as exc_info:
        native_tool_name(value, location="provider.calls[0]")

    _assert_control_code(exc_info, "invalid_tool_name")


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ('{"query":', "invalid_tool_argument_json"),
        ("[1,2]", "invalid_tool_arguments"),
        ([], "invalid_tool_arguments"),
        (None, "invalid_tool_arguments"),
        ({"value": float("nan")}, "invalid_tool_arguments"),
        ({1: "not-a-json-key"}, "invalid_tool_arguments"),
        ({"value": {1, 2}}, "invalid_tool_arguments"),
    ],
)
def test_native_tool_arguments_reject_invalid_json_and_non_objects(value: Any, code: str) -> None:
    with pytest.raises(ControlMessageError) as exc_info:
        native_tool_arguments(value, tool_name="memory.search", location="provider.calls[0]")

    _assert_control_code(exc_info, code)


def test_native_tool_arguments_reject_cycles_in_sdk_objects() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ControlMessageError) as exc_info:
        native_tool_arguments(cyclic, tool_name="memory.search", location="provider.calls[0]")

    _assert_control_code(exc_info, "invalid_tool_arguments")


def test_openai_compatible_rejects_invalid_argument_json_before_tool_call_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            id="call_bad",
                            function=SimpleNamespace(
                                name="memory.search",
                                arguments='{"query":',
                            ),
                        )
                    ]
                )
            )
        ]
    )
    monkeypatch.setattr(
        "nested_memvid_agent.llm.openai_compatible_provider.ToolCall",
        lambda **kwargs: pytest.fail(f"ToolCall constructed from malformed data: {kwargs}"),
    )

    with pytest.raises(ControlMessageError) as exc_info:
        _chat_completion_tool_calls(response)

    _assert_control_code(exc_info, "invalid_tool_argument_json")


def test_openai_compatible_stream_rejects_malformed_buffered_arguments() -> None:
    buffers: dict[int, dict[str, str]] = {}
    _accumulate_delta_tool_calls(
        buffers,
        [
            SimpleNamespace(
                index=0,
                id="call_stream",
                function=SimpleNamespace(name="memory.search", arguments='{"query":'),
            )
        ],
    )

    with pytest.raises(ControlMessageError) as exc_info:
        _buffered_tool_calls(buffers)

    _assert_control_code(exc_info, "invalid_tool_argument_json")


def test_openai_compatible_stream_accepts_split_json_fragments() -> None:
    buffers: dict[int, dict[str, str]] = {}
    _accumulate_delta_tool_calls(
        buffers,
        [
            SimpleNamespace(
                index=0,
                id="call_stream",
                function=SimpleNamespace(name="memory.", arguments='{"query":'),
            )
        ],
    )
    _accumulate_delta_tool_calls(
        buffers,
        [
            SimpleNamespace(
                index=0,
                function=SimpleNamespace(name="search", arguments='"split"}'),
            )
        ],
    )

    assert _buffered_tool_calls(buffers)[0].arguments == {"query": "split"}


def test_resilient_provider_classifies_real_native_parse_failure_as_nonretryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            del kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[
                                SimpleNamespace(
                                    id="call_bad",
                                    function=SimpleNamespace(
                                        name="memory.search",
                                        arguments='{"query":',
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "nested_memvid_agent.llm.openai_compatible_provider.import_module",
        lambda name: SimpleNamespace(OpenAI=FakeOpenAI),
    )
    registry = ProviderHealthRegistry()
    provider = ResilientLLMProvider(
        OpenAICompatibleProvider(
            model="local-model",
            base_url="http://127.0.0.1:11434/v1",
        ),
        provider_id="native-parse-test",
        registry=registry,
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.generate([ChatMessage(role="user", content="search")], [])

    assert exc_info.value.code == "invalid_tool_argument_json"
    assert exc_info.value.retryable is False
    health = registry.snapshot("native-parse-test")
    assert health["state"] == "degraded"
    assert health["failure_class"] == "invalid_tool_argument_json"


def test_openai_responses_rejects_function_call_without_name() -> None:
    response = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", call_id="call_bad", arguments="{}")]
    )

    with pytest.raises(ControlMessageError) as exc_info:
        _response_tool_calls(response)

    _assert_control_code(exc_info, "invalid_tool_name")


def test_anthropic_rejects_non_object_tool_input() -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="toolu_bad", name="memory.search", input=[])],
        stop_reason="tool_use",
    )

    with pytest.raises(ControlMessageError) as exc_info:
        _anthropic_response_to_llm_response(response, tools=[])

    _assert_control_code(exc_info, "invalid_tool_arguments")


def test_gemini_rejects_non_object_function_arguments() -> None:
    response = SimpleNamespace(
        function_calls=[SimpleNamespace(name="memory.search", args=["not", "an", "object"])]
    )

    with pytest.raises(ControlMessageError) as exc_info:
        _gemini_tool_calls(response)

    _assert_control_code(exc_info, "invalid_tool_arguments")


@pytest.mark.parametrize(
    ("raw_calls", "code"),
    [
        ([{"type": "function"}], "invalid_tool_call"),
        (
            [
                {
                    "type": "function",
                    "function": {"name": "memory.search", "arguments": "not-json"},
                }
            ],
            "invalid_tool_argument_json",
        ),
    ],
)
def test_ollama_rejects_malformed_native_calls(raw_calls: Any, code: str) -> None:
    with pytest.raises(ControlMessageError) as exc_info:
        _ollama_tool_calls(raw_calls)

    _assert_control_code(exc_info, code)
