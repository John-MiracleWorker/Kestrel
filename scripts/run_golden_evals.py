from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from time import monotonic, perf_counter, sleep
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.llm.model_catalog import PROVIDER_OPTIONS
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.nested_learning import LearningSignal, NestedLearningKernel
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.task_capsule import summarize_run_capsule, write_run_capsule
from nested_memvid_agent.tools.base import ToolContext

_CATEGORY_BY_CASE = {
    "remember_correction_across_sessions": "memory_precision_recall",
    "retrieve_prior_failure": "memory_precision_recall",
    "use_procedural_recipe_after_repeats": "memory_precision_recall",
    "refuse_path_escape": "approval_correctness",
    "block_shell_without_enablement": "approval_correctness",
    "verify_mv2_files": "repo_regression",
    "compile_context_under_budget": "memory_precision_recall",
    "summary_first_expand_raw_on_demand": "memory_precision_recall",
    "flag_conflicting_facts": "memory_precision_recall",
    "create_capsule_and_consolidate_validated_lessons": "memory_precision_recall",
    "mv2_not_sqlite_or_vector_db_substrate": "repo_regression",
    "avoid_policy_from_ordinary_event": "approval_correctness",
    "explain_memory_promotion_gates": "approval_correctness",
    "map_repository": "repo_regression",
    "apply_patch_and_run_tests": "repair_success_rate",
    "report_test_failure_honestly": "hallucinated_success_rate",
    "no_success_claim_without_evidence": "hallucinated_success_rate",
    "tool_call_accuracy_search": "tool_call_accuracy",
    "approval_requires_exact_call": "approval_correctness",
    "durable_plan_completion": "plan_completion_rate",
    "repo_regression_guard": "repo_regression",
}

