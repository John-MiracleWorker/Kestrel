"""
Agent State Graph — LangGraph-based orchestration for the Kestrel agent loop.

This replaces the sequential while-loop in loop.py with a graph-based
state machine. Each node wraps an existing Kestrel component, preserving
all business logic while gaining LangGraph's benefits:
  - Automatic checkpointing at every node transition
  - Native human-in-the-loop via interrupt()
  - Subgraph composition (research, content generation)
  - Time-travel debugging and task replay

Graph topology:
  START → initialize → supervisor ─┬─ (research) → research_subgraph → complete
                                    ├─ (content)  → content_subgraph  → complete
                                    └─ (plan)     → plan ─┬─ (simple) ──→ execute ─┬─ (reflect) → reflect ─┬─ (continue) → execute
                                                           │                        │                        ├─ (replan)   → plan
                                                           │                        ├─ (done)    → complete   └─ (done)     → complete
                                                           └─ (complex) → council → approve ─┬─ (approved) → execute
                                                                                             └─ (denied)  → complete
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Optional

from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.agent_graph")


# ── Routing functions ──────────────────────────────────────────────────────────

def _route_after_supervisor(state: KestrelState) -> str:
    """Route based on supervisor task classification."""
    route = state.get("supervisor_route", "plan")
    if route == "research":
        return "research_subgraph"
    if route == "content":
        return "content_subgraph"
    return "plan"


def _route_after_plan(state: KestrelState) -> str:
    """Decide whether to go to council or directly to execution."""
    if state.get("needs_council", False):
        return "council"
    return "execute"


def _route_after_approval(state: KestrelState) -> str:
    """Route based on human approval result."""
    if state.get("approval_granted") is False:
        return "complete"
    return "execute"


def _route_after_execute(state: KestrelState) -> str:
    """Route based on execution outcome."""
    route = state.get("route", "needs_reflection")
    if route == "done":
        return "complete"
    if route == "needs_approval":
        return "approve"
    return "reflect"


def _route_after_reflect(state: KestrelState) -> str:
    """Route based on reflection outcome."""
    route = state.get("route", "done")
    if route == "continue":
        return "execute"
    if route == "replan":
        return "plan"
    return "complete"


# ── Supervisor node wrapper ────────────────────────────────────────────────────

async def _supervisor_node(
    state: KestrelState,
    *,
    supervisor=None,
    use_supervisor_routing: bool = False,
) -> dict[str, Any]:
    """Classify the task and set supervisor_route for conditional routing.

    When use_supervisor_routing is False (default), always routes to 'plan'
    so the standard agent path runs.  Set USE_SUPERVISOR_ROUTING=true to
    activate research/content subgraph routing.
    """
    import os
    active = use_supervisor_routing or (
        os.getenv("USE_SUPERVISOR_ROUTING", "false").lower() == "true"
    )

    if not active or supervisor is None:
        return {"supervisor_route": "plan"}

    try:
        task = state["task"]
        routing = await supervisor.route(task)
        raw_route = routing.get("route", "execute")
        # Map supervisor route values to graph node names
        if raw_route == "research_graph":
            supervisor_route = "research"
        elif raw_route == "content_graph":
            supervisor_route = "content"
        else:
            supervisor_route = "plan"

        logger.info(f"Supervisor routed task '{task.goal[:60]}' → {supervisor_route}")
        return {
            "supervisor_route": supervisor_route,
            "plan_complexity": routing.get("plan_complexity", 0.0),
        }
    except Exception as e:
        logger.warning(f"Supervisor routing failed, defaulting to plan: {e}")
        return {"supervisor_route": "plan"}


# ── Research subgraph wrapper ──────────────────────────────────────────────────

async def _research_subgraph_node(
    state: KestrelState,
    *,
    research_graph=None,
) -> dict[str, Any]:
    """Invoke the research subgraph for deep-research tasks.

    Translates the KestrelState task into ResearchState, runs the subgraph,
    and stores the report back onto the task as task.result.
    """
    task = state["task"]
    from agent.types import TaskStatus
    from datetime import datetime, timezone

    if research_graph is None:
        logger.warning("research_graph not wired — falling back to direct execute route")
        return {"supervisor_route": "plan"}

    from agent.runtime.state import ResearchState
    research_state = ResearchState(
        topic=task.goal,
        max_agents=5,
        search_backend="tavily",
        parent_task_id=task.id,
    )

    try:
        config = {"configurable": {"thread_id": f"{task.id}-research"}}
        final_state = await research_graph.ainvoke(research_state, config=config)
        task.result = final_state.get("report", "Research completed.")
        task.status = TaskStatus.COMPLETE
        task.completed_at = datetime.now(timezone.utc)
    except Exception as e:
        logger.error(f"Research subgraph failed: {e}", exc_info=True)
        task.status = TaskStatus.FAILED
        task.error = str(e)

    return {"status": task.status.value}


# ── Content subgraph wrapper ───────────────────────────────────────────────────

async def _content_subgraph_node(
    state: KestrelState,
    *,
    content_graph=None,
) -> dict[str, Any]:
    """Invoke the content subgraph for AIGC tasks.

    Translates the KestrelState task into ContentState, runs the subgraph,
    and stores the formatted output path back onto the task as task.result.
    """
    task = state["task"]
    from agent.types import TaskStatus
    from datetime import datetime, timezone
    import os

    if content_graph is None:
        logger.warning("content_graph not wired — falling back to direct execute route")
        return {"supervisor_route": "plan"}

    # Infer content type from goal keywords
    goal_lower = task.goal.lower()
    if any(kw in goal_lower for kw in ("slide", "presentation", "powerpoint")):
        content_type = "slides"
    elif any(kw in goal_lower for kw in ("webpage", "web page", "html", "website")):
        content_type = "webpage"
    elif any(kw in goal_lower for kw in ("pdf", "report", "document")):
        content_type = "pdf"
    else:
        content_type = "slides"

    from agent.runtime.state import ContentState
    content_state = ContentState(
        content_type=content_type,
        source_text=task.goal,
        output_dir=os.path.expanduser("~/.kestrel/outputs"),
        parent_task_id=task.id,
    )

    try:
        config = {"configurable": {"thread_id": f"{task.id}-content"}}
        final_state = await content_graph.ainvoke(content_state, config=config)
        output_path = final_state.get("formatted_output", "")
        task.result = f"Content generated: {output_path}\n{final_state.get('review_feedback', '')}"
        task.status = TaskStatus.COMPLETE
        task.completed_at = datetime.now(timezone.utc)
    except Exception as e:
        logger.error(f"Content subgraph failed: {e}", exc_info=True)
        task.status = TaskStatus.FAILED
        task.error = str(e)

    return {"status": task.status.value}


# ── Graph builder ──────────────────────────────────────────────────────────────

def build_agent_graph(
    *,
    # Existing Kestrel components injected as dependencies
    provider=None,
    tool_registry=None,
    guardrails=None,
    persistence=None,
    model: str = "",
    api_key: str = "",
    learner=None,
    checkpoint_manager=None,
    memory_graph=None,
    evidence_chain=None,
    event_callback=None,
    reflection_engine=None,
    model_router=None,
    provider_resolver=None,
    approval_memory=None,
    simulator=None,
    persona_learner=None,
    metrics=None,
    council=None,
    executor=None,
    step_scheduler=None,
    planner=None,
    checkpointer=None,
    # Supervisor and subgraphs (optional — activated via USE_SUPERVISOR_ROUTING)
    supervisor=None,
    research_graph=None,
    content_graph=None,
    kernel_policy_service=None,
    subsystem_bootstrapper=None,
):
    """Build the complete agent state graph.

    All Kestrel components are injected via keyword arguments and bound
    to node functions using functools.partial. This preserves the
    existing component interfaces while adding LangGraph orchestration.

    Supervisor routing (research/content subgraphs) is enabled when
    USE_SUPERVISOR_ROUTING=true is set in the environment, or when
    explicit supervisor/research_graph/content_graph instances are provided.
    """
    from langgraph.graph import END, START, StateGraph

    from agent.runtime.nodes.initialize import initialize_node
    from agent.runtime.nodes.policy import policy_node
    from agent.runtime.nodes.plan import plan_node
    from agent.runtime.nodes.execute import execute_node
    from agent.runtime.nodes.reflect import reflect_node
    from agent.runtime.nodes.council import council_node
    from agent.runtime.nodes.approve import approve_node
    from agent.runtime.nodes.complete import complete_node

    graph = StateGraph(KestrelState)

    # ── Bind dependencies to nodes ───────────────────────────────
    graph.add_node("initialize", functools.partial(
        initialize_node,
        learner=learner,
        memory_graph=memory_graph,
        persona_learner=persona_learner,
        event_callback=event_callback,
    ))

    graph.add_node("policy", functools.partial(
        policy_node,
        kernel_policy_service=kernel_policy_service,
        subsystem_bootstrapper=subsystem_bootstrapper,
    ))

    graph.add_node("supervisor", functools.partial(
        _supervisor_node,
        supervisor=supervisor,
    ))

    graph.add_node("plan", functools.partial(
        plan_node,
        planner=planner,
        tool_registry=tool_registry,
        reflection_engine=reflection_engine,
        simulator=simulator,
        evidence_chain=evidence_chain,
        event_callback=event_callback,
    ))

    graph.add_node("council", functools.partial(
        council_node,
        council=council,
        tool_registry=tool_registry,
        event_callback=event_callback,
    ))

    graph.add_node("approve", functools.partial(
        approve_node,
        guardrails=guardrails,
        executor=executor,
        persistence=persistence,
        event_callback=event_callback,
    ))

    graph.add_node("execute", functools.partial(
        execute_node,
        step_scheduler=step_scheduler,
        persistence=persistence,
        event_callback=event_callback,
    ))

    graph.add_node("reflect", functools.partial(
        reflect_node,
        evidence_chain=evidence_chain,
        provider=provider,
        model=model,
        api_key=api_key,
        event_callback=event_callback,
    ))

    graph.add_node("complete", functools.partial(
        complete_node,
        persistence=persistence,
        learner=learner,
        metrics=metrics,
        evidence_chain=evidence_chain,
        memory_graph=memory_graph,
        persona_learner=persona_learner,
        provider=provider,
        model=model,
        api_key=api_key,
        event_callback=event_callback,
    ))

    # ── Subgraph nodes (research & content) ──────────────────────
    graph.add_node("research_subgraph", functools.partial(
        _research_subgraph_node,
        research_graph=research_graph,
    ))

    graph.add_node("content_subgraph", functools.partial(
        _content_subgraph_node,
        content_graph=content_graph,
    ))

    # ── Wire edges ───────────────────────────────────────────────
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "policy")
    graph.add_edge("policy", "supervisor")

    graph.add_conditional_edges("supervisor", _route_after_supervisor, {
        "plan": "plan",
        "research_subgraph": "research_subgraph",
        "content_subgraph": "content_subgraph",
    })

    graph.add_conditional_edges("plan", _route_after_plan, {
        "council": "council",
        "execute": "execute",
    })

    graph.add_edge("council", "approve")

    graph.add_conditional_edges("approve", _route_after_approval, {
        "execute": "execute",
        "complete": "complete",
    })

    graph.add_conditional_edges("execute", _route_after_execute, {
        "reflect": "reflect",
        "complete": "complete",
        "approve": "approve",
    })

    graph.add_conditional_edges("reflect", _route_after_reflect, {
        "execute": "execute",
        "plan": "plan",
        "complete": "complete",
    })

    # Subgraphs always route to complete after finishing
    graph.add_edge("research_subgraph", "complete")
    graph.add_edge("content_subgraph", "complete")

    graph.add_edge("complete", END)

    # ── Compile with optional checkpointer ───────────────────────
    compile_kwargs = {}
    if checkpointer:
        compile_kwargs["checkpointer"] = checkpointer

    return graph.compile(**compile_kwargs)
