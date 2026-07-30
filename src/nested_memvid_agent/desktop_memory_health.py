from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import cast

from .layers import DEFAULT_LAYER_SPECS
from .platform_primitives import is_link_or_reparse_point

_MAX_PROBE_LAYER_BYTES = 1_073_741_824
_MV2_HEADER = b"MV2\x00"
_MV2_FOOTER = b"MV2FOOT!"
_MV2_FOOTER_WINDOW_BYTES = 256
_RECEIPT_SCHEMA = "kestrel.desktop.memvid-preflight-receipt.v1"
_CANONICAL_FILENAMES = tuple(spec.mv2_file for spec in DEFAULT_LAYER_SPECS.values())

SDKMetadataProbe = Callable[[str], bool]


@dataclass(frozen=True)
class DesktopMemvidLayerReceipt:
    """Identity of one layer after a successful real SDK close."""

    filename: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class DesktopMemvidPreflightReceipt:
    """In-memory evidence that the current six files were opened successfully."""

    memory_dir: str = field(repr=False)
    layers: tuple[DesktopMemvidLayerReceipt, ...]
    launch_nonce_digest: str | None = field(default=None, repr=False)
    resource_manifest_digest: str | None = None
    schema: str = _RECEIPT_SCHEMA

    def bind(
        self,
        *,
        launch_nonce_digest: str,
        resource_manifest_digest: str,
    ) -> DesktopMemvidPreflightReceipt:
        """Bind startup evidence to one verified Desktop launch generation."""

        nonce = _sha256_digest(
            launch_nonce_digest,
            prefix="",
        )
        manifest = _sha256_digest(
            resource_manifest_digest,
            prefix="sha256:",
        )
        return replace(
            self,
            launch_nonce_digest=nonce,
            resource_manifest_digest=manifest,
        )


def capture_desktop_memvid_preflight_receipt(
    memory_dir: Path,
    *,
    max_layer_bytes: int = _MAX_PROBE_LAYER_BYTES,
) -> DesktopMemvidPreflightReceipt:
    """Capture metadata only after the caller has closed all six SDK handles."""

    _validate_max_layer_bytes(max_layer_bytes)
    root = Path(memory_dir)
    if not root.is_absolute():
        raise ValueError("desktop_memory_directory_must_be_absolute")

    root_descriptor: int | None = None
    root_metadata: os.stat_result | None = None
    opened: list[int] = []
    layers: list[DesktopMemvidLayerReceipt] = []
    try:
        root_descriptor, root_metadata = _open_verified_directory(root)
        for filename in _CANONICAL_FILENAMES:
            descriptor, metadata = _open_verified_file(
                root / filename,
                max_bytes=max_layer_bytes,
            )
            opened.append(descriptor)
            layers.append(_layer_receipt(filename, metadata))
        if root_metadata is None or not _same_open_directory(
            root,
            root_descriptor,
            root_metadata,
        ):
            raise ValueError("desktop_memory_directory_changed")
        for filename, descriptor, expected in zip(
            _CANONICAL_FILENAMES,
            opened,
            layers,
            strict=True,
        ):
            if not _same_open_file(
                root / filename,
                descriptor,
                expected,
                max_bytes=max_layer_bytes,
            ):
                raise ValueError("desktop_memory_layer_changed")
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)

    return DesktopMemvidPreflightReceipt(
        memory_dir=str(root.resolve(strict=True)),
        layers=tuple(layers),
    )


