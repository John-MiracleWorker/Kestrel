from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.effective_settings import (
    SETTING_CATEGORIES,
    SETTING_DESCRIPTORS,
    apply_setting_change,
    project_settings,
)
from nested_memvid_agent.runtime_settings import (
    RuntimeSettingsConflict,
    RuntimeSettingsStore,
)


def _config(tmp_path: Path, **overrides: object) -> AgentConfig:
    base = AgentConfig(
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state" / "agent.db",
        log_dir=tmp_path / "logs",
        workspace=tmp_path / "workspace",
        allow_web=True,
    )
    from dataclasses import replace

    return replace(base, **overrides)


def _store(tmp_path: Path) -> RuntimeSettingsStore:
    return RuntimeSettingsStore(tmp_path / "config" / "runtime_settings.json")


def test_effective_setting_exposes_parent_blocker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    runtime = store.load(config)

    projection = project_settings(
        runtime=runtime,
        capabilities=({"key": "network", "effective_enabled": False},),
    )

    web_search = projection.require("tools.web_search.enabled")
    assert web_search.configured_value is True
    assert web_search.effective_value is False
    assert web_search.blockers == ("capability:network_disabled",)
    assert web_search.applies == "new_runs"


def test_effective_setting_is_unblocked_when_capability_is_enabled(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    runtime = store.load(config)

    projection = project_settings(
        runtime=runtime,
        capabilities=({"key": "network", "effective_enabled": True},),
    )

    web_search = projection.require("tools.web_search.enabled")
    assert web_search.configured_value is True
    assert web_search.effective_value is True
    assert web_search.blockers == ()
    assert web_search.applies == "new_runs"


def test_projection_covers_every_category_exactly_once(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    projection = project_settings(runtime=store.load(config), capabilities=())

    assert SETTING_CATEGORIES == (
        "General",
        "Models and providers",
        "Safety and permissions",
        "Storage and memory",
        "Containment",
        "Appearance",
        "Notifications",
        "Updates",
        "Diagnostics",
        "Advanced",
    )
    ids = [setting.setting_id for setting in projection.settings]
    assert len(ids) == len(set(ids))
    assert {descriptor.id for descriptor in SETTING_DESCRIPTORS} == set(ids)
    assert {setting.category for setting in projection.settings} <= set(SETTING_CATEGORIES)
    assert set(projection.categories) == set(SETTING_CATEGORIES)


def test_every_projected_setting_exposes_truthful_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    runtime = store.load(config)
    projection = project_settings(runtime=runtime, capabilities=())

    assert projection.revision == runtime.revision
    for setting in projection.settings:
        assert setting.revision == runtime.revision
        assert setting.provenance == runtime.sources.get(setting.key, "launch")
        assert isinstance(setting.blockers, tuple)
        assert isinstance(setting.undo_available, bool)
        assert setting.undo_available is True
        assert isinstance(setting.restart_required, bool)
        assert isinstance(setting.requires_approval, bool)
        assert setting.type in {"boolean", "enum", "number", "string", "path"}
        if setting.type == "enum":
            assert setting.allowed_values
            assert setting.configured_value in setting.allowed_values
            assert setting.effective_value in setting.allowed_values
        if setting.key == "require_api_auth":
            assert setting.provenance == "launch"


def test_launch_controlled_settings_are_read_only_and_launch_sourced(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    projection = project_settings(runtime=store.load(config), capabilities=())

    api_auth = projection.require("server.require_api_auth")
    assert api_auth.writable is False
    assert api_auth.restart_required is True
    assert api_auth.provenance == "launch"
    assert api_auth.applies == "restart"
    assert api_auth.configured_value is config.require_api_auth
    assert api_auth.effective_value is config.require_api_auth


def test_setting_revision_follows_store_revision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    first = store.load(config)
    first_projection = project_settings(runtime=first, capabilities=())

    updated = apply_setting_change(
        store,
        config,
        setting_id="model",
        value="mock-v2",
        expected_revision=first.revision,
    )
    second_projection = project_settings(runtime=store.load(config), capabilities=())

    assert updated.settings.revision != first.revision
    assert second_projection.revision == updated.settings.revision
    assert second_projection.require("model").configured_value == "mock-v2"
    assert second_projection.require("model").revision == updated.settings.revision
    assert first_projection.require("model").configured_value == "mock"


def test_apply_setting_change_rejects_stale_revision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    initial = store.load(config)
    apply_setting_change(
        store,
        config,
        setting_id="model",
        value="mock-v2",
        expected_revision=initial.revision,
    )

    with pytest.raises(RuntimeSettingsConflict):
        apply_setting_change(
            store,
            config,
            setting_id="model",
            value="mock-v3",
            expected_revision=initial.revision,
        )

    assert store.load(config).model == "mock-v2"


def test_apply_setting_change_rejects_unknown_and_read_only_settings(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    revision = store.load(config).revision

    with pytest.raises(KeyError):
        apply_setting_change(
            store,
            config,
            setting_id="does.not.exist",
            value=True,
            expected_revision=revision,
        )
    with pytest.raises(ValueError, match="read_only"):
        apply_setting_change(
            store,
            config,
            setting_id="server.require_api_auth",
            value=True,
            expected_revision=revision,
        )
    with pytest.raises(ValueError, match="read_only"):
        apply_setting_change(
            store,
            config,
            setting_id="paths.state_path",
            value=str(tmp_path / "other.db"),
            expected_revision=revision,
        )


def test_apply_setting_change_validates_value_against_descriptor(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(tmp_path)
    revision = store.load(config).revision

    with pytest.raises(ValueError):
        apply_setting_change(
            store,
            config,
            setting_id="model",
            value="   ",
            expected_revision=revision,
        )
    with pytest.raises(ValueError):
        apply_setting_change(
            store,
            config,
            setting_id="models.temperature",
            value=9.5,
            expected_revision=revision,
        )
    with pytest.raises(ValueError):
        apply_setting_change(
            store,
            config,
            setting_id="models.provider",
            value="not-a-provider",
            expected_revision=revision,
        )
    with pytest.raises(ValueError):
        apply_setting_change(
            store,
            config,
            setting_id="tools.web_search.enabled",
            value="definitely-not-a-bool",
            expected_revision=revision,
        )


def test_blocked_setting_still_commits_and_reflects_in_projection(tmp_path: Path) -> None:
    config = _config(tmp_path, allow_web=False)
    store = _store(tmp_path)
    initial = store.load(config)

    updated = apply_setting_change(
        store,
        config,
        setting_id="tools.web_search.enabled",
        value=True,
        expected_revision=initial.revision,
    )

    projection = project_settings(
        runtime=updated.settings,
        capabilities=({"key": "network", "effective_enabled": False},),
    )
    web_search = projection.require("tools.web_search.enabled")
    assert web_search.configured_value is True
    assert web_search.effective_value is False
    assert web_search.blockers == ("capability:network_disabled",)
    # A blocked capability must never report effective success.
    assert web_search.configured_value != web_search.effective_value
