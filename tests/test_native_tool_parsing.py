from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Literal

import pytest

from nested_memvid_agent.llm.anthropic_provider import (
    _anthropic_messages,
    _anthropic_response_to_llm_response,
)
from nested_memvid_agent.llm.base import ProviderError
from nested_memvid_agent.llm.gemini_provider import _gemini_tool_calls
from nested_memvid_agent.llm.ollama_provider import _ollama_tool_calls
from nested_memvid_agent.llm.openai_compatible_provider import (
    OpenAICompatibleProvider,
    _accumulate_delta_tool_calls,
    _buffered_tool_calls,
    _chat_completion_tool_calls,
    _to_chat_completion_dict,
)
from nested_memvid_agent.llm.openai_provider import _response_tool_calls, _to_responses_input
from nested_memvid_agent.llm.parser import (
    ControlMessageError,
    native_tool_arguments,
    native_tool_call_id,
    native_tool_name,
    normalize_tool_calls,
    parse_agent_response,
    validate_tool_result_pairs,
)
from nested_memvid_agent.llm.resilience import ProviderHealthRegistry, ResilientLLMProvider
from nested_memvid_agent.runtime_models import ChatMessage, LLMOptions, ToolCall, ToolSpec


def _assert_control_code(exc_info: pytest.ExceptionInfo[ControlMessageError], code: str) -> None:
    assert exc_info.value.code == code


@pytest.mark.parametrize("value", [None, "", " memory.search", "memory.search ", 7])
def test_native_tool_name_rejects_missing_or_inexact_values(value: Any) -> None:
    with pytest.raises(ControlMessageError) as exc_info:
        native_tool_name(value, location="provider.calls[0]")

    _assert_control_code(exc_info, "invalid_tool_name")


@pytest.mark.parametrize(
    "value",
    ["", "   ", " call_exact", "call_exact ", 7, True, "x" * 257],
)
def test_native_tool_call_id_rejects_unbounded_or_inexact_values(value: Any) -> None:
    with pytest.raises(ControlMessageError) as exc_info:
        native_tool_call_id(value, location="provider.calls[0].id", required=True)

    _assert_control_code(exc_info, "invalid_tool_call_id")


def test_native_tool_call_id_preserves_a_supplied_valid_id_exactly() -> None:
    call_id = "call.Provider:01_exact"

    assert native_tool_call_id(call_id, location="provider.calls[0].id", required=True) is call_id


def test_strict_control_message_rejects_an_explicit_non_string_call_id() -> None:
    with pytest.raises(ControlMessageError) as exc_info:
        parse_agent_response(
            '{"message":"","tool_calls":[{"id":7,"name":"memory.search","arguments":{}}]}',
            strict=True,
        )

    _assert_control_code(exc_info, "invalid_tool_call_id")


def test_normalized_provider_response_rejects_duplicate_call_ids() -> None:
    calls = [
        ToolCall(name="memory.search", arguments={"query": "first"}, id="call_duplicate"),
        ToolCall(name="memory.search", arguments={"query": "second"}, id="call_duplicate"),
    ]

    with pytest.raises(ControlMessageError) as exc_info:
        normalize_tool_calls(calls)

    _assert_control_code(exc_info, "duplicate_tool_call_id")


def test_tool_result_pairing_rejects_unknown_and_duplicate_results() -> None:
    call = ToolCall(name="memory.search", arguments={}, id="call_pair")
    assistant = ChatMessage(role="assistant", content="", tool_calls=(call,))
    result = ChatMessage(role="tool", content="done", tool_call_id=call.id)

    validate_tool_result_pairs([assistant, result])
    with pytest.raises(ControlMessageError) as unknown_exc:
        validate_tool_result_pairs(
            [ChatMessage(role="tool", content="done", tool_call_id="unknown")]
        )
    with pytest.raises(ControlMessageError) as duplicate_exc:
        validate_tool_result_pairs([assistant, result, result])

    _assert_control_code(unknown_exc, "unpaired_tool_result")
    _assert_control_code(duplicate_exc, "unpaired_tool_result")


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_tool_result_pairing_rejects_an_intervening_conversation_turn(
    role: Literal["user", "assistant"],
) -> None:
    call = ToolCall(name="memory.search", arguments={}, id="call_pending")
    assistant = ChatMessage(role="assistant", content="", tool_calls=(call,))
    intervening = ChatMessage(role=role, content="intervening")
    result = ChatMessage(role="tool", content="late", tool_call_id=call.id)

    with pytest.raises(ControlMessageError) as exc_info:
        validate_tool_result_pairs([assistant, intervening, result])

    _assert_control_code(exc_info, "missing_tool_result")


