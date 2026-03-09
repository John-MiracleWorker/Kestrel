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
  START → initialize → plan ─┬─ (simple) ──→ execute ─┬─ (reflect) → reflect ─┬─ (continue) → execute
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
):
    """Build the complete agent state graph.

    All Kestrel components are injected via keyword arguments and bound
    to node functions using functools.partial. This preserves the
    existing component interfaces while adding LangGraph orchestration.
    """
    from langgraph.graph import END, START, StateGraph

    from agent.runtime.nodes.initialize import initialize_node
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
        memory_graph=memory_graph,
        persona_learner=persona_learner,
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
        event_callback=event_callback,
    ))

    # ── Wire edges ───────────────────────────────────────────────
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "plan")

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

    graph.add_edge("complete", END)

    # ── Compile with optional checkpointer ───────────────────────
    compile_kwargs = {}
    if checkpointer:
        compile_kwargs["checkpointer"] = checkpointer

    return graph.compile(**compile_kwargs)