def inspect_desktop_memvid_readiness(
    memory_dir: Path,
    *,
    receipt: DesktopMemvidPreflightReceipt | None = None,
    launch_nonce_digest: str | None = None,
    resource_manifest_digest: str | None = None,
    sdk_metadata_probe: SDKMetadataProbe | None = None,
    max_layer_bytes: int = _MAX_PROBE_LAYER_BYTES,
) -> bool:
    """Verify current startup evidence with bounded, non-mutating operations.

    A normal SDK open is intentionally absent: the pinned SDK updates metadata
    even in read-only mode. Instead, readiness stays true only while the exact
    files last opened successfully by the runtime retain their identities and
    timestamps. The live probe reads fixed header/footer windows and uses only
    the SDK's finite single-file and lock-status metadata operations.
    """

    try:
        _validate_max_layer_bytes(max_layer_bytes)
        root = Path(memory_dir)
        if not root.is_absolute():
            return False
        if not _receipt_matches_generation(
            receipt,
            root=root,
            launch_nonce_digest=launch_nonce_digest,
            resource_manifest_digest=resource_manifest_digest,
        ):
            return False
        if receipt is None:
            return False

        root_descriptor: int | None = None
        root_metadata: os.stat_result | None = None
        opened: list[tuple[Path, int, os.stat_result, DesktopMemvidLayerReceipt]] = []
        ready = True
        try:
            root_descriptor, root_metadata = _open_verified_directory(root)
            for expected in receipt.layers:
                path = root / expected.filename
                descriptor, metadata = _open_verified_file(
                    path,
                    max_bytes=max_layer_bytes,
                )
                opened.append((path, descriptor, metadata, expected))
                if not _metadata_matches_receipt(
                    metadata, expected
                ) or not _has_bounded_mv2_structure(
                    descriptor,
                    size=metadata.st_size,
                ):
                    ready = False
                    break

            probe = sdk_metadata_probe or _sdk_metadata_probe
            if ready:
                for path, _descriptor, _metadata, _expected in opened:
                    if probe(str(path)) is not True:
                        ready = False
                        break

            if ready:
                for path, descriptor, _metadata, expected in opened:
                    if not _same_open_file(
                        path,
                        descriptor,
                        expected,
                        max_bytes=max_layer_bytes,
                    ):
                        ready = False
                        break
            if root_metadata is None or not _same_open_directory(
                root,
                root_descriptor,
                root_metadata,
            ):
                ready = False
        finally:
            for _path, descriptor, _metadata, _expected in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    ready = False
            if root_descriptor is not None:
                try:
                    os.close(root_descriptor)
                except OSError:
                    ready = False
        return ready
    except Exception:
        return False


def _sdk_metadata_probe(filename: str) -> bool:
    module = import_module("memvid_sdk")
    verify_single_file = getattr(module, "verify_single_file", None)
    lock_who = getattr(module, "lock_who", None)
    if not callable(verify_single_file) or not callable(lock_who):
        raise RuntimeError("memvid_sdk_metadata_probe_unavailable")
    verify_single_file(filename)
    lock_status = lock_who(filename)
    return isinstance(lock_status, Mapping) and lock_status.get("locked") is False


def _receipt_matches_generation(
    receipt: DesktopMemvidPreflightReceipt | None,
    *,
    root: Path,
    launch_nonce_digest: str | None,
    resource_manifest_digest: str | None,
) -> bool:
    if (
        type(receipt) is not DesktopMemvidPreflightReceipt
        or receipt.schema != _RECEIPT_SCHEMA
        or tuple(layer.filename for layer in receipt.layers) != _CANONICAL_FILENAMES
        or len(receipt.layers) != len(_CANONICAL_FILENAMES)
    ):
        return False
    try:
        normalized_root = str(root.resolve(strict=True))
        nonce = _sha256_digest(
            launch_nonce_digest,
            prefix="",
        )
        manifest = _sha256_digest(
            resource_manifest_digest,
            prefix="sha256:",
        )
    except (OSError, TypeError, ValueError):
        return False
    return (
        secrets.compare_digest(receipt.memory_dir, normalized_root)
        and receipt.launch_nonce_digest is not None
        and secrets.compare_digest(
            receipt.launch_nonce_digest,
            nonce,
        )
        and receipt.resource_manifest_digest is not None
        and secrets.compare_digest(
            receipt.resource_manifest_digest,
            manifest,
        )
    )


