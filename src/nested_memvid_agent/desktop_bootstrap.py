from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from hashlib import sha256
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from .desktop_memory_health import DesktopMemvidPreflightReceipt
from .layers import DEFAULT_LAYER_SPECS
from .platform_primitives import is_link_or_reparse_point
from .private_artifacts import read_private_text
from .routing.ledger_schema import ROUTING_SCHEMA_VERSION
from .state_store import SCHEMA_VERSION

_BOOTSTRAP_SCHEMA = "kestrel.desktop.bootstrap.v1"
_READINESS_SCHEMA = "kestrel.desktop.readiness.v1"
_MAX_BOOTSTRAP_BYTES = 16 * 1024
_BOOTSTRAP_KEYS = frozenset(
    {
        "schema",
        "profile_id",
        "profile_root",
        "state_path",
        "memory_dir",
        "runtime_settings_path",
        "launch_nonce",
        "api_token",
        "parent_pid",
        "parent_birth_marker",
        "resource_manifest_digest",
        "assurance_mode",
        "memory_layers",
    }
)
_DEFAULT_MEMORY_LAYERS = tuple(layer.value for layer in DEFAULT_LAYER_SPECS)


@dataclass(frozen=True)
class DesktopReadiness:
    profile_id: str
    launch_nonce_digest: str
    sidecar_version: str
    state_schema_version: int
    routing_schema_version: int
    memory_layers: tuple[str, ...]
    schema: str = _READINESS_SCHEMA
    ready: bool = True

    def to_public_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "ready": self.ready,
            "profile_id": self.profile_id,
            "launch_nonce_digest": self.launch_nonce_digest,
            "sidecar_version": self.sidecar_version,
            "state_schema_version": self.state_schema_version,
            "routing_schema_version": self.routing_schema_version,
            "memory_layers": list(self.memory_layers),
        }


@dataclass(frozen=True)
class DesktopLaunchConfig:
    profile_id: str
    profile_root: Path
    state_path: Path
    memory_dir: Path
    runtime_settings_path: Path
    launch_nonce: str = field(repr=False)
    api_token: str = field(repr=False)
    parent_pid: int
    parent_birth_marker: str
    resource_manifest_digest: str
    assurance_mode: str = "release"
    memory_preflight_receipt: DesktopMemvidPreflightReceipt | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        profile_id = _required_string(self.profile_id, "profile_id")
        launch_nonce = _required_string(self.launch_nonce, "launch_nonce")
        api_token = _required_string(self.api_token, "api_token")
        parent_birth_marker = _required_string(
            self.parent_birth_marker,
            "parent_birth_marker",
        )
        resource_manifest_digest = _required_string(
            self.resource_manifest_digest,
            "resource_manifest_digest",
        )
        assurance_mode = _required_string(self.assurance_mode, "assurance_mode")
        if assurance_mode not in {"developer", "release"}:
            raise ValueError("assurance_mode must be developer or release")
        if isinstance(self.parent_pid, bool) or not isinstance(self.parent_pid, int):
            raise ValueError("parent_pid must be an integer")
        if self.parent_pid <= 0:
            raise ValueError("parent_pid must be positive")

        profile_root = Path(self.profile_root).expanduser().resolve(strict=False)
        normalized_paths = {
            "state_path": _resolved_descendant(self.state_path, profile_root, "state_path"),
            "memory_dir": _resolved_descendant(self.memory_dir, profile_root, "memory_dir"),
            "runtime_settings_path": _resolved_descendant(
                self.runtime_settings_path,
                profile_root,
                "runtime_settings_path",
            ),
        }
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "profile_root", profile_root)
        object.__setattr__(self, "launch_nonce", launch_nonce)
        object.__setattr__(self, "api_token", api_token)
        object.__setattr__(self, "parent_birth_marker", parent_birth_marker)
        object.__setattr__(self, "resource_manifest_digest", resource_manifest_digest)
        object.__setattr__(self, "assurance_mode", assurance_mode)
        for name, path in normalized_paths.items():
            object.__setattr__(self, name, path)

    def launch_nonce_matches(self, candidate: str) -> bool:
        if not isinstance(candidate, str):
            return False
        return secrets.compare_digest(
            candidate.encode("utf-8"),
            self.launch_nonce.encode("utf-8"),
        )

    def readiness(self) -> DesktopReadiness:
        return DesktopReadiness(
            profile_id=self.profile_id,
            launch_nonce_digest=sha256(self.launch_nonce.encode("utf-8")).hexdigest(),
            sidecar_version=importlib_metadata.version("nested-memvid-agent"),
            state_schema_version=SCHEMA_VERSION,
            routing_schema_version=ROUTING_SCHEMA_VERSION,
            memory_layers=_DEFAULT_MEMORY_LAYERS,
        )

    def to_public_payload(self) -> dict[str, object]:
        return self.readiness().to_public_payload()


