"""Isolated attempt workspaces for Flock qualification (Adaptive Flock Task 8).

Candidate code executes only in a qualified containment path or an isolated
worktree allowed by the corpus item. Staging is fail-closed: when the
required containment cannot be provided, the attempt is blocked with
``containment_required`` before any routing lease is persisted or any
provider contact happens. Staged workspaces are never deleted by the
executor; they are left in place for receipt-bound cleanup/review.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .qualification_digest import canonical_digest, canonical_json

__all__ = [
    "CONTAINMENT_MODES",
    "ContainmentMode",
    "AttemptWorkspace",
    "QualificationAttemptBlocked",
    "QualificationWorkspace",
]

ContainmentMode = Literal["read_only", "isolated_worktree", "qualified_containment"]

CONTAINMENT_MODES: tuple[str, ...] = (
    "read_only",
    "isolated_worktree",
    "qualified_containment",
)

_LEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RECEIPT_NAME = "attempt.workspace.json"


class QualificationAttemptBlocked(RuntimeError):
    """A qualification attempt was blocked fail-closed before provider contact."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(reason if not detail else f"{reason}: {detail}")


@dataclass
class AttemptWorkspace:
    """One staged attempt workspace bound to its lease and tree digest."""

    workspace_id: str
    lease_id: str
    containment: ContainmentMode
    tree_digest: str
    root: str | None
    receipt_ref: str
    state: str = "staged"


class QualificationWorkspace:
    """Stage isolated, digest-bound attempt workspaces.

    ``containment_available`` models the host's ability to provide an
    isolated worktree / qualified containment path. When it is unavailable,
    only ``read_only`` attempts may be staged; any containment-requiring
    attempt is blocked instead of falling back to the host.
    """

    def __init__(
        self,
        base_dir: str | Path,
        *,
        containment_available: bool = True,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._containment_available = bool(containment_available)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def containment_available(self) -> bool:
        return self._containment_available

    def stage(
        self,
        *,
        lease_id: str,
        containment: ContainmentMode,
        tree_digest: str,
    ) -> AttemptWorkspace:
        """Stage the containment path required by one attempt lease."""

        if not isinstance(lease_id, str) or _LEASE_ID_RE.fullmatch(lease_id) is None:
            raise ValueError("lease_id must be a non-empty path-safe identifier")
        if containment not in CONTAINMENT_MODES:
            raise ValueError(f"containment must be one of {', '.join(CONTAINMENT_MODES)}")
        receipt_ref = "workspace:" + canonical_digest(
            {
                "lease_id": lease_id,
                "containment": containment,
                "tree_digest": tree_digest,
            }
        )
        if containment == "read_only":
            # Read-only attempts stage no mutable tree at all.
            return AttemptWorkspace(
                workspace_id=f"ws-{lease_id}",
                lease_id=lease_id,
                containment=containment,
                tree_digest=tree_digest,
                root=None,
                receipt_ref=receipt_ref,
            )
        if not self._containment_available:
            raise QualificationAttemptBlocked(
                "containment_required",
                f"containment mode {containment!r} is unavailable; "
                "candidate code never falls back to the host",
            )
        root = self._base_dir / lease_id
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise QualificationAttemptBlocked(
                "workspace_conflict",
                f"attempt workspace already exists for lease {lease_id!r}",
            ) from exc
        except OSError as exc:
            raise QualificationAttemptBlocked(
                "containment_required",
                f"attempt workspace cannot be staged: {exc}",
            ) from exc
        receipt = {
            "lease_id": lease_id,
            "containment": containment,
            "tree_digest": tree_digest,
            "receipt_ref": receipt_ref,
        }
        (root / _RECEIPT_NAME).write_text(
            json.dumps(json.loads(canonical_json(receipt)), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return AttemptWorkspace(
            workspace_id=f"ws-{lease_id}",
            lease_id=lease_id,
            containment=containment,
            tree_digest=tree_digest,
            root=str(root),
            receipt_ref=receipt_ref,
        )

    def finalize(self, workspace: AttemptWorkspace) -> AttemptWorkspace:
        """Mark the workspace released for receipt-bound cleanup/review.

        The workspace directory is deliberately left in place; nothing here
        deletes attempt evidence.
        """

        if not isinstance(workspace, AttemptWorkspace):
            raise ValueError("workspace must be an AttemptWorkspace")
        workspace.state = "released"
        return workspace
