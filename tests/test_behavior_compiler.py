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
from nested_memvid_agent.behavior_compiler import (
    BehaviorCompiler,
    BehaviorCompilerConfig,
    BehaviorCompileRequest,
    ToolPreflightContext,
)
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


def test_compile_for_tool_call_returns_empty_when_no_active_delta_matches(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta(
        "delta_unrelated_tool",
        kind=BehaviorDeltaKind.TOOL_HEURISTIC,
        layer=MemoryLayer.PROCEDURAL,
        trigger=TriggerSpec(tool_names=("repair.validate",)),
    )
    ledger.record_delta(delta)
    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True))

    compiled = compiler.compile_for_tool_call(
        ToolPreflightContext(
            run_id="run-tool",
            task_id="task-tool",
            objective="Run tests",
            tool_name="test.run",
            tool_arguments={"command": ["pytest"]},
        ),
        ledger.list_deltas(),
    )

    assert compiled.text == ""
    assert compiled.deltas == ()
    assert ledger.list_activations(delta.id) == []


def test_compile_for_tool_call_ignores_non_active_deltas(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    for status in (
        BehaviorDeltaStatus.PROPOSED,
        BehaviorDeltaStatus.STAGED,
        BehaviorDeltaStatus.REJECTED,
    ):
        ledger.record_delta(
            _delta(
                f"delta_{status.value}",
                kind=BehaviorDeltaKind.TOOL_HEURISTIC,
                layer=MemoryLayer.PROCEDURAL,
                status=status,
                trigger=TriggerSpec(tool_names=("test.run",)),
            )
        )
    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True))

    compiled = compiler.compile_for_tool_call(
        ToolPreflightContext(
            run_id="run-tool",
            task_id=None,
            objective="Run tests",
            tool_name="test.run",
            tool_arguments={"command": ["pytest"]},
        ),
        ledger.list_deltas(),
    )

    assert compiled.text == ""
    assert compiled.deltas == ()


def test_compile_for_tool_call_matches_active_tool_heuristic_by_tool_name(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta(
        "delta_retry_strategy",
        kind=BehaviorDeltaKind.TOOL_HEURISTIC,
        layer=MemoryLayer.PROCEDURAL,
        behavior_change=(
            "Before retrying the same validation tool with unchanged arguments, "
            "require a changed strategy or changed arguments."
        ),
        trigger=TriggerSpec(tool_names=("test.run", "repair.validate"), risk_tags=("repeated_failure",)),
    )
    ledger.record_delta(delta)
    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True))

    compiled = compiler.compile_for_tool_call(
        ToolPreflightContext(
            run_id="run-tool",
            task_id="task-tool",
            objective="Fix the failing tests",
            tool_name="test.run",
            tool_arguments={"command": ["pytest", "-q"]},
            risk_tags=("repeated_failure",),
            tool_call_id="call-1",
        ),
        ledger.list_deltas(),
    )

    assert "TOOL BEHAVIOR-DELTA PREFLIGHT" in compiled.text
    assert "changed strategy or changed arguments" in compiled.text
    assert "delta_retry_strategy" in compiled.text
    assert compiled.deltas == (delta,)
    activations = ledger.list_activations(delta.id)
    assert len(activations) == 1
    assert "matched_tool_name" in activations[0].activation_reason


def test_compile_for_tool_call_matches_active_procedure_by_path_glob(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta(
        "delta_docs_procedure",
        kind=BehaviorDeltaKind.PROCEDURE,
        layer=MemoryLayer.PROCEDURAL,
        behavior_change="Before editing controlled self-modification docs, keep default-off behavior explicit.",
        trigger=TriggerSpec(path_globs=("docs/*.md",)),
    )
    ledger.record_delta(delta)
    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True))

    compiled = compiler.compile_for_tool_call(
        ToolPreflightContext(
            run_id="run-docs",
            task_id="task-docs",
            objective="Update controlled self-modification docs",
            tool_name="file.write",
            tool_arguments={"path": "docs/CONTROLLED_SELF_MODIFICATION.md"},
            touched_paths=("docs/CONTROLLED_SELF_MODIFICATION.md",),
            tool_call_id="call-docs",
        ),
        ledger.list_deltas(),
    )

    assert compiled.deltas == (delta,)
    assert "ACTIVE PROCEDURES" in compiled.text
    assert "default-off behavior" in compiled.text
    assert "matched_path_glob" in ledger.list_activations(delta.id)[0].activation_reason


