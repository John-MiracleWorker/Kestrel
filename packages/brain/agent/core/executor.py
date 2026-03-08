import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

from agent.types import (
    AgentTask,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalTier,
    RiskLevel,
    StepStatus,
    TaskEvent,
    TaskEventType,
    TaskStatus,
    ToolCall,
    ToolResult,
)
from agent.guardrails import Guardrails
from agent.observability import MetricsCollector
from agent.evidence import EvidenceChain
from agent.model_router import ModelRouter
from agent.core.verifier import VerifierEngine
from agent.diagnostics import DiagnosticTracker, classify_error, ErrorCategory
from agent.tool_cache import ToolCache
from agent.mcp_expansion import MCPExpansionEngine

logger = logging.getLogger("brain.agent.core.executor")

# ── Constants ────────────────────────────────────────────────────────
MAX_PARALLEL_TOOLS = 5       # Max concurrent tool executions per turn (default fallback)
RETRY_MAX_ATTEMPTS = 3       # Max retries for transient tool failures
RETRY_BASE_DELAY_S = 1.0     # Base delay for exponential backoff

# ── Error categories that should never be retried ────────────────────
_NO_RETRY_CATEGORIES = frozenset({
    ErrorCategory.AUTH,
    ErrorCategory.NOT_FOUND,
    ErrorCategory.DEPENDENCY,
    ErrorCategory.SEMANTIC,
    ErrorCategory.IMPOSSIBLE,
})

# ── System Prompt for the Reasoning LLM ──────────────────────────────
AGENT_SYSTEM_PROMPT = """\
You are Kestrel, an autonomous AI agent. You are executing a multi-step task.

Current goal: {goal}
Current step: {step_description}

Instructions:
1. Analyze the current situation and decide which tool to call next.
2. You may call up to 5 tools per turn if they are independent, read-only/low-risk, and do not require approval. Prefer batching and parallel-safe tools. Wait for all results before proceeding.
3. ONLY call `task_complete` when the ENTIRE original goal has been fully achieved — not just the current step. If the current step is done but there are more steps remaining in the plan, do NOT call `task_complete`. Instead, just finish your tool calls for this step and the system will automatically advance to the next step.
4. Before calling `task_complete`, review the original goal and verify every part of it has been addressed. For example, if asked to "summarize 10 emails", do not complete until all 10 have been fetched AND summarized.
5. If you need clarification from the user, call `ask_human` with your question.
6. Think step-by-step. Explain your reasoning before acting.

Error Recovery Protocol:
- When a tool fails, DIAGNOSE before retrying. Read the error message carefully.
- NEVER retry the exact same tool call with identical arguments — it will fail again.
- If an error is about a missing file/command, verify the path exists first.
- If an error is about dependencies, install them before retrying the operation.
- If an error is about auth/permissions, check credentials before retrying.
- If a server/process crashed, check stderr output and requirements before reconnecting.
- After 3 failures on the same step, STOP and use diagnostic tools (system_health, host_read, host_list) to gather information, or call `ask_human` to ask the user for help.

Verification & Evidence Rules:
- Before calling `task_complete`, you MUST verify that the ENTIRE original goal is satisfied, not just the current step.
- You MUST explicitly cite the tool outputs that prove ALL parts of the goal were accomplished in your summary.
- Your final summary will be strictly evaluated by an independent Verifier Engine against your tool execution history.
- If you make unsupported claims, hallucinate actions, or claim completion when parts of the goal are unfinished, your completion will be REJECTED.
- If you are on step {step_index} of {total_steps} and earlier steps remain, you almost certainly should NOT be calling `task_complete` yet.

Host Filesystem Strategy:
- Use project_recall(name) FIRST to check for cached project context.
- Use host_tree(path) ONCE for full directory tree — never call host_list repeatedly.
- Use host_find(pattern) or host_search(query, path) before broad reads to narrow scope quickly.
- Use host_batch_read(paths) for grouped file reads instead of repeated host_read calls.
- host_write requires human approval.

Self-Repair & Self-Improvement:
- You are NOT in an isolated sandbox. Your source code is mounted at /project inside the container.
- You CAN read and write your own source files using host_read and host_write.
- The `self_improve` tool provides: scan (analyze code), propose (generate fixes), apply (patch + test + hot-reload), rollback, and history.
- If one of your own tools is broken, DO NOT tell the user you cannot fix it. Instead, diagnose the error, read the source file, and use host_write or self_improve to patch and fix it.
- After editing a Python file, use the self_improve tool's hot-reload capability or ask the user to rebuild if needed.
- NEVER say you are in a sandbox or that you cannot modify your own code — you can and should fix issues proactively.

Progress: Step {step_index}/{total_steps} | Iteration {iteration}/{max_iterations}
{diagnostic_context}
Previous observations for this step:
{observations}
"""

# ── Compact system prompt for local models (~300 tokens) ─────────────
AGENT_SYSTEM_PROMPT_LOCAL = """\
You are Kestrel, an autonomous AI agent.

Goal: {goal}
Step: {step_description}

Rules:
- Call the appropriate tool to accomplish the current step.
- Call `task_complete` ONLY when the ENTIRE goal is done, not just this step.
- Call `ask_human` if you need clarification.
- If a tool fails, read the error and try a different approach. Never retry identical calls.
- After 3 failures, call `ask_human` for help.

Progress: Step {step_index}/{total_steps} | Iteration {iteration}/{max_iterations}
{diagnostic_context}
{observations}
"""


