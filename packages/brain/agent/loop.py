"""
Agent Loop Engine — ReAct (Reason + Act) state machine for autonomous execution.

This is the heart of Kestrel's autonomous agent. It orchestrates:
  1. Planning — decompose the goal into steps
  2. Executing — run tools and observe results
  3. Reflecting — decide next action based on observations
  4. Completing — summarize results and report

The loop is fully resumable: all state is persisted to PostgreSQL,
so tasks survive service restarts and can be paused/resumed.
"""

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
    GuardrailConfig,
    RiskLevel,
    StepStatus,
    TaskEvent,
    TaskEventType,
    TaskPlan,
    TaskStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent.planner import TaskPlanner
from agent.learner import TaskLearner
from agent.memory_graph import MemoryGraph
from agent.evidence import EvidenceChain, DecisionType

logger = logging.getLogger("brain.agent.loop")


# ── System Prompt for the Reasoning LLM ──────────────────────────────

AGENT_SYSTEM_PROMPT = """\
You are Kestrel, an autonomous AI agent. You are executing a multi-step task.

Current goal: {goal}
Current step: {step_description}

Instructions:
1. Analyze the current situation and decide which tool to call next.
2. Call exactly ONE tool per turn. Wait for its result before proceeding.
3. If the step is complete, call `task_complete` with a summary of what you accomplished.
4. If you need clarification from the user, call `ask_human` with your question.
5. If you encounter an error, try an alternative approach before giving up.
6. Think step-by-step. Explain your reasoning before acting.

Progress: Step {step_index}/{total_steps} | Iteration {iteration}/{max_iterations}

Previous observations for this step:
{observations}
"""


