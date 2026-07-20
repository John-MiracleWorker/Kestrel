from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_EXTENSION_FILES = 512
MAX_EXTENSION_ENTRIES = 1024
MAX_EXTENSION_DEPTH = 64
MAX_EXTENSION_FILE_BYTES = 8 * 1024 * 1024
MAX_EXTENSION_TREE_BYTES = 32 * 1024 * 1024
MAX_FILESYSTEM_SCOPES = 16
MAX_FILESYSTEM_SCOPE_ENTRIES = 4096
MAX_FILESYSTEM_SCOPE_FILE_BYTES = 64 * 1024 * 1024
MAX_FILESYSTEM_SCOPE_TREE_BYTES = 256 * 1024 * 1024
MAX_FILESYSTEM_SCOPE_DEPTH = 64
_CONTROL_TREE_NAMES = frozenset({".git", ".nest"})


class ExtensionPolicyError(ValueError):
    """Raised when executable-extension policy cannot be enforced safely."""


@dataclass(frozen=True, order=True)
class FilesystemScope:
    """A single workspace subtree exposed to an extension container."""

    path: str
    access: str
    root: str = "workspace"

    def to_payload(self) -> dict[str, str]:
        return {"root": self.root, "path": self.path, "access": self.access}


@dataclass(frozen=True)
class ExtensionScopes:
    """Canonical, default-deny executable-extension scopes.

    The extension's own snapshotted source is always mounted read-only as its
    execution substrate and is not a grant to user data. Workspace access must
    be declared explicitly. Network access is intentionally limited to
    ``none`` in this first containment phase. Secret injection is also denied
    until a broker-to-mounted-file path exists; raw secret values must never be
    smuggled through a manifest or container argv.
    """

    filesystem: tuple[FilesystemScope, ...] = ()
    network: str = "none"
    secrets: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "filesystem": [item.to_payload() for item in self.filesystem],
            "network": {"mode": self.network},
            "secrets": list(self.secrets),
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedFilesystemScope:
    source: Path
    target: str
    access: str
    declared_path: str
    source_stat: os.stat_result


def parse_extension_scopes(value: object) -> ExtensionScopes:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ExtensionPolicyError("extension_scopes_must_be_an_object")
    unknown = sorted(str(key) for key in value if str(key) not in {"filesystem", "network", "secrets"})
    if unknown:
        raise ExtensionPolicyError(f"unknown_extension_scope:{','.join(unknown)}")

    raw_filesystem = value.get("filesystem", [])
    if not isinstance(raw_filesystem, list):
        raise ExtensionPolicyError("filesystem_scopes_must_be_a_list")
    if len(raw_filesystem) > MAX_FILESYSTEM_SCOPES:
        raise ExtensionPolicyError("too_many_filesystem_scopes")
    filesystem: list[FilesystemScope] = []
    seen: set[str] = set()
    for raw_scope in raw_filesystem:
        if not isinstance(raw_scope, dict):
            raise ExtensionPolicyError("filesystem_scope_must_be_an_object")
        scope_unknown = sorted(
            str(key) for key in raw_scope if str(key) not in {"root", "path", "access"}
        )
        if scope_unknown:
            raise ExtensionPolicyError(f"unknown_filesystem_scope_field:{','.join(scope_unknown)}")
        root = str(raw_scope.get("root", "workspace")).strip().lower()
        if root != "workspace":
            raise ExtensionPolicyError("filesystem_scope_root_must_be_workspace")
        path = _canonical_relative_path(raw_scope.get("path", ""))
        access = str(raw_scope.get("access", "read")).strip().lower()
        if access not in {"read", "write"}:
            raise ExtensionPolicyError("filesystem_scope_access_must_be_read_or_write")
        if access == "write":
            raise ExtensionPolicyError("extension_write_scope_unsupported")
        identity = f"{root}:{path}".casefold()
        if identity in seen:
            raise ExtensionPolicyError("duplicate_filesystem_scope")
        seen.add(identity)
        filesystem.append(FilesystemScope(root=root, path=path, access=access))

    filesystem.sort()
    _reject_overlapping_filesystem_scopes(filesystem)

    raw_network = value.get("network", {"mode": "none"})
    if isinstance(raw_network, str):
        network = raw_network.strip().lower()
    elif isinstance(raw_network, dict):
        network_unknown = sorted(str(key) for key in raw_network if str(key) != "mode")
        if network_unknown:
            raise ExtensionPolicyError(f"unknown_network_scope_field:{','.join(network_unknown)}")
        network = str(raw_network.get("mode", "none")).strip().lower()
    else:
        raise ExtensionPolicyError("network_scope_must_be_an_object")
    if network != "none":
        raise ExtensionPolicyError("extension_network_scope_unsupported")

    raw_secrets = value.get("secrets", [])
    if not isinstance(raw_secrets, list) or not all(isinstance(item, str) for item in raw_secrets):
        raise ExtensionPolicyError("secret_scopes_must_be_a_list_of_names")
    if raw_secrets:
        raise ExtensionPolicyError("extension_secret_scopes_unsupported")

    return ExtensionScopes(filesystem=tuple(filesystem), network=network, secrets=())


