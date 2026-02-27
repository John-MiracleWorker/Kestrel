import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

from agent.types import (
    AgentTask,
    ApprovalRequest,
    ApprovalStatus,
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
from agent.diagnostics import DiagnosticTracker

logger = logging.getLogger("brain.agent.core.executor")

# ── Constants ────────────────────────────────────────────────────────
MAX_PARALLEL_TOOLS = 5       # Max concurrent tool executions per turn
RETRY_MAX_ATTEMPTS = 3       # Max retries for transient tool failures
RETRY_BASE_DELAY_S = 1.0     # Base delay for exponential backoff

# ── System Prompt for the Reasoning LLM ──────────────────────────────
AGENT_SYSTEM_PROMPT = """\
You are Kestrel, an autonomous AI agent. You are executing a multi-step task.

Current goal: {goal}
Current step: {step_description}

Instructions:
1. Analyze the current situation and decide which tool to call next.
2. You may call up to 5 tools per turn if they are independent, read-only/low-risk, and do not require approval. Prefer batching and parallel-safe tools. Wait for all results before proceeding.
3. If the step is complete, call `task_complete` with a summary of what you accomplished.
4. If you need clarification from the user, call `ask_human` with your question.
5. Think step-by-step. Explain your reasoning before acting.

Error Recovery Protocol:
- When a tool fails, DIAGNOSE before retrying. Read the error message carefully.
- NEVER retry the exact same tool call with identical arguments — it will fail again.
- If an error is about a missing file/command, verify the path exists first.
- If an error is about dependencies, install them before retrying the operation.
- If an error is about auth/permissions, check credentials before retrying.
- If a server/process crashed, check stderr output and requirements before reconnecting.
- After 3 failures on the same step, STOP and use diagnostic tools (system_health, host_read, host_list) to gather information, or call `ask_human` to ask the user for help.

Verification & Evidence Rules:
- Before calling `task_complete`, you MUST explicitly cite the tool outputs that prove your work in your summary.
- Your final summary will be strictly evaluated by an independent Verifier Engine against your tool execution history.
- If you make unsupported claims or hallucinate actions you didn't take, your completion will be REJECTED.

Host Filesystem Strategy:
- Use project_recall(name) FIRST to check for cached project context.
- Use host_tree(path) ONCE for full directory tree — never call host_list repeatedly.
- Use host_find(pattern) or host_search(query, path) before broad reads to narrow scope quickly.
- Use host_batch_read(paths) for grouped file reads instead of repeated host_read calls.
- host_write requires human approval.

Progress: Step {step_index}/{total_steps} | Iteration {iteration}/{max_iterations}
{diagnostic_context}
Previous observations for this step:
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
        self._step_diagnostics: dict[str, DiagnosticTracker] = {}  # step_id → tracker

    def _get_tracker(self, step_id: str) -> DiagnosticTracker:
        """Get or create a DiagnosticTracker for a step."""
        if step_id not in self._step_diagnostics:
            self._step_diagnostics[step_id] = DiagnosticTracker()
        return self._step_diagnostics[step_id]

    async def _wait_for_approval(self, task: AgentTask) -> bool:
        """
        Block until the pending approval is resolved.
        Returns True if approved, False if denied/expired.
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
                    return status == ApprovalStatus.APPROVED.value
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
                return result

            error_lower = (result.error or "").lower()
            is_transient = any(kw in error_lower for kw in [
                "timeout", "rate limit", "connection", "network",
                "503", "502", "429", "temporarily unavailable",
            ])

            if not is_transient or attempt == max_attempts - 1:
                self._metrics.record_tool_execution(
                    tool_name=tool_call.name,
                    execution_time_ms=result.execution_time_ms,
                    success=False,
                )
                return result

            delay = RETRY_BASE_DELAY_S * (2 ** attempt)
            logger.info(
                f"Retrying {tool_call.name} after transient failure "
                f"(attempt {attempt + 1}/{max_attempts}, delay {delay:.1f}s): "
                f"{result.error[:100]}"
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

        for tc_data in parsed_calls:
            func = tc_data.get("function", {})
            tool_name = func.get("name", "")
            try:
                tool_args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_args = {}

            is_control = tool_name in ("task_complete", "ask_human")
            needs_approval = self._guardrails.needs_approval(
                tool_name, tool_args, task.config,
                tool_registry=self._tools,
            )

            if is_control or needs_approval:
                sequential_queue.append(tc_data)
            else:
                parallel_batch.append(tc_data)

        if len(parallel_batch) > 1:
            logger.info(
                f"Parallel tool dispatch: {len(parallel_batch)} tools "
                f"({', '.join(tc.get('function', {}).get('name', '?') for tc in parallel_batch)})"
            )

            semaphore = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

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

                step.tool_calls.append({
                    "id": tool_call.id,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": result.output if result.success else result.error,
                    "success": result.success,
                    "time_ms": result.execution_time_ms,
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

        approval_needed = self._guardrails.needs_approval(
            tool_name, tool_args, task.config,
            tool_registry=self._tools,
        )

        if approval_needed:
            request = ApprovalRequest(
                id=str(uuid.uuid4()),
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_args=tool_args,
                risk_level=self._tools.get_risk_level(tool_name),
                reason=approval_needed,
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
                content=approval_needed,
                progress=self._progress_callback(task),
            )
            # Generator suspends here. The loop in loop.py waits for
            # human approval (polling the DB).  When the consumer resumes
            # iteration, execution falls through to the tool-execution
            # code below — so the approved tool actually runs.
            # If the approval is denied the loop breaks and this
            # generator is cleaned up without reaching the code below.

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
            for remaining in task.plan.steps:
                if remaining.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS) and remaining.id != step.id:
                    remaining.status = StepStatus.SKIPPED
                    remaining.result = "Skipped — task completed early"
                    remaining.completed_at = datetime.now(timezone.utc)

        elif tool_name == "ask_human":
            question = tool_args.get("question", "The agent needs your input")
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
            # Inject diagnostic context into chat-mode as a system message
            if diagnostic_context:
                messages.append({
                    "role": "system",
                    "content": diagnostic_context,
                })
            for tc in step.tool_calls[-10:]:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tc.get("id", "call_1"),
                        "type": "function",
                        "function": {
                            "name": tc.get("tool", ""),
                            "arguments": json.dumps(tc.get("args", {})),
                        },
                        **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", "call_1"),
                    "content": tc.get("result", ""),
                })
        else:
            system_prompt = AGENT_SYSTEM_PROMPT.format(
                goal=task.goal,
                step_description=step.description,
                step_index=step.index + 1,
                total_steps=total,
                iteration=task.iterations,
                max_iterations=task.config.max_iterations,
                observations=observations,
                diagnostic_context=("\n" + diagnostic_context + "\n") if diagnostic_context else "",
            )

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
                for tc in recent:
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": tc.get("id", "call_1"),
                            "type": "function",
                            "function": {
                                "name": tc.get("tool", ""),
                                "arguments": json.dumps(tc.get("args", {})),
                            },
                            **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                        }],
                    })
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
            # fall back to a cloud provider
            if hasattr(active_provider, 'is_ready') and not active_provider.is_ready():
                logger.info(f"Provider {route.provider} not ready, trying cloud fallback")
                if self._provider_resolver:
                    for cloud_name in ("google", "openai", "anthropic"):
                        try:
                            cloud_p = self._provider_resolver(cloud_name)
                            if cloud_p.is_ready():
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

                if was_compacted and needs_escalation(messages, route.provider):
                    if self._provider_resolver:
                        for cloud_name in ("google", "openai", "anthropic"):
                            try:
                                cloud_p = self._provider_resolver(cloud_name)
                                if cloud_p.is_ready():
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

            # Use generate_with_tools with empty tools list — this gives us
            # the full failover chain while preventing tool calls.
            response = await active_provider.generate_with_tools(
                messages=messages,
                model=routed_model,
                tools=[],  # Empty = no tools, just text response
                temperature=routed_temp,
                max_tokens=routed_max_tokens,
                api_key=self._api_key,
            )

            text = response.get("content", "")
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

        try:
            from agent.tool_selector import ToolSelector
            selector = ToolSelector(self._tools.list_tools())
            selected_tools = selector.select(
                step_description=step.description,
                expected_tools=step_expected,
                provider=route.provider,
            )
            tool_schemas = [t.to_openai_schema() for t in selected_tools]
        except Exception as e:
            logger.warning(f"ToolSelector failed, using all tools: {e}")
            tool_schemas = [t.to_openai_schema() for t in self._tools.list_tools()]
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
            from agent.context_compactor import compact_context, needs_escalation

            messages, was_compacted = await compact_context(
                messages=messages,
                provider_name=route.provider,
                provider=active_provider,
                model=routed_model,
            )

            if was_compacted and needs_escalation(messages, route.provider):
                if self._provider_resolver:
                    for cloud_name in ("google", "openai", "anthropic"):
                        try:
                            cloud_p = self._provider_resolver(cloud_name)
                            if cloud_p.is_ready():
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
            # If the local provider failed (e.g. ollama 400), swap to
            # a cloud provider and retry with the same context.
            if route.provider in ("ollama", "local") and self._provider_resolver:
                logger.warning(
                    f"Provider {route.provider} failed: {llm_err}. "
                    f"Attempting cloud failover..."
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
                        logger.warning(f"Cloud failover to {cloud_name} failed: {cloud_err}")
                        continue
                else:
                    # All cloud providers failed too
                    logger.error(f"All providers failed for step: {llm_err}")
                    step.status = StepStatus.FAILED
                    step.error = f"All providers failed: {str(llm_err)[:300]}"
                    await self._persistence.update_task(task)
                    return
            else:
                # Non-local provider failed — just fail the step
                logger.error(f"LLM API error during step execution: {llm_err}", exc_info=True)
                step.status = StepStatus.FAILED
                step.error = f"LLM API error: {str(llm_err)[:300]}"
                await self._persistence.update_task(task)
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

            # Capture LLM's text content alongside tool calls
            # (e.g. "I'll check the MCP server..." before calling tools).
            # This ensures step.result has meaningful content even if
            # task_complete is never explicitly called.
            if response.get("content") and not step.result:
                step.result = response["content"]

            if len(tool_calls) > 1:
                logger.info(f"LLM returned {len(tool_calls)} tool calls — dispatching in parallel")
                async for event in self._execute_tools_parallel(tool_calls, task, step):
                    yield event
            else:
                async for event in self._execute_single_tool(tool_calls[0], task, step):
                    yield event

        elif response.get("content"):
            text = response["content"]

            if task.messages:
                step.status = StepStatus.COMPLETE
                step.result = text
                step.completed_at = datetime.now(timezone.utc)
                await self._persistence.update_task(task)
            else:
                yield TaskEvent(
                    type=TaskEventType.THINKING,
                    task_id=task.id,
                    step_id=step.id,
                    content=text,
                    progress=self._progress_callback(task),
                )

                if any(phrase in text.lower() for phrase in [
                    "step is complete",
                    "this step is done",
                    "completed this step",
                    "no tools needed",
                ]):
                    step.status = StepStatus.COMPLETE
                    step.result = text
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
