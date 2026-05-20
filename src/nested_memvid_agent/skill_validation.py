from __future__ import annotations

from typing import Any


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