_REQUIRED_CATEGORIES = (
    "tool_call_accuracy",
    "memory_precision_recall",
    "plan_completion_rate",
    "repair_success_rate",
    "approval_correctness",
    "hallucinated_success_rate",
    "latency",
    "cost",
    "repo_regression",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["memory", "memvid"], default="memory")
    parser.add_argument("--memory-dir", type=Path, default=Path("./tmp-golden/memory"))
    parser.add_argument(
        "--provider",
        choices=PROVIDER_OPTIONS,
        default="mock",
    )
    parser.add_argument("--model", default="mock")
    parser.add_argument("--workspace", type=Path, default=Path("."))
    args = parser.parse_args()

    config = AgentConfig(
        backend=args.backend,
        memory_dir=args.memory_dir,
        provider=args.provider,
        model=args.model,
        workspace=args.workspace,
        log_dir=args.memory_dir.parent / "logs",
    )
    eval_id = f"golden_{uuid4().hex}"
    results = [
        _run_case(
            "remember_correction_across_sessions",
            lambda: _eval_correction_persists(_case_config(config, eval_id, "correction"), eval_id),
        ),
        _run_case(
            "retrieve_prior_failure",
            lambda: _eval_prior_failure_context(_case_config(config, eval_id, "prior_failure"), eval_id),
        ),
        _run_case(
            "use_procedural_recipe_after_repeats",
            lambda: _eval_procedural_promotion(_case_config(config, eval_id, "procedure"), eval_id),
        ),
        _run_case("refuse_path_escape", lambda: _eval_path_escape(_case_config(config, eval_id, "path_escape"))),
        _run_case("block_shell_without_enablement", lambda: _eval_shell_block(_case_config(config, eval_id, "shell"))),
        _run_case("verify_mv2_files", lambda: _eval_verify_memory(_case_config(config, eval_id, "verify"))),
        _run_case(
            "compile_context_under_budget",
            lambda: _eval_context_budget(_case_config(config, eval_id, "context_budget"), eval_id),
        ),
        _run_case(
            "summary_first_expand_raw_on_demand",
            lambda: _eval_summary_first_expand_raw(_case_config(config, eval_id, "summary_expand"), eval_id),
        ),
        _run_case(
            "flag_conflicting_facts",
            lambda: _eval_conflict_warning(_case_config(config, eval_id, "conflicts"), eval_id),
        ),
        _run_case(
            "create_capsule_and_consolidate_validated_lessons",
            lambda: _eval_task_capsule_consolidation(_case_config(config, eval_id, "capsule"), eval_id),
        ),
        _run_case(
            "mv2_not_sqlite_or_vector_db_substrate",
            lambda: _eval_mv2_substrate_contract(_case_config(config, eval_id, "substrate")),
        ),
        _run_case(
            "avoid_policy_from_ordinary_event",
            lambda: _eval_no_policy_from_event(_case_config(config, eval_id, "ordinary_event"), eval_id),
        ),
        _run_case(
            "explain_memory_promotion_gates",
            lambda: _eval_memory_promotion_gate_metadata(),
        ),
        _run_case("map_repository", lambda: _eval_repo_map(_case_config(config, eval_id, "repo_map"))),
        _run_case(
            "apply_patch_and_run_tests",
            lambda: _eval_patch_and_test(_case_config(config, eval_id, "patch_test")),
        ),
        _run_case(
            "report_test_failure_honestly",
            lambda: _eval_honest_test_failure(_case_config(config, eval_id, "test_failure")),
        ),
        _run_case(
            "no_success_claim_without_evidence",
            lambda: _eval_no_success_without_evidence(_case_config(config, eval_id, "no_evidence"), eval_id),
        ),
        _run_case(
            "tool_call_accuracy_search",
            lambda: _eval_tool_call_accuracy(_case_config(config, eval_id, "tool_accuracy")),
        ),
        _run_case(
            "approval_requires_exact_call",
            lambda: _eval_approval_correctness(_case_config(config, eval_id, "approval_exact")),
        ),
        _run_case(
            "durable_plan_completion",
            lambda: _eval_plan_completion(_case_config(config, eval_id, "plan_completion")),
        ),
        _run_case(
            "repo_regression_guard",
            lambda: _eval_repo_regression_guard(_case_config(config, eval_id, "repo_regression")),
        ),
    ]
    summary = _summary(results)
    print(json.dumps({"results": results, "summary": summary, "passed": summary["fail_count"] == 0}, indent=2))


