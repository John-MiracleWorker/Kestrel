from __future__ import annotations

from agent.core.executor_shared import *

class TaskExecutorCoreMixin:
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

    @staticmethod
    def _tool_result_event_metadata(result: ToolResult) -> dict[str, Any]:
        metadata = dict(result.metadata or {})
        metadata.setdefault("success", result.success)
        metadata.setdefault("execution_time_ms", result.execution_time_ms)
        return metadata

    def _build_tool_context(self, task: AgentTask) -> dict[str, Any]:
        exec_ctx = getattr(task, "execution_context", None)
        if exec_ctx:
            return exec_ctx.to_tool_context()

        context: dict[str, Any] = {}
        if task.workspace_id:
            context["workspace_id"] = task.workspace_id
        if task.user_id:
            context["user_id"] = task.user_id
        return context

    @staticmethod
    def _collect_action_receipts(task: AgentTask) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        if not task.plan:
            return receipts
        for planned_step in task.plan.steps:
            for tool_call in getattr(planned_step, "tool_calls", []) or []:
                metadata = tool_call.get("metadata") or {}
                if not isinstance(metadata, dict):
                    continue
                execution = metadata.get("execution") if isinstance(metadata.get("execution"), dict) else {}
                receipt = None
                if isinstance(metadata.get("receipt"), dict):
                    receipt = metadata.get("receipt")
                elif isinstance(execution.get("receipt"), dict):
                    receipt = execution.get("receipt")
                if not isinstance(receipt, dict):
                    continue
                receipt_id = str(receipt.get("receipt_id") or "")
                if receipt_id and receipt_id in seen_ids:
                    continue
                if receipt_id:
                    seen_ids.add(receipt_id)
                receipts.append(dict(receipt))
        return receipts

    async def _persist_verifier_claim_evidence(
        self,
        *,
        task: AgentTask,
        step_id: str,
        claims: list[dict[str, Any]],
    ) -> list[str]:
        pool = getattr(self._persistence, "_pool", None)
        if pool is None or not claims:
            return []

        claim_ids: list[str] = []
        async with pool.acquire() as conn:
            for claim in claims:
                claim_id = str(uuid.uuid4())
                claim_ids.append(claim_id)
                await conn.execute(
                    """
                    INSERT INTO verifier_claim_evidence (
                        id,
                        workspace_id,
                        task_id,
                        step_id,
                        claim_text,
                        verdict,
                        confidence,
                        rationale,
                        supporting_receipt_ids,
                        artifact_refs,
                        metadata_json
                    )
                    VALUES (
                        $1::uuid,
                        NULLIF($2, '')::uuid,
                        $3::uuid,
                        $4,
                        $5,
                        $6,
                        $7,
                        $8,
                        $9::jsonb,
                        $10::jsonb,
                        $11::jsonb
                    )
                    """,
                    claim_id,
                    task.workspace_id,
                    task.id,
                    step_id,
                    str(claim.get("claim_text") or ""),
                    str(claim.get("verdict") or "unknown"),
                    float(claim.get("confidence") or 0.0),
                    str(claim.get("rationale") or ""),
                    json.dumps(claim.get("supporting_receipt_ids") or [], default=str),
                    json.dumps(claim.get("artifact_refs") or [], default=str),
                    json.dumps(claim.get("metadata") or {}, default=str),
                )
        return claim_ids

    def _resolve_tool_tier(
        self,
        task: AgentTask,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> tuple[ApprovalTier, str]:
        tier, tier_reason = self._guardrails.check_tool(
            tool_name, tool_args, task.config,
            tool_registry=self._tools,
        )

        exec_ctx = getattr(task, "execution_context", None)
        if not exec_ctx:
            return tier, tier_reason

        policy_engine = getattr(exec_ctx, "services", {}).get("policy_engine")
        if not policy_engine:
            return tier, tier_reason

        decision = policy_engine.decide(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_definition=self._tools.get_tool(tool_name),
            execution_context=exec_ctx,
        )
        if not decision.allowed:
            return ApprovalTier.BLOCK, decision.rationale
        if decision.approval_required and tier != ApprovalTier.BLOCK:
            return ApprovalTier.CONFIRM, decision.rationale
        return tier, tier_reason or decision.rationale

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
        logger.warning(
            f"Approval timed out after {timeout_s}s for task {task.id} "
            f"(tool: {task.pending_approval.tool_name if task.pending_approval else 'unknown'}). "
            f"No response received — treating as denied."
        )
        return False

    async def _handle_text_only_response(
        self,
        task: AgentTask,
        step: Any,
        text: str,
        total_steps: int,
    ) -> None:
        """Track text-only loops and complete the step when the model is clearly done."""
        self._text_only_streak.setdefault(step.id, 0)
        self._text_only_streak[step.id] += 1

        self._text_only_total.setdefault(step.id, 0)
        self._text_only_total[step.id] += 1

        is_simple_chat = bool(task.messages and total_steps == 1)
        has_done_work = bool(step.tool_calls)
        is_done_phrase = any(
            phrase in text.lower()
            for phrase in [
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
            ]
        )
        has_high_streak = self._text_only_streak[step.id] >= 2
        has_excessive_text = self._text_only_total[step.id] >= 5

        step.result = _strip_think(text)
        if is_simple_chat or has_done_work or is_done_phrase or has_high_streak or has_excessive_text:
            step.status = StepStatus.COMPLETE
            step.completed_at = datetime.now(timezone.utc)
            await self._persistence.update_task(task)

    async def run_step(self, task: AgentTask, step: Any) -> AsyncIterator[TaskEvent]:
        """Runs a single iteration of the ReAct loop for a given step."""
        async for event in self._reason_and_act(task, step):
            yield event