class AgentLoop:
    """
    ReAct agent loop — plans, executes tools, observes results, and reflects.

    Usage:
        loop = AgentLoop(provider, tool_registry, guardrails, persistence)
        async for event in loop.run(task):
            # Stream events to the client in real-time
            print(event)
    """

    def __init__(
        self,
        provider,
        tool_registry: "ToolRegistry",
        guardrails: "Guardrails",
        persistence: "TaskPersistence",
        model: str = "",
        learner: Optional[TaskLearner] = None,
        checkpoint_manager=None,
        memory_graph: Optional[MemoryGraph] = None,
        evidence_chain: Optional[EvidenceChain] = None,
        api_key: str = "",
        event_callback=None,
    ):
        self._provider = provider
        self._tools = tool_registry
        self._guardrails = guardrails
        self._persistence = persistence
        self._model = model
        self._api_key = api_key
        self._planner = TaskPlanner(provider, model)
        self._learner = learner
        self._checkpoints = checkpoint_manager
        self._memory_graph = memory_graph
        self._evidence_chain = evidence_chain
        self._event_callback = event_callback

        # Callback for approval resolution (set by the gRPC handler)
        self._approval_callback: Optional[Callable] = None

    async def run(self, task: AgentTask) -> AsyncIterator[TaskEvent]:
        """
        Execute an agent task, yielding events as they occur.

        This is the main entry point. It runs the full planning → execution
        → reflection loop until the task is complete, fails, or is paused.
        """
        start_time = time.monotonic()

        # ── Wire multi-agent coordinator for this task ────────
        try:
            from agent.coordinator import Coordinator
            coordinator = Coordinator(
                agent_loop=self,
                persistence=self._persistence,
                tool_registry=self._tools,
                event_callback=self._event_callback,
            )
            self._tools._coordinator = coordinator
            self._tools._current_task = task
        except Exception as e:
            logger.warning(f"Coordinator init skipped: {e}")
        try:
            # ── Phase 0: Enrich with Past Lessons + Memory Graph ─
            lesson_context = ""
            if self._learner:
                try:
                    lesson_context = await self._learner.enrich_context(
                        workspace_id=task.workspace_id,
                        goal=task.goal,
                    )
                    if lesson_context and self._event_callback:
                        lesson_count = lesson_context.count('\n') + 1
                        await self._event_callback("lessons_loaded", {
                            "count": lesson_count,
                            "preview": lesson_context[:150],
                        })
                except Exception as e:
                    logger.warning(f"Lesson enrichment failed: {e}")

            # Query the memory graph for relevant context
            memory_context = ""
            if self._memory_graph:
                try:
                    # Extract key terms from the goal for graph querying
                    goal_terms = [w for w in task.goal.split() if len(w) > 3][:5]
                    memory_context = await self._memory_graph.format_for_prompt(
                        workspace_id=task.workspace_id,
                        query_entities=goal_terms,
                    )
                    if memory_context and self._event_callback:
                        mem_lines = [l for l in memory_context.split('\n') if l.strip()]
                        await self._event_callback("memory_recalled", {
                            "count": len(mem_lines),
                            "entities": goal_terms,
                            "preview": memory_context[:200],
                        })
                except Exception as e:
                    logger.warning(f"Memory graph query failed: {e}")

            # ── Phase 1: Planning ────────────────────────────────
            if task.status == TaskStatus.PLANNING:
                task.status = TaskStatus.PLANNING
                await self._persistence.update_task(task)

                context = self._build_context(task)
                if lesson_context:
                    context += f"\n\n{lesson_context}"
                if memory_context:
                    context += f"\n\n{memory_context}"

                try:
                    plan = await self._planner.create_plan(
                        goal=task.goal,
                        available_tools=self._tools.list_tools(),
                        context=context,
                    )
                    task.plan = plan
                except Exception as e:
                    logger.warning(f"Planning failed, using single-step fallback: {e}")
                    task.plan = TaskPlan(
                        goal=task.goal,
                        steps=[TaskStep(
                            index=0,
                            description=f"Execute the goal directly: {task.goal[:200]}",
                            status=StepStatus.PENDING,
                        )],
                        reasoning=f"Planning failed ({e}) — executing as single step",
                    )

                await self._persistence.update_task(task)

                # Emit plan for UI process bar
                if self._event_callback:
                    await self._event_callback("plan_created", {
                        "step_count": len(task.plan.steps),
                        "steps": [
                            {"index": s.index, "description": s.description[:100]}
                            for s in task.plan.steps[:6]
                        ],
                    })

                # Record plan decision in evidence chain
                if self._evidence_chain:
                    self._evidence_chain.record_plan_decision(
                        plan_summary=f"Created {len(task.plan.steps)}-step plan for: {task.goal[:100]}",
                        reasoning=f"Decomposed goal into {len(task.plan.steps)} steps based on available tools",
                        confidence=0.7,
                    )

                yield TaskEvent(
                    type=TaskEventType.PLAN_CREATED,
                    task_id=task.id,
                    content=json.dumps(task.plan.to_dict()),
                    progress=self._progress(task),
                )

            # ── Phase 2: Execution Loop ──────────────────────────
            # Guard: if plan is still None (shouldn't happen, but be safe), create fallback
            if task.plan is None:
                task.plan = TaskPlan(
                    goal=task.goal,
                    steps=[TaskStep(
                        index=0,
                        description=f"Respond to: {task.goal[:200]}",
                        status=StepStatus.PENDING,
                    )],
                )

            task.status = TaskStatus.EXECUTING
            await self._persistence.update_task(task)

            while not task.plan.is_complete:
                task.iterations += 1

                # Budget checks
                budget_error = self._guardrails.check_budget(task)
                if budget_error:
                    task.status = TaskStatus.FAILED
                    task.error = budget_error
                    await self._persistence.update_task(task)
                    yield TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        content=budget_error,
                        progress=self._progress(task),
                    )
                    return

                # Wall-clock time check
                elapsed = time.monotonic() - start_time
                if elapsed > task.config.max_wall_time_seconds:
                    task.status = TaskStatus.FAILED
                    task.error = f"Wall-clock time limit exceeded ({int(elapsed)}s)"
                    await self._persistence.update_task(task)
                    yield TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        content=task.error,
                        progress=self._progress(task),
                    )
                    return

                # Get next step
                step = task.plan.current_step
                if not step:
                    # No more executable steps (deps not met or all done)
                    break

                if step.status == StepStatus.PENDING:
                    step.status = StepStatus.IN_PROGRESS
                    step.started_at = datetime.now(timezone.utc)
                    await self._persistence.update_task(task)

                    yield TaskEvent(
                        type=TaskEventType.STEP_STARTED,
                        task_id=task.id,
                        step_id=step.id,
                        content=step.description,
                        progress=self._progress(task),
                    )

                # ── Reason: Ask LLM what to do ───────────────────
                async for event in self._reason_and_act(task, step):
                    yield event

                    # Check if we need to pause for approval
                    if event.type == TaskEventType.APPROVAL_NEEDED:
                        task.status = TaskStatus.WAITING_APPROVAL
                        await self._persistence.update_task(task)

                        # Wait for approval (this blocks until resolved)
                        approved = await self._wait_for_approval(task)
                        if not approved:
                            # Denied — skip this step
                            step.status = StepStatus.SKIPPED
                            step.result = "Skipped — human denied approval"
                            task.status = TaskStatus.EXECUTING
                            await self._persistence.update_task(task)
                            break  # Move to next step

                        task.status = TaskStatus.EXECUTING
                        await self._persistence.update_task(task)

                    # Check if step completed
                    if step.status == StepStatus.COMPLETE:
                        yield TaskEvent(
                            type=TaskEventType.STEP_COMPLETE,
                            task_id=task.id,
                            step_id=step.id,
                            content=step.result or "",
                            progress=self._progress(task),
                        )
                        break

                    if step.status == StepStatus.FAILED:
                        # Try retry (max 3 attempts per step)
                        if step.attempts < 3:
                            step.status = StepStatus.IN_PROGRESS
                            step.attempts += 1
                            logger.info(
                                f"Retrying step {step.id} (attempt {step.attempts})"
                            )
                        else:
                            yield TaskEvent(
                                type=TaskEventType.TASK_FAILED,
                                task_id=task.id,
                                step_id=step.id,
                                content=step.error or "Step failed after 3 attempts",
                                progress=self._progress(task),
                            )
                            task.status = TaskStatus.FAILED
                            task.error = f"Step '{step.description}' failed: {step.error}"
                            await self._persistence.update_task(task)
                            return

                # ── Reflect: Should we replan? ───────────────────
                # Skip reflection if budget is already exhausted
                budget_ok = self._guardrails.check_budget(task) is None
                if (
                    budget_ok
                    and step
                    and step.status == StepStatus.COMPLETE
                    and task.iterations % 5 == 0
                    and task.plan.revision_count < 3
                ):
                    task.status = TaskStatus.REFLECTING
                    await self._persistence.update_task(task)

                    revised = await self._planner.revise_plan(
                        plan=task.plan,
                        observations=step.result or "",
                        available_tools=self._tools.list_tools(),
                    )
                    task.plan = revised
                    task.status = TaskStatus.EXECUTING
                    await self._persistence.update_task(task)

            # ── Phase 3: Completion ──────────────────────────────
            task.status = TaskStatus.COMPLETE
            task.completed_at = datetime.now(timezone.utc)

            # Build final summary from step results
            results = []
            for s in task.plan.steps:
                if s.result:
                    # In chat mode, use raw result without step description prefix
                    if task.messages:
                        results.append(s.result)
                    else:
                        results.append(f"**{s.description}**: {s.result}")
            task.result = "\n".join(results) if results else "Task completed successfully."

            await self._persistence.update_task(task)

            # Emit final process bar events
            if self._event_callback:
                # Token usage
                await self._event_callback("token_usage", {
                    "total_tokens": task.token_usage,
                    "iterations": task.iterations,
                    "tool_calls": task.tool_calls_count,
                })
                # Evidence summary
                if self._evidence_chain and self._evidence_chain._decisions:
                    await self._event_callback("evidence_summary", {
                        "decision_count": len(self._evidence_chain._decisions),
                        "decisions": [
                            {"type": d.decision_type.value, "description": d.description[:80]}
                            for d in self._evidence_chain._decisions[:5]
                        ],
                    })

            yield TaskEvent(
                type=TaskEventType.TASK_COMPLETE,
                task_id=task.id,
                content=task.result,
                progress=self._progress(task),
            )

            # ── Phase 3b: Persist evidence chain ─────────────────
            if self._evidence_chain:
                try:
                    await self._evidence_chain.persist()
                except Exception as e:
                    logger.warning(f"Evidence chain persistence failed: {e}")

            # ── Phase 4: Learn from this task ────────────────────
            if self._learner:
                try:
                    await self._learner.extract_lessons(task)
                except Exception as e:
                    logger.warning(f"Post-task learning failed: {e}")

        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            await self._persistence.update_task(task)
            yield TaskEvent(
                type=TaskEventType.TASK_PAUSED,
                task_id=task.id,
                content="Task cancelled",
                progress=self._progress(task),
            )

        except Exception as e:
            logger.error(f"Agent loop error: {e}", exc_info=True)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            await self._persistence.update_task(task)
            yield TaskEvent(
                type=TaskEventType.TASK_FAILED,
                task_id=task.id,
                content=str(e),
                progress=self._progress(task),
            )

    async def _reason_and_act(
        self,
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        """
        One iteration of the ReAct loop:
        1. Build prompt with current observations
        2. Call LLM with available tools
        3. If LLM returns a tool call → execute it
        4. If LLM returns text → treat as thinking/reflection
        """
        # Build the agent prompt
        observations = "\n".join(
            f"[{tc.get('tool', '?')}] → {tc.get('result', '?')}"
            for tc in step.tool_calls
        ) or "(none yet)"

        done, total = task.plan.progress

        # If the task has pre-built messages (from chat), use those
        # Otherwise, build agent-specific messages
        if task.messages:
            messages = list(task.messages)  # Copy to avoid mutation
            # Add tool call history from this step if any
            for tc in step.tool_calls[-6:]:
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
                        # Preserve Gemini raw part (includes thought_signature)
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
            )

            # Build messages: system + conversation history for this step
            messages = [{"role": "system", "content": system_prompt}]

            # Add step-specific messages
            if not step.tool_calls:
                messages.append({
                    "role": "user",
                    "content": f"Execute this step: {step.description}",
                })
            else:
                # Replay tool call history for context
                messages.append({
                    "role": "user",
                    "content": f"Continue executing: {step.description}",
                })
                for tc in step.tool_calls[-6:]:  # Last 6 tool calls for context
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
                            # Preserve Gemini raw part (includes thought_signature)
                            **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                        }],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", "call_1"),
                        "content": tc.get("result", ""),
                    })

        # Get available tools as OpenAI function schemas
        tool_schemas = [t.to_openai_schema() for t in self._tools.list_tools()]

        # Call LLM with function calling
        response = await self._provider.generate_with_tools(
            messages=messages,
            model=self._model,
            tools=tool_schemas,
            temperature=0.2,
            max_tokens=4096,
            api_key=self._api_key,
        )

        # ── Handle LLM response ─────────────────────────────────
        if response.get("tool_calls"):
            for tc_data in response["tool_calls"]:
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

                # Check guardrails
                approval_needed = self._guardrails.needs_approval(
                    tool_name, tool_args, task.config,
                )

                if approval_needed:
                    request = ApprovalRequest(
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
                        progress=self._progress(task),
                    )
                    return  # Pause execution until approved

                # Emit tool_called event
                yield TaskEvent(
                    type=TaskEventType.TOOL_CALLED,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_args=json.dumps(tool_args),
                    progress=self._progress(task),
                )

                # Record tool decision in evidence chain
                if self._evidence_chain:
                    self._evidence_chain.record_tool_decision(
                        tool_name=tool_name,
                        args=tool_args,
                        reasoning=f"LLM selected {tool_name} for step: {step.description[:80]}",
                    )

                # Execute the tool (inject workspace context)
                tool_context = {"workspace_id": task.workspace_id} if task.workspace_id else {}
                result = await self._tools.execute(tool_call, context=tool_context)
                task.tool_calls_count += 1

                # Inline budget check — stop immediately if limits exceeded
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
                        progress=self._progress(task),
                    )
                    return  # Exit _reason_and_act, outer loop will catch FAILED

                # Record in step history
                step.tool_calls.append({
                    "id": tool_call.id,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": result.output if result.success else result.error,
                    "success": result.success,
                    "time_ms": result.execution_time_ms,
                    # Preserve Gemini raw part for thought_signature support
                    **({"_gemini_raw_part": tc_data["_gemini_raw_part"]} if "_gemini_raw_part" in tc_data else {}),
                })

                yield TaskEvent(
                    type=TaskEventType.TOOL_RESULT,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_result=result.output if result.success else result.error,
                    progress=self._progress(task),
                )

                # Check for special control tools
                if tool_name == "task_complete":
                    step.status = StepStatus.COMPLETE
                    step.result = tool_args.get("summary", result.output)
                    step.completed_at = datetime.now(timezone.utc)

                elif tool_name == "ask_human":
                    # Emit an APPROVAL_NEEDED event so the frontend shows the question
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
                        progress=self._progress(task),
                    )

                    # Wait for the user to respond
                    approved = await self._wait_for_approval(task)
                    task.status = TaskStatus.RUNNING

                    if not approved:
                        step.result = "User did not respond / declined"
                        step.status = StepStatus.COMPLETE
                        step.completed_at = datetime.now(timezone.utc)

                elif not result.success:
                    step.error = result.error
                    # Don't mark failed yet — the loop will handle retries

                await self._persistence.update_task(task)

        elif response.get("content"):
            text = response["content"]

            # In chat mode (task.messages is set), a plain text response
            # IS the final answer — not intermediate thinking.
            if task.messages:
                step.status = StepStatus.COMPLETE
                step.result = text
                step.completed_at = datetime.now(timezone.utc)
                await self._persistence.update_task(task)
            else:
                # Autonomous task mode — text without a tool call is thinking
                yield TaskEvent(
                    type=TaskEventType.THINKING,
                    task_id=task.id,
                    step_id=step.id,
                    content=text,
                    progress=self._progress(task),
                )

                # If the LLM is done thinking without a tool call,
                # it might mean the step is simple enough to complete directly
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

    async def _wait_for_approval(self, task: AgentTask) -> bool:
        """
        Block until the pending approval is resolved.
        Returns True if approved, False if denied/expired.
        """
        if not task.pending_approval:
            return True

        approval = task.pending_approval
        max_wait = 1800  # 30 minutes
        poll_interval = 2  # seconds

        elapsed = 0
        while elapsed < max_wait:
            # Check approval status from persistence
            updated = await self._persistence.get_approval(approval.id)
            if updated and updated.status != ApprovalStatus.PENDING:
                task.pending_approval = None
                return updated.status == ApprovalStatus.APPROVED

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        # Expired
        task.pending_approval = None
        return False

    def _progress(self, task: AgentTask) -> dict:
        """Build progress snapshot."""
        done, total = task.plan.progress if task.plan else (0, 0)
        return {
            "current_step": done,
            "total_steps": total,
            "iterations": task.iterations,
            "tokens_used": task.token_usage,
            "tool_calls": task.tool_calls_count,
        }

    def _build_context(self, task: AgentTask) -> str:
        """Build context string for the planner."""
        parts = [f"Workspace: {task.workspace_id}"]
        if task.conversation_id:
            parts.append(f"Conversation: {task.conversation_id}")
        return "\n".join(parts)


# ── Task Persistence Interface ───────────────────────────────────────


class TaskPersistence:
    """
    Abstract interface for persisting agent task state.
    Implemented by the database layer in server.py.
    """

    async def save_task(self, task: AgentTask) -> None:
        """Save a new task to the database."""
        raise NotImplementedError

    async def update_task(self, task: AgentTask) -> None:
        """Update an existing task."""
        raise NotImplementedError

    async def get_task(self, task_id: str) -> Optional[AgentTask]:
        """Load a task by ID."""
        raise NotImplementedError

    async def save_approval(self, approval: ApprovalRequest) -> None:
        """Save an approval request."""
        raise NotImplementedError

    async def get_approval(self, approval_id: str) -> Optional[ApprovalRequest]:
        """Get an approval request by ID."""
        raise NotImplementedError

    async def resolve_approval(
        self,
        approval_id: str,
        status: ApprovalStatus,
        decided_by: str,
    ) -> None:
        """Resolve an approval request."""
        raise NotImplementedError
