from __future__ import annotations

import shlex
from pathlib import Path

from .base import ToolContext
from .workspace_tools import _assert_workspace_path_allowed, _safe_path


def _validate_patch_paths(
    workspace: Path,
    patch_text: str,
    *,
    context: ToolContext | None = None,
) -> None:
    for line in patch_text.splitlines():
        raw_paths: list[str] = []
        if line.startswith("diff --git "):
            try:
                raw_paths = shlex.split(line.removeprefix("diff --git "))
            except ValueError as exc:
                raise ValueError("Patch contains an invalid Git path header.") from exc
            if len(raw_paths) != 2:
                raise ValueError("Patch contains an invalid Git path header.")
        elif line.startswith(("--- ", "+++ ")):
            raw_paths = [line[4:].split("\t", maxsplit=1)[0].strip()]
        else:
            prefix = next(
                (
                    candidate
                    for candidate in ("rename from ", "rename to ", "copy from ", "copy to ")
                    if line.startswith(candidate)
                ),
                None,
            )
            if prefix is not None:
                raw_paths = [line.removeprefix(prefix).strip()]
        for raw in raw_paths:
            _validate_patch_path(workspace, raw, context=context)


def _validate_patch_path(
    workspace: Path,
    raw: str,
    *,
    context: ToolContext | None,
) -> None:
    if not raw:
        raise ValueError("Patch contains an empty path header.")
    if raw == "/dev/null":
        return
    if "\\" in raw or "\x00" in raw or raw.startswith(("\"", "'")):
        # Git's C-style quoted paths can encode protected characters with
        # octal escapes. Reject ambiguous encodings instead of validating a
        # lexical spelling that `git apply` will later decode differently.
        raise ValueError("Patch contains an encoded or ambiguous path header.")

    interpreted_paths = [raw[2:]] if raw.startswith(("a/", "b/")) else [raw]
    if not raw.startswith(("a/", "b/")) and "/" in raw:
        # `git apply` normally removes one leading path component. Validate
        # that interpretation too so x/config/vault cannot bypass a check that
        # only considered workspace/x/config/vault.
        interpreted_paths.append(raw.split("/", maxsplit=1)[1])
    for interpreted in dict.fromkeys(interpreted_paths):
        if not interpreted:
            raise ValueError("Patch contains an empty path header.")
        candidate = _safe_path(workspace, interpreted)
        if context is not None:
            _assert_workspace_path_allowed(
                context,
                candidate,
                requested_path=interpreted,
            )
