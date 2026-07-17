from __future__ import annotations

import json
from pathlib import Path

from scripts.demo_controlled_self_modification import run_demo


def test_controlled_self_modification_demo_proves_full_auditable_loop(tmp_path: Path) -> None:
    result = run_demo(output_dir=tmp_path, backend="memory")

    assert result["passed"] is True
    assert result["capsule"]["exists"] is True
    assert result["capsule"]["path"].endswith("complete.mv2")
    assert result["proposal"]["status"] == "proposed"
    assert result["proposal"]["evidence_count"] >= 1
    assert result["initial_gate"]["status"] == "staged"
    assert "replay_not_passed" in result["initial_gate"]["blocked_by"]
    assert result["replay"]["passed"] is True
    assert result["activation_gate"]["status"] == "active"
    assert result["compiled"]["activation_count"] == 1
    assert "ACTIVE TOOL HEURISTICS" in result["compiled"]["text"]
    assert result["outcome"]["outcome"] == "useful"
    assert result["rollback"]["status"] == "rolled_back"
    assert result["post_rollback_compile"]["activation_count"] == 0
    assert result["report"]["summary"]["total_deltas"] == 1
    assert result["report"]["summary"]["rollback_rate"] == 1.0

    report_path = Path(result["artifacts"]["report_path"])
    json_path = Path(result["artifacts"]["json_path"])
    assert report_path.exists()
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    markdown = report_path.read_text(encoding="utf-8")
    assert "capsule → proposal → mutation gate → replay → activation → outcome → rollback" in markdown
    assert result["proposal"]["delta_id"] in markdown
