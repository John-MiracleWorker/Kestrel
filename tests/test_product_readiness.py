from __future__ import annotations

from pathlib import Path

import pytest
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
    assert report.experience_mode.value == "demo"
    assert report.to_dict()["experience_mode"] == "demo"
    assert report.fail_count == 0
    assert report.ready is True
    checks = {check.check_id: check for check in report.checks}
    assert checks["provider_configuration"].status == SetupReadinessStatus.PASS
    assert checks["memory_storage"].status == SetupReadinessStatus.PASS
    assert checks["api_auth"].status == SetupReadinessStatus.WARN
    assert checks["proactive_routines"].status == SetupReadinessStatus.PASS
    assert checks["validation_container"].status == SetupReadinessStatus.PASS


def test_setup_readiness_distinguishes_demo_disconnected_and_connected_modes(
    tmp_path: Path,
) -> None:
    mock = AgentConfig(provider="mock", model="mock", workspace=tmp_path)
    missing_credential = AgentConfig(
        provider="openai",
        model="gpt-test",
        api_key_env="MISSING_KESTREL_MODE_TEST_KEY",
        workspace=tmp_path,
    )
    unvalidated = AgentConfig(
        provider="openai-compatible",
        model="local-model",
        base_url="http://127.0.0.1:1234/v1",
        workspace=tmp_path,
    )

    global_provider_health_registry.reset()
    try:
        assert build_setup_readiness_report(mock).experience_mode.value == "demo"
        missing_report = build_setup_readiness_report(missing_credential)
        assert missing_report.experience_mode.value == "model_not_connected"
        assert missing_report.ready is False
        assert (
            build_setup_readiness_report(unvalidated).experience_mode.value
            == "model_not_connected"
        )

        global_provider_health_registry.record_failure(
            provider_health_id(unvalidated),
            failure_class="endpoint_unreachable",
            retryable=False,
            failure_threshold=3,
        )
        assert (
            build_setup_readiness_report(unvalidated).experience_mode.value
            == "model_not_connected"
        )

        global_provider_health_registry.record_success(
            provider_health_id(unvalidated)
        )
        connected = build_setup_readiness_report(unvalidated)
        assert connected.experience_mode.value == "connected"
        assert connected.to_dict()["experience_mode"] == "connected"
    finally:
        global_provider_health_registry.reset()


def test_setup_readiness_next_action_prioritizes_non_provider_failures(
    tmp_path: Path,
) -> None:
    report = build_setup_readiness_report(
        AgentConfig(
            provider="openai",
            model="gpt-test",
            api_key_env="MISSING_KESTREL_ACTION_TEST_KEY",
            workspace=tmp_path / "missing-workspace",
        )
    )

    assert report.experience_mode.value == "model_not_connected"
    assert report.next_action.startswith("Create the workspace")


