from types import SimpleNamespace

import pytest

from services.tool_parser import parse_agent_event


class _DummyProvider:
    async def stream(self, *args, **kwargs):
        if False:
            yield ""


def _make_response(**kwargs):
    return kwargs


def _make_event(event_type: str, **overrides):
    data = {
        "type": SimpleNamespace(value=event_type),
        "content": "",
        "tool_name": "",
        "tool_args": "",
        "tool_result": "",
        "approval_id": "",
        "task_id": "task-1",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


async def _collect(event, channel: str):
    return [
        chunk
        async for chunk in parse_agent_event(
            ("agent_event", event),
            full_response_parts=[],
            tool_results_gathered=[],
            provider=_DummyProvider(),
            model="test-model",
            api_key="",
            make_response_fn=_make_response,
            thinking_shown={"shown": [False], "channel": channel},
        )
    ]


@pytest.mark.asyncio
async def test_telegram_suppresses_verbose_tool_call_content():
    chunks = await _collect(
        _make_event("tool_called", tool_name="file_list", tool_args='{"path":"."}'),
        channel="telegram",
    )

    assert not any(chunk.get("content_delta") for chunk in chunks)
    assert any(
        chunk.get("metadata", {}).get("agent_status") == "calling"
        for chunk in chunks
    )


@pytest.mark.asyncio
async def test_telegram_approval_request_stays_metadata_only():
    chunks = await _collect(
        _make_event(
            "approval_needed",
            content="Tool requires approval",
            approval_id="approval-123",
        ),
        channel="telegram",
    )

    assert not any(chunk.get("content_delta") for chunk in chunks)
    assert any(
        chunk.get("metadata", {}).get("approval_id") == "approval-123"
        for chunk in chunks
    )


@pytest.mark.asyncio
async def test_telegram_ask_human_still_emits_reply_prompt():
    chunks = await _collect(
        _make_event(
            "approval_needed",
            content="Which version should I use?",
            approval_id="",
        ),
        channel="telegram",
    )

    combined = "".join(chunk.get("content_delta") or "" for chunk in chunks)
    assert "Reply in the chat to continue." in combined


@pytest.mark.asyncio
async def test_telegram_suppresses_step_complete_content():
    """Telegram should not stream step_complete tokens (internal commentary).

    The final response for Telegram is delivered via task_complete instead.
    """
    chunks = await _collect(
        _make_event("step_complete", content="commentary to=host find code{}"),
        channel="telegram",
    )

    assert not any(chunk.get("content_delta") for chunk in chunks)


@pytest.mark.asyncio
async def test_web_channel_streams_step_complete_content():
    """Non-Telegram channels should receive step_complete content for streaming."""
    chunks = await _collect(
        _make_event("step_complete", content="Here is the result..."),
        channel="web",
    )

    combined = "".join(chunk.get("content_delta") or "" for chunk in chunks)
    assert "Here is the result..." in combined
