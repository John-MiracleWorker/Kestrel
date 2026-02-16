"""
State Checkpointing — save and restore agent task state for rollback.

Auto-checkpoints before high-risk tool calls so tasks can be
rewound to a known-good state if something goes wrong.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("brain.agent.checkpoints")


@dataclass
class Checkpoint:
    """Snapshot of agent task state at a point in time."""
    id: str
    task_id: str
    step_index: int
    label: str
    state_json: str  # serialized task state
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "step_index": self.step_index,
            "label": self.label,
            "created_at": self.created_at,
        }


class CheckpointManager:
    """
    Manages task state checkpoints for rollback.

    Checkpoints are stored in PostgreSQL and contain the full serialized
    task state (plan, step results, iterator counts, etc).
    """

    def __init__(self, pool):
        """pool is an asyncpg connection pool."""
        self._pool = pool

    async def save_checkpoint(
        self,
        task,
        label: str = "",
    ) -> str:
        """
        Save a checkpoint of the current task state.
        Returns the checkpoint ID.
        """
        checkpoint_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Determine step index
        step_index = 0
        if task.plan:
            done, _ = task.plan.progress
            step_index = done

        if not label:
            label = f"Step {step_index} checkpoint"

        # Serialize task state
        state = {
            "id": task.id,
            "status": task.status.value,
            "iterations": task.iterations,
            "tool_calls_count": task.tool_calls_count,
            "token_usage": task.token_usage,
            "result": task.result,
            "error": task.error,
            "plan": task.plan.to_dict() if task.plan else None,
        }

        state_json = json.dumps(state)

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO agent_checkpoints (id, task_id, step_index, label, state_json, created_at)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                    """,
                    checkpoint_id, task.id, step_index, label,
                    state_json, now,
                )

            logger.info(f"Checkpoint saved: {checkpoint_id} for task {task.id}")
            return checkpoint_id

        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            return ""

    async def restore_checkpoint(
        self,
        task,
        checkpoint_id: str,
    ) -> bool:
        """
        Restore a task to a previous checkpoint.
        Returns True if successful.
        """
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT state_json FROM agent_checkpoints WHERE id = $1 AND task_id = $2",
                    checkpoint_id, task.id,
                )

            if not row:
                logger.warning(f"Checkpoint {checkpoint_id} not found")
                return False

            state = json.loads(row["state_json"])

            # Restore task fields
            from agent.types import TaskStatus, TaskPlan
            task.status = TaskStatus(state["status"])
            task.iterations = state["iterations"]
            task.tool_calls_count = state["tool_calls_count"]
            task.token_usage = state["token_usage"]
            task.result = state.get("result")
            task.error = state.get("error")
            if state.get("plan"):
                task.plan = TaskPlan.from_dict(state["plan"])

            logger.info(f"Restored checkpoint {checkpoint_id} for task {task.id}")
            return True

        except Exception as e:
            logger.error(f"Failed to restore checkpoint: {e}")
            return False

    async def list_checkpoints(self, task_id: str) -> list[Checkpoint]:
        """List all checkpoints for a task, ordered by creation time."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, task_id, step_index, label, created_at
                    FROM agent_checkpoints
                    WHERE task_id = $1
                    ORDER BY created_at ASC
                    """,
                    task_id,
                )

            return [
                Checkpoint(
                    id=row["id"],
                    task_id=row["task_id"],
                    step_index=row["step_index"],
                    label=row["label"],
                    state_json="",  # Don't load full state for listing
                    created_at=row["created_at"],
                )
                for row in rows
            ]

        except Exception as e:
            logger.error(f"Failed to list checkpoints: {e}")
            return []

    async def cleanup(self, task_id: str) -> None:
        """Remove all checkpoints for a completed task (keep last 3)."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    DELETE FROM agent_checkpoints
                    WHERE task_id = $1
                    AND id NOT IN (
                        SELECT id FROM agent_checkpoints
                        WHERE task_id = $1
                        ORDER BY created_at DESC
                        LIMIT 3
                    )
                    """,
                    task_id,
                )
        except Exception as e:
            logger.error(f"Checkpoint cleanup failed: {e}")
