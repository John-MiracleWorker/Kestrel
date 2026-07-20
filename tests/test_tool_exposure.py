from __future__ import annotations

from nested_memvid_agent.runtime_models import ToolSpec
from nested_memvid_agent.tool_exposure import select_relevant_tool_specs


def _spec(
    name: str,
    description: str,
    *,
    aliases: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        aliases=aliases,
        capabilities=capabilities,
    )


def test_native_tool_exposure_is_bounded_relevant_and_keeps_discovery() -> None:
    specs = [
        _spec("tool.registry", "List active tools."),
        _spec("memory.search", "Search nested memory."),
        _spec("file.read", "Read a workspace file."),
        _spec("diagnosis.classify", "Classify a provider failure."),
        _spec("git.status", "Inspect git status."),
    ]

    selected = select_relevant_tool_specs(
        specs,
        objective="Use diagnosis.classify on this provider timeout failure.",
        limit=2,
    )

    assert [spec.name for spec in selected] == ["tool.registry", "diagnosis.classify"]


def test_native_tool_exposure_preserves_canonical_name_for_alias_match() -> None:
    specs = [
        _spec("tool.registry", "List active tools."),
        _spec("memory.search", "Search nested memory.", aliases=("recall",)),
        _spec("file.read", "Read a workspace file."),
    ]

    selected = select_relevant_tool_specs(specs, objective="Please recall Orbit.", limit=2)

    assert [spec.name for spec in selected] == ["tool.registry", "memory.search"]
    assert all(spec.name != "recall" for spec in selected)


def test_native_tool_exposure_never_adds_tools_outside_active_input() -> None:
    active_specs = [
        _spec("tool.registry", "List active tools."),
        _spec("file.read", "Read a workspace file."),
    ]

    selected = select_relevant_tool_specs(
        active_specs,
        objective="Run shell.run and then file.read.",
        limit=2,
    )

    assert [spec.name for spec in selected] == ["tool.registry", "file.read"]


def test_native_tool_exposure_prioritizes_validated_discovery_next_round() -> None:
    specs = [
        _spec("tool.registry", "List active tools."),
        _spec("memory.search", "Search nested memory."),
        _spec("file.read", "Read a workspace file."),
        _spec("git.status", "Inspect git status."),
    ]

    selected = select_relevant_tool_specs(
        specs,
        objective="Discover the exact repository inspection capability.",
        limit=2,
        preferred_names=("git.status",),
    )

    assert [spec.name for spec in selected] == ["tool.registry", "git.status"]


def test_native_tool_exposure_ignores_unknown_alias_and_inactive_preferences() -> None:
    specs = [
        _spec("tool.registry", "List active tools."),
        _spec("memory.search", "Search nested memory.", aliases=("recall",)),
        _spec("file.read", "Read a workspace file."),
    ]

    selected = select_relevant_tool_specs(
        specs,
        objective="Read the workspace file.",
        limit=2,
        preferred_names=("recall", "shell.run", "forged.tool"),
    )

    assert [spec.name for spec in selected] == ["tool.registry", "file.read"]


def test_unbounded_native_tool_exposure_preserves_registry_order() -> None:
    specs = [
        _spec("z.last", "Last."),
        _spec("a.first", "First."),
    ]

    assert select_relevant_tool_specs(specs, objective="anything", limit=None) == specs