def consume_desktop_bootstrap(path: Path) -> DesktopLaunchConfig:
    bootstrap_path = Path(path)
    before_read = _validate_bootstrap_artifact(bootstrap_path)
    try:
        text = read_private_text(bootstrap_path)
    except ValueError as exc:
        if "symbolic link" in str(exc):
            raise PermissionError(
                f"Desktop bootstrap must not be a symlink: {bootstrap_path}"
            ) from exc
        raise
    if text is None:
        raise FileNotFoundError(bootstrap_path)
    if len(text.encode("utf-8")) > _MAX_BOOTSTRAP_BYTES:
        raise ValueError("Desktop bootstrap input exceeds 16 KiB")
    after_read = os.lstat(bootstrap_path)
    if is_link_or_reparse_point(after_read) or not os.path.samestat(before_read, after_read):
        raise PermissionError("Desktop bootstrap changed during its verified read")
    bootstrap_path.unlink()
    return _parse_bootstrap(text)


def _validate_bootstrap_artifact(path: Path) -> os.stat_result:
    metadata = os.lstat(path)
    if is_link_or_reparse_point(metadata):
        raise PermissionError(f"Desktop bootstrap must not be a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise PermissionError(f"Desktop bootstrap must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise PermissionError(f"Desktop bootstrap must not be hard-linked: {path}")
    if metadata.st_size > _MAX_BOOTSTRAP_BYTES:
        raise ValueError("Desktop bootstrap input exceeds 16 KiB")
    if os.name != "nt":
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and metadata.st_uid != geteuid():
            raise PermissionError("Desktop bootstrap must be owned by the current user")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o077 or not mode & stat.S_IRUSR:
            raise PermissionError("Desktop bootstrap must be owner-only")
    return metadata


def _parse_bootstrap(text: str) -> DesktopLaunchConfig:
    try:
        payload: Any = json.loads(text)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Desktop bootstrap must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Desktop bootstrap must be a JSON object")
    keys = frozenset(payload)
    if keys != _BOOTSTRAP_KEYS:
        missing = sorted(_BOOTSTRAP_KEYS - keys)
        extra = sorted(keys - _BOOTSTRAP_KEYS)
        raise ValueError(
            f"Desktop bootstrap keys must match exactly; missing={missing!r}, extra={extra!r}"
        )
    if payload["schema"] != _BOOTSTRAP_SCHEMA:
        raise ValueError(f"Desktop bootstrap schema must be {_BOOTSTRAP_SCHEMA}")
    memory_layers = payload["memory_layers"]
    if not isinstance(memory_layers, list) or tuple(memory_layers) != _DEFAULT_MEMORY_LAYERS:
        raise ValueError("Desktop bootstrap must declare exactly the six default memory layers")
    return DesktopLaunchConfig(
        profile_id=_required_string(payload["profile_id"], "profile_id"),
        profile_root=_path_value(payload["profile_root"], "profile_root"),
        state_path=_path_value(payload["state_path"], "state_path"),
        memory_dir=_path_value(payload["memory_dir"], "memory_dir"),
        runtime_settings_path=_path_value(
            payload["runtime_settings_path"],
            "runtime_settings_path",
        ),
        launch_nonce=_required_string(payload["launch_nonce"], "launch_nonce"),
        api_token=_required_string(payload["api_token"], "api_token"),
        parent_pid=payload["parent_pid"],
        parent_birth_marker=_required_string(
            payload["parent_birth_marker"],
            "parent_birth_marker",
        ),
        resource_manifest_digest=_required_string(
            payload["resource_manifest_digest"],
            "resource_manifest_digest",
        ),
        assurance_mode=_required_string(payload["assurance_mode"], "assurance_mode"),
    )


def _path_value(value: object, name: str) -> Path:
    return Path(_required_string(value, name))


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _resolved_descendant(value: Path, root: Path, name: str) -> Path:
    resolved = Path(value).expanduser().resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{name} must resolve under profile_root") from exc
    if not relative.parts:
        raise ValueError(f"{name} must resolve under profile_root")
    return resolved
