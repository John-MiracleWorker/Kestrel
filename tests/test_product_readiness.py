from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.factory import provider_health_id
from nested_memvid_agent.llm.model_catalog import PROVIDER_OPTIONS
from nested_memvid_agent.llm.resilience import global_provider_health_registry
from nested_memvid_agent.product_readiness import (
    ProductReadinessStatus,
    build_product_readiness_report,
)
from nested_memvid_agent.provider_certification import (
    PROVIDER_CERTIFICATION_POLICY_VERSION,
    ProviderCertificationState,
    ProviderCertificationStatus,
    build_provider_certification_report,
)
from nested_memvid_agent.server_product_routes import register_product_routes
from nested_memvid_agent.setup_readiness import SetupReadinessStatus, build_setup_readiness_report


def test_product_readiness_report_exposes_all_productization_categories() -> None:
    report = build_product_readiness_report()

    assert report.schema == "kestrel.product_readiness.v2"
    assert report.scope == "full_product_including_hosted_team"
    assert report.headline.total_categories == 11
    assert report.headline.ready_count >= 0
    assert report.headline.partial_count > 0
    assert report.headline.missing_count > 0
    assert report.headline.product_ready is False

    category_ids = {category.category_id for category in report.categories}
    assert category_ids == {
        "local_product_stability",
        "golden_repair_workflow",
        "proactive_personal_routines",
        "safe_autonomous_learning",
        "production_auth_workspaces",
        "sandboxed_extensibility",
        "provider_certification",
        "product_ux_onboarding",
        "operations_release_engineering",
        "channels_ingress",
        "metrics_proof",
    }


def test_product_readiness_category_payloads_include_evidence_and_next_actions() -> None:
    report = build_product_readiness_report()

    auth = report.category("production_auth_workspaces")
    assert auth.status == ProductReadinessStatus.MISSING
    assert auth.evidence
    assert auth.remaining_work
    assert auth.next_action

    learning = report.category("safe_autonomous_learning")
    assert learning.status == ProductReadinessStatus.READY
    assert any("behavior" in item.lower() for item in learning.evidence)
    assert any("auto-activation" in item.lower() for item in learning.evidence)
    assert not any("auto-activate" in item.lower() for item in learning.remaining_work)

    repair = report.category("golden_repair_workflow")
    assert repair.status == ProductReadinessStatus.PARTIAL
    assert any("default" in item.lower() and "worktree" in item.lower() for item in repair.evidence)
    assert any("coherent" in item.lower() and "worktree" in item.lower() for item in repair.evidence)
    assert not any("default" in item.lower() and "worktree" in item.lower() for item in repair.remaining_work)
    assert not any("coherent" in item.lower() and "worktree" in item.lower() for item in repair.remaining_work)

    routines = report.category("proactive_personal_routines")
    assert routines.status == ProductReadinessStatus.PARTIAL
    assert any("fenced" in item.lower() for item in routines.evidence)
    assert any("delivery" in item.lower() for item in routines.remaining_work)

    operations = report.category("operations_release_engineering")
    assert any("support bundle" in item.lower() for item in operations.evidence)
    assert any("containment" in item.lower() for item in operations.evidence)
    assert not any("support bundle" in item.lower() for item in operations.remaining_work)

    onboarding = report.category("product_ux_onboarding")
    assert any("first-run setup" in item.lower() for item in onboarding.evidence)
    assert not any(item.lower().startswith("add first-run onboarding") for item in onboarding.remaining_work)


def test_product_readiness_report_serializes_to_public_dict() -> None:
    report = build_product_readiness_report()
    payload = report.to_dict()

    assert payload["schema"] == "kestrel.product_readiness.v2"
    assert payload["scope"] == "full_product_including_hosted_team"
    assert payload["headline"]["product_ready"] is False
    assert payload["categories"][0]["status"] in {"ready", "partial", "missing"}
    assert all(category["next_action"] for category in payload["categories"])