def test_tool_result_pairing_rejects_unresolved_calls_at_end_of_history() -> None:
    call = ToolCall(name="memory.search", arguments={}, id="call_unresolved")
    assistant = ChatMessage(role="assistant", content="", tool_calls=(call,))

    with pytest.raises(ControlMessageError) as exc_info:
        validate_tool_result_pairs([assistant])

    _assert_control_code(exc_info, "missing_tool_result")


def _render_openai_compatible_history(messages: list[ChatMessage]) -> Any:
    provider = OpenAICompatibleProvider(
        model="local-model",
        base_url="http://127.0.0.1:11434/v1",
    )
    return provider._request_payload(messages, [], LLMOptions())["messages"]


@pytest.mark.parametrize(
    "renderer",
    [_render_openai_compatible_history, _to_responses_input, _anthropic_messages],
)
@pytest.mark.parametrize("failure", ["intervening_turn", "unresolved_end"])
def test_id_based_provider_serializers_reject_incomplete_tool_call_histories(
    renderer: Any,
    failure: str,
) -> None:
    call = ToolCall(name="memory.search", arguments={}, id="call_incomplete")
    assistant = ChatMessage(role="assistant", content="", tool_calls=(call,))
    messages = [assistant]
    if failure == "intervening_turn":
        messages.append(ChatMessage(role="user", content="too early"))

    with pytest.raises(ControlMessageError) as exc_info:
        renderer(messages)

    _assert_control_code(exc_info, "missing_tool_result")


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


def test_openai_compatible_stream_rejects_conflicting_call_ids() -> None:
    buffers: dict[int, dict[str, str]] = {}
    _accumulate_delta_tool_calls(
        buffers,
        [
            SimpleNamespace(
                index=0,
                id="call_original",
                function=SimpleNamespace(name="memory.search", arguments="{}"),
            )
        ],
    )

    with pytest.raises(ControlMessageError) as exc_info:
        _accumulate_delta_tool_calls(
            buffers,
            [
                SimpleNamespace(
                    index=0,
                    id="call_replaced",
                    function=SimpleNamespace(),
                )
            ],
        )

    _assert_control_code(exc_info, "invalid_tool_call_id")


