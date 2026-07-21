from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from ..config import MAX_TOOL_TIMEOUT_SECONDS
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from .base import AgentTool, ToolContext

CapabilityGate = Callable[[ToolSpec], tuple[bool, str]]

# Python threads cannot be force-stopped safely.  Cancellation-aware tools get
# a short grace period to publish their terminal result; tools which explicitly
# opt into settlement waiting get a larger, still-hard-bounded window.  Once
# either window expires the only safe answer is "outcome unresolved" -- never a
# retryable timeout that could duplicate a side effect which committed late.
_CANCELLATION_SETTLEMENT_SECONDS = 0.25
_CANCELLATION_HOOK_MAX_SECONDS = 0.10
_TRUSTED_SETTLEMENT_MAX_SECONDS = 5.0

ToolIdentity = tuple[str, str, str, str]
ToolExecutionIdentity = tuple[ToolIdentity, str, str, str, str]


class RuntimeToolFence:
    """Coordinate duplicate and unresolved tool executions within one runtime.

    A ``RunManager`` owns one fence and shares it with every short-lived registry
    it creates. Standalone registries receive a private fence, so tests and
    independent embedded runtimes cannot quarantine one another through module
    state.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._quarantined: set[ToolIdentity] = set()
        self._inflight: set[ToolExecutionIdentity] = set()

    def reserve(
        self,
        tool_identity: ToolIdentity,
        execution_scope: str,
        execution_origin: str,
        call_id: str,
        arguments_digest: str,
    ) -> Literal["quarantined", "inflight"] | None:
        execution_identity = (
            tool_identity,
            execution_scope,
            execution_origin,
            call_id,
            arguments_digest,
        )
        with self._lock:
            if tool_identity in self._quarantined:
                return "quarantined"
            if execution_identity in self._inflight:
                return "inflight"
            self._inflight.add(execution_identity)
        return None

    def finish(
        self,
        tool_identity: ToolIdentity,
        execution_scope: str,
        execution_origin: str,
        call_id: str,
        arguments_digest: str,
        *,
        unresolved: bool,
    ) -> None:
        execution_identity = (
            tool_identity,
            execution_scope,
            execution_origin,
            call_id,
            arguments_digest,
        )
        with self._lock:
            self._inflight.discard(execution_identity)
            if unresolved:
                self._quarantined.add(tool_identity)


class ToolRegistry:
    def __init__(
        self,
        *,
        capability_gate: CapabilityGate | None = None,
        runtime_fence: RuntimeToolFence | None = None,
    ) -> None:
        self._tools: dict[str, AgentTool] = {}
        self._aliases: dict[str, str] = {}
        self._capability_gate = capability_gate
        self._runtime_fence = runtime_fence or RuntimeToolFence()

    def set_capability_gate(self, gate: CapabilityGate | None) -> None:
        """Install a live, fail-closed gate for operator capability policy.

        The callback is intentionally evaluated for both discovery and execution.
        A registry that was built before a capability was disabled therefore cannot
        be used to bypass the current control-plane decision.
        """

        self._capability_gate = gate

    def register(self, tool: AgentTool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.spec.name}")
        self._tools[tool.spec.name] = tool
        for alias in tool.spec.aliases:
            if alias in self._tools or alias in self._aliases:
                raise ValueError(f"Alias conflict: {alias}")
            self._aliases[alias] = tool.spec.name

    def specs(self) -> list[ToolSpec]:
        specs = self.all_specs()
        if self._capability_gate is None:
            return specs
        return [spec for spec in specs if self._capability_gate(spec)[0]]

    def all_specs(self) -> list[ToolSpec]:
        """Return the stable catalog, including operator-disabled tools."""

        return [tool.spec for tool in self._tools.values()]

    def spec_for(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        if tool is None:
            canonical = self._aliases.get(name)
            if canonical is not None:
                tool = self._tools.get(canonical)
        return None if tool is None else tool.spec

    def canonical_name(self, name: str) -> str | None:
        """Resolve a public name or alias to the canonical registered tool name."""

        if name in self._tools:
            return name
        return self._aliases.get(name)

    def execute(self, call: ToolCall, context: ToolContext) -> ToolExecution:
        if not isinstance(call.arguments, dict):
            return _failure(
                call,
                content=f"Tool {call.name} arguments must be a JSON object.",
                error="invalid_tool_arguments",
            )
        if not _is_json_tool_value(call.arguments):
            return _failure(
                call,
                content=f"Tool {call.name} arguments must be one finite JSON object.",
                error="invalid_tool_arguments",
            )
        tool = self._tools.get(call.name)
        if tool is None:
            canonical = self._aliases.get(call.name)
            if canonical is not None:
                tool = self._tools.get(canonical)
        if tool is None:
            return _failure(call, content=f"Unknown tool: {call.name}", error="unknown_tool")

        if self._capability_gate is not None:
            enabled, disabled_reason = self._capability_gate(tool.spec)
            if not enabled:
                return _failure(
                    call,
                    content=disabled_reason
                    or f"Tool {tool.spec.name} is disabled by capability policy.",
                    error="tool_disabled",
                )

        if not context.tool_specs:
            context.tool_specs = tuple(self.specs())

        # Underscore-prefixed fields are reserved for runtime metadata. Never
        # let model/user arguments select process-tracking or cancellation IDs.
        arguments = {
            key: value for key, value in call.arguments.items() if not str(key).startswith("_")
        }
        if getattr(tool, "needs_call_id", False):
            arguments["_tool_call_id"] = call.id

        enabled, disabled_reason = _capability_enabled(tool, context)
        if not enabled:
            return _failure(call, content=disabled_reason, error="tool_disabled")

        if tool.spec.requires_approval or tool.spec.risk in {"high", "critical"}:
            if _is_exact_call_approved(call, arguments, context):
                return self._run_registered_tool(tool, call, arguments, context)
            if context.approval_handler is not None:
                return context.approval_handler(call, tool.spec, context)
            return _failure(
                call,
                content=f"Tool {call.name} requires explicit approval for this exact call.",
                error="approval_required",
            )

        return self._run_registered_tool(tool, call, arguments, context)

    def _run_registered_tool(
        self,
        tool: AgentTool,
        call: ToolCall,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolExecution:
        tool_identity = (
            tool.spec.source,
            tool.spec.server_id or "",
            tool.spec.skill_id or "",
            tool.spec.name,
        )
        # Distinct calls -- including parallel subagents in one run -- may use
        # the same tool concurrently. Fence only the exact durable call identity
        # so a retry/replay of that call cannot race its original execution. An
        # unresolved result quarantines the implementation across every registry
        # and run owned by this Kestrel runtime.
        execution_scope = context.run_id or "__unscoped__"
        execution_origin = context.execution_origin or "__default__"
        arguments_digest = _tool_arguments_fence_digest(arguments)
        if arguments_digest is None:
            return _failure(
                call,
                content=f"Tool {call.name} arguments changed during validation.",
                error="invalid_tool_arguments",
            )
        fence_status = self._runtime_fence.reserve(
            tool_identity,
            execution_scope,
            execution_origin,
            call.id,
            arguments_digest,
        )
        if fence_status == "quarantined":
            return _tool_fence_failure(call, tool.spec.name, quarantined=True)
        if fence_status == "inflight":
            return _tool_fence_failure(call, tool.spec.name, quarantined=False)
        try:
            result = _run_tool(tool, call, arguments, context)
        except BaseException:
            self._runtime_fence.finish(
                tool_identity,
                execution_scope,
                execution_origin,
                call.id,
                arguments_digest,
                unresolved=True,
            )
            raise
        self._runtime_fence.finish(
            tool_identity,
            execution_scope,
            execution_origin,
            call.id,
            arguments_digest,
            unresolved=result.error == "tool_outcome_unresolved",
        )
        return result


def _tool_fence_failure(
    call: ToolCall,
    tool_name: str,
    *,
    quarantined: bool,
) -> ToolExecution:
    if quarantined:
        return ToolExecution(
            call=_public_tool_call(call),
            success=False,
            content=(
                f"Tool {tool_name} is quarantined because an earlier execution did not "
                "settle after cancellation. Reconcile that outcome and restart the "
                "Kestrel runtime before invoking it again."
            ),
            data={
                "outcome_indeterminate": True,
                "retryable": False,
                "reconciliation_required": True,
                "tool_quarantined": True,
            },
            error="tool_quarantined_after_unresolved_outcome",
        )
    return ToolExecution(
        call=_public_tool_call(call),
        success=False,
        content=(
            f"Tool {tool_name} already has an in-flight execution in this runtime. "
            "A concurrent duplicate was not started."
        ),
        data={
            "outcome_indeterminate": False,
            "retryable": False,
            "reconciliation_required": False,
            "tool_execution_in_progress": True,
        },
        error="tool_execution_in_progress",
    )


def _tool_arguments_fence_digest(arguments: dict[str, Any]) -> str | None:
    public_arguments = {
        str(key): value for key, value in arguments.items() if not str(key).startswith("_")
    }
    try:
        encoded = json.dumps(
            public_arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError):
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_json_tool_value(
    value: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> bool:
    if depth > 64:
        return False
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list | dict):
        active = seen if seen is not None else set()
        identity = id(value)
        if identity in active:
            return False
        active.add(identity)
        try:
            if isinstance(value, list):
                return all(
                    _is_json_tool_value(item, depth=depth + 1, seen=active) for item in value
                )
            return all(
                isinstance(key, str) and _is_json_tool_value(item, depth=depth + 1, seen=active)
                for key, item in value.items()
            )
        finally:
            active.remove(identity)
    return False


def _capability_enabled(tool: AgentTool, context: ToolContext) -> tuple[bool, str]:
    if tool.spec.source == "skill" and "executable-skill" in tool.spec.capabilities:
        if context.config.allow_executable_skills:
            return True, ""
        return (
            False,
            f"Tool {tool.spec.name} is disabled. Enable allow_executable_skills before requesting approval.",
        )
    enablement_attr = _ENABLEMENT_BY_TOOL.get(tool.spec.name)
    if not enablement_attr:
        return True, ""
    if bool(getattr(context.config, enablement_attr)):
        return True, ""
    return (
        False,
        f"Tool {tool.spec.name} is disabled. Enable {enablement_attr} before requesting approval.",
    )


def tool_enablement_status(spec: ToolSpec, config: Any | None) -> dict[str, Any]:
    enablement_attr = _enablement_attr_for_spec(spec)
    if enablement_attr is None:
        return {"enabled": True, "enablement_flag": None}
    return {
        "enabled": bool(config is not None and getattr(config, enablement_attr, False)),
        "enablement_flag": enablement_attr,
    }


def _enablement_attr_for_spec(spec: ToolSpec) -> str | None:
    if spec.source == "skill" and "executable-skill" in spec.capabilities:
        return "allow_executable_skills"
    return _ENABLEMENT_BY_TOOL.get(spec.name)


def _is_exact_call_approved(
    call: ToolCall, arguments: dict[str, Any], context: ToolContext
) -> bool:
    if call.id not in context.approved_tool_call_ids:
        return False
    approved_arguments = context.approved_tool_call_arguments
    if approved_arguments is None or call.id not in approved_arguments:
        return False
    return _arguments_match(approved_arguments[call.id], arguments)


def _arguments_match(approved: dict[str, Any], actual: dict[str, Any]) -> bool:
    if approved == actual:
        return True
    public_approved = {
        key: value for key, value in approved.items() if not str(key).startswith("_")
    }
    public_actual = {key: value for key, value in actual.items() if not str(key).startswith("_")}
    return public_approved == public_actual


def _run_tool(
    tool: AgentTool, call: ToolCall, arguments: dict[str, Any], context: ToolContext
) -> ToolExecution:
    public_call = _public_tool_call(call)
    try:
        timeout = float(getattr(context.config, "tool_timeout_seconds", 30.0))
    except (TypeError, ValueError):
        timeout = math.nan
    if not math.isfinite(timeout) or not 0.001 <= timeout <= MAX_TOOL_TIMEOUT_SECONDS:
        return ToolExecution(
            call=public_call,
            success=False,
            content=(
                "Tool execution was rejected because tool_timeout_seconds is not a finite "
                f"value between 0.001 and {MAX_TOOL_TIMEOUT_SECONDS:.0f}."
            ),
            data={"retryable": False},
            error="invalid_tool_timeout",
        )
    execution_id = f"tool-exec-{uuid4().hex}"
    runtime_arguments = dict(arguments)
    cancellation_id = call.id
    if getattr(tool, "needs_call_id", False):
        runtime_arguments["_tool_execution_id"] = execution_id
        cancellation_id = execution_id
    results: Queue[ToolExecution] = Queue(maxsize=1)
    worker_finished = Event()

    def target() -> None:
        try:
            results.put(tool.run(runtime_arguments, context))
        except BaseException as exc:  # noqa: BLE001 - worker must always signal quiescence
            results.put(
                _failure(
                    call, content=f"{type(exc).__name__}: {exc}", error="tool_execution_failed"
                )
            )
        finally:
            worker_finished.set()

    thread = Thread(target=target, daemon=True)
    cancellation_finished: Event | None = None
    worker_may_have_started = False
    try:
        # ``Thread.start()`` is not an atomic boundary from the caller's point
        # of view: an asynchronous interruption can arrive after the native
        # worker was launched but before ``start()`` returns. Keep launch inside
        # the same exceptional-unwind fence as the result wait so that such a
        # worker cannot outlive agent-owned memory without resource retention.
        thread.start()
        worker_may_have_started = True
        try:
            return _bind_execution_call(results.get(timeout=timeout), public_call)
        except Empty:
            cancellation_finished = _start_tool_cancellation(tool, cancellation_id)
            cancellation_hook_settled = cancellation_finished.wait(_CANCELLATION_HOOK_MAX_SECONDS)
            settlement_timeout = _CANCELLATION_SETTLEMENT_SECONDS
            if getattr(tool, "wait_for_completion_on_timeout", False):
                settlement_timeout = min(
                    max(timeout * 2, _CANCELLATION_SETTLEMENT_SECONDS),
                    _TRUSTED_SETTLEMENT_MAX_SECONDS,
                )
            settlement_deadline = monotonic() + settlement_timeout
            try:
                completed = _bind_execution_call(
                    results.get(timeout=settlement_timeout),
                    public_call,
                )
            except Empty:
                # The worker may still commit after this return.  At-most-once is
                # preserved by making the ambiguity explicit and non-retryable;
                # callers must reconcile externally instead of starting a second
                # execution.  The daemon thread cannot keep process shutdown open.
                _retain_context_resources_until_settled(
                    context,
                    execution_id=execution_id,
                    worker_finished=worker_finished,
                    cancellation_finished=cancellation_finished,
                )
                return _unresolved_tool_execution(
                    public_call,
                    timeout=timeout,
                    settlement_timeout=settlement_timeout,
                    cancellation_hook_settled=cancellation_hook_settled,
                    execution_may_still_be_running=thread.is_alive(),
                )
            if not cancellation_hook_settled:
                cancellation_hook_settled = cancellation_finished.wait(
                    max(settlement_deadline - monotonic(), 0.0)
                )
            if not cancellation_hook_settled:
                # The worker may have settled while its untrusted cancellation hook
                # remains live. Reporting the worker result would clear quarantine
                # even though that hook can resume and mutate the same execution.
                _retain_context_resources_until_settled(
                    context,
                    execution_id=execution_id,
                    worker_finished=worker_finished,
                    cancellation_finished=cancellation_finished,
                )
                return _unresolved_tool_execution(
                    public_call,
                    timeout=timeout,
                    settlement_timeout=settlement_timeout,
                    cancellation_hook_settled=False,
                    execution_may_still_be_running=thread.is_alive(),
                )
            # Once quiesced, the actual outcome is known. Returning a fabricated
            # timeout would make a committed operation look retryable. Preserve the
            # real result while exposing that the response deadline was exceeded.
            return ToolExecution(
                call=completed.call,
                success=completed.success,
                content=completed.content,
                data={
                    **completed.data,
                    "tool_deadline_exceeded": True,
                    "tool_timeout_seconds": timeout,
                },
                error=completed.error,
            )
    except BaseException as launch_or_wait_error:
        worker_may_have_started = (
            worker_may_have_started or thread.ident is not None or worker_finished.is_set()
        )
        if not worker_may_have_started and isinstance(launch_or_wait_error, Exception):
            # An ordinary Thread.start failure with no observable worker
            # identity or terminal signal is pre-start. Asynchronous process
            # control exceptions are different: they can arrive at any bytecode
            # boundary after the native launch, so absence of an identity is not
            # proof that no worker exists and must remain fail-closed below.
            raise
        # Queue waits and result binding can themselves be interrupted after the
        # worker starts. ``Thread.start()`` itself has the same ambiguous edge.
        # Preserve the original exception for process control, but first make the
        # still-live execution explicit to resource and retry fences.
        if cancellation_finished is None:
            cancellation_finished = _start_tool_cancellation(tool, cancellation_id)
            try:
                cancellation_finished.wait(_CANCELLATION_HOOK_MAX_SECONDS)
            except BaseException:
                pass
        try:
            _retain_context_resources_until_settled(
                context,
                execution_id=execution_id,
                worker_finished=worker_finished,
                cancellation_finished=cancellation_finished,
            )
        except BaseException:
            pass
        raise


def _unresolved_tool_execution(
    call: ToolCall,
    *,
    timeout: float,
    settlement_timeout: float,
    cancellation_hook_settled: bool,
    execution_may_still_be_running: bool,
) -> ToolExecution:
    return ToolExecution(
        call=call,
        success=False,
        content=(
            f"Tool {call.name} exceeded its {timeout:.3f}s deadline and did not fully "
            "settle after cancellation; its final outcome is unknown. Automatic retry "
            "is suppressed."
        ),
        data={
            "tool_deadline_exceeded": True,
            "tool_timeout_seconds": timeout,
            "settlement_timeout_seconds": settlement_timeout,
            "cancellation_hook_settled": cancellation_hook_settled,
            "cancellation_hook_may_still_be_running": not cancellation_hook_settled,
            "outcome_indeterminate": True,
            "retryable": False,
            "reconciliation_required": True,
            "execution_may_still_be_running": execution_may_still_be_running,
            "resource_quarantine_required": True,
        },
        error="tool_outcome_unresolved",
    )


def _failure(call: ToolCall, *, content: str, error: str) -> ToolExecution:
    return ToolExecution(
        call=_public_tool_call(call),
        success=False,
        content=content,
        error=error,
    )


def _public_tool_call(call: ToolCall) -> ToolCall:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    return ToolCall(
        name=call.name,
        arguments={key: value for key, value in arguments.items() if not str(key).startswith("_")},
        id=call.id,
        strategy=call.strategy,
    )


def _bind_execution_call(
    execution: ToolExecution,
    call: ToolCall,
) -> ToolExecution:
    """Bind all outcomes to the exact public request admitted by the registry."""

    if execution.call == call:
        return execution
    return ToolExecution(
        call=call,
        success=execution.success,
        content=execution.content,
        data=execution.data,
        error=execution.error,
    )


def _cancel_tool(tool: AgentTool, call_id: str) -> None:
    try:
        tool.cancel(call_id)
    except Exception:
        return


def _start_tool_cancellation(tool: AgentTool, call_id: str) -> Event:
    """Start cancellation while retaining a quiescence event even if launch fails."""

    finished = Event()

    def cancel() -> None:
        try:
            _cancel_tool(tool, call_id)
        finally:
            finished.set()

    try:
        cancellation_thread = Thread(
            target=cancel,
            name=f"kestrel-tool-cancel-{tool.spec.name}",
            daemon=True,
        )
        cancellation_thread.start()
    except BaseException:
        # An unsignaled event intentionally keeps resources quarantined. The
        # cancellation hook was not proven to have started or settled.
        pass
    return finished


def _retain_context_resources_until_settled(
    context: ToolContext,
    *,
    execution_id: str,
    worker_finished: Event,
    cancellation_finished: Event,
) -> None:
    """Keep agent-owned resources live until indeterminate tool code is quiescent."""

    retain = getattr(context.memory, "retain_for_unsettled_tool_execution", None)
    release = getattr(context.memory, "release_unsettled_tool_execution", None)
    if not callable(retain) or not callable(release):
        return
    retain(execution_id)

    def wait_for_quiescence() -> None:
        worker_finished.wait()
        cancellation_finished.wait()
        release(execution_id)

    Thread(
        target=wait_for_quiescence,
        name="kestrel-tool-resource-quiescence",
        daemon=True,
    ).start()


_ENABLEMENT_BY_TOOL = {
    "file.write": "allow_file_write",
    "patch.apply": "allow_file_write",
    "shell.run": "allow_shell",
    "test.run": "allow_shell",
    "lint.run": "allow_shell",
    "repair.prepare": "allow_file_write",
    "repair.apply_patch": "allow_file_write",
    "repair.validate": "allow_shell",
    "repair.orchestrate_validate": "allow_shell",
    "repair.review": "allow_file_write",
    "repair.rollback": "allow_file_write",
    "codex.exec": "allow_codex_cli",
    "skill.install": "allow_file_write",
    "plugin.review": "allow_plugin_install",
    "plugin.install": "allow_plugin_install",
    "git.commit": "allow_git_commit",
    "memory.import": "allow_memory_import",
    "memory.correct": "allow_memory_import",
    "memory.policy_promote": "allow_policy_writes",
    "web.search": "allow_web",
    "web.fetch": "allow_web",
    "self.propose_change": "allow_self_modification",
}

# ---------------------------------------------------------------------------
# Non-retryable error codes — these are deterministic and should not be retried
# ---------------------------------------------------------------------------

_NON_RETRYABLE_ERRORS = frozenset(
    {
        "not_found",
        "empty_results",
        "unknown_tool",
        "approval_required",
        "approval_pending",
        "tool_disabled",
        "invalid_tool_arguments",
        "invalid_tool_timeout",
        "retry_blocked",
        "sensitive_tool_arguments_rejected",
        "mcp_tool_outcome_indeterminate",
        "mcp_tool_remote_error",
        "mcp_session_cleanup_incomplete",
        "tool_outcome_unresolved",
        "tool_quarantined_after_unresolved_outcome",
        "tool_execution_in_progress",
        "extension_cleanup_pending",
        "extension_cleanup_unverified",
        "path_sandbox_violation",
    }
)

_RETRYABLE_ERRORS = frozenset(
    {
        "transient_error",
        "tool_timeout",
        "tool_execution_failed",
        "provider_failure",
        "mcp_failure",
        "missing_dependency",
    }
)


def _is_retryable_error(execution: ToolExecution) -> bool:
    """Return True if a failed tool execution should be retried programmatically."""
    if execution.success:
        return False
    error = execution.error or ""
    if error in _NON_RETRYABLE_ERRORS:
        return False
    if error in _RETRYABLE_ERRORS:
        return True
    # For unclassified errors, use diagnosis heuristics on the content
    from ..diagnosis import classify_failure

    classification = classify_failure(execution.content, source=f"tool:{execution.call.name}")
    return classification.retryable


# ---------------------------------------------------------------------------
# RetryingRegistry — transparently retries transient tool failures
# ---------------------------------------------------------------------------


class RetryingRegistry(ToolRegistry):
    """Wraps a ToolRegistry to automatically retry transient failures.

    The LLM never sees intermediate failures — only the final result
    (success or the last non-retryable failure).
    """

    def __init__(
        self,
        inner: ToolRegistry,
        *,
        max_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
    ) -> None:
        self._inner = inner
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = max(0.0, backoff_base_seconds)

    def register(self, tool: Any) -> None:
        self._inner.register(tool)

    def set_capability_gate(self, gate: CapabilityGate | None) -> None:
        self._inner.set_capability_gate(gate)

    def specs(self) -> list[ToolSpec]:
        return self._inner.specs()

    def all_specs(self) -> list[ToolSpec]:
        return self._inner.all_specs()

    def spec_for(self, name: str) -> ToolSpec | None:
        return self._inner.spec_for(name)

    def canonical_name(self, name: str) -> str | None:
        return self._inner.canonical_name(name)

    def execute(self, call: ToolCall, context: ToolContext) -> ToolExecution:
        last_execution: ToolExecution | None = None
        spec = self._inner.spec_for(call.name)
        for attempt in range(1, self._max_attempts + 1):
            execution = self._inner.execute(call, context)
            if execution.success:
                return execution
            if not _is_retryable_error(execution):
                return execution
            if not _transparent_retry_is_safe(spec):
                return execution
            last_execution = execution
            if attempt < self._max_attempts and self._backoff_base > 0:
                import time

                delay = self._backoff_base * (2 ** (attempt - 1))
                time.sleep(delay)
        # All retries exhausted — return the last failure
        return (
            last_execution
            if last_execution is not None
            else _failure(
                call, content="Retry exhausted with no execution.", error="retry_exhausted"
            )
        )


def _transparent_retry_is_safe(spec: ToolSpec | None) -> bool:
    """Allow hidden retries only for trusted, non-approved, non-mutating built-ins."""
    if spec is None or spec.source != "builtin" or spec.requires_approval:
        return False
    return spec.risk == "low" or "read-only" in spec.capabilities
