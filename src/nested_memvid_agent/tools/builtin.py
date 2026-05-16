from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..consolidation import Consolidator
from ..context_frames import default_frame_type_for_memory, estimate_tokens, from_memory_record
from ..context_packer import ContextPacker, ContextPackRequest
from ..diagnosis import classify_failure
from ..models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from ..nested_learning import LearningSignal, NestedLearningKernel
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..task_capsule import summarize_run_capsule
from .base import AgentTool, ToolContext
from .registry import ToolRegistry


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
            layers = tuple(MemoryLayer(value) for value in layer_values) if isinstance(layer_values, list) else tuple(MemoryLayer)
        except ValueError as exc:
            return self._result(
                call,
                success=False,
                content=f"Unknown memory layer: {exc}",
                error="invalid_tool_arguments",
            )
        k = int(arguments.get("k", 8))
        hits = context.memory.retrieve(RetrievalQuery(query=query, layers=layers, k_per_layer=k))
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
        description="Write a validated memory record to a nested memory layer. Policy writes require config enablement.",
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
        try:
            frame_type = str(arguments.get("frame_type") or default_frame_type_for_memory(kind, layer))
            parent_ids_arg = arguments.get("parent_ids")
            child_ids_arg = arguments.get("child_ids")
            parent_ids = [str(item) for item in parent_ids_arg] if isinstance(parent_ids_arg, list) else []
            child_ids = [str(item) for item in child_ids_arg] if isinstance(child_ids_arg, list) else []
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
            return self._result(call, success=True, content=f"Wrote memory {record_id}", data={"record_id": record_id})
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
                    token_budget=int(arguments.get("token_budget", context.config.context_pack_token_budget)),
                    allowed_layers=layers,
                    expand_raw=bool(arguments.get("expand_raw", context.config.context_pack_expand_raw)),
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
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
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
            return self._result(call, success=False, content="Missing frame_id or record_id", error="missing_id")
        try:
            hit = _find_memory_by_id(context, lookup_id)
            if hit is None:
                return self._result(call, success=False, content=f"No memory found for {lookup_id}", error="not_found")
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
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="context_expand_failed")


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
            return self._result(call, success=False, content="Missing run_id", error="missing_run_id")
        try:
            summary = summarize_run_capsule(
                runs_dir=context.config.memory_dir.parent / "runs",
                run_id=run_id,
                backend=context.config.backend,
            )
            kernel = NestedLearningKernel()
            decisions = []
            for signal in summary.learning_signals:
                decision = kernel.decide(signal)
                payload = decision.to_payload()
                payload["signal_title"] = signal.title
                payload["dry_run"] = True
                decisions.append(payload)
            payload = {**summary.to_payload(), "dry_run": True, "nested_learning_decisions": decisions}
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="capsule_summarize_failed")


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
        capabilities=("task-capsule", "nested-learning", "memory-write"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        public_arguments = {key: value for key, value in arguments.items() if not str(key).startswith("_")}
        call_id = str(arguments.get("_tool_call_id") or "")
        call = (
            ToolCall(name=self.spec.name, arguments=public_arguments, id=call_id)
            if call_id
            else ToolCall(name=self.spec.name, arguments=public_arguments)
        )
        run_id = str(arguments.get("run_id", "")).strip()
        if not run_id:
            return self._result(call, success=False, content="Missing run_id", error="missing_run_id")
        dry_run = bool(arguments.get("dry_run", False))
        include_policy = bool(arguments.get("include_policy", False))
        try:
            summary = summarize_run_capsule(
                runs_dir=context.config.memory_dir.parent / "runs",
                run_id=run_id,
                backend=context.config.backend,
            )
            plan = _capsule_apply_plan(summary, context=context, include_policy=include_policy)
            if dry_run:
                payload = {**summary.to_payload(), "dry_run": True, "applied": False, "decisions": plan}
                return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)

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

            if context.config.require_approval_for_high_risk_tools and call.id not in context.approved_tool_call_ids:
                if context.approval_handler is not None:
                    return context.approval_handler(call, self.spec, context)
                return self._result(
                    call,
                    success=False,
                    content="Capsule apply requires approval before writing memory.",
                    data={"status": "approval_required"},
                    error="approval_required",
                )

            wrote = False
            for item in plan:
                if item.get("will_write") is not True:
                    continue
                signal_index = item.get("signal_index")
                if not isinstance(signal_index, int):
                    continue
                signal = summary.learning_signals[signal_index]
                decision = NestedLearningKernel().decide(signal)
                if decision.target_layer is None:
                    continue
                record = NestedLearningKernel().to_memory_record(signal, decision)
                if _memory_has_content_hash(context, record.layer, record.content_hash):
                    item["skipped"] = "duplicate_content_hash"
                    item["will_write"] = False
                    continue
                item["record_id"] = context.memory.put(record)
                wrote = True
            if wrote:
                context.memory.seal_all()
            payload = {**summary.to_payload(), "dry_run": False, "applied": wrote, "decisions": plan}
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
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
            hits = context.memory.retrieve(RetrievalQuery(query=query, layers=layers, k_per_layer=k))
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
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="memory_conflicts_failed")


