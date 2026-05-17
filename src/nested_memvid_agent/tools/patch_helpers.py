from __future__ import annotations

from pathlib import Path

from .workspace_tools import _safe_path


def _validate_patch_paths(workspace: Path, patch_text: str) -> None:
    for line in patch_text.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw = line[4:].split("\t", maxsplit=1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        _safe_path(workspace, raw)
