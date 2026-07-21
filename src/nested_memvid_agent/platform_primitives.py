from __future__ import annotations

import os
import signal
from collections.abc import Callable
from typing import cast


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
