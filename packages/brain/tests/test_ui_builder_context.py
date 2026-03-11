import json

import pytest

from agent.execution_context import ExecutionContext
from agent.tools import ToolRegistry
from agent.tools.ui_builder import register_ui_builder_tools
from agent.types import ToolCall
from agent.ui_artifacts import UIArtifactManager


@pytest.mark.asyncio
async def test_ui_builder_uses_execution_context_workspace_and_manager():
    registry = ToolRegistry()
    register_ui_builder_tools(registry)
    manager = UIArtifactManager()
    execution_context = ExecutionContext.create(
        task_id="task-ui",
        queue_id="queue-ui",
        agent_profile_id="agent-ui",
        workspace_id="workspace-ui",
        user_id="user-ui",
        source="chat",
        services={
            "ui_manager": manager,
            "ui_artifact_manager": manager,
        },
    )

    create_result = await registry.execute(
        ToolCall(
            name="create_ui_artifact",
            arguments={
                "title": "Ops Board",
                "description": "Queue status dashboard",
                "component_code": "<div>hello</div>",
                "component_type": "html",
            },
        ),
        context=execution_context.to_tool_context(),
    )
    create_payload = json.loads(create_result.output)

    assert create_result.success is True
    assert create_payload["success"] is True
    assert create_payload["artifact"]["workspace_id"] == "workspace-ui"

    list_result = await registry.execute(
        ToolCall(name="list_ui_artifacts", arguments={}),
        context=execution_context.to_tool_context(),
    )
    list_payload = json.loads(list_result.output)

    assert list_result.success is True
    assert list_payload["count"] == 1
    assert list_payload["artifacts"][0]["title"] == "Ops Board"
