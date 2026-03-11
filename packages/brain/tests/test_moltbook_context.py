import pytest

from agent.execution_context import ExecutionContext
from agent.tools import moltbook as moltbook_tools
from agent.tools import moltbook_autonomous


@pytest.mark.asyncio
async def test_moltbook_session_uses_execution_context_workspace(monkeypatch):
    seen = {
        "workspace_ids": [],
        "recorded_workspace_ids": [],
    }

    async def _fake_feed(**kwargs):
        return {
            "feed": [
                {
                    "id": "post-1",
                    "title": "Agent memory matters",
                    "content": "agent memory and autonomous systems",
                    "submolt": kwargs.get("submolt", "agents"),
                    "upvotes": 3,
                    "comments": 1,
                }
            ]
        }

    async def _fake_recently_engaged(workspace_id: str, hours: int = 48):
        seen["workspace_ids"].append(workspace_id)
        return set()

    async def _fake_memory_context(workspace_id: str):
        seen["workspace_ids"].append(workspace_id)
        return "Prior memory"

    async def _fake_record_session(workspace_id: str, engaged_posts: list[dict]):
        seen["recorded_workspace_ids"].append(workspace_id)

    monkeypatch.setattr(moltbook_tools, "_load_api_key", lambda: "test-key")
    monkeypatch.setattr(moltbook_tools, "_get_feed", _fake_feed)
    monkeypatch.setattr(moltbook_autonomous, "get_recently_engaged_post_ids", _fake_recently_engaged)
    monkeypatch.setattr(moltbook_autonomous, "get_memory_context", _fake_memory_context)
    monkeypatch.setattr(moltbook_autonomous, "record_session_in_memory_graph", _fake_record_session)

    execution_context = ExecutionContext.create(
        task_id="task-social",
        queue_id="queue-social",
        agent_profile_id="agent-social",
        workspace_id="workspace-social",
        user_id="user-social",
        source="automation",
    )

    result = await moltbook_autonomous.moltbook_session(
        execution_context=execution_context,
        submolts=["agents"],
        limit_per_submolt=2,
    )

    assert result["status"] == "session_ready"
    assert result["relevant_post_count"] == 1
    assert seen["workspace_ids"] == ["workspace-social", "workspace-social"]
    assert seen["recorded_workspace_ids"] == ["workspace-social"]