def test_all_missing_provider_tool_call_ids_are_collision_resistant() -> None:
    search_spec = ToolSpec(
        name="memory.search",
        description="Search memory.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    native_calls = [
        _chat_completion_tool_calls(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            tool_calls=[
                                SimpleNamespace(
                                    function=SimpleNamespace(
                                        name="memory.search",
                                        arguments=f'{{"query":"chat-{index}"}}',
                                    )
                                )
                            ]
                        )
                    )
                ]
            )
        )[0]
        for index in range(2)
    ]
    stream_calls = [
        _buffered_tool_calls(
            {
                0: {
                    "id": "",
                    "name": "memory.search",
                    "arguments": f'{{"query":"stream-{index}"}}',
                }
            }
        )[0]
        for index in range(2)
    ]
    responses_calls = [
        _response_tool_calls(
            SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="memory.search",
                        arguments=f'{{"query":"responses-{index}"}}',
                    )
                ]
            )
        )[0]
        for index in range(2)
    ]
    anthropic_calls = [
        _anthropic_response_to_llm_response(
            SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="memory.search",
                        input={"query": f"anthropic-{index}"},
                    )
                ],
                stop_reason="tool_use",
            ),
            tools=[],
        ).tool_calls[0]
        for index in range(2)
    ]
    parsed_calls = [
        parse_agent_response(
            (
                '{"message":"","tool_calls":['
                f'{{"name":"memory.search","arguments":{{"query":"parsed-{index}"}}}}]}}'
            ),
            tools=[search_spec],
            strict=True,
        ).tool_calls[0]
        for index in range(2)
    ]
    ollama_calls = [
        _ollama_tool_calls(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "memory.search",
                        "arguments": {"query": f"ollama-{index}"},
                    },
                }
            ]
        )[0]
        for index in range(2)
    ]
    gemini_calls = [
        _gemini_tool_calls(
            SimpleNamespace(
                function_calls=[
                    SimpleNamespace(name="memory.search", args={"query": f"gemini-{index}"})
                ]
            )
        )[0]
        for index in range(2)
    ]

    for calls in (
        native_calls,
        stream_calls,
        responses_calls,
        anthropic_calls,
        parsed_calls,
        ollama_calls,
        gemini_calls,
    ):
        assert len(calls) == 2
        assert len({call.id for call in calls}) == 2
        assert all(call.id.startswith("tool_") for call in calls)


def test_gemini_and_ollama_preserve_valid_provider_tool_call_ids() -> None:
    gemini_call = _gemini_tool_calls(
        SimpleNamespace(
            function_calls=[
                SimpleNamespace(
                    id="gemini_exact",
                    name="memory.search",
                    args={"query": "gemini"},
                )
            ]
        )
    )[0]
    ollama_call = _ollama_tool_calls(
        [
            {
                "id": "ollama_exact",
                "type": "function",
                "function": {
                    "name": "memory.search",
                    "arguments": {"query": "ollama"},
                },
            }
        ]
    )[0]

    assert gemini_call.id == "gemini_exact"
    assert ollama_call.id == "ollama_exact"


@pytest.mark.parametrize(
    "parser",
    [
        lambda: _chat_completion_tool_calls(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            tool_calls=[
                                SimpleNamespace(
                                    id=7,
                                    function=SimpleNamespace(
                                        name="memory.search",
                                        arguments='{"query":"chat"}',
                                    ),
                                )
                            ]
                        )
                    )
                ]
            )
        ),
        lambda: _response_tool_calls(
            SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="function_call",
                        call_id=7,
                        name="memory.search",
                        arguments='{"query":"responses"}',
                    )
                ]
            )
        ),
        lambda: _anthropic_response_to_llm_response(
            SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id=7,
                        name="memory.search",
                        input={"query": "anthropic"},
                    )
                ],
                stop_reason="tool_use",
            ),
            tools=[],
        ),
        lambda: _gemini_tool_calls(
            SimpleNamespace(
                function_calls=[
                    SimpleNamespace(id=7, name="memory.search", args={"query": "gemini"})
                ]
            )
        ),
        lambda: _ollama_tool_calls(
            [
                {
                    "id": 7,
                    "type": "function",
                    "function": {
                        "name": "memory.search",
                        "arguments": {"query": "ollama"},
                    },
                }
            ]
        ),
    ],
)
def test_native_provider_parsers_do_not_coerce_explicit_call_ids(parser: Any) -> None:
    with pytest.raises(ControlMessageError) as exc_info:
        parser()

    _assert_control_code(exc_info, "invalid_tool_call_id")


@pytest.mark.parametrize(
    "renderer",
    [
        lambda message: _to_chat_completion_dict(message),
        lambda message: _to_responses_input([message]),
        lambda message: _anthropic_messages([message]),
    ],
)
def test_provider_serializers_reject_unpaired_tool_results(renderer: Any) -> None:
    with pytest.raises(ControlMessageError) as exc_info:
        renderer(ChatMessage(role="tool", content="unpaired result"))

    _assert_control_code(exc_info, "missing_tool_call_id")


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
