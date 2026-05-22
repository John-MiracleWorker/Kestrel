from __future__ import annotations

from nested_memvid_agent.product_readiness import ProductReadinessStatus, build_product_readiness_report


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
