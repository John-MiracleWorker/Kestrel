#!/usr/bin/env python3
"""Stage the exact generated web workbench into the Python release package."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web" / "dist"
TARGET = ROOT / "src" / "nested_memvid_agent" / "web_dist"
REQUIRED = ("index.html", "THIRD_PARTY_NOTICES.txt")


def main() -> int:
    missing = [name for name in REQUIRED if not (SOURCE / name).is_file()]
    if missing:
        raise SystemExit(
            "generated web release is incomplete; run `npm run build --prefix web`: "
            + ", ".join(missing)
        )
    symlinks = sorted(path.relative_to(SOURCE) for path in SOURCE.rglob("*") if path.is_symlink())
    if symlinks:
        raise SystemExit(f"generated web release contains symlinks: {symlinks}")

    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(SOURCE, TARGET)
    print(f"Staged web release: {SOURCE.relative_to(ROOT)} -> {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
