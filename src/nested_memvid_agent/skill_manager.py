from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extension_policy import (
    ExtensionPolicyError,
    ExtensionScopes,
    extension_tree_digest,
    parse_extension_scopes,
)
from .extension_runner import (
    OCI_TOOL_TIMEOUT_MARGIN_SECONDS,
    ContainerExecutionRequest,
    OCIContainerRunner,
)
from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .runtime_models import ToolCall, ToolExecution, ToolSpec
from .security_boundary import redact_secrets, redact_text
from .skill_validation import validate_skill_manifest
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

    def __init__(
        self,
        root: Path,
        state: AgentStateStore,
        *,
        container_runner: OCIContainerRunner | None = None,
    ) -> None:
        self.root = root
        self.state = state
        self.container_runner = container_runner or OCIContainerRunner()
        self.validation_errors: list[dict[str, Any]] = []
        self.capability_policy: Any | None = None
        self.root.mkdir(parents=True, exist_ok=True)

    def discover(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        self.validation_errors = []
        root_resolved = self.root.resolve()
        for skill_dir in sorted(path for path in self.root.iterdir() if path.is_dir() and not path.is_symlink()):
            try:
                resolved = skill_dir.resolve()
            except OSError as exc:
                self.validation_errors.append({"path": str(skill_dir), "errors": [f"path_resolve_failed:{type(exc).__name__}"]})
                continue
            if resolved != root_resolved and root_resolved not in resolved.parents:
                self.validation_errors.append({"path": str(skill_dir), "errors": ["skill_path_escapes_root"]})
                continue
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
            try:
                tree_digest = extension_tree_digest(skill_dir)
            except (OSError, ExtensionPolicyError) as exc:
                self.validation_errors.append(
                    {"path": str(skill_dir), "errors": [str(exc)], "warnings": validation["warnings"]}
                )
                continue
            manifest = dict(manifest)
            manifest["validation"] = validation
            manifest["provenance"] = _skill_provenance(
                skill_dir,
                manifest_text,
                instructions,
                tree_sha256=tree_digest,
            )
            capsule = SkillCapsule(
                id=str(manifest.get("id", skill_dir.name)),
                name=str(manifest.get("name", skill_dir.name)),
                description=str(manifest.get("description", "")),
                path=skill_dir,
                manifest=manifest,
                instructions=instructions,
                # Files discovered from disk are declarative input, not an
                # authorization grant. New skills start off until the owner
                # enables them through the capability control plane.
                enabled=False,
            )
            # Discovery describes what exists on disk. It must not undo an
            # explicit operator decision already stored in the control plane.
            try:
                capsule = SkillCapsule(
                    id=capsule.id,
                    name=capsule.name,
                    description=capsule.description,
                    path=capsule.path,
                    manifest=capsule.manifest,
                    instructions=capsule.instructions,
                    enabled=bool(self.state.get_skill(capsule.id)["enabled"]),
                )
            except KeyError:
                pass
            found.append(self.state.upsert_skill(_capsule_to_state(capsule)))
        return found

    def discover_report(self) -> dict[str, Any]:
        skills = self.discover()
        errors = list(self.validation_errors)
        discovered_count = len(skills)
        enabled_count = sum(1 for skill in skills if bool(skill.get("enabled", False)))
        rejected_count = len(errors)
        if discovered_count and rejected_count:
            message = (
                f"Discovered {discovered_count} skill capsule(s); "
                f"validation rejected {rejected_count} skill capsule(s)."
            )
        elif discovered_count:
            message = f"Discovered {discovered_count} skill capsule(s)."
        elif rejected_count:
            message = f"Validation rejected {rejected_count} skill capsule(s); no valid skills discovered."
        else:
            message = f"No skill capsules found in {self.root}."
        return {
            "skills": skills,
            "discovered_count": discovered_count,
            "enabled_count": enabled_count,
            "skills_dir": str(self.root),
            "validation_errors": errors,
            "message": message,
        }

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
        stored = self.state.get_skill(skill_id)
        return {
            "installed": True,
            "dry_run": False,
            "skill_id": skill_id,
            "path": str(skill_dir),
            "validation": validation,
            "provenance": dict(stored.get("manifest", {})).get("provenance", {}),
        }

    def tool_adapters(self, *, include_disabled: bool = False) -> list[AgentTool]:
        adapters: list[AgentTool] = []
        for skill in self.state.list_skills():
            adapter = SkillToolAdapter(
                _capsule_from_state(skill),
                capability_policy=self.capability_policy,
                container_runner=self.container_runner,
            )
            parent_enabled = bool(skill["enabled"])
            if self.capability_policy is not None:
                parent_enabled = self.capability_policy.parent_decision(
                    "skill", str(skill["id"]), entity_enabled=parent_enabled
                ).effective_enabled
            if include_disabled or (
                parent_enabled
                and (
                    self.capability_policy is None
                    or self.capability_policy.tool_decision(adapter.spec).effective_enabled
                )
            ):
                adapters.append(adapter)
        return adapters


class SkillToolAdapter(AgentTool):
    wait_for_completion_on_timeout = True

    def __init__(
        self,
        capsule: SkillCapsule,
        *,
        capability_policy: Any | None = None,
        container_runner: OCIContainerRunner | None = None,
    ) -> None:
        self.capsule = capsule
        self.capability_policy = capability_policy
        self.container_runner = container_runner or OCIContainerRunner()
        risk = str(capsule.manifest.get("risk", "medium"))
        runtime = capsule.manifest.get("runtime", {"type": "instruction"})
        runtime_type = str(runtime.get("type", "instruction")) if isinstance(runtime, dict) else "instruction"
        executable_runtime = runtime_type in {"python", "shell", "container"}
        capabilities = [str(item) for item in capsule.manifest.get("capabilities", ["skill", "nested-learning"])]
        if executable_runtime and "executable-skill" not in capabilities:
            capabilities.append("executable-skill")
        if executable_runtime:
            capabilities.append(f"runtime:{runtime_type}")
            try:
                scopes = parse_extension_scopes(capsule.manifest.get("scopes", {}))
            except ExtensionPolicyError:
                capabilities.append("extension-scopes-invalid")
            else:
                capabilities.extend(_scope_capabilities(scopes))
            if runtime_type in {"python", "shell"}:
                capabilities.append("host-runtime-disabled")
            else:
                capabilities.append("container-isolated")
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
            risk="high" if executable_runtime or risk == "high" else "medium" if risk == "medium" else "low",
            requires_approval=True
            if executable_runtime
            else bool(capsule.manifest.get("requires_approval", risk in {"medium", "high"})),
            source="skill",
            skill_id=capsule.id,
            capabilities=tuple(capabilities),
            produces_validation=bool(capsule.manifest.get("produces_validation", False)),
        )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        if self.capability_policy is not None:
            decision = self.capability_policy.tool_decision(self.spec)
            if not decision.effective_enabled:
                return self._result(
                    call,
                    success=False,
                    content=f"Skill is disabled by {', '.join(decision.blocked_by)}.",
                    error="tool_disabled",
                )
        task = str(arguments.get("task", "")).strip()
        if not task:
            return self._result(call, success=False, content="Missing skill task.", error="missing_task")

        runtime = self.capsule.manifest.get("runtime", {"type": "instruction"})
        runtime_type = str(runtime.get("type", "instruction")) if isinstance(runtime, dict) else "instruction"
        if runtime_type in {"python", "shell", "container"} and not context.config.allow_executable_skills:
            return self._result(
                call,
                success=False,
                content="Executable skill runtimes are disabled by default.",
                error="tool_disabled",
            )
        container_timeout_budget: float | None = None
        if runtime_type == "container":
            container_timeout_budget = _container_timeout_budget(context.config.tool_timeout_seconds)
            if container_timeout_budget is None:
                return self._result(
                    call,
                    success=False,
                    content="Tool timeout is too small to start and reliably clean up a container.",
                    error="extension_timeout_budget_too_small",
                )
        try:
            scopes = parse_extension_scopes(self.capsule.manifest.get("scopes", {}))
            runtime_result = _run_skill_runtime(
                self.capsule,
                arguments=arguments,
                runtime=runtime if isinstance(runtime, dict) else {"type": "instruction"},
                task=task,
                workspace=context.workspace,
                scopes=scopes,
                container_runner=self.container_runner,
                execution_timeout_seconds=container_timeout_budget,
            )
        except (ExtensionPolicyError, OSError, ValueError) as exc:
            return self._result(call, success=False, content=f"{type(exc).__name__}: {exc}", error="skill_runtime_failed")

        safe_runtime_result = redact_secrets(runtime_result)
        runtime_result = safe_runtime_result if isinstance(safe_runtime_result, dict) else runtime_result
        content = redact_text(str(runtime_result["content"]))
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


