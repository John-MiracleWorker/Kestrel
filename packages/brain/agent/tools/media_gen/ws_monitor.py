"""
WebSocket Monitor — real-time generation progress listener for ComfyUI.

Connects to the ComfyUI WebSocket endpoint and yields progress events
as the generation pipeline executes on the remote host. Designed to be
non-blocking within the Kestrel Agent's async event loop.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import AsyncGenerator, Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from agent.tools.media_gen.config import (
    CLIENT_ID,
    SWARM_WS_URL,
    WS_CONNECT_TIMEOUT,
)

logger = logging.getLogger("brain.agent.tools.media_gen.ws_monitor")


# ── Event Types ──────────────────────────────────────────────────────


class EventType(str, Enum):
    """ComfyUI WebSocket event types we care about."""
    PROGRESS = "progress"
    EXECUTED = "executed"
    EXECUTING = "executing"
    EXECUTION_START = "execution_start"
    EXECUTION_ERROR = "execution_error"
    EXECUTION_CACHED = "execution_cached"


@dataclass
class ProgressEvent:
    """A generation progress update."""
    event_type: EventType
    prompt_id: Optional[str] = None
    node_id: Optional[str] = None
    current_step: int = 0
    max_steps: int = 0
    percentage: float = 0.0
    is_complete: bool = False
    is_error: bool = False
    error_message: str = ""

    @property
    def progress_text(self) -> str:
        """Human-readable progress string."""
        if self.is_error:
            return f"Error: {self.error_message}"
        if self.is_complete:
            return "Generation complete"
        if self.max_steps > 0:
            return f"Step {self.current_step}/{self.max_steps} ({self.percentage:.0f}%)"
        if self.event_type == EventType.EXECUTING:
            return f"Executing node {self.node_id}..."
        return f"Event: {self.event_type.value}"


# ── Monitor ──────────────────────────────────────────────────────────


async def monitor_generation(
    prompt_id: str,
    timeout: float = 300,
    client_id: str = None,
    ws_url: str = None,
) -> AsyncGenerator[ProgressEvent, None]:
    """
    Connect to ComfyUI WebSocket and yield progress events for a specific job.

    This is an async generator that yields ProgressEvent objects as the
    generation progresses. It terminates when:
      - The pipeline completes (executing with node=None)
      - An execution error occurs
      - The timeout is reached

    Args:
        prompt_id: The prompt_id to monitor (from SwarmClient.submit_job).
        timeout: Max seconds to wait for completion.
        client_id: The client ID used when submitting the job.
        ws_url: Override WebSocket base URL.

    Yields:
        ProgressEvent objects with generation status updates.
    """
    cid = client_id or CLIENT_ID
    base = (ws_url or SWARM_WS_URL).rstrip("/")
    uri = f"{base}/ws?clientId={cid}"

    logger.info(f"Connecting to WebSocket: {uri} (prompt_id={prompt_id})")

    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(
                uri,
                open_timeout=WS_CONNECT_TIMEOUT,
                ping_interval=20,
                ping_timeout=60,
                max_size=50 * 1024 * 1024,  # 50 MB for preview images
            ) as ws:
                logger.info("WebSocket connected, listening for events...")

                async for raw_msg in ws:
                    # ComfyUI sends binary data for preview images — skip
                    if isinstance(raw_msg, bytes):
                        continue

                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.warning(f"Non-JSON WebSocket message: {raw_msg[:100]}")
                        continue

                    msg_type = msg.get("type", "")
                    data = msg.get("data", {})

                    # Only process events for our prompt
                    msg_prompt_id = data.get("prompt_id")
                    if msg_prompt_id and msg_prompt_id != prompt_id:
                        continue

                    event = _parse_event(msg_type, data, prompt_id)
                    if event is None:
                        continue

                    yield event

                    # Terminal conditions
                    if event.is_complete or event.is_error:
                        return

    except asyncio.TimeoutError:
        logger.warning(f"WebSocket monitor timed out after {timeout}s for prompt_id={prompt_id}")
        yield ProgressEvent(
            event_type=EventType.EXECUTION_ERROR,
            prompt_id=prompt_id,
            is_error=True,
            error_message=f"Generation timed out after {timeout} seconds",
        )

    except (ConnectionRefusedError, OSError) as e:
        logger.error(f"Cannot connect to WebSocket at {uri}: {e}")
        yield ProgressEvent(
            event_type=EventType.EXECUTION_ERROR,
            prompt_id=prompt_id,
            is_error=True,
            error_message=(
                f"Cannot connect to SwarmUI WebSocket. "
                f"Is the host PC awake and SwarmUI running? Error: {e}"
            ),
        )

    except ConnectionClosed as e:
        logger.warning(f"WebSocket closed unexpectedly: {e}")
        yield ProgressEvent(
            event_type=EventType.EXECUTION_ERROR,
            prompt_id=prompt_id,
            is_error=True,
            error_message=f"WebSocket connection closed: {e}",
        )


def _parse_event(msg_type: str, data: dict, prompt_id: str) -> Optional[ProgressEvent]:
    """Parse a raw ComfyUI WebSocket message into a ProgressEvent."""

    if msg_type == "progress":
        current = data.get("value", 0)
        maximum = data.get("max", 0)
        pct = (current / maximum * 100) if maximum > 0 else 0
        return ProgressEvent(
            event_type=EventType.PROGRESS,
            prompt_id=prompt_id,
            node_id=data.get("node"),
            current_step=current,
            max_steps=maximum,
            percentage=pct,
        )

    elif msg_type == "executing":
        node_id = data.get("node")
        # node is None → entire pipeline finished
        if node_id is None:
            return ProgressEvent(
                event_type=EventType.EXECUTING,
                prompt_id=prompt_id,
                is_complete=True,
            )
        return ProgressEvent(
            event_type=EventType.EXECUTING,
            prompt_id=prompt_id,
            node_id=node_id,
        )

    elif msg_type == "executed":
        return ProgressEvent(
            event_type=EventType.EXECUTED,
            prompt_id=prompt_id,
            node_id=data.get("node"),
        )

    elif msg_type == "execution_start":
        return ProgressEvent(
            event_type=EventType.EXECUTION_START,
            prompt_id=prompt_id,
        )

    elif msg_type == "execution_error":
        return ProgressEvent(
            event_type=EventType.EXECUTION_ERROR,
            prompt_id=prompt_id,
            node_id=data.get("node_id"),
            is_error=True,
            error_message=data.get("exception_message", "Unknown execution error"),
        )

    elif msg_type == "execution_cached":
        return ProgressEvent(
            event_type=EventType.EXECUTION_CACHED,
            prompt_id=prompt_id,
        )

    return None