def test_setup_readiness_gives_mode_specific_next_actions(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("KESTREL_READINESS_API_TOKEN", "configured-for-test")
    shared = {
        "workspace": tmp_path,
        "memory_dir": memory_dir,
        "state_path": state_dir / "agent.db",
        "log_dir": logs_dir,
        "enable_worker_isolation": True,
        "require_api_auth": True,
        "api_auth_token_env": "KESTREL_READINESS_API_TOKEN",
    }

    demo = build_setup_readiness_report(
        AgentConfig(provider="mock", model="mock", **shared)
    )
    disconnected = build_setup_readiness_report(
        AgentConfig(
            provider="openai-compatible",
            model="local-model",
            base_url="http://127.0.0.1:1234/v1",
            **shared,
        )
    )

    assert demo.next_action.startswith("Demo is ready.")
    assert "`kestrel chat`" in demo.next_action
    assert disconnected.experience_mode.value == "model_not_connected"
    assert disconnected.ready is True
    assert disconnected.next_action.startswith("Open Settings")
    assert "live provider smoke request" in disconnected.next_action


def test_setup_readiness_open_circuit_gives_settings_recovery(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    for name in ("memory", "state", "logs"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("KESTREL_CIRCUIT_API_TOKEN", "configured-for-test")
    config = AgentConfig(
        provider="openai-compatible",
        model="local-model",
        base_url="http://127.0.0.1:1234/v1",
        workspace=tmp_path,
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state" / "agent.db",
        log_dir=tmp_path / "logs",
        enable_worker_isolation=True,
        require_api_auth=True,
        api_auth_token_env="KESTREL_CIRCUIT_API_TOKEN",
    )

    global_provider_health_registry.reset()
    try:
        global_provider_health_registry.record_failure(
            provider_health_id(config),
            failure_class="endpoint_unreachable",
            retryable=True,
            failure_threshold=1,
        )
        report = build_setup_readiness_report(config)

        assert report.experience_mode.value == "model_not_connected"
        assert report.ready is False
        assert report.next_action.startswith("Open Settings")
        assert "live provider smoke request" in report.next_action
    finally:
        global_provider_health_registry.reset()


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


def test_setup_readiness_uses_metadata_only_credential_status(
    tmp_path: Path,
) -> None:
    resolver_calls: list[str | None] = []
    status_calls: list[str | None] = []

    def forbidden_resolver(name_or_ref: str | None) -> str | None:
        resolver_calls.append(name_or_ref)
        raise AssertionError("setup readiness must not resolve raw values")

    def metadata_status(name_or_ref: str | None) -> dict[str, object]:
        status_calls.append(name_or_ref)
        return {
            "source_env": name_or_ref,
            "configured": True,
            "validated": False,
            "source": "broker",
        }

    report = build_setup_readiness_report(
        AgentConfig(
            provider="openai",
            model="gpt-test",
            api_key_env="OPENAI_API_KEY",
            workspace=tmp_path,
            memory_dir=tmp_path / "memory",
        ),
        secret_resolver=forbidden_resolver,
        secret_status=metadata_status,
        credential_storage={
            "schema": "kestrel.desktop_credential_readiness.v1",
            "state": "session_only",
            "backend": "Session memory",
            "persistence": "session",
            "reason": "secret_service_missing",
            "remediation": (
                "Start an unlocked Linux Secret Service to keep "
                "credentials across restarts."
            ),
        },
    )

    payload = report.to_dict()
    checks = {
        check["check_id"]: check for check in payload["checks"]
    }
    assert resolver_calls == []
    assert status_calls == ["OPENAI_API_KEY"]
    assert checks["provider_configuration"]["status"] == "pass"
    assert checks["credential_storage"]["status"] == "warn"
    assert payload["credential_storage"] == {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": "session_only",
        "backend": "Session memory",
        "persistence": "session",
        "reason": "secret_service_missing",
        "remediation": (
            "Start an unlocked Linux Secret Service to keep "
            "credentials across restarts."
        ),
    }


@pytest.mark.parametrize(
    ("state", "backend", "persistence", "reason", "status"),
    [
        (
            "available",
            "macOS Keychain",
            "persistent",
            "ready",
            "pass",
        ),
        (
            "session_only",
            "Session memory",
            "session",
            "secret_service_missing",
            "warn",
        ),
        (
            "locked_vault_required",
            "macOS Keychain",
            "none",
            "vault_locked",
            "warn",
        ),
        (
            "unavailable",
            None,
            "none",
            "keyring_package_missing",
            "warn",
        ),
    ],
)
def test_setup_readiness_projects_exact_credential_storage_state(
    tmp_path: Path,
    state: str,
    backend: str | None,
    persistence: str,
    reason: str,
    status: str,
) -> None:
    resolver_calls: list[str | None] = []
    credential_storage = {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": state,
        "backend": backend,
        "persistence": persistence,
        "reason": reason,
        "remediation": "Fixed safe remediation.",
    }

    report = build_setup_readiness_report(
        AgentConfig(
            provider="mock",
            model="mock",
            workspace=tmp_path,
            memory_dir=tmp_path / "memory",
        ),
        secret_resolver=lambda value: (
            resolver_calls.append(value) or "must-not-resolve"
        ),
        secret_status=lambda _value: pytest.fail(
            "mock provider should not inspect credentials"
        ),
        credential_storage=credential_storage,
    )
    payload = report.to_dict()
    checks = {
        check["check_id"]: check for check in payload["checks"]
    }

    assert checks["credential_storage"] == {
        "check_id": "credential_storage",
        "title": "Credential storage",
        "status": status,
        "detail": (
            f"Credential storage state is {state}; "
            f"persistence is {persistence}."
        ),
        "recovery": "Fixed safe remediation.",
    }
    assert payload["credential_storage"] == credential_storage
    assert resolver_calls == []
    assert report.ready is True


def test_cloud_provider_missing_metadata_still_fails_closed_without_resolve(
    tmp_path: Path,
) -> None:
    resolver_calls: list[str | None] = []
    status_calls: list[str | None] = []

    report = build_setup_readiness_report(
        AgentConfig(
            provider="openai",
            model="gpt-test",
            api_key_env="OPENAI_API_KEY",
            workspace=tmp_path,
            memory_dir=tmp_path / "memory",
        ),
        secret_resolver=lambda value: (
            resolver_calls.append(value) or "must-not-resolve"
        ),
        secret_status=lambda value: (
            status_calls.append(value)
            or {
                "source_env": value,
                "configured": False,
                "validated": False,
                "source": "missing",
            }
        ),
        credential_storage={
            "schema": "kestrel.desktop_credential_readiness.v1",
            "state": "available",
            "backend": "macOS Keychain",
            "persistence": "persistent",
            "reason": "ready",
            "remediation": "No recovery needed.",
        },
    )
    checks = {
        check.check_id: check for check in report.checks
    }

    assert resolver_calls == []
    assert status_calls == ["OPENAI_API_KEY"]
    assert (
        checks["provider_configuration"].status
        == SetupReadinessStatus.FAIL
    )
    assert checks["credential_storage"].status == (
        SetupReadinessStatus.PASS
    )
    assert report.ready is False


def test_product_setup_route_injects_metadata_status_and_storage_readiness(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    status_calls: list[str | None] = []
    credential_storage = {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": "session_only",
        "backend": "Session memory",
        "persistence": "session",
        "reason": "secret_service_missing",
        "remediation": "Start Linux Secret Service.",
    }
    register_product_routes(
        app,
        active_config=lambda: AgentConfig(
            provider="openai",
            model="gpt-test",
            api_key_env="OPENAI_API_KEY",
            workspace=tmp_path,
        ),
        secret_resolver=lambda _value: pytest.fail(
            "route must not resolve a raw secret"
        ),
        secret_status=lambda value: (
            status_calls.append(value)
            or {
                "configured": True,
                "validated": False,
                "source": "broker",
            }
        ),
        credential_storage=lambda: credential_storage,
    )

    response = TestClient(app).get("/api/product/setup")

    assert response.status_code == 200
    assert response.json()["credential_storage"] == (
        credential_storage
    )
    assert status_calls == ["OPENAI_API_KEY"]


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
