"""Manage agent loop execution and stream response chunks."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Callable

from core.config import logger
from core.grpc_setup import brain_pb2


_SENTINEL = object()


def make_activity_callback(output_queue: asyncio.Queue, make_response: Callable):
    """Create the activity callback that pushes events to the output queue."""

    async def _activity_callback(activity_type: str, data: dict):
        """Push activity events directly to the output stream."""
        if activity_type in (
            "delegation_started", "delegation_progress",
            "delegation_complete", "parallel_delegation_started",
            "parallel_delegation_complete",
            "council_started", "council_opinion",
            "council_debate", "council_verdict",
        ):
            await output_queue.put(make_response(
                chunk_type=0,
                metadata={
                    "agent_status": "delegation",
                    "delegation_type": activity_type,
                    "delegation": json.dumps(data),
                },
            ))
        elif activity_type == "routing_info":
            await output_queue.put(make_response(
                chunk_type=0,
                metadata={
                    "agent_status": "routing_info",
                    "provider": data.get("provider", ""),
                    "model": data.get("model", ""),
                    "was_escalated": str(data.get("was_escalated", False)).lower(),
                    "complexity": str(data.get("complexity", 0)),
                },
            ))
        else:
            await output_queue.put(make_response(
                chunk_type=0,
                metadata={
                    "agent_status": "agent_activity",
                    "activity": json.dumps({
                        "activity_type": activity_type, **data
                    }),
                },
            ))

    return _activity_callback


async def run_chat_stream(
    agent_loop: Any,
    chat_task: Any,
    output_queue: asyncio.Queue,
    make_response: Callable,
    provider: Any,
    model: str,
    api_key: str,
    full_response_parts: list[str],
) -> AsyncGenerator:
    """Run the agent loop in background and yield response chunks.

    Args:
        agent_loop: AgentLoop instance.
        chat_task: Configured AgentTask.
        output_queue: Unified asyncio.Queue for all events.
        make_response: Factory callable to build ChatResponse protobuf.
        provider: Active LLM provider.
        model: Active model identifier.
        api_key: API key for the provider.
        full_response_parts: Mutable list; accumulated response text is appended here.

    Yields:
        ChatResponse protobuf chunks.
    """
    tool_results_gathered: list = []

    async def _run_agent_loop():
        """Run agent loop in background, pushing events to output_queue."""
        try:
            async for event in agent_loop.run(chat_task):
                await output_queue.put(("agent_event", event))
        except Exception as e:
            logger.error(f"Agent loop error in background: {e}", exc_info=True)
            await output_queue.put(("error", str(e)))
        finally:
            await output_queue.put(_SENTINEL)

    agent_task_bg = asyncio.create_task(_run_agent_loop())
    thinking_shown = [False]

    try:
        while True:
            item = await output_queue.get()

            if item is _SENTINEL:
                break

            # Direct response chunks from activity callbacks
            if isinstance(item, brain_pb2.ChatResponse):
                yield item
                continue

            # Tuple from the agent loop background task
            if isinstance(item, tuple):
                from services.tool_parser import parse_agent_event
                async for response_chunk in parse_agent_event(
                    item, full_response_parts, tool_results_gathered,
                    provider, model, api_key, make_response,
                    thinking_shown=thinking_shown,
                ):
                    yield response_chunk
    finally:
        if not agent_task_bg.done():
            agent_task_bg.cancel()
