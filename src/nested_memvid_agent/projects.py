from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .security_boundary import redact_secrets, redact_text

PROJECT_EXPORT_FORMAT = "kestrel.project.v1"
PROJECT_PRIVACY_CLASSES = frozenset(
    {"local_required", "local_preferred", "approved_cloud"}
)
_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_TEXT_LENGTH = 4_096
_MAX_POLICY_BYTES = 65_536
_MAX_RECIPES = 64
_MAX_ALLOWED_PATHS = 256
_RECIPE_KEYS = frozenset({"name", "command", "working_directory"})
_EXPORT_PROJECT_KEYS = frozenset(
    {
        "project_id",
        "display_name",
        "repository_path",
        "remote",
        "default_branch",
        "allowed_paths",
        "provider_policy",
        "cost_budget",
        "privacy_class",
        "test_recipes",
        "build_recipes",
        "capability_ceiling",
        "baseline_index_digest",
    }
)


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str
    repository_path: str
    remote: str | None
    default_branch: str
    allowed_paths: tuple[str, ...]
    provider_policy: dict[str, Any]
    cost_budget: float | None
    privacy_class: str
    test_recipes: tuple[dict[str, str], ...]
    build_recipes: tuple[dict[str, str], ...]
    capability_ceiling: tuple[str, ...]
    baseline_index_digest: str | None
    revision: int
    archived_at: str | None
    created_at: str
    updated_at: str


class ProjectConflictError(RuntimeError):
    """Raised when a project compare-and-swap revision is stale."""

    def __init__(self, current: ProjectRecord) -> None:
        self.current = current
        super().__init__("project_revision_conflict")


def normalize_project_fields(
    *,
    project_id: str,
    display_name: str,
    repository_path: str | Path,
    remote: str | None = None,
    default_branch: str = "main",
    allowed_paths: Sequence[str] = (".",),
    provider_policy: Mapping[str, Any] | None = None,
    cost_budget: float | int | None = None,
    privacy_class: str = "local_required",
    test_recipes: Sequence[Mapping[str, str]] = (),
    build_recipes: Sequence[Mapping[str, str]] = (),
    capability_ceiling: Sequence[str] | None = None,
    active_capability_keys: Iterable[str] = (),
    baseline_index_digest: str | None = None,
) -> dict[str, Any]:
    active = frozenset(_capability_key(item) for item in active_capability_keys)
    return {
        "project_id": _project_id(project_id),
        "display_name": _bounded_text(
            display_name,
            field_name="display_name",
            maximum=256,
        ),
        "repository_path": str(canonical_repository_path(repository_path)),
        "remote": _optional_remote(remote),
        "default_branch": _bounded_text(
            default_branch,
            field_name="default_branch",
            maximum=256,
        ),
        "allowed_paths": normalize_allowed_paths(allowed_paths),
        "provider_policy": _json_mapping(provider_policy or {}, "provider_policy"),
        "cost_budget": _cost_budget(cost_budget),
        "privacy_class": _privacy_class(privacy_class),
        "test_recipes": normalize_recipes(test_recipes, field_name="test_recipes"),
        "build_recipes": normalize_recipes(build_recipes, field_name="build_recipes"),
        "capability_ceiling": _normalize_capability_ceiling(
            capability_ceiling,
            active_capability_keys=active,
        ),
        "baseline_index_digest": _optional_bounded_text(
            baseline_index_digest,
            field_name="baseline_index_digest",
            maximum=512,
        ),
    }


