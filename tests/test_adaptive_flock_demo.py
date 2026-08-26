"""S11 / PR #311 reconciliation — Adaptive Flock routing demo test.

The demo (``scripts/demo_adaptive_flock.py``) exercises Kestrel's real
routing contracts and scorer without providers, execution, or Git
operations.  The reconciliation pins the production-truth block: learned
routing is NOT wired into the v0.6 runtime, no live grant exists, the only
v0.6 learned-authority class is an exact owner-activated low-risk summarizer
scope, and real decisions fall back deterministically with
``durable_grant_required``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.demo_adaptive_flock import build_demo_report, production_truth_block

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "demo_adaptive_flock.py"


def test_production_truth_block_is_stable_and_truthful() -> None:
    """The report's production-truth block never drifts from the runtime
    reality: learned routing is inert, no live grant, deterministic fallback."""
    block = production_truth_block()
    assert block["wired_activation_evaluator"] is False
    assert block["live_grant"] is False
    assert block["learned_authority_class"] == "v06_low_risk_summarizer_only"
    assert block["deterministic_fallback_reason"] == "durable_grant_required"
    assert "contract-level" in block["claim"]


def test_shadow_demo_routes_each_role_without_making_assignments_actionable() -> None:
    report = build_demo_report()

    assert report["demo"] == "kestrel-adaptive-flock-routing"
    assert report["mode"] == "shadow"
    assert report["routing_only"] is True
    assert report["actionable"] is False
    assert [route["role"] for route in report["routes"]] == [
        "planner",
        "executor",
        "reviewer",
    ]
    assert [route["selected_target_id"] for route in report["routes"]] == [
        "planner-cloud",
        "coder-local",
        "reviewer-independent",
    ]
    assert all(route["actionable"] is False for route in report["routes"])

    executor = report["routes"][1]
    tiny_local = next(
        candidate
        for candidate in executor["candidates"]
        if candidate["target_id"] == "tiny-local"
    )
    assert tiny_local["eligible"] is False
    assert "tools_unsupported" in tiny_local["reason_codes"]
    assert "context_too_small" in tiny_local["reason_codes"]

    reviewer = report["routes"][2]
    coder_local = next(
        candidate
        for candidate in reviewer["candidates"]
        if candidate["target_id"] == "coder-local"
    )
    assert "review_target_not_independent" in coder_local["reason_codes"]
    assert "review_model_family_not_independent" in coder_local["reason_codes"]

    assert report["safety"] == {
        "provider_calls": 0,
        "files_modified": False,
        "git_changes": False,
        "merges_or_pushes": False,
    }
    # The report states production truth alongside the contract exercise.
    assert report["production_truth"]["wired_activation_evaluator"] is False
    assert report["production_truth"]["live_grant"] is False


def test_constrained_demo_marks_route_decisions_actionable_at_contract_level() -> None:
    report = build_demo_report(mode="constrained")

    assert report["mode"] == "constrained"
    # Contract-level actionability only; production authority is still inert.
    assert report["actionable"] is True
    assert all(route["actionable"] is True for route in report["routes"])
    assert report["production_truth"]["wired_activation_evaluator"] is False
    assert report["production_truth"]["live_grant"] is False


def test_json_cli_is_deterministic_and_machine_readable() -> None:
    command = [sys.executable, str(SCRIPT), "--mode", "shadow", "--json"]
    first = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == 0
    assert first.stderr == ""
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["mode"] == "shadow"
    assert payload["routing_only"] is True
    assert payload["safety"]["provider_calls"] == 0
    assert payload["production_truth"]["wired_activation_evaluator"] is False
    assert payload["production_truth"]["live_grant"] is False
    assert payload["production_truth"]["deterministic_fallback_reason"] == (
        "durable_grant_required"
    )
