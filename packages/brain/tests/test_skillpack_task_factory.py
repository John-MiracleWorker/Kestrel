from __future__ import annotations

import asyncio
from types import SimpleNamespace

import core.runtime as runtime_module
from agent.tools import ToolRegistry
from agent.types import RiskLevel, ToolDefinition
from services.request_context import build_request_context
from services.task_factory import create_chat_task


class _Pool:
    async def fetchrow(self, *_args, **_kwargs):
        return None


class _WorkspaceAgentStore:
    async def ensure_profile(self, _workspace_id: str):
        return SimpleNamespace(
            id="agent-profile-1",
            tool_policy_bundle=(),
            autonomy_policy="balanced",
            kernel_preset="core",
        )


class _SkillPackManager:
    async def register_selected_tools(self, registry, workspace_id: str, selected_packs: list[dict]):
        async def _handler(**_kwargs):
            return {"success": True, "output": workspace_id}

        registry.register(
            ToolDefinition(
                name="pack_echo",
                description="Pack echo",
                parameters={"type": "object", "properties": {}},
                risk_level=RiskLevel.LOW,
                category="skill",
                source="skill",
            ),
            _handler,
        )
        return 1


def test_create_chat_task_registers_selected_skill_pack_tools(monkeypatch):
    registry = ToolRegistry()
    original_skill_pack_manager = getattr(runtime_module, "skill_pack_manager", None)
    original_workspace_agent_store = getattr(runtime_module, "workspace_agent_store", None)
    original_hands_client = getattr(runtime_module, "hands_client", None)
    original_vector_store = getattr(runtime_module, "vector_store", None)
    original_execution_runtime = getattr(runtime_module, "execution_runtime", None)
    original_enabled_tool_bundles = getattr(runtime_module, "enabled_tool_bundles", None)
    original_feature_mode = getattr(runtime_module, "feature_mode", None)
    try:
        runtime_module.skill_pack_manager = _SkillPackManager()
        runtime_module.workspace_agent_store = _WorkspaceAgentStore()
        runtime_module.hands_client = None
        runtime_module.vector_store = None
        runtime_module.execution_runtime = None
        runtime_module.enabled_tool_bundles = []
        runtime_module.feature_mode = "core"

        monkeypatch.setattr("services.task_factory.build_tool_registry", lambda **_kwargs: registry)

        request = SimpleNamespace(
            user_id="user-1",
            conversation_id="conversation-1",
        )
        ctx = SimpleNamespace(
            pool=_Pool(),
            user_content="use the selected pack tool",
            messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "use the selected pack tool"}],
            channel_name="web",
            request_metadata={},
            return_route={},
            selected_skill_packs=[{"pack_id": "demo-pack", "name": "Demo Pack"}],
        )

        task = asyncio.run(create_chat_task(request, ctx, "workspace-1"))
    finally:
        runtime_module.skill_pack_manager = original_skill_pack_manager
        runtime_module.workspace_agent_store = original_workspace_agent_store
        runtime_module.hands_client = original_hands_client
        runtime_module.vector_store = original_vector_store
        runtime_module.execution_runtime = original_execution_runtime
        runtime_module.enabled_tool_bundles = original_enabled_tool_bundles
        runtime_module.feature_mode = original_feature_mode

    tool_names = {tool.name for tool in task._tool_registry.list_tools()}
    assert "pack_echo" in tool_names
    assert task._selected_skill_packs[0]["pack_id"] == "demo-pack"


def test_build_request_context_auto_connects_selected_skill_pack_mcp(monkeypatch):
    original_skill_pack_manager = getattr(runtime_module, "skill_pack_manager", None)
    try:
        class _ProviderConfig:
            def __init__(self, _pool):
                self._pool = _pool

            async def get_config(self, _workspace_id: str):
                return {"provider": "lmstudio", "model": "demo-model", "api_key": "", "settings": {}}

        class _RequestSkillPackManager:
            async def select_packs(self, workspace_id: str, user_content: str, history=None):
                return {
                    "snapshot_id": "snapshot-1",
                    "packs": [{"pack_id": "filesystem-pack", "name": "Filesystem Pack"}],
                    "prompt_block": "## Active Skill Packs\nUse the filesystem workflow.",
                }

            async def auto_connect_selected_mcp(self, workspace_id: str, selected_packs: list[dict]):
                return [
                    {
                        "pack_id": "filesystem-pack",
                        "server_name": "filesystem",
                        "connected": True,
                        "tools": [{"name": "read_file"}],
                    }
                ]

        async def _fake_build_chat_context(*_args, **_kwargs):
            return [
                {"role": "system", "content": "System base prompt"},
                {"role": "user", "content": "Inspect this repo with the filesystem pack"},
            ]

        async def _get_pool():
            return object()

        async def _get_redis():
            return SimpleNamespace(get=lambda _key: None)

        runtime_module.skill_pack_manager = _RequestSkillPackManager()
        monkeypatch.setattr("services.request_context.ProviderConfig", _ProviderConfig)
        monkeypatch.setattr("services.request_context.get_provider", lambda _name: object())
        monkeypatch.setattr("services.request_context.get_pool", _get_pool)
        monkeypatch.setattr("services.request_context.get_redis", _get_redis)
        monkeypatch.setattr("services.context_builder.build_chat_context", _fake_build_chat_context)

        request = SimpleNamespace(parameters={}, provider="", model="")
        ctx = asyncio.run(build_request_context(request, "workspace-1"))
    finally:
        runtime_module.skill_pack_manager = original_skill_pack_manager

    assert ctx.selected_skill_packs[0]["pack_id"] == "filesystem-pack"
    assert ctx.selected_skill_mcp_servers[0]["server_name"] == "filesystem"
    assert "Skill Pack MCP Servers" in ctx.messages[0]["content"]
    assert "`filesystem` from `filesystem-pack`" in ctx.messages[0]["content"]
