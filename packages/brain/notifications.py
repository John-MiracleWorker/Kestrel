"""
Notification Router — delivers proactive notifications from Brain to users.

Sources:
  - Cron job completions
  - Webhook events
  - Background agent insights
  - @mentions

Delivery channels:
  - WebSocket push (web)
  - Future: Telegram DM, mobile push (FCM)
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("brain.notifications")


@dataclass
class Notification:
    """A notification to deliver to a user."""
    id: str
    user_id: str
    workspace_id: str
    type: str  # 'info', 'success', 'warning', 'task_complete', 'mention'
    title: str
    body: str = ""
    source: str = ""
    data: dict = field(default_factory=dict)
    read: bool = False
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "body": self.body,
            "source": self.source,
            "data": self.data,
            "read": self.read,
            "created_at": self.created_at,
        }


class NotificationRouter:
    """
    Routes notifications to users via available channels.

    Rate-limited to 5 notifications per user per hour.
    """

    def __init__(self, pool, redis_pool=None):
        self._pool = pool
        self._redis = redis_pool
        self._ws_callback: Optional[Callable] = None
        self._rate_counts: dict[str, list[float]] = {}  # user_id -> timestamps
        self._max_per_hour = 5

    def set_ws_callback(self, callback: Callable):
        """Set callback for WebSocket delivery: callback(user_id, payload)"""
        self._ws_callback = callback

    def _rate_limited(self, user_id: str) -> bool:
        """Check if user has hit rate limit."""
        now = datetime.now(timezone.utc).timestamp()
        timestamps = self._rate_counts.get(user_id, [])
        # Remove entries older than 1 hour
        timestamps = [t for t in timestamps if now - t < 3600]
        self._rate_counts[user_id] = timestamps
        return len(timestamps) >= self._max_per_hour

    async def send(
        self,
        user_id: str,
        workspace_id: str,
        type: str,
        title: str,
        body: str = "",
        source: str = "",
        data: dict = None,
    ) -> Optional[Notification]:
        """Send a notification to a user."""
        if self._rate_limited(user_id):
            logger.warning(f"Rate limited notifications for user {user_id}")
            return None

        notification = Notification(
            id=str(uuid.uuid4()),
            user_id=user_id,
            workspace_id=workspace_id,
            type=type,
            title=title,
            body=body,
            source=source,
            data=data or {},
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Persist to DB
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO notifications
                        (id, user_id, workspace_id, type, title, body, source, data, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                    """,
                    notification.id, user_id, workspace_id, type,
                    title, body, source, json.dumps(data or {}),
                    datetime.fromisoformat(notification.created_at),
                )
        except Exception as e:
            logger.error(f"Failed to persist notification: {e}")

        # Track rate
        now = datetime.now(timezone.utc).timestamp()
        self._rate_counts.setdefault(user_id, []).append(now)

        # Deliver via WebSocket
        if self._ws_callback:
            try:
                await self._ws_callback(user_id, {
                    "type": "notification",
                    "notification": notification.to_dict(),
                })
            except Exception as e:
                logger.error(f"WS notification delivery failed: {e}")

        # Publish to Redis so Gateway can push to WebSockets
        if self._redis:
            try:
                payload = json.dumps({
                    "userId": user_id,
                    "notification": notification.to_dict(),
                }, default=str)
                await self._redis.publish("notifications", payload)
            except Exception as e:
                logger.error(f"Redis notification publish failed: {e}")

        logger.info(f"Notification sent: {title} → {user_id} (via {source})")
        return notification

    async def get_unread(self, user_id: str, limit: int = 20) -> list[dict]:
        """Get unread notifications for a user."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, type, title, body, source, data, read, created_at
                    FROM notifications
                    WHERE user_id = $1 AND read = false
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    user_id, limit,
                )
                return [
                    {
                        "id": str(r["id"]),
                        "type": r["type"],
                        "title": r["title"],
                        "body": r["body"],
                        "source": r["source"],
                        "data": json.loads(r["data"]) if isinstance(r["data"], str) else r["data"],
                        "read": r["read"],
                        "created_at": str(r["created_at"]),
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Failed to fetch notifications: {e}")
            return []

    async def mark_read(self, notification_id: str) -> bool:
        """Mark a notification as read."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE notifications SET read = true WHERE id = $1",
                    notification_id,
                )
            return True
        except Exception as e:
            logger.error(f"Failed to mark notification read: {e}")
            return False

    async def mark_all_read(self, user_id: str) -> int:
        """Mark all notifications as read for a user."""
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE notifications SET read = true WHERE user_id = $1 AND read = false",
                    user_id,
                )
                count = int(result.split(" ")[-1]) if result else 0
                return count
        except Exception as e:
            logger.error(f"Failed to mark all read: {e}")
            return 0