class ListFilesTool(AgentTool):
    spec = ToolSpec(
        name="file.list",
        description="List files under the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            path = _safe_path(context.workspace, str(arguments.get("path", ".")))
            max_entries = int(arguments.get("max_entries", 80))
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:max_entries]
            data = [{"name": p.name, "type": "dir" if p.is_dir() else "file"} for p in entries]
            return self._result(call, success=True, content=json.dumps(data, indent=2), data={"entries": data})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_list_failed")


class ReadFileTool(AgentTool):
    spec = ToolSpec(
        name="file.read",
        description="Read a text file under the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}},
            "required": ["path"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            path = _safe_path(context.workspace, str(arguments.get("path", "")))
            max_chars = int(arguments.get("max_chars", 20_000))
            text = path.read_text(errors="replace")[:max_chars]
            return self._result(call, success=True, content=text, data={"path": str(path), "chars": len(text)})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_read_failed")


class WriteFileTool(AgentTool):
    spec = ToolSpec(
        name="file.write",
        description="Write a text file under workspace. Disabled unless allow_file_write is true.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            path = _safe_path(context.workspace, str(arguments.get("path", "")))
            path.parent.mkdir(parents=True, exist_ok=True)
            text = str(arguments.get("content", ""))
            path.write_text(text)
            return self._result(call, success=True, content=f"Wrote {path}", data={"path": str(path), "chars": len(text)})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_write_failed")


class ShellRunTool(AgentTool):
    spec = ToolSpec(
        name="shell.run",
        description="Run an allowlisted shell command in the workspace. Disabled unless allow_shell is true.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "integer"}},
            "required": ["command"],
        },
        risk="high",
        requires_approval=True,
    )
    allowed_first_tokens = {"echo", "pwd", "python", "python3", "pytest", "ruff", "mypy", "ls", "cat"}

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
            return self._result(call, success=False, content="command must be list[str]", error="bad_command")
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(call, success=False, content="Command is not allowlisted", error="command_not_allowlisted")
        try:
            completed = subprocess.run(  # noqa: S603 - intentionally allowlisted
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=int(arguments.get("timeout", 30)),
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode},
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="shell_failed")


class CodexExecTool(AgentTool):
    spec = ToolSpec(
        name="codex.exec",
        description="Delegate a bounded non-interactive task to the local Codex CLI in this workspace.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "model": {"type": "string"},
                "sandbox": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write"],
                    "default": "read-only",
                },
                "timeout": {"type": "integer", "minimum": 30, "maximum": 3600},
                "ephemeral": {"type": "boolean", "default": True},
                "json_events": {"type": "boolean", "default": False},
                "skip_git_repo_check": {"type": "boolean", "default": False},
                "max_output_chars": {"type": "integer", "minimum": 1000, "maximum": 100000},
            },
            "required": ["prompt"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("codex-cli", "delegation"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return self._result(call, success=False, content="Missing prompt", error="missing_prompt")

        sandbox = str(arguments.get("sandbox", "read-only"))
        if sandbox not in {"read-only", "workspace-write"}:
            return self._result(call, success=False, content="Unsupported Codex sandbox", error="bad_sandbox")

        timeout = max(30, min(int(arguments.get("timeout", 600)), 3600))
        max_output_chars = max(1000, min(int(arguments.get("max_output_chars", 40_000)), 100_000))
        command = [
            "codex",
            "exec",
            "--cd",
            str(context.workspace.resolve()),
            "--sandbox",
            sandbox,
            "--color",
            "never",
        ]
        model = str(arguments.get("model", "")).strip()
        if model:
            command.extend(["--model", model])
        if bool(arguments.get("ephemeral", True)):
            command.append("--ephemeral")
        if bool(arguments.get("json_events", False)):
            command.append("--json")
        if bool(arguments.get("skip_git_repo_check", False)):
            command.append("--skip-git-repo-check")
        command.append(prompt)

        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable and argument vector
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout = _truncate(completed.stdout, max_output_chars)
            stderr = _truncate(completed.stderr, max_output_chars)
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "returncode": completed.returncode,
                    "sandbox": sandbox,
                    "model": model or None,
                    "stdout_truncated": len(completed.stdout) > max_output_chars,
                    "stderr_truncated": len(completed.stderr) > max_output_chars,
                },
                error=None if completed.returncode == 0 else "codex_nonzero_exit",
            )
        except FileNotFoundError:
            return self._result(call, success=False, content="Codex CLI not found on PATH.", error="codex_cli_not_found")
        except subprocess.TimeoutExpired as exc:
            return self._result(call, success=False, content=f"Codex CLI timed out after {timeout}s: {exc}", error="codex_timeout")
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="codex_cli_failed")


