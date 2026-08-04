"""Import owner-selected, project-isolated real task evidence into the corpus.

Only repeatable, owner-selected tasks from completed runs of the selected
project may enter the qualification corpus.  Every imported item is an
immutable :class:`CorpusItem` whose contract and acceptance plan are bound to
the exact project authority, repository path, and validated tree digest.

Safety invariants:

- a task whose run is not bound to the selected project is rejected;
- high-risk tasks, and tasks without authenticated test/review/validation
  receipts bound to the exact task/run/project/tree, stay diagnostic-only;
- no registered secret value may appear in the prompt, contract, or artifacts;
- each target privacy class needs explicit owner approval;
- importing never mutates the original runs or tasks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

from ..project_policy import (
    ACTIONABLE_RISK_LEVELS,
    REPEATABILITY_CLASSES,
    TRUSTED_RECEIPT_TYPES,
    privacy_exposure_approved,
    project_authority_digest,
    validate_repeatability,
)
from ..projects import ProjectRecord
from ..security_boundary import redact_text
from ..state_store import AgentStateStore, RunRecord, TaskNodeRecord
from .qualification_digest import canonical_digest, canonical_json
from .qualification_models import CorpusItem, RiskLevel

if TYPE_CHECKING:
    from ..control_plane_integrity import ControlPlaneIntegrity

__all__ = [
    "REPEATABILITY_CLASSES",
    "RealTaskCorpusImporter",
]

_RISK_LEVELS = ("low", "medium", "high", "critical")
_TREE_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CONTRACT_SCHEMA = "kestrel.flock.real_task_contract.v1"
_ACCEPTANCE_PLAN_SCHEMA = "kestrel.flock.real_task_acceptance_plan.v1"


class RealTaskCorpusImporter:
    """Import selected completed real tasks as immutable corpus items."""

    def __init__(
        self,
        state: AgentStateStore,
        *,
        integrity: ControlPlaneIntegrity | None = None,
        approved_privacy_classes: Iterable[str] = (),
        repeatability: str = "read_only",
    ) -> None:
        self._state = state
        self._integrity = integrity
        self._approved_privacy_classes = frozenset(approved_privacy_classes)
        self._repeatability = validate_repeatability(repeatability)

    @property
    def repeatability(self) -> str:
        return self._repeatability

    def import_tasks(
        self,
        *,
        project_id: str,
        task_ids: Iterable[str],
    ) -> tuple[CorpusItem, ...]:
        """Import the selected tasks as project-isolated corpus items."""

        project = self._selected_project(project_id)
        return tuple(self._import_task(project, str(task_id)) for task_id in task_ids)

    def _selected_project(self, project_id: str) -> ProjectRecord:
        try:
            project = self._state.get_project(project_id)
        except KeyError as exc:
            raise ValueError(f"unknown selected project: {project_id!r}") from exc
        if project.archived_at is not None:
            raise ValueError(f"selected project is archived: {project_id!r}")
        return project

    def _import_task(self, project: ProjectRecord, task_id: str) -> CorpusItem:
        try:
            task, run = self._state.get_task_run_binding(task_id)
        except KeyError as exc:
            raise ValueError(f"unknown task: {task_id!r}") from exc
        if run.project_id != project.project_id:
            raise ValueError(
                f"task {task_id!r} does not belong to the selected project {project.project_id!r}"
            )
        authority_digest = project_authority_digest(project)
        receipts = self._trusted_receipts(project, task, run)
        tree_digest = receipts[0]["tree_digest"] if receipts else None
        risk = cast(RiskLevel, task.risk if task.risk in _RISK_LEVELS else "critical")
        item_id = f"real:{project.project_id}:{task.task_id}"
        contract = {
            "schema": _CONTRACT_SCHEMA,
            "item_id": item_id,
            "project_id": project.project_id,
            "project_authority_digest": authority_digest,
            "repository_path": project.repository_path,
            "run_id": run.run_id,
            "task_id": task.task_id,
            "title": task.title,
            "goal": task.goal,
            "task_family": task.profile,
            "risk": risk,
            "capabilities": list(task.required_tools),
            "acceptance_criteria": list(task.acceptance_criteria),
            "tree_digest": tree_digest,
        }
        privacy_approved = privacy_exposure_approved(project, self._approved_privacy_classes)
        acceptance_plan = {
            "schema": _ACCEPTANCE_PLAN_SCHEMA,
            "item_id": item_id,
            "project_id": project.project_id,
            "project_authority_digest": authority_digest,
            "path_ceiling": list(project.allowed_paths),
            "privacy_class": project.privacy_class,
            "privacy_exposure_approved": privacy_approved,
            "repeatability": self._repeatability,
            "validation_receipts": list(receipts),
        }
        reasons = self._exclusion_reasons(
            project,
            task,
            risk=risk,
            privacy_approved=privacy_approved,
            receipts=receipts,
        )
        return CorpusItem(
            item_id=item_id,
            task_family=task.profile,
            risk=risk,
            capabilities=tuple(task.required_tools),
            task_contract_digest=canonical_digest(contract),
            acceptance_plan_digest=canonical_digest(acceptance_plan),
            evidence_kind="real_project",
            actionable=not reasons,
            exclusion_reasons=tuple(reasons),
        )

    def _exclusion_reasons(
        self,
        project: ProjectRecord,
        task: TaskNodeRecord,
        *,
        risk: str,
        privacy_approved: bool,
        receipts: tuple[dict[str, Any], ...],
    ) -> list[str]:
        reasons: list[str] = []
        if task.status != "completed":
            reasons.append("task_not_completed")
        if risk not in ACTIONABLE_RISK_LEVELS:
            reasons.append("risk_not_actionable")
        if not project.allowed_paths:
            reasons.append("path_ceiling_missing")
        if not privacy_approved:
            reasons.append("privacy_exposure_not_approved")
        if self._contains_secret_material(task):
            reasons.append("secret_material_present")
        if not receipts:
            reasons.append("trusted_acceptance_evidence_missing")
        return reasons

    def _contains_secret_material(self, task: TaskNodeRecord) -> bool:
        parts = [task.title, task.goal, *task.acceptance_criteria]
        if isinstance(task.result, Mapping):
            parts.append(canonical_json(dict(task.result)))
        return any(redact_text(part) != part for part in parts if part)

    def _trusted_receipts(
        self,
        project: ProjectRecord,
        task: TaskNodeRecord,
        run: RunRecord,
    ) -> tuple[dict[str, Any], ...]:
        result = task.result if isinstance(task.result, Mapping) else {}
        envelopes = result.get("validation_receipts")
        if not isinstance(envelopes, (list, tuple)):
            return ()
        trusted: list[dict[str, Any]] = []
        for envelope in envelopes:
            payload = self._verified_payload(envelope, project, task, run)
            if payload is None or not isinstance(envelope, Mapping):
                continue
            trusted.append(
                {
                    "receipt_type": payload["receipt_type"],
                    "tree_digest": payload["tree_digest"],
                    "payload_digest": envelope.get("payload_digest"),
                    "key_id": envelope.get("key_id"),
                }
            )
        return tuple(trusted)

    def _verified_payload(
        self,
        envelope: object,
        project: ProjectRecord,
        task: TaskNodeRecord,
        run: RunRecord,
    ) -> Mapping[str, Any] | None:
        if self._integrity is None or not isinstance(envelope, Mapping):
            return None
        if not self._integrity.verify(envelope):
            return None
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return None
        if payload.get("receipt_type") not in TRUSTED_RECEIPT_TYPES:
            return None
        if payload.get("verdict") != "pass":
            return None
        if payload.get("task_id") != task.task_id:
            return None
        if payload.get("run_id") != run.run_id:
            return None
        if payload.get("project_id") != project.project_id:
            return None
        if payload.get("repository_path") != project.repository_path:
            return None
        tree_digest = payload.get("tree_digest")
        if not isinstance(tree_digest, str) or _TREE_DIGEST_RE.fullmatch(tree_digest) is None:
            return None
        return payload
