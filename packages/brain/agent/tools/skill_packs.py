from __future__ import annotations

from agent.types import RiskLevel, ToolDefinition


SKILL_SEARCH_TOOL = ToolDefinition(
    name="skill_search",
    description="Search bundled, installed, and configured marketplace skill packs by name, tags, description, or SKILL.md contents.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "include_marketplace": {"type": "boolean", "default": True},
        },
        "required": ["query"],
    },
    risk_level=RiskLevel.LOW,
    category="skill",
    availability_requirements=("skill_pack_manager",),
    use_cases=("find a skill pack", "discover reusable capabilities"),
)

SKILL_LIST_TOOL = ToolDefinition(
    name="skill_list",
    description="List local and marketplace skill packs with install, trust, and component state.",
    parameters={
        "type": "object",
        "properties": {
            "include_synthetic": {"type": "boolean", "default": True},
            "include_marketplace": {"type": "boolean", "default": True},
        },
    },
    risk_level=RiskLevel.LOW,
    category="skill",
    availability_requirements=("skill_pack_manager",),
)

SKILL_INSPECT_TOOL = ToolDefinition(
    name="skill_inspect",
    description="Inspect one skill pack in detail.",
    parameters={
        "type": "object",
        "properties": {
            "pack_id": {"type": "string"},
        },
        "required": ["pack_id"],
    },
    risk_level=RiskLevel.LOW,
    category="skill",
    availability_requirements=("skill_pack_manager",),
)

SKILL_INSTALL_TOOL = ToolDefinition(
    name="skill_install",
    description="Install or enable a skill pack from the bundled catalog, a configured marketplace, a local path, or a remote archive URL. Dependencies are resolved automatically.",
    parameters={
        "type": "object",
        "properties": {
            "pack_id": {"type": "string"},
            "source_path": {"type": "string"},
            "source_url": {"type": "string"},
            "scope": {"type": "string", "enum": ["user", "workspace"], "default": "user"},
        },
    },
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    category="skill",
    availability_requirements=("skill_pack_manager",),
)

SKILL_IMPORT_TOOL = ToolDefinition(
    name="skill_import",
    description="Import a local SKILL.md folder or archive as a managed skill pack.",
    parameters={
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "scope": {"type": "string", "enum": ["user", "workspace"], "default": "user"},
        },
        "required": ["source_path"],
    },
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    category="skill",
    availability_requirements=("skill_pack_manager",),
)

SKILL_ENABLE_TOOL = ToolDefinition(
    name="skill_enable",
    description="Enable an installed skill pack.",
    parameters={
        "type": "object",
        "properties": {
            "pack_id": {"type": "string"},
        },
        "required": ["pack_id"],
    },
    risk_level=RiskLevel.MEDIUM,
    category="skill",
    availability_requirements=("skill_pack_manager",),
)

SKILL_DISABLE_TOOL = ToolDefinition(
    name="skill_disable",
    description="Disable an installed skill pack.",
    parameters={
        "type": "object",
        "properties": {
            "pack_id": {"type": "string"},
        },
        "required": ["pack_id"],
    },
    risk_level=RiskLevel.MEDIUM,
    category="skill",
    availability_requirements=("skill_pack_manager",),
)

SKILL_REMOVE_TOOL = ToolDefinition(
    name="skill_remove",
    description="Remove an installed skill pack.",
    parameters={
        "type": "object",
        "properties": {
            "pack_id": {"type": "string"},
        },
        "required": ["pack_id"],
    },
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    category="skill",
    availability_requirements=("skill_pack_manager",),
)


def register_skill_pack_tools(registry) -> None:
    async def skill_search_handler(query: str, include_marketplace: bool = True, skill_pack_manager=None, current_task=None, **_kwargs) -> dict:
        workspace_id = getattr(current_task, "workspace_id", "") if current_task else ""
        return await skill_pack_manager.search(workspace_id, query, include_marketplace=include_marketplace)

    async def skill_list_handler(include_synthetic: bool = True, include_marketplace: bool = True, skill_pack_manager=None, current_task=None, **_kwargs) -> dict:
        workspace_id = getattr(current_task, "workspace_id", "") if current_task else ""
        return await skill_pack_manager.catalog(
            workspace_id,
            include_synthetic=include_synthetic,
            include_marketplace=include_marketplace,
        )

    async def skill_inspect_handler(pack_id: str, skill_pack_manager=None, current_task=None, **_kwargs) -> dict:
        workspace_id = getattr(current_task, "workspace_id", "") if current_task else ""
        pack = await skill_pack_manager.inspect(workspace_id, pack_id)
        return {"success": pack is not None, "pack": pack, "error": "" if pack is not None else f"Unknown skill pack: {pack_id}"}

    async def skill_install_handler(pack_id: str = "", source_path: str = "", source_url: str = "", scope: str = "user", skill_pack_manager=None, current_task=None, **_kwargs) -> dict:
        workspace_id = getattr(current_task, "workspace_id", "") if current_task else ""
        return await skill_pack_manager.install(
            workspace_id=workspace_id,
            pack_id=pack_id,
            source_path=source_path,
            source_url=source_url,
            scope=scope,
        )

    async def skill_import_handler(source_path: str, scope: str = "user", skill_pack_manager=None, current_task=None, **_kwargs) -> dict:
        workspace_id = getattr(current_task, "workspace_id", "") if current_task else ""
        return await skill_pack_manager.import_pack(
            workspace_id=workspace_id,
            source_path=source_path,
            scope=scope,
        )

    async def skill_enable_handler(pack_id: str, skill_pack_manager=None, current_task=None, **_kwargs) -> dict:
        workspace_id = getattr(current_task, "workspace_id", "") if current_task else ""
        return await skill_pack_manager.enable(workspace_id, pack_id)

    async def skill_disable_handler(pack_id: str, skill_pack_manager=None, current_task=None, **_kwargs) -> dict:
        workspace_id = getattr(current_task, "workspace_id", "") if current_task else ""
        return await skill_pack_manager.disable(workspace_id, pack_id)

    async def skill_remove_handler(pack_id: str, skill_pack_manager=None, current_task=None, **_kwargs) -> dict:
        workspace_id = getattr(current_task, "workspace_id", "") if current_task else ""
        return await skill_pack_manager.remove(workspace_id, pack_id)

    registry.register(SKILL_SEARCH_TOOL, skill_search_handler)
    registry.register(SKILL_LIST_TOOL, skill_list_handler)
    registry.register(SKILL_INSPECT_TOOL, skill_inspect_handler)
    registry.register(SKILL_INSTALL_TOOL, skill_install_handler)
    registry.register(SKILL_IMPORT_TOOL, skill_import_handler)
    registry.register(SKILL_ENABLE_TOOL, skill_enable_handler)
    registry.register(SKILL_DISABLE_TOOL, skill_disable_handler)
    registry.register(SKILL_REMOVE_TOOL, skill_remove_handler)
