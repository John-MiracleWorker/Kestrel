from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ..consolidation import Consolidator
from ..context_frames import (
    default_frame_type_for_memory,
    estimate_tokens,
    from_memory_record,
    make_correction_frame,
    to_memory_record,
)
from ..context_packer import ContextPacker, ContextPackRequest
from ..extension_transaction import (
    DirectorySwap,
    ExtensionCleanupIncompleteError,
    create_sibling_stage,
    extension_lock,
    fsync_tree,
    path_exists,
    read_regular_file,
    remove_tree_verified,
    write_regular_file,
)
from ..models import EvidenceRef, MemoryHit, MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from ..nested_learning import (
    STABLE_MEMORY_LAYERS,
    LearningSignal,
    NestedLearningKernel,
    ValidationEvidence,
    compute_validation_score,
    resolve_validation_evidence,
)
from ..plugin_manager import PluginManager
from ..policy_provenance import (
    POLICY_PROMOTION_TOOL,
    policy_approval_metadata,
    public_tool_arguments,
)
from ..promotion_ledger import OUTCOME_KINDS, PromotionLedger
from ..repair_integrity import load_review_receipt, load_validation_receipt
from ..retention import RetentionCompactor
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..secret_broker import SecretBroker, build_secret_broker, is_secret_ref
from ..self_profile import (
    SELF_PROFILE_SCHEMA,
    TRUSTED_ONBOARDING_LOCATOR,
    TRUSTED_ONBOARDING_ORIGIN,
    TRUSTED_ONBOARDING_PROVENANCE_SCHEMA,
    TRUSTED_ONBOARDING_SOURCE,
    trusted_onboarding_record_ids,
)
from ..skill_validation import validate_skill_manifest
from ..state_store import AgentStateStore
from ..task_capsule import capsule_signal_staging_record, summarize_run_capsule
from .base import AgentTool, ToolContext
from .command_tools import (
    CodexExecTool,
    LintRunTool,
    PatchApplyTool,
    ShellRunTool,
    TestRunTool,
)
from .diagnosis_tools import DiagnosisClassifyTool, DiagnosisRecallTool
from .discovery_tools import (
    McpRegistryTool,
    PluginRegistryTool,
    ProjectScriptsTool,
    SkillDiscoverTool,
    SkillInspectTool,
    ToolRegistryTool,
)
from .git_tools import (
    GitBranchTool,
    GitCommitTool,
    GitCreateLocalBranchTool,
    GitDiffTool,
    GitExportPatchTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
)
from .registry import ToolRegistry
from .repair_tools import (
    RepairApplyPatchTool,
    RepairOrchestrateValidateTool,
    RepairPrepareTool,
    RepairReviewTool,
    RepairRollbackTool,
    RepairStatusTool,
    RepairValidateTool,
)
from .validation_helpers import (
    _evidence_refs_arg,
    _validation_evidence_arg,
    _validation_evidence_payload_for_output,
)
from .web_tools import WebFetchTool, WebSearchTool
from .workspace_tools import (
    FileStatTool,
    FindFilesTool,
    ListFilesTool,
    ReadFileTool,
    RepoMapTool,
    RepoSearchTool,
    WriteFileTool,
)


class MemorySearchTool(AgentTool):
    spec = ToolSpec(
        name="memory.search",
        description="Search nested .mv2 memory layers and return ranked supporting records.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 20},
                "include_inactive": {"type": "boolean"},
                "include_retrieval_artifacts": {"type": "boolean"},
            },
            "required": ["query"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        layer_values = arguments.get("layers")
        try:
            layers = (
                tuple(MemoryLayer(value) for value in layer_values)
                if isinstance(layer_values, list)
                else tuple(MemoryLayer)
            )
        except ValueError as exc:
            return self._result(
                call,
                success=False,
                content=f"Unknown memory layer: {exc}",
                error="invalid_tool_arguments",
            )
        k = int(arguments.get("k", 8))
        hits = context.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=layers,
                k_per_layer=k,
                include_inactive=bool(arguments.get("include_inactive", False)),
                include_retrieval_artifacts=bool(
                    arguments.get("include_retrieval_artifacts", False)
                ),
            )
        )
        rows = []
        for hit in hits[:k]:
            rows.append(
                {
                    "layer": hit.record.layer.value,
                    "kind": hit.record.kind.value,
                    "title": hit.record.title,
                    "score": hit.score,
                    "snippet": hit.snippet or hit.record.content[:500],
                }
            )
        return self._result(
            call,
            success=True,
            content=json.dumps(rows, indent=2),
            data={"hits": rows},
        )


