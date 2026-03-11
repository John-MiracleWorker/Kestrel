from __future__ import annotations
"""
UI Builder tools — let the agent create and iterate on persistent
interactive UI components during chat.
"""

import logging
from agent.types import ToolDefinition, RiskLevel

logger = logging.getLogger("brain.agent.tools.ui_builder")


CREATE_UI_TOOL = ToolDefinition(
    name="create_ui_artifact",
    description=(
        "Generate a persistent interactive UI component (dashboard, form, viewer, etc.) "
        "from a natural language description. The component is saved as a workspace artifact "
        "that the user can view and interact with."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Title for the UI artifact",
            },
            "description": {
                "type": "string",
                "description": "What the UI should do and look like",
            },
            "component_code": {
                "type": "string",
                "description": (
                    "The React/HTML component code. Should be a self-contained "
                    "component that renders the UI."
                ),
            },
            "component_type": {
                "type": "string",
                "enum": ["react", "html", "markdown"],
                "default": "react",
            },
        },
        "required": ["title", "component_code"],
    },
    risk_level=RiskLevel.LOW,
    timeout_seconds=15,
    category="ui",
    availability_requirements=("ui_artifact_manager",),
)

UPDATE_UI_TOOL = ToolDefinition(
    name="update_ui_artifact",
    description="Update an existing UI artifact with new code. Creates a new version.",
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "ID of the artifact to update",
            },
            "component_code": {
                "type": "string",
                "description": "Updated component code",
            },
            "change_description": {
                "type": "string",
                "description": "What changed in this version",
            },
        },
        "required": ["artifact_id", "component_code"],
    },
    risk_level=RiskLevel.LOW,
    timeout_seconds=15,
    category="ui",
    availability_requirements=("ui_artifact_manager",),
)

LIST_UI_TOOL = ToolDefinition(
    name="list_ui_artifacts",
    description="List all UI artifacts in the current workspace.",
    parameters={"type": "object", "properties": {}},
    risk_level=RiskLevel.LOW,
    timeout_seconds=10,
    category="ui",
    availability_requirements=("ui_artifact_manager",),
)


async def handle_create_ui(
    title: str,
    component_code: str,
    description: str = "",
    component_type: str = "react",
    execution_context=None,
    ui_manager=None,
    ui_artifact_manager=None,
    workspace_id: str = "",
    user_id: str = "",
) -> dict:
    manager = ui_manager or ui_artifact_manager
    resolved_workspace_id = workspace_id or getattr(execution_context, "workspace_id", "") or "default"
    resolved_user_id = user_id or getattr(execution_context, "user_id", "") or "agent"
    if not manager:
        return {"error": "UI artifact manager not initialized"}
    try:
        artifact = await manager.create(
            workspace_id=resolved_workspace_id,
            title=title,
            component_code=component_code,
            description=description,
            component_type=component_type,
            created_by=resolved_user_id,
        )
        return {"success": True, "artifact": artifact.to_dict()}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def handle_update_ui(
    artifact_id: str,
    component_code: str,
    change_description: str = "",
    execution_context=None,
    ui_manager=None,
    ui_artifact_manager=None,
) -> dict:
    manager = ui_manager or ui_artifact_manager
    if not manager:
        return {"error": "UI artifact manager not initialized"}
    try:
        artifact = await manager.update(
            artifact_id, component_code, change_description
        )
        if not artifact:
            return {"success": False, "error": "Artifact not found"}
        return {"success": True, "artifact": artifact.to_dict()}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def handle_list_ui(
    execution_context=None,
    ui_manager=None,
    ui_artifact_manager=None,
    workspace_id: str = "",
) -> dict:
    manager = ui_manager or ui_artifact_manager
    resolved_workspace_id = workspace_id or getattr(execution_context, "workspace_id", "") or "default"
    if not manager:
        return {"error": "UI artifact manager not initialized"}
    artifacts = await manager.list(resolved_workspace_id)
    return {"success": True, "artifacts": artifacts, "count": len(artifacts)}


def register_ui_builder_tools(registry) -> None:
    registry.register(definition=CREATE_UI_TOOL, handler=handle_create_ui)
    registry.register(definition=UPDATE_UI_TOOL, handler=handle_update_ui)
    registry.register(definition=LIST_UI_TOOL, handler=handle_list_ui)
    logger.info("UI builder tools registered")
