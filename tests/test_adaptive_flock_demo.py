from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.demo_adaptive_flock import build_demo_report

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "demo_adaptive_flock.py"


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


def test_constrained_demo_marks_route_decisions_actionable() -> None:
    report = build_demo_report(mode="constrained")

    assert report["mode"] == "constrained"
    assert report["actionable"] is True
    assert all(route["actionable"] is True for route in report["routes"])


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
