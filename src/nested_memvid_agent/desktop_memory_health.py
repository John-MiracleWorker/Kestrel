from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .backends.memvid_backend import MemvidBackend
from .layers import DEFAULT_LAYER_SPECS
from .platform_primitives import is_link_or_reparse_point


class _ReadOnlyMemvidBackend(Protocol):
    def open(self) -> None: ...

    def close(self) -> object: ...


BackendFactory = Callable[..., _ReadOnlyMemvidBackend]


def inspect_desktop_memvid_readiness(
    memory_dir: Path,
    *,
    backend_factory: BackendFactory | None = None,
) -> bool:
    """Probe every canonical layer through a non-blocking read-only reopen.

    Missing, replaced, busy, corrupt, or otherwise unreadable layers fail
    closed. The probe never asks Memvid to create a container and closes every
    successfully opened handle before returning.
    """

    factory: BackendFactory = backend_factory or MemvidBackend
    opened: list[_ReadOnlyMemvidBackend] = []
    ready = True
    try:
        for layer, spec in DEFAULT_LAYER_SPECS.items():
            path = Path(memory_dir) / spec.mv2_file
            try:
                metadata = path.lstat()
            except OSError:
                ready = False
                break
            if (
                is_link_or_reparse_point(metadata)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                ready = False
                break
            backend = factory(
                path=path,
                layer=layer,
                read_only=True,
                path_lock_blocking=False,
            )
            backend.open()
            opened.append(backend)
    except Exception:
        ready = False
    finally:
        for backend in reversed(opened):
            try:
                backend.close()
            except Exception:
                ready = False
    return ready
