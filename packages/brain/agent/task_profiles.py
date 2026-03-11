"""Task-profile and tool-bundle policy helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from core.feature_mode import FeatureMode, enabled_bundles_for_mode, mode_supports_labs, mode_supports_ops

if TYPE_CHECKING:
    from agent.tools import ToolRegistry
    from agent.types import ToolDefinition


class TaskProfile(str, Enum):
    CHAT = "chat"
    RESEARCH = "research"
    CODING = "coding"
    OPS = "ops"
    MEDIA = "media"
    SELF_REPAIR = "self_repair"


@dataclass(frozen=True)
class BundleSpec:
    categories: tuple[str, ...] = ()
    include_names: tuple[str, ...] = ()
    exclude_names: tuple[str, ...] = ()


_CONTROL_TOOLS = ("ask_human", "task_complete")
_HOST_CONTROL_EXCLUDES = ("host_shell", "host_python")
_LABS_ONLY_NAMES = (
    "computer_use",
    "generate_media",
    "vram_generate_image",
    "check_media_host",
    "build_automation",
    "daemon_create",
    "daemon_list",
    "daemon_stop",
    "time_travel_list",
    "time_travel_fork",
    "time_travel_compare",
    "create_ui",
    "update_ui",
    "list_ui",
    "delegate",
    "delegate_parallel",
    "create_specialist",
    "list_specialists",
    "remove_specialist",
    "self_improve",
    "git_ops",
)
_OPS_ONLY_NAMES = (
    "telegram_notify",
    "mcp_call",
    "mcp_list_servers",
    "mcp_connect_server",
    "mcp_disconnect_server",
    "mcp_install_tool",
    "mcp_uninstall_tool",
    "mcp_enable_tool",
    "mcp_disable_tool",
    "schedule_manage",
    "daemon_create",
    "daemon_list",
    "daemon_stop",
    "moltbook",
    "moltbook_autonomous",
    "model_swap",
    "container_control",
    "process_kill",
    "sessions_list",
    "sessions_send",
    "sessions_history",
    "sessions_inbox",
)

_BUNDLE_SPECS: dict[str, BundleSpec] = {
    "chat": BundleSpec(
        categories=("control", "memory"),
        include_names=("system_health", "process_list"),
        exclude_names=_HOST_CONTROL_EXCLUDES + _LABS_ONLY_NAMES + _OPS_ONLY_NAMES,
    ),
    "research": BundleSpec(
        categories=("web", "data", "memory"),
        include_names=("file_read", "file_list"),
        exclude_names=("file_write",) + _LABS_ONLY_NAMES + _OPS_ONLY_NAMES,
    ),
    "coding": BundleSpec(
        categories=("code", "file", "analysis", "memory"),
        include_names=("system_health", "process_list"),
        exclude_names=_LABS_ONLY_NAMES + _OPS_ONLY_NAMES,
    ),
    "ops": BundleSpec(
        categories=("automation", "mcp", "sessions", "social", "infrastructure"),
        include_names=("telegram_notify", "model_swap", "system_health", "process_list"),
        exclude_names=_LABS_ONLY_NAMES,
    ),
    "media": BundleSpec(
        categories=("media", "computer_use", "ui"),
        exclude_names=(),
    ),
    "self_repair": BundleSpec(
        categories=("development", "analysis", "host_file", "skill"),
        include_names=("system_health", "process_list"),
        exclude_names=(),
    ),
}

_PROFILE_KEYWORDS: tuple[tuple[TaskProfile, tuple[str, ...]], ...] = (
    (TaskProfile.SELF_REPAIR, ("fix kestrel", "fix the repo", "self repair", "self-repair", "repair the agent", "fix this codebase")),
    (TaskProfile.MEDIA, ("image", "video", "screenshot", "computer use", "browser automation")),
    (TaskProfile.OPS, ("deploy", "cron", "automation", "notify", "telegram", "mcp", "integration", "daemon", "ops")),
    (TaskProfile.CODING, ("code", "refactor", "test", "bug", "debug", "patch", "implement", "repository", "repo", "file", "python", "typescript", "javascript")),
    (TaskProfile.RESEARCH, ("research", "search", "find out", "browse", "web", "investigate", "look up", "summarize")),
)


def infer_task_profile(goal: str, feature_mode: FeatureMode) -> TaskProfile:
    text = (goal or "").strip().lower()
    for profile, keywords in _PROFILE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return _clamp_profile(profile, feature_mode)
    return TaskProfile.CHAT


def _clamp_profile(profile: TaskProfile, feature_mode: FeatureMode) -> TaskProfile:
    if profile == TaskProfile.MEDIA and not mode_supports_labs(feature_mode):
        return TaskProfile.RESEARCH
    if profile == TaskProfile.SELF_REPAIR and not mode_supports_labs(feature_mode):
        return TaskProfile.CODING
    if profile == TaskProfile.OPS and not mode_supports_ops(feature_mode):
        return TaskProfile.CODING
    return profile


def bundles_for_profile(profile: TaskProfile, feature_mode: FeatureMode) -> tuple[str, ...]:
    clamped = _clamp_profile(profile, feature_mode)
    if clamped == TaskProfile.RESEARCH:
        bundles = ("chat", "research")
    elif clamped == TaskProfile.CODING:
        bundles = ("chat", "coding")
    elif clamped == TaskProfile.OPS:
        bundles = ("chat", "coding", "ops")
    elif clamped == TaskProfile.MEDIA:
        bundles = ("chat", "media")
    elif clamped == TaskProfile.SELF_REPAIR:
        bundles = ("chat", "coding", "self_repair")
    else:
        bundles = ("chat",)
    mode_bundles = set(enabled_bundles_for_mode(feature_mode))
    return tuple(bundle for bundle in bundles if bundle in mode_bundles)


def allowed_tool_names_for_bundles(
    registry: "ToolRegistry",
    bundle_names: tuple[str, ...] | list[str],
) -> list[str]:
    allowed: list[str] = []
    seen: set[str] = set()
    for tool in registry.list_tools():
        if _tool_allowed_in_bundles(tool, bundle_names):
            allowed.append(tool.name)
            seen.add(tool.name)
    for tool_name in _CONTROL_TOOLS:
        if tool_name in registry._definitions and tool_name not in seen:
            allowed.append(tool_name)
    return allowed


def allowed_tool_names_for_profile(
    registry: "ToolRegistry",
    profile: TaskProfile,
    feature_mode: FeatureMode,
) -> list[str]:
    return allowed_tool_names_for_bundles(registry, bundles_for_profile(profile, feature_mode))


def filter_registry_for_profile(
    registry: "ToolRegistry",
    profile: TaskProfile,
    feature_mode: FeatureMode,
) -> "ToolRegistry":
    filtered = registry.filter(allowed_tool_names_for_profile(registry, profile, feature_mode))
    filtered._task_profile = profile.value
    filtered._enabled_bundles = bundles_for_profile(profile, feature_mode)
    filtered._feature_mode = feature_mode.value
    return filtered


def _tool_allowed_in_bundles(tool: "ToolDefinition", bundle_names: tuple[str, ...] | list[str]) -> bool:
    for bundle_name in bundle_names:
        spec = _BUNDLE_SPECS.get(bundle_name)
        if not spec:
            continue
        if tool.name in spec.exclude_names:
            continue
        if tool.name in spec.include_names:
            return True
        if tool.category in spec.categories:
            return True
    return False
