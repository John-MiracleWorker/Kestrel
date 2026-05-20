from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    TriggerSpec,
    ValidationPlan,
)
from nested_memvid_agent.behavior_delta_ledger import BehaviorDeltaLedger
from nested_memvid_agent.behavior_compiler import BehaviorCompiler, BehaviorCompilerConfig, BehaviorCompileRequest
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.models import EvidenceRef, MemoryLayer
from nested_memvid_agent.state_store import AgentStateStore


def _delta(
    delta_id: str,
    *,
    kind: BehaviorDeltaKind,
    layer: MemoryLayer,
    risk: BehaviorDeltaRisk = BehaviorDeltaRisk.MEDIUM,
    status: BehaviorDeltaStatus = BehaviorDeltaStatus.ACTIVE,
    behavior_change: str = "Run approval-gate tests before modifying policy memory.",
    trigger: TriggerSpec | None = None,
    importance: float = 0.5,
) -> BehaviorDelta:
    return BehaviorDelta(
        id=delta_id,
        title=delta_id.replace("_", " "),
        kind=kind,
        target_layer=layer,
        risk=risk,
        status=status,
        trigger=trigger or TriggerSpec(query_patterns=("policy",), semantic_hint="policy memory work"),
        behavior_change=behavior_change,
        evidence_refs=(EvidenceRef(source="task_capsule", locator=f"run-1:{delta_id}", quote="validated"),),
        validation_plan=ValidationPlan(replay_scenarios=("scenario",), min_validation_score=0.8),
        importance=importance,
    )


def test_agent_config_behavior_delta_flags_default_off_and_env_enabled(monkeypatch) -> None:
    monkeypatch.delenv("NEST_AGENT_ENABLE_BEHAVIOR_DELTAS", raising=False)
    monkeypatch.delenv("NEST_AGENT_MAX_ACTIVE_DELTAS_PER_RUN", raising=False)

    default_config = AgentConfig.from_env()

    assert default_config.enable_behavior_deltas is False
    assert default_config.max_active_deltas_per_run == 8

    monkeypatch.setenv("NEST_AGENT_ENABLE_BEHAVIOR_DELTAS", "1")
    monkeypatch.setenv("NEST_AGENT_MAX_ACTIVE_DELTAS_PER_RUN", "3")

    enabled_config = AgentConfig.from_env()

    assert enabled_config.enable_behavior_deltas is True
    assert enabled_config.max_active_deltas_per_run == 3


def test_disabled_compiler_returns_empty_output_and_logs_no_activations(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta("delta_policy", kind=BehaviorDeltaKind.POLICY, layer=MemoryLayer.POLICY)
    ledger.record_delta(delta)

    compiled = BehaviorCompiler(
        ledger=ledger,
        config=BehaviorCompilerConfig(enabled=False),
    ).compile(BehaviorCompileRequest(objective="Modify policy memory", query="policy", run_id="run-1"))

    assert compiled.text == ""
    assert compiled.deltas == ()
    assert ledger.list_activations(delta.id) == []


def test_compiler_includes_only_relevant_active_deltas_in_structured_sections(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    active_policy = _delta(
        "delta_policy",
        kind=BehaviorDeltaKind.POLICY,
        layer=MemoryLayer.POLICY,
        risk=BehaviorDeltaRisk.HIGH,
        behavior_change="Preserve .mv2 as the canonical durable memory substrate.",
        trigger=TriggerSpec(query_patterns=("mv2", "memory"), memory_layers=(MemoryLayer.POLICY,)),
    )
    active_procedure = _delta(
        "delta_retry",
        kind=BehaviorDeltaKind.TOOL_HEURISTIC,
        layer=MemoryLayer.PROCEDURAL,
        behavior_change="Before retrying validation, compare the previous command and require a changed strategy.",
        trigger=TriggerSpec(tool_names=("tests.run",), task_types=("debugging",)),
    )
    irrelevant = _delta(
        "delta_irrelevant",
        kind=BehaviorDeltaKind.PROCEDURE,
        layer=MemoryLayer.PROCEDURAL,
        behavior_change="Use design review checklists for UI polish.",
        trigger=TriggerSpec(query_patterns=("frontend",), task_types=("ui_design",)),
    )
    staged = _delta(
        "delta_staged",
        kind=BehaviorDeltaKind.PROCEDURE,
        layer=MemoryLayer.PROCEDURAL,
        status=BehaviorDeltaStatus.STAGED,
        behavior_change="Staged deltas are not compiled.",
        trigger=TriggerSpec(query_patterns=("mv2",)),
    )
    for delta in (active_policy, active_procedure, irrelevant, staged):
        ledger.record_delta(delta)

    compiled = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True)).compile(
        BehaviorCompileRequest(
            objective="Debug memory validation for .mv2 policy behavior",
            query="mv2 memory validation",
            task_type="debugging",
            tool_names=("tests.run",),
            memory_layers=(MemoryLayer.POLICY,),
            run_id="run-1",
        )
    )

    assert "ACTIVE POLICY CONSTRAINTS:" in compiled.text
    assert "ACTIVE TOOL HEURISTICS:" in compiled.text
    assert "DELTA EVIDENCE:" in compiled.text
    assert "Preserve .mv2" in compiled.text
    assert "changed strategy" in compiled.text
    assert "UI polish" not in compiled.text
    assert "Staged deltas" not in compiled.text
    assert [delta.id for delta in compiled.deltas] == ["delta_policy", "delta_retry"]


