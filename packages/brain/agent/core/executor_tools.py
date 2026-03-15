from __future__ import annotations

from agent.core.executor_shared import *

class TaskExecutorToolsMixin:
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
            tier, _reason = self._resolve_tool_tier(
                task,
                tool_name,
                tool_args,
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
                    tool_context = self._build_tool_context(task)
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
                    "metadata": result.metadata,
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
                    metadata=self._tool_result_event_metadata(result),
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

        tier, tier_reason = self._resolve_tool_tier(
            task,
            tool_name,
            tool_args,
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

        tool_context = self._build_tool_context(task)
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
                metadata=self._tool_result_event_metadata(result),
            )
            return

        step.tool_calls.append({
            "id": tool_call.id,
            "tool": tool_name,
            "args": tool_args,
            "result": result.output if result.success else result.error,
            "success": result.success,
            "time_ms": result.execution_time_ms,
            "metadata": result.metadata,
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
            metadata=self._tool_result_event_metadata(result),
        )

        # ── Auto-inject display_markdown for media gen tools ─────────
        # If the tool result contains display_markdown (e.g. image URLs),
        # inject it directly into the response stream so the user sees the
        # image without relying on the LLM to include it.
        if result.success and tool_name in ("vram_generate_image", "generate_media"):
            try:
                result_data = json.loads(result.output) if isinstance(result.output, str) else result.output
                display_md = result_data.get("display_markdown", "") if isinstance(result_data, dict) else ""
                if display_md:
                    yield TaskEvent(
                        type=TaskEventType.STEP_COMPLETE,
                        task_id=task.id,
                        step_id=step.id,
                        content=f"\n\n{display_md}\n",
                        progress=self._progress_callback(task),
                    )
                    # Auto-complete the step so the LLM doesn't try to
                    # do another round (which would use the cloud fallback)
                    step.status = StepStatus.COMPLETE
                    step.result = display_md
                    step.completed_at = datetime.now(timezone.utc)
                    # Mark remaining steps as done
                    for remaining in task.plan.steps:
                        if remaining.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS) and remaining.id != step.id:
                            remaining.status = StepStatus.SKIPPED
                            remaining.result = "Skipped — media delivered"
                            remaining.completed_at = datetime.now(timezone.utc)
                    await self._persistence.update_task(task)
                    return
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass  # Non-JSON result, continue normally

        if tool_name == "task_complete":
            if self._verifier:
                summary = tool_args.get("summary", result.output)
                receipts = self._collect_action_receipts(task)
                
                yield TaskEvent(
                    type=TaskEventType.VERIFIER_STARTED,
                    task_id=task.id,
                    step_id=step.id,
                    content="Verifying task complete claims against accumulated evidence...",
                    progress=self._progress_callback(task),
                )
                
                verification = await self._verifier.verify_detailed(
                    task.goal,
                    summary,
                    self._evidence_chain,
                    action_receipts=receipts,
                )
                passed = bool(verification.get("passed"))
                critique = str(verification.get("critique", ""))
                verifier_evidence_ids = await self._persist_verifier_claim_evidence(
                    task=task,
                    step_id=step.id,
                    claims=list(verification.get("claims") or []),
                )
                
                if not passed:
                    error_msg = f"Verification Failed. You must fix these unsupported claims before completing the task:\n{critique}"

                    yield TaskEvent(
                        type=TaskEventType.VERIFIER_FAILED,
                        task_id=task.id,
                        step_id=step.id,
                        content=error_msg,
                        progress=self._progress_callback(task),
                        metadata={"verifier_evidence_ids": verifier_evidence_ids},
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
                    metadata={"verifier_evidence_ids": verifier_evidence_ids},
                )

            step.status = StepStatus.COMPLETE
            step.result = _strip_think(tool_args.get("summary", result.output))
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

