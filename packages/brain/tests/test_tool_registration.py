"""Tests that tool registration respects feature mode boundaries."""

from unittest.mock import patch, MagicMock

from agent.tools import build_tool_registry, _register_core_tools, _register_ops_tools, _register_labs_tools


def _noop_register(registry, **kwargs):
    """Stub that does nothing — prevents real tool module imports."""
    pass


# Paths to all register_*_tools functions called inside each tier
_CORE_PATCHES = [
    "agent.tools.code.register_code_tools",
    "agent.tools.web.register_web_tools",
    "agent.tools.files.register_file_tools",
    "agent.tools.host_files.register_host_file_tools",
    "agent.tools.data.register_data_tools",
    "agent.tools.memory.register_memory_tools",
    "agent.tools.human.register_human_tools",
    "agent.tools.system_tools.register_system_tools",
    "agent.tools.host_execution.register_host_execution_tools",
]

_OPS_PATCHES = [
    "agent.tools.moltbook.register_moltbook_tools",
    "agent.tools.moltbook_autonomous.register_moltbook_autonomous_tools",
    "agent.tools.schedule.register_schedule_tools",
    "agent.tools.model_swap.register_model_swap_tools",
    "agent.tools.telegram_notify.register_telegram_tools",
    "agent.tools.mcp.register_mcp_tools",
    "agent.tools.container_control.register_container_tools",
]

_LABS_PATCHES = [
    "agent.tools.git.register_git_tools",
    "agent.tools.self_improve.register_self_improve_tools",
    "agent.tools.scanner.register_scanner_tools",
    "agent.tools.computer_use.register_computer_use_tools",
    "agent.tools.media_gen.register_media_gen_tools",
    "agent.tools.build_automation.register_build_automation_tools",
    "agent.tools.daemon_control.register_daemon_tools",
    "agent.tools.time_travel.register_time_travel_tools",
    "agent.tools.ui_builder.register_ui_builder_tools",
    "agent.tools.delegate.DELEGATE_TOOL",
    "agent.tools.delegate.DELEGATE_PARALLEL_TOOL",
    "agent.tools.delegate.CREATE_SPECIALIST_TOOL",
    "agent.tools.delegate.LIST_SPECIALISTS_TOOL",
    "agent.tools.delegate.REMOVE_SPECIALIST_TOOL",
]


def _build_patchers():
    """Create mock patches for all register functions so we can track calls."""
    patchers = {}
    for path in _CORE_PATCHES + _OPS_PATCHES:
        p = patch(path, side_effect=_noop_register)
        patchers[path] = p
    # For labs, also patch the delegate tool constants
    for path in _LABS_PATCHES:
        if "register_" in path:
            p = patch(path, side_effect=_noop_register)
        else:
            p = patch(path, MagicMock())
        patchers[path] = p
    return patchers


def test_core_mode_skips_ops_and_labs_imports():
    """In CORE mode, only core registration functions are called."""
    with patch("agent.tools._register_core_tools") as core_mock, \
         patch("agent.tools._register_ops_tools") as ops_mock, \
         patch("agent.tools._register_labs_tools") as labs_mock, \
         patch("agent.tools.set_active_runtime"):
        build_tool_registry(feature_mode="core")
        core_mock.assert_called_once()
        ops_mock.assert_not_called()
        labs_mock.assert_not_called()


def test_ops_mode_registers_core_and_ops():
    """In OPS mode, core and ops are registered but not labs."""
    with patch("agent.tools._register_core_tools") as core_mock, \
         patch("agent.tools._register_ops_tools") as ops_mock, \
         patch("agent.tools._register_labs_tools") as labs_mock, \
         patch("agent.tools.set_active_runtime"):
        build_tool_registry(feature_mode="ops")
        core_mock.assert_called_once()
        ops_mock.assert_called_once()
        labs_mock.assert_not_called()


def test_labs_mode_registers_all_tiers():
    """In LABS mode, all three tiers are registered."""
    with patch("agent.tools._register_core_tools") as core_mock, \
         patch("agent.tools._register_ops_tools") as ops_mock, \
         patch("agent.tools._register_labs_tools") as labs_mock, \
         patch("agent.tools.set_active_runtime"):
        build_tool_registry(feature_mode="labs")
        core_mock.assert_called_once()
        ops_mock.assert_called_once()
        labs_mock.assert_called_once()


def test_bundle_filtering_preserved_after_mode_registration():
    """Bundle filtering still works on top of mode-based registration."""
    with patch("agent.tools._register_core_tools") as core_mock, \
         patch("agent.tools.set_active_runtime"):
        # Even with bundles, the core registration should still be called
        registry = build_tool_registry(
            feature_mode="core",
            enabled_bundles=("chat",),
        )
        core_mock.assert_called_once()
        assert registry._enabled_bundles == ("chat",)
        assert registry._feature_mode == "core"