def _layer_receipt(
    filename: str,
    metadata: os.stat_result,
) -> DesktopMemvidLayerReceipt:
    return DesktopMemvidLayerReceipt(
        filename=filename,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _metadata_matches_receipt(
    metadata: os.stat_result,
    receipt: DesktopMemvidLayerReceipt,
) -> bool:
    return (
        metadata.st_dev == receipt.device
        and metadata.st_ino == receipt.inode
        and metadata.st_size == receipt.size
        and metadata.st_mtime_ns == receipt.mtime_ns
        and metadata.st_ctime_ns == receipt.ctime_ns
    )


def _has_bounded_mv2_structure(
    descriptor: int,
    *,
    size: int,
) -> bool:
    if size < len(_MV2_HEADER) + len(_MV2_FOOTER):
        return False
    header = _read_descriptor_at(
        descriptor,
        size=len(_MV2_HEADER),
        offset=0,
    )
    tail_size = min(size, _MV2_FOOTER_WINDOW_BYTES)
    tail = _read_descriptor_at(
        descriptor,
        size=tail_size,
        offset=size - tail_size,
    )
    return header == _MV2_HEADER and _MV2_FOOTER in tail


def _read_descriptor_at(
    descriptor: int,
    *,
    size: int,
    offset: int,
) -> bytes:
    pread = getattr(os, "pread", None)
    if callable(pread):
        return cast(bytes, pread(descriptor, size, offset))
    os.lseek(descriptor, offset, os.SEEK_SET)
    return os.read(descriptor, size)


def _open_verified_directory(
    path: Path,
) -> tuple[int | None, os.stat_result]:
    before = path.lstat()
    _validate_directory_metadata(before)
    if os.name == "nt":
        return None, before
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        if not _same_open_directory(path, descriptor, before):
            raise ValueError("desktop_memory_directory_changed")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, before


def _open_verified_file(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[int, os.stat_result]:
    before = path.lstat()
    _validate_file_metadata(before, max_bytes=max_bytes)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        if not _same_open_file(
            path,
            descriptor,
            before,
            max_bytes=max_bytes,
        ):
            raise ValueError("desktop_memory_layer_changed")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, before


def _same_open_directory(
    path: Path,
    descriptor: int | None,
    expected: os.stat_result,
) -> bool:
    try:
        visible = path.lstat()
        _validate_directory_metadata(visible)
        if descriptor is None:
            return (
                os.path.samestat(expected, visible)
                and visible.st_mtime_ns == expected.st_mtime_ns
                and visible.st_ctime_ns == expected.st_ctime_ns
            )
        opened = os.fstat(descriptor)
        _validate_directory_metadata(opened)
    except (OSError, ValueError):
        return False
    return (
        os.path.samestat(expected, opened)
        and os.path.samestat(opened, visible)
        and opened.st_mtime_ns == expected.st_mtime_ns
        and visible.st_mtime_ns == opened.st_mtime_ns
        and opened.st_ctime_ns == expected.st_ctime_ns
        and visible.st_ctime_ns == opened.st_ctime_ns
    )


def _same_open_file(
    path: Path,
    descriptor: int,
    expected: os.stat_result | DesktopMemvidLayerReceipt,
    *,
    max_bytes: int,
) -> bool:
    try:
        opened = os.fstat(descriptor)
        visible = path.lstat()
        _validate_file_metadata(opened, max_bytes=max_bytes)
        _validate_file_metadata(visible, max_bytes=max_bytes)
    except (OSError, ValueError):
        return False
    if isinstance(expected, DesktopMemvidLayerReceipt):
        expected_matches = _metadata_matches_receipt(opened, expected)
    else:
        expected_matches = os.path.samestat(expected, opened) and (
            opened.st_size == expected.st_size
            and opened.st_mtime_ns == expected.st_mtime_ns
            and opened.st_ctime_ns == expected.st_ctime_ns
        )
    return (
        expected_matches
        and os.path.samestat(opened, visible)
        and visible.st_size == opened.st_size
        and visible.st_mtime_ns == opened.st_mtime_ns
        and visible.st_ctime_ns == opened.st_ctime_ns
    )


def _validate_directory_metadata(metadata: os.stat_result) -> None:
    if is_link_or_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("desktop_memory_directory_untrusted")
    _validate_owner_and_mode(metadata, expected_mode=0o700)


def _validate_file_metadata(
    metadata: os.stat_result,
    *,
    max_bytes: int,
) -> None:
    if (
        is_link_or_reparse_point(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 0
        or metadata.st_size > max_bytes
    ):
        raise ValueError("desktop_memory_layer_untrusted")
    _validate_owner_and_mode(metadata, expected_mode=0o600)


def _validate_owner_and_mode(
    metadata: os.stat_result,
    *,
    expected_mode: int,
) -> None:
    if os.name == "nt":
        return
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid) or metadata.st_uid != geteuid():
        raise PermissionError("desktop_memory_owner_untrusted")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise PermissionError("desktop_memory_mode_untrusted")


def _validate_max_layer_bytes(max_layer_bytes: int) -> None:
    if (
        isinstance(max_layer_bytes, bool)
        or not isinstance(max_layer_bytes, int)
        or not 1 <= max_layer_bytes <= _MAX_PROBE_LAYER_BYTES
    ):
        raise ValueError("desktop_memory_layer_bound_invalid")


def _sha256_digest(
    value: str | None,
    *,
    prefix: str,
) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("invalid_sha256_digest")
    digest = value[len(prefix) :]
    if (
        len(digest) != 64
        or digest.lower() != digest
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("invalid_sha256_digest")
    return f"{prefix}{digest}"