def _run_skill_runtime(
    capsule: SkillCapsule,
    *,
    arguments: dict[str, Any],
    runtime: dict[str, Any],
    task: str,
    workspace: Path,
    scopes: ExtensionScopes,
    container_runner: OCIContainerRunner,
    execution_timeout_seconds: float | None,
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
    if runtime_type in {"python", "shell"}:
        return {
            "success": False,
            "content": (
                f"Host {runtime_type} skill execution is disabled. "
                "Use a digest-pinned container runtime with explicit scopes."
            ),
            "data": {
                "runtime": runtime_type,
                "containment": "required",
                "scopes": scopes.to_payload(),
            },
            "error": "extension_sandbox_required",
        }
    if runtime_type == "container":
        provenance = capsule.manifest.get("provenance", {})
        expected_tree_digest = (
            str(provenance.get("tree_sha256", "")) if isinstance(provenance, dict) else ""
        )
        if not expected_tree_digest.startswith("sha256:"):
            return {
                "success": False,
                "content": "Executable skill integrity metadata is unavailable; rediscover the skill.",
                "data": {"runtime": "container", "scopes": scopes.to_payload()},
                "error": "extension_integrity_unavailable",
            }
        image = str(runtime.get("image", ""))
        raw_command = runtime.get("command")
        command = tuple(str(item) for item in raw_command) if isinstance(raw_command, list) else ()
        result = container_runner.run(
            ContainerExecutionRequest(
                extension_id=capsule.id,
                source_dir=capsule.path,
                expected_tree_digest=expected_tree_digest,
                workspace=workspace,
                scopes=scopes,
                image=image,
                command=command,
                stdin=json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                timeout_seconds=min(
                    _runtime_timeout(runtime),
                    execution_timeout_seconds or _runtime_timeout(runtime),
                ),
            )
        )
        content = (
            f"Skill: {capsule.name}\n"
            "Runtime: container\n"
            f"Image: {image}\n"
            f"Exit code: {result.returncode}\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )
        if result.content and not result.success:
            content = f"{result.content}\n\n{content}"
        return {
            "success": result.success,
            "content": content,
            "data": {
                "runtime": "container",
                "containment": "oci",
                "image": image,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "tree_digest": result.tree_digest,
                "scope_digest": result.scope_digest,
                "scopes": scopes.to_payload(),
            },
            "error": result.error,
        }
    return {
        "success": False,
        "content": f"Unsupported skill runtime: {runtime_type}",
        "data": {"runtime": runtime_type},
        "error": "unsupported_runtime",
    }


def _runtime_timeout(runtime: dict[str, Any]) -> float:
    return max(1.0, min(float(runtime.get("timeout", 10)), 120.0))


def _container_timeout_budget(tool_timeout_seconds: float) -> float | None:
    timeout = max(float(tool_timeout_seconds), 0.001)
    if timeout <= OCI_TOOL_TIMEOUT_MARGIN_SECONDS:
        return None
    return timeout - OCI_TOOL_TIMEOUT_MARGIN_SECONDS


def _safe_skill_id(skill_id: str) -> bool:
    return skill_id.replace("_", "-").replace("-", "").isalnum()


def _safe_skill_dir(root: Path, skill_id: str) -> Path:
    target = (root / skill_id).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"Skill path escapes skills root: {skill_id}")
    return target


def _skill_provenance(
    skill_dir: Path,
    manifest_text: str,
    instructions: str,
    *,
    tree_sha256: str | None = None,
) -> dict[str, Any]:
    payload = {
        "path": str(skill_dir),
        "manifest_sha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
        "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
    }
    if tree_sha256:
        payload["tree_sha256"] = tree_sha256
    return payload


def _scope_capabilities(scopes: ExtensionScopes) -> list[str]:
    capabilities = [f"extension-scope:{scopes.digest()}", "network:none", "secrets:none"]
    capabilities.extend(
        f"filesystem:{scope.root}:{scope.path}:{scope.access}"
        for scope in scopes.filesystem
    )
    return capabilities


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
