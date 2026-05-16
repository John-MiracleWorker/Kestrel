from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
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
        self.validation_errors: list[dict[str, Any]] = []
        self.root.mkdir(parents=True, exist_ok=True)

    def discover(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        self.validation_errors = []
        for skill_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            manifest_path = skill_dir / "skill.json"
            instructions_path = skill_dir / "SKILL.md"
            if not manifest_path.exists() or not instructions_path.exists():
                continue
            try:
                manifest_text = manifest_path.read_text(encoding="utf-8")
                instructions = instructions_path.read_text(encoding="utf-8")
                manifest = json.loads(manifest_text)
            except (OSError, json.JSONDecodeError) as exc:
                self.validation_errors.append({"path": str(skill_dir), "errors": [f"manifest_read_failed:{type(exc).__name__}"]})
                continue
            validation = validate_skill_manifest(manifest)
            if validation["errors"]:
                self.validation_errors.append({"path": str(skill_dir), **validation})
                continue
            manifest = dict(manifest)
            manifest["validation"] = validation
            manifest["provenance"] = _skill_provenance(skill_dir, manifest_text, instructions)
            capsule = SkillCapsule(
                id=str(manifest.get("id", skill_dir.name)),
                name=str(manifest.get("name", skill_dir.name)),
                description=str(manifest.get("description", "")),
                path=skill_dir,
                manifest=manifest,
                instructions=instructions,
                enabled=bool(manifest.get("enabled", True)),
            )
            found.append(self.state.upsert_skill(_capsule_to_state(capsule)))
        return found

    def list_skills(self) -> list[dict[str, Any]]:
        return self.state.list_skills()

    def set_enabled(self, skill_id: str, enabled: bool) -> dict[str, Any]:
        return self.state.set_skill_enabled(skill_id, enabled)

    def install_skill(
        self,
        *,
        manifest: dict[str, Any],
        instructions: str,
        overwrite: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        validation = validate_skill_manifest(manifest)
        skill_id = str(manifest.get("id", "")).strip()
        if not skill_id:
            raise ValueError("Skill manifest must include id.")
        if not _safe_skill_id(skill_id):
            raise ValueError(f"Unsafe skill id: {skill_id}")
        if validation["errors"]:
            return {"installed": False, "dry_run": dry_run, "validation": validation}
        skill_dir = _safe_skill_dir(self.root, skill_id)
        if skill_dir.exists() and not overwrite:
            raise FileExistsError(f"Skill already exists: {skill_id}")
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True)
        if dry_run:
            return {
                "installed": False,
                "dry_run": True,
                "skill_id": skill_id,
                "path": str(skill_dir),
                "validation": validation,
                "provenance": _skill_provenance(skill_dir, manifest_text, instructions),
            }
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.json").write_text(manifest_text + "\n", encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(instructions, encoding="utf-8")
        self.discover()
        return {
            "installed": True,
            "dry_run": False,
            "skill_id": skill_id,
            "path": str(skill_dir),
            "validation": validation,
            "provenance": _skill_provenance(skill_dir, manifest_text, instructions),
        }

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

        runtime = self.capsule.manifest.get("runtime", {"type": "instruction"})
        try:
            runtime_result = _run_skill_runtime(
                self.capsule,
                arguments=arguments,
                runtime=runtime if isinstance(runtime, dict) else {"type": "instruction"},
                task=task,
            )
        except Exception as exc:  # noqa: BLE001 - skill boundary returns structured failure
            return self._result(call, success=False, content=f"{type(exc).__name__}: {exc}", error="skill_runtime_failed")

        content = runtime_result["content"]
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
            success=bool(runtime_result["success"]),
            content=content,
            data={**runtime_result["data"], "skill_id": self.capsule.id, "memory_record_id": record_id},
            error=None if runtime_result["success"] else str(runtime_result.get("error") or "skill_failed"),
        )


