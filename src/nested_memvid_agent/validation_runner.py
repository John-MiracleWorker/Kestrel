from __future__ import annotations

import hashlib
import os
import stat
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

from .extension_policy import extension_tree_digest, parse_extension_scopes
from .extension_runner import (
    ContainerExecutionRequest,
    ContainerExecutionResult,
    OCIContainerRunner,
)
from .repair_integrity import (
    hardened_readonly_git_command,
    hardened_readonly_git_environment,
    repair_snapshot,
    require_git_root,
)
from .security_boundary import assert_path_not_sensitive

_MAX_VALIDATION_FILES = 512
_MAX_VALIDATION_BYTES = 32 * 1024 * 1024
_MAX_VALIDATION_FILE_BYTES = 8 * 1024 * 1024
_MAX_VALIDATION_PATH_BYTES = 2 * 1024 * 1024


class ValidationIsolationError(RuntimeError):
    """Raised when a trusted validation container cannot be proven safe."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ValidationContainerRunner(Protocol):
    def run(self, request: ContainerExecutionRequest) -> ContainerExecutionResult: ...


@dataclass(frozen=True)
class IsolatedValidationResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    isolation: dict[str, Any]


def run_isolated_validation(
    *,
    workspace: Path,
    image: str | None,
    command: list[str],
    timeout_seconds: float,
    expected_repair_snapshot: dict[str, Any] | None = None,
    runner: ValidationContainerRunner | None = None,
) -> IsolatedValidationResult:
    """Run validation against a private exact Git-worktree snapshot.

    There is deliberately no host-process fallback. The container receives no
    live workspace mount, Git metadata, Kestrel state, secret broker, repair
    receipts, host home directory, network, or writable source tree.
    """

    selected_image = str(image or "").strip()
    if not selected_image:
        raise ValidationIsolationError(
            "validation_container_required",
            "Trusted validation requires NEST_AGENT_VALIDATION_CONTAINER_IMAGE "
            "set to a preloaded digest-pinned OCI image; host fallback is disabled.",
        )
    if not command:
        raise ValidationIsolationError(
            "validation_command_required", "Trusted validation requires a command."
        )

    root = require_git_root(workspace)
    before = repair_snapshot(root)
    if expected_repair_snapshot is not None:
        _require_same_candidate(expected_repair_snapshot, before)

    with tempfile.TemporaryDirectory(prefix="kestrel-validation-source-") as temp_name:
        private_root = Path(temp_name)
        private_root.chmod(0o700)
        source = private_root / "candidate"
        source.mkdir(mode=0o700)
        source_digest = _copy_git_candidate(root, source)
        source.chmod(0o500)

        after_copy = repair_snapshot(root)
        _require_same_candidate(before, after_copy)
        result = (runner or OCIContainerRunner()).run(
            ContainerExecutionRequest(
                extension_id=f"validation-{uuid4().hex}",
                source_dir=source,
                expected_tree_digest=source_digest,
                workspace=root,
                scopes=parse_extension_scopes({}),
                image=selected_image,
                command=tuple(_container_command(command)),
                stdin="",
                timeout_seconds=timeout_seconds,
            )
        )

    after_run = repair_snapshot(root)
    _require_same_candidate(before, after_run)
    _require_completed_container(result)
    assert result.returncode is not None
    isolation = {
        "schema_version": 1,
        "mode": "oci_snapshot_v1",
        "image": selected_image,
        "network": "none",
        "workspace_mount": "private_read_only_snapshot",
        "host_fallback": False,
        "source_tree_digest": result.tree_digest,
        "repair_diff_digest": before["diff_digest"],
        "repair_head_sha": before["head_sha"],
        "repair_branch": before["branch"],
    }
    return IsolatedValidationResult(
        args=tuple(command),
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        isolation=isolation,
    )


def _copy_git_candidate(root: Path, destination: Path) -> str:
    paths = _git_candidate_paths(root)
    if len(paths) > _MAX_VALIDATION_FILES:
        raise ValidationIsolationError(
            "validation_snapshot_file_limit",
            f"Validation candidate exceeds {_MAX_VALIDATION_FILES} files.",
        )
    path_bytes = sum(len(os.fsencode(path)) for path in paths)
    if path_bytes > _MAX_VALIDATION_PATH_BYTES:
        raise ValidationIsolationError(
            "validation_snapshot_path_limit",
            "Validation candidate path manifest is too large.",
        )
    total_bytes = 0
    for relative in paths:
        source = root.joinpath(*PurePosixPath(relative).parts)
        if not source.exists() and not source.is_symlink():
            # A tracked deletion is intentionally absent from the candidate.
            continue
        assert_path_not_sensitive(root, source, requested_path=relative)
        _reject_control_path(relative)
        _reject_symlink_parents(root, relative)
        metadata = source.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationIsolationError(
                "validation_snapshot_nonregular",
                f"Validation candidate path is not a regular file: {relative}",
            )
        if metadata.st_nlink != 1:
            raise ValidationIsolationError(
                "validation_snapshot_hardlink",
                f"Validation candidate path has multiple hard links: {relative}",
            )
        if metadata.st_size > _MAX_VALIDATION_FILE_BYTES:
            raise ValidationIsolationError(
                "validation_snapshot_file_size",
                f"Validation candidate file is too large: {relative}",
            )
        total_bytes += metadata.st_size
        if total_bytes > _MAX_VALIDATION_BYTES:
            raise ValidationIsolationError(
                "validation_snapshot_size_limit",
                f"Validation candidate exceeds {_MAX_VALIDATION_BYTES} bytes.",
            )
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _copy_regular_file(source, target, metadata=metadata, relative=relative)

    digest = extension_tree_digest(destination)
    if not digest.startswith("sha256:"):
        raise ValidationIsolationError(
            "validation_snapshot_digest_invalid",
            "Validation snapshot digest could not be established.",
        )
    return digest


def _git_candidate_paths(root: Path) -> list[str]:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        hardened_readonly_git_command(
            [
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ],
            workspace=root,
        ),
        cwd=root,
        env=hardened_readonly_git_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or len(completed.stdout) > _MAX_VALIDATION_PATH_BYTES:
        raise ValidationIsolationError(
            "validation_snapshot_manifest_failed",
            "Unable to build a bounded validation candidate manifest.",
        )
    raw_paths = completed.stdout.split(b"\0")
    paths: set[str] = set()
    for raw_path in raw_paths:
        if not raw_path:
            continue
        path = os.fsdecode(raw_path)
        _validate_relative_path(path)
        if _is_control_path(path):
            # Kestrel creates `.nest` receipts in otherwise clean repair
            # worktrees. Control state is never candidate source and is never
            # copied into the untrusted validation boundary.
            continue
        paths.add(path)
    return sorted(paths, key=lambda item: os.fsencode(item))


def _copy_regular_file(
    source: Path,
    target: Path,
    *,
    metadata: os.stat_result,
    relative: str,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        if not _same_file(metadata, opened):
            raise ValidationIsolationError(
                "validation_snapshot_changed",
                f"Validation candidate changed while snapshotting: {relative}",
            )
        hasher = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as source_handle, target.open(
            "xb"
        ) as target_handle:
            while chunk := source_handle.read(64 * 1024):
                hasher.update(chunk)
                target_handle.write(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        if not _same_file(opened, os.fstat(descriptor)):
            raise ValidationIsolationError(
                "validation_snapshot_changed",
                f"Validation candidate changed while snapshotting: {relative}",
            )
        # Hashing here is intentional even though the outer tree digest hashes
        # again: it forces a full descriptor read before metadata revalidation.
        if len(hasher.digest()) != hashlib.sha256().digest_size:
            raise AssertionError("sha256 digest size invariant failed")
    finally:
        os.close(descriptor)
    target.chmod(0o500 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o400)


def _validate_relative_path(path: str) -> None:
    pure = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValidationIsolationError(
            "validation_snapshot_path_invalid",
            "Validation candidate contains a non-portable path.",
        )


def _reject_control_path(path: str) -> None:
    if _is_control_path(path):
        raise ValidationIsolationError(
            "validation_snapshot_control_path",
            "Validation candidate includes a protected control path.",
        )


def _is_control_path(path: str) -> bool:
    return any(part.casefold() in {".git", ".nest"} for part in PurePosixPath(path).parts)


def _reject_symlink_parents(root: Path, relative: str) -> None:
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current /= part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationIsolationError(
                "validation_snapshot_parent_invalid",
                f"Validation candidate parent is not a real directory: {relative}",
            )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _container_command(command: list[str]) -> list[str]:
    normalized = list(command)
    if normalized:
        executable = Path(normalized[0]).name.casefold()
        if normalized[0] == sys.executable or executable.startswith("python"):
            normalized[0] = "python"
    return normalized


def _require_same_candidate(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    drift = [
        field
        for field in ("branch", "head_sha", "diff_digest")
        if expected.get(field) != actual.get(field)
    ]
    if drift:
        raise ValidationIsolationError(
            "validation_candidate_changed",
            "Validation candidate changed during isolated execution: " + ", ".join(drift),
        )


def _require_completed_container(result: ContainerExecutionResult) -> None:
    if result.returncode is not None and result.error in {None, "container_nonzero_exit"}:
        return
    raise ValidationIsolationError(
        str(result.error or "validation_container_failed"),
        str(result.content or "Validation container did not complete safely."),
    )
