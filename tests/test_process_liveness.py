from __future__ import annotations

import os
import subprocess
import sys

from nested_memvid_agent.process_liveness import process_is_alive


def test_current_process_is_alive() -> None:
    assert process_is_alive(os.getpid()) is True


def test_waited_child_process_is_not_alive() -> None:
    child = subprocess.Popen(  # noqa: S603 - deterministic local child process
        [sys.executable, "-c", "pass"]
    )

    assert child.wait(timeout=5) == 0
    assert process_is_alive(child.pid) is False