def extension_scope_validation_errors(value: object) -> list[str]:
    try:
        parse_extension_scopes(value)
    except ExtensionPolicyError as exc:
        return [str(exc)]
    return []


def resolve_filesystem_scopes(scopes: ExtensionScopes, workspace: Path) -> tuple[ResolvedFilesystemScope, ...]:
    workspace_path = workspace.expanduser()
    if workspace_path.is_symlink():
        raise ExtensionPolicyError("extension_workspace_symlink_rejected")
    root = workspace_path.resolve()
    if not root.exists() or not root.is_dir():
        raise ExtensionPolicyError("extension_workspace_unavailable")
    resolved: list[ResolvedFilesystemScope] = []
    for scope in scopes.filesystem:
        requested = root if scope.path == "." else root.joinpath(*PurePosixPath(scope.path).parts)
        _reject_symlink_components(root, requested, scope.path)
        try:
            source = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ExtensionPolicyError(f"extension_scope_path_unavailable:{scope.path}") from exc
        try:
            relative = source.relative_to(root)
        except ValueError as exc:
            raise ExtensionPolicyError(f"extension_scope_path_escapes_workspace:{scope.path}") from exc
        if "," in str(source):
            raise ExtensionPolicyError("extension_scope_path_contains_unsupported_comma")
        try:
            source_stat = source.lstat()
        except OSError as exc:
            raise ExtensionPolicyError(
                f"extension_scope_path_unavailable:{scope.path}"
            ) from exc
        target = "/workspace/" + PurePosixPath(*relative.parts).as_posix()
        resolved.append(
            ResolvedFilesystemScope(
                source=source,
                target=target,
                access=scope.access,
                declared_path=scope.path,
                source_stat=source_stat,
            )
        )
    result = tuple(resolved)
    validate_resolved_filesystem_scopes(result)
    return result


def validate_resolved_filesystem_scopes(
    scopes: tuple[ResolvedFilesystemScope, ...],
) -> None:
    """Validate scope roots without treating a pathname scan as isolation.

    Recursive policy enforcement happens while copying through directory file
    descriptors in :func:`copy_readonly_filesystem_scope_snapshots`.  This
    shallow check is also used immediately before mounting Kestrel-owned
    snapshots and verifies that their root objects have not been replaced.
    """

    for scope in scopes:
        if scope.access != "read":
            raise ExtensionPolicyError("extension_write_scope_unsupported")
        try:
            current = scope.source.lstat()
        except OSError as exc:
            raise ExtensionPolicyError(
                f"extension_scope_scan_failed:{scope.declared_path}"
            ) from exc
        if not _same_stat_snapshot(scope.source_stat, current):
            raise ExtensionPolicyError(
                f"extension_scope_changed_during_snapshot:{scope.declared_path}"
            )
        if stat.S_ISLNK(current.st_mode):
            raise ExtensionPolicyError(
                f"extension_scope_path_symlink_rejected:{scope.declared_path}"
            )
        if stat.S_ISREG(current.st_mode):
            if current.st_nlink != 1:
                raise ExtensionPolicyError(
                    f"extension_scope_hardlink_rejected:{scope.declared_path}"
                )
            continue
        if not stat.S_ISDIR(current.st_mode):
            raise ExtensionPolicyError(
                f"extension_scope_nonregular_rejected:{scope.declared_path}"
            )


