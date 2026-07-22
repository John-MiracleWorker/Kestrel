from __future__ import annotations

import errno
import sys
from pathlib import Path

import pytest

import nested_memvid_agent.file_lock as file_lock_module
from nested_memvid_agent.file_lock import lock_exclusive, lock_shared, unlock


@pytest.mark.parametrize("winerror", [32, 33])
def test_windows_nonblocking_lock_contention_has_portable_exception(
    winerror: int,
) -> None:
    error = file_lock_module._windows_lock_error(
        winerror,
        blocking=False,
        message="injected Windows lock contention",
    )

    assert isinstance(error, BlockingIOError)
    assert error.errno == errno.EAGAIN


@pytest.mark.skipif(sys.platform != "win32", reason="Windows LockFileEx semantics")
def test_windows_empty_file_preserves_shared_and_exclusive_lock_semantics(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "memory.lock"
    lock_path.touch()

    with (
        lock_path.open("r+", encoding="utf-8") as first,
        lock_path.open("r+", encoding="utf-8") as second,
        lock_path.open("r+", encoding="utf-8") as contender,
    ):
        lock_shared(first, blocking=False)
        lock_shared(second, blocking=False)
        try:
            with pytest.raises(BlockingIOError):
                lock_exclusive(contender, blocking=False)
        finally:
            unlock(second)
            unlock(first)

        lock_exclusive(contender, blocking=False)
        unlock(contender)

    assert lock_path.read_bytes() == b""
