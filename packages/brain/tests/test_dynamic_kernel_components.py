from __future__ import annotations

from agent.kernel_policy import KernelPolicyService
from agent.tool_selector import ToolSelector
from agent.tools import ToolRegistry
from agent.tools.catalog import SEARCH_TOOL_CATALOG_TOOL, register_catalog_tools
from agent.tools.create_skill import register_create_skill_tools
from agent.tools.media_gen import register_media_gen_tools
from agent.tools.sessions import register_sessions_tools
from agent.types import AgentTask, GuardrailConfig, RiskLevel, ToolDefinition


async def _noop(**kwargs):
    return kwargs


def _tool(name: str, category: str, *, availability_requirements: tuple[str, ...] = ()) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        risk_level=RiskLevel.LOW,
        category=category,
        availability_requirements=availability_requirements,
    )


def test_register_helpers_expose_dynamic_tools():
    registry = ToolRegistry()
    register_catalog_tools(registry)
    register_create_skill_tools(registry)
    register_media_gen_tools(registry)
    register_sessions_tools(registry)

    names = {tool.name for tool in registry.list_tools()}
    assert SEARCH_TOOL_CATALOG_TOOL.name in names
    assert "create_skill" in names
    assert "generate_media" in names
    assert "check_media_host" in names
    assert "vram_generate_image" in names
    assert "sessions_list" in names
    assert "sessions_send" in names


def test_selector_prefers_media_for_dalle_requests():
    registry = ToolRegistry()
    registry.register(_tool("ask_human", "control"), _noop)
    registry.register(_tool("task_complete", "control"), _noop)
    registry.register(_tool("search_tool_catalog", "control"), _noop)
    registry.register(_tool("web_search", "web"), _noop)
    registry.register(_tool("generate_media", "media", availability_requirements=("media",)), _noop)

    selector = ToolSelector(registry)
    selected = selector.select("make a dalle-style image of a neon owl", provider="google")
    names = [tool.name for tool in selected]

    assert "generate_media" in names
    assert names.index("generate_media") < names.index("web_search")


def test_selector_deprioritizes_unavailable_media():
    registry = ToolRegistry()
    registry.register(_tool("ask_human", "control"), _noop)
    registry.register(_tool("task_complete", "control"), _noop)
    registry.register(_tool("search_tool_catalog", "control"), _noop)
    registry.register(_tool("web_search", "web"), _noop)
    registry.register(
        ToolDefinition(
            name="generate_media",
            description="generate_media",
            parameters={"type": "object", "properties": {}},
            risk_level=RiskLevel.LOW,
            category="media",
            availability_requirements=("media",),
            lifecycle_state="approved",
        ),
        _noop,
    )

    class _UnavailableBootstrapper:
        def status(self, name: str) -> str:
            return "unavailable" if name == "media" else "ready"

        async def ensure(self, name: str):
            return None

        def snapshot(self) -> dict[str, str]:
            return {"media": "unavailable"}

    import core.runtime as runtime_module

    original = getattr(runtime_module, "subsystem_bootstrapper", None)
    runtime_module.subsystem_bootstrapper = _UnavailableBootstrapper()
    try:
        selector = ToolSelector(registry)
        selected = selector.select("make a dalle-style image of a neon owl", provider="google")
        names = [tool.name for tool in selected]
        assert "web_search" in names
        assert "generate_media" in names
        assert names.index("web_search") < names.index("generate_media")
    finally:
        runtime_module.subsystem_bootstrapper = original


def test_kernel_policy_uses_soft_preset_and_subsystem_health():
    policy = KernelPolicyService().evaluate(
        task=AgentTask(
            user_id="u",
            workspace_id="w",
            goal="Create a new tool to monitor Telegram media generation failures",
            config=GuardrailConfig(),
        ),
        execution_context=type("Ctx", (), {"kernel_preset": "labs"})(),
        subsystem_health={"simulation": "ready"},
        persona_context="Prefers concise technical answers.",
    )

    assert policy.preset == "labs"
    assert policy.use_reflection is True
    assert policy.use_simulation is True
    assert "capability_gap" in policy.active_nodes