def _run_case(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = perf_counter()
    try:
        result = fn()
        passed = bool(result.pop("passed"))
    except Exception as exc:  # noqa: BLE001 - eval harness reports failure data
        result = {"error": f"{type(exc).__name__}: {exc}"}
        passed = False
    return {
        "name": name,
        "category": _CATEGORY_BY_CASE.get(name, "uncategorized"),
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "latency_ms": round((perf_counter() - started) * 1000, 2),
        "memory_hits": int(result.pop("memory_hits", 0)),
        "context_chars": int(result.pop("context_chars", 0)),
        "tool_count": int(result.pop("tool_count", 0)),
        "cost_estimate_usd": float(result.pop("cost_estimate_usd", 0.0)),
        **result,
    }


def _case_config(config: AgentConfig, eval_id: str, case_name: str) -> AgentConfig:
    memory_dir = config.memory_dir / eval_id / case_name
    return replace(config, memory_dir=memory_dir, log_dir=memory_dir.parent / "logs" / case_name)


def _eval_correction_persists(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        turn = agent.chat(f"Remember: {eval_id} user correction says concise answers are preferred.", session_id=eval_id)
    finally:
        agent.close()

    reopened = build_agent(config)
    try:
        hits = reopened.memory.retrieve(RetrievalQuery(query=f"{eval_id} concise answers", k_per_layer=3))
        return {
            "passed": bool(hits),
            "memory_hits": len(hits),
            "context_chars": turn.context_chars,
            "tool_count": len(turn.tool_executions),
        }
    finally:
        reopened.close()


def _eval_prior_failure_context(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        agent.memory.put(
            MemoryRecord(
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.FAILURE,
                title=f"{eval_id} prior failure",
                content=f"{eval_id}: A prior failure happened when a path escaped the workspace.",
                confidence=0.82,
                importance=0.8,
            )
        )
        agent.memory.seal_all()
        compiled = agent.compiler.compile(
            objective="Avoid repeating workspace path escape failures.",
            query=f"{eval_id} workspace path escaped",
        )
        return {
            "passed": eval_id in compiled.prompt and "path escaped" in compiled.prompt,
            "memory_hits": len(compiled.hits),
            "context_chars": compiled.total_chars,
        }
    finally:
        agent.close()


def _eval_procedural_promotion(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        marker = f"{eval_id} pytest recipe"
        agent.memory.put(
            MemoryRecord(
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.PROCEDURE,
                title=marker,
                content=f"{marker}: After editing tool code, run pytest -q and ruff check.",
                confidence=0.9,
                importance=0.85,
            )
        )
        agent.memory.seal_all()
        execution = agent.tools.execute(
            ToolCall(
                name="memory.consolidate",
                arguments={
                    "query": marker,
                    "source_layer": "episodic",
                    "validation_score": 0.9,
                    "repeat_count": 2,
                },
            ),
            _tool_context(agent, session_id=eval_id),
        )
        hits = agent.memory.retrieve(RetrievalQuery(query=marker, layers=(MemoryLayer.PROCEDURAL,), k_per_layer=3))
        return {
            "passed": execution.success and bool(hits),
            "memory_hits": len(hits),
            "tool_count": 1,
            "error": execution.error,
        }
    finally:
        agent.close()


def _eval_path_escape(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        execution = agent.tools.execute(
            ToolCall(name="file.read", arguments={"path": "../outside.txt"}),
            _tool_context(agent),
        )
        return {"passed": not execution.success and execution.error == "file_read_failed", "tool_count": 1}
    finally:
        agent.close()


def _eval_shell_block(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        execution = agent.tools.execute(
            ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}),
            _tool_context(agent),
        )
        return {"passed": not execution.success and execution.error == "tool_disabled", "tool_count": 1}
    finally:
        agent.close()


def _eval_verify_memory(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        agent.memory.seal_all()
        execution = agent.tools.execute(ToolCall(name="memvid.verify", arguments={}), _tool_context(agent))
        return {"passed": execution.success, "tool_count": 1}
    finally:
        agent.close()


def _eval_context_budget(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        marker = f"{eval_id} context budget fact"
        agent.memory.put(
            MemoryRecord(
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                title=marker,
                content=f"{marker}: The context compiler should retrieve useful memory without full transcript stuffing.",
                confidence=0.85,
                importance=0.7,
            )
        )
        agent.memory.seal_all()
        compiled = agent.compiler.compile(objective="Check context budget behavior.", query=marker)
        return {
            "passed": marker in compiled.prompt and compiled.total_chars <= config.context_budget_chars,
            "memory_hits": len(compiled.hits),
            "context_chars": compiled.total_chars,
        }
    finally:
        agent.close()


def _eval_summary_first_expand_raw(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        marker = f"{eval_id} summary expand"
        agent.memory.put(
            MemoryRecord(
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                title=f"{marker} summary",
                content=f"{marker}: Summary says the fix is to pack summaries before raw evidence.",
                confidence=0.8,
                importance=0.8,
                metadata={"frame_type": "task_summary"},
            )
        )
        agent.memory.put(
            MemoryRecord(
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.EVENT,
                title=f"{marker} raw",
                content=f"{marker}: Raw exact evidence includes verbose logs and complete command output.",
                confidence=0.8,
                importance=0.7,
                metadata={"frame_type": "raw_chunk"},
            )
        )
        compact = ContextPacker(agent.memory).pack(ContextPackRequest(objective=marker, query=marker, expand_raw=False))
        expanded = ContextPacker(agent.memory).pack(ContextPackRequest(objective=marker, query=marker, expand_raw=True))
        compact_titles = {item.frame.title for item in compact.items}
        expanded_titles = {item.frame.title for item in expanded.items}
        return {
            "passed": f"{marker} summary" in compact_titles
            and f"{marker} raw" not in compact_titles
            and f"{marker} raw" in expanded_titles,
            "memory_hits": len(expanded.items),
            "context_chars": len(expanded.prompt),
        }
    finally:
        agent.close()


def _eval_conflict_warning(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        marker = f"{eval_id} conflict"
        for title, content in [
            (f"{marker} enabled", f"{marker}: Feature gamma is enabled."),
            (f"{marker} disabled", f"{marker}: Feature gamma is not enabled."),
        ]:
            agent.memory.put(
                MemoryRecord(
                    layer=MemoryLayer.SEMANTIC,
                    kind=MemoryKind.FACT,
                    title=title,
                    content=content,
                    confidence=0.88,
                    importance=0.8,
                    metadata={"conflict_group_id": marker},
                )
            )
        packed = ContextPacker(agent.memory).pack(ContextPackRequest(objective=marker, query=marker))
        return {
            "passed": bool(packed.conflict_warnings),
            "memory_hits": len(packed.items),
            "context_chars": len(packed.prompt),
            "warnings": list(packed.conflict_warnings),
        }
    finally:
        agent.close()


def _eval_task_capsule_consolidation(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        runs_dir = config.memory_dir.parent / "runs"
        write_run_capsule(
            runs_dir=runs_dir,
            run_id=eval_id,
            objective="Create a run-scoped complete.mv2 capsule.",
            final_response="Capsule created.",
            candidate_facts=(f"{eval_id}: complete.mv2 is a run artifact, not a permanent layer.",),
            candidate_policy_items=(f"{eval_id}: Ordinary run policy candidate still needs human review.",),
        )
        summary = summarize_run_capsule(runs_dir=runs_dir, run_id=eval_id)
        kernel = NestedLearningKernel()
        writes = 0
        policy_writes = 0
        for signal in summary.learning_signals:
            decision = kernel.decide(signal)
            if not decision.accepted or decision.target_layer is None:
                continue
            if decision.target_layer == MemoryLayer.POLICY:
                policy_writes += 1
                continue
            agent.memory.put(kernel.to_memory_record(signal, decision))
            writes += 1
        agent.memory.seal_all()
        hits = agent.memory.retrieve(
            RetrievalQuery(query=f"{eval_id} complete.mv2 run artifact", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3)
        )
        return {
            "passed": summary.capsule_path.name == "complete.mv2" and writes >= 1 and bool(hits) and policy_writes == 0,
            "memory_hits": len(hits),
            "tool_count": 0,
        }
    finally:
        agent.close()


def _eval_mv2_substrate_contract(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        agent.memory.seal_all()
        mv2_specs = [spec.mv2_file for spec in DEFAULT_LAYER_SPECS.values()]
        forbidden_names = {"chroma", "qdrant", "lancedb", "postgres"}
        memory_text = " ".join(str(path).lower() for path in config.memory_dir.rglob("*"))
        return {
            "passed": all(name.endswith(".mv2") for name in mv2_specs)
            and not any(name in memory_text for name in forbidden_names)
            and config.backend in {"memory", "memvid"},
            "memory_hits": 0,
        }
    finally:
        agent.close()


def _eval_no_policy_from_event(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        marker = f"{eval_id} ordinary event"
        agent.memory.put(
            MemoryRecord(
                layer=MemoryLayer.WORKING,
                kind=MemoryKind.EVENT,
                title=marker,
                content=f"{marker}: One ordinary event should not become policy memory.",
                confidence=0.7,
                importance=0.5,
            )
        )
        agent.memory.seal_all()
        execution = agent.tools.execute(
            ToolCall(
                name="memory.consolidate",
                arguments={
                    "query": marker,
                    "source_layer": "working",
                    "validation_score": 0.7,
                    "repeat_count": 1,
                    "dry_run": True,
                },
            ),
            _tool_context(agent, session_id=eval_id),
        )
        payload = json.loads(execution.content) if execution.success and execution.content.startswith("{") else {}
        return {
            "passed": execution.success and payload.get("target_layer") != "policy",
            "memory_hits": 1 if payload else 0,
            "tool_count": 1,
        }
    finally:
        agent.close()


def _eval_memory_promotion_gate_metadata() -> dict[str, Any]:
    signal = LearningSignal(
        title="One-off repair recipe",
        content="Run pytest once after editing a file.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=0.9,
        repeat_count=1,
        requested_target_layer=MemoryLayer.PROCEDURAL,
    )
    decision = NestedLearningKernel().decide(signal)
    payload = decision.to_payload()
    raw_requirements = payload.get("promotion_requirements", {})
    requirements = raw_requirements if isinstance(raw_requirements, dict) else {}
    return {
        "passed": (
            not decision.accepted
            and requirements.get("target_layer") == "procedural"
            and requirements.get("min_repeat_count") == 2
            and requirements.get("observed_repeat_count") == 1
        ),
        "tool_count": 0,
        "context_chars": len(json.dumps(payload)),
    }


def _eval_repo_map(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        execution = agent.tools.execute(
            ToolCall(name="repo.map", arguments={"max_entries": 80, "max_depth": 2}),
            _tool_context(agent),
        )
        return {
            "passed": execution.success and "pyproject.toml" in execution.content,
            "tool_count": 1,
        }
    finally:
        agent.close()


def _eval_patch_and_test(config: AgentConfig) -> dict[str, Any]:
    workspace = Path("/private/tmp") / f"kestrel-golden-{config.memory_dir.parent.name}-workspace-patch"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "calc.txt"
    target.write_text("bad\n", encoding="utf-8")
    agent = build_agent(replace(config, workspace=workspace, allow_file_write=True, allow_shell=True))
    patch = """diff --git a/calc.txt b/calc.txt
--- a/calc.txt
+++ b/calc.txt
@@ -1 +1 @@
-bad
+good
"""
    try:
        patch_call = ToolCall(name="patch.apply", arguments={"patch": patch})
        patch_result = agent.tools.execute(
            patch_call,
            _tool_context(
                agent,
                approved_tool_call_ids=frozenset({patch_call.id}),
                approved_tool_call_arguments={patch_call.id: patch_call.arguments},
            ),
        )
        test_call = ToolCall(
            name="test.run",
            arguments={
                "command": [
                    "python3",
                    "-c",
                    "from pathlib import Path; assert Path('calc.txt').read_text() == 'good\\n'",
                ]
            },
        )
        test_result = agent.tools.execute(
            test_call,
            _tool_context(
                agent,
                approved_tool_call_ids=frozenset({test_call.id}),
                approved_tool_call_arguments={test_call.id: test_call.arguments},
            ),
        )
        return {
            "passed": patch_result.success and test_result.success,
            "tool_count": 2,
            "patch_error": patch_result.error,
            "test_error": test_result.error,
        }
    finally:
        agent.close()


def _eval_honest_test_failure(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(replace(config, allow_shell=True))
    try:
        call = ToolCall(name="test.run", arguments={"command": ["python3", "-c", "import sys; sys.exit(4)"]})
        execution = agent.tools.execute(
            call,
            _tool_context(
                agent,
                approved_tool_call_ids=frozenset({call.id}),
                approved_tool_call_arguments={call.id: call.arguments},
            ),
        )
        return {
            "passed": not execution.success and execution.error == "nonzero_exit" and "exit_code=4" in execution.content,
            "tool_count": 1,
            "error": execution.error,
        }
    finally:
        agent.close()


def _eval_no_success_without_evidence(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        marker = f"{eval_id} unverified failed fix"
        agent.memory.put(
            MemoryRecord(
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.FAILURE,
                title=marker,
                content=f"{marker}: A failed test run must not become a trusted procedure.",
                confidence=0.8,
                importance=0.8,
            )
        )
        execution = agent.tools.execute(
            ToolCall(
                name="memory.consolidate",
                arguments={"query": marker, "source_layer": "episodic", "validation_score": 0.6, "repeat_count": 1},
            ),
            _tool_context(agent, session_id=eval_id),
        )
        procedural_hits = agent.memory.retrieve(
            RetrievalQuery(query=marker, layers=(MemoryLayer.PROCEDURAL,), k_per_layer=3)
        )
        return {
            "passed": execution.success and not procedural_hits,
            "tool_count": 1,
            "memory_hits": len(procedural_hits),
        }
    finally:
        agent.close()


def _eval_tool_call_accuracy(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        result = agent.chat("/search golden tool accuracy", session_id="golden_tool_accuracy")
        executed = [execution.call.name for execution in result.tool_executions]
        return {
            "passed": executed == ["memory.search"] and result.stop_reason == "complete",
            "tool_count": len(result.tool_executions),
            "context_chars": result.context_chars,
            "executed_tools": executed,
        }
    finally:
        agent.close()


def _eval_approval_correctness(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(replace(config, allow_shell=True))
    try:
        call = ToolCall(name="shell.run", arguments={"command": ["echo", "approval"]}, id="approval_eval")
        blocked = agent.tools.execute(call, _tool_context(agent))
        approved_wrong_args = agent.tools.execute(
            call,
            _tool_context(
                agent,
                approved_tool_call_ids=frozenset({call.id}),
                approved_tool_call_arguments={call.id: {"command": ["echo", "different"]}},
            ),
        )
        approved_exact = agent.tools.execute(
            call,
            _tool_context(
                agent,
                approved_tool_call_ids=frozenset({call.id}),
                approved_tool_call_arguments={call.id: call.arguments},
            ),
        )
        return {
            "passed": blocked.error == "approval_required"
            and approved_wrong_args.error == "approval_required"
            and approved_exact.success,
            "tool_count": 3,
            "blocked_error": blocked.error,
            "wrong_args_error": approved_wrong_args.error,
        }
    finally:
        agent.close()


def _eval_plan_completion(config: AgentConfig) -> dict[str, Any]:
    manager_config = replace(
        config,
        state_path=config.memory_dir.parent / "state.db",
        skills_dir=config.memory_dir.parent / "skills",
        plugins_dir=config.memory_dir.parent / "plugins",
    )
    state = AgentStateStore(manager_config.state_path)
    manager = RunManager(
        config=manager_config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(manager_config.skills_dir, state),
    )
    run = manager.create_run(message="/search Inspect and report plan completion", session_id="golden_plan")
    deadline = monotonic() + max(15.0, float(manager_config.timeout_seconds) + 15.0)
    final = manager.get_run(run.run_id)
    while monotonic() < deadline:
        final = manager.get_run(run.run_id)
        if str(final["status"]) in {"completed", "failed", "blocked"}:
            break
        sleep(0.05)
    graph = manager.task_graph(run.run_id)
    trace = manager.run_trace(run.run_id)
    root = graph["tasks"][0]
    return {
        "passed": final["status"] == "completed"
        and root["plan"]["graph_runtime"]["can_revise_plan"] is True
        and trace["summary"]["span_counts"].get("plan", 0) >= 1
        and trace["summary"]["span_counts"].get("review", 0) >= 1,
        "tool_count": int(final.get("tool_count", 0)),
        "plan_task_count": len(graph["tasks"]),
        "span_count": trace["summary"]["span_count"],
    }


def _eval_repo_regression_guard(config: AgentConfig) -> dict[str, Any]:
    workspace = Path("/private/tmp") / f"kestrel-golden-{config.memory_dir.parent.name}-workspace-regression"
    workspace.mkdir(parents=True, exist_ok=True)
    sentinel = workspace / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    before = {path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8") for path in workspace.rglob("*") if path.is_file()}
    agent = build_agent(replace(config, workspace=workspace))
    try:
        execution = agent.tools.execute(
            ToolCall(name="repo.map", arguments={"max_entries": 20, "max_depth": 2}),
            _tool_context(agent),
        )
        after = {path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8") for path in workspace.rglob("*") if path.is_file()}
        return {
            "passed": execution.success and before == after,
            "tool_count": 1,
            "mutated_files": sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key)),
        }
    finally:
        agent.close()


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    pass_count = sum(1 for item in results if item["passed"])
    fail_count = len(results) - pass_count
    latencies = [float(item.get("latency_ms", 0)) for item in results]
    context_sizes = [int(item.get("context_chars", 0)) for item in results]
    tool_counts = [int(item.get("tool_count", 0)) for item in results]
    return {
        "pass_count": pass_count,
        "fail_count": fail_count,
        "latency_ms_max": max(latencies) if latencies else 0,
        "context_chars_max": max(context_sizes) if context_sizes else 0,
        "tool_count_total": sum(tool_counts),
        "cost_estimate_usd_total": round(sum(float(item.get("cost_estimate_usd", 0.0)) for item in results), 6),
        "categories": _category_summary(results),
        "promotion_precision": None,
        "false_promotion_count": sum(1 for item in results if item["name"] == "no_success_claim_without_evidence" and not item["passed"]),
    }


def _category_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, dict[str, Any]] = {}
    for category in _REQUIRED_CATEGORIES:
        categories[category] = {
            "case_count": 0,
            "pass_count": 0,  # nosec B105
            "fail_count": 0,
            "score": None,
        }
    for item in results:
        category = str(item.get("category", "uncategorized"))
        bucket = categories.setdefault(category, {"case_count": 0, "pass_count": 0, "fail_count": 0, "score": None})  # nosec B105
        bucket["case_count"] = int(bucket["case_count"]) + 1
        if item["passed"]:
            bucket["pass_count"] = int(bucket["pass_count"]) + 1
        else:
            bucket["fail_count"] = int(bucket["fail_count"]) + 1
    for bucket in categories.values():
        case_count = int(bucket["case_count"])
        bucket["score"] = None if case_count == 0 else round(int(bucket["pass_count"]) / case_count, 4)
    latency_score = 1.0 if not results else round(max(0.0, min(1.0, 1000.0 / max(float(item.get("latency_ms", 0.1)) for item in results))), 4)
    categories["latency"] = {
        "case_count": len(results),
        "pass_count": sum(1 for item in results if float(item.get("latency_ms", 0)) <= 1000),
        "fail_count": sum(1 for item in results if float(item.get("latency_ms", 0)) > 1000),
        "score": latency_score,
        "latency_ms_max": max(float(item.get("latency_ms", 0)) for item in results) if results else 0,
    }
    categories["cost"] = {
        "case_count": len(results),
        "pass_count": len(results),
        "fail_count": 0,
        "score": 1.0,
        "cost_estimate_usd_total": round(sum(float(item.get("cost_estimate_usd", 0.0)) for item in results), 6),
    }
    return categories


def _tool_context(
    agent: Any,
    session_id: str = "golden",
    *,
    approved_tool_call_ids: frozenset[str] = frozenset(),
    approved_tool_call_arguments: dict[str, dict[str, Any]] | None = None,
) -> ToolContext:
    return ToolContext(
        memory=agent.memory,
        config=agent.config,
        workspace=agent.config.workspace,
        event_log=agent.event_log,
        session_id=session_id,
        approved_tool_call_ids=approved_tool_call_ids,
        approved_tool_call_arguments=approved_tool_call_arguments,
    )


if __name__ == "__main__":
    main()
