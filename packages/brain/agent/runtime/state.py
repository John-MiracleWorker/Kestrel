"""
LangGraph state definitions for Kestrel's agent orchestration.

Defines the typed state that flows through the agent state graph,
mapping directly to existing Kestrel domain types (AgentTask, TaskPlan, etc.).
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from agent.types import (
    AgentTask,
    TaskPlan,
    ToolResult,
)


class KestrelState(TypedDict, total=False):
    """Core state flowing through the main agent graph.

    Each field maps to an existing Kestrel domain object or runtime
    artifact. LangGraph manages persistence/checkpointing of this state
    automatically via the configured checkpointer.
    """

    # ── Domain objects ───────────────────────────────────────────
    task: AgentTask
    plan: Optional[TaskPlan]

    # ── Execution artifacts ──────────────────────────────────────
    step_results: list[ToolResult]
    messages: list[dict[str, Any]]         # LLM conversation history
    memory_context: list[str]              # Retrieved memory snippets
    evidence_chain: list[dict[str, Any]]   # Evidence bindings
    council_verdict: Optional[dict[str, Any]]

    # ── Control flow ─────────────────────────────────────────────
    status: str                            # Maps to TaskStatus enum value
    iteration: int
    kestrel_checkpoint_id: Optional[str]

    # ── Context enrichment ───────────────────────────────────────
    lesson_context: str                    # Past lessons from TaskLearner
    persona_context: str                   # User persona preferences

    # ── Routing signals (set by nodes, read by conditional edges) ──
    route: Optional[str]                   # Next route decision
    plan_complexity: float                 # Complexity score from model_router
    needs_council: bool                    # Whether council review is needed
    approval_granted: Optional[bool]       # Result of human-in-the-loop

    # ── Supervisor routing ────────────────────────────────────────
    supervisor_route: Optional[str]        # "plan" | "research" | "content"
    simulation_warning: Optional[str]      # Summary from failed simulation gate


class ResearchState(TypedDict, total=False):
    """State for the deep research subgraph."""

    topic: str
    angles: list[str]                      # Decomposed research angles
    findings: list[dict[str, Any]]         # Per-angle research results
    analysis: str                          # Cross-referenced analysis
    report: str                            # Final synthesized report
    report_format: str                     # "markdown" | "pdf" | "slides" | "webpage"
    search_backend: str                    # Preferred search backend
    max_agents: int                        # Max parallel research agents
    parent_task_id: str                    # Link back to main task


class ContentState(TypedDict, total=False):
    """State for the AIGC content generation subgraph."""

    content_type: str                      # "slides" | "webpage" | "pdf" | "video"
    source_text: str                       # Input text/report to format
    outline: list[dict[str, Any]]          # Content outline/structure
    draft: str                             # Generated draft content
    formatted_output: str                  # Path to formatted output file
    review_feedback: str                   # LLM review notes
    output_dir: str                        # Task output directory
    parent_task_id: str


def create_initial_state(task: AgentTask) -> KestrelState:
    """Create the initial KestrelState from an AgentTask.

    This is the entry point for the LangGraph engine — it converts
    Kestrel's existing AgentTask into the typed state dict.
    """
    return KestrelState(
        task=task,
        plan=task.plan,
        step_results=[],
        messages=list(task.messages) if task.messages else [],
        memory_context=[],
        evidence_chain=[],
        council_verdict=None,
        status=task.status.value,
        iteration=0,
        kestrel_checkpoint_id=None,
        lesson_context="",
        persona_context="",
        route=None,
        plan_complexity=0.0,
        needs_council=False,
        approval_granted=None,
        supervisor_route=None,
        simulation_warning=None,
    )