def copy_readonly_filesystem_scope_snapshots(
    scopes: tuple[ResolvedFilesystemScope, ...],
    destination_root: Path,
    *,
    workspace: Path,
) -> tuple[ResolvedFilesystemScope, ...]:
    """Snapshot bounded read grants so the engine never binds live user data."""

    destination_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    directory_flags = _required_scope_directory_flags()
    workspace_root = workspace.expanduser().resolve(strict=True)
    workspace_descriptor = os.open(workspace_root, directory_flags)
    try:
        remaining_entries = MAX_FILESYSTEM_SCOPE_ENTRIES
        remaining_bytes = MAX_FILESYSTEM_SCOPE_TREE_BYTES
        snapshots: list[ResolvedFilesystemScope] = []
        for index, scope in enumerate(scopes):
            destination = destination_root / f"scope-{index:02d}"
            consumed_entries, consumed_bytes = _copy_readonly_scope_tree(
                workspace_descriptor,
                scope,
                destination,
                remaining_entries=remaining_entries,
                remaining_bytes=remaining_bytes,
                directory_flags=directory_flags,
            )
            remaining_entries -= consumed_entries
            remaining_bytes -= consumed_bytes
            snapshots.append(
                ResolvedFilesystemScope(
                    source=destination,
                    target=scope.target,
                    access="read",
                    declared_path=scope.declared_path,
                    source_stat=destination.lstat(),
                )
            )
    finally:
        os.close(workspace_descriptor)
    destination_root.chmod(0o500)
    return tuple(snapshots)


def _reject_symlink_components(root: Path, requested: Path, declared_path: str) -> None:
    """Reject lexical symlinks before resolving an extension workspace grant."""

    current = root
    for part in requested.relative_to(root).parts:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ExtensionPolicyError(
                f"extension_scope_path_unavailable:{declared_path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ExtensionPolicyError(
                f"extension_scope_path_symlink_rejected:{declared_path}"
            )


def extension_tree_digest(root: Path) -> str:
    """Hash a bounded, symlink-free extension tree including executable mode bits."""

    return _process_extension_tree(root, destination=None)


def copy_extension_snapshot(source: Path, destination: Path) -> str:
    """Copy a bounded extension tree without following links, then hash the snapshot."""

    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    result = _process_extension_tree(source, destination=destination)
    destination.chmod(0o500)
    return result


@dataclass
class _ExtensionTreeBudget:
    entries: int = 0
    files: int = 0
    bytes: int = 0