class MemoryWriteTool(AgentTool):
    spec = ToolSpec(
        name="memory.write",
        description=(
            "Write direct working/episodic memory records. Stable layers require memory.learn, "
            "self.remember, memory.correct, memory.import, or an admin path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "layer": {"type": "string"},
                "kind": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "frame_type": {"type": "string"},
                "parent_ids": {"type": "array", "items": {"type": "string"}},
                "child_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["layer", "title", "content"],
        },
        risk="medium",
    )

    _DIRECT_WRITE_LAYERS = {MemoryLayer.WORKING, MemoryLayer.EPISODIC}
    _STABLE_WRITE_PATHS = (
        "memory.learn",
        "self.remember",
        "memory.correct",
        "memory.import",
        "admin path",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            layer = MemoryLayer(str(arguments.get("layer", "working")))
            kind = MemoryKind(str(arguments.get("kind", "observation")))
        except ValueError as exc:
            return self._result(call, success=False, content=str(exc), error="bad_memory_enum")
        if layer == MemoryLayer.POLICY and not context.config.allow_policy_writes:
            return self._result(
                call,
                success=False,
                content="Policy writes are disabled by default.",
                error="policy_write_disabled",
            )
        if layer not in self._DIRECT_WRITE_LAYERS:
            allowed_layers = sorted(layer.value for layer in self._DIRECT_WRITE_LAYERS)
            safe_paths = list(self._STABLE_WRITE_PATHS)
            return self._result(
                call,
                success=False,
                content=(
                    f"Direct memory.write is only allowed for {', '.join(allowed_layers)} layers. "
                    f"Use {'/'.join(safe_paths)} for {layer.value} memory so validation, provenance, "
                    "confidence, and approval gates are preserved."
                ),
                data={
                    "requested_layer": layer.value,
                    "allowed_direct_layers": allowed_layers,
                    "safe_write_paths": safe_paths,
                },
                error="stable_memory_write_rejected",
            )
        try:
            frame_type = str(
                arguments.get("frame_type") or default_frame_type_for_memory(kind, layer)
            )
            parent_ids_arg = arguments.get("parent_ids")
            child_ids_arg = arguments.get("child_ids")
            parent_ids = (
                [str(item) for item in parent_ids_arg] if isinstance(parent_ids_arg, list) else []
            )
            child_ids = (
                [str(item) for item in child_ids_arg] if isinstance(child_ids_arg, list) else []
            )
            record = MemoryRecord(
                layer=layer,
                kind=kind,
                title=str(arguments.get("title", "Memory")),
                content=str(arguments.get("content", "")),
                confidence=float(arguments.get("confidence", 0.7)),
                importance=float(arguments.get("importance", 0.5)),
                metadata={
                    "session_id": context.session_id,
                    "source": "tool.memory.write",
                    "frame_type": frame_type,
                    "parent_ids": parent_ids,
                    "child_ids": child_ids,
                },
            )
            record_id = context.memory.put(record)
            return self._result(
                call,
                success=True,
                content=f"Wrote memory {record_id}",
                data={"record_id": record_id},
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary must report errors to agent
            return self._result(call, success=False, content=str(exc), error="memory_write_failed")


class ContextPackTool(AgentTool):
    spec = ToolSpec(
        name="context.pack",
        description="Compile an on-demand pseudo-context window for the current objective.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "token_budget": {"type": "integer", "minimum": 256, "maximum": 50000},
                "layers": {"type": "array", "items": {"type": "string"}},
                "expand_raw": {"type": "boolean"},
                "include_telemetry": {"type": "boolean"},
            },
            "required": ["query"],
        },
        capabilities=("pseudo-context", "mv2-context"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
            packed = ContextPacker(context.memory).pack(
                ContextPackRequest(
                    objective=query,
                    query=query,
                    token_budget=int(
                        arguments.get("token_budget", context.config.context_pack_token_budget)
                    ),
                    allowed_layers=layers,
                    expand_raw=bool(
                        arguments.get("expand_raw", context.config.context_pack_expand_raw)
                    ),
                    include_telemetry=bool(arguments.get("include_telemetry", True)),
                )
            )
            payload = {
                "packed_prompt": packed.prompt,
                "token_estimate": packed.token_estimate,
                "selected_item_count": len(packed.items),
                "selected_layers": sorted({item.frame.layer.value for item in packed.items}),
                "conflict_warnings": list(packed.conflict_warnings),
                "evidence_refs": list(packed.evidence_refs),
                "telemetry": packed.telemetry,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="context_pack_failed")


class ContextExpandTool(AgentTool):
    spec = ToolSpec(
        name="context.expand",
        description="Expand a specific memory/frame into raw supporting context.",
        parameters={
            "type": "object",
            "properties": {
                "frame_id": {"type": "string"},
                "record_id": {"type": "string"},
                "max_tokens": {"type": "integer", "minimum": 64, "maximum": 50000},
                "include_children": {"type": "boolean"},
                "include_parents": {"type": "boolean"},
            },
        },
        capabilities=("pseudo-context", "mv2-context"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        lookup_id = str(arguments.get("frame_id") or arguments.get("record_id") or "").strip()
        if not lookup_id:
            return self._result(
                call, success=False, content="Missing frame_id or record_id", error="missing_id"
            )
        try:
            hit = _find_memory_by_id(context, lookup_id)
            if hit is None:
                return self._result(
                    call,
                    success=False,
                    content=f"No memory found for {lookup_id}",
                    error="not_found",
                )
            frame = from_memory_record(hit.record)
            related = _related_frames(
                context,
                frame,
                include_children=bool(arguments.get("include_children", False)),
                include_parents=bool(arguments.get("include_parents", False)),
            )
            max_tokens = int(arguments.get("max_tokens", 2000))
            raw_content = _truncate_by_tokens(hit.record.content, max_tokens)
            payload = {
                "frame_id": frame.id,
                "record_id": hit.record.id,
                "raw_content": raw_content,
                "token_estimate": estimate_tokens(raw_content),
                "parent_ids": list(frame.parent_ids),
                "child_ids": list(frame.child_ids),
                "related": related,
                "evidence_metadata": {
                    "source_uri": frame.source_uri,
                    "source_span": frame.source_span,
                    "content_hash": frame.content_hash,
                    "layer": frame.layer.value,
                    "kind": frame.kind.value,
                },
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="context_expand_failed"
            )


class CapsuleSummarizeTool(AgentTool):
    spec = ToolSpec(
        name="capsule.summarize",
        description="Summarize a completed run capsule and show candidate learning signals.",
        parameters={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["run_id"],
        },
        risk="medium",
        capabilities=("task-capsule", "nested-learning"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        run_id = str(arguments.get("run_id", "")).strip()
        if not run_id:
            return self._result(
                call, success=False, content="Missing run_id", error="missing_run_id"
            )
        try:
            summary = summarize_run_capsule(
                runs_dir=context.config.memory_dir.parent / "runs",
                run_id=run_id,
                backend=context.config.backend,
            )
            summary = _resolve_capsule_summary_evidence(summary, context=context)
            kernel = NestedLearningKernel()
            decisions = []
            for signal in summary.learning_signals:
                decision = kernel.decide(signal)
                payload = decision.to_payload()
                payload["signal_title"] = signal.title
                payload["dry_run"] = True
                decisions.append(payload)
            payload = {
                **summary.to_payload(),
                "dry_run": True,
                "nested_learning_decisions": decisions,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="capsule_summarize_failed"
            )


class CapsuleApplyTool(AgentTool):
    needs_call_id = True
    spec = ToolSpec(
        name="capsule.apply",
        description="Apply accepted learning signals from a completed run capsule. Requires auto-consolidation config and approval before writing.",
        parameters={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "dry_run": {"type": "boolean"},
                "include_policy": {"type": "boolean"},
            },
            "required": ["run_id"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("task-capsule", "nested-learning", "memory-write"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        public_arguments = {
            key: value for key, value in arguments.items() if not str(key).startswith("_")
        }
        call_id = str(arguments.get("_tool_call_id") or "")
        call = (
            ToolCall(name=self.spec.name, arguments=public_arguments, id=call_id)
            if call_id
            else ToolCall(name=self.spec.name, arguments=public_arguments)
        )
        run_id = str(arguments.get("run_id", "")).strip()
        if not run_id:
            return self._result(
                call, success=False, content="Missing run_id", error="missing_run_id"
            )
        dry_run = bool(arguments.get("dry_run", False))
        include_policy = bool(arguments.get("include_policy", False))
        try:
            summary = summarize_run_capsule(
                runs_dir=context.config.memory_dir.parent / "runs",
                run_id=run_id,
                backend=context.config.backend,
            )
            summary = _resolve_capsule_summary_evidence(summary, context=context)
            plan = _capsule_apply_plan(summary, context=context, include_policy=include_policy)
            if dry_run:
                payload = {
                    **summary.to_payload(),
                    "dry_run": True,
                    "applied": False,
                    "decisions": plan,
                }
                return self._result(
                    call, success=True, content=json.dumps(payload, indent=2), data=payload
                )

            if not context.config.enable_auto_consolidation:
                payload = {
                    **summary.to_payload(),
                    "dry_run": False,
                    "applied": False,
                    "decisions": plan,
                    "auto_consolidation_enabled": False,
                }
                return self._result(
                    call,
                    success=False,
                    content=json.dumps(payload, indent=2),
                    data=payload,
                    error="auto_consolidation_disabled",
                )

            wrote = False
            for item in plan:
                if item.get("will_write") is not True:
                    continue
                signal_index = item.get("signal_index")
                if not isinstance(signal_index, int):
                    continue
                signal = summary.learning_signals[signal_index]
                if item.get("write_mode") == "unvalidated_episodic_staging":
                    staged_record = capsule_signal_staging_record(signal)
                    if staged_record is None:
                        continue
                    if _memory_has_content_hash(
                        context,
                        MemoryLayer.EPISODIC,
                        staged_record.content_hash,
                    ):
                        item["skipped"] = "duplicate_content_hash"
                        item["will_write"] = False
                        continue
                    item["record_id"] = context.memory.put(staged_record)
                    item["actual_layer"] = MemoryLayer.EPISODIC.value
                    item["validation_status"] = "unresolved"
                    wrote = True
                    continue
                decision = NestedLearningKernel().decide(signal)
                if decision.target_layer is None:
                    continue
                record = NestedLearningKernel().to_memory_record(signal, decision)
                if _memory_has_content_hash(context, record.layer, record.content_hash):
                    item["skipped"] = "duplicate_content_hash"
                    item["will_write"] = False
                    continue
                source_ids = _stable_signal_source_record_ids(signal)
                item["record_id"] = context.memory.put_validated(
                    record,
                    authority="nested_learning",
                    source_record_ids=source_ids,
                    validation_evidence=signal.validation_evidence,
                )
                wrote = True
            if wrote:
                context.memory.seal_all()
            payload = {
                **summary.to_payload(),
                "dry_run": False,
                "applied": wrote,
                "decisions": plan,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="capsule_apply_failed")


class MemoryConflictsTool(AgentTool):
    spec = ToolSpec(
        name="memory.conflicts",
        description="Search for conflicting memories around a claim/query.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
        capabilities=("pseudo-context", "conflict-detection"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
            k = int(arguments.get("k", 8))
            packed = ContextPacker(context.memory).pack(
                ContextPackRequest(
                    objective=f"Find conflicting memories for: {query}",
                    query=query,
                    allowed_layers=layers,
                    token_budget=context.config.context_pack_token_budget,
                    k_per_layer=k,
                    include_telemetry=True,
                )
            )
            hits = context.memory.retrieve(
                RetrievalQuery(query=query, layers=layers, k_per_layer=k)
            )
            possible_conflicts = []
            for hit in hits[:k]:
                metadata = hit.record.metadata
                possible_conflicts.append(
                    {
                        "record_id": hit.record.id,
                        "frame_id": metadata.get("frame_id") or hit.frame_id,
                        "layer": hit.record.layer.value,
                        "kind": hit.record.kind.value,
                        "title": hit.record.title,
                        "confidence": hit.record.confidence,
                        "importance": hit.record.importance,
                        "score": hit.score,
                        "conflict_group_id": metadata.get("conflict_group_id"),
                        "snippet": hit.snippet or hit.record.content[:500],
                    }
                )
            payload = {
                "query": query,
                "conflict_warnings": list(packed.conflict_warnings),
                "possible_conflicts": possible_conflicts,
                "recommended_action": "Expand raw evidence and validate the conflicting claim before writing or promoting memory."
                if packed.conflict_warnings
                else "No conflict metadata detected; still validate high-impact claims against evidence.",
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="memory_conflicts_failed"
            )


class MemvidVerifyTool(AgentTool):
    spec = ToolSpec(
        name="memvid.verify",
        description="Verify every nested memory layer.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        results = context.memory.verify_all()
        rows = {layer.value: ok for layer, ok in results.items()}
        return self._result(
            call,
            success=all(rows.values()),
            content=json.dumps(rows, indent=2),
            data={"layers": rows},
        )


class MemvidDoctorTool(AgentTool):
    spec = ToolSpec(
        name="memvid.doctor",
        description="Run read-only doctor checks on memory layers when the backend supports it.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        if arguments.get("dry_run") is False:
            return self._result(
                call,
                success=False,
                content=(
                    "Mutating Memvid doctor repairs are not exposed as a low-risk agent tool; "
                    "run an owner-controlled maintenance workflow instead."
                ),
                error="mutating_doctor_disabled",
            )
        rows: dict[str, object] = {}
        for layer, backend in context.memory.backends.items():
            doctor = getattr(backend, "doctor", None)
            if callable(doctor):
                rows[layer.value] = doctor(dry_run=True)
            else:
                rows[layer.value] = {"ok": backend.verify(), "doctor_available": False}
        return self._result(
            call,
            success=True,
            content=json.dumps(rows, indent=2, default=str),
            data={"layers": rows},
        )


class MemvidStatsTool(AgentTool):
    spec = ToolSpec(
        name="memvid.stats",
        description="Return backend statistics for memory layers when available.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        rows: dict[str, object] = {}
        for layer, backend in context.memory.backends.items():
            stats = getattr(backend, "stats", None)
            if callable(stats):
                rows[layer.value] = stats()
            else:
                rows[layer.value] = {"ok": backend.verify(), "stats_available": False}
        return self._result(
            call,
            success=True,
            content=json.dumps(rows, indent=2, default=str),
            data={"layers": rows},
        )


class MemoryLedgerTool(AgentTool):
    spec = ToolSpec(
        name="memory.ledger",
        description="Summarize promotion ledger outcomes without changing memory gates.",
        parameters={
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "target_layer": {"type": "string"},
                "outcome": {"type": "string"},
            },
        },
        capabilities=("nested-learning", "memory-diagnostics"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            since = _datetime_arg(arguments.get("since"))
            target_layer = _layer_arg(arguments.get("target_layer"))
            outcome = _outcome_arg(arguments.get("outcome"))
            ledger = context.memory.ledger or PromotionLedger(
                AgentStateStore(context.config.state_path)
            )
            payload = ledger.summarize(
                since=since, target_layer=target_layer, outcome=outcome
            ).to_payload()
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary returns structured diagnostics
            return self._result(call, success=False, content=str(exc), error="memory_ledger_failed")


class MemoryConsolidateTool(AgentTool):
    spec = ToolSpec(
        name="memory.consolidate",
        description="Promote a validated memory candidate through the nested consolidation pipeline.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source_layer": {"type": "string"},
                "source_record_id": {"type": "string"},
                "validation_evidence": {"type": "object"},
                "validation_score": {"type": "number", "minimum": 0, "maximum": 1},
                "repeat_count": {"type": "integer", "minimum": 1},
                "explicit_instruction": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["query"],
        },
        risk="medium",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            source_layer = _layer_arg(arguments.get("source_layer"))
            layers = (source_layer,) if source_layer else tuple(MemoryLayer)
            hits = context.memory.retrieve(
                RetrievalQuery(query=query, layers=layers, k_per_layer=1)
            )
            if not hits:
                return self._result(
                    call,
                    success=False,
                    content="No consolidation candidate found.",
                    error="candidate_not_found",
                )
            validation_evidence = _validation_evidence_arg(arguments)
            if validation_evidence is not None:
                validation_evidence, _, _ = _resolve_runtime_validation_evidence(
                    validation_evidence,
                    context=context,
                    expected_subject_record_id=hits[0].record.id,
                )
            validation_score = (
                compute_validation_score(validation_evidence)
                if validation_evidence is not None
                else float(arguments.get("validation_score", 0.7))
            )
            repeat_count = int(arguments.get("repeat_count", 1))
            explicit_instruction = bool(arguments.get("explicit_instruction", False))
            candidate = Consolidator().propose(
                hits[0].record,
                validation_score=validation_score,
                validation_evidence=validation_evidence,
                repeat_count=repeat_count,
                explicit_instruction=explicit_instruction,
            )
            if candidate is None:
                return self._result(
                    call, success=True, content="No promotion proposed.", data={"promoted": False}
                )
            if (
                candidate.target_layer == MemoryLayer.POLICY
                and not context.config.allow_policy_writes
            ):
                return self._result(
                    call,
                    success=False,
                    content="Policy promotion is disabled by default.",
                    error="policy_write_disabled",
                )
            if candidate.target_layer == MemoryLayer.POLICY:
                return self._result(
                    call,
                    success=False,
                    content=(
                        "Policy promotion requires the dedicated exact-call approval path. "
                        "Use memory.policy_promote with structured repeated evidence."
                    ),
                    error="policy_approval_required",
                )
            promoted = Consolidator().promote(candidate)
            promoted.metadata.update(
                {
                    "session_id": context.session_id,
                    "run_id": context.run_id,
                }
            )
            dry_run = bool(arguments.get("dry_run", False))
            source_ids = tuple(str(item) for item in promoted.metadata.get("source_record_ids", []))
            record_id = (
                None
                if dry_run
                else context.memory.put_validated(
                    promoted,
                    authority="nested_learning",
                    source_record_ids=source_ids,
                    validation_evidence=candidate.signal.validation_evidence,
                )
            )
            if record_id is not None:
                context.memory.seal_all()
            payload = {
                "promoted": True,
                "dry_run": dry_run,
                "record_id": record_id,
                "source_record_id": candidate.source.id,
                "source_layer": candidate.source.layer.value,
                "target_layer": candidate.target_layer.value,
                "reason": candidate.reason,
                "confidence": candidate.promoted_confidence,
                "context_flow": candidate.flow.to_metadata(),
                "optimizer_trace": candidate.optimizer_trace.to_metadata(),
                "validation_score": validation_score,
                "validation_evidence": _validation_evidence_payload_for_output(
                    validation_evidence, validation_score
                ),
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="memory_consolidate_failed"
            )


class MemoryLearnTool(AgentTool):
    spec = ToolSpec(
        name="memory.learn",
        description="Compress a validated learning signal into the correct nested memory layer using context-flow gates.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "kind": {"type": "string"},
                "source_layer": {"type": "string"},
                "source_record_id": {"type": "string"},
                "target_layer": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "validation_evidence": {"type": "object"},
                "validation_score": {"type": "number", "minimum": 0, "maximum": 1},
                "repeat_count": {"type": "integer", "minimum": 1},
                "explicit_instruction": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "source": {"type": "string"},
                "locator": {"type": "string"},
            },
            "required": ["title", "content"],
        },
        risk="medium",
        capabilities=("nested-learning", "continuum-memory"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        title = str(arguments.get("title", "")).strip()
        content = str(arguments.get("content", "")).strip()
        if not title:
            return self._result(call, success=False, content="Missing title", error="missing_title")
        if not content:
            return self._result(
                call, success=False, content="Missing content", error="missing_content"
            )
        try:
            source_layer = _layer_arg(arguments.get("source_layer")) or MemoryLayer.WORKING
            source_record_id = str(arguments.get("source_record_id") or "").strip()
            source_record = (
                context.memory.get_record(None, source_record_id, include_inactive=False)
                if source_record_id
                else None
            )
            target_layer = _layer_arg(arguments.get("target_layer"))
            kind = MemoryKind(str(arguments.get("kind", MemoryKind.OBSERVATION.value)))
            validation_evidence = _validation_evidence_arg(arguments)
            if validation_evidence is not None:
                validation_evidence, _, _ = _resolve_runtime_validation_evidence(
                    validation_evidence,
                    context=context,
                    expected_subject_record_id=source_record_id or None,
                )
            if target_layer == MemoryLayer.POLICY and not context.config.allow_policy_writes:
                return self._result(
                    call,
                    success=False,
                    content="Policy promotion is disabled by default.",
                    data={"policy_write_enabled": False},
                    error="policy_write_disabled",
                )
            if target_layer == MemoryLayer.POLICY:
                return self._result(
                    call,
                    success=False,
                    content=(
                        "Policy promotion requires the dedicated exact-call approval path. "
                        "Use memory.policy_promote with structured repeated evidence."
                    ),
                    error="policy_approval_required",
                )
            signal = LearningSignal(
                title=title,
                content=content,
                kind=kind,
                source_layer=source_layer,
                confidence=float(arguments.get("confidence", 0.6)),
                importance=float(arguments.get("importance", 0.5)),
                validation_score=None
                if validation_evidence is not None
                else float(arguments.get("validation_score", 0.7)),
                validation_evidence=validation_evidence,
                repeat_count=int(arguments.get("repeat_count", 1)),
                explicit_instruction=bool(arguments.get("explicit_instruction", False)),
                source=str(arguments.get("source", "tool.memory.learn")),
                locator=str(arguments.get("locator", context.session_id)),
                metadata={"session_id": context.session_id, "run_id": context.run_id},
                requested_target_layer=target_layer,
            )
            kernel = NestedLearningKernel()
            decision = kernel.decide(signal)
            if not decision.accepted:
                payload = {
                    **decision.to_payload(),
                    "dry_run": bool(arguments.get("dry_run", False)),
                    "record_id": None,
                    "validation_score": signal.computed_validation_score,
                    "validation_evidence": _validation_evidence_payload_for_output(
                        validation_evidence,
                        signal.computed_validation_score,
                    ),
                }
                return self._result(
                    call,
                    success=True,
                    content=json.dumps(payload, indent=2),
                    data=payload,
                )
            if (
                decision.target_layer == MemoryLayer.POLICY
                and not context.config.allow_policy_writes
            ):
                payload = decision.to_payload()
                payload["policy_write_enabled"] = False
                return self._result(
                    call,
                    success=False,
                    content=json.dumps(payload, indent=2),
                    data=payload,
                    error="policy_write_disabled",
                )
            if decision.target_layer == MemoryLayer.POLICY:
                return self._result(
                    call,
                    success=False,
                    content=(
                        "Policy promotion requires the dedicated exact-call approval path. "
                        "Use memory.policy_promote with structured repeated evidence."
                    ),
                    data=decision.to_payload(),
                    error="policy_approval_required",
                )
            if decision.target_layer in STABLE_MEMORY_LAYERS and (
                source_record is None
                or source_record.layer != source_layer
                or source_record.title != title
                or source_record.content != content
                or source_record.kind != kind
            ):
                return self._result(
                    call,
                    success=False,
                    content=(
                        "Stable learning requires an active source_record_id whose layer, "
                        "title, content, and kind exactly match the proposed claim."
                    ),
                    data=decision.to_payload(),
                    error="stable_learning_source_mismatch",
                )
            record = kernel.to_memory_record(signal, decision)
            dry_run = bool(arguments.get("dry_run", False))
            source_ids = tuple(
                dict.fromkeys(
                    (
                        *((source_record.id,) if source_record is not None else ()),
                        *_stable_signal_source_record_ids(signal),
                    )
                )
            )
            if source_record is not None:
                record.evidence.append(
                    EvidenceRef(source="memory_record", locator=source_record.id)
                )
            record_id = (
                None
                if dry_run
                else context.memory.put_validated(
                    record,
                    authority="nested_learning",
                    source_record_ids=source_ids,
                    validation_evidence=signal.validation_evidence,
                )
            )
            if record_id is not None:
                context.memory.seal_all()
            payload = {
                **decision.to_payload(),
                "dry_run": dry_run,
                "record_id": record_id,
                "validation_score": signal.computed_validation_score,
                "validation_evidence": _validation_evidence_payload_for_output(
                    validation_evidence,
                    signal.computed_validation_score,
                ),
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="memory_learn_failed")


class MemoryPolicyPromoteTool(AgentTool):
    """The sole built-in path that can create system-trusted policy memory."""

    needs_call_id = True
    spec = ToolSpec(
        name=POLICY_PROMOTION_TOOL,
        description=(
            "Stage or promote a policy candidate. Promotion requires owner approval of "
            "this exact call plus claim-bound receipts from at least five distinct "
            "validation tasks."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "stage_proposal": {"type": "boolean"},
                "source_record_id": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "validation_evidence": {"type": "object"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["title", "content"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("nested-learning", "continuum-memory", "policy-write"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        public_arguments = public_tool_arguments(arguments)
        call_id = str(arguments.get("_tool_call_id") or "")
        call = ToolCall(name=self.spec.name, arguments=public_arguments, id=call_id)
        title = str(public_arguments.get("title", "")).strip()
        content = str(public_arguments.get("content", "")).strip()
        if not title:
            return self._result(call, success=False, content="Missing title", error="missing_title")
        if not content:
            return self._result(
                call, success=False, content="Missing content", error="missing_content"
            )
        if not context.config.allow_policy_writes:
            return self._result(
                call,
                success=False,
                content="Policy promotion is disabled by default.",
                error="policy_write_disabled",
            )
        receipt = (
            context.approval_receipts.get(call_id)
            if context.approval_receipts is not None
            else None
        )
        approval = (
            policy_approval_metadata(
                receipt,
                call_id=call_id,
                arguments=public_arguments,
                run_id=context.run_id,
            )
            if isinstance(receipt, dict)
            else None
        )
        if approval is None:
            return self._result(
                call,
                success=False,
                content="A durable owner approval receipt for this exact call is required.",
                error="approval_provenance_required",
            )
        dry_run = bool(public_arguments.get("dry_run", False))
        source_record_id = str(public_arguments.get("source_record_id") or "").strip()
        stage_proposal = bool(public_arguments.get("stage_proposal", False))
        if stage_proposal:
            if source_record_id or public_arguments.get("validation_evidence") is not None:
                return self._result(
                    call,
                    success=False,
                    content=(
                        "Policy proposal staging cannot include source_record_id or validation "
                        "evidence. Stage first, then validate that exact proposal."
                    ),
                    error="policy_proposal_arguments_invalid",
                )
            proposal_id = None
            if not dry_run:
                proposal_id = context.memory.put(
                    MemoryRecord(
                        title=title,
                        content=content,
                        layer=MemoryLayer.EPISODIC,
                        kind=MemoryKind.POLICY,
                        confidence=max(float(public_arguments.get("confidence", 0.95)), 0.5),
                        importance=float(public_arguments.get("importance", 0.95)),
                        metadata={
                            "frame_type": "trace_stub",
                            "validation_status": "policy_promotion_candidate",
                            "policy_promotion_candidate": True,
                            "proposal_approval_id": approval["approval_id"],
                            "session_id": context.session_id,
                            "run_id": context.run_id,
                        },
                        evidence=[
                            EvidenceRef(
                                source=POLICY_PROMOTION_TOOL,
                                locator=approval["approval_id"],
                            )
                        ],
                    )
                )
                context.memory.seal_all()
            payload = {
                "staged": True,
                "promoted": False,
                "dry_run": dry_run,
                "proposal_id": proposal_id,
                "next_action": (
                    "Run test.run, lint.run, repair.validate, and repair.review with "
                    "subject_record_id set to proposal_id, then submit a separately approved "
                    "memory.policy_promote call using those memory_record receipts."
                ),
            }
            return self._result(
                call,
                success=True,
                content=json.dumps(payload, indent=2),
                data=payload,
            )
        if not source_record_id:
            return self._result(
                call,
                success=False,
                content=(
                    "Policy promotion requires source_record_id for a proposal staged by an "
                    "earlier approved call. Use stage_proposal=true first."
                ),
                error="policy_source_record_required",
            )
        proposal = context.memory.get_record(
            MemoryLayer.EPISODIC,
            source_record_id,
            include_inactive=False,
        )
        if (
            proposal is None
            or proposal.kind != MemoryKind.POLICY
            or proposal.title != title
            or proposal.content != content
            or proposal.metadata.get("policy_promotion_candidate") is not True
            or proposal.metadata.get("validation_status") != "policy_promotion_candidate"
            or not str(proposal.metadata.get("proposal_approval_id") or "").strip()
            or str(proposal.metadata.get("session_id") or "") != context.session_id
            or proposal.metadata.get("run_id") != context.run_id
        ):
            return self._result(
                call,
                success=False,
                content=(
                    "source_record_id must name the active, exact policy proposal staged in "
                    "this session and run."
                ),
                error="policy_source_record_mismatch",
            )
        validation_evidence = _validation_evidence_arg(
            public_arguments,
            trust_human_explicit=True,
        )
        policy_spec = context.memory.specs[MemoryLayer.POLICY]
        if validation_evidence is None or not validation_evidence.human_explicit:
            return self._result(
                call,
                success=False,
                content="Policy promotion requires structured, human-explicit validation evidence.",
                error="policy_evidence_invalid",
            )
        distinct_tasks = {
            (ref.source.strip(), ref.locator.strip())
            for ref in validation_evidence.task_refs
            if ref.source.strip() and ref.locator.strip()
        }
        if len(distinct_tasks) < policy_spec.min_repeat_count_for_promotion:
            return self._result(
                call,
                success=False,
                content=(
                    "Policy promotion requires at least "
                    f"{policy_spec.min_repeat_count_for_promotion} distinct task evidence refs; "
                    f"received {len(distinct_tasks)}."
                ),
                error="policy_repeat_evidence_insufficient",
            )
        (
            resolved_evidence,
            source_record_ids,
            resolved_artifact_bindings,
            unresolved_refs,
        ) = _resolve_policy_evidence(
            validation_evidence,
            context=context,
            subject_record_id=source_record_id,
        )
        if unresolved_refs:
            return self._result(
                call,
                success=False,
                content=(
                    "Policy evidence must resolve to claim-bound, current-run validation "
                    "receipts from the correct tool class. Unresolved refs: "
                    + ", ".join(unresolved_refs)
                ),
                data={"unresolved_evidence_refs": unresolved_refs},
                error="policy_evidence_unresolved",
            )
        validation_evidence = resolved_evidence
        score = compute_validation_score(validation_evidence)
        if score < policy_spec.promotion_threshold:
            return self._result(
                call,
                success=False,
                content=(
                    f"Policy validation score {score:.2f} is below "
                    f"{policy_spec.promotion_threshold:.2f}."
                ),
                error="policy_evidence_invalid",
            )
        try:
            signal = LearningSignal(
                title=title,
                content=content,
                kind=MemoryKind.POLICY,
                source_layer=MemoryLayer.PROCEDURAL,
                confidence=float(public_arguments.get("confidence", 0.95)),
                importance=float(public_arguments.get("importance", 0.95)),
                validation_score=None,
                validation_evidence=validation_evidence,
                repeat_count=len(distinct_tasks),
                explicit_instruction=True,
                source=POLICY_PROMOTION_TOOL,
                locator=approval["approval_id"],
                metadata={
                    "session_id": context.session_id,
                    "run_id": context.run_id,
                    "approval_provenance": approval,
                    "resolved_artifact_bindings": resolved_artifact_bindings,
                },
                requested_target_layer=MemoryLayer.POLICY,
            )
            kernel = NestedLearningKernel(specs=context.memory.specs)
            decision = kernel.decide(signal, action="promote")
            if not decision.accepted or decision.target_layer != MemoryLayer.POLICY:
                return self._result(
                    call,
                    success=False,
                    content=decision.reason,
                    data=decision.to_payload(),
                    error="policy_memory_rejected",
                )
            record = kernel.to_memory_record(signal, decision)
            source_record_ids = tuple(
                dict.fromkeys((source_record_id, *source_record_ids))
            )
            record.evidence.append(
                EvidenceRef(source="memory_record", locator=source_record_id)
            )
            record_id = (
                None
                if dry_run
                else context.memory.put_validated(
                    record,
                    authority="nested_learning",
                    source_record_ids=source_record_ids,
                    validation_evidence=validation_evidence,
                )
            )
            if record_id is not None:
                context.memory.seal_all()
            promotion_payload: dict[str, Any] = {
                **decision.to_payload(),
                "dry_run": dry_run,
                "record_id": record_id,
                "record_content_hash": record.content_hash,
                "validation_score": score,
                "repeat_count": len(distinct_tasks),
                "approval_id": approval["approval_id"],
                "validation_evidence": validation_evidence.to_metadata(),
            }
            return self._result(
                call,
                success=True,
                content=json.dumps(promotion_payload, indent=2),
                data=promotion_payload,
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="policy_promote_failed"
            )


class MemoryInspectTool(AgentTool):
    spec = ToolSpec(
        name="memory.inspect",
        description="Inspect matching memory records with provenance and metadata.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 50},
                "include_inactive": {"type": "boolean"},
                "include_retrieval_artifacts": {"type": "boolean"},
            },
            "required": ["query"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
        hits = context.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=layers,
                k_per_layer=int(arguments.get("k", 8)),
                include_inactive=bool(arguments.get("include_inactive", False)),
                include_retrieval_artifacts=bool(
                    arguments.get("include_retrieval_artifacts", False)
                ),
            )
        )
        rows = [_memory_hit_payload(hit) for hit in hits]
        return self._result(
            call, success=True, content=json.dumps(rows, indent=2), data={"hits": rows}
        )


class MemoryCorrectTool(AgentTool):
    spec = ToolSpec(
        name="memory.correct",
        description="Write a correction frame for a target memory record and supersede the target by default.",
        parameters={
            "type": "object",
            "properties": {
                "target_record_id": {"type": "string"},
                "correction_text": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "object"}},
                "dry_run": {"type": "boolean"},
            },
            "required": ["target_record_id", "correction_text"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        target_id = str(arguments.get("target_record_id", "")).strip()
        correction_text = str(arguments.get("correction_text", "")).strip()
        if not target_id:
            return self._result(
                call,
                success=False,
                content="Missing target_record_id",
                error="missing_target_record_id",
            )
        if not correction_text:
            return self._result(
                call,
                success=False,
                content="Missing correction_text",
                error="missing_correction_text",
            )
        target = context.memory.get_record(None, target_id, include_inactive=True)
        if target is None:
            return self._result(
                call,
                success=False,
                content="Target memory record not found.",
                error="target_not_found",
            )
        evidence = _evidence_refs_arg(arguments.get("evidence"))
        dry_run = bool(arguments.get("dry_run", False))
        frame = make_correction_frame(
            target_record_id=target.id,
            layer=target.layer,
            correction_text=correction_text,
            evidence=evidence,
            title=f"Correction: {target.title}",
        )
        record_id = None
        if not dry_run:
            correction_record = to_memory_record(frame)
            correction_record.evidence.append(
                EvidenceRef(source="memory_record", locator=target.id)
            )
            record_id = (
                context.memory.put_validated(
                    correction_record,
                    authority="approved_correction",
                    source_record_ids=(target.id,),
                )
                if target.layer in STABLE_MEMORY_LAYERS
                else context.memory.put(correction_record)
            )
            context.memory.tombstone(
                target.layer, target.id, reason="corrected", superseded_by=str(record_id)
            )
            context.memory.seal_all()
        payload = {
            "corrected": True,
            "dry_run": dry_run,
            "target_record_id": target.id,
            "target_layer": target.layer.value,
            "correction_record_id": record_id,
            "correction_frame_id": frame.id,
            "evidence": [ref.__dict__ for ref in evidence],
        }
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class MemoryCompactTool(AgentTool):
    spec = ToolSpec(
        name="memory.compact",
        description="Compact TTL-eligible working/episodic memory. Dry-run by default.",
        parameters={
            "type": "object",
            "properties": {
                "layer": {"type": "string"},
                "apply": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
            },
        },
        risk="medium",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        layer = _layer_arg(arguments.get("layer")) or MemoryLayer.WORKING
        dry_run = bool(arguments.get("dry_run", not bool(arguments.get("apply", False))))
        report = RetentionCompactor(context.memory).compact_layer(layer, dry_run=dry_run)
        if not dry_run:
            context.memory.seal_all()
        return self._result(
            call, success=True, content=json.dumps(report, indent=2, default=str), data=report
        )


class MemoryExportTool(AgentTool):
    spec = ToolSpec(
        name="memory.export",
        description="Export a bounded, paginated page of memory records as structured JSON.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "include_inactive": {"type": "boolean"},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
        rows: list[dict[str, object]] = []
        query = str(arguments.get("query", "")).strip()
        include_inactive = bool(arguments.get("include_inactive", False))
        if query:
            k_per_layer = int(arguments.get("k", 20))
            hits = context.memory.retrieve(
                RetrievalQuery(
                    query=query,
                    layers=layers,
                    k_per_layer=k_per_layer,
                    include_inactive=include_inactive,
                )
            )
            rows = [_memory_record_payload(hit.record) for hit in hits]
            payload: dict[str, object] = {
                "mode": "query",
                "records": rows,
                "count": len(rows),
                "layers": [layer.value for layer in layers],
                "k_per_layer": k_per_layer,
                "include_inactive": include_inactive,
                "complete_export": False,
            }
        else:
            offset = int(arguments.get("offset", 0))
            limit = int(arguments.get("limit", 100))
            if offset < 0 or not 1 <= limit <= 1000:
                return self._result(
                    call,
                    success=False,
                    content="offset must be non-negative and limit must be between 1 and 1000",
                    error="bad_pagination",
                )
            total = 0
            for layer in layers:
                for record in context.memory.iter_records(
                    layer,
                    include_inactive=include_inactive,
                ):
                    if offset <= total < offset + limit:
                        rows.append(_memory_record_payload(record))
                    total += 1
            next_offset = offset + len(rows) if offset + len(rows) < total else None
            payload = {
                "mode": "full",
                "records": rows,
                "count": len(rows),
                "total": total,
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset,
                "truncated": next_offset is not None,
                "include_inactive": include_inactive,
                "layers": [layer.value for layer in layers],
                "complete_export": offset == 0 and next_offset is None,
            }
        return self._result(
            call,
            success=True,
            content=json.dumps(payload, indent=2),
            data=payload,
        )


class MemoryImportTool(AgentTool):
    spec = ToolSpec(
        name="memory.import",
        description="Import explicit memory records. Requires approval and keeps policy writes gated separately.",
        parameters={
            "type": "object",
            "properties": {
                "records": {"type": "array", "items": {"type": "object"}},
                "dry_run": {"type": "boolean"},
            },
            "required": ["records"],
        },
        risk="critical",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        raw_records = arguments.get("records")
        if not isinstance(raw_records, list):
            return self._result(
                call, success=False, content="records must be a list", error="bad_records"
            )
        if not all(isinstance(item, dict) for item in raw_records):
            return self._result(
                call, success=False, content="Every record must be an object", error="bad_records"
            )
        try:
            records = [_memory_record_from_payload(item, imported=True) for item in raw_records]
        except Exception as exc:  # noqa: BLE001 - import payload validation boundary
            return self._result(call, success=False, content=str(exc), error="bad_records")
        if (
            any(record.layer == MemoryLayer.POLICY for record in records)
            and not context.config.allow_policy_writes
        ):
            return self._result(
                call,
                success=False,
                content="Policy memory import is disabled by default.",
                error="policy_write_disabled",
            )
        staged_layers = [
            record.layer.value for record in records if record.layer in STABLE_MEMORY_LAYERS
        ]
        records = [_stage_untrusted_import(record) for record in records]
        dry_run = bool(arguments.get("dry_run", False))
        ids: list[str] = []
        if not dry_run:
            try:
                for record in records:
                    ids.append(context.memory.put(record))
                context.memory.seal_all()
            except Exception as exc:  # noqa: BLE001 - import should report failed writes structurally
                return self._result(
                    call, success=False, content=str(exc), error="memory_import_failed"
                )
        payload = {
            "dry_run": dry_run,
            "imported": len(records),
            "record_ids": ids,
            "staged_stable_records": len(staged_layers),
            "requested_stable_layers": staged_layers,
            "stable_import_status": "untrusted_episodic_staging",
        }
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SkillInstallTool(AgentTool):
    spec = ToolSpec(
        name="skill.install",
        description="Install or update a local skill capsule under the configured skills directory. Requires file-write enablement and approval.",
        parameters={
            "type": "object",
            "properties": {
                "manifest": {"type": "object"},
                "instructions": {"type": "string"},
                "overwrite": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["manifest", "instructions"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("skill-install", "tool-upload", "provenance"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        manifest_raw = arguments.get("manifest")
        instructions = str(arguments.get("instructions", ""))
        if not isinstance(manifest_raw, dict):
            return self._result(
                call, success=False, content="manifest must be an object", error="bad_manifest"
            )
        if not instructions.strip():
            return self._result(
                call,
                success=False,
                content="instructions cannot be empty",
                error="missing_instructions",
            )
        manifest = dict(manifest_raw)
        skill_id = str(manifest.get("id", "")).strip()
        if not skill_id:
            return self._result(
                call, success=False, content="manifest.id is required", error="missing_skill_id"
            )
        if not _safe_skill_id(skill_id):
            return self._result(
                call, success=False, content=f"Unsafe skill id: {skill_id}", error="unsafe_skill_id"
            )
        validation = validate_skill_manifest(manifest)
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True)
        payload: dict[str, Any] = {
            "skill_id": skill_id,
            "validation": validation,
            "manifest_sha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
            "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            "dry_run": bool(arguments.get("dry_run", False)),
        }
        if validation["errors"]:
            return self._result(
                call,
                success=False,
                content=json.dumps(payload, indent=2),
                data=payload,
                error="invalid_skill_manifest",
            )

        try:
            skills_root = context.config.skills_dir.resolve()
            skill_dir = skills_root / skill_id
            payload["path"] = str(skill_dir)
            if payload["dry_run"]:
                return self._result(
                    call, success=True, content=json.dumps(payload, indent=2), data=payload
                )
            with extension_lock(skills_root, ".skill-install.lock"):
                if path_exists(skill_dir) and not bool(arguments.get("overwrite", False)):
                    return self._result(
                        call,
                        success=False,
                        content=json.dumps(payload, indent=2),
                        data=payload,
                        error="skill_exists",
                    )
                stage = create_sibling_stage(skills_root, prefix=skill_id)
                try:
                    manifest_bytes = (manifest_text + "\n").encode()
                    instructions_bytes = instructions.encode()
                    write_regular_file(stage / "skill.json", manifest_bytes)
                    write_regular_file(stage / "SKILL.md", instructions_bytes)
                    if read_regular_file(stage / "skill.json") != manifest_bytes:
                        raise ValueError("Staged skill manifest changed during validation.")
                    if read_regular_file(stage / "SKILL.md") != instructions_bytes:
                        raise ValueError("Staged skill instructions changed during validation.")
                    persisted_manifest = json.loads(
                        read_regular_file(stage / "skill.json").decode("utf-8")
                    )
                    persisted_validation = validate_skill_manifest(persisted_manifest)
                    if persisted_validation["errors"]:
                        raise ValueError("Staged skill manifest failed validation.")
                    fsync_tree(stage)

                    swap = DirectorySwap(live=skill_dir, stage=stage)
                    swap.publish()
                    try:
                        if read_regular_file(skill_dir / "skill.json") != manifest_bytes:
                            raise ValueError("Published skill manifest changed.")
                        if read_regular_file(skill_dir / "SKILL.md") != instructions_bytes:
                            raise ValueError("Published skill instructions changed.")
                    except BaseException:
                        swap.restore()
                        raise
                    swap.finalize()
                    payload["installed"] = True
                    return self._result(
                        call,
                        success=True,
                        content=json.dumps(payload, indent=2),
                        data=payload,
                    )
                finally:
                    if path_exists(stage):
                        remove_tree_verified(stage)
        except ExtensionCleanupIncompleteError as exc:
            return self._result(
                call,
                success=False,
                content=str(exc),
                error="skill_install_cleanup_incomplete",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="skill_install_failed")


class PluginInstallTool(AgentTool):
    spec = ToolSpec(
        name="plugin.install",
        description="Install a public GitHub plugin repo into the local plugin registry. Requires plugin-install enablement and exact approval.",
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "ref": {"type": "string"},
                "enable": {"type": "boolean"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["source"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("plugin-install", "github", "provenance"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        source = str(arguments.get("source", "")).strip()
        if not source:
            return self._result(
                call, success=False, content="source is required", error="missing_source"
            )
        state = AgentStateStore(context.config.state_path)
        manager = PluginManager(context.config.plugins_dir, state)
        try:
            plugin = manager.install(
                source,
                ref=str(arguments["ref"]).strip() if arguments.get("ref") else None,
                enable=bool(arguments.get("enable", False)),
                overwrite=bool(arguments.get("overwrite", False)),
            )
            record_id = manager.write_audit_memory(context.memory, action="install", plugin=plugin)
            payload = {**plugin, "memory_record_id": record_id}
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001 - plugin install boundary reports structured failure
            return self._result(
                call, success=False, content=str(exc), error="plugin_install_failed"
            )


class PluginReviewTool(AgentTool):
    spec = ToolSpec(
        name="plugin.review",
        description="Review a public GitHub plugin repo without installing it. Requires plugin-install enablement and exact approval.",
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "ref": {"type": "string"},
            },
            "required": ["source"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("plugin-review", "github", "provenance", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        source = str(arguments.get("source", "")).strip()
        if not source:
            return self._result(
                call, success=False, content="source is required", error="missing_source"
            )
        state = AgentStateStore(context.config.state_path)
        manager = PluginManager(context.config.plugins_dir, state)
        try:
            review = manager.review(
                source,
                ref=str(arguments["ref"]).strip() if arguments.get("ref") else None,
            )
            return self._result(
                call, success=True, content=json.dumps(review, indent=2), data=review
            )
        except Exception as exc:  # noqa: BLE001 - plugin review boundary reports structured failure
            return self._result(call, success=False, content=str(exc), error="plugin_review_failed")


class SelfInspectTool(AgentTool):
    spec = ToolSpec(
        name="self.inspect",
        description="Inspect Kestrel's non-secret self model, capabilities, tools, memory layers, skills, plugins, and MCP state.",
        parameters={
            "type": "object",
            "properties": {
                "include_tools": {"type": "boolean"},
                "include_state": {"type": "boolean"},
            },
        },
        capabilities=("self-model", "introspection", "soul"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        payload = _self_snapshot(
            context,
            include_tools=bool(arguments.get("include_tools", False)),
            include_state=bool(arguments.get("include_state", True)),
        )
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SelfReflectTool(AgentTool):
    spec = ToolSpec(
        name="self.reflect",
        description="Summarize what Kestrel knows about itself and the user from the Soul/self memory layer.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 12},
            },
        },
        risk="medium",
        capabilities=("self-model", "reflection", "soul"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(
            arguments.get("query") or "Kestrel identity capabilities user workflow preferences"
        ).strip()
        k = max(1, min(int(arguments.get("k", 6)), 12))
        hits = context.memory.retrieve(
            RetrievalQuery(query=query, layers=(MemoryLayer.SELF,), k_per_layer=k)
        )
        rows = [_memory_hit_payload(hit) for hit in hits[:k]]
        all_self_hits = [
            MemoryHit(
                record=record,
                score=1.0,
                source_backend="trusted_self_scan",
                frame_id=str(record.metadata.get("frame_id") or record.id),
            )
            for record in context.memory.iter_records(MemoryLayer.SELF)
        ]
        trusted_ids = trusted_onboarding_record_ids(
            all_self_hits,
            spec=context.memory.specs[MemoryLayer.SELF],
        )
        trusted_onboarding_rows = [
            _memory_hit_payload(hit) for hit in all_self_hits if hit.record.id in trusted_ids
        ]
        payload = {
            "identity": _self_identity(),
            "query": query,
            "self_memory_hits": rows,
            "trusted_onboarding_hits": trusted_onboarding_rows,
            "reflection": _self_reflection_text(rows),
        }
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SelfRememberTool(AgentTool):
    spec = ToolSpec(
        name="self.remember",
        description="Write a validated self-model record to the Soul/self .mv2 layer with provenance and nested-learning metadata.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "schema": {"type": "string"},
                "validation_status": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "source": {"type": "string"},
                "locator": {"type": "string"},
            },
            "required": ["title", "content", "schema", "validation_status"],
        },
        risk="medium",
        capabilities=("self-model", "memory-write", "soul"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        title = str(arguments.get("title", "")).strip()
        content = str(arguments.get("content", "")).strip()
        schema = str(arguments.get("schema", "")).strip()
        validation_status = str(arguments.get("validation_status", "")).strip()
        if not title:
            return self._result(call, success=False, content="Missing title", error="missing_title")
        if not content:
            return self._result(
                call, success=False, content="Missing content", error="missing_content"
            )
        if schema not in _SELF_SCHEMAS:
            return self._result(
                call,
                success=False,
                content=f"Unknown self schema: {schema}",
                error="invalid_self_schema",
            )
        if not validation_status:
            return self._result(
                call,
                success=False,
                content="Missing validation_status",
                error="missing_validation_status",
            )
        confidence = float(arguments.get("confidence", 0.82))
        importance = float(arguments.get("importance", 0.72))
        source = str(arguments.get("source") or "self.remember")
        locator = str(arguments.get("locator") or context.run_id or context.session_id)
        trusted_onboarding = (
            context.trusted_request_origin == TRUSTED_ONBOARDING_ORIGIN
            and schema == SELF_PROFILE_SCHEMA
            and validation_status == "user_confirmed"
            and source == TRUSTED_ONBOARDING_SOURCE
            and locator == TRUSTED_ONBOARDING_LOCATOR
        )
        source_record_ids: tuple[str, ...] = ()
        validation_evidence: ValidationEvidence | None = None
        candidate_id: str | None = None
        if trusted_onboarding:
            candidate_id = context.memory.put(
                MemoryRecord(
                    title=title,
                    content=content,
                    layer=MemoryLayer.EPISODIC,
                    kind=MemoryKind.FACT,
                    confidence=max(confidence, 0.5),
                    importance=importance,
                    metadata={
                        "frame_type": "section_summary",
                        "validation_status": "onboarding_candidate",
                        "self_schema": schema,
                        "trusted_request_origin": TRUSTED_ONBOARDING_ORIGIN,
                    },
                    evidence=[
                        EvidenceRef(
                            source=TRUSTED_ONBOARDING_ORIGIN,
                            locator=context.run_id or context.session_id,
                        )
                    ],
                )
            )
            receipt_id = context.memory.put_runtime_validation_receipt(
                tool_name=TRUSTED_ONBOARDING_SOURCE,
                tool_call_id=context.run_id or context.session_id,
                evidence_bucket="human",
                command=(
                    TRUSTED_ONBOARDING_PROVENANCE_SCHEMA,
                    TRUSTED_ONBOARDING_ORIGIN,
                    locator,
                ),
                output_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                session_id=context.session_id,
                run_id=context.run_id,
                subject_record_id=candidate_id,
            )
            onboarding_receipt = context.memory.get_record(MemoryLayer.EPISODIC, receipt_id)
            if onboarding_receipt is None:
                raise RuntimeError("Authenticated onboarding receipt was not persisted.")
            onboarding_receipt.metadata.update(
                {
                    "authenticated_onboarding_receipt": True,
                    "trusted_request_origin": TRUSTED_ONBOARDING_ORIGIN,
                }
            )
            context.memory.upsert(onboarding_receipt)
            source_record_ids = (candidate_id, receipt_id)
            validation_evidence = resolve_validation_evidence(
                ValidationEvidence(
                    task_refs=(EvidenceRef(source="memory_record", locator=receipt_id),),
                    human_explicit=True,
                ),
                status="human_confirmed",
                artifact_ids=(receipt_id,),
            )
        signal = LearningSignal(
            title=title,
            content=content,
            kind=MemoryKind.FACT,
            source_layer=MemoryLayer.EPISODIC,
            confidence=confidence,
            importance=importance,
            validation_score=None if validation_evidence is not None else confidence,
            validation_evidence=validation_evidence,
            repeat_count=1,
            explicit_instruction=trusted_onboarding,
            source=source,
            locator=locator,
            tags={"self_schema": schema},
            metadata={
                "self_schema": schema,
                "validation_status": validation_status,
                "provenance": source,
                "session_id": context.session_id,
                "run_id": context.run_id,
            },
            requested_target_layer=MemoryLayer.SELF,
        )
        kernel = NestedLearningKernel()
        decision = kernel.decide(signal, action="write")
        if not decision.accepted:
            return self._result(
                call,
                success=False,
                content=decision.reason,
                data=decision.to_payload(),
                error="self_memory_rejected",
            )
        record = kernel.to_memory_record(signal, decision)
        record.metadata.update(
            {
                "self_schema": schema,
                "validation_status": validation_status,
                "provenance": source,
                "frame_type": "self_model",
            }
        )
        if trusted_onboarding:
            if candidate_id is None:
                raise RuntimeError("Trusted onboarding candidate was not persisted.")
            record.metadata["onboarding_provenance"] = {
                "schema": TRUSTED_ONBOARDING_PROVENANCE_SCHEMA,
                "origin": TRUSTED_ONBOARDING_ORIGIN,
                "source": TRUSTED_ONBOARDING_SOURCE,
                "locator": TRUSTED_ONBOARDING_LOCATOR,
            }
            record.evidence.append(
                EvidenceRef(source="memory_record", locator=candidate_id)
            )
        try:
            record_id = context.memory.put_validated(
                record,
                authority="nested_learning",
                source_record_ids=source_record_ids,
                validation_evidence=validation_evidence,
            )
            context.memory.seal_all()
        except Exception as exc:  # noqa: BLE001 - tool boundary
            return self._result(
                call,
                success=False,
                content=str(exc),
                data=decision.to_payload(),
                error="self_remember_failed",
            )
        payload = {"record_id": record_id, "decision": decision.to_payload(), "self_schema": schema}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SelfProposeChangeTool(AgentTool):
    spec = ToolSpec(
        name="self.propose_change",
        description="Record an approval-gated self-change request for Kestrel without applying code changes directly.",
        parameters={
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["request"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("self-modification", "safe-repair", "soul"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        request = str(arguments.get("request", "")).strip()
        if not request:
            return self._result(
                call, success=False, content="Missing request", error="missing_request"
            )
        payload = {
            "request": request,
            "rationale": str(arguments.get("rationale", "")).strip(),
            "target_workspace": str(context.workspace),
            "required_gates": [
                "repair.prepare",
                "repair.apply_patch",
                "repair.validate",
                "repair.review",
                "git.commit",
            ],
            "approval_required_before_execution": True,
            "push_or_merge_allowed": False,
        }
        proposal = MemoryRecord(
            title="Self-change request",
            content=json.dumps(payload, indent=2),
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.EVENT,
            confidence=0.86,
            importance=0.85,
            metadata={
                "frame_type": "trace_stub",
                "self_schema": "self_change_request",
                "validation_status": "operator_requested",
                "approval_gated": True,
                "session_id": context.session_id,
                "run_id": context.run_id,
            },
            evidence=[EvidenceRef(source="self.propose_change", locator=call.id)],
        )
        try:
            record_id = context.memory.put(proposal)
            context.memory.seal_all()
        except Exception as exc:  # noqa: BLE001 - tool boundary
            return self._result(
                call,
                success=False,
                content=str(exc),
                data=payload,
                error="self_change_proposal_failed",
            )
        data = {
            **payload,
            "memory_record_id": record_id,
            "memory_layer": MemoryLayer.EPISODIC.value,
            "memory_error": None,
        }
        return self._result(
            call,
            success=True,
            content=json.dumps(data, indent=2),
            data=data,
        )


def build_default_tools(enabled_names: tuple[str, ...] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    enabled = {name.strip() for name in enabled_names or () if name.strip()}

    def register(tool: AgentTool) -> None:
        if enabled and tool.spec.name not in enabled:
            return
        registry.register(tool)

    register(ToolRegistryTool())
    register(SkillDiscoverTool())
    register(SkillInspectTool())
    register(PluginRegistryTool())
    register(McpRegistryTool())
    register(ProjectScriptsTool())
    register(DiagnosisClassifyTool())
    register(DiagnosisRecallTool())
    register(RepairPrepareTool())
    register(RepairStatusTool())
    register(RepairApplyPatchTool())
    register(RepairValidateTool())
    register(RepairOrchestrateValidateTool())
    register(RepairReviewTool())
    register(RepairRollbackTool())
    register(SelfInspectTool())
    register(SelfReflectTool())
    register(SelfRememberTool())
    register(SelfProposeChangeTool())
    register(WebSearchTool())
    register(WebFetchTool())
    register(MemorySearchTool())
    register(MemoryWriteTool())
    register(ContextPackTool())
    register(ContextExpandTool())
    register(CapsuleSummarizeTool())
    register(CapsuleApplyTool())
    register(MemoryConflictsTool())
    register(ListFilesTool())
    register(ReadFileTool())
    register(FindFilesTool())
    register(FileStatTool())
    register(WriteFileTool())
    register(ShellRunTool())
    register(CodexExecTool())
    register(RepoSearchTool())
    register(RepoMapTool())
    register(PatchApplyTool())
    register(TestRunTool())
    register(LintRunTool())
    register(GitStatusTool())
    register(GitDiffTool())
    register(GitExportPatchTool())
    register(GitBranchTool())
    register(GitCreateLocalBranchTool())
    register(GitLogTool())
    register(GitShowTool())
    register(GitCommitTool())
    register(MemvidVerifyTool())
    register(MemvidDoctorTool())
    register(MemvidStatsTool())
    register(MemoryLedgerTool())
    register(MemoryLearnTool())
    register(MemoryPolicyPromoteTool())
    register(MemoryConsolidateTool())
    register(MemoryCorrectTool())
    register(MemoryCompactTool())
    register(MemoryInspectTool())
    register(MemoryExportTool())
    register(MemoryImportTool())
    register(SkillInstallTool())
    register(PluginReviewTool())
    register(PluginInstallTool())
    return registry


_SELF_SCHEMAS = {
    "identity_summary",
    "capability_snapshot",
    "user_profile",
    "agent_persona",
    "user_workflow_preference",
    "self_change_request",
    "validation_metadata",
}


def _self_identity() -> dict[str, str]:
    return {
        "name": "Kestrel",
        "display_name": "Soul",
        "description": "A local-first, memory-native engineering agent runtime with nested .mv2 memory layers.",
    }


def _self_snapshot(
    context: ToolContext, *, include_tools: bool, include_state: bool
) -> dict[str, Any]:
    config = context.config
    state = AgentStateStore(config.state_path)
    secret_broker = build_secret_broker(config.secret_store_path, backend=config.secret_backend)
    memory_layers = [
        {
            "layer": layer.value,
            "mv2_file": spec.mv2_file,
            "description": spec.description,
            "update_cadence": spec.update_cadence,
            "min_write_confidence": spec.min_write_confidence,
            "promotion_threshold": spec.promotion_threshold,
        }
        for layer, spec in context.memory.iter_layers()
    ]
    payload: dict[str, Any] = {
        "identity": _self_identity(),
        "provider": {
            "provider": config.provider,
            "model": config.model,
            "fallback_provider": config.fallback_provider,
            "fallback_model": config.fallback_model,
            "base_url_configured": bool(config.base_url),
            "api_key_env": config.api_key_env,
            "api_key_configured": bool(config.api_key_env and os.getenv(config.api_key_env)),
        },
        "config": {
            "backend": config.backend,
            "workspace": str(config.workspace),
            "memory_dir": str(config.memory_dir),
            "secret_store_path": str(config.secret_store_path),
            "secret_backend": config.secret_backend,
            "allow_shell": config.allow_shell,
            "allow_file_write": config.allow_file_write,
            "allow_policy_writes": config.allow_policy_writes,
            "allow_codex_cli": config.allow_codex_cli,
            "allow_plugin_install": config.allow_plugin_install,
            "allow_git_commit": config.allow_git_commit,
            "allow_git_push": config.allow_git_push,
            "allow_remote_mutation": config.allow_remote_mutation,
            "git_write_mode": config.git_write_mode,
            "protected_branches": list(config.protected_branches),
            "allow_memory_import": config.allow_memory_import,
            "allow_executable_skills": config.allow_executable_skills,
            "allow_mcp_network_endpoints": config.allow_mcp_network_endpoints,
            "allow_web": config.allow_web,
            "allow_self_modification": config.allow_self_modification,
            "require_approval_for_high_risk_tools": config.require_approval_for_high_risk_tools,
            "approval_ttl_seconds": config.approval_ttl_seconds,
            "web_backend": config.web_backend,
            "web_timeout_seconds": config.web_timeout_seconds,
            "web_max_results": config.web_max_results,
            "web_max_bytes": config.web_max_bytes,
        },
        "memory_layers": memory_layers,
    }
    if include_tools:
        payload["tools"] = [spec.to_public_dict() for spec in build_default_tools().specs()]
    else:
        payload["tool_count"] = len(build_default_tools().specs())
    if include_state:
        payload["skills"] = _safe_state_list(state.list_skills)
        payload["plugins"] = _safe_state_list(state.list_plugins)
        payload["mcp_servers"] = [
            _redact_mcp_server(server, secret_broker)
            for server in _safe_state_list(state.list_mcp_servers)
        ]
    return payload


def _safe_state_list(loader: Any) -> list[dict[str, Any]]:
    try:
        rows = loader()
    except Exception:
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _redact_mcp_server(server: dict[str, Any], secret_broker: SecretBroker) -> dict[str, Any]:
    safe = dict(server)
    safe.pop("env", None)
    secret_env = safe.pop("secret_env", {})
    if isinstance(secret_env, dict):
        safe["secret_env_status"] = {
            str(key): {
                "env": str(value),
                "secret_ref": str(value) if is_secret_ref(str(value)) else None,
                "configured": bool(secret_broker.resolve(str(value))),
            }
            for key, value in secret_env.items()
        }
    return safe


def _self_reflection_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No validated Soul/self memory matched the query yet."
    titles = [
        str(row.get("record", {}).get("title") or row.get("title") or "self memory")
        for row in rows[:5]
    ]
    return "Relevant Soul/self memory: " + "; ".join(titles)


def _safe_skill_id(skill_id: str) -> bool:
    return skill_id.replace("_", "-").replace("-", "").isalnum()


def _memory_hit_payload(hit: Any) -> dict[str, object]:
    return {
        "score": hit.score,
        "frame_id": hit.frame_id,
        "source_backend": hit.source_backend,
        "snippet": hit.snippet,
        "record": _memory_record_payload(hit.record),
    }


def _memory_record_payload(record: MemoryRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "layer": record.layer.value,
        "kind": record.kind.value,
        "title": record.title,
        "content": record.content,
        "confidence": record.confidence,
        "importance": record.importance,
        "tags": record.tags,
        "metadata": record.metadata,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "content_hash": record.content_hash,
        "evidence": [
            {"source": evidence.source, "locator": evidence.locator, "quote": evidence.quote}
            for evidence in record.evidence
        ],
    }


_RUNTIME_TRANSCRIPT_AUTHORITY_FIELDS = frozenset(
    {
        "channel",
        "channel_id",
        "channel_message_id",
        "channel_metadata",
        "channel_user_id",
        "conversation_id",
        "frame_id",
        "frame_type",
        "runtime_source_uri",
        "session_id",
        "source_span",
        "source_uri",
        "transcript_scope",
        "turn_origin",
        "authenticated_onboarding_receipt",
        "cognition_schema",
        "nested_learning",
        "onboarding_provenance",
        "promotion_evidence_receipt",
        "promotion_id",
        "promotion_status",
        "stable_write_envelope",
        "validation_evidence",
        "validation_method",
        "validation_status",
    }
)


def _memory_record_from_payload(item: dict[str, Any], *, imported: bool = False) -> MemoryRecord:
    metadata = dict(item.get("metadata", {})) if isinstance(item.get("metadata"), dict) else {}
    if imported:
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in _RUNTIME_TRANSCRIPT_AUTHORITY_FIELDS
        }
        # Imports are always untrusted memory data, even when an operator approves the
        # exact call. Approval authorizes the write; it does not authenticate runtime
        # transcript provenance supplied by the payload.
        metadata["memory_imported"] = True
        metadata["import_trust"] = "untrusted_data"
    record = MemoryRecord(
        layer=MemoryLayer(str(item.get("layer", MemoryLayer.WORKING.value))),
        kind=MemoryKind(str(item.get("kind", MemoryKind.OBSERVATION.value))),
        title=str(item.get("title", "Imported memory")),
        content=str(item.get("content", "")),
        confidence=float(item.get("confidence", 0.8)),
        importance=float(item.get("importance", 0.5)),
        tags=dict(item.get("tags", {})) if isinstance(item.get("tags"), dict) else {},
        metadata=metadata,
    )
    record_id = str(item.get("id") or "").strip()
    if record_id:
        record.id = record_id
    return record


def _stage_untrusted_import(record: MemoryRecord) -> MemoryRecord:
    if record.layer not in STABLE_MEMORY_LAYERS:
        return record
    requested_layer = record.layer
    return replace(
        record,
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.EVENT,
        metadata={
            **record.metadata,
            "import_requested_layer": requested_layer.value,
            "validation_status": "untrusted_import_staged",
            "stable_recall_eligible": False,
        },
        evidence=[
            *record.evidence,
            EvidenceRef(
                source="memory.import",
                locator=record.id,
                quote=f"Requested {requested_layer.value} record staged as untrusted episodic data.",
            ),
        ],
    )


def _layer_arg(value: object) -> MemoryLayer | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return MemoryLayer(text)


def _datetime_arg(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _outcome_arg(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text not in OUTCOME_KINDS:
        raise ValueError(f"Unknown promotion outcome: {text}")
    return text


def _layers_arg(value: object) -> tuple[MemoryLayer, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    layers: list[MemoryLayer] = []
    for item in value:
        text = str(item).strip()
        if text:
            layers.append(MemoryLayer(text))
    return tuple(layers) if layers else None


def _find_memory_by_id(context: ToolContext, lookup_id: str) -> Any | None:
    for backend in context.memory.backends.values():
        records = getattr(backend, "records", None)
        if isinstance(records, list):
            for record in records:
                metadata = getattr(record, "metadata", {})
                if record.id == lookup_id or str(metadata.get("frame_id", "")) == lookup_id:
                    return type("_Hit", (), {"record": record})()
    hits = context.memory.retrieve(RetrievalQuery(query=lookup_id, k_per_layer=5))
    for hit in hits:
        metadata = hit.record.metadata
        if (
            hit.record.id == lookup_id
            or str(metadata.get("frame_id", "")) == lookup_id
            or hit.frame_id == lookup_id
        ):
            return hit
    return None


def _stable_signal_source_record_ids(signal: LearningSignal) -> tuple[str, ...]:
    evidence = signal.validation_evidence
    if evidence is None:
        return ()
    return tuple(
        dict.fromkeys(
            ref.locator.strip()
            for ref in evidence.all_refs()
            if ref.source.strip() == "memory_record" and ref.locator.strip()
        )
    )


def _resolve_runtime_validation_evidence(
    evidence: ValidationEvidence,
    *,
    context: ToolContext,
    expected_subject_record_id: str | None,
) -> tuple[ValidationEvidence, tuple[str, ...], list[str]]:
    """Resolve current-run receipts bound to one exact durable claim candidate."""

    unresolved: list[str] = []
    artifact_ids: list[str] = []
    expected_subject_id = (expected_subject_record_id or "").strip()
    for bucket, refs in (
        ("test", evidence.test_refs),
        ("lint", evidence.lint_refs),
        ("repair", evidence.repair_refs),
        ("review", evidence.review_refs),
        (None, evidence.task_refs),
    ):
        for ref in refs:
            source = ref.source.strip()
            locator = ref.locator.strip()
            label = f"{source}:{locator}"
            if source != "memory_record" or not locator:
                unresolved.append(label)
                continue
            record = context.memory.get_record(None, locator, include_inactive=False)
            if record is None or not context.memory.is_authenticated_validation_receipt(
                record,
                evidence_bucket=bucket,
                require_subject_binding=True,
            ):
                unresolved.append(label)
                continue
            binding = context.memory.validation_receipt_subject(record)
            if binding is None or binding[0] != expected_subject_id:
                unresolved.append(label)
                continue
            receipt_session_id = binding[2]
            receipt_run_id = binding[3]
            if (
                (context.run_id is not None and receipt_run_id != context.run_id)
                or (
                    context.run_id is None
                    and (receipt_run_id is not None or receipt_session_id != context.session_id)
                )
            ):
                unresolved.append(label)
                continue
            artifact_ids.append(record.id)
    if unresolved or not artifact_ids:
        return evidence, (), sorted(set(unresolved or ["no_authenticated_runtime_receipts"]))
    runtime_evidence = replace(evidence, human_explicit=False)
    resolved = resolve_validation_evidence(
        runtime_evidence,
        status="runtime_validated",
        artifact_ids=tuple(dict.fromkeys(artifact_ids)),
    )
    return resolved, tuple(dict.fromkeys(artifact_ids)), []


def _resolve_capsule_summary_evidence(summary: Any, *, context: ToolContext) -> Any:
    signals: list[LearningSignal] = []
    for signal in summary.learning_signals:
        evidence = signal.validation_evidence
        if evidence is None:
            signals.append(signal)
            continue
        expected_subject_id = str(
            (signal.metadata or {}).get("source_record_id") or ""
        ).strip()
        resolved, _, _ = _resolve_runtime_validation_evidence(
            evidence,
            context=context,
            expected_subject_record_id=expected_subject_id or None,
        )
        signals.append(
            replace(
                signal,
                validation_evidence=resolved,
                validation_score=None,
            )
        )
    return replace(summary, learning_signals=tuple(signals))


def _resolve_policy_evidence(
    evidence: ValidationEvidence,
    *,
    context: ToolContext,
    subject_record_id: str | None = None,
) -> tuple[
    ValidationEvidence,
    tuple[str, ...],
    dict[str, dict[str, str]],
    list[str],
]:
    unresolved: list[str] = []
    artifact_ids: list[str] = []
    source_record_ids: list[str] = []
    artifact_bindings: dict[str, dict[str, str]] = {}
    resolved_refs: dict[str, list[EvidenceRef]] = {
        "test": [],
        "lint": [],
        "repair": [],
        "review": [],
        "task": [],
    }
    for bucket, refs in (
        ("test", evidence.test_refs),
        ("lint", evidence.lint_refs),
        ("repair", evidence.repair_refs),
        ("review", evidence.review_refs),
        ("task", evidence.task_refs),
    ):
        for ref in refs:
            source = ref.source.strip()
            locator = ref.locator.strip()
            label = f"{source}:{locator}"
            if not source or not locator:
                unresolved.append(label)
                continue
            if source != "memory_record":
                # Raw signed repair artifacts prove that a tool ran, but they do
                # not bind that run to this exact policy claim.  Only the
                # runtime's HMAC receipt can supply claim/run/bucket binding.
                unresolved.append(label)
                continue
            record = context.memory.get_record(None, locator, include_inactive=False)
            if record is None or not _record_is_resolved_promotion_evidence(
                record,
                context=context,
                expected_subject_record_id=subject_record_id,
                expected_evidence_bucket=bucket,
            ):
                unresolved.append(label)
                continue
            payload = record.metadata.get("validation_receipt_payload")
            if not isinstance(payload, dict):
                unresolved.append(label)
                continue
            source_record_ids.append(record.id)
            artifact_ids.append(record.id)
            resolved_refs[bucket].append(ref)
            artifact_bindings[record.id] = {
                "source": str(record.metadata.get("signed_artifact_source") or ""),
                "locator": str(record.metadata.get("signed_artifact_locator") or ""),
                "evidence_bucket": str(payload.get("evidence_bucket") or ""),
            }
    if unresolved:
        return evidence, (), {}, sorted(set(unresolved))
    resolved_payload = ValidationEvidence(
        test_refs=tuple(resolved_refs["test"]),
        lint_refs=tuple(resolved_refs["lint"]),
        repair_refs=tuple(resolved_refs["repair"]),
        review_refs=tuple(resolved_refs["review"]),
        task_refs=tuple(resolved_refs["task"]),
        human_explicit=evidence.human_explicit,
        source_evidence_chars=evidence.source_evidence_chars,
    )
    resolved = resolve_validation_evidence(
        resolved_payload,
        status="operator_approved",
        artifact_ids=tuple(dict.fromkeys(artifact_ids)),
    )
    return resolved, tuple(dict.fromkeys(source_record_ids)), artifact_bindings, []


def _record_is_resolved_promotion_evidence(
    record: MemoryRecord,
    *,
    context: ToolContext,
    expected_subject_record_id: str | None,
    expected_evidence_bucket: str,
) -> bool:
    if not record.evidence:
        return False
    metadata = record.metadata
    source = str(metadata.get("signed_artifact_source") or "").strip()
    locator = str(metadata.get("signed_artifact_locator") or "").strip()
    if context.memory.is_authenticated_validation_receipt(
        record,
        require_subject_binding=True,
    ):
        binding = context.memory.validation_receipt_subject(record)
        expected_subject_id = (expected_subject_record_id or "").strip()
        payload = record.metadata.get("validation_receipt_payload")
        if not isinstance(payload, dict):
            return False
        receipt_bucket = str(payload.get("evidence_bucket") or "")
        tool_name = str(payload.get("tool_name") or "")
        if expected_evidence_bucket != "task" and receipt_bucket != expected_evidence_bucket:
            return False
        allowed_tools = {
            "test": frozenset({"test.run"}),
            "lint": frozenset({"lint.run"}),
            "repair": frozenset({"repair.validate", "repair.orchestrate_validate"}),
            "review": frozenset({"repair.review"}),
        }
        if receipt_bucket not in allowed_tools or tool_name not in allowed_tools[receipt_bucket]:
            return False
        artifact_pair_valid = bool(source) == bool(locator)
        if receipt_bucket == "repair":
            artifact_pair_valid = (
                source == "repair.validate"
                and bool(locator)
                and _signed_promotion_artifact_is_valid(
                    source,
                    locator,
                    workspace=context.workspace,
                )
            )
        elif receipt_bucket == "review":
            artifact_pair_valid = (
                source == "repair.review"
                and bool(locator)
                and _signed_promotion_artifact_is_valid(
                    source,
                    locator,
                    workspace=context.workspace,
                )
            )
        elif source or locator:
            artifact_pair_valid = False
        return bool(
            binding is not None
            and binding[0] == expected_subject_id
            and (
                (context.run_id is not None and binding[3] == context.run_id)
                or (
                    context.run_id is None
                    and binding[3] is None
                    and binding[2] == context.session_id
                )
            )
            and artifact_pair_valid
        )
    # Legacy unsigned memory receipts were not bound to the promoted claim and
    # therefore remain audit-only.  They cannot authorize a policy write.
    return False


def _signed_promotion_artifact_is_valid(
    source: str,
    locator: str,
    *,
    workspace: Any,
) -> bool:
    try:
        if source == "repair.validate":
            return load_validation_receipt(workspace, locator).get("success") is True
        if source == "repair.review":
            receipt = load_review_receipt(workspace, locator)
            validation = receipt.get("validation")
            commit_gate = receipt.get("commit_gate")
            return bool(
                isinstance(validation, dict)
                and validation.get("success") is True
                and isinstance(commit_gate, dict)
                and commit_gate.get("commit_allowed") is True
            )
    except (FileNotFoundError, ValueError):
        return False
    return False


def _related_frames(
    context: ToolContext,
    frame: Any,
    *,
    include_children: bool,
    include_parents: bool,
) -> list[dict[str, object]]:
    wanted: set[str] = set()
    if include_children:
        wanted.update(frame.child_ids)
    if include_parents:
        wanted.update(frame.parent_ids)
    if not wanted:
        return []
    related: list[dict[str, object]] = []
    for item_id in sorted(wanted):
        hit = _find_memory_by_id(context, item_id)
        if hit is None:
            related.append({"id": item_id, "found": False})
            continue
        record = hit.record
        related.append(
            {
                "id": item_id,
                "found": True,
                "title": record.title,
                "layer": record.layer.value,
                "kind": record.kind.value,
                "snippet": record.content[:500],
            }
        )
    return related


def _truncate_by_tokens(text: str, max_tokens: int) -> str:
    max_chars = max(max_tokens * 4, 0)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[TRUNCATED_BY_CONTEXT_EXPAND]"


def _capsule_apply_plan(
    summary: Any, *, context: ToolContext, include_policy: bool
) -> list[dict[str, object]]:
    kernel = NestedLearningKernel()
    plan: list[dict[str, object]] = []
    for index, signal in enumerate(summary.learning_signals):
        decision = kernel.decide(signal)
        payload = decision.to_payload()
        payload["signal_index"] = index
        payload["signal_title"] = signal.title
        payload["signal_kind"] = signal.kind.value
        payload["requested_target_layer"] = (
            signal.requested_target_layer.value if signal.requested_target_layer else None
        )
        payload["will_write"] = False
        if not decision.accepted or decision.target_layer is None:
            staged_record = capsule_signal_staging_record(signal)
            if staged_record is not None:
                if _memory_has_content_hash(
                    context,
                    MemoryLayer.EPISODIC,
                    staged_record.content_hash,
                ):
                    payload["blocked"] = "duplicate_content_hash"
                else:
                    payload.update(
                        {
                            "will_write": True,
                            "write_mode": "unvalidated_episodic_staging",
                            "actual_layer": MemoryLayer.EPISODIC.value,
                            "requested_stable_layer": staged_record.metadata[
                                "requested_stable_layer"
                            ],
                            "validation_status": "unresolved",
                            "stable_promotion_blocked": "authenticated_validation_required",
                        }
                    )
            else:
                payload["blocked"] = (
                    "policy_requires_explicit_instruction"
                    if signal.requested_target_layer == MemoryLayer.POLICY
                    and not signal.explicit_instruction
                    else "nested_learning_rejected"
                )
        elif decision.target_layer == MemoryLayer.POLICY:
            payload["blocked"] = (
                "policy_excluded_from_capsule_apply"
                if not include_policy
                else "policy_requires_dedicated_exact_call_approval"
            )
        else:
            record = kernel.to_memory_record(signal, decision)
            if _memory_has_content_hash(context, decision.target_layer, record.content_hash):
                payload["blocked"] = "duplicate_content_hash"
            else:
                payload["will_write"] = True
        plan.append(payload)
    return plan


def _memory_has_content_hash(context: ToolContext, layer: MemoryLayer, content_hash: str) -> bool:
    backend = context.memory.backends.get(layer)
    records = getattr(backend, "records", None)
    if isinstance(records, list):
        return any(getattr(record, "content_hash", None) == content_hash for record in records)
    hits = context.memory.retrieve(
        RetrievalQuery(query=content_hash, layers=(layer,), k_per_layer=3)
    )
    return any(
        hit.record.content_hash == content_hash
        or hit.record.metadata.get("content_hash") == content_hash
        for hit in hits
    )
