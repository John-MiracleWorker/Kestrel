from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.nested_learning import NestedLearningKernel
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.task_capsule import summarize_run_capsule, write_run_capsule
from nested_memvid_agent.tools.base import ToolContext


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["memory", "memvid"], default="memory")
    parser.add_argument("--memory-dir", type=Path, default=Path("./tmp-golden/memory"))
    parser.add_argument("--provider", choices=["mock", "openai"], default="mock")
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
        "passed": passed,
        "latency_ms": round((perf_counter() - started) * 1000, 2),
        "memory_hits": int(result.pop("memory_hits", 0)),
        "context_chars": int(result.pop("context_chars", 0)),
        "tool_count": int(result.pop("tool_count", 0)),
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
        return {"passed": not execution.success and execution.error == "approval_required", "tool_count": 1}
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
        patch_result = agent.tools.execute(ToolCall(name="patch.apply", arguments={"patch": patch}), _tool_context(agent))
        test_result = agent.tools.execute(
            ToolCall(
                name="test.run",
                arguments={
                    "command": [
                        "python3",
                        "-c",
                        "from pathlib import Path; assert Path('calc.txt').read_text() == 'good\\n'",
                    ]
                },
            ),
            _tool_context(agent),
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
        execution = agent.tools.execute(
            ToolCall(name="test.run", arguments={"command": ["python3", "-c", "import sys; sys.exit(4)"]}),
            _tool_context(agent),
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
        "promotion_precision": None,
        "false_promotion_count": sum(1 for item in results if item["name"] == "no_success_claim_without_evidence" and not item["passed"]),
    }


def _tool_context(agent: Any, session_id: str = "golden") -> ToolContext:
    return ToolContext(
        memory=agent.memory,
        config=agent.config,
        workspace=agent.config.workspace,
        event_log=agent.event_log,
        session_id=session_id,
    )


if __name__ == "__main__":
    main()
