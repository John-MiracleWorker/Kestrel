from __future__ import annotations

import os
import signal
import stat
from collections.abc import Callable
from typing import cast

_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def is_windows_reparse_point(metadata: os.stat_result) -> bool:
    """Return whether ``lstat`` metadata identifies a Windows reparse point."""

    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    """Detect POSIX links and Windows reparse points from ``lstat`` metadata.

    Python 3.11 has no portable ``Path.is_junction`` API. Windows exposes the
    required junction/reparse bit on ``st_file_attributes``; POSIX metadata has
    no such field and therefore keeps its existing symlink-only behavior.
    """

    return stat.S_ISLNK(metadata.st_mode) or is_windows_reparse_point(metadata)


def chmod_descriptor(descriptor: int, mode: int) -> None:
    """Apply a POSIX mode to an open descriptor or fail closed.

    Callers use this only on platforms where descriptor modes are part of the
    security model.  Looking the primitive up dynamically keeps the module
    importable on native Windows without weakening POSIX enforcement.
    """

    implementation = getattr(os, "fchmod", None)
    if not callable(implementation):
        raise OSError("descriptor mode changes are unavailable")
    cast(Callable[[int, int], None], implementation)(descriptor, mode)


def signal_process_group(group_id: int, signal_number: int) -> None:
    """Signal a POSIX process group or fail closed when unsupported."""

    implementation = getattr(os, "killpg", None)
    if not callable(implementation):
        raise OSError("process-group signalling is unavailable")
    cast(Callable[[int, int], None], implementation)(group_id, signal_number)


def required_signal(name: str) -> int:
    """Return an OS signal number, rejecting platforms that do not define it."""

    value: object = getattr(signal, name, None)
    if not isinstance(value, int):
        raise OSError(f"{name} is unavailable")
    return value