def _process_extension_tree(source: Path, *, destination: Path | None) -> str:
    if source.is_symlink():
        raise ExtensionPolicyError("extension_root_must_be_a_real_directory")
    try:
        resolved = source.expanduser().resolve(strict=True)
        expected_root = resolved.lstat()
    except OSError as exc:
        raise ExtensionPolicyError("extension_root_must_be_a_real_directory") from exc
    if not stat.S_ISDIR(expected_root.st_mode):
        raise ExtensionPolicyError("extension_root_must_be_a_real_directory")
    directory_flags = _required_directory_flags(
        "extension_snapshot_platform_unsupported"
    )
    descriptor = _open_absolute_directory_descriptor(resolved, directory_flags)
    digest = hashlib.sha256()
    try:
        opened_root = os.fstat(descriptor)
        if not _same_stat_snapshot(expected_root, opened_root):
            raise ExtensionPolicyError("extension_tree_changed_during_read:.")
        _process_extension_directory_descriptor(
            descriptor,
            relative=PurePosixPath("."),
            destination=destination,
            digest=digest,
            budget=_ExtensionTreeBudget(),
            root_device=opened_root.st_dev,
            depth=0,
            directory_flags=directory_flags,
        )
        if not _same_stat_snapshot(opened_root, os.fstat(descriptor)):
            raise ExtensionPolicyError("extension_tree_changed_during_read:.")
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def _process_extension_directory_descriptor(
    descriptor: int,
    *,
    relative: PurePosixPath,
    destination: Path | None,
    digest: Any,
    budget: _ExtensionTreeBudget,
    root_device: int,
    depth: int,
    directory_flags: int,
) -> None:
    if depth > MAX_EXTENSION_DEPTH:
        raise ExtensionPolicyError("extension_tree_depth_limit_exceeded")
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                budget.entries += 1
                if budget.entries > MAX_EXTENSION_ENTRIES:
                    raise ExtensionPolicyError(
                        "extension_tree_entry_limit_exceeded"
                    )
                entries.append((entry.name, entry.stat(follow_symlinks=False)))
    except ExtensionPolicyError:
        raise
    except OSError as exc:
        raise ExtensionPolicyError("extension_tree_scan_failed") from exc

    for name, metadata in sorted(entries, key=lambda item: item[0]):
        child_relative = (
            PurePosixPath(name)
            if relative == PurePosixPath(".")
            else relative / name
        )
        display_path = child_relative.as_posix()
        if metadata.st_dev != root_device:
            raise ExtensionPolicyError(
                f"extension_tree_filesystem_crossing_rejected:{display_path}"
            )
        if stat.S_ISLNK(metadata.st_mode):
            raise ExtensionPolicyError(
                f"extension_tree_symlink_rejected:{display_path}"
            )
        target = destination / name if destination is not None else None
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(_digest_bytes(f"dir\0{display_path}\0"))
            try:
                child_descriptor = os.open(
                    name, directory_flags, dir_fd=descriptor
                )
            except OSError as exc:
                raise ExtensionPolicyError(
                    f"extension_tree_changed_during_read:{display_path}"
                ) from exc
            try:
                opened = os.fstat(child_descriptor)
                if not _same_stat_snapshot(metadata, opened):
                    raise ExtensionPolicyError(
                        f"extension_tree_changed_during_read:{display_path}"
                    )
                if target is not None:
                    target.mkdir(mode=0o700)
                _process_extension_directory_descriptor(
                    child_descriptor,
                    relative=child_relative,
                    destination=target,
                    digest=digest,
                    budget=budget,
                    root_device=root_device,
                    depth=depth + 1,
                    directory_flags=directory_flags,
                )
                if not _same_stat_snapshot(opened, os.fstat(child_descriptor)):
                    raise ExtensionPolicyError(
                        f"extension_tree_changed_during_read:{display_path}"
                    )
                if target is not None:
                    target.chmod(0o500)
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ExtensionPolicyError(
                f"extension_tree_nonregular_rejected:{display_path}"
            )
        if metadata.st_nlink != 1:
            raise ExtensionPolicyError(
                f"extension_tree_hardlink_rejected:{display_path}"
            )
        budget.files += 1
        if budget.files > MAX_EXTENSION_FILES:
            raise ExtensionPolicyError("extension_tree_file_limit_exceeded")
        if metadata.st_size > MAX_EXTENSION_FILE_BYTES:
            raise ExtensionPolicyError(
                f"extension_tree_file_too_large:{display_path}"
            )
        budget.bytes += metadata.st_size
        if budget.bytes > MAX_EXTENSION_TREE_BYTES:
            raise ExtensionPolicyError("extension_tree_size_limit_exceeded")
        executable = int(bool(stat.S_IMODE(metadata.st_mode) & 0o111))
        digest.update(
            _digest_bytes(
                f"file\0{display_path}\0{executable}\0{metadata.st_size}\0"
            )
        )
        _hash_and_copy_extension_file_at(
            descriptor,
            name,
            target,
            metadata=metadata,
            relative=display_path,
            digest=digest,
        )


def _hash_and_copy_extension_file_at(
    parent_descriptor: int,
    name: str,
    target: Path | None,
    *,
    metadata: os.stat_result,
    relative: str,
    digest: Any,
) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        _validate_open_extension_file(
            os.fstat(descriptor), metadata=metadata, relative=relative
        )
        target_handle = target.open("xb") if target is not None else None
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
                while chunk := source_handle.read(64 * 1024):
                    digest.update(chunk)
                    if target_handle is not None:
                        target_handle.write(chunk)
        finally:
            if target_handle is not None:
                target_handle.close()
        _validate_open_extension_file(
            os.fstat(descriptor), metadata=metadata, relative=relative
        )
    finally:
        os.close(descriptor)
    if target is not None:
        target.chmod(0o500 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o400)


