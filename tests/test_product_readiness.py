from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.product_readiness import ProductReadinessStatus, build_product_readiness_report
from nested_memvid_agent.server_product_routes import register_product_routes
from nested_memvid_agent.setup_readiness import SetupReadinessStatus, build_setup_readiness_report


def test_product_readiness_report_exposes_all_productization_categories() -> None:
    report = build_product_readiness_report()

    assert report.schema == "kestrel.product_readiness.v1"
    assert report.headline.total_categories == 10
    assert report.headline.ready_count >= 0
    assert report.headline.partial_count > 0
    assert report.headline.missing_count > 0
    assert report.headline.product_ready is False

    category_ids = {category.category_id for category in report.categories}
    assert category_ids == {
        "local_product_stability",
        "golden_repair_workflow",
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
    assert learning.status == ProductReadinessStatus.PARTIAL
    assert any("behavior" in item.lower() for item in learning.evidence)
    assert any("auto-activate" in item.lower() for item in learning.remaining_work)


def test_product_readiness_report_serializes_to_public_dict() -> None:
    report = build_product_readiness_report()
    payload = report.to_dict()

    assert payload["schema"] == "kestrel.product_readiness.v1"
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
