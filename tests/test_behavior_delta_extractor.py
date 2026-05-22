from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from nested_memvid_agent.behavior_delta import (
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
)
from nested_memvid_agent.behavior_delta_extractor import (
    BehaviorDeltaExtractor,
    extract_behavior_deltas_from_capsule,
)
from nested_memvid_agent.behavior_delta_ledger import BehaviorDeltaLedger
from nested_memvid_agent.cli import main
from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.task_capsule import write_run_capsule


def test_extracts_policy_delta_from_explicit_instruction_capsule() -> None:
    capsule = {
        "run_id": "run-policy",
        "objective": "Preserve canonical memory substrate",
        "candidate_policy_items": [
            "Do not replace .mv2 with Chroma, Qdrant, FAISS, or SQLite FTS as canonical memory."
        ],
    }

    deltas = extract_behavior_deltas_from_capsule(capsule, run_id="run-policy")

    assert len(deltas) == 1
    delta = deltas[0]
    assert delta.status == BehaviorDeltaStatus.PROPOSED
    assert delta.kind == BehaviorDeltaKind.POLICY
    assert delta.target_layer == MemoryLayer.POLICY
    assert delta.risk == BehaviorDeltaRisk.HIGH
    assert delta.validation_plan.requires_human_approval is True
    assert delta.validation_plan.requires_exact_call_approval is True
    assert delta.evidence_refs[0].source == "task_capsule"
    assert delta.evidence_refs[0].locator == "run-policy:candidate_policy_items:1"
    assert ".mv2" in delta.trigger.query_patterns
    assert "Do not replace .mv2" in delta.behavior_change
    assert delta.metadata["extraction_source"] == "candidate_policy_items"


def test_extracts_procedure_delta_from_specific_reusable_lesson() -> None:
    capsule = {
        "run_id": "run-procedure",
        "objective": "Fix validation loop",
        "candidate_procedures": [
            "When validation fails, compare the failed command and arguments before retrying; require a changed strategy."
        ],
    }

    deltas = extract_behavior_deltas_from_capsule(capsule, run_id="run-procedure")

    assert len(deltas) == 1
    delta = deltas[0]
    assert delta.kind == BehaviorDeltaKind.PROCEDURE
    assert delta.target_layer == MemoryLayer.PROCEDURAL
    assert delta.risk == BehaviorDeltaRisk.MEDIUM
    assert delta.validation_plan.requires_human_approval is False
    assert delta.validation_plan.min_repeat_count == 2
    assert "validation" in delta.trigger.query_patterns
    assert "changed strategy" in delta.behavior_change


def test_extracts_tool_heuristic_from_repeated_failed_tool_attempts() -> None:
    capsule = {
        "run_id": "run-tools",
        "objective": "Repair test failure",
        "tool_calls": [
            {"tool": "shell.run", "arguments": {"command": "python -m pytest tests/test_x.py"}, "success": False},
            {"tool": "shell.run", "arguments": {"command": "python -m pytest tests/test_x.py"}, "success": False},
        ],
        "errors_encountered": ["pytest failed twice with the same command"],
    }

    deltas = extract_behavior_deltas_from_capsule(capsule, run_id="run-tools")

    assert len(deltas) == 1
    delta = deltas[0]
    assert delta.kind == BehaviorDeltaKind.TOOL_HEURISTIC
    assert delta.target_layer == MemoryLayer.PROCEDURAL
    assert delta.risk == BehaviorDeltaRisk.MEDIUM
    assert delta.trigger.tool_names == ("shell.run",)
    assert "unchanged retries" in delta.behavior_change
    assert delta.metadata["repeat_count"] == 2


def test_vague_candidates_are_rejected() -> None:
    capsule = {
        "run_id": "run-vague",
        "objective": "General cleanup",
        "reusable_lessons": ["Be more careful next time."],
        "candidate_procedures": ["Improve things."],
    }

    deltas = extract_behavior_deltas_from_capsule(capsule, run_id="run-vague")

    assert deltas == []


def test_cli_deltas_propose_dry_run_prints_proposals_without_recording(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = tmp_path / "runs"
    state_path = tmp_path / "state.db"
    write_run_capsule(
        runs_dir=runs_dir,
        run_id="run-cli",
        objective="Learn validation repair procedure",
        backend="memory",
        candidate_procedures=(
            "When validation fails, inspect the failing command before retrying with a changed strategy.",
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "deltas",
            "propose",
            "--run-id",
            "run-cli",
            "--runs-dir",
            str(runs_dir),
            "--state-path",
            str(state_path),
            "--backend",
            "memory",
            "--dry-run",
            "--json",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["proposal_count"] == 1
    assert payload["proposals"][0]["status"] == "proposed"
    assert payload["proposals"][0]["kind"] == "procedure"
    assert BehaviorDeltaLedger(AgentStateStore(state_path)).list_deltas() == []


def test_extractor_can_record_proposals_when_not_dry_run(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    ledger = BehaviorDeltaLedger(AgentStateStore(state_path))
    capsule = {
        "run_id": "run-record",
        "objective": "Capture correction",
        "candidate_corrections": [
            "When a user corrects a durable fact, mark the prior record inactive and preserve evidence."
        ],
    }

    deltas = BehaviorDeltaExtractor(ledger=ledger).propose_from_capsule(
        capsule,
        run_id="run-record",
        dry_run=False,
    )

    assert len(deltas) == 1
    assert ledger.list_deltas() == deltas
    assert deltas[0].status == BehaviorDeltaStatus.PROPOSED