class RepoSearchTool(AgentTool):
    spec = ToolSpec(
        name="repo.search",
        description="Search text files under the configured workspace root without leaving the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_file_bytes": {"type": "integer", "minimum": 256, "maximum": 1000000},
            },
            "required": ["query"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            root = _safe_path(context.workspace, str(arguments.get("path", ".")))
            max_results = int(arguments.get("max_results", 25))
            max_file_bytes = int(arguments.get("max_file_bytes", 300_000))
            rows: list[dict[str, object]] = []
            query_lower = query.lower()
            for path in _iter_repo_files(root, context.workspace, max_file_bytes=max_file_bytes):
                rel = path.relative_to(context.workspace.resolve())
                try:
                    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
                        if query_lower in line.lower():
                            rows.append({"path": str(rel), "line": lineno, "text": line[:400]})
                            if len(rows) >= max_results:
                                return self._result(
                                    call,
                                    success=True,
                                    content=json.dumps(rows, indent=2),
                                    data={"matches": rows},
                                )
                except UnicodeDecodeError:
                    continue
            return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"matches": rows})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repo_search_failed")


class RepoMapTool(AgentTool):
    spec = ToolSpec(
        name="repo.map",
        description="Return a bounded file tree for the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 500},
                "max_depth": {"type": "integer", "minimum": 0, "maximum": 8},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            root = _safe_path(context.workspace, str(arguments.get("path", ".")))
            workspace = context.workspace.resolve()
            max_entries = int(arguments.get("max_entries", 120))
            max_depth = int(arguments.get("max_depth", 3))
            rows: list[dict[str, object]] = []
            for current_root, dirs, files in os.walk(root):
                current = Path(current_root)
                rel_current = current.relative_to(workspace)
                depth = 0 if str(rel_current) == "." else len(rel_current.parts)
                dirs[:] = sorted(d for d in dirs if not _skip_repo_name(d))
                if depth > max_depth:
                    dirs[:] = []
                    continue
                for dirname in dirs:
                    rows.append({"path": str((current / dirname).relative_to(workspace)), "type": "dir"})
                    if len(rows) >= max_entries:
                        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
                for filename in sorted(files):
                    if _skip_repo_name(filename):
                        continue
                    item = current / filename
                    rows.append({"path": str(item.relative_to(workspace)), "type": "file", "bytes": item.stat().st_size})
                    if len(rows) >= max_entries:
                        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
            return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repo_map_failed")


class PatchApplyTool(AgentTool):
    spec = ToolSpec(
        name="patch.apply",
        description="Apply a unified diff inside the workspace. Disabled unless file writes are enabled.",
        parameters={
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
                "check": {"type": "boolean"},
            },
            "required": ["patch"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        patch_text = str(arguments.get("patch", ""))
        check_only = bool(arguments.get("check", False))
        if not patch_text.strip():
            return self._result(call, success=False, content="Missing patch", error="missing_patch")
        try:
            _validate_patch_paths(context.workspace, patch_text)
            command = ["git", "apply", "--check"] if check_only else ["git", "apply", "--whitespace=nowarn"]
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
                command,
                cwd=context.workspace,
                input=patch_text,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode, "check": check_only},
                error=None if completed.returncode == 0 else "patch_apply_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="patch_apply_failed")


class TestRunTool(AgentTool):
    spec = ToolSpec(
        name="test.run",
        description="Run a bounded test command in the workspace. Disabled unless shell execution is enabled.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
            },
        },
        risk="high",
        requires_approval=True,
    )
    allowed_first_tokens = {"pytest", "python", "python3"}

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command", ["pytest", "-q"])
        if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
            return self._result(call, success=False, content="command must be list[str]", error="bad_command")
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(call, success=False, content="Command is not allowlisted", error="command_not_allowlisted")
        try:
            completed = subprocess.run(  # noqa: S603 - intentionally allowlisted
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=int(arguments.get("timeout", 120)),
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode},
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="test_run_failed")


