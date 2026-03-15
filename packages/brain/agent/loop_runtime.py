from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

from core.feature_mode import get_feature_mode, mode_supports_labs, mode_supports_ops
from core import runtime as runtime_module
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
    TaskStep,
    TaskStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent.core.planner import TaskPlanner
from agent.learner import TaskLearner
from agent.core.memory_graph import MemoryGraph
from agent.evidence import EvidenceChain, DecisionType
from agent.observability import MetricsCollector
from agent.model_router import ModelRouter, classify_step
from agent.council import CouncilSession, CouncilRole
from agent.core.executor import TaskExecutor
from agent.step_scheduler import StepScheduler
from agent.simulation import OutcomeSimulator
from agent.state_machine import TaskStateMachine

logger = logging.getLogger("brain.agent.loop")

class AgentLoopRuntimeMixin:
    async def _run_langgraph(self, task: AgentTask) -> AsyncIterator[TaskEvent]:
        """Execute via the LangGraph state graph engine."""
        from agent.runtime.engine import LangGraphEngine

        engine = LangGraphEngine(
            provider=self._provider,
            tool_registry=self._tools,
            guardrails=self._guardrails,
            persistence=self._persistence,
            model=self._model,
            api_key=self._api_key,
            learner=self._learner,
            checkpoint_manager=self._checkpoints,
            memory_graph=self._memory_graph,
            evidence_chain=self._evidence_chain,
            event_callback=self._event_callback,
            reflection_engine=self._reflection_engine,
            model_router=self._model_router,
            provider_resolver=self._provider_resolver,
            approval_memory=self._approval_memory,
            simulator=self._simulator,
            persona_learner=self._persona_learner,
            verifier=getattr(self._executor, '_verifier', None),
            kernel_policy_service=getattr(runtime_module, "kernel_policy_service", None),
            subsystem_bootstrapper=getattr(runtime_module, "subsystem_bootstrapper", None),
        )
        async for event in engine.run(task):
            yield event

    async def _run_legacy(self, task: AgentTask) -> AsyncIterator[TaskEvent]:
        """
        Legacy execution path — the original ReAct loop.

        Preserved as fallback when USE_LANGGRAPH=false.
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
                    # Extract meaningful terms — skip stop words, keep technical nouns
                    _STOP_WORDS = frozenset({
                        "the", "this", "that", "then", "than", "with", "from", "have",
                        "will", "your", "what", "when", "where", "how", "should", "would",
                        "could", "into", "need", "make", "also", "some", "more", "just",
                        "about", "been", "they", "them", "their", "does", "done", "task",
                        "using", "which", "these", "those", "here", "there", "after",
                        "before", "please", "like", "want", "help", "create", "build",
                    })
                    goal_terms = [
                        w.lower().strip(".,!?:;")
                        for w in task.goal.split()
                        if len(w) > 3 and w.lower() not in _STOP_WORDS
                    ][:8]  # up from 5 — more terms = better graph recall
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

            # ── Phase 0b: Load user persona for prompt injection ──
            if self._persona_learner and task.user_id:
                try:
                    persona_prefs = await self._persona_learner.load_persona(task.user_id)
                    persona_context = self._persona_learner.format_for_prompt(persona_prefs)
                    if persona_context:
                        self._executor._persona_context = persona_context
                        if self._event_callback:
                            await self._event_callback("persona_loaded", {
                                "user_id": task.user_id,
                                "preview": persona_context[:200],
                            })
                except Exception as e:
                    logger.warning(f"Persona loading failed: {e}")

            # ── Phase 1: Planning ────────────────────────────────
            if task.status == TaskStatus.PLANNING:
                task.status = TaskStatus.PLANNING
                await self._persistence.update_task(task)

                # Fast-path: chat-originated tasks skip the expensive planner
                # LLM call.  A single-step plan is sufficient because the
                # executor already handles tool selection and calling.
                # The planner's multi-step decomposition only adds value for
                # complex automated agent tasks, not interactive chat.
                if task.messages:
                    logger.info(
                        f"Chat fast-path: skipping planner for '{task.goal[:60]}'"
                    )
                    task.plan = TaskPlan(
                        goal=task.goal,
                        steps=[TaskStep(
                            index=0,
                            description=task.goal[:200],
                            status=StepStatus.PENDING,
                        )],
                        reasoning="Chat-originated task — direct execution",
                    )
                else:
                    context = self._build_context(task)
                    if lesson_context:
                        context += f"\n\n{lesson_context}"
                    if memory_context:
                        # Deduplicate: skip memory lines whose first 60 chars already
                        # appear in the lesson context to avoid repeating known facts.
                        if lesson_context:
                            lesson_fingerprints = {
                                line.strip().lower()[:60]
                                for line in lesson_context.splitlines()
                                if line.strip()
                            }
                            deduped_mem = "\n".join(
                                line for line in memory_context.splitlines()
                                if line.strip().lower()[:60] not in lesson_fingerprints
                            )
                            if deduped_mem.strip():
                                context += f"\n\n{deduped_mem}"
                        else:
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

                # ── Reflection: Red-team the plan before execution ──
                if _feature_mode_allows_reflection() and self._reflection_engine and len(task.plan.steps) > 2:
                    try:
                        plan_text = json.dumps(task.plan.to_dict())
                        reflection = await self._reflection_engine.reflect(
                            plan=plan_text,
                            task_goal=task.goal,
                        )
                        logger.info(
                            f"Reflection: confidence={reflection.confidence_score:.2f} "
                            f"risk={reflection.estimated_risk_level} "
                            f"proceed={reflection.should_proceed} "
                            f"critiques={len(reflection.critique_points)}"
                        )
                        if self._evidence_chain:
                            self._evidence_chain.record_plan_decision(
                                plan_summary=f"Reflection: {reflection.estimated_risk_level} risk, confidence={reflection.confidence_score:.2f}",
                                reasoning=reflection.confidence_justification[:200],
                                confidence=reflection.confidence_score,
                            )
                        if not reflection.should_proceed:
                            yield TaskEvent(
                                type=TaskEventType.THINKING,
                                task_id=task.id,
                                content=f"⚠ Reflection flagged critical issues (confidence={reflection.confidence_score:.2f}). Proceeding with caution.\n" +
                                        "\n".join(f"- [{c.severity}] {c.description}" for c in reflection.critique_points[:3]),
                                progress=self._progress(task),
                            )
                    except Exception as e:
                        logger.warning(f"Reflection engine failed: {e}")

                # ── Simulation Gate: Pre-flight outcome simulation ─────
                if _feature_mode_allows_simulation() and self._simulator and len(task.plan.steps) > 1:
                    try:
                        sim_result = await self._simulator.simulate(
                            plan=task.plan,
                            tool_names=[t.name for t in self._tools.list_tools()],
                        )
                        if self._evidence_chain:
                            self._evidence_chain.record_plan_decision(
                                plan_summary=f"Simulation: {sim_result.recommendation} (risk={sim_result.overall_risk})",
                                reasoning=sim_result.summary(),
                                confidence=0.8 if sim_result.should_proceed else 0.3,
                            )
                        if not sim_result.should_proceed:
                            yield TaskEvent(
                                type=TaskEventType.SIMULATION_COMPLETE,
                                task_id=task.id,
                                content=sim_result.summary(),
                                progress=self._progress(task),
                            )
                            yield TaskEvent(
                                type=TaskEventType.APPROVAL_NEEDED,
                                task_id=task.id,
                                content="Simulation recommends aborting this plan. Proceed anyway?",
                                progress=self._progress(task),
                            )
                            task.status = TaskStatus.WAITING_APPROVAL
                            await self._persistence.update_task(task)

                            approved = await self._executor._wait_for_approval(task)
                            if not approved:
                                task.status = TaskStatus.FAILED
                                task.error = "User aborted after simulation warning."
                                await self._persistence.update_task(task)
                                return

                            task.status = TaskStatus.EXECUTING
                            await self._persistence.update_task(task)
                        else:
                            yield TaskEvent(
                                type=TaskEventType.SIMULATION_COMPLETE,
                                task_id=task.id,
                                content=sim_result.summary(),
                                progress=self._progress(task),
                            )
                    except Exception as e:
                        logger.warning(f"Simulation gate failed: {e}")

                # ── Council Deliberation: Multi-Agent Consensus ─────
                # Only invoke council for complex plans (complexity > 5.0)
                # to avoid wasting 5+ LLM calls on simple tasks
                plan_complexity = float(len(task.plan.steps))  # rough default
                try:
                    from agent.model_router import estimate_complexity, classify_step
                    _st = classify_step(task.goal)
                    plan_complexity = estimate_complexity(task.goal, _st)
                except Exception:
                    pass

                # Council thresholds:
                #   < 7.0  — skip entirely (routine tasks, no council needed)
                #   7.0–8.5 + no HIGH risk — skip (proactive safe-plan bypass)
                #   7.0–9.0 — deliberate_lite(): 3 most relevant members, no debate (~40% cheaper)
                #   > 9.0  — full deliberate(): all 5 members + optional debate round
                if (
                    _feature_mode_allows_council()
                    and
                    hasattr(self, "_council") and self._council
                    and plan_complexity > 7.0
                    and not self._should_skip_council(task, plan_complexity)
                ):
                    try:
                        plan_text = json.dumps(task.plan.to_dict())
                        if plan_complexity <= 9.0:
                            # Moderate complexity — mini council (3 members, no debate)
                            verdict = await self._council.deliberate_lite(
                                proposal=plan_text,
                                context=task.goal,
                                top_n=3,
                            )
                        else:
                            # High complexity — full council
                            verdict = await self._council.deliberate(
                                proposal=plan_text,
                                context=task.goal,
                                include_debate=_council_debate_enabled(),
                            )
                        
                        if verdict.requires_user_review:
                            from agent.council import VoteType as _VT
                            _is_hard_reject = (verdict.consensus == _VT.REJECT)

                            if _is_hard_reject:
                                # Council explicitly rejected — block on user approval
                                yield TaskEvent(
                                    type=TaskEventType.THINKING,
                                    task_id=task.id,
                                    content=f"⚖️ Council Rejected Plan: {verdict.review_reason}\n\n" +
                                            "\n".join(f"- {c}" for c in verdict.synthesized_concerns),
                                    progress=self._progress(task),
                                )
                                yield TaskEvent(
                                    type=TaskEventType.APPROVAL_NEEDED,
                                    task_id=task.id,
                                    content="The Council rejected this plan. Proceed anyway?",
                                    progress=self._progress(task),
                                )
                                task.status = TaskStatus.WAITING_APPROVAL
                                await self._persistence.update_task(task)

                                approved = await self._executor._wait_for_approval(task)
                                if not approved:
                                    task.status = TaskStatus.FAILED
                                    task.error = "User denied plan after Council review."
                                    await self._persistence.update_task(task)
                                    return

                                task.status = TaskStatus.EXECUTING
                                await self._persistence.update_task(task)
                            else:
                                # Council has concerns but didn't reject — proceed with warning
                                yield TaskEvent(
                                    type=TaskEventType.THINKING,
                                    task_id=task.id,
                                    content=f"⚖️ Council noted concerns (proceeding): {verdict.review_reason}\n" +
                                            "\n".join(f"- {c}" for c in verdict.synthesized_concerns[:3]),
                                    progress=self._progress(task),
                                )

                    except Exception as e:
                        logger.warning(f"Council deliberation failed: {e}")

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

            await self._transition(task, TaskStatus.EXECUTING)

            # Use the parallel step scheduler for DAG-aware execution
            async for event in self._step_scheduler.execute_plan(
                task=task,
                start_time=start_time,
                should_replan_fn=self._should_replan,
                progress_fn=self._progress,
            ):
                yield event
                if event.type == TaskEventType.TASK_FAILED:
                    return

            # ── Phase 3: Verification + Completion ────────────────
            # Transition through REFLECTING state before marking COMPLETE
            await self._transition(task, TaskStatus.REFLECTING)
            # (Verification itself is handled by the executor's task_complete handler)
            await self._transition(task, TaskStatus.COMPLETE)
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
                # Token usage (enriched with metrics collector data)
                metrics_data = self._metrics.metrics.to_dict()
                await self._event_callback("token_usage", {
                    "total_tokens": task.token_usage,
                    "iterations": task.iterations,
                    "tool_calls": task.tool_calls_count,
                    "estimated_cost_usd": metrics_data.get("estimated_cost_usd", 0),
                    "llm_calls": metrics_data.get("llm_calls", 0),
                    "avg_tool_time_ms": metrics_data.get("avg_tool_time_ms", 0),
                    "total_elapsed_ms": metrics_data.get("total_elapsed_ms", 0),
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

            # ── Phase 4: Update memory graph with task entities ───
            if self._memory_graph and task.result:
                try:
                    from agent.core.memory_graph import extract_entities_llm
                    _entities, _relations = await extract_entities_llm(
                        provider=self._provider,
                        model=self._model,
                        api_key=self._api_key,
                        user_message=task.goal,
                        assistant_response=task.result,
                    )
                    if _entities:
                        await self._memory_graph.extract_and_store(
                            conversation_id=task.id,
                            workspace_id=task.workspace_id,
                            entities=_entities,
                            relations=_relations,
                        )
                        logger.info(f"Memory graph: stored {len(_entities)} entities, {len(_relations)} relations from task {task.id}")
                except Exception as e:
                    logger.warning(f"Memory graph update failed: {e}")

            # ── Phase 4b: Observe persona signals ──────────────────
            if self._persona_learner and task.result:
                try:
                    await self._persona_learner.observe_communication(
                        user_id=task.user_id,
                        user_message=task.goal,
                        agent_response=task.result[:500],
                    )
                    await self._persona_learner.observe_session_timing(task.user_id)
                except Exception as e:
                    logger.warning(f"Persona observation failed: {e}")

            # ── Phase 5: Learn from this task ────────────────────
            if self._learner:
                # Skip extraction for trivial tasks — a 1-step, error-free,
                # low-tool-call run has nothing novel worth a 1024-token LLM call.
                _is_trivial = (
                    task.iterations <= 2
                    and task.tool_calls_count < 5
                    and task.status == TaskStatus.COMPLETE
                    and (not task.plan or len(task.plan.steps) <= 1)
                )
                if _is_trivial:
                    logger.debug(
                        f"Skipping lesson extraction for trivial task {task.id} "
                        f"(iterations={task.iterations}, tools={task.tool_calls_count})"
                    )
                else:
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
            import traceback
            tb = traceback.format_exc()
            logger.error(f"Agent loop error: {e}", exc_info=True)
            
            task.status = TaskStatus.FAILED
            task.error = str(e)
            try:
                await self._persistence.update_task(task)
            except Exception as persist_error:
                logger.error(f"Failed to persist task FAILED state: {persist_error}")
                
            yield TaskEvent(
                type=TaskEventType.TASK_FAILED,
                task_id=task.id,
                content=f"Fatal Error: {e}\n\n```python\n{tb}\n```",
                progress=self._progress(task),
            )

            # ── Auto-Recovery ────────────────────────────────────────────────
            # If this is not already a recovery task, spawn a coding specialist
            # to investigate and fix the crash.
            if "[Auto-Recovery]" not in task.goal and hasattr(self._tools, "_coordinator") and self._tools._coordinator:
                logger.info(f"Initiating auto-recovery for task {task.id} crash...")
                recovery_goal = (
                    f"[Auto-Recovery] The agent loop crashed with an unhandled exception.\n"
                    f"Please investigate the codebase to find and fix the root cause.\n"
                    f"Do not ask for human approval unless strictly necessary to test the fix.\n\n"
                    f"Exception: {e}\n\n"
                    f"Traceback:\n```python\n{tb}\n```"
                )
                asyncio.create_task(
                    self._tools._coordinator.delegate(
                        parent_task=task,
                        goal=recovery_goal,
                        specialist_type="coder",
                        max_tokens_override=32000,
                    )
                )

    async def _transition(self, task: AgentTask, new_status: TaskStatus) -> None:
        """Safely transition task status through the state machine."""
        self._state_machine.check_transition(task.id, task.status, new_status)
        task.status = new_status
        await self._persistence.update_task(task)

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
            
        workspace_file = os.path.expanduser("~/.kestrel/WORKSPACE.md")
        if os.path.exists(workspace_file):
            try:
                with open(workspace_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    parts.append("\n=== System Workspace Context ===")
                    parts.append(content)
                    parts.append("================================\n")
            except Exception as e:
                logger.warning(f"Failed to read WORKSPACE.md: {e}")
                
        return "\n".join(parts)

