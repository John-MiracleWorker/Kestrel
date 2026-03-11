"""
PostgresCheckpointer — bridges Kestrel's CheckpointManager to LangGraph's
checkpointer interface.

LangGraph expects a checkpointer that implements get/put for serialized
graph state. This adapter wraps Kestrel's existing PostgreSQL-backed
CheckpointManager to provide that interface.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("brain.agent.runtime.checkpointer")


def _decode_state(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    return json.loads(value)


class PostgresCheckpointer:
    """
    LangGraph-compatible checkpointer backed by Kestrel's PostgreSQL
    checkpoint storage.

    Implements the minimal interface LangGraph needs:
      - aget(config) -> Optional[checkpoint]
      - aput(config, checkpoint, metadata) -> config
      - alist(config) -> AsyncIterator[checkpoint]
    """

    def __init__(self, checkpoint_manager):
        """
        Args:
            checkpoint_manager: Kestrel's CheckpointManager instance
                (from packages/brain/agent/checkpoints.py)
        """
        self._manager = checkpoint_manager

    async def aget(self, config: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Retrieve the latest checkpoint for a thread."""
        thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id:
            return None

        try:
            latest = await self._manager.get_latest_checkpoint(task_id=thread_id)
            if not latest:
                return None
            return {
                "id": latest.id,
                "ts": latest.created_at,
                "channel_values": _decode_state(latest.state_json),
            }
        except Exception as e:
            logger.warning(f"Checkpoint retrieval failed for {thread_id}: {e}")
            return None

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Persist a checkpoint."""
        thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id:
            return config

        try:
            state_json = json.dumps(
                checkpoint.get("channel_values", {}),
                default=str,
            )
            step_index = metadata.get("step", 0) if metadata else 0
            label = metadata.get("node", "unknown") if metadata else "unknown"

            await self._manager.create_checkpoint(
                task_id=thread_id,
                step_index=step_index,
                label=f"langgraph:{label}",
                state_json=state_json,
            )
        except Exception as e:
            logger.warning(f"Checkpoint save failed for {thread_id}: {e}")

        return config

    async def alist(self, config: dict[str, Any]):
        """List all checkpoints for a thread (async generator)."""
        thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id:
            return

        try:
            checkpoints = await self._manager.list_checkpoints(task_id=thread_id)
            for cp in checkpoints:
                yield {
                    "id": cp.id,
                    "ts": cp.created_at,
                    "channel_values": _decode_state(cp.state_json),
                }
        except Exception as e:
            logger.warning(f"Checkpoint listing failed for {thread_id}: {e}")
