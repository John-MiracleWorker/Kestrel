from datetime import datetime, timezone

import pytest

from agent.sessions import SessionInfo, SessionManager


class _FakeConn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.rows


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.mark.asyncio
async def test_session_manager_prunes_expired_sessions():
    fake_conn = _FakeConn(
        rows=[
            {
                "id": "session-1",
                "task_id": "task-1",
                "workspace_id": "workspace-1",
            }
        ]
    )
    manager = SessionManager(pool=_FakePool(fake_conn))
    manager._sessions["session-1"] = SessionInfo(
        session_id="session-1",
        task_id="task-1",
        workspace_id="workspace-1",
        user_id="user-1",
        agent_type="task",
        status="active",
        started_at=datetime.now(timezone.utc).isoformat(),
        last_activity=datetime.now(timezone.utc).isoformat(),
    )

    result = await manager.prune_inactive_sessions(limit=10)

    assert result["pruned_count"] == 1
    assert result["session_ids"] == ["session-1"]
    assert manager._sessions["session-1"].status == "completed"
    assert fake_conn.calls