def canonical_repository_path(value: str | Path) -> Path:
    requested = Path(value)
    if not requested.is_absolute():
        raise ValueError("repository_path must be absolute")
    if requested.is_symlink():
        raise ValueError("repository_path must not be a symbolic link")
    lexical = Path(os.path.abspath(os.fspath(requested)))
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("repository_path must be an existing directory") from exc
    if lexical != resolved:
        raise ValueError("repository_path must be canonical and contain no symbolic links")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ValueError("repository_path must be an accessible non-symlink directory") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("repository_path must be an existing directory")
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and metadata.st_uid != geteuid():
            raise PermissionError("repository_path must be owned by the current user")
        current = os.stat(resolved, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("repository_path changed during validation")
    finally:
        os.close(descriptor)
    return resolved


def normalize_allowed_paths(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("allowed_paths must be a sequence of relative paths")
    if not values:
        raise ValueError("allowed_paths must contain at least one relative path")
    if len(values) > _MAX_ALLOWED_PATHS:
        raise ValueError(f"allowed_paths must contain at most {_MAX_ALLOWED_PATHS} entries")
    normalized: list[str] = []
    for raw in values:
        value = _bounded_text(raw, field_name="allowed_path", maximum=1_024)
        if "\\" in value:
            raise ValueError("allowed paths must use forward slashes")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("allowed paths must remain relative to the repository")
        canonical = path.as_posix()
        if canonical in {"", "/"}:
            canonical = "."
        if canonical not in normalized:
            normalized.append(canonical)
    if "." in normalized and len(normalized) > 1:
        raise ValueError("allowed path '.' already includes every repository path")
    return tuple(sorted(normalized))


def normalize_recipes(
    values: Sequence[Mapping[str, str]],
    *,
    field_name: str,
) -> tuple[dict[str, str], ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be a sequence")
    if len(values) > _MAX_RECIPES:
        raise ValueError(f"{field_name} must contain at most {_MAX_RECIPES} recipes")
    recipes: list[dict[str, str]] = []
    names: set[str] = set()
    for raw in values:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field_name} entries must be objects")
        unknown = set(raw) - _RECIPE_KEYS
        if unknown:
            raise ValueError(f"{field_name} contains unsupported fields: {sorted(unknown)}")
        name = _bounded_text(
            raw.get("name", ""),
            field_name=f"{field_name}.name",
            maximum=128,
        )
        if name.casefold() in names:
            raise ValueError(f"{field_name} recipe names must be unique")
        names.add(name.casefold())
        recipe = {
            "name": name,
            "command": _bounded_text(
                raw.get("command", ""),
                field_name=f"{field_name}.command",
                maximum=8_192,
            ),
        }
        if "working_directory" in raw:
            working_directory = normalize_allowed_paths(
                (str(raw["working_directory"]),)
            )[0]
            recipe["working_directory"] = working_directory
        recipes.append(recipe)
    return tuple(recipes)


def export_project(project: ProjectRecord) -> dict[str, object]:
    metadata: dict[str, object] = {
        "project_id": project.project_id,
        "display_name": project.display_name,
        "repository_path": project.repository_path,
        "remote": project.remote,
        "default_branch": project.default_branch,
        "allowed_paths": list(project.allowed_paths),
        "provider_policy": project.provider_policy,
        "cost_budget": project.cost_budget,
        "privacy_class": project.privacy_class,
        "test_recipes": list(project.test_recipes),
        "build_recipes": list(project.build_recipes),
        "capability_ceiling": list(project.capability_ceiling),
        "baseline_index_digest": project.baseline_index_digest,
    }
    safe = redact_secrets(metadata)
    if not isinstance(safe, dict):
        raise RuntimeError("project export redaction failed")
    return {
        "format": PROJECT_EXPORT_FORMAT,
        "project": cast(dict[str, object], safe),
    }


def import_project_document(
    document: Mapping[str, Any],
    *,
    active_capability_keys: Iterable[str],
) -> dict[str, Any]:
    if set(document) != {"format", "project"}:
        raise ValueError("project import contains unsupported top-level fields")
    if document.get("format") != PROJECT_EXPORT_FORMAT:
        raise ValueError("unsupported project export format")
    raw = document.get("project")
    if not isinstance(raw, Mapping):
        raise ValueError("project import metadata must be an object")
    unknown = set(raw) - _EXPORT_PROJECT_KEYS
    missing = _EXPORT_PROJECT_KEYS - set(raw)
    if unknown or missing:
        raise ValueError(
            "project import metadata fields do not match the reviewable export contract"
        )
    fields = dict(raw)
    fields["capability_ceiling"] = _normalize_capability_ceiling(
        fields.get("capability_ceiling"),
        active_capability_keys=frozenset(
            _capability_key(item) for item in active_capability_keys
        ),
    )
    return fields


def _normalize_capability_ceiling(
    values: object,
    *,
    active_capability_keys: frozenset[str],
) -> tuple[str, ...]:
    if values is None:
        return tuple(sorted(active_capability_keys))
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("capability_ceiling must be a sequence of capability keys")
    normalized = tuple(sorted({_capability_key(item) for item in values}))
    inactive = sorted(set(normalized) - active_capability_keys)
    if inactive:
        raise ValueError(
            "capability_ceiling may contain only a known active capability: "
            + ", ".join(inactive)
        )
    return normalized


def _capability_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("capability keys must be strings")
    normalized = value.strip()
    kind, separator, capability_id = normalized.partition(":")
    if (
        kind not in {"tool", "mcp_server", "skill"}
        or not separator
        or not capability_id
        or len(normalized) > 640
        or any(not character.isprintable() for character in normalized)
    ):
        raise ValueError(f"invalid capability key: {normalized or '<empty>'}")
    return normalized


def _project_id(value: str) -> str:
    if not isinstance(value, str) or not _PROJECT_ID_PATTERN.fullmatch(value.strip()):
        raise ValueError(
            "project_id must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_', or '-'"
        )
    return value.strip()


def _privacy_class(value: str) -> str:
    normalized = _bounded_text(
        value,
        field_name="privacy_class",
        maximum=32,
    ).lower()
    if normalized not in PROJECT_PRIVACY_CLASSES:
        raise ValueError(
            "privacy_class must be local_required, local_preferred, or approved_cloud"
        )
    return normalized


def _optional_remote(value: str | None) -> str | None:
    normalized = _optional_bounded_text(
        value,
        field_name="remote",
        maximum=2_048,
    )
    if normalized is not None and redact_text(normalized) != normalized:
        raise ValueError("remote must not contain embedded credentials")
    return normalized


def _cost_budget(value: float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("cost_budget must be a non-negative number or null")
    normalized = float(value)
    if not isfinite(normalized) or normalized < 0 or normalized > 1_000_000_000:
        raise ValueError("cost_budget must be finite and between 0 and 1000000000")
    return normalized


def _json_mapping(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    normalized = _json_value(dict(value), field_name=field_name, depth=0)
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must be an object")
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > _MAX_POLICY_BYTES:
        raise ValueError(f"{field_name} must be at most {_MAX_POLICY_BYTES} encoded bytes")
    return normalized


def _json_value(value: Any, *, field_name: str, depth: int) -> Any:
    if depth > 8:
        raise ValueError(f"{field_name} nesting is too deep")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > _MAX_TEXT_LENGTH:
            raise ValueError(f"{field_name} string values must be bounded")
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field_name} numbers must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} keys must be strings")
            normalized_key = _bounded_text(
                key,
                field_name=f"{field_name} key",
                maximum=256,
            )
            result[normalized_key] = _json_value(
                item,
                field_name=field_name,
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ValueError(f"{field_name} arrays must be bounded")
        return [
            _json_value(item, field_name=field_name, depth=depth + 1)
            for item in value
        ]
    raise ValueError(f"{field_name} must contain only JSON values")


def _bounded_text(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    if any(not character.isprintable() for character in normalized):
        raise ValueError(f"{field_name} contains non-printable characters")
    return normalized


def _optional_bounded_text(
    value: object,
    *,
    field_name: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name=field_name, maximum=maximum)
