"""
Agent-to-Agent Session Management — cross-session messaging and discovery.

Inspired by OpenClaw's sessions_* tools:
  - sessions_list  — discover active agent sessions
  - sessions_send  — send a message to another session
  - sessions_history — read another session's transcript

This enables agents to collaborate across sessions, share findings,
and coordinate multi-step work without sharing token context.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("brain.agent.sessions")


@dataclass
class SessionMessage:
    """A message exchanged between agent sessions."""
    id: str
    from_session_id: str
    to_session_id: str
    content: str
    message_type: str = "text"  # text, request, response, announce
    reply_to: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_session_id": self.from_session_id,
            "to_session_id": self.to_session_id,
            "content": self.content,
            "message_type": self.message_type,
            "reply_to": self.reply_to,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


@dataclass
class SessionInfo:
    """Metadata about an active agent session."""
    session_id: str
    task_id: Optional[str]
    workspace_id: str
    user_id: str
    agent_type: str  # "main", "specialist", "cron"
    status: str  # "active", "idle", "paused", "completed"
    model: str = ""
    current_goal: str = ""
    token_usage: int = 0
    started_at: str = ""
    last_activity: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "agent_type": self.agent_type,
            "status": self.status,
            "model": self.model,
            "current_goal": self.current_goal[:200] if self.current_goal else "",
            "token_usage": self.token_usage,
            "started_at": self.started_at,
            "last_activity": self.last_activity,
        }


class SessionManager:
    """
    Manages inter-agent session communication.

    Sessions are registered when an agent task starts and deregistered
    when it completes. Messages between sessions are persisted in
    PostgreSQL and optionally pushed via Redis pub/sub for real-time delivery.
    """

    def __init__(self, pool, redis=None):
        self._pool = pool
        self._redis = redis
        self._sessions: dict[str, SessionInfo] = {}

    async def register_session(
        self,
        session_id: str,
        task_id: str,
        workspace_id: str,
        user_id: str,
        agent_type: str = "main",
        model: str = "",
        goal: str = "",
    ) -> None:
        """Register a new active session."""
        now = datetime.now(timezone.utc).isoformat()
        info = SessionInfo(
            session_id=session_id,
            task_id=task_id,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_type=agent_type,
            status="active",
            model=model,
            current_goal=goal,
            started_at=now,
            last_activity=now,
        )
        self._sessions[session_id] = info

        # Persist to DB
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO agent_sessions (id, task_id, workspace_id, user_id,
                        agent_type, status, model, current_goal, started_at, last_activity)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)
                    ON CONFLICT (id) DO UPDATE SET
                        status = 'active', last_activity = $9, current_goal = $8
                    """,
                    session_id, task_id, workspace_id, user_id,
                    agent_type, "active", model, goal, now,
                )
        except Exception as e:
            logger.error(f"Failed to persist session: {e}")

        logger.info(f"Session registered: {session_id} ({agent_type})")

    async def deregister_session(self, session_id: str) -> None:
        """Mark a session as completed."""
        if session_id in self._sessions:
            self._sessions[session_id].status = "completed"

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_sessions SET status = 'completed', last_activity = $2 WHERE id = $1",
                    session_id, datetime.now(timezone.utc).isoformat(),
                )
        except Exception as e:
            logger.error(f"Failed to deregister session: {e}")

    async def list_sessions(
        self,
        workspace_id: str,
        active_only: bool = True,
    ) -> list[dict]:
        """List sessions, optionally filtered to active only."""
        try:
            async with self._pool.acquire() as conn:
                query = "SELECT * FROM agent_sessions WHERE workspace_id = $1"
                if active_only:
                    query += " AND status = 'active'"
                query += " ORDER BY last_activity DESC LIMIT 50"

                rows = await conn.fetch(query, workspace_id)
                return [
                    {
                        "session_id": row["id"],
                        "task_id": row["task_id"],
                        "agent_type": row["agent_type"],
                        "status": row["status"],
                        "model": row["model"],
                        "current_goal": (row["current_goal"] or "")[:200],
                        "started_at": str(row["started_at"]),
                        "last_activity": str(row["last_activity"]),
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return list(self._sessions.values())

    async def send_message(
        self,
        from_session_id: str,
        to_session_id: str,
        content: str,
        message_type: str = "text",
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SessionMessage:
        """Send a message from one session to another."""
        msg = SessionMessage(
            id=str(uuid.uuid4()),
            from_session_id=from_session_id,
            to_session_id=to_session_id,
            content=content,
            message_type=message_type,
            reply_to=reply_to,
            metadata=metadata or {},
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Persist
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO agent_session_messages
                        (id, from_session_id, to_session_id, content,
                         message_type, reply_to, metadata, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                    """,
                    msg.id, msg.from_session_id, msg.to_session_id,
                    msg.content, msg.message_type, msg.reply_to,
                    json.dumps(msg.metadata), msg.created_at,
                )
        except Exception as e:
            logger.error(f"Failed to persist session message: {e}")

        # Real-time push via Redis pub/sub
        if self._redis:
            try:
                await self._redis.publish(
                    f"session:{to_session_id}:messages",
                    json.dumps(msg.to_dict()),
                )
            except Exception as e:
                logger.warning(f"Redis publish failed: {e}")

        logger.info(f"Session message: {from_session_id} → {to_session_id}")
        return msg

    async def get_history(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch message history for a session (sent + received)."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT * FROM agent_session_messages
                    WHERE from_session_id = $1 OR to_session_id = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    session_id, limit,
                )
                return [
                    {
                        "id": row["id"],
                        "from_session_id": row["from_session_id"],
                        "to_session_id": row["to_session_id"],
                        "content": row["content"],
                        "message_type": row["message_type"],
                        "reply_to": row["reply_to"],
                        "created_at": str(row["created_at"]),
                    }
                    for row in reversed(rows)
                ]
        except Exception as e:
            logger.error(f"Failed to get session history: {e}")
            return []

    async def get_inbox(
        self,
        session_id: str,
        since: Optional[str] = None,
    ) -> list[dict]:
        """Get unread/recent messages sent to a specific session."""
        try:
            async with self._pool.acquire() as conn:
                if since:
                    rows = await conn.fetch(
                        """
                        SELECT * FROM agent_session_messages
                        WHERE to_session_id = $1 AND created_at > $2
                        ORDER BY created_at ASC
                        """,
                        session_id, since,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT * FROM agent_session_messages
                        WHERE to_session_id = $1
                        ORDER BY created_at DESC
                        LIMIT 20
                        """,
                        session_id,
                    )

                return [
                    {
                        "id": row["id"],
                        "from_session_id": row["from_session_id"],
                        "content": row["content"],
                        "message_type": row["message_type"],
                        "created_at": str(row["created_at"]),
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Failed to get inbox: {e}")
            return []