def validate_skill_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not str(manifest.get("id", "")).strip():
        errors.append("missing_id")
    if not str(manifest.get("description", "")).strip():
        errors.append("missing_description")
    risk = str(manifest.get("risk", "medium")).strip().lower()
    if risk not in {"low", "medium", "high"}:
        errors.append("invalid_risk")
    runtime = manifest.get("runtime", {"type": "instruction"})
    if not isinstance(runtime, dict):
        errors.append("invalid_runtime")
    elif str(runtime.get("type", "instruction")) not in {"instruction", "python", "shell", "container"}:
        errors.append("unsupported_runtime")
    for field in ("capabilities", "permissions", "tests"):
        if field in manifest and not isinstance(manifest[field], list):
            errors.append(f"invalid_{field}")
    for field in ("parameters", "inputs", "outputs"):
        if field in manifest and not isinstance(manifest[field], dict):
            errors.append(f"invalid_{field}")
    if "version" not in manifest:
        warnings.append("missing_version")
    if "permissions" not in manifest:
        warnings.append("missing_permissions")
    if "runtime" not in manifest:
        warnings.append("default_instruction_runtime")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _run_skill_runtime(
    capsule: SkillCapsule,
    *,
    arguments: dict[str, Any],
    runtime: dict[str, Any],
    task: str,
) -> dict[str, Any]:
    runtime_type = str(runtime.get("type", "instruction"))
    payload = {
        "task": task,
        "arguments": {key: value for key, value in arguments.items() if key != "task"},
        "context": arguments.get("context", {}),
        "skill": {
            "id": capsule.id,
            "name": capsule.name,
            "description": capsule.description,
        },
    }
    if runtime_type == "instruction":
        content = (
            f"Skill: {capsule.name}\n"
            f"Task: {task}\n\n"
            f"Instructions:\n{capsule.instructions.strip()}\n"
        )
        return {"success": True, "content": content, "data": {"runtime": "instruction"}}
    if runtime_type == "python":
        entrypoint = str(runtime.get("entrypoint", "skill.py"))
        script = _safe_skill_path(capsule.path, entrypoint)
        if not script.exists() or not script.is_file():
            return {
                "success": False,
                "content": f"Python skill entrypoint does not exist: {entrypoint}",
                "data": {"runtime": "python", "entrypoint": entrypoint},
                "error": "missing_entrypoint",
            }
        return _run_skill_process(
            capsule,
            command=[sys.executable, str(script)],
            payload=payload,
            runtime_type="python",
            timeout_seconds=_runtime_timeout(runtime),
        )
    if runtime_type == "shell":
        command = runtime.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            return {
                "success": False,
                "content": "Shell skill runtime requires command as list[str].",
                "data": {"runtime": "shell"},
                "error": "bad_skill_command",
            }
        if not command:
            return {
                "success": False,
                "content": "Shell skill runtime command cannot be empty.",
                "data": {"runtime": "shell"},
                "error": "bad_skill_command",
            }
        return _run_skill_process(
            capsule,
            command=list(command),
            payload=payload,
            runtime_type="shell",
            timeout_seconds=_runtime_timeout(runtime),
        )
    if runtime_type == "container":
        return {
            "success": False,
            "content": "Container skill runtime is not available in this local sandbox yet.",
            "data": {"runtime": "container"},
            "error": "container_runtime_unavailable",
        }
    return {
        "success": False,
        "content": f"Unsupported skill runtime: {runtime_type}",
        "data": {"runtime": runtime_type},
        "error": "unsupported_runtime",
    }


def _run_skill_process(
    capsule: SkillCapsule,
    *,
    command: list[str],
    payload: dict[str, Any],
    runtime_type: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603 - list argv, skill cwd, timeout, no shell
        command,
        cwd=capsule.path,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1", "NEST_SKILL_SANDBOX": "1"},
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    content = (
        f"Skill: {capsule.name}\n"
        f"Runtime: {runtime_type}\n"
        f"Exit code: {completed.returncode}\n\n"
        f"STDOUT:\n{stdout}\n\n"
        f"STDERR:\n{stderr}"
    )
    return {
        "success": completed.returncode == 0,
        "content": content,
        "data": {
            "runtime": runtime_type,
            "returncode": completed.returncode,
            "stdout": stdout[:4000],
            "stderr": stderr[:4000],
        },
        "error": None if completed.returncode == 0 else "skill_nonzero_exit",
    }


def _runtime_timeout(runtime: dict[str, Any]) -> int:
    return max(1, min(int(runtime.get("timeout", 10)), 120))


def _safe_skill_id(skill_id: str) -> bool:
    return skill_id.replace("_", "-").replace("-", "").isalnum()


def _safe_skill_dir(root: Path, skill_id: str) -> Path:
    target = (root / skill_id).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"Skill path escapes skills root: {skill_id}")
    return target


def _safe_skill_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"Skill path escapes skill root: {relative}")
    return target


def _skill_provenance(skill_dir: Path, manifest_text: str, instructions: str) -> dict[str, Any]:
    return {
        "path": str(skill_dir),
        "manifest_sha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
        "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
    }


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