def _digest_bytes(value: str) -> bytes:
    return value.encode("utf-8", errors="surrogateescape")


def _canonical_relative_path(value: object) -> str:
    raw = str(value).strip()
    if not raw:
        raise ExtensionPolicyError("filesystem_scope_path_required")
    if "\\" in raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ExtensionPolicyError("filesystem_scope_path_not_portable")
    path = PurePosixPath(raw)
    if raw == ".":
        raise ExtensionPolicyError("filesystem_scope_workspace_root_rejected")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ExtensionPolicyError("filesystem_scope_path_must_be_relative")
    normalized = path.as_posix()
    if any(part.casefold() in _CONTROL_TREE_NAMES for part in path.parts):
        raise ExtensionPolicyError("filesystem_scope_control_tree_rejected")
    return normalized


def _reject_overlapping_filesystem_scopes(scopes: list[FilesystemScope]) -> None:
    paths = [(scope, PurePosixPath(scope.path)) for scope in scopes]
    for index, (left_scope, left) in enumerate(paths):
        for right_scope, right in paths[index + 1 :]:
            if left_scope.root != right_scope.root:
                continue
            if left == PurePosixPath(".") or right == PurePosixPath("."):
                raise ExtensionPolicyError("overlapping_filesystem_scopes")
            if left in right.parents or right in left.parents:
                raise ExtensionPolicyError("overlapping_filesystem_scopes")


def _scope_display_path(declared_path: str, relative: PurePosixPath) -> str:
    if relative == PurePosixPath("."):
        return declared_path
    return f"{declared_path}/{relative.as_posix()}"


def _copy_readonly_scope_tree(
    workspace_descriptor: int,
    scope: ResolvedFilesystemScope,
    destination: Path,
    *,
    remaining_entries: int,
    remaining_bytes: int,
    directory_flags: int,
) -> tuple[int, int]:
    declared_path = scope.declared_path
    if remaining_entries < 1:
        raise ExtensionPolicyError("extension_scope_entry_limit_exceeded")
    try:
        descriptor, root_metadata = _open_declared_scope_descriptor(
            workspace_descriptor,
            declared_path=declared_path,
            directory_flags=directory_flags,
        )
    except OSError as exc:
        raise ExtensionPolicyError(
            f"extension_scope_scan_failed:{declared_path}"
        ) from exc
    try:
        if not _same_stat_snapshot(scope.source_stat, root_metadata):
            raise ExtensionPolicyError(
                f"extension_scope_changed_during_snapshot:{declared_path}"
            )
        if stat.S_ISREG(root_metadata.st_mode):
            _validate_scope_file_metadata(
                root_metadata,
                display_path=declared_path,
                remaining_bytes=remaining_bytes,
            )
            _copy_regular_descriptor(
                descriptor,
                destination,
                metadata=root_metadata,
                relative=declared_path,
            )
            return 1, root_metadata.st_size
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise ExtensionPolicyError(
                f"extension_scope_nonregular_rejected:{declared_path}"
            )
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        budget = _ScopeSnapshotBudget(
            remaining_entries=remaining_entries - 1,
            remaining_bytes=remaining_bytes,
            consumed_entries=1,
        )
        _copy_scope_directory_descriptor(
            descriptor,
            destination,
            relative=PurePosixPath("."),
            declared_path=declared_path,
            root_device=root_metadata.st_dev,
            budget=budget,
            depth=0,
            directory_flags=directory_flags,
        )
        if not _same_stat_snapshot(root_metadata, os.fstat(descriptor)):
            raise ExtensionPolicyError(
                f"extension_scope_changed_during_snapshot:{declared_path}"
            )
        destination.chmod(0o500)
        return budget.consumed_entries, budget.consumed_bytes
    finally:
        os.close(descriptor)


def _required_scope_directory_flags() -> int:
    return _required_directory_flags(
        "extension_scope_snapshot_platform_unsupported"
    )


