from app import initializer_plan_for_mode
from core.feature_mode import FeatureMode, enabled_bundles_for_mode, mode_supports_labs, mode_supports_ops


def test_enabled_bundles_by_mode():
    assert enabled_bundles_for_mode(FeatureMode.CORE) == ("chat", "research", "coding")
    assert enabled_bundles_for_mode(FeatureMode.OPS) == ("chat", "research", "coding", "ops")
    assert enabled_bundles_for_mode(FeatureMode.LABS) == (
        "chat",
        "research",
        "coding",
        "ops",
        "media",
        "self_repair",
    )


def test_mode_support_helpers():
    assert not mode_supports_ops(FeatureMode.CORE)
    assert not mode_supports_labs(FeatureMode.CORE)
    assert mode_supports_ops(FeatureMode.OPS)
    assert not mode_supports_labs(FeatureMode.OPS)
    assert mode_supports_ops(FeatureMode.LABS)
    assert mode_supports_labs(FeatureMode.LABS)


def test_initializer_plan_for_core_skips_ops_and_labs():
    assert initializer_plan_for_mode(FeatureMode.CORE) == (
        "init_db",
        "init_memory",
        "init_hands_client",
        "init_agent_core",
        "init_provider_discovery",
        "init_mcp_auto_connect",
    )


def test_initializer_plan_for_labs_includes_all_layers():
    assert initializer_plan_for_mode(FeatureMode.LABS) == (
        "init_db",
        "init_memory",
        "init_hands_client",
        "init_agent_core",
        "init_provider_discovery",
        "init_ops",
        "init_labs",
        "init_mcp_auto_connect",
    )