def test_compiler_caps_deduplicates_and_prioritizes_policy_self_procedural(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    deltas = [
        _delta(
            "delta_procedure",
            kind=BehaviorDeltaKind.PROCEDURE,
            layer=MemoryLayer.PROCEDURAL,
            behavior_change="Shared behavior change.",
            trigger=TriggerSpec(query_patterns=("shared",)),
            importance=0.9,
        ),
        _delta(
            "delta_self",
            kind=BehaviorDeltaKind.SELF_MODEL_RULE,
            layer=MemoryLayer.SELF,
            behavior_change="Prefer verified self-model evidence over guesses.",
            trigger=TriggerSpec(query_patterns=("shared",)),
            importance=0.4,
        ),
        _delta(
            "delta_policy",
            kind=BehaviorDeltaKind.POLICY,
            layer=MemoryLayer.POLICY,
            risk=BehaviorDeltaRisk.HIGH,
            behavior_change="Preserve policy approval gates.",
            trigger=TriggerSpec(query_patterns=("shared",)),
            importance=0.3,
        ),
        _delta(
            "delta_duplicate",
            kind=BehaviorDeltaKind.TOOL_HEURISTIC,
            layer=MemoryLayer.PROCEDURAL,
            behavior_change="Shared behavior change.",
            trigger=TriggerSpec(query_patterns=("shared",)),
            importance=1.0,
        ),
    ]
    for delta in deltas:
        ledger.record_delta(delta)

    compiled = BehaviorCompiler(
        ledger=ledger,
        config=BehaviorCompilerConfig(enabled=True, max_active_deltas_per_run=2),
    ).compile(BehaviorCompileRequest(objective="shared task", query="shared", run_id="run-1"))

    assert [delta.id for delta in compiled.deltas] == ["delta_policy", "delta_self"]
    assert "Preserve policy approval gates" in compiled.text
    assert "Prefer verified self-model" in compiled.text
    assert "Shared behavior change" not in compiled.text


def test_compiler_records_one_activation_per_run_per_delta(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta("delta_policy", kind=BehaviorDeltaKind.POLICY, layer=MemoryLayer.POLICY)
    ledger.record_delta(delta)
    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True))
    request = BehaviorCompileRequest(objective="policy work", query="policy", run_id="run-1", task_id="task-1")

    first = compiler.compile(request)
    second = compiler.compile(request)

    activations = ledger.list_activations(delta.id)
    assert first.deltas == second.deltas == (delta,)
    assert len(activations) == 1
    assert activations[0].run_id == "run-1"
    assert activations[0].task_id == "task-1"
    assert activations[0].compiled_section == "ACTIVE POLICY CONSTRAINTS"
