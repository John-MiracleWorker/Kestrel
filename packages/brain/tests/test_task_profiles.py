from agent.task_profiles import TaskProfile, filter_registry_for_profile, infer_task_profile
from agent.tools import ToolRegistry
from agent.types import RiskLevel, ToolDefinition
from core.feature_mode import FeatureMode


async def _noop(**kwargs):
    return kwargs


def _register(registry: ToolRegistry, name: str, category: str) -> None:
    registry.register(
        ToolDefinition(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            risk_level=RiskLevel.LOW,
            category=category,
        ),
        _noop,
    )


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    _register(registry, "ask_human", "control")
    _register(registry, "task_complete", "control")
    _register(registry, "memory_search", "memory")
    _register(registry, "web_search", "web")
    _register(registry, "file_read", "file")
    _register(registry, "file_write", "file")
    _register(registry, "code_execute", "code")
    _register(registry, "schedule_manage", "automation")
    _register(registry, "system_health", "general")
    _register(registry, "self_improve", "development")
    _register(registry, "host_read", "host_file")
    _register(registry, "generate_media", "media")
    return registry


def test_infer_task_profile_clamps_ops_in_core_mode():
    assert infer_task_profile("set up a cron automation", FeatureMode.CORE) == TaskProfile.CODING


def test_core_chat_profile_uses_small_safe_bundle():
    registry = _make_registry()
    filtered = filter_registry_for_profile(registry, TaskProfile.CHAT, FeatureMode.CORE)
    names = {tool.name for tool in filtered.list_tools()}

    assert names == {"ask_human", "task_complete", "memory_search", "system_health"}


def test_core_ops_profile_never_leaks_automation_tools():
    registry = _make_registry()
    filtered = filter_registry_for_profile(registry, TaskProfile.OPS, FeatureMode.CORE)
    names = {tool.name for tool in filtered.list_tools()}

    assert "code_execute" in names
    assert "schedule_manage" not in names
    assert "self_improve" not in names


def test_labs_self_repair_profile_allows_repair_tools():
    registry = _make_registry()
    filtered = filter_registry_for_profile(registry, TaskProfile.SELF_REPAIR, FeatureMode.LABS)
    names = {tool.name for tool in filtered.list_tools()}

    assert "self_improve" in names
    assert "host_read" in names
    assert "generate_media" not in names
