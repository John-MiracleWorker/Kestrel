from __future__ import annotations
"""
UI Builder tools — let the agent create and iterate on persistent
interactive UI components during chat.
"""

import logging
from agent.types import ToolDefinition, RiskLevel

logger = logging.getLogger("brain.agent.tools.ui_builder")

_ui_manager = None
_current_workspace_id = None


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
)

LIST_UI_TOOL = ToolDefinition(
    name="list_ui_artifacts",
    description="List all UI artifacts in the current workspace.",
    parameters={"type": "object", "properties": {}},
    risk_level=RiskLevel.LOW,
    timeout_seconds=10,
    category="ui",
)


async def handle_create_ui(
    title: str,
    component_code: str,
    description: str = "",
    component_type: str = "react",
) -> dict:
    if not _ui_manager:
        return {"error": "UI artifact manager not initialized"}
    try:
        artifact = await _ui_manager.create(
            workspace_id=_current_workspace_id or "default",
            title=title,
            component_code=component_code,
            description=description,
            component_type=component_type,
        )
        return {"success": True, "artifact": artifact.to_dict()}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def handle_update_ui(
    artifact_id: str,
    component_code: str,
    change_description: str = "",
) -> dict:
    if not _ui_manager:
        return {"error": "UI artifact manager not initialized"}
    try:
        artifact = await _ui_manager.update(
            artifact_id, component_code, change_description
        )
        if not artifact:
            return {"success": False, "error": "Artifact not found"}
        return {"success": True, "artifact": artifact.to_dict()}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def handle_list_ui() -> dict:
    if not _ui_manager:
        return {"error": "UI artifact manager not initialized"}
    artifacts = await _ui_manager.list(_current_workspace_id or "default")
    return {"success": True, "artifacts": artifacts, "count": len(artifacts)}


def register_ui_builder_tools(registry) -> None:
    registry.register(definition=CREATE_UI_TOOL, handler=handle_create_ui)
    registry.register(definition=UPDATE_UI_TOOL, handler=handle_update_ui)
    registry.register(definition=LIST_UI_TOOL, handler=handle_list_ui)
    logger.info("UI builder tools registered")