def test_setup_readiness_reports_first_run_prerequisites(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    report = build_setup_readiness_report(
        AgentConfig(
            provider="mock",
            model="mock",
            workspace=tmp_path,
            memory_dir=memory_dir,
            state_path=state_dir / "agent.db",
            log_dir=logs_dir,
            enable_worker_isolation=True,
            require_api_auth=False,
        )
    )

    assert report.schema == "kestrel.setup_readiness.v1"
    assert report.fail_count == 0
    assert report.ready is True
    checks = {check.check_id: check for check in report.checks}
    assert checks["provider_configuration"].status == SetupReadinessStatus.PASS
    assert checks["memory_storage"].status == SetupReadinessStatus.PASS
    assert checks["api_auth"].status == SetupReadinessStatus.WARN
    assert checks["proactive_routines"].status == SetupReadinessStatus.PASS
    assert checks["validation_container"].status == SetupReadinessStatus.PASS


def test_setup_readiness_requires_pinned_oci_image_for_arbitrary_code_tools(
    tmp_path: Path,
) -> None:
    missing = build_setup_readiness_report(
        AgentConfig(workspace=tmp_path, allow_shell=True, allow_codex_cli=True)
    )
    missing_check = {check.check_id: check for check in missing.checks}[
        "validation_container"
    ]
    assert missing.ready is False
    assert missing_check.status == SetupReadinessStatus.FAIL
    assert "test.run" in missing_check.detail
    assert "codex.exec" in missing_check.detail

    mutable = build_setup_readiness_report(
        AgentConfig(
            workspace=tmp_path,
            allow_shell=True,
            validation_container_image="example.invalid/kestrel-validation:latest",
        )
    )
    mutable_check = {check.check_id: check for check in mutable.checks}[
        "validation_container"
    ]
    assert mutable_check.status == SetupReadinessStatus.FAIL

    pinned = build_setup_readiness_report(
        AgentConfig(
            workspace=tmp_path,
            allow_shell=True,
            validation_container_image=(
                "example.invalid/kestrel-validation@sha256:" + "a" * 64
            ),
        )
    )
    pinned_check = {check.check_id: check for check in pinned.checks}[
        "validation_container"
    ]
    assert pinned_check.status == SetupReadinessStatus.PASS


def test_setup_readiness_warns_when_proactive_api_owner_gate_is_open(
    tmp_path: Path,
) -> None:
    open_report = build_setup_readiness_report(
        AgentConfig(
            workspace=tmp_path,
            memory_dir=tmp_path / "memory-open",
            state_path=tmp_path / "state-open" / "agent.db",
            log_dir=tmp_path / "logs-open",
            enable_proactive_routines=True,
            require_api_auth=False,
        )
    )
    gated_report = build_setup_readiness_report(
        AgentConfig(
            workspace=tmp_path,
            memory_dir=tmp_path / "memory-gated",
            state_path=tmp_path / "state-gated" / "agent.db",
            log_dir=tmp_path / "logs-gated",
            enable_proactive_routines=True,
            require_api_auth=True,
        )
    )

    open_checks = {check.check_id: check for check in open_report.checks}
    gated_checks = {check.check_id: check for check in gated_report.checks}
    assert open_checks["proactive_routines"].status == SetupReadinessStatus.WARN
    assert gated_checks["proactive_routines"].status == SetupReadinessStatus.PASS


def test_setup_readiness_flags_missing_workspace_and_provider_secret(tmp_path: Path) -> None:
    report = build_setup_readiness_report(
        AgentConfig(
            provider="openai",
            model="gpt-4.1-mini",
            api_key_env="MISSING_KES_TEST_TOKEN",
            workspace=tmp_path / "missing-workspace",
            memory_dir=tmp_path / "missing-memory",
            state_path=tmp_path / "missing-state" / "agent.db",
            log_dir=tmp_path / "missing-logs",
        )
    )

    assert report.ready is False
    checks = {check.check_id: check for check in report.checks}
    assert checks["provider_configuration"].status == SetupReadinessStatus.FAIL
    assert checks["workspace"].status == SetupReadinessStatus.FAIL
    assert checks["memory_storage"].status == SetupReadinessStatus.WARN
    assert "MISSING_KES_TEST_TOKEN" in checks["provider_configuration"].detail


def test_setup_readiness_accepts_broker_resolved_provider_secret(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    report = build_setup_readiness_report(
        AgentConfig(
            provider="ollama-cloud",
            model="gpt-oss:120b",
            api_key_env="OLLAMA_API_KEY",
            workspace=tmp_path,
            memory_dir=memory_dir,
            state_path=state_dir / "agent.db",
            log_dir=logs_dir,
        ),
        secret_resolver=lambda name: "raw-broker-secret" if name == "OLLAMA_API_KEY" else None,
    )

    checks = {check.check_id: check for check in report.checks}
    assert checks["provider_configuration"].status == SetupReadinessStatus.PASS
    assert "OLLAMA_API_KEY" in checks["provider_configuration"].detail
    assert "raw-broker-secret" not in checks["provider_configuration"].detail


def test_setup_readiness_requires_openrouter_secret_even_with_default_base_url(tmp_path: Path) -> None:
    report = build_setup_readiness_report(
        AgentConfig(
            provider="openrouter",
            model="openai/gpt-5.5",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="MISSING_OPENROUTER_TEST_KEY",
            workspace=tmp_path,
            memory_dir=tmp_path / "memory",
        )
    )

    checks = {check.check_id: check for check in report.checks}
    assert checks["provider_configuration"].status == SetupReadinessStatus.FAIL
    assert "MISSING_OPENROUTER_TEST_KEY" in checks["provider_configuration"].detail


def test_setup_readiness_uses_endpoint_and_credential_scoped_provider_health(
    tmp_path: Path,
) -> None:
    healthy = AgentConfig(
        provider="openai-compatible",
        model="shared-model",
        base_url="https://healthy.example/v1",
        workspace=tmp_path,
        memory_dir=tmp_path / "memory",
    )
    other_endpoint = AgentConfig(
        **{**healthy.__dict__, "base_url": "https://unknown.example/v1"}
    )
    global_provider_health_registry.reset()
    global_provider_health_registry.record_success(provider_health_id(healthy))
    try:
        healthy_checks = {
            check.check_id: check for check in build_setup_readiness_report(healthy).checks
        }
        other_checks = {
            check.check_id: check
            for check in build_setup_readiness_report(other_endpoint).checks
        }
    finally:
        global_provider_health_registry.reset()

    assert healthy_checks["provider_operational"].status == SetupReadinessStatus.PASS
    assert other_checks["provider_operational"].status == SetupReadinessStatus.WARN


def test_product_setup_route_uses_active_config(tmp_path: Path) -> None:
    app = FastAPI()
    config = AgentConfig(provider="mock", workspace=tmp_path, memory_dir=tmp_path / "memory")
    register_product_routes(app, active_config=lambda: config)
    client = TestClient(app)

    response = client.get("/api/product/setup")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "kestrel.setup_readiness.v1"
    assert any(check["check_id"] == "workspace" for check in payload["checks"])


def test_provider_certification_report_is_redacted_and_actionable(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-providerCertificationSecret123456")

    report = build_provider_certification_report(
        AgentConfig(
            provider="openai",
            model="gpt-test",
            api_key_env="OPENAI_API_KEY",
            workspace=tmp_path,
        )
    )
    payload = report.to_dict()

    assert payload["schema"] == "kestrel.provider_certification.v2"
    assert payload["policy_version"] == PROVIDER_CERTIFICATION_POLICY_VERSION
    assert payload["subject"] == {"commit": "unknown", "tree_digest": "unknown"}
    assert payload["headline"]["total_providers"] == len(PROVIDER_OPTIONS)
    assert payload["headline"]["release_certified"] is False
    assert "sk-proj-providerCertificationSecret" not in str(payload)

    providers = {provider["provider"]: provider for provider in payload["providers"]}
    assert tuple(providers) == PROVIDER_OPTIONS
    assert providers["mock"]["status"] == ProviderCertificationStatus.CERTIFIED.value
    assert (
        providers["mock"]["readiness"]["status"]
        == ProviderCertificationStatus.CONFIGURED.value
    )
    assert providers["mock"]["certification_state"] == ProviderCertificationState.IMPLEMENTED.value
    assert providers["mock"]["last_tested"] is None
    assert providers["openai"]["status"] == ProviderCertificationStatus.CONFIGURED.value
    assert providers["openai"]["certification_state"] == ProviderCertificationState.IMPLEMENTED.value
    assert providers["openai"]["api_key_env"] == {"name": "OPENAI_API_KEY", "present": True}
    assert providers["anthropic"]["status"] == ProviderCertificationStatus.BLOCKED.value
    assert providers["anthropic"]["api_key_env"]["present"] is False
    assert providers["codex-cli"]["status"] in {
        ProviderCertificationStatus.CONFIGURED.value,
        ProviderCertificationStatus.MANUAL_VALIDATION_REQUIRED.value,
    }


def test_product_provider_certification_route_uses_active_config(tmp_path: Path) -> None:
    app = FastAPI()
    config = AgentConfig(provider="mock", workspace=tmp_path)
    register_product_routes(app, active_config=lambda: config)
    client = TestClient(app)

    response = client.get("/api/product/provider-certification")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "kestrel.provider_certification.v2"
    assert [provider["provider"] for provider in payload["providers"]] == list(PROVIDER_OPTIONS)
    mock = next(provider for provider in payload["providers"] if provider["provider"] == "mock")
    assert mock["certification_state"] == ProviderCertificationState.IMPLEMENTED.value
    assert mock["generate"]["status"] == "not_run"
