from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nested_memvid_agent.file_lock import lock_exclusive, lock_shared, unlock


@pytest.mark.skipif(sys.platform != "win32", reason="Windows LockFileEx semantics")
def test_windows_shared_locks_allow_one_runtime_to_open_multiple_layers(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "memory.lock"
    lock_path.write_text("0", encoding="utf-8")

    with (
        lock_path.open("r+", encoding="utf-8") as first,
        lock_path.open("r+", encoding="utf-8") as second,
        lock_path.open("r+", encoding="utf-8") as contender,
    ):
        lock_shared(first, blocking=False)
        lock_shared(second, blocking=False)
        try:
            with pytest.raises(OSError):
                lock_exclusive(contender, blocking=False)
        finally:
            unlock(second)
            unlock(first)

        lock_exclusive(contender, blocking=False)
        unlock(contender)