def test_compile_for_tool_call_policy_gate_requires_active_relevant_match(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    staged_policy = _delta(
        "delta_staged_policy_gate",
        kind=BehaviorDeltaKind.APPROVAL_GATE_RULE,
        layer=MemoryLayer.POLICY,
        risk=BehaviorDeltaRisk.HIGH,
        status=BehaviorDeltaStatus.STAGED,
        behavior_change="Before modifying memory or policy, verify approval gates.",
        trigger=TriggerSpec(tool_names=("memory.correct",), memory_layers=(MemoryLayer.POLICY,)),
    )
    active_irrelevant = _delta(
        "delta_irrelevant_policy_gate",
        kind=BehaviorDeltaKind.APPROVAL_GATE_RULE,
        layer=MemoryLayer.POLICY,
        risk=BehaviorDeltaRisk.HIGH,
        behavior_change="Before modifying memory or policy, verify approval gates.",
        trigger=TriggerSpec(tool_names=("git.commit",), memory_layers=(MemoryLayer.POLICY,)),
    )
    active_relevant = _delta(
        "delta_active_policy_gate",
        kind=BehaviorDeltaKind.APPROVAL_GATE_RULE,
        layer=MemoryLayer.POLICY,
        risk=BehaviorDeltaRisk.HIGH,
        behavior_change="Before modifying memory or policy, verify approval gates.",
        trigger=TriggerSpec(tool_names=("memory.correct",), memory_layers=(MemoryLayer.POLICY,)),
    )
    for delta in (staged_policy, active_irrelevant, active_relevant):
        ledger.record_delta(delta)
    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True))

    compiled = compiler.compile_for_tool_call(
        ToolPreflightContext(
            run_id="run-policy",
            task_id="task-policy",
            objective="Correct policy memory",
            tool_name="memory.correct",
            tool_arguments={"layer": "policy"},
            memory_layers=(MemoryLayer.POLICY,),
            risk_tags=("approval_required",),
            tool_call_id="call-policy",
        ),
        ledger.list_deltas(),
    )

    assert compiled.deltas == (active_relevant,)
    assert "ACTIVE POLICY CONSTRAINTS" in compiled.text
    assert "verify approval gates" in compiled.text
    assert ledger.list_activations(staged_policy.id) == []
    assert ledger.list_activations(active_irrelevant.id) == []


def test_compile_for_tool_call_deduplicates_activation_per_run_tool_call_delta(tmp_path: Path) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(tmp_path / "state.db"))
    delta = _delta(
        "delta_tool_dedupe",
        kind=BehaviorDeltaKind.TOOL_HEURISTIC,
        layer=MemoryLayer.PROCEDURAL,
        trigger=TriggerSpec(tool_names=("test.run",)),
    )
    ledger.record_delta(delta)
    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True))
    context = ToolPreflightContext(
        run_id="run-tool",
        task_id="task-tool",
        objective="Run tests",
        tool_name="test.run",
        tool_arguments={"command": ["pytest"]},
        tool_call_id="call-dedupe",
    )

    first = compiler.compile_for_tool_call(context, ledger.list_deltas())
    second = compiler.compile_for_tool_call(context, ledger.list_deltas())

    assert first.deltas == second.deltas == (delta,)
    assert len(ledger.list_activations(delta.id)) == 1
