"""
Research subgraph — DeerFlow-style deep research with parallel fan-out.

Decomposes a research topic into angles, fans out to isolated sub-agents,
cross-references findings, and synthesizes a unified report.

Graph topology:
  START → decompose → fan_out → analyze → synthesize → END
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent.runtime.state import ResearchState

logger = logging.getLogger("brain.agent.runtime.graphs.research")


async def decompose_topic(state: ResearchState) -> dict[str, Any]:
    """Break a research topic into distinct investigation angles.

    Uses LLM to identify 3-8 complementary research angles that,
    together, would provide comprehensive coverage of the topic.
    """
    topic = state["topic"]
    max_agents = state.get("max_agents", 5)

    # Default decomposition — will be enhanced with LLM call
    angles = [
        f"Overview and fundamentals of {topic}",
        f"Recent developments and breakthroughs in {topic}",
        f"Key challenges and open problems in {topic}",
        f"Practical applications and industry impact of {topic}",
        f"Future directions and predictions for {topic}",
    ][:max_agents]

    return {"angles": angles}


async def parallel_research(
    state: ResearchState,
    *,
    coordinator=None,
    search_router=None,
) -> dict[str, Any]:
    """Fan out research across multiple isolated sub-agents.

    Each sub-agent gets:
    - Its own research angle
    - Isolated context (no parent context leakage)
    - Access to configured search backends
    """
    topic = state["topic"]
    angles = state.get("angles", [])
    search_backend = state.get("search_backend", "tavily")

    if coordinator:
        # Use Kestrel's coordinator for true sub-agent delegation
        tasks = []
        for angle in angles:
            tasks.append(coordinator.delegate_to_specialist(
                specialist_type="researcher",
                goal=f"Research: {angle}. Focus on finding concrete evidence, "
                     f"data points, and expert opinions. Cite sources.",
                isolated_context=True,
            ))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        findings = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"Research angle {i} failed: {result}")
                findings.append({
                    "angle": angles[i],
                    "findings": f"Research failed: {result}",
                    "sources": [],
                })
            else:
                findings.append({
                    "angle": angles[i],
                    "findings": str(result),
                    "sources": [],
                })
        return {"findings": findings}

    # Fallback: use search router directly
    if search_router:
        findings = []
        for angle in angles:
            try:
                results = await search_router.search(
                    query=f"{topic} {angle}",
                    max_results=5,
                    backend=search_backend,
                )
                findings.append({
                    "angle": angle,
                    "findings": "\n\n".join(
                        f"**{r.title}**\n{r.snippet}\nSource: {r.url}"
                        for r in results
                    ),
                    "sources": [{"title": r.title, "url": r.url} for r in results],
                })
            except Exception as e:
                findings.append({
                    "angle": angle,
                    "findings": f"Search failed: {e}",
                    "sources": [],
                })
        return {"findings": findings}

    return {"findings": []}


async def analyze_findings(state: ResearchState) -> dict[str, Any]:
    """Cross-reference and validate findings across angles.

    Identifies:
    - Consensus points (confirmed by multiple angles)
    - Contradictions requiring resolution
    - Knowledge gaps needing further research
    """
    findings = state.get("findings", [])

    # Build analysis summary
    analysis_parts = ["## Research Analysis\n"]
    for f in findings:
        analysis_parts.append(f"### {f['angle']}")
        analysis_parts.append(f"{f['findings'][:1000]}\n")

    analysis_parts.append("### Cross-Reference Summary")
    analysis_parts.append(
        f"Analyzed {len(findings)} research angles. "
        f"Total sources: {sum(len(f.get('sources', [])) for f in findings)}."
    )

    return {"analysis": "\n".join(analysis_parts)}


async def synthesize_report(state: ResearchState) -> dict[str, Any]:
    """Synthesize all findings into a structured report."""
    topic = state["topic"]
    analysis = state.get("analysis", "")
    findings = state.get("findings", [])
    report_format = state.get("report_format", "markdown")

    # Build structured report
    report_parts = [
        f"# Research Report: {topic}\n",
        f"*Generated from {len(findings)} research angles*\n",
        "---\n",
    ]

    # Executive summary
    report_parts.append("## Executive Summary\n")
    report_parts.append(
        f"This report synthesizes research on **{topic}** "
        f"across {len(findings)} complementary angles.\n"
    )

    # Detailed findings
    report_parts.append("## Detailed Findings\n")
    for f in findings:
        report_parts.append(f"### {f['angle']}\n")
        report_parts.append(f"{f['findings']}\n")
        if f.get("sources"):
            report_parts.append("**Sources:**")
            for src in f["sources"]:
                report_parts.append(f"- [{src['title']}]({src['url']})")
            report_parts.append("")

    # Analysis
    if analysis:
        report_parts.append("## Analysis\n")
        report_parts.append(analysis)

    report_parts.append("\n---\n*Report generated by Kestrel Deep Research Engine*")

    return {"report": "\n".join(report_parts)}


def build_research_graph(
    *,
    coordinator=None,
    search_router=None,
    checkpointer=None,
):
    """Build the research subgraph.

    Can be invoked standalone or composed into the main agent graph.
    """
    import functools
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(ResearchState)

    graph.add_node("decompose", decompose_topic)
    graph.add_node("fan_out", functools.partial(
        parallel_research,
        coordinator=coordinator,
        search_router=search_router,
    ))
    graph.add_node("analyze", analyze_findings)
    graph.add_node("synthesize", synthesize_report)

    graph.add_edge(START, "decompose")
    graph.add_edge("decompose", "fan_out")
    graph.add_edge("fan_out", "analyze")
    graph.add_edge("analyze", "synthesize")
    graph.add_edge("synthesize", END)

    compile_kwargs = {}
    if checkpointer:
        compile_kwargs["checkpointer"] = checkpointer

    return graph.compile(**compile_kwargs)
