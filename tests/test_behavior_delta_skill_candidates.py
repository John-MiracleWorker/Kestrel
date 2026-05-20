from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from nested_memvid_agent.behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    TriggerSpec,
    ValidationPlan,
)
from nested_memvid_agent.behavior_delta_ledger import BehaviorDeltaLedger
from nested_memvid_agent.behavior_delta_skill import render_skill_candidate_preview
from nested_memvid_agent.cli import main
from nested_memvid_agent.models import EvidenceRef, MemoryLayer
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


def _skill_delta(delta_id: str = "delta_skill_retry") -> BehaviorDelta:
    return BehaviorDelta(
        id=delta_id,
        title="Changed validation retry workflow",
        kind=BehaviorDeltaKind.SKILL_CANDIDATE,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.MEDIUM,
        status=BehaviorDeltaStatus.STAGED,
        trigger=TriggerSpec(
            query_patterns=("validation", "pytest", "retry"),
            task_types=("repair", "debugging"),
            tool_names=("shell.run",),
            semantic_hint="Use when a validation command fails and the next attempt needs a changed strategy.",
        ),
        behavior_change="When validation fails, compare the failed command and arguments before retrying; block unchanged retries unless a changed strategy is supplied.",
        evidence_refs=(EvidenceRef(source="task_capsule", locator="run-123:candidate_procedures:1", quote="pytest failed twice"),),
        validation_plan=ValidationPlan(
            required_checks=("pytest target", "full regression"),
            replay_scenarios=("repeated_validation_retry_requires_changed_strategy",),
            min_validation_score=0.82,
            min_repeat_count=2,
        ),
        metadata={
            "pitfalls": ["Do not rerun the exact same failing command without a changed strategy."],
            "verification": ["The next validation command differs or includes a documented reason for retrying."],
        },
    )


def test_skill_candidate_preview_renders_manifest_and_skill_md_without_installing(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    preview = render_skill_candidate_preview(_skill_delta(), skill_id="validation-retry-strategy")

    assert preview.installable is False
    assert preview.manifest["id"] == "validation-retry-strategy"
    assert preview.manifest["runtime"] == {"type": "instruction"}
    assert preview.manifest["requires_approval"] is True
    assert preview.manifest["provenance"]["behavior_delta_id"] == "delta_skill_retry"
    assert preview.validation["ok"] is True
    assert "# Skill Name" not in preview.instructions
    assert "# Changed validation retry workflow" in preview.instructions
    assert "## Trigger" in preview.instructions
    assert "validation" in preview.instructions
    assert "## Procedure" in preview.instructions
    assert "changed strategy" in preview.instructions
    assert "## Verification" in preview.instructions
    assert "pytest target" in preview.instructions
    assert "## Pitfalls" in preview.instructions
    assert "exact same failing command" in preview.instructions
    assert "## Evidence" in preview.instructions
    assert "run-123:candidate_procedures:1" in preview.instructions
    assert SkillManager(tmp_path / "skills", state).list_skills() == []


def test_skill_preview_rejects_non_skill_candidate_delta() -> None:
    delta = BehaviorDelta(
        id="delta_proc",
        title="Procedure only",
        kind=BehaviorDeltaKind.PROCEDURE,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.MEDIUM,
        trigger=TriggerSpec(query_patterns=("validation",)),
        behavior_change="When validation fails, inspect logs before retrying.",
        evidence_refs=(EvidenceRef(source="test", locator="x"),),
        validation_plan=ValidationPlan(required_checks=("pytest",)),
    )

    with pytest.raises(ValueError, match="skill_candidate"):
        render_skill_candidate_preview(delta)


def test_skill_preview_blocks_executable_runtime_from_delta_metadata() -> None:
    delta = BehaviorDelta(
        id="delta_exec_skill",
        title="Dangerous generated skill",
        kind=BehaviorDeltaKind.SKILL_CANDIDATE,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.HIGH,
        status=BehaviorDeltaStatus.STAGED,
        trigger=TriggerSpec(query_patterns=("deploy",)),
        behavior_change="Run a shell deploy script automatically.",
        evidence_refs=(EvidenceRef(source="test", locator="danger"),),
        validation_plan=ValidationPlan(required_checks=("review",), requires_human_approval=True),
        metadata={"skill_runtime": {"type": "shell", "command": ["deploy.sh"]}},
    )

    preview = render_skill_candidate_preview(delta)

    assert preview.manifest["runtime"] == {"type": "instruction"}
    assert preview.validation["ok"] is True
    assert "deploy.sh" not in json.dumps(preview.manifest)
    assert "Executable code was not generated" in preview.instructions


def test_cli_memory_deltas_skill_preview_outputs_json_and_does_not_install(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "state.db"
    skills_dir = tmp_path / "skills"
    ledger = BehaviorDeltaLedger(AgentStateStore(state_path))
    delta = _skill_delta("delta_cli_skill")
    ledger.record_delta(delta)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "deltas",
            "skill-preview",
            "delta_cli_skill",
            "--state-path",
            str(state_path),
            "--skills-dir",
            str(skills_dir),
            "--json",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["delta_id"] == "delta_cli_skill"
    assert payload["installable"] is False
    assert payload["validation"]["ok"] is True
    assert payload["manifest"]["runtime"] == {"type": "instruction"}
    assert "## Evidence" in payload["instructions"]
    assert not skills_dir.exists() or not any(skills_dir.iterdir())
    assert SkillManager(skills_dir, AgentStateStore(state_path)).list_skills() == []
