"""Project policy helpers for Adaptive Flock real-task qualification imports.

This module complements :mod:`nested_memvid_agent.projects` with the closed
vocabularies and authority digests required when owner-selected, completed
real project tasks are imported into the Flock qualification corpus.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from .projects import ProjectRecord
from .routing.qualification_digest import canonical_digest

__all__ = [
    "ACTIONABLE_RISK_LEVELS",
    "REPEATABILITY_CLASSES",
    "TRUSTED_RECEIPT_TYPES",
    "RepeatabilityClass",
    "privacy_exposure_approved",
    "project_authority_digest",
    "project_authority_payload",
    "validate_repeatability",
]

RepeatabilityClass = Literal["read_only", "isolated_worktree", "qualified_containment"]

REPEATABILITY_CLASSES: tuple[str, ...] = (
    "read_only",
    "isolated_worktree",
    "qualified_containment",
)

#: Only low/medium risk tasks may become actionable qualification evidence.
ACTIONABLE_RISK_LEVELS: tuple[str, ...] = ("low", "medium")

#: Authenticated receipt types accepted as trusted acceptance evidence.
TRUSTED_RECEIPT_TYPES: tuple[str, ...] = ("review", "test", "validation")


def validate_repeatability(value: str) -> str:
    """Return *value* when it is a registered repeatability classification."""

    if value not in REPEATABILITY_CLASSES:
        allowed = ", ".join(REPEATABILITY_CLASSES)
        raise ValueError(f"repeatability must be one of: {allowed}")
    return value


def project_authority_payload(project: ProjectRecord) -> dict[str, object]:
    """Canonical projection of the project authority an import is bound to."""

    return {
        "schema": "kestrel.flock.project_authority.v1",
        "project_id": project.project_id,
        "repository_path": project.repository_path,
        "privacy_class": project.privacy_class,
        "allowed_paths": list(project.allowed_paths),
        "capability_ceiling": list(project.capability_ceiling),
        "revision": project.revision,
    }


def project_authority_digest(project: ProjectRecord) -> str:
    """SHA-256 digest of the canonical project authority projection."""

    return canonical_digest(project_authority_payload(project))


def privacy_exposure_approved(
    project: ProjectRecord,
    approved_privacy_classes: Iterable[str],
) -> bool:
    """True only when the owner explicitly approved the project's privacy class."""

    return project.privacy_class in frozenset(approved_privacy_classes)
