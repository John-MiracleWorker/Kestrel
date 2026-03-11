from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from agent.tool_catalog import _default_catalog_dir


@dataclass(frozen=True)
class KernelNodeDefinition:
    name: str
    purpose: str
    prerequisites: tuple[str, ...] = ()
    activation_signals: tuple[str, ...] = ()
    cost_class: str = "medium"
    risk_impact: str = "low"
    config_schema: dict | None = None
    version: str = "v1"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "prerequisites": list(self.prerequisites),
            "activation_signals": list(self.activation_signals),
            "cost_class": self.cost_class,
            "risk_impact": self.risk_impact,
            "config_schema": self.config_schema or {},
            "version": self.version,
        }


class KernelNodeRegistry:
    """Versioned metadata registry for adaptive kernel nodes."""

    def __init__(self, *, markdown_path: str | None = None, json_path: str | None = None) -> None:
        base_dir = _default_catalog_dir()
        self.markdown_path = str(Path(markdown_path) if markdown_path else base_dir / "kernel-playbook.md")
        self.json_path = str(Path(json_path) if json_path else base_dir / "kernel-playbook.json")
        self._definitions = {
            item.name: item for item in (
                KernelNodeDefinition(
                    name="initialize",
                    purpose="Load lessons, semantic memory, and persona context.",
                    activation_signals=("task start", "new conversation", "background task"),
                    cost_class="low",
                ),
                KernelNodeDefinition(
                    name="policy",
                    purpose="Choose the active orchestration policy, node thresholds, and routing preferences.",
                    activation_signals=("task complexity", "risk", "subsystem health", "persona"),
                    cost_class="low",
                ),
                KernelNodeDefinition(
                    name="plan",
                    purpose="Break the goal into executable steps with expected tool hints.",
                    activation_signals=("complex task", "replan after drift"),
                    cost_class="medium",
                ),
                KernelNodeDefinition(
                    name="council",
                    purpose="Run weighted multi-perspective review for risky or complex plans.",
                    prerequisites=("llm_provider",),
                    activation_signals=("high complexity", "security-sensitive plan", "low confidence"),
                    cost_class="high",
                    risk_impact="medium",
                ),
                KernelNodeDefinition(
                    name="approve",
                    purpose="Pause for human approval when policy or risk requires it.",
                    activation_signals=("high risk", "policy gate", "simulation warning"),
                    cost_class="low",
                    risk_impact="high",
                ),
                KernelNodeDefinition(
                    name="execute",
                    purpose="Run the selected plan steps with adaptive tool injection.",
                    activation_signals=("plan ready",),
                    cost_class="high",
                    risk_impact="high",
                ),
                KernelNodeDefinition(
                    name="reflect",
                    purpose="Assess execution outcome and decide whether to continue, replan, or finish.",
                    activation_signals=("step failure", "uncertainty", "budget pressure"),
                    cost_class="medium",
                ),
                KernelNodeDefinition(
                    name="simulate",
                    purpose="Predict side effects and reversibility before execution.",
                    prerequisites=("simulation",),
                    activation_signals=("high impact", "irreversible actions", "write-heavy task"),
                    cost_class="medium",
                    risk_impact="medium",
                ),
                KernelNodeDefinition(
                    name="capability_gap",
                    purpose="Resolve missing capabilities by searching tools, recipes, or drafting a new skill.",
                    activation_signals=("no suitable tool", "repeated tool failure"),
                    cost_class="medium",
                    risk_impact="medium",
                ),
                KernelNodeDefinition(
                    name="complete",
                    purpose="Persist evidence, final answer, memory updates, and persona observations.",
                    activation_signals=("plan complete", "task terminal"),
                    cost_class="low",
                ),
                KernelNodeDefinition(
                    name="proactive_followup",
                    purpose="Schedule or enqueue follow-up work after a task finishes.",
                    prerequisites=("opportunity_engine",),
                    activation_signals=("background automation", "long-running objective"),
                    cost_class="low",
                    risk_impact="medium",
                ),
            )
        }
        self.write_playbook()

    def list_nodes(self) -> list[KernelNodeDefinition]:
        return list(self._definitions.values())

    def get(self, name: str) -> KernelNodeDefinition | None:
        return self._definitions.get(name)

    def to_dict(self) -> list[dict]:
        return [definition.to_dict() for definition in self.list_nodes()]

    def write_playbook(self) -> None:
        md_path = Path(self.markdown_path)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        json_path = Path(self.json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)

        markdown = ["# Kestrel Kernel Playbook", ""]
        for node in self.list_nodes():
            markdown.extend(
                [
                    f"## {node.name}",
                    f"- Purpose: {node.purpose}",
                    f"- Prerequisites: {', '.join(node.prerequisites) if node.prerequisites else '(none)'}",
                    f"- Activation signals: {', '.join(node.activation_signals) if node.activation_signals else '(none)'}",
                    f"- Cost class: {node.cost_class}",
                    f"- Risk impact: {node.risk_impact}",
                    "",
                ]
            )

        md_path.write_text("\n".join(markdown), encoding="utf-8")
        json_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
