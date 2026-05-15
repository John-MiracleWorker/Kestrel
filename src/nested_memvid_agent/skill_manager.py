from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .runtime_models import ToolCall, ToolExecution, ToolSpec
from .state_store import AgentStateStore
from .tools.base import AgentTool, ToolContext


@dataclass(frozen=True)
class SkillCapsule:
    id: str
    name: str
    description: str
    path: Path
    manifest: dict[str, Any]
    instructions: str
    enabled: bool = True


class SkillManager:
    """Discovers nested-learning skill capsules and exposes them as tools."""

    def __init__(self, root: Path, state: AgentStateStore) -> None:
        self.root = root
        self.state = state
        self.root.mkdir(parents=True, exist_ok=True)

    def discover(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for skill_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            manifest_path = skill_dir / "skill.json"
            instructions_path = skill_dir / "SKILL.md"
            if not manifest_path.exists() or not instructions_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            capsule = SkillCapsule(
                id=str(manifest.get("id", skill_dir.name)),
                name=str(manifest.get("name", skill_dir.name)),
                description=str(manifest.get("description", "")),
                path=skill_dir,
                manifest=manifest,
                instructions=instructions_path.read_text(encoding="utf-8"),
                enabled=bool(manifest.get("enabled", True)),
            )
            found.append(self.state.upsert_skill(_capsule_to_state(capsule)))
        return found

    def list_skills(self) -> list[dict[str, Any]]:
        return self.state.list_skills()

    def tool_adapters(self) -> list[AgentTool]:
        adapters: list[AgentTool] = []
        for skill in self.state.list_skills():
            if skill["enabled"]:
                adapters.append(SkillToolAdapter(_capsule_from_state(skill)))
        return adapters


class SkillToolAdapter(AgentTool):
    def __init__(self, capsule: SkillCapsule) -> None:
        self.capsule = capsule
        risk = str(capsule.manifest.get("risk", "medium"))
        self.spec = ToolSpec(
            name=f"skill.{capsule.id}.run",
            description=capsule.description or f"Run skill capsule {capsule.name}.",
            parameters=dict(
                capsule.manifest.get(
                    "parameters",
                    {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "context": {"type": "object"},
                        },
                        "required": ["task"],
                    },
                )
            ),
            risk="high" if risk == "high" else "medium" if risk == "medium" else "low",
            requires_approval=bool(capsule.manifest.get("requires_approval", risk in {"medium", "high"})),
            source="skill",
            skill_id=capsule.id,
            capabilities=tuple(str(item) for item in capsule.manifest.get("capabilities", ["skill", "nested-learning"])),
        )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        task = str(arguments.get("task", "")).strip()
        if not task:
            return self._result(call, success=False, content="Missing skill task.", error="missing_task")

        content = (
            f"Skill: {self.capsule.name}\n"
            f"Task: {task}\n\n"
            f"Instructions:\n{self.capsule.instructions.strip()}\n"
        )
        record = MemoryRecord(
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.EVENT,
            title=f"Skill run: {self.capsule.name}",
            content=content[:4000],
            confidence=0.7,
            importance=0.6,
            tags={"skill_id": self.capsule.id},
            metadata={"session_id": context.session_id, "run_id": context.run_id, "skill_id": self.capsule.id},
            evidence=[EvidenceRef(source="skill", locator=self.capsule.id)],
        )
        record_id = context.memory.put(record)
        context.memory.seal_all()
        return self._result(
            call,
            success=True,
            content=content,
            data={"skill_id": self.capsule.id, "memory_record_id": record_id},
        )


def _capsule_to_state(capsule: SkillCapsule) -> dict[str, Any]:
    return {
        "id": capsule.id,
        "name": capsule.name,
        "description": capsule.description,
        "path": str(capsule.path),
        "manifest": capsule.manifest,
        "enabled": capsule.enabled,
    }


def _capsule_from_state(row: dict[str, Any]) -> SkillCapsule:
    path = Path(str(row["path"]))
    instructions_path = path / "SKILL.md"
    instructions = instructions_path.read_text(encoding="utf-8") if instructions_path.exists() else ""
    return SkillCapsule(
        id=str(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        path=path,
        manifest=dict(row["manifest"]),
        instructions=instructions,
        enabled=bool(row["enabled"]),
    )