def _required_directory_flags(error: str) -> int:
    if (
        os.open not in os.supports_dir_fd
        or os.scandir not in os.supports_fd
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise ExtensionPolicyError(error)
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_absolute_directory_descriptor(path: Path, directory_flags: int) -> int:
    if os.name == "nt" or not path.is_absolute():
        raise ExtensionPolicyError("extension_snapshot_platform_unsupported")
    current = os.open(path.anchor, directory_flags)
    try:
        for part in path.parts[1:]:
            metadata = os.stat(part, dir_fd=current, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ExtensionPolicyError(
                    "extension_root_must_be_a_real_directory"
                )
            child = os.open(part, directory_flags, dir_fd=current)
            opened = os.fstat(child)
            if not _same_stat_snapshot(metadata, opened):
                os.close(child)
                raise ExtensionPolicyError("extension_tree_changed_during_read:.")
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def _open_declared_scope_descriptor(
    workspace_descriptor: int,
    *,
    declared_path: str,
    directory_flags: int,
) -> tuple[int, os.stat_result]:
    parts = PurePosixPath(declared_path).parts
    current = os.dup(workspace_descriptor)
    opened = os.fstat(current)
    try:
        for index, part in enumerate(parts):
            metadata = os.stat(part, dir_fd=current, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ExtensionPolicyError(
                    f"extension_scope_path_symlink_rejected:{declared_path}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                flags = directory_flags
            elif stat.S_ISREG(metadata.st_mode) and index == len(parts) - 1:
                flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            else:
                raise ExtensionPolicyError(
                    f"extension_scope_nonregular_rejected:{declared_path}"
                )
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
            opened = os.fstat(current)
            if not _same_stat_snapshot(metadata, opened):
                raise ExtensionPolicyError(
                    f"extension_scope_changed_during_snapshot:{declared_path}"
                )
        return current, opened
    except Exception:
        os.close(current)
        raise


def _validate_scope_file_metadata(
    metadata: os.stat_result,
    *,
    display_path: str,
    remaining_bytes: int,
) -> None:
    if metadata.st_nlink != 1:
        raise ExtensionPolicyError(
            f"extension_scope_hardlink_rejected:{display_path}"
        )
    if metadata.st_size > MAX_FILESYSTEM_SCOPE_FILE_BYTES:
        raise ExtensionPolicyError(
            f"extension_scope_file_too_large:{display_path}"
        )
    if metadata.st_size > remaining_bytes:
        raise ExtensionPolicyError("extension_scope_size_limit_exceeded")


@dataclass
class _ScopeSnapshotBudget:
    remaining_entries: int
    remaining_bytes: int
    consumed_entries: int = 0
    consumed_bytes: int = 0


def _copy_scope_directory_descriptor(
    descriptor: int,
    destination: Path,
    *,
    relative: PurePosixPath,
    declared_path: str,
    root_device: int,
    budget: _ScopeSnapshotBudget,
    depth: int,
    directory_flags: int,
) -> None:
    if depth > MAX_FILESYSTEM_SCOPE_DEPTH:
        raise ExtensionPolicyError("extension_scope_depth_limit_exceeded")
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                budget.remaining_entries -= 1
                budget.consumed_entries += 1
                if budget.remaining_entries < 0:
                    raise ExtensionPolicyError(
                        "extension_scope_entry_limit_exceeded"
                    )
                metadata = entry.stat(follow_symlinks=False)
                entries.append((entry.name, metadata))
    except ExtensionPolicyError:
        raise
    except OSError as exc:
        raise ExtensionPolicyError("extension_scope_scan_failed") from exc

    for name, metadata in sorted(entries, key=lambda item: item[0]):
        child_relative = (
            PurePosixPath(name)
            if relative == PurePosixPath(".")
            else relative / name
        )
        display_path = _scope_display_path(declared_path, child_relative)
        if name.casefold() in _CONTROL_TREE_NAMES:
            raise ExtensionPolicyError(
                f"extension_scope_control_tree_rejected:{display_path}"
            )
        if metadata.st_dev != root_device:
            raise ExtensionPolicyError(
                f"extension_scope_filesystem_crossing_rejected:{display_path}"
            )
        if stat.S_ISLNK(metadata.st_mode):
            raise ExtensionPolicyError(
                f"extension_scope_path_symlink_rejected:{display_path}"
            )
        target = destination / name
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child_descriptor = os.open(
                    name, directory_flags, dir_fd=descriptor
                )
            except OSError as exc:
                raise ExtensionPolicyError(
                    f"extension_scope_changed_during_snapshot:{display_path}"
                ) from exc
            try:
                opened = os.fstat(child_descriptor)
                if not _same_stat_identity(metadata, opened):
                    raise ExtensionPolicyError(
                        f"extension_scope_changed_during_snapshot:{display_path}"
                    )
                target.mkdir(mode=0o700)
                _copy_scope_directory_descriptor(
                    child_descriptor,
                    target,
                    relative=child_relative,
                    declared_path=declared_path,
                    root_device=root_device,
                    budget=budget,
                    depth=depth + 1,
                    directory_flags=directory_flags,
                )
                if not _same_stat_snapshot(metadata, os.fstat(child_descriptor)):
                    raise ExtensionPolicyError(
                        f"extension_scope_changed_during_snapshot:{display_path}"
                    )
                target.chmod(0o500)
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ExtensionPolicyError(
                f"extension_scope_nonregular_rejected:{display_path}"
            )
        _validate_scope_file_metadata(
            metadata,
            display_path=display_path,
            remaining_bytes=budget.remaining_bytes,
        )
        _copy_regular_file_at(
            descriptor,
            name,
            target,
            metadata=metadata,
            relative=display_path,
        )
        budget.remaining_bytes -= metadata.st_size
        budget.consumed_bytes += metadata.st_size


def _copy_regular_file_at(
    parent_descriptor: int,
    name: str,
    target: Path,
    *,
    metadata: os.stat_result,
    relative: str,
) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        _validate_open_extension_file(
            os.fstat(descriptor), metadata=metadata, relative=relative
        )
        with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
            with target.open("xb") as target_handle:
                while chunk := source_handle.read(64 * 1024):
                    target_handle.write(chunk)
        _validate_open_extension_file(
            os.fstat(descriptor), metadata=metadata, relative=relative
        )
    finally:
        os.close(descriptor)
    target.chmod(0o400)


def _copy_regular_descriptor(
    descriptor: int,
    target: Path,
    *,
    metadata: os.stat_result,
    relative: str,
) -> None:
    _validate_open_extension_file(
        os.fstat(descriptor), metadata=metadata, relative=relative
    )
    with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
        with target.open("xb") as target_handle:
            while chunk := source_handle.read(64 * 1024):
                target_handle.write(chunk)
    _validate_open_extension_file(
        os.fstat(descriptor), metadata=metadata, relative=relative
    )
    target.chmod(0o400)


def _validate_open_extension_file(
    opened: os.stat_result,
    *,
    metadata: os.stat_result,
    relative: str,
) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise ExtensionPolicyError(f"extension_tree_nonregular_rejected:{relative}")
    if opened.st_nlink != 1:
        raise ExtensionPolicyError(f"extension_tree_hardlink_rejected:{relative}")
    identity = (opened.st_dev, opened.st_ino)
    expected_identity = (metadata.st_dev, metadata.st_ino)
    stable = (
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
        stat.S_IMODE(opened.st_mode),
    )
    expected_stable = (
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )
    if identity != expected_identity or stable != expected_stable:
        raise ExtensionPolicyError(f"extension_tree_changed_during_read:{relative}")


def _same_stat_identity(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (
        stat.S_IFMT(expected.st_mode) == stat.S_IFMT(actual.st_mode)
        and expected.st_dev == actual.st_dev
        and expected.st_ino == actual.st_ino
    )


def _same_stat_snapshot(expected: os.stat_result, actual: os.stat_result) -> bool:
    return _same_stat_identity(expected, actual) and (
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
        stat.S_IMODE(expected.st_mode),
        expected.st_nlink,
    ) == (
        actual.st_size,
        actual.st_mtime_ns,
        actual.st_ctime_ns,
        stat.S_IMODE(actual.st_mode),
        actual.st_nlink,
    )
