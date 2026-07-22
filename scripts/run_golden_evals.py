from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - fixed local git fixture commands only
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
from nested_memvid_agent.context_compiler import ContextCompiler, ContextCompilerConfig
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS
from nested_memvid_agent.llm.model_catalog import PROVIDER_OPTIONS
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.security_boundary import redact_secrets
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.task_capsule import write_run_capsule
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


def main() -> int:
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
    parser.add_argument(
        "--validation-container-image",
        default=os.getenv("NEST_AGENT_VALIDATION_CONTAINER_IMAGE"),
        help=(
            "Preloaded digest-pinned OCI image used by repair and post-repair "
            "validation; host fallback is never used."
        ),
    )
    parser.add_argument(
        "--max-case-latency-ms",
        type=float,
        help=(
            "Optional fail-closed maximum wall-clock latency for every golden case. "
            "Use a backend/environment-specific threshold."
        ),
    )
    args = parser.parse_args()
    if args.max_case_latency_ms is not None and args.max_case_latency_ms <= 0:
        parser.error("--max-case-latency-ms must be greater than 0")

    config = AgentConfig(
        backend=args.backend,
        memory_dir=args.memory_dir,
        provider=args.provider,
        model=args.model,
        workspace=args.workspace,
        log_dir=args.memory_dir.parent / "logs",
        validation_container_image=args.validation_container_image,
    )
    eval_id = f"golden_{uuid4().hex}"
    results = [
        _run_case(
            "remember_correction_across_sessions",
            lambda: _eval_correction_persists(_case_config(config, eval_id, "correction"), eval_id),
        ),
        _run_case(
            "retrieve_prior_failure",
            lambda: _eval_prior_failure_context(
                _case_config(config, eval_id, "prior_failure"), eval_id
            ),
        ),
        _run_case(
            "use_procedural_recipe_after_repeats",
            lambda: _eval_procedural_promotion(_case_config(config, eval_id, "procedure"), eval_id),
        ),
        _run_case(
            "refuse_path_escape",
            lambda: _eval_path_escape(_case_config(config, eval_id, "path_escape")),
        ),
        _run_case(
            "block_shell_without_enablement",
            lambda: _eval_shell_block(_case_config(config, eval_id, "shell")),
        ),
        _run_case(
            "verify_mv2_files", lambda: _eval_verify_memory(_case_config(config, eval_id, "verify"))
        ),
        _run_case(
            "compile_context_under_budget",
            lambda: _eval_context_budget(_case_config(config, eval_id, "context_budget"), eval_id),
        ),
        _run_case(
            "summary_first_expand_raw_on_demand",
            lambda: _eval_summary_first_expand_raw(
                _case_config(config, eval_id, "summary_expand"), eval_id
            ),
        ),
        _run_case(
            "flag_conflicting_facts",
            lambda: _eval_conflict_warning(_case_config(config, eval_id, "conflicts"), eval_id),
        ),
        _run_case(
            "create_capsule_and_consolidate_validated_lessons",
            lambda: _eval_task_capsule_consolidation(
                _case_config(config, eval_id, "capsule"), eval_id
            ),
        ),
        _run_case(
            "mv2_not_sqlite_or_vector_db_substrate",
            lambda: _eval_mv2_substrate_contract(_case_config(config, eval_id, "substrate")),
        ),
        _run_case(
            "avoid_policy_from_ordinary_event",
            lambda: _eval_no_policy_from_event(
                _case_config(config, eval_id, "ordinary_event"), eval_id
            ),
        ),
        _run_case(
            "explain_memory_promotion_gates",
            lambda: _eval_memory_promotion_gate_metadata(
                _case_config(config, eval_id, "promotion_gates"), eval_id
            ),
        ),
        _run_case(
            "map_repository", lambda: _eval_repo_map(_case_config(config, eval_id, "repo_map"))
        ),
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
            lambda: _eval_no_success_without_evidence(
                _case_config(config, eval_id, "no_evidence"), eval_id
            ),
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
    summary = _summary(results, max_case_latency_ms=args.max_case_latency_ms)
    functional_passed = summary["fail_count"] == 0
    latency_gate = dict(summary["acceptance"]["latency"])
    passed = _aggregate_passed(summary)
    report = {
        "schema": "kestrel.golden_eval_report.v2",
        "configuration": {
            "backend": args.backend,
            "provider": args.provider,
            "model": args.model,
            "max_case_latency_ms": args.max_case_latency_ms,
        },
        "results": results,
        "summary": summary,
        "acceptance": {
            "functional": {"required": True, "passed": functional_passed},
            "latency": latency_gate,
            "cost": dict(summary["acceptance"]["cost"]),
        },
        "passed": passed,
    }
    print(json.dumps(report, indent=2))
    return _report_exit_code(report)


def _report_exit_code(report: dict[str, Any]) -> int:
    """Fail closed when the emitted aggregate is not an explicit pass."""

    return 0 if report.get("passed") is True else 1


def _aggregate_passed(summary: dict[str, Any]) -> bool:
    """Require functional success plus every configured performance gate."""

    acceptance = summary.get("acceptance")
    if not isinstance(acceptance, dict):
        return False
    latency_gate = acceptance.get("latency")
    if not isinstance(latency_gate, dict):
        return False
    configured = latency_gate.get("gate_configured")
    latency_passed = latency_gate.get("passed")
    if configured is True:
        performance_accepted = latency_passed is True
    elif configured is False:
        performance_accepted = latency_passed is None
    else:
        performance_accepted = False
    return summary.get("fail_count") == 0 and performance_accepted


def _run_case(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = perf_counter()
    cost_estimate_usd: float | None = None
    try:
        result = fn()
        passed = bool(result.pop("passed"))
        raw_cost = result.pop("cost_estimate_usd", None)
        if raw_cost is not None:
            cost_estimate_usd = float(raw_cost)
    except Exception as exc:  # noqa: BLE001 - eval harness reports failure data
        result = {"error": redact_secrets(f"{type(exc).__name__}: {exc}")}
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
        "cost_estimate_usd": cost_estimate_usd,
        **result,
    }


def _case_config(config: AgentConfig, eval_id: str, case_name: str) -> AgentConfig:
    memory_dir = config.memory_dir / eval_id / case_name
    case_root = memory_dir.parent
    isolation_root = case_root / "isolated-runtime" / case_name
    return replace(
        config,
        memory_dir=memory_dir,
        log_dir=case_root / "logs" / case_name,
        state_path=case_root / "state" / case_name / "agent.db",
        secret_store_path=isolation_root / "secrets" / "local_vault.json",
        skills_dir=isolation_root / "skills",
        plugins_dir=isolation_root / "plugins",
        mcp_config_path=isolation_root / "config" / "mcp_servers.json",
        channel_config_path=isolation_root / "config" / "channels.json",
        worker_worktree_dir=isolation_root / "worktrees",
    )


def _eval_correction_persists(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        turn = agent.chat(
            f"Remember: {eval_id} user correction says concise answers are preferred.",
            session_id=eval_id,
        )
    finally:
        agent.close()

    reopened = build_agent(config)
    try:
        hits = reopened.memory.retrieve(
            RetrievalQuery(query=f"{eval_id} concise answers", k_per_layer=3)
        )
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
    validation_workspace = config.memory_dir.parent / "validation-workspace"
    _prepare_validation_workspace(validation_workspace)
    config = replace(
        config,
        workspace=validation_workspace,
        allow_shell=True,
        allow_file_write=True,
        tool_retry_max_attempts=0,
    )
    agent = build_agent(config)
    try:
        marker = f"{eval_id} pytest recipe"
        candidate_id = agent.memory.put(
            MemoryRecord(
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.PROCEDURE,
                title=marker,
                content=f"{marker}: After editing tool code, run pytest -q and ruff check.",
                confidence=0.9,
                importance=0.85,
                metadata={"session_id": eval_id, "run_id": None},
            )
        )
        agent.memory.seal_all()
        evidence, validation_tools = _run_claim_bound_validation(
            agent,
            candidate_id=candidate_id,
            session_id=eval_id,
        )
        execution = agent.tools.execute(
            ToolCall(
                name="memory.consolidate",
                arguments={
                    "query": marker,
                    "source_layer": "episodic",
                    "source_record_id": candidate_id,
                    "validation_evidence": evidence,
                    # This mirrors the two independently authenticated task
                    # receipts above; the sink still derives/enforces the
                    # effective repeat count from those receipts.
                    "repeat_count": 2,
                },
            ),
            _tool_context(agent, session_id=eval_id),
        )
        hits = agent.memory.retrieve(
            RetrievalQuery(query=marker, layers=(MemoryLayer.PROCEDURAL,), k_per_layer=3)
        )
        return {
            "passed": execution.success
            and execution.data.get("promoted") is True
            and execution.data.get("target_layer") == "procedural"
            and execution.data.get("validation_evidence", {}).get("resolved") is True
            and bool(hits),
            "memory_hits": len(hits),
            "tool_count": len(validation_tools) + 1,
            "validation_tools": validation_tools,
            "error": execution.error,
            "error_detail": execution.content if not execution.success else None,
        }
    finally:
        agent.close()


def _prepare_validation_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    sentinel = workspace / "validated_change.txt"
    sentinel.write_text("baseline\n", encoding="utf-8")
    commands = (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "golden-eval@invalid.local"),
        ("git", "config", "user.name", "Kestrel Golden Eval"),
        ("git", "add", "validated_change.txt"),
        ("git", "commit", "-q", "-m", "baseline"),
        ("git", "checkout", "-q", "-b", "kestrel/worker/golden-validation"),
    )
    for command in commands:
        subprocess.run(  # noqa: S603  # nosec B603 - fixed local git fixture commands
            command,
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
    sentinel.write_text("validated candidate\n", encoding="utf-8")


def _run_claim_bound_validation(
    agent: Any,
    *,
    candidate_id: str,
    session_id: str,
) -> tuple[dict[str, object], list[str]]:
    executions: list[tuple[str, Any]] = []

    def execute(name: str, arguments: dict[str, Any], suffix: str) -> Any:
        call = ToolCall(name=name, arguments=arguments, id=f"golden_{suffix}_{uuid4().hex}")
        result = agent.tools.execute(
            call,
            _tool_context(
                agent,
                session_id=session_id,
                approved_tool_call_ids=frozenset({call.id}),
                approved_tool_call_arguments={call.id: arguments},
            ),
        )
        if not result.success:
            raise RuntimeError(f"{name} validation failed: {result.error}: {result.content}")
        executions.append((name, result))
        return result

    test_one = execute(
        "test.run",
        {
            "command": ["python3", "-c", "print('golden test receipt one')"],
            "subject_record_id": candidate_id,
        },
        "test_one",
    )
    lint = execute(
        "lint.run",
        {
            "command": ["python3", "-c", "print('golden lint receipt')"],
            "subject_record_id": candidate_id,
        },
        "lint",
    )
    repair = execute(
        "repair.validate",
        {
            "command": ["python3", "-c", "print('golden repair receipt')"],
            "subject_record_id": candidate_id,
        },
        "repair",
    )
    validation_id = str(repair.data.get("validation_id") or "")
    review = execute(
        "repair.review",
        {
            "validation_id": validation_id,
            "summary": "Golden evaluation reviewed the exact validated fixture diff.",
            "subject_record_id": candidate_id,
        },
        "review",
    )
    test_two = execute(
        "test.run",
        {
            "command": ["python3", "-c", "print('golden test receipt two')"],
            "subject_record_id": candidate_id,
        },
        "test_two",
    )

    test_one_ref = _validation_receipt_ref(test_one, "validation_evidence", "test_refs")
    test_two_ref = _validation_receipt_ref(test_two, "validation_evidence", "test_refs")
    evidence: dict[str, object] = {
        "test_refs": [test_one_ref],
        "lint_refs": [_validation_receipt_ref(lint, "validation_evidence", "lint_refs")],
        "repair_refs": [_validation_receipt_ref(repair, "validation_evidence", "repair_refs")],
        "review_refs": [
            _validation_receipt_ref(review, "runtime_validation_evidence", "review_refs")
        ],
        "task_refs": [test_one_ref, test_two_ref],
    }
    return evidence, [name for name, _ in executions]


def _validation_receipt_ref(
    execution: Any,
    payload_key: str,
    refs_key: str,
) -> dict[str, str]:
    payload = execution.data.get(payload_key)
    refs = payload.get(refs_key) if isinstance(payload, dict) else None
    ref = refs[0] if isinstance(refs, list) and refs else None
    if not isinstance(ref, dict):
        raise RuntimeError(f"Validation result did not contain {payload_key}.{refs_key}.")
    source = str(ref.get("source") or "")
    locator = str(ref.get("locator") or "")
    if source != "memory_record" or not locator:
        raise RuntimeError(f"Validation result contained an unauthenticated {refs_key} ref.")
    return {"source": source, "locator": locator}


def _eval_path_escape(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        execution = agent.tools.execute(
            ToolCall(name="file.read", arguments={"path": "../outside.txt"}),
            _tool_context(agent),
        )
        return {
            "passed": not execution.success and execution.error == "file_read_failed",
            "tool_count": 1,
        }
    finally:
        agent.close()


def _eval_shell_block(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        execution = agent.tools.execute(
            ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}),
            _tool_context(agent),
        )
        return {
            "passed": not execution.success and execution.error == "tool_disabled",
            "tool_count": 1,
        }
    finally:
        agent.close()


def _eval_verify_memory(config: AgentConfig) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        agent.memory.seal_all()
        execution = agent.tools.execute(
            ToolCall(name="memvid.verify", arguments={}), _tool_context(agent)
        )
        return {"passed": execution.success, "tool_count": 1}
    finally:
        agent.close()


def _eval_context_budget(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    # This case evaluates packing, not promotion.  Build an explicitly
    # integrity-disabled fixture store so the setup cannot be mistaken for a
    # supported stable-memory write path.
    memory = build_memory_system(
        config.backend,
        config.memory_dir,
        max_file_bytes=config.memory_max_layer_bytes,
        enforce_stable_write_integrity=False,
    )
    try:
        marker = f"{eval_id} context budget fact"
        memory.put(
            MemoryRecord(
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                title=marker,
                content=f"{marker}: The context compiler should retrieve useful memory without full transcript stuffing.",
                confidence=0.85,
                importance=0.7,
            )
        )
        memory.seal_all()
        compiler = ContextCompiler(
            memory,
            config=ContextCompilerConfig(
                total_budget_chars=config.context_budget_chars,
                context_pack_token_budget=config.context_pack_token_budget,
            ),
        )
        compiled = compiler.compile(objective="Check context budget behavior.", query=marker)
        return {
            "passed": marker in compiled.prompt
            and compiled.total_chars <= config.context_budget_chars,
            "memory_hits": len(compiled.hits),
            "context_chars": compiled.total_chars,
            "fixture_mode": "integrity_disabled_packing_only",
        }
    finally:
        memory.close_all()


def _eval_summary_first_expand_raw(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    agent = build_agent(config)
    try:
        marker = f"{eval_id} summary expand"
        summary_id = f"summary_{uuid4().hex}"
        raw_id = f"raw_{uuid4().hex}"
        raw_payload = (
            f"{raw_id}: Raw exact evidence includes verbose logs and complete command output."
        )
        summary_title = f"{marker} summary"
        agent.memory.put(
            MemoryRecord(
                id=summary_id,
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                title=summary_title,
                content=f"{marker}: Summary says the fix is to pack summaries before raw evidence.",
                confidence=0.8,
                importance=0.8,
                metadata={
                    "frame_type": "task_summary",
                    "frame_id": summary_id,
                    "child_ids": [raw_id],
                },
            )
        )
        agent.memory.put(
            MemoryRecord(
                id=raw_id,
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.EVENT,
                title="Supporting raw evidence",
                content=raw_payload,
                confidence=0.8,
                importance=0.7,
                metadata={
                    "frame_type": "raw_chunk",
                    "frame_id": raw_id,
                    "parent_ids": [summary_id],
                },
            )
        )
        compact = ContextPacker(agent.memory).pack(
            ContextPackRequest(objective=marker, query=marker, expand_raw=False)
        )
        expanded = ContextPacker(agent.memory).pack(
            ContextPackRequest(objective=marker, query=marker, expand_raw=True)
        )
        compact_titles = {item.frame.title for item in compact.items}
        expanded_summary = next(
            (item for item in expanded.items if item.frame.id == summary_id),
            None,
        )
        return {
            "passed": summary_title in compact_titles
            and raw_payload not in compact.prompt
            and raw_payload in expanded.prompt
            and expanded_summary is not None
            and expanded_summary.reason == "expanded_child_frames",
            "memory_hits": len(expanded.items),
            "context_chars": len(expanded.prompt),
        }
    finally:
        agent.close()


def _eval_conflict_warning(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    # Conflict rendering is a packing-only contract.  The fixture deliberately
    # bypasses promotion integrity while the production write paths remain
    # covered by dedicated promotion cases.
    memory = build_memory_system(
        config.backend,
        config.memory_dir,
        max_file_bytes=config.memory_max_layer_bytes,
        enforce_stable_write_integrity=False,
    )
    try:
        marker = f"{eval_id} conflict"
        for title, content in [
            (f"{marker} enabled", f"{marker}: Feature gamma is enabled."),
            (f"{marker} disabled", f"{marker}: Feature gamma is not enabled."),
        ]:
            memory.put(
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
        memory.seal_all()
        packed = ContextPacker(memory).pack(ContextPackRequest(objective=marker, query=marker))
        return {
            "passed": bool(packed.conflict_warnings),
            "memory_hits": len(packed.items),
            "context_chars": len(packed.prompt),
            "warnings": list(packed.conflict_warnings),
            "fixture_mode": "integrity_disabled_packing_only",
        }
    finally:
        memory.close_all()


def _eval_task_capsule_consolidation(config: AgentConfig, eval_id: str) -> dict[str, Any]:
    config = replace(
        config,
        enable_auto_consolidation=True,
        allow_policy_writes=True,
    )
    agent = build_agent(config)
    try:
        runs_dir = config.memory_dir.parent / "runs"
        write_run_capsule(
            runs_dir=runs_dir,
            run_id=eval_id,
            objective="Create a run-scoped complete.mv2 capsule.",
            backend=config.backend,
            final_response="Capsule created.",
            candidate_facts=(f"{eval_id}: complete.mv2 is a run artifact, not a permanent layer.",),
            candidate_policy_items=(
                f"{eval_id}: Ordinary run policy candidate still needs human review.",
            ),
        )
        arguments = {"run_id": eval_id, "include_policy": True}
        call = ToolCall(name="capsule.apply", arguments=arguments, id=f"capsule_apply_{eval_id}")
        execution = agent.tools.execute(
            call,
            _tool_context(
                agent,
                session_id=eval_id,
                approved_tool_call_ids=frozenset({call.id}),
                approved_tool_call_arguments={call.id: arguments},
            ),
        )
        payload = execution.data if isinstance(execution.data, dict) else {}
        decisions = payload.get("decisions")
        decision_rows = decisions if isinstance(decisions, list) else []
        fact_decision = next(
            (
                row
                for row in decision_rows
                if isinstance(row, dict) and row.get("signal_kind") == "fact"
            ),
            {},
        )
        policy_decision = next(
            (
                row
                for row in decision_rows
                if isinstance(row, dict) and row.get("requested_target_layer") == "policy"
            ),
            {},
        )
        episodic_hits = agent.memory.retrieve(
            RetrievalQuery(
                query=f"{eval_id} complete.mv2 run artifact",
                layers=(MemoryLayer.EPISODIC,),
                k_per_layer=3,
            )
        )
        semantic_hits = agent.memory.retrieve(
            RetrievalQuery(
                query=f"{eval_id} complete.mv2 run artifact",
                layers=(MemoryLayer.SEMANTIC,),
                k_per_layer=3,
            )
        )
        policy_hits = agent.memory.retrieve(
            RetrievalQuery(
                query=f"{eval_id} Ordinary run policy candidate",
                layers=(MemoryLayer.POLICY,),
                k_per_layer=3,
            )
        )
        staged = episodic_hits[0].record if episodic_hits else None
        return {
            "passed": execution.success
            and payload.get("capsule_path", "").endswith("complete.mv2")
            and fact_decision.get("write_mode") == "unvalidated_episodic_staging"
            and fact_decision.get("actual_layer") == "episodic"
            and fact_decision.get("stable_promotion_blocked") == "authenticated_validation_required"
            and policy_decision.get("blocked") == "policy_requires_explicit_instruction"
            and staged is not None
            and staged.metadata.get("capsule_apply_status") == "unvalidated_episodic_staging"
            and staged.metadata.get("stable_recall_eligible") is False
            and not semantic_hits
            and not policy_hits,
            "memory_hits": len(episodic_hits),
            "tool_count": 1,
            "staging_status": None
            if staged is None
            else staged.metadata.get("capsule_apply_status"),
            "policy_block": policy_decision.get("blocked"),
            "error": execution.error,
            "error_detail": execution.content if not execution.success else None,
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
        payload = (
            json.loads(execution.content)
            if execution.success and execution.content.startswith("{")
            else {}
        )
        return {
            "passed": execution.success and payload.get("target_layer") != "policy",
            "memory_hits": 1 if payload else 0,
            "tool_count": 1,
        }
    finally:
        agent.close()


def _eval_memory_promotion_gate_metadata(
    config: AgentConfig,
    eval_id: str,
) -> dict[str, Any]:
    config = replace(config, allow_policy_writes=True, tool_retry_max_attempts=0)
    agent = build_agent(config)
    title = f"{eval_id} two-phase policy proposal"
    content = "Policy promotion must validate the exact staged claim before a stable write."
    try:
        stage_arguments: dict[str, Any] = {
            "title": title,
            "content": content,
            "confidence": 0.99,
            "stage_proposal": True,
        }
        stage, stage_approval_id = _execute_approved_policy_call(
            agent,
            arguments=stage_arguments,
            session_id=eval_id,
            run_id=eval_id,
            call_id=f"golden_policy_stage_{uuid4().hex}",
        )
        proposal_id = str(stage.data.get("proposal_id") or "")
        next_action = str(stage.data.get("next_action") or "")

        promote_arguments: dict[str, Any] = {
            "title": title,
            "content": content,
            "source_record_id": proposal_id,
            "confidence": 0.99,
        }
        promote, promote_approval_id = _execute_approved_policy_call(
            agent,
            arguments=promote_arguments,
            session_id=eval_id,
            run_id=eval_id,
            call_id=f"golden_policy_promote_{uuid4().hex}",
        )
        proposal = agent.memory.get_record(
            MemoryLayer.EPISODIC,
            proposal_id,
            include_inactive=False,
        )
        policy_hits = agent.memory.retrieve(
            RetrievalQuery(query=title, layers=(MemoryLayer.POLICY,), k_per_layer=3)
        )
        return {
            "passed": stage.success
            and bool(proposal_id)
            and proposal is not None
            and proposal.metadata.get("policy_promotion_candidate") is True
            and proposal.metadata.get("proposal_approval_id") == stage_approval_id
            and "subject_record_id set to proposal_id" in next_action
            and "separately approved" in next_action
            and stage_approval_id != promote_approval_id
            and not promote.success
            and promote.error == "policy_evidence_invalid"
            and not policy_hits,
            "tool_count": 2,
            "memory_hits": 1 if proposal is not None else 0,
            "phase_one": "approved_exact_call_staged_claim",
            "phase_two_gate": promote.error,
            "policy_writes": len(policy_hits),
        }
    finally:
        agent.close()


def _execute_approved_policy_call(
    agent: Any,
    *,
    arguments: dict[str, Any],
    session_id: str,
    run_id: str,
    call_id: str,
) -> tuple[Any, str]:
    state = AgentStateStore(agent.config.state_path)
    approval = state.create_approval(
        approval_id=f"approval_{call_id}",
        run_id=run_id,
        tool_call_id=call_id,
        tool_name="memory.policy_promote",
        arguments=arguments,
        risk="high",
    )
    approval, applied = state.decide_approval_once(
        str(approval["approval_id"]),
        status="approved",
        decision={
            "approved": True,
            "arguments": arguments,
            "principal": "owner",
        },
        principal="owner",
    )
    if not applied:
        raise RuntimeError("Golden policy approval could not be durably applied.")
    call = ToolCall(name="memory.policy_promote", arguments=arguments, id=call_id)
    execution = agent.tools.execute(
        call,
        _tool_context(
            agent,
            session_id=session_id,
            run_id=run_id,
            approved_tool_call_ids=frozenset({call_id}),
            approved_tool_call_arguments={call_id: arguments},
            approval_receipts={call_id: approval},
        ),
    )
    state.record_approval_result(
        str(approval["approval_id"]),
        {
            "tool": execution.call.name,
            "tool_call_id": execution.call.id,
            "arguments": execution.call.arguments,
            "success": execution.success,
            "content": execution.content,
            "data": execution.data,
            "error": execution.error,
        },
    )
    return execution, str(approval["approval_id"])


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


def _golden_case_workspace(config: AgentConfig, label: str) -> Path:
    """Return a case-local workspace on every supported operating system."""

    workspace = config.memory_dir.parent / "workspaces" / f"{config.memory_dir.name}-{label}"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _eval_patch_and_test(config: AgentConfig) -> dict[str, Any]:
    workspace = _golden_case_workspace(config, "patch")
    target = workspace / "calc.txt"
    target.write_text("bad\n", encoding="utf-8")
    _prepare_validation_workspace(workspace)
    agent = build_agent(
        replace(config, workspace=workspace, allow_file_write=True, allow_shell=True)
    )
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
    # This is a test-runner contract, not a scan of the caller's repository.
    # Keep the synthetic failing command in its own clean workspace so ignored
    # operator secrets correctly present in the real checkout do not make the
    # deterministic golden gate environment-dependent.
    workspace = config.memory_dir.parent / "test-failure-workspace"
    _prepare_validation_workspace(workspace)
    agent = build_agent(replace(config, workspace=workspace, allow_shell=True))
    try:
        call = ToolCall(
            name="test.run", arguments={"command": ["python3", "-c", "import sys; sys.exit(4)"]}
        )
        execution = agent.tools.execute(
            call,
            _tool_context(
                agent,
                approved_tool_call_ids=frozenset({call.id}),
                approved_tool_call_arguments={call.id: call.arguments},
            ),
        )
        return {
            "passed": not execution.success
            and execution.error == "nonzero_exit"
            and "exit_code=4" in execution.content,
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
                arguments={
                    "query": marker,
                    "source_layer": "episodic",
                    "validation_score": 0.6,
                    "repeat_count": 1,
                },
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
        call = ToolCall(
            name="shell.run", arguments={"command": ["echo", "approval"]}, id="approval_eval"
        )
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
    run = manager.create_run(
        message="/search Inspect and report plan completion", session_id="golden_plan"
    )
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
    graph_contract = root["plan"]["graph_runtime"]
    review = (root.get("result") or {}).get("orchestration_review") or {}
    artifact = review.get("artifact") or {}
    return {
        "passed": final["status"] == "completed"
        and root["status"] == "completed"
        and graph_contract["can_revise_plan"] is False
        and graph_contract["can_rewrite_task_dag"] is False
        and root["plan"]["semantic_plan"]["source"] == "deterministic_task_graph"
        and artifact.get("decision") == "pass"
        and bool(artifact.get("evidence"))
        and trace["summary"]["span_counts"].get("plan", 0) >= 1
        and trace["summary"]["span_counts"].get("review", 0) >= 1,
        "tool_count": int(final.get("tool_count", 0)),
        "plan_task_count": len(graph["tasks"]),
        "span_count": trace["summary"]["span_count"],
        "semantic_plan_source": root["plan"]["semantic_plan"]["source"],
        "review_decision": artifact.get("decision"),
    }


def _eval_repo_regression_guard(config: AgentConfig) -> dict[str, Any]:
    workspace = _golden_case_workspace(config, "regression")
    sentinel = workspace / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    before = {
        path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
        for path in workspace.rglob("*")
        if path.is_file()
    }
    agent = build_agent(replace(config, workspace=workspace))
    try:
        execution = agent.tools.execute(
            ToolCall(name="repo.map", arguments={"max_entries": 20, "max_depth": 2}),
            _tool_context(agent),
        )
        after = {
            path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
            for path in workspace.rglob("*")
            if path.is_file()
        }
        return {
            "passed": execution.success and before == after,
            "tool_count": 1,
            "mutated_files": sorted(
                key for key in set(before) | set(after) if before.get(key) != after.get(key)
            ),
        }
    finally:
        agent.close()


def _summary(
    results: list[dict[str, Any]],
    *,
    max_case_latency_ms: float | None = None,
) -> dict[str, Any]:
    pass_count = sum(1 for item in results if item["passed"])
    fail_count = len(results) - pass_count
    latencies = [float(item.get("latency_ms", 0)) for item in results]
    context_sizes = [int(item.get("context_chars", 0)) for item in results]
    tool_counts = [int(item.get("tool_count", 0)) for item in results]
    cost_measurement = _cost_measurement(results)
    latency_acceptance = _latency_acceptance(
        latencies,
        max_case_latency_ms=max_case_latency_ms,
    )
    return {
        "pass_count": pass_count,
        "fail_count": fail_count,
        "latency_ms_max": max(latencies) if latencies else None,
        "context_chars_max": max(context_sizes) if context_sizes else 0,
        "tool_count_total": sum(tool_counts),
        "cost_estimate_usd_total": cost_measurement["cost_estimate_usd_total"],
        "categories": _category_summary(
            results,
            max_case_latency_ms=max_case_latency_ms,
        ),
        "acceptance": {
            "latency": latency_acceptance,
            "cost": cost_measurement,
        },
        "promotion_precision": None,
        "false_promotion_count": sum(
            1
            for item in results
            if item["name"] == "no_success_claim_without_evidence" and not item["passed"]
        ),
    }


def _category_summary(
    results: list[dict[str, Any]],
    *,
    max_case_latency_ms: float | None = None,
) -> dict[str, Any]:
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
        bucket = categories.setdefault(
            category,
            {"case_count": 0, "pass_count": 0, "fail_count": 0, "score": None},  # nosec B105
        )
        bucket["case_count"] = int(bucket["case_count"]) + 1
        if item["passed"]:
            bucket["pass_count"] = int(bucket["pass_count"]) + 1
        else:
            bucket["fail_count"] = int(bucket["fail_count"]) + 1
    for bucket in categories.values():
        case_count = int(bucket["case_count"])
        bucket["score"] = (
            None if case_count == 0 else round(int(bucket["pass_count"]) / case_count, 4)
        )
    latencies = [float(item.get("latency_ms", 0.0)) for item in results]
    latency_acceptance = _latency_acceptance(
        latencies,
        max_case_latency_ms=max_case_latency_ms,
    )
    latency_pass_count = (
        None
        if max_case_latency_ms is None
        else sum(1 for latency in latencies if latency <= max_case_latency_ms)
    )
    latency_fail_count = (
        None
        if latency_pass_count is None
        else len(latencies) - latency_pass_count
    )
    categories["latency"] = {
        "case_count": len(results),
        "pass_count": latency_pass_count,
        "fail_count": latency_fail_count,
        "score": (
            None
            if latency_pass_count is None or not latencies
            else round(latency_pass_count / len(latencies), 4)
        ),
        **latency_acceptance,
    }
    cost_measurement = _cost_measurement(results)
    categories["cost"] = {
        "case_count": len(results),
        "pass_count": None,
        "fail_count": None,
        "score": None,
        **cost_measurement,
    }
    return categories


def _latency_acceptance(
    latencies: list[float],
    *,
    max_case_latency_ms: float | None,
) -> dict[str, Any]:
    observed_max = max(latencies) if latencies else None
    configured = max_case_latency_ms is not None
    if max_case_latency_ms is None:
        passed = None
    else:
        passed = observed_max is not None and observed_max <= max_case_latency_ms
    return {
        "measurement_status": "measured" if latencies else "unmeasured",
        "gate_configured": configured,
        "required": configured,
        "threshold_max_case_latency_ms": max_case_latency_ms,
        "latency_ms_max": observed_max,
        "passed": passed,
    }


def _cost_measurement(results: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [
        float(value)
        for item in results
        if (value := item.get("cost_estimate_usd")) is not None
    ]
    measured_count = len(measured)
    if measured_count == 0:
        status = "unmeasured"
    elif measured_count == len(results):
        status = "measured"
    else:
        status = "partially_measured"
    return {
        "measurement_status": status,
        "gate_configured": False,
        "required": False,
        "measured_case_count": measured_count,
        "unmeasured_case_count": len(results) - measured_count,
        "cost_estimate_usd_total": round(sum(measured), 6) if measured else None,
        "passed": None,
        "residual": (
            "Provider usage and pricing were not supplied for every golden case; "
            "cost is not an acceptance gate."
            if status != "measured"
            else "Cost is measured but no budget threshold is configured."
        ),
    }


def _tool_context(
    agent: Any,
    session_id: str = "golden",
    *,
    run_id: str | None = None,
    approved_tool_call_ids: frozenset[str] = frozenset(),
    approved_tool_call_arguments: dict[str, dict[str, Any]] | None = None,
    approval_receipts: dict[str, dict[str, Any]] | None = None,
) -> ToolContext:
    return ToolContext(
        memory=agent.memory,
        config=agent.config,
        workspace=agent.config.workspace,
        event_log=agent.event_log,
        session_id=session_id,
        run_id=run_id,
        approved_tool_call_ids=approved_tool_call_ids,
        approved_tool_call_arguments=approved_tool_call_arguments,
        approval_receipts=approval_receipts,
    )


if __name__ == "__main__":
    raise SystemExit(main())