class TaskExecutor:
    """
    Handles the execution phase of the AgentLoop (ReAct loop).
    Executes tools, routes models, checks guardrails, and retries failures.
    """

    def __init__(
        self,
        provider,
        tool_registry,
        guardrails: Guardrails,
        persistence,
        metrics: MetricsCollector,
        model: str,
        api_key: str,
        model_router: ModelRouter,
        provider_resolver=None,
        event_callback: Optional[Callable] = None,
        evidence_chain: Optional[EvidenceChain] = None,
        progress_callback: Optional[Callable] = None,
        verifier: Optional[VerifierEngine] = None,
        approval_memory=None,
        persona_context: str = "",
        tool_cache: Optional[ToolCache] = None,
    ):
        self._provider = provider
        self._tools = tool_registry
        self._guardrails = guardrails
        self._persistence = persistence
        self._metrics = metrics
        self._model = model
        self._api_key = api_key
        self._model_router = model_router
        self._provider_resolver = provider_resolver
        self._event_callback = event_callback
        self._evidence_chain = evidence_chain
        self._progress_callback = progress_callback or (lambda t: 0.0)
        self._verifier = verifier
        self._persona_context = persona_context
        self._tool_cache = tool_cache or ToolCache()
        self._mcp_expansion = MCPExpansionEngine()
        # Detect if a workspace model was explicitly configured
        # (e.g. 'glm-5:cloud') — prevents unwanted cloud escalation
        self._has_explicit_model = bool(
            model and model not in ('', 'default', 'glm5', 'glm-5:cloud', 'glm5:cloud')
        )
        self._approval_memory = approval_memory
        self._step_diagnostics: dict[str, DiagnosticTracker] = {}  # step_id → tracker
        self._text_only_streak: dict[str, int] = {}  # step_id → consecutive text-only responses
        self._text_only_total: dict[str, int] = {}   # step_id → total text-only responses (never resets)

    def _get_tracker(self, step_id: str) -> DiagnosticTracker:
        """Get or create a DiagnosticTracker for a step."""
        if step_id not in self._step_diagnostics:
            self._step_diagnostics[step_id] = DiagnosticTracker()
        return self._step_diagnostics[step_id]

    def _compute_parallel_limit(self, tool_calls: list[dict]) -> int:
        """Dynamic parallelism: lower for risky tools, higher for safe ones.

        Returns:
            2 if any tool in the batch is HIGH risk
            4 if any tool is MEDIUM risk
            8 if all tools are LOW risk (reads, searches)
        """
        has_high = False
        has_medium = False
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            risk = self._tools.get_risk_level(name)
            if risk == RiskLevel.HIGH:
                has_high = True
                break
            elif risk == RiskLevel.MEDIUM:
                has_medium = True

        if has_high:
            return 2
        elif has_medium:
            return 4
        else:
            return 8

    async def _wait_for_approval(self, task: AgentTask) -> bool:
        """
        Block until the pending approval is resolved.
        Returns True if approved, False if denied/expired.

        Records the decision in approval memory so future matching
        patterns can be auto-approved.
        """
        if not task.pending_approval:
            return True

        timeout_s = 300
        start = time.time()
        while time.time() - start < timeout_s:
            approval = await self._persistence.get_approval(task.pending_approval.id)
            if approval:
                status = approval.status.value if isinstance(approval.status, ApprovalStatus) else str(approval.status)
                status = status.lower()
                if status != ApprovalStatus.PENDING.value:
                    approved = status == ApprovalStatus.APPROVED.value
                    # Record in approval memory for pattern learning
                    if self._approval_memory:
                        try:
                            await self._approval_memory.record_approval(
                                tool_name=task.pending_approval.tool_name,
                                tool_args=task.pending_approval.tool_args,
                                approved=approved,
                                user_id=task.user_id,
                                workspace_id=task.workspace_id,
                            )
                        except Exception as e:
                            logger.warning("Failed to record approval pattern: %s", e)
                    return approved
            await asyncio.sleep(2.0)
        return False

    async def run_step(self, task: AgentTask, step: Any) -> AsyncIterator[TaskEvent]:
        """Runs a single iteration of the ReAct loop for a given step."""
        async for event in self._reason_and_act(task, step):
            yield event

    async def _execute_tool_with_retry(
        self,
        tool_call: ToolCall,
        tool_context: dict,
        max_attempts: int = RETRY_MAX_ATTEMPTS,
    ) -> ToolResult:
        """Execute a tool with smart retry based on error classification.

        Uses the DiagnosticTracker's classify_error() to determine retry strategy:
        - TRANSIENT: retry with exponential backoff (up to max_attempts)
        - SERVER_CRASH: one retry after a longer 5s delay for process recovery
        - UNKNOWN: one retry only
        - AUTH/NOT_FOUND/DEPENDENCY/SEMANTIC/IMPOSSIBLE: fail immediately
        """
        # ── Cache check: return cached result for deterministic read-only tools ──
        workspace_id = tool_context.get("workspace_id", "")
        tool_def = self._tools.get_tool(tool_call.name)
        cached = await self._tool_cache.get(
            tool_call.name, tool_call.arguments, workspace_id, tool_def
        )
        if cached is not None:
            self._metrics.record_tool_execution(
                tool_name=tool_call.name,
                execution_time_ms=0,
                success=True,
            )
            return cached

        last_result = None
        for attempt in range(max_attempts):
            result = await self._tools.execute(tool_call, context=tool_context)
            last_result = result

            if result.success:
                self._metrics.record_tool_execution(
                    tool_name=tool_call.name,
                    execution_time_ms=result.execution_time_ms,
                    success=True,
                )
                # Cache successful results for cacheable tools
                await self._tool_cache.set(
                    tool_call.name, tool_call.arguments,
                    workspace_id, result, tool_def
                )
                return result

            # Classify the error to decide retry strategy
            category, hint = classify_error(result.error or "")

            # Non-retryable errors: fail immediately
            if category in _NO_RETRY_CATEGORIES:
                logger.info(
                    f"Tool {tool_call.name} failed with non-retryable error "
                    f"[{category.value}]: {hint}"
                )
                self._metrics.record_tool_execution(
                    tool_name=tool_call.name,
                    execution_time_ms=result.execution_time_ms,
                    success=False,
                )
                return result

            # Server crash: allow exactly one retry with longer delay
            if category == ErrorCategory.SERVER_CRASH and attempt >= 1:
                logger.info(
                    f"Tool {tool_call.name} server crash persists after retry — giving up"
                )
                self._metrics.record_tool_execution(
                    tool_name=tool_call.name,
                    execution_time_ms=result.execution_time_ms,
                    success=False,
                )
                return result

            # Unknown errors: allow exactly one retry
            if category == ErrorCategory.UNKNOWN and attempt >= 1:
                logger.info(
                    f"Tool {tool_call.name} unknown error persists after retry — giving up"
                )
                self._metrics.record_tool_execution(
                    tool_name=tool_call.name,
                    execution_time_ms=result.execution_time_ms,
                    success=False,
                )
                return result

            # Last attempt exhausted
            if attempt == max_attempts - 1:
                self._metrics.record_tool_execution(
                    tool_name=tool_call.name,
                    execution_time_ms=result.execution_time_ms,
                    success=False,
                )
                return result

            # Retryable: compute delay based on error type
            if category == ErrorCategory.SERVER_CRASH:
                delay = 5.0  # Longer delay for crash recovery
            else:
                delay = RETRY_BASE_DELAY_S * (2 ** attempt)

            logger.info(
                f"Retrying {tool_call.name} after [{category.value}] failure "
                f"(attempt {attempt + 1}/{max_attempts}, delay {delay:.1f}s): "
                f"{(result.error or '')[:100]}"
            )
            await asyncio.sleep(delay)

        return last_result

    async def _execute_tools_parallel(
        self,
        parsed_calls: list[dict],
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        parallel_batch = []
        sequential_queue = []
        seen_signatures: set[tuple[str, str]] = set()

        for tc_data in parsed_calls:
            func = tc_data.get("function", {})
            tool_name = func.get("name", "")
            raw_args = func.get("arguments", "{}")
            try:
                tool_args = json.loads(raw_args)
            except json.JSONDecodeError:
                tool_args = {}

            # Deduplicate: skip tool calls with identical (name, arguments)
            sig = (tool_name, raw_args)
            if sig in seen_signatures:
                logger.warning(
                    f"Skipping duplicate tool call in parallel batch: {tool_name}"
                )
                continue
            seen_signatures.add(sig)

            is_control = tool_name in ("task_complete", "ask_human")
            tier, _reason = self._guardrails.check_tool(
                tool_name, tool_args, task.config,
                tool_registry=self._tools,
            )

            if is_control or tier in (ApprovalTier.CONFIRM, ApprovalTier.BLOCK):
                sequential_queue.append(tc_data)
            else:
                parallel_batch.append(tc_data)

        if len(parallel_batch) > 1:
            logger.info(
                f"Parallel tool dispatch: {len(parallel_batch)} tools "
                f"({', '.join(tc.get('function', {}).get('name', '?') for tc in parallel_batch)})"
            )

            parallel_limit = self._compute_parallel_limit(parallel_batch)
            semaphore = asyncio.Semaphore(parallel_limit)

            async def _run_one(tc_data: dict) -> tuple[dict, ToolCall, ToolResult]:
                async with semaphore:
                    func = tc_data.get("function", {})
                    tool_name = func.get("name", "")
                    try:
                        tool_args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        tool_args = {}

                    tool_call = ToolCall(
                        id=tc_data.get("id", "call"),
                        name=tool_name,
                        arguments=tool_args,
                    )
                    tool_context = {"workspace_id": task.workspace_id} if task.workspace_id else {}
                    result = await self._execute_tool_with_retry(tool_call, tool_context)
                    return tc_data, tool_call, result

            results = await asyncio.gather(
                *(_run_one(tc) for tc in parallel_batch),
                return_exceptions=True,
            )

            for item in results:
                if isinstance(item, Exception):
                    logger.error(f"Parallel tool execution error: {item}")
                    continue

                tc_data, tool_call, result = item
                func = tc_data.get("function", {})
                tool_name = func.get("name", "")
                try:
                    tool_args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_args = {}

                task.tool_calls_count += 1

                if self._evidence_chain:
                    self._evidence_chain.record_tool_decision(
                        tool_name=tool_name,
                        args=tool_args,
                        reasoning=f"LLM selected {tool_name} (parallel batch) for step: {step.description[:80]}",
                    )

                turn_id = tc_data.get("turn_id")
                if not turn_id:
                    # Fallback if somehow not set
                    turn_id = str(uuid.uuid4())
                
                step.tool_calls.append({
                    "id": tool_call.id,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": result.output if result.success else result.error,
                    "success": result.success,
                    "time_ms": result.execution_time_ms,
                    "turn_id": turn_id,
                    **({"_gemini_raw_part": tc_data["_gemini_raw_part"]} if "_gemini_raw_part" in tc_data else {}),
                })

                # Record in diagnostic tracker
                tracker = self._get_tracker(step.id)
                tracker.record(
                    tool_name=tool_name,
                    args=tool_args,
                    result_output=result.output if result.success else "",
                    success=result.success,
                    error=result.error if not result.success else None,
                )

                yield TaskEvent(
                    type=TaskEventType.TOOL_CALLED,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_args=json.dumps(tool_args),
                    progress=self._progress_callback(task),
                )
                yield TaskEvent(
                    type=TaskEventType.TOOL_RESULT,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_result=result.output if result.success else result.error,
                    progress=self._progress_callback(task),
                )

                if not result.success:
                    step.error = result.error

                budget_error = self._guardrails.check_budget(task)
                if budget_error:
                    logger.warning(f"Budget exceeded during parallel tools: {budget_error}")
                    step.status = StepStatus.COMPLETE
                    step.result = f"Stopped: {budget_error}"
                    step.completed_at = datetime.now(timezone.utc)
                    await self._persistence.update_task(task)
                    return

        elif len(parallel_batch) == 1:
            sequential_queue = parallel_batch + sequential_queue

        for tc_data in sequential_queue:
            async for event in self._execute_single_tool(tc_data, task, step):
                yield event
                if step.status in (StepStatus.COMPLETE, StepStatus.FAILED, StepStatus.SKIPPED):
                    return

        await self._persistence.update_task(task)

    async def _execute_single_tool(
        self,
        tc_data: dict,
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        func = tc_data.get("function", {})
        tool_name = func.get("name", "")
        try:
            tool_args = json.loads(func.get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_args = {}

        tool_call = ToolCall(
            id=tc_data.get("id", "call"),
            name=tool_name,
            arguments=tool_args,
        )

        tier, tier_reason = self._guardrails.check_tool(
            tool_name, tool_args, task.config,
            tool_registry=self._tools,
        )

        if tier == ApprovalTier.BLOCK:
            # Blocked pattern — fail the step immediately
            yield TaskEvent(
                type=TaskEventType.TOOL_RESULT,
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_result=tier_reason,
                progress=self._progress_callback(task),
            )
            return

        if tier == ApprovalTier.INFORM:
            # Auto-approved but notable — notify user without blocking
            yield TaskEvent(
                type=TaskEventType.TOOL_AUTO_APPROVED,
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_args=json.dumps(tool_args),
                content=tier_reason,
                progress=self._progress_callback(task),
            )
            # Fall through to execute the tool

        if tier == ApprovalTier.CONFIRM:
            request = ApprovalRequest(
                id=str(uuid.uuid4()),
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_args=tool_args,
                risk_level=self._tools.get_risk_level(tool_name),
                reason=tier_reason,
            )
            task.pending_approval = request
            await self._persistence.save_approval(request)

            yield TaskEvent(
                type=TaskEventType.APPROVAL_NEEDED,
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_args=json.dumps(tool_args),
                approval_id=request.id,
                content=tier_reason,
                progress=self._progress_callback(task),
            )
            # Generator suspends here. The loop in loop.py waits for
            # human approval (polling the DB).  When the consumer resumes
            # iteration, execution falls through to the tool-execution
            # code below — so the approved tool actually runs.
            # If the approval is denied the loop breaks and this
            # generator is cleaned up without reaching the code below.

        # SILENT tier: no notification, just execute

        # --- INTERCEPT PREMATURE TASK_COMPLETE BEFORE EXECUTION ---
        if tool_name == "task_complete":
            pending_steps = [
                s for s in task.plan.steps
                if s.status == StepStatus.PENDING and s.id != step.id
            ]
            if pending_steps:
                # The LLM believes this step is done.  Instead of returning
                # an error (which keeps the step IN_PROGRESS and causes the
                # outer loop to re-invoke _reason_and_act — hammering the
                # LLM API until the iteration budget is exhausted), auto-
                # complete the *current* step so the system advances.
                summary = tool_args.get("summary", step.result or "Step completed")

                logger.info(
                    f"task_complete called with {len(pending_steps)} pending step(s) — "
                    f"auto-completing current step '{step.description[:60]}' and advancing."
                )

                step.status = StepStatus.COMPLETE
                step.result = summary
                step.completed_at = datetime.now(timezone.utc)

                if not getattr(step, "tool_calls", None):
                    step.tool_calls = []

                step.tool_calls.append({
                    "id": tool_call.id,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": summary,
                    "success": True,
                    "time_ms": 0,
                    **({"_gemini_raw_part": tc_data["_gemini_raw_part"]} if "_gemini_raw_part" in tc_data else {}),
                })

                yield TaskEvent(
                    type=TaskEventType.TOOL_CALLED,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_args=json.dumps(tool_args),
                    progress=self._progress_callback(task),
                )

                yield TaskEvent(
                    type=TaskEventType.TOOL_RESULT,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_result=summary,
                    progress=self._progress_callback(task),
                )

                await self._persistence.update_task(task)
                return

        yield TaskEvent(
            type=TaskEventType.TOOL_CALLED,
            task_id=task.id,
            step_id=step.id,
            tool_name=tool_name,
            tool_args=json.dumps(tool_args),
            progress=self._progress_callback(task),
        )

        if self._evidence_chain:
            self._evidence_chain.record_tool_decision(
                tool_name=tool_name,
                args=tool_args,
                reasoning=f"LLM selected {tool_name} for step: {step.description[:80]}",
            )

        tool_context = {"workspace_id": task.workspace_id} if task.workspace_id else {}
        result = await self._execute_tool_with_retry(tool_call, tool_context)
        task.tool_calls_count += 1

        budget_error = self._guardrails.check_budget(task)
        if budget_error:
            logger.warning(f"Budget exceeded mid-step: {budget_error}")
            step.status = StepStatus.COMPLETE
            step.result = f"Stopped: {budget_error}"
            step.completed_at = datetime.now(timezone.utc)
            await self._persistence.update_task(task)
            yield TaskEvent(
                type=TaskEventType.TOOL_RESULT,
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_result=result.output if result.success else result.error,
                progress=self._progress_callback(task),
            )
            return

        step.tool_calls.append({
            "id": tool_call.id,
            "tool": tool_name,
            "args": tool_args,
            "result": result.output if result.success else result.error,
            "success": result.success,
            "time_ms": result.execution_time_ms,
            "turn_id": tc_data.get("turn_id", str(uuid.uuid4())),
            **({"_gemini_raw_part": tc_data["_gemini_raw_part"]} if "_gemini_raw_part" in tc_data else {}),
        })

        # Record in diagnostic tracker
        tracker = self._get_tracker(step.id)
        tracker.record(
            tool_name=tool_name,
            args=tool_args,
            result_output=result.output if result.success else "",
            success=result.success,
            error=result.error if not result.success else None,
        )

        yield TaskEvent(
            type=TaskEventType.TOOL_RESULT,
            task_id=task.id,
            step_id=step.id,
            tool_name=tool_name,
            tool_result=result.output if result.success else result.error,
            progress=self._progress_callback(task),
        )

        if tool_name == "task_complete":
            if self._verifier:
                summary = tool_args.get("summary", result.output)
                
                yield TaskEvent(
                    type=TaskEventType.VERIFIER_STARTED,
                    task_id=task.id,
                    step_id=step.id,
                    content="Verifying task complete claims against accumulated evidence...",
                    progress=self._progress_callback(task),
                )
                
                passed, critique = await self._verifier.verify(task.goal, summary, self._evidence_chain)
                
                if not passed:
                    error_msg = f"Verification Failed. You must fix these unsupported claims before completing the task:\n{critique}"

                    yield TaskEvent(
                        type=TaskEventType.VERIFIER_FAILED,
                        task_id=task.id,
                        step_id=step.id,
                        content=error_msg,
                        progress=self._progress_callback(task),
                    )

                    if self._metrics:
                        self._metrics.record_verifier_result(False, critique)

                    # Overwrite the success/output so the agent sees the failure
                    step.tool_calls[-1]["result"] = error_msg
                    step.tool_calls[-1]["success"] = False

                    # Track the error so the agent loop can detect the
                    # failure and manage retries properly (without this,
                    # step.status stays IN_PROGRESS and the loop cannot
                    # distinguish a verifier rejection from a normal turn).
                    step.error = error_msg
                    step.status = StepStatus.FAILED
                    await self._persistence.update_task(task)

                    return # Abort completion, let agent retry

                if self._metrics:
                    self._metrics.record_verifier_result(True, critique)
                
                yield TaskEvent(
                    type=TaskEventType.VERIFIER_PASSED,
                    task_id=task.id,
                    step_id=step.id,
                    content=f"Verification passed. {critique}",
                    progress=self._progress_callback(task),
                )

            step.status = StepStatus.COMPLETE
            step.result = tool_args.get("summary", result.output)
            step.completed_at = datetime.now(timezone.utc)

            # At this point, pending_steps is guaranteed to be empty because we 
            # intercepted it above if it wasn't. It's safe to skip any lingering 
            # in-progress continuous steps or leftover steps.
            for remaining in task.plan.steps:
                if remaining.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS) and remaining.id != step.id:
                    remaining.status = StepStatus.SKIPPED
                    remaining.result = "Skipped — task completed early"
                    remaining.completed_at = datetime.now(timezone.utc)

        elif tool_name == "ask_human":
            question = tool_args.get("question", "The agent needs your input")
            
            if getattr(task, "messages", None) is not None:
                # Chat Mode: yield the question and end the task.
                # Do not set pending_approval so AgentLoop doesn't block.
                yield TaskEvent(
                    type=TaskEventType.APPROVAL_NEEDED,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name="ask_human",
                    content=question,
                    approval_id="",
                    progress=self._progress_callback(task),
                )
                
                step.result = "Asked user in chat. Ending current iteration."
                step.status = StepStatus.COMPLETE
                step.completed_at = datetime.now(timezone.utc)
                
                # Mark remaining steps as skipped so the loop completes naturally
                for remaining in task.plan.steps:
                    if remaining.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS) and remaining.id != step.id:
                        remaining.status = StepStatus.SKIPPED
                        remaining.result = "Skipped — waiting for user reply in chat"
                        remaining.completed_at = datetime.now(timezone.utc)
                
                await self._persistence.update_task(task)
                return

            approval_request = ApprovalRequest(
                id=str(uuid.uuid4()),
                task_id=task.id,
                step_id=step.id,
                tool_name="ask_human",
                tool_args=tool_args,
                reason=question,
            )
            task.pending_approval = approval_request
            task.status = TaskStatus.WAITING_APPROVAL
            await self._persistence.save_approval(approval_request)
            await self._persistence.update_task(task)

            yield TaskEvent(
                type=TaskEventType.APPROVAL_NEEDED,
                task_id=task.id,
                step_id=step.id,
                tool_name="ask_human",
                content=question,
                approval_id=approval_request.id,
                progress=self._progress_callback(task),
            )

            approved = await self._wait_for_approval(task)
            task.status = TaskStatus.RUNNING

            if not approved:
                step.result = "User did not respond / declined"
                step.status = StepStatus.COMPLETE
                step.completed_at = datetime.now(timezone.utc)

        elif not result.success:
            step.error = result.error

        await self._persistence.update_task(task)

    async def _reason_and_act(
        self,
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        observations = "\n".join(
            f"[{tc.get('tool', '?')}] → {tc.get('result', '?')}"
            for tc in step.tool_calls
        ) or "(none yet)"

        done, total = task.plan.progress

        # Build diagnostic context from tracked attempts
        tracker = self._get_tracker(step.id)
        diagnostic_context = tracker.build_diagnostic_prompt()

        if task.messages:
            messages = list(task.messages)
            
            # Inject the agent execution rules and context so the LLM
            # knows it's executing a specific step in a plan.
            # Use compact prompt for local models to reduce input tokens
            _is_local_strategy = str(getattr(self._model_router, '_strategy', '')) == 'local_first'
            _prompt_template = AGENT_SYSTEM_PROMPT_LOCAL if _is_local_strategy else AGENT_SYSTEM_PROMPT
            system_prompt = _prompt_template.format(
                goal=task.goal,
                step_description=step.description,
                step_index=step.index + 1,
                total_steps=total,
                iteration=task.iterations,
                max_iterations=task.config.max_iterations,
                observations=observations,
                diagnostic_context=("\n" + diagnostic_context + "\n") if diagnostic_context else "",
            )
            # Inject persona context if available
            if self._persona_context:
                system_prompt += f"\n\n{self._persona_context}"

            messages.append({
                "role": "user",
                "content": f"[System Instructions]\n{system_prompt}",
            })
            
            # Group tool calls by turn_id so parallel tool calls from
            # the same LLM response stay in a single assistant message.
            # This is required for Gemini's thought_signature validation.
            recent_tcs = step.tool_calls[-10:]
            grouped_turns = {}
            for tc in recent_tcs:
                tid = tc.get("turn_id", tc.get("id", str(uuid.uuid4())))
                if tid not in grouped_turns:
                    grouped_turns[tid] = []
                grouped_turns[tid].append(tc)

            for tid, calls in grouped_turns.items():
                tcs = []
                for tc in calls:
                    tcs.append({
                        "id": tc.get("id", "call_1"),
                        "type": "function",
                        "function": {
                            "name": tc.get("tool", ""),
                            "arguments": json.dumps(tc.get("args", {})),
                        },
                        **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                    })

                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tcs,
                })

                for tc in calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", "call_1"),
                        "content": tc.get("result", ""),
                    })
        else:
            _is_local_strategy = str(getattr(self._model_router, '_strategy', '')) == 'local_first'
            _prompt_template = AGENT_SYSTEM_PROMPT_LOCAL if _is_local_strategy else AGENT_SYSTEM_PROMPT
            system_prompt = _prompt_template.format(
                goal=task.goal,
                step_description=step.description,
                step_index=step.index + 1,
                total_steps=total,
                iteration=task.iterations,
                max_iterations=task.config.max_iterations,
                observations=observations,
                diagnostic_context=("\n" + diagnostic_context + "\n") if diagnostic_context else "",
            )
            # Inject persona context if available
            if self._persona_context:
                system_prompt += f"\n\n{self._persona_context}"

            messages = [{"role": "system", "content": system_prompt}]

            if not step.tool_calls:
                messages.append({
                    "role": "user",
                    "content": f"Execute this step: {step.description}",
                })
            else:
                # Show up to 10 recent tool calls; if older calls were
                # trimmed, prepend a compact summary so the LLM knows
                # what it already tried (prevents redundant retries).
                recent_window = 10
                skipped = step.tool_calls[:-recent_window] if len(step.tool_calls) > recent_window else []
                recent = step.tool_calls[-recent_window:]

                continue_msg = f"Continue executing: {step.description}"
                if skipped:
                    summary_lines = []
                    for tc in skipped:
                        status = "✓" if tc.get("success") else "✗"
                        summary_lines.append(
                            f"  {status} {tc.get('tool', '?')}({json.dumps(tc.get('args', {}))[:60]})"
                        )
                    continue_msg += (
                        f"\n\nEarlier attempts ({len(skipped)} calls, summarized):\n"
                        + "\n".join(summary_lines)
                    )

                messages.append({
                    "role": "user",
                    "content": continue_msg,
                })
                grouped_turns = {}
                for tc in recent:
                    tid = tc.get("turn_id", tc.get("id", str(uuid.uuid4())))
                    if tid not in grouped_turns:
                        grouped_turns[tid] = []
                    grouped_turns[tid].append(tc)

                for tid, calls in grouped_turns.items():
                    tcs = []
                    for tc in calls:
                        tcs.append({
                            "id": tc.get("id", "call_1"),
                            "type": "function",
                            "function": {
                                "name": tc.get("tool", ""),
                                "arguments": json.dumps(tc.get("args", {})),
                            },
                            **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                        })

                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tcs,
                    })

                    for tc in calls:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", "call_1"),
                            "content": tc.get("result", ""),
                        })

        # ── Conversational shortcut ────────────────────────────────
        # For simple chat messages (greetings, small talk, basic Q&A),
        # skip tool-calling entirely and stream a direct text response.
        # This prevents the LLM from proactively calling tools like
        # moltbook when the user just says "hey kestrel!".
        #
        # Criteria for the shortcut:
        #   1. Chat mode (task.messages is set — came from StreamChat)
        #   2. No prior tool calls on this step (first iteration)
        #   3. Step was pre-classified as simple ("Respond to the user")
        _is_simple_chat = (
            task.messages
            and not step.tool_calls
            and step.description.startswith("Respond to the user")
        )

        if _is_simple_chat:
            # Stream directly — no tools offered to the LLM
            logger.info("Simple chat shortcut: streaming without tools")

            route = self._model_router.select(
                step_description=step.description,
                expected_tools=[],
            )
            routed_model = route.model if route.model else self._model
            routed_temp = route.temperature
            routed_max_tokens = route.max_tokens
            if self._provider_resolver and route.provider:
                try:
                    active_provider = self._provider_resolver(route.provider)
                except Exception:
                    active_provider = self._provider
            else:
                active_provider = self._provider

            # If the selected provider isn't ready (e.g. ollama from Docker),
            # fall back to a cloud provider — but NOT if user explicitly set a local model
            if (
                hasattr(active_provider, 'is_ready')
                and not active_provider.is_ready()
                and not self._has_explicit_model
            ):
                logger.info(f"Provider {route.provider} not ready, trying cloud fallback")
                if self._provider_resolver:
                    for cloud_name in ("google", "openai", "anthropic"):
                        try:
                            cloud_p = self._provider_resolver(cloud_name)
                            actual_provider_name = getattr(cloud_p, "provider", "")
                            if actual_provider_name == cloud_name and cloud_p.is_ready():
                                active_provider = cloud_p
                                routed_model = ""  # Use provider's default
                                logger.info(f"Fell back to {cloud_name}")
                                break
                        except Exception:
                            continue

            # Apply context compaction + cloud escalation (same as full path)
            try:
                from agent.context_compactor import compact_context, needs_escalation

                messages, was_compacted = await compact_context(
                    messages=messages,
                    provider_name=route.provider,
                    provider=active_provider,
                    model=routed_model,
                )

                if was_compacted and needs_escalation(messages, route.provider, model=routed_model) and not self._has_explicit_model:
                    if self._provider_resolver:
                        for cloud_name in ("google", "openai", "anthropic"):
                            try:
                                cloud_p = self._provider_resolver(cloud_name)
                                actual_provider_name = getattr(cloud_p, "provider", "")
                                if actual_provider_name == cloud_name and cloud_p.is_ready():
                                    logger.info(
                                        f"Context overflow: escalating {route.provider} → {cloud_name}"
                                    )
                                    active_provider = cloud_p
                                    routed_model = ""
                                    break
                            except Exception:
                                continue
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"Context compaction failed (non-fatal): {e}")

            if self._event_callback:
                try:
                    await self._event_callback("routing_info", {
                        "provider": route.provider,
                        "model": routed_model,
                        "was_escalated": False,
                        "complexity": 0,
                    })
                except Exception:
                    pass

            # Stream tokens directly so the user sees real-time output.
            # Parse <think>...</think> tags into THINKING events.
            streamed_text = []
            think_buffer = []
            in_think = False

            async def _do_stream(provider_to_use, model_to_use):
                nonlocal in_think
                async for token in provider_to_use.stream(
                    messages=messages,
                    model=model_to_use,
                    temperature=routed_temp,
                    max_tokens=routed_max_tokens,
                ):
                    # Detect <think> tags
                    combined = "".join(think_buffer) + token if think_buffer else token

                    if not in_think and "<think>" in combined:
                        in_think = True
                        # Split: before <think> goes as content, after starts thinking
                        before = combined.split("<think>", 1)[0]
                        after = combined.split("<think>", 1)[1]
                        if before.strip():
                            streamed_text.append(before)
                            yield TaskEvent(
                                type=TaskEventType.STEP_COMPLETE,
                                task_id=task.id,
                                step_id=step.id,
                                content=before,
                            )
                        think_buffer.clear()
                        think_buffer.append(after)
                        continue

                    if in_think:
                        think_buffer.append(token)
                        joined = "".join(think_buffer)
                        if "</think>" in joined:
                            # End of thinking block
                            think_content = joined.split("</think>", 1)[0]
                            remainder = joined.split("</think>", 1)[1]
                            in_think = False
                            think_buffer.clear()
                            # Emit thinking event
                            if think_content.strip():
                                yield TaskEvent(
                                    type=TaskEventType.THINKING,
                                    task_id=task.id,
                                    step_id=step.id,
                                    content=think_content.strip(),
                                )
                            # Emit any text after </think>
                            if remainder.strip():
                                streamed_text.append(remainder)
                                yield TaskEvent(
                                    type=TaskEventType.STEP_COMPLETE,
                                    task_id=task.id,
                                    step_id=step.id,
                                    content=remainder,
                                )
                        continue

                    # Regular token — stream it
                    streamed_text.append(token)
                    yield TaskEvent(
                        type=TaskEventType.STEP_COMPLETE,
                        task_id=task.id,
                        step_id=step.id,
                        content=token,
                    )

                # Flush any remaining think buffer as thinking
                if think_buffer:
                    remaining = "".join(think_buffer)
                    if remaining.strip():
                        if in_think:
                            yield TaskEvent(
                                type=TaskEventType.THINKING,
                                task_id=task.id,
                                step_id=step.id,
                                content=remaining.strip(),
                            )
                        else:
                            streamed_text.append(remaining)
                            yield TaskEvent(
                                type=TaskEventType.STEP_COMPLETE,
                                task_id=task.id,
                                step_id=step.id,
                                content=remaining,
                            )
                    think_buffer.clear()

            try:
                async for event in _do_stream(active_provider, routed_model):
                    yield event
            except Exception as chat_err:
                # ── Cloud failover for simple chat path ──────────────
                from providers.ollama import OllamaUnavailableError
                from providers.lmstudio import LMStudioUnavailableError
                is_local_failure = (
                    isinstance(chat_err, (OllamaUnavailableError, LMStudioUnavailableError))
                    or 'timeout' in str(chat_err).lower()
                    or 'connect' in str(chat_err).lower()
                )
                if is_local_failure and self._provider_resolver:
                    logger.warning(
                        f"Simple chat: {route.provider} failed ({chat_err}), "
                        f"attempting cloud failover..."
                    )
                    from providers_registry import get_cloud_fallback
                    fallback = get_cloud_fallback()
                    if fallback:
                        cloud_name, cloud_p = fallback
                        logger.info(f"Simple chat: falling back to {cloud_name}")
                        async for event in _do_stream(cloud_p, ""):
                            yield event
                    else:
                        raise
                else:
                    raise

            text = "".join(streamed_text)
            logger.info(
                f"Simple chat result: {len(text)} chars, "
                f"preview={text[:100]!r}"
            )
            step.status = StepStatus.COMPLETE
            step.result = text
            step.completed_at = datetime.now(timezone.utc)
            await self._persistence.update_task(task)
            return

        # ── Full tool-calling path (complex messages / agentic tasks) ──

        # Select relevant tools instead of sending all 38 schemas.
        # This keeps the context small for local models and improves accuracy.
        step_expected = getattr(step, 'expected_tools', None)

        route = self._model_router.select(
            step_description=step.description,
            expected_tools=step_expected,
        )

        routed_model = route.model if route.model else self._model
        routed_temp = route.temperature
        routed_max_tokens = route.max_tokens

        if self._provider_resolver and route.provider:
            try:
                active_provider = self._provider_resolver(route.provider)
            except Exception:
                active_provider = self._provider
        else:
            active_provider = self._provider

        try:
            from agent.tool_selector import ToolSelector
            selector = ToolSelector(self._tools.list_tools())
            is_local = route.provider in ("ollama", "local", "lmstudio")
            runtime_mode = os.getenv("KESTREL_RUNTIME_MODE", "docker")
            intent_tags: list[str] = []
            approval_state = (
                task.pending_approval.status.value
                if task.pending_approval and getattr(task.pending_approval, "status", None)
                else "pending"
            )

            if is_local:
                # Local models: use instant keyword matching (no extra LLM call)
                selected_tools = selector.select(
                    step_description=step.description,
                    expected_tools=step_expected,
                    provider=route.provider,
                    runtime_mode=runtime_mode,
                    intent_tags=intent_tags,
                    approval_state=approval_state,
                )
            else:
                # Cloud models: use LLM picker to save token cost
                try:
                    selected_tools = await selector.select_with_llm(
                        step_description=step.description,
                        provider=active_provider,
                        model=routed_model,
                        api_key=self._api_key,
                        expected_tools=step_expected,
                        runtime_mode=runtime_mode,
                        intent_tags=intent_tags,
                        approval_state=approval_state,
                    )
                except Exception as e:
                    logger.warning(f"LLM tool selection failed: {e}, using keyword fallback")
                    selected_tools = selector.select(
                        step_description=step.description,
                        expected_tools=step_expected,
                        provider=route.provider,
                        runtime_mode=runtime_mode,
                        intent_tags=intent_tags,
                        approval_state=approval_state,
                    )

            tool_schemas = [t.to_openai_schema() for t in selected_tools]
        except Exception as e:
            logger.warning(f"ToolSelector failed, using all tools: {e}")
            tool_schemas = [t.to_openai_schema() for t in self._tools.list_tools()]

        try:
            from agent.context_compactor import compact_context, needs_escalation

            messages, was_compacted = await compact_context(
                messages=messages,
                provider_name=route.provider,
                provider=active_provider,
                model=routed_model,
            )

            if was_compacted and needs_escalation(messages, route.provider, model=routed_model) and not self._has_explicit_model:
                if self._provider_resolver:
                    for cloud_name in ("google", "openai", "anthropic"):
                        try:
                            cloud_p = self._provider_resolver(cloud_name)
                            # Provider resolver may silently fall back to ollama if cloud is not ready.
                            # Ensure we actually got the cloud provider we asked for before blanking the model.
                            actual_provider_name = getattr(cloud_p, "provider", "")
                            if actual_provider_name == cloud_name and cloud_p.is_ready():
                                logger.info(
                                    f"Context overflow: escalating {route.provider} → {cloud_name}"
                                )
                                active_provider = cloud_p
                                routed_model = ""
                                break
                        except Exception:
                            continue
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Context compaction failed (non-fatal): {e}")

        if self._event_callback:
            try:
                await self._event_callback("routing_info", {
                    "provider": route.provider,
                    "model": routed_model or route.model,
                    "was_escalated": getattr(route, '_escalated', False),
                    "complexity": getattr(route, '_complexity', 0),
                })
            except Exception:
                pass

        logger.debug(
            f"Dispatching to {route.provider}:{routed_model} "
            f"(step={step.description[:50]}...)"
        )

        try:
            # Notify UI that we are blocked on tool generation
            # Extremely important for local models like Qwen 3.5 that "think"
            # for 90 seconds without streaming during tool calls.
            yield TaskEvent(
                type=TaskEventType.THINKING,
                task_id=task.id,
                step_id=step.id,
                content="Analyzing prompt to select optimal tools...",
            )
        except Exception:
            pass

        try:
            response = await active_provider.generate_with_tools(
                messages=messages,
                model=routed_model,
                tools=tool_schemas,
                temperature=routed_temp,
                max_tokens=routed_max_tokens,
                api_key=self._api_key,
            )

            # Check for error content from the provider (not an exception)
            content = response.get("content", "")
            if content.startswith("[Error:") and not response.get("tool_calls"):
                raise RuntimeError(content)

        except Exception as llm_err:
            # ── Graceful cloud failover ──────────────────────────────
            # If the local provider failed, swap to a cloud provider and
            # retry with the same context.
            #
            # ALWAYS failover when a local provider is genuinely unavailable:
            #   - OllamaUnavailableError / LMStudioUnavailableError
            #   - 429 rate limit
            # These indicate the provider cannot serve ANY request, so
            # respecting _has_explicit_model would just cause task failure.
            from providers.ollama import OllamaUnavailableError
            from providers.lmstudio import LMStudioUnavailableError
            is_local_down = isinstance(llm_err, (OllamaUnavailableError, LMStudioUnavailableError))
            is_rate_limited = '429' in str(llm_err)
            is_timeout = 'timeout' in str(llm_err).lower()
            is_connection_error = 'connect' in str(llm_err).lower()
            should_always_failover = is_local_down or is_rate_limited or is_timeout or is_connection_error

            if route.provider in ("ollama", "lmstudio", "local") and self._provider_resolver and (
                not self._has_explicit_model or should_always_failover
            ):
                failover_reason = (
                    "Local provider unavailable" if is_local_down else
                    "429 rate limit" if is_rate_limited else
                    "timeout" if is_timeout else
                    "connection error" if is_connection_error else
                    "error"
                )
                logger.warning(
                    f"Provider {route.provider} failed: {type(llm_err).__name__}: {llm_err}. "
                    f"Attempting cloud failover ({failover_reason})..."
                )
                # Build failover list: workspace's configured provider first
                configured = getattr(self._provider, 'provider', '')
                cloud_order = [configured] if configured in ("google", "openai", "anthropic") else []
                for c in ("google", "openai", "anthropic"):
                    if c not in cloud_order:
                        cloud_order.append(c)

                for cloud_name in cloud_order:
                    try:
                        # Use get_provider directly — NOT self._provider_resolver
                        # which is resolve_provider() and would fall back to ollama.
                        from providers_registry import get_provider
                        cloud_p = get_provider(cloud_name)
                        if not cloud_p.is_ready():
                            continue

                        # Re-select tools with cloud limits (can handle more)
                        try:
                            from agent.tool_selector import ToolSelector
                            selector = ToolSelector(self._tools.list_tools())
                            cloud_tools = selector.select(
                                step_description=step.description,
                                expected_tools=step_expected,
                                provider=cloud_name,
                                runtime_mode=runtime_mode,
                                intent_tags=intent_tags,
                                approval_state=approval_state,
                            )
                            cloud_schemas = [t.to_openai_schema() for t in cloud_tools]
                        except Exception:
                            cloud_schemas = tool_schemas

                        logger.info(
                            f"Cloud failover: {route.provider} → {cloud_name} "
                            f"({len(cloud_schemas)} tools)"
                        )

                        if self._event_callback:
                            try:
                                await self._event_callback("routing_info", {
                                    "provider": cloud_name,
                                    "model": "",
                                    "was_escalated": True,
                                    "complexity": 0,
                                })
                            except Exception:
                                pass

                        try:
                            yield TaskEvent(
                                type=TaskEventType.THINKING,
                                task_id=task.id,
                                step_id=step.id,
                                content=f"Analyzing prompt with {cloud_name} fallback...",
                            )
                        except Exception:
                            pass

                        response = await cloud_p.generate_with_tools(
                            messages=messages,
                            model="",  # Use provider's default
                            tools=cloud_schemas,
                            temperature=routed_temp,
                            max_tokens=max(routed_max_tokens, 8192),
                            api_key=self._api_key,
                        )
                        # Update active_provider for subsequent logic
                        active_provider = cloud_p
                        break
                    except Exception as cloud_err:
                        import traceback
                        logger.warning(
                            f"Cloud failover to {cloud_name} failed: "
                            f"{type(cloud_err).__name__}: {cloud_err!r}\n"
                            f"{traceback.format_exc()}"
                        )
                        continue
                else:
                    # All cloud providers failed too
                    logger.error(f"All providers failed for step: {llm_err}")
                    step.status = StepStatus.FAILED
                    step.error = f"All providers failed: {str(llm_err)[:300]}"
                    await self._persistence.update_task(task)
                    yield TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        step_id=step.id,
                        content=step.error,
                        progress=self._progress_callback(task),
                    )
                    return
            else:
                # Non-local provider failed — just fail the step
                logger.error(f"LLM API error during step execution: {llm_err}", exc_info=True)
                step.status = StepStatus.FAILED
                step.error = f"LLM API error: {str(llm_err)[:300]}"
                await self._persistence.update_task(task)
                yield TaskEvent(
                    type=TaskEventType.TASK_FAILED,
                    task_id=task.id,
                    step_id=step.id,
                    content=step.error,
                    progress=self._progress_callback(task),
                )
                return

        if response.get("usage"):
            usage = response["usage"]
            self._metrics.record_llm_call(
                model=self._model,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cached_tokens=usage.get("cached_tokens", 0),
            )
            if self._event_callback:
                await self._event_callback("metrics_update", self._metrics.metrics.to_compact_dict())

        if response.get("tool_calls"):
            tool_calls = response["tool_calls"]
            logger.info(
                f"LLM returned {len(tool_calls)} tool call(s): "
                f"{[tc.get('function', {}).get('name', '?') for tc in tool_calls]}"
            )

            # Reset text-only streak — LLM is actively using tools
            self._text_only_streak[step.id] = 0

            # Capture LLM's text content alongside tool calls
            # (e.g. "I'll check the MCP server..." before calling tools).
            # This ensures step.result has meaningful content even if
            # task_complete is never explicitly called.
            if response.get("content") and not step.result:
                step.result = response["content"]

            if len(tool_calls) > 1:
                logger.info(f"LLM returned {len(tool_calls)} tool calls — dispatching in parallel")

                # Assign a unified turn_id to all tool calls from this batch
                turn_id = str(uuid.uuid4())
                for tc in tool_calls:
                    tc["turn_id"] = turn_id

                async for event in self._execute_tools_parallel(tool_calls, task, step):
                    yield event
            else:
                tc = tool_calls[0]
                tc["turn_id"] = str(uuid.uuid4())
                async for event in self._execute_single_tool(tc, task, step):
                    yield event

        elif response.get("content"):
            text = response["content"]

            logger.info(
                f"LLM returned text-only (no tool calls): "
                f"streak={self._text_only_streak.get(step.id, 0)+1}, "
                f"total={self._text_only_total.get(step.id, 0)+1}, "
                f"preview={text[:80]!r}"
            )

            yield TaskEvent(
                type=TaskEventType.THINKING,
                task_id=task.id,
                step_id=step.id,
                content=text,
                progress=self._progress_callback(task),
            )

            # Track consecutive text-only responses to detect stuck loops
            self._text_only_streak.setdefault(step.id, 0)
            self._text_only_streak[step.id] += 1

            # Track TOTAL text-only responses (never resets on tool calls).
            # This catches the alternating pattern: text → tool → text → tool
            # where the consecutive streak resets but the LLM is clearly stuck.
            self._text_only_total.setdefault(step.id, 0)
            self._text_only_total[step.id] += 1

            is_simple_chat = bool(task.messages and total == 1)
            has_done_work = bool(step.tool_calls)
            is_done_phrase = any(phrase in text.lower() for phrase in [
                "step is complete",
                "this step is done",
                "completed this step",
                "no tools needed",
                "done!",
                "here's the summary",
                "here's what was",
                "here's what i",
                "task complete",
                "all tasks completed",
                "cleanup complete",
                "i've completed",
                "successfully completed",
                "successfully deleted",
                "actions taken",
            ])
            # Safety valve: if LLM returns text-only 2+ times in a row,
            # it's stuck in a summary loop — force-complete the step.
            has_high_streak = self._text_only_streak[step.id] >= 2
            # Also force-complete if total text-only responses (including
            # non-consecutive) exceed threshold — catches alternating
            # text/tool-call loops where the streak keeps resetting.
            has_excessive_text = self._text_only_total[step.id] >= 5

            # Always capture the LLM's text — even if we don't mark
            # the step complete yet, the text may contain useful output
            # and ensures the deadlock detector treats partial work as
            # complete rather than failed.
            step.result = text

            if is_simple_chat or has_done_work or is_done_phrase or has_high_streak or has_excessive_text:
                step.status = StepStatus.COMPLETE
                step.completed_at = datetime.now(timezone.utc)
                await self._persistence.update_task(task)

        else:
            # LLM returned no tool calls AND no content — this is an API
            # error or empty response.  Mark the step as failed so the
            # agent loop can retry or surface the error instead of silently
            # falling through to "Task completed successfully."
            logger.warning(
                f"LLM returned empty response for step '{step.description[:80]}' "
                f"(provider={getattr(active_provider, 'provider', '?')}, model={routed_model})"
            )
            step.status = StepStatus.FAILED
            step.error = (
                "LLM returned an empty response (no content and no tool calls). "
                "This usually means the API rejected the request or the model is unavailable."
            )
            await self._persistence.update_task(task)