class LintRunTool(AgentTool):
    spec = ToolSpec(
        name="lint.run",
        description="Run a bounded lint/typecheck command in the workspace. Disabled unless shell execution is enabled.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
            },
        },
        risk="high",
        requires_approval=True,
    )
    allowed_first_tokens = {"ruff", "mypy", "python", "python3"}

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command", ["ruff", "check", "."])
        if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
            return self._result(call, success=False, content="command must be list[str]", error="bad_command")
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(call, success=False, content="Command is not allowlisted", error="command_not_allowlisted")
        try:
            completed = subprocess.run(  # noqa: S603 - intentionally allowlisted
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=int(arguments.get("timeout", 120)),
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode},
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="lint_run_failed")


class GitStatusTool(AgentTool):
    spec = ToolSpec(
        name="git.status",
        description="Return read-only git status for the workspace.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        return _git_read(call, context, ["git", "status", "--short", "--branch"], "git_status_failed")


class GitDiffTool(AgentTool):
    spec = ToolSpec(
        name="git.diff",
        description="Return read-only git diff for the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command = ["git", "diff", "--cached"] if bool(arguments.get("staged", False)) else ["git", "diff"]
        result = _git_read(call, context, command, "git_diff_failed")
        max_chars = int(arguments.get("max_chars", 40_000))
        if len(result.content) > max_chars:
            return self._result(
                call,
                success=result.success,
                content=result.content[:max_chars] + "\n... truncated ...",
                data={**result.data, "truncated": True},
                error=result.error,
            )
        return result


class GitBranchTool(AgentTool):
    spec = ToolSpec(
        name="git.branch",
        description="Return read-only branch information for the workspace.",
        parameters={"type": "object", "properties": {"all": {"type": "boolean"}}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        command = ["git", "branch", "--all"] if bool(arguments.get("all", False)) else ["git", "branch", "--show-current"]
        return _git_read(ToolCall(name=self.spec.name, arguments=arguments), context, command, "git_branch_failed")


class GitCommitTool(AgentTool):
    spec = ToolSpec(
        name="git.commit",
        description="Commit already-staged workspace changes. Requires explicit approval and never pushes.",
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        message = str(arguments.get("message", "")).strip()
        if not message:
            return self._result(call, success=False, content="Missing commit message", error="missing_message")
        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
                ["git", "commit", "-m", message],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode},
                error=None if completed.returncode == 0 else "git_commit_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="git_commit_failed")


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
        return self._result(call, success=all(rows.values()), content=json.dumps(rows, indent=2), data={"layers": rows})


class MemvidDoctorTool(AgentTool):
    spec = ToolSpec(
        name="memvid.doctor",
        description="Run dry-run doctor checks on memory layers when the backend supports it.",
        parameters={
            "type": "object",
            "properties": {"dry_run": {"type": "boolean"}},
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        dry_run = bool(arguments.get("dry_run", True))
        rows: dict[str, object] = {}
        for layer, backend in context.memory.backends.items():
            doctor = getattr(backend, "doctor", None)
            if callable(doctor):
                rows[layer.value] = doctor(dry_run=dry_run)
            else:
                rows[layer.value] = {"ok": backend.verify(), "doctor_available": False}
        return self._result(call, success=True, content=json.dumps(rows, indent=2, default=str), data={"layers": rows})


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
        return self._result(call, success=True, content=json.dumps(rows, indent=2, default=str), data={"layers": rows})


class MemoryConsolidateTool(AgentTool):
    spec = ToolSpec(
        name="memory.consolidate",
        description="Promote a validated memory candidate through the nested consolidation pipeline.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source_layer": {"type": "string"},
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
            hits = context.memory.retrieve(RetrievalQuery(query=query, layers=layers, k_per_layer=1))
            if not hits:
                return self._result(call, success=False, content="No consolidation candidate found.", error="candidate_not_found")
            validation_score = float(arguments.get("validation_score", 0.7))
            repeat_count = int(arguments.get("repeat_count", 1))
            explicit_instruction = bool(arguments.get("explicit_instruction", False))
            candidate = Consolidator().propose(
                hits[0].record,
                validation_score=validation_score,
                repeat_count=repeat_count,
                explicit_instruction=explicit_instruction,
            )
            if candidate is None:
                return self._result(call, success=True, content="No promotion proposed.", data={"promoted": False})
            if candidate.target_layer == MemoryLayer.POLICY and not context.config.allow_policy_writes:
                return self._result(
                    call,
                    success=False,
                    content="Policy promotion is disabled by default.",
                    error="policy_write_disabled",
                )
            promoted = Consolidator().promote(candidate)
            dry_run = bool(arguments.get("dry_run", False))
            record_id = None if dry_run else context.memory.put(promoted)
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
            }
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="memory_consolidate_failed")


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
                "target_layer": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
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
            return self._result(call, success=False, content="Missing content", error="missing_content")
        try:
            source_layer = _layer_arg(arguments.get("source_layer")) or MemoryLayer.WORKING
            target_layer = _layer_arg(arguments.get("target_layer"))
            kind = MemoryKind(str(arguments.get("kind", MemoryKind.OBSERVATION.value)))
            signal = LearningSignal(
                title=title,
                content=content,
                kind=kind,
                source_layer=source_layer,
                confidence=float(arguments.get("confidence", 0.6)),
                importance=float(arguments.get("importance", 0.5)),
                validation_score=float(arguments.get("validation_score", 0.7)),
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
                return self._result(
                    call,
                    success=True,
                    content=json.dumps(decision.to_payload(), indent=2),
                    data=decision.to_payload(),
                )
            if decision.target_layer == MemoryLayer.POLICY and not context.config.allow_policy_writes:
                payload = decision.to_payload()
                payload["policy_write_enabled"] = False
                return self._result(
                    call,
                    success=False,
                    content=json.dumps(payload, indent=2),
                    data=payload,
                    error="policy_write_disabled",
                )
            record = kernel.to_memory_record(signal, decision)
            dry_run = bool(arguments.get("dry_run", False))
            record_id = None if dry_run else context.memory.put(record)
            if record_id is not None:
                context.memory.seal_all()
            payload = {
                **decision.to_payload(),
                "dry_run": dry_run,
                "record_id": record_id,
            }
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="memory_learn_failed")


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
            RetrievalQuery(query=query, layers=layers, k_per_layer=int(arguments.get("k", 8)))
        )
        rows = [_memory_hit_payload(hit) for hit in hits]
        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"hits": rows})


class MemoryExportTool(AgentTool):
    spec = ToolSpec(
        name="memory.export",
        description="Export memory records as structured JSON. Use query for backends without full record iteration.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
        rows: list[dict[str, object]] = []
        query = str(arguments.get("query", "")).strip()
        if query:
            hits = context.memory.retrieve(
                RetrievalQuery(query=query, layers=layers, k_per_layer=int(arguments.get("k", 20)))
            )
            rows = [_memory_record_payload(hit.record) for hit in hits]
        else:
            for layer in layers:
                backend = context.memory.backends.get(layer)
                records = getattr(backend, "records", None)
                if isinstance(records, list):
                    rows.extend(_memory_record_payload(record) for record in records)
        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"records": rows})


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
            return self._result(call, success=False, content="records must be a list", error="bad_records")
        if not all(isinstance(item, dict) for item in raw_records):
            return self._result(call, success=False, content="Every record must be an object", error="bad_records")
        try:
            records = [_memory_record_from_payload(item) for item in raw_records]
        except Exception as exc:  # noqa: BLE001 - import payload validation boundary
            return self._result(call, success=False, content=str(exc), error="bad_records")
        if any(record.layer == MemoryLayer.POLICY for record in records) and not context.config.allow_policy_writes:
            return self._result(
                call,
                success=False,
                content="Policy memory import is disabled by default.",
                error="policy_write_disabled",
            )
        dry_run = bool(arguments.get("dry_run", False))
        ids: list[str] = []
        if not dry_run:
            try:
                for record in records:
                    ids.append(context.memory.put(record))
                context.memory.seal_all()
            except Exception as exc:  # noqa: BLE001 - import should report failed writes structurally
                return self._result(call, success=False, content=str(exc), error="memory_import_failed")
        payload = {"dry_run": dry_run, "imported": len(records), "record_ids": ids}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class DiagnosisClassifyTool(AgentTool):
    spec = ToolSpec(
        name="diagnosis.classify",
        description="Classify a runtime/tool/test/provider failure and return the matching diagnostic playbook.",
        parameters={
            "type": "object",
            "properties": {
                "failure_text": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["failure_text"],
        },
        capabilities=("self-diagnosis", "failure-classification"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del context
        call = ToolCall(name=self.spec.name, arguments=arguments)
        failure_text = str(arguments.get("failure_text", "")).strip()
        if not failure_text:
            return self._result(call, success=False, content="Missing failure_text", error="missing_failure_text")
        classification = classify_failure(failure_text, source=str(arguments.get("source", "")))
        payload = classification.to_payload()
        content = json.dumps(payload, indent=2)
        return self._result(call, success=True, content=content, data=payload)


class DiagnosisRecallTool(AgentTool):
    spec = ToolSpec(
        name="diagnosis.recall",
        description="Classify a failure and retrieve similar prior failure lessons from procedural/episodic memory before retrying.",
        parameters={
            "type": "object",
            "properties": {
                "failure_text": {"type": "string"},
                "source": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["failure_text"],
        },
        capabilities=("self-diagnosis", "failure-recall", "nested-memory"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        failure_text = str(arguments.get("failure_text", "")).strip()
        if not failure_text:
            return self._result(call, success=False, content="Missing failure_text", error="missing_failure_text")
        classification = classify_failure(failure_text, source=str(arguments.get("source", "")))
        k = max(1, min(int(arguments.get("k", 5)), 10))
        query = f"{classification.category} {failure_text}"
        hits = context.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=(MemoryLayer.PROCEDURAL, MemoryLayer.EPISODIC, MemoryLayer.WORKING),
                k_per_layer=k,
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
        payload = {
            **classification.to_payload(),
            "query": query,
            "hits": rows,
            "retry_guidance": {
                "must_change_strategy_before_retry": bool(rows),
                "reason": "Similar prior failures were found; use recalled lessons before repeating the action."
                if rows
                else "No prior lesson found; follow the diagnostic playbook and record validated findings.",
            },
        }
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


def build_default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(DiagnosisClassifyTool())
    registry.register(DiagnosisRecallTool())
    registry.register(MemorySearchTool())
    registry.register(MemoryWriteTool())
    registry.register(ContextPackTool())
    registry.register(ContextExpandTool())
    registry.register(CapsuleSummarizeTool())
    registry.register(CapsuleApplyTool())
    registry.register(MemoryConflictsTool())
    registry.register(ListFilesTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ShellRunTool())
    registry.register(CodexExecTool())
    registry.register(RepoSearchTool())
    registry.register(RepoMapTool())
    registry.register(PatchApplyTool())
    registry.register(TestRunTool())
    registry.register(LintRunTool())
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitBranchTool())
    registry.register(GitCommitTool())
    registry.register(MemvidVerifyTool())
    registry.register(MemvidDoctorTool())
    registry.register(MemvidStatsTool())
    registry.register(MemoryLearnTool())
    registry.register(MemoryConsolidateTool())
    registry.register(MemoryInspectTool())
    registry.register(MemoryExportTool())
    registry.register(MemoryImportTool())
    return registry


def _safe_path(root: Path, relative: str) -> Path:
    root_resolved = root.resolve()
    path = (root_resolved / relative).resolve()
    if root_resolved not in path.parents and path != root_resolved:
        raise ValueError(f"Path escapes workspace: {relative}")
    return path


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... truncated ..."


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
        "evidence": [
            {"source": evidence.source, "locator": evidence.locator, "quote": evidence.quote}
            for evidence in record.evidence
        ],
    }


def _memory_record_from_payload(item: dict[str, Any]) -> MemoryRecord:
    record = MemoryRecord(
        layer=MemoryLayer(str(item.get("layer", MemoryLayer.WORKING.value))),
        kind=MemoryKind(str(item.get("kind", MemoryKind.OBSERVATION.value))),
        title=str(item.get("title", "Imported memory")),
        content=str(item.get("content", "")),
        confidence=float(item.get("confidence", 0.8)),
        importance=float(item.get("importance", 0.5)),
        tags=dict(item.get("tags", {})) if isinstance(item.get("tags"), dict) else {},
        metadata=dict(item.get("metadata", {})) if isinstance(item.get("metadata"), dict) else {},
    )
    record_id = str(item.get("id") or "").strip()
    if record_id:
        record.id = record_id
    return record


def _skip_repo_name(name: str) -> bool:
    return name in {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"} or name.startswith(".")


def _iter_repo_files(root: Path, workspace: Path, *, max_file_bytes: int) -> list[Path]:
    workspace_root = workspace.resolve()
    files: list[Path] = []
    if root.is_file():
        if root.stat().st_size <= max_file_bytes:
            return [root]
        return []
    for current_root, dirs, filenames in os.walk(root):
        dirs[:] = sorted(dirname for dirname in dirs if not _skip_repo_name(dirname))
        current = Path(current_root)
        for filename in sorted(filenames):
            if _skip_repo_name(filename):
                continue
            path = current / filename
            try:
                path.relative_to(workspace_root)
            except ValueError:
                continue
            if path.stat().st_size <= max_file_bytes:
                files.append(path)
    return files


def _validate_patch_paths(workspace: Path, patch_text: str) -> None:
    for line in patch_text.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw = line[4:].split("\t", maxsplit=1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        _safe_path(workspace, raw)


def _git_read(call: ToolCall, context: ToolContext, command: list[str], error_code: str) -> ToolExecution:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed read-only git commands
            command,
            cwd=context.workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        return ToolExecution(
            call=call,
            success=completed.returncode == 0,
            content=content,
            data={"returncode": completed.returncode},
            error=None if completed.returncode == 0 else error_code,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolExecution(call=call, success=False, content=str(exc), error=error_code)


def _layer_arg(value: object) -> MemoryLayer | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return MemoryLayer(text)


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
        if hit.record.id == lookup_id or str(metadata.get("frame_id", "")) == lookup_id or hit.frame_id == lookup_id:
            return hit
    return None


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


def _capsule_apply_plan(summary: Any, *, context: ToolContext, include_policy: bool) -> list[dict[str, object]]:
    kernel = NestedLearningKernel()
    plan: list[dict[str, object]] = []
    for index, signal in enumerate(summary.learning_signals):
        decision = kernel.decide(signal)
        payload = decision.to_payload()
        payload["signal_index"] = index
        payload["signal_title"] = signal.title
        payload["signal_kind"] = signal.kind.value
        payload["requested_target_layer"] = signal.requested_target_layer.value if signal.requested_target_layer else None
        payload["will_write"] = False
        if not decision.accepted or decision.target_layer is None:
            payload["blocked"] = (
                "policy_requires_explicit_instruction"
                if signal.requested_target_layer == MemoryLayer.POLICY and not signal.explicit_instruction
                else "nested_learning_rejected"
            )
        elif decision.target_layer == MemoryLayer.POLICY:
            if not include_policy:
                payload["blocked"] = "policy_excluded_from_capsule_apply"
            elif not context.config.allow_policy_writes:
                payload["blocked"] = "policy_write_disabled"
            elif not signal.explicit_instruction:
                payload["blocked"] = "policy_requires_explicit_instruction"
            else:
                payload["will_write"] = True
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
    hits = context.memory.retrieve(RetrievalQuery(query=content_hash, layers=(layer,), k_per_layer=3))
    return any(hit.record.content_hash == content_hash or hit.record.metadata.get("content_hash") == content_hash for hit in hits)
