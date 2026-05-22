from __future__ import annotations

import shutil
import subprocess  # nosec B404 - tests execute the local fixture in a temp copy
import sys
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "examples" / "golden_repair_demo"


def test_golden_repair_demo_fixture_fails_then_expected_patch_passes(tmp_path: Path) -> None:
    demo = tmp_path / "golden_repair_demo"
    shutil.copytree(FIXTURE_ROOT, demo)

    failing = subprocess.run(  # nosec B603
        [sys.executable, "-m", "pytest", "-q"],
        cwd=demo,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert failing.returncode != 0
    assert "test_subtracts_numbers" in failing.stdout

    patch = subprocess.run(  # nosec B603
        ["git", "apply", "expected_fix.patch"],
        cwd=demo,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert patch.returncode == 0, patch.stderr

    passing = subprocess.run(  # nosec B603
        [sys.executable, "-m", "pytest", "-q"],
        cwd=demo,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert passing.returncode == 0, passing.stdout + passing.stderr
