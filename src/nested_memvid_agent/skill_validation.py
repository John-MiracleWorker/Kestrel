from __future__ import annotations

from typing import Any

from .extension_policy import extension_scope_validation_errors


def _digest_pinned_image(value: object) -> bool:
    reference = str(value).strip()
    name, marker, digest = reference.rpartition("@sha256:")
    return bool(
        marker
        and name
        and not name.startswith("-")
        and not any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in name)
        and len(digest) == 64
        and all(character in "0123456789abcdefABCDEF" for character in digest)
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
    else:
        runtime_type = str(runtime.get("type", "instruction"))
        if runtime_type not in {"instruction", "python", "shell", "container"}:
            errors.append("unsupported_runtime")
        elif runtime_type in {"python", "shell"}:
            warnings.append("host_runtime_execution_disabled")
        elif runtime_type == "container":
            if not _digest_pinned_image(runtime.get("image")):
                errors.append("container_image_not_digest_pinned")
            command = runtime.get("command")
            if (
                not isinstance(command, list)
                or not command
                or not all(
                    isinstance(item, str)
                    and item
                    and not any(ord(char) < 32 or ord(char) == 127 for char in item)
                    for item in command
                )
            ):
                errors.append("invalid_container_command")
        if "timeout" in runtime and (
            isinstance(runtime["timeout"], bool)
            or not isinstance(runtime["timeout"], (int, float))
            or not 1 <= float(runtime["timeout"]) <= 120
        ):
            errors.append("invalid_runtime_timeout")
    errors.extend(extension_scope_validation_errors(manifest.get("scopes", {})))
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
