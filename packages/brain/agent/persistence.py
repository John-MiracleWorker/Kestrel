"""
PostgreSQL-backed persistence for agent tasks and approvals.

Implements the TaskPersistence interface from agent.loop using asyncpg.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Any

from agent.types import (
    AgentTask,
    ApprovalRequest,
    ApprovalStatus,
    GuardrailConfig,
    RiskLevel,
    TaskPlan,
    TaskStatus,
)
from agent.loop import TaskPersistence

logger = logging.getLogger("brain.agent.persistence")


class PostgresTaskPersistence(TaskPersistence):
    """Concrete implementation of TaskPersistence using PostgreSQL."""

    def __init__(self, pool):
        self._pool = pool

    async def save_task(self, task: AgentTask) -> None:
        """Insert a new agent task."""
        # Protobuf sends empty strings for unset fields; PostgreSQL UUID
        # columns reject '' — convert to None for nullable UUID cols.
        conv_id = task.conversation_id or None
        ws_id = task.workspace_id or None

        # Ensure user row exists (gateway may authenticate users not yet
        # in brain's local users table). ON CONFLICT DO NOTHING is safe.
        if task.user_id:
            placeholder_email = f"{task.user_id}@placeholder.local"
            await self._pool.execute(
                """INSERT INTO users (id, email, password_hash, salt, display_name, created_at)
                   VALUES ($1, $2, '', '', 'User', NOW())
                   ON CONFLICT (id) DO NOTHING""",
                task.user_id, placeholder_email,
            )

        await self._pool.execute(
            """
            INSERT INTO agent_tasks (id, user_id, workspace_id, conversation_id,
                                     goal, status, plan, config, result, error,
                                     token_usage, tool_calls_count, iterations)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            task.id,
            task.user_id,
            ws_id,
            conv_id,
            task.goal,
            task.status.value if isinstance(task.status, TaskStatus) else task.status,
            json.dumps(task.plan.to_dict()) if task.plan else None,
            json.dumps(task.config.to_dict()),
            task.result,
            task.error,
            task.token_usage,
            task.tool_calls_count,
            task.iterations,
        )

    async def update_task(self, task: AgentTask) -> None:
        """Update an existing agent task."""
        await self._pool.execute(
            """
            UPDATE agent_tasks
            SET status = $2, plan = $3, config = $4, result = $5, error = $6,
                token_usage = $7, tool_calls_count = $8, iterations = $9,
                completed_at = $10
            WHERE id = $1
            """,
            task.id,
            task.status.value if isinstance(task.status, TaskStatus) else task.status,
            json.dumps(task.plan.to_dict()) if task.plan else None,
            json.dumps(task.config.to_dict()),
            task.result,
            task.error,
            task.token_usage,
            task.tool_calls_count,
            task.iterations,
            task.completed_at,
        )

    async def get_task(self, task_id: str) -> Optional[AgentTask]:
        """Load a task from the database."""
        row = await self._pool.fetchrow(
            "SELECT * FROM agent_tasks WHERE id = $1", task_id,
        )
        if not row:
            return None

        task = AgentTask(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            workspace_id=str(row["workspace_id"]),
            conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
            goal=row["goal"],
            status=TaskStatus(row["status"]),
            plan=TaskPlan.from_dict(json.loads(row["plan"])) if row["plan"] else None,
            config=GuardrailConfig.from_dict(json.loads(row["config"])) if row["config"] else GuardrailConfig(),
            result=row["result"],
            error=row["error"],
            token_usage=row["token_usage"],
            tool_calls_count=row["tool_calls_count"],
            iterations=row["iterations"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )
        return task

    async def save_approval(self, approval: ApprovalRequest) -> None:
        """Insert a new approval request."""
        await self._pool.execute(
            """
            INSERT INTO agent_approvals (id, task_id, step_id, tool_name,
                                         tool_args, risk_level, reason, status,
                                         expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            approval.id,
            approval.task_id,
            approval.step_id,
            approval.tool_name,
            json.dumps(approval.tool_args),
            approval.risk_level.value if isinstance(approval.risk_level, RiskLevel) else approval.risk_level,
            approval.reason,
            approval.status.value if isinstance(approval.status, ApprovalStatus) else approval.status,
            approval.expires_at,
        )

    async def get_approval(self, approval_id: str) -> Optional[ApprovalRequest]:
        """Load an approval request."""
        row = await self._pool.fetchrow(
            "SELECT * FROM agent_approvals WHERE id = $1", approval_id,
        )
        if not row:
            return None

        return ApprovalRequest(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            step_id=row["step_id"],
            tool_name=row["tool_name"],
            tool_args=json.loads(row["tool_args"]) if row["tool_args"] else {},
            risk_level=RiskLevel(row["risk_level"]),
            reason=row["reason"],
            status=ApprovalStatus(row["status"]),
            decided_by=str(row["decided_by"]) if row["decided_by"] else None,
            decided_at=row["decided_at"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
        )

    async def resolve_approval(
        self,
        approval_id: str,
        status: ApprovalStatus,
        decided_by: str,
    ) -> bool:
        """Resolve a pending approval request owned by the deciding user."""
        row = await self._pool.fetchrow(
            """
            UPDATE agent_approvals AS a
            SET status = $2, decided_by = $3, decided_at = now()
            FROM agent_tasks AS t
            WHERE a.id = $1
              AND a.status = 'pending'
              AND a.task_id = t.id
              AND t.user_id = $3
            RETURNING a.id
            """,
            approval_id,
            status.value if isinstance(status, ApprovalStatus) else status,
            decided_by,
        )
        return row is not None

    async def list_pending_approvals(self, user_id: str, workspace_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List unresolved approval requests owned by a user."""
        query = """
            SELECT a.id, a.task_id, a.tool_name, a.reason, a.created_at
            FROM agent_approvals AS a
            JOIN agent_tasks AS t ON t.id = a.task_id
            WHERE a.status = 'pending'
              AND t.user_id = $1
        """
        params: list[Any] = [user_id]

        if workspace_id:
            query += " AND t.workspace_id = $2"
            params.append(workspace_id)

        query += " ORDER BY a.created_at DESC"

        rows = await self._pool.fetch(query, *params)
        return [
            {
                "approval_id": str(row["id"]),
                "task_id": str(row["task_id"]),
                "tool_name": row["tool_name"] or "",
                "reason": row["reason"] or "",
                "created_at": row["created_at"],
            }
            for row in rows
        ]
