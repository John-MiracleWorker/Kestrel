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


def test_infer_task_profile_uses_goal_not_mode_gates():
    assert infer_task_profile("set up a cron automation", FeatureMode.CORE) == TaskProfile.OPS


def test_chat_profile_marks_registry_but_does_not_hide_tools():
    registry = _make_registry()
    filtered = filter_registry_for_profile(registry, TaskProfile.CHAT, FeatureMode.CORE)
    names = {tool.name for tool in filtered.list_tools()}

    assert names == {tool.name for tool in registry.list_tools()}
    assert filtered._task_profile == TaskProfile.CHAT.value
    assert filtered._enabled_bundles == ("chat",)


def test_ops_profile_preserves_ops_metadata_in_core_preset():
    registry = _make_registry()
    filtered = filter_registry_for_profile(registry, TaskProfile.OPS, FeatureMode.CORE)
    names = {tool.name for tool in filtered.list_tools()}

    assert "schedule_manage" in names
    assert "self_improve" in names
    assert filtered._enabled_bundles == ("chat", "coding", "ops")


def test_self_repair_profile_keeps_repair_bundle_metadata():
    registry = _make_registry()
    filtered = filter_registry_for_profile(registry, TaskProfile.SELF_REPAIR, FeatureMode.LABS)
    names = {tool.name for tool in filtered.list_tools()}

    assert "self_improve" in names
    assert "host_read" in names
    assert "generate_media" in names
    assert filtered._enabled_bundles == ("chat", "coding", "self_repair")
