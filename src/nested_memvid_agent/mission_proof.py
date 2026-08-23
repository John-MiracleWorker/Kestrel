"""Server-authored mission proof projection (JOURNEY-002).

``kestrel.mission_proof.v1`` is a read-only reducer: it aggregates durable
evidence for one admitted run into one coherent projection and reports each
evidence family as ``present``, ``missing``, ``stale``, or ``mismatched``
without any UI inference. The projection is server-authored: it only ever
contains bounded receipt/handle metadata (ids, digests, counts, statuses) and
never raw memory content, tool output, or secrets.

Every family reduces over a durable source:

- ``binding``    -- the accepted launch binding persisted with the run
                   (JOURNEY-001) plus the live project revision.
- ``contract``   -- the admitted mission plan and objective digest.
- ``roles``      -- durable task-role assignments in the task graph.
- ``routing``    -- S5/S6 zero-authority shadow observation ledger.
- ``isolation``  -- the execution boundary recorded at admission and the
                   worker isolation event log.
- ``change``     -- durable change events (patch/change request).
- ``validation`` -- durable validation events (test/validation runs).
- ``review``     -- durable independent-review events and reviewer role
                   routing evidence.
- ``risks``      -- admitted mission task risk levels.
- ``approval``   -- durable approval requests bound to the run.
- ``shipping``   -- durable shipping events (commit/PR/promotion).
- ``capsule``    -- the durable run capsule completion marker.
- ``learning``   -- durable lesson/learning events bound to the run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .mission_control import _payload_digest
from .state_store import RunRecord

MISSION_PROOF_SCHEMA = "kestrel.mission_proof.v1"

EvidenceStatus = Literal["present", "missing", "stale", "mismatched"]

# Bounded event vocabularies for the durable run step log. These are the only
# event families the reducer treats as evidence for each section; anything else
# is intentionally ignored so the projection never invents authority.
_CHANGE_EVENT_TYPES = frozenset(
    {
        "patch.apply",
        "github.change_request_prepared",
        "change.created",
        "behavior.delta.recorded",
    }
)
_VALIDATION_EVENT_TYPES = frozenset(
    {
        "validation.completed",
        "browser.validation.completed",
        "validation.run",
    }
)
_REVIEW_EVENT_TYPES = frozenset(
    {
        "review.completed",
        "review.requested",
    }
)
_SHIPPING_EVENT_TYPES = frozenset(
    {
        "github.change_request_prepared",
        "commit.created",
        "ship.completed",
        "promotion.created",
        "candidate.selected",
    }
)
_LEARNING_EVENT_TYPES = frozenset(
    {
        "lesson.created",
        "lesson.recall",
        "capsule.summarize",
        "learning.signal",
    }
)
_ISOLATION_EVENT_TYPES = frozenset(
    {
        "worker.isolated",
        "repair.worktree.prepared",
    }
)
_CAPSULE_COMPLETION_MARKER = "capsule.complete.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _project_value(project: Any, key: str, default: Any = None) -> Any:
    """Read one project attribute regardless of record representation."""
    if project is None:
        return default
    if isinstance(project, Mapping):
        return project.get(key, default)
    return getattr(project, key, default)


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type", ""))
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _present(status: EvidenceStatus, detail: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "detail": detail,
        "evidence": dict(evidence),
    }


def _binding_section(
    run: RunRecord,
    *,
    project: Mapping[str, Any] | None,
) -> dict[str, Any]:
    binding = run.mission_binding
    if not binding:
        return _present(
            "missing",
            "No accepted mission launch binding was persisted with this run.",
            {},
        )
    stored_digest = str(binding.get("binding_digest", ""))
    recomputed = _payload_digest(
        {key: value for key, value in binding.items() if key != "binding_digest"}
    )
    if stored_digest != recomputed:
        return _present(
            "mismatched",
            "The persisted launch binding digest does not match its own fields; "
            "the binding was substituted after admission.",
            {
                "stored_binding_digest": stored_digest,
                "recomputed_binding_digest": recomputed,
                "project_id": binding.get("project_id"),
            },
        )
    project_revision = int(binding.get("project_revision", 0) or 0)
    live_revision = int(_project_value(project, "revision", 0) or 0)
    if live_revision and live_revision != project_revision:
        return _present(
            "stale",
            "The admitted project revision is behind the current project "
            "revision; later evidence may not describe the admitted state.",
            {
                "admitted_project_revision": project_revision,
                "current_project_revision": live_revision,
                "objective_digest": binding.get("objective_digest"),
                "plan_digest": binding.get("plan_digest"),
                "preflight_digest": binding.get("preflight_digest"),
            },
        )
    return _present(
        "present",
        "The accepted launch binding is persisted and internally consistent.",
        {
            "project_id": binding.get("project_id"),
            "project_revision": project_revision,
            "template_id": binding.get("template_id"),
            "routing_mode": binding.get("routing_mode"),
            "routing_enabled": binding.get("routing_enabled"),
            "policy_id": binding.get("policy_id"),
            "objective_digest": binding.get("objective_digest"),
            "plan_digest": binding.get("plan_digest"),
            "preflight_digest": binding.get("preflight_digest"),
            "binding_digest": stored_digest,
        },
    )


def _contract_section(
    run: RunRecord,
    *,
    mission_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    binding = run.mission_binding
    preflight = run.mission_preflight
    if not binding or not preflight:
        return _present(
            "missing",
            "No admitted mission contract is persisted for this run.",
            {},
        )
    objective_digest = str(binding.get("objective_digest", ""))
    computed_objective_digest = hashlib.sha256(
        run.message.strip().encode("utf-8")
    ).hexdigest()
    if objective_digest and computed_objective_digest != objective_digest:
        return _present(
            "mismatched",
            "The run objective digest does not match the admitted binding; the "
            "objective was substituted after admission.",
            {
                "admitted_objective_digest": objective_digest,
                "run_objective_digest": computed_objective_digest,
            },
        )
    admitted_tasks = list(preflight.get("tasks") or [])
    admitted_plan_digest = _payload_digest({"tasks": admitted_tasks})
    admitted_binding_plan_digest = str(binding.get("plan_digest", ""))
    if (
        admitted_binding_plan_digest
        and admitted_plan_digest != admitted_binding_plan_digest
    ):
        return _present(
            "mismatched",
            "The admitted preflight plan digest does not match the binding plan "
            "digest; the plan was substituted after admission.",
            {
                "admitted_plan_digest": admitted_binding_plan_digest,
                "preflight_plan_digest": admitted_plan_digest,
                "task_count": len(admitted_tasks),
            },
        )
    if not mission_tasks:
        return _present(
            "missing",
            "The admitted contract exists but the mission task graph is not yet "
            "materialized for this run.",
            {
                "admitted_task_count": len(admitted_tasks),
                "objective_digest": objective_digest,
                "plan_digest": admitted_binding_plan_digest,
            },
        )
    return _present(
        "present",
        "The admitted mission contract is persisted and the task graph is "
        "materialized.",
        {
            "task_count": len(mission_tasks),
            "objective_digest": objective_digest,
            "plan_digest": admitted_binding_plan_digest,
            "mission_task_ids": sorted(
                {str(task.get("task_id", "")) for task in mission_tasks}
            ),
        },
    )


def _roles_section(mission_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    profiles = sorted(
        {
            str(task.get("profile", ""))
            for task in mission_tasks
            if str(task.get("profile", "")).strip()
        }
    )
    if not profiles:
        return _present(
            "missing",
            "No durable task-role assignments exist for this run.",
            {},
        )
    return _present(
        "present",
        "Durable task-role assignments exist for this run.",
        {"profiles": profiles, "task_count": len(mission_tasks)},
    )


def _routing_section(
    observations: list[dict[str, Any]],
    binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not observations:
        return _present(
            "missing",
            "No shadow routing observations were recorded for this run.",
            {},
        )
    binding_routing_enabled = (
        bool(binding.get("routing_enabled")) if binding else False
    )
    binding_routing_mode = str(binding.get("routing_mode", "off")) if binding else "off"
    mismatched_authorities: list[str] = []
    bounded_observations: list[dict[str, Any]] = []
    for observation in observations:
        authority = str(observation.get("actual_authority", ""))
        role = str(observation.get("role", ""))
        bounded_observations.append(
            {
                "observation_id": observation.get("observation_id"),
                "role": role,
                "actual_authority": authority,
                "verdict": observation.get("verdict"),
            }
        )
        if (
            authority == "adaptive_activated"
            and (not binding_routing_enabled or binding_routing_mode == "off")
        ):
            mismatched_authorities.append(role)
    if mismatched_authorities:
        return _present(
            "mismatched",
            "Executed routing authority contradicts the admitted routing "
            "configuration.",
            {
                "observation_count": len(observations),
                "contradicting_roles": sorted(set(mismatched_authorities)),
                "admitted_routing_enabled": binding_routing_enabled,
                "admitted_routing_mode": binding_routing_mode,
            },
        )
    return _present(
        "present",
        "Shadow routing observations are recorded and consistent with the "
        "admitted routing configuration.",
        {
            "observation_count": len(observations),
            "observations": bounded_observations[:20],
        },
    )


def _isolation_section(
    run: RunRecord,
    *,
    project: Mapping[str, Any] | None,
    event_types: dict[str, int],
) -> dict[str, Any]:
    isolated_events = sum(
        count for event_type, count in event_types.items() if event_type in _ISOLATION_EVENT_TYPES
    )
    if project is not None:
        repository_path = str(_project_value(project, "repository_path", "") or "")
        allowed_paths = list(_project_value(project, "allowed_paths", ()) or [])
        if repository_path and run.workspace != repository_path:
            return _present(
                "mismatched",
                "The run workspace differs from the admitted project repository "
                "path; the execution boundary was changed after admission.",
                {
                    "run_workspace": run.workspace,
                    "project_repository_path": repository_path,
                },
            )
        preflight = run.mission_preflight or {}
        index_freshness = str((preflight.get("index") or {}).get("freshness", ""))
        working_tree_state = str(
            (preflight.get("working_tree") or {}).get("state", "")
        )
        if index_freshness == "stale":
            return _present(
                "stale",
                "The admitted preflight recorded a stale repository index; "
                "isolation evidence predates the current index.",
                {
                    "index_freshness": index_freshness,
                    "allowed_path_count": len(allowed_paths),
                    "isolated_worker_events": isolated_events,
                },
            )
        return _present(
            "present",
            "The run workspace matches the admitted project boundary and "
            "isolation evidence is recorded.",
            {
                "workspace_bound": True,
                "allowed_path_count": len(allowed_paths),
                "working_tree_state": working_tree_state,
                "isolated_worker_events": isolated_events,
            },
        )
    if isolated_events:
        return _present(
            "present",
            "Worker isolation events are recorded for this run.",
            {"isolated_worker_events": isolated_events},
        )
    return _present(
        "missing",
        "No isolation evidence is recorded for this run.",
        {},
    )


def _event_section(
    label: str,
    event_types: dict[str, int],
    recognized: frozenset[str],
) -> dict[str, Any]:
    present_types = sorted(
        event_type for event_type in recognized if event_types.get(event_type, 0) > 0
    )
    if not present_types:
        return _present(
            "missing",
            f"No {label} evidence is recorded for this run.",
            {},
        )
    return _present(
        "present",
        f"{label.capitalize()} evidence is recorded for this run.",
        {"events": {event_type: event_types[event_type] for event_type in present_types}},
    )


def _risks_section(preflight: Mapping[str, Any] | None) -> dict[str, Any]:
    tasks = list((preflight or {}).get("tasks") or [])
    if not tasks:
        return _present(
            "missing",
            "No admitted mission task risk levels are persisted for this run.",
            {},
        )
    risk_levels = sorted({str(task.get("risk", "low")) for task in tasks})
    return _present(
        "present",
        "Admitted mission task risk levels are persisted.",
        {
            "task_count": len(tasks),
            "risk_levels": risk_levels,
            "highest_risk": risk_levels[-1] if risk_levels else "low",
        },
    )


def _approval_section(
    approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    if not approvals:
        return _present(
            "missing",
            "No durable approval requests are bound to this run.",
            {},
        )
    by_status: dict[str, int] = {}
    for approval in approvals:
        status = str(approval.get("status", ""))
        by_status[status] = by_status.get(status, 0) + 1
    return _present(
        "present",
        "Durable approval requests are bound to this run.",
        {
            "approval_count": len(approvals),
            "by_status": by_status,
            "approval_ids": [str(a.get("approval_id", "")) for a in approvals[:20]],
        },
    )


def _capsule_section(runs_dir: Path, run_id: str) -> dict[str, Any]:
    marker_path = runs_dir / run_id / _CAPSULE_COMPLETION_MARKER
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return _present(
            "missing",
            "No completed run capsule marker exists for this run.",
            {},
        )
    status = str(payload.get("status", ""))
    if status != "complete":
        return _present(
            "mismatched",
            "The run capsule marker is present but does not report completion.",
            {"marker_status": status},
        )
    return _present(
        "present",
        "A completed run capsule marker exists for this run.",
        {
            "backend": payload.get("backend"),
            "artifacts": list(payload.get("artifacts") or []),
            "completed_at": payload.get("completed_at"),
        },
    )


def build_mission_proof(
    *,
    state: Any,
    run: RunRecord,
    routing_ledger: Any,
    runs_dir: Path,
    project: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reduce durable run evidence into one server-authored projection.

    The projection is read-only: it never mutates state, routing, or policy and
    only exposes bounded receipt/handle metadata. Every section reports
    ``present``, ``missing``, ``stale``, or ``mismatched`` so downstream UI can
    render evidence truthfully without deriving authority from presentation.
    """
    if project is None and run.project_id is not None:
        try:
            project = state.get_project(run.project_id)
        except KeyError:
            project = None

    tasks = state.list_task_nodes(run.run_id)
    mission_tasks = [
        {
            "task_id": task.task_id,
            "title": task.title,
            "profile": task.profile,
            "status": task.status,
            "risk": task.risk,
            "approved": task.approved,
        }
        for task in tasks
        if (task.plan or {}).get("source") == "mission_control"
    ]
    events = state.list_run_steps(run.run_id)
    event_types = _event_type_counts(events)
    observations = [
        observation.to_payload()
        for observation in routing_ledger.list_shadow_observations(run_id=run.run_id)
    ]
    approvals = state.list_approvals(run_id=run.run_id, expire=False)

    binding = run.mission_binding
    preflight = run.mission_preflight

    sections = {
        "binding": _binding_section(run, project=project),
        "contract": _contract_section(run, mission_tasks=mission_tasks),
        "roles": _roles_section(mission_tasks),
        "routing": _routing_section(observations, binding),
        "isolation": _isolation_section(run, project=project, event_types=event_types),
        "change": _event_section(
            "change", event_types, _CHANGE_EVENT_TYPES
        ),
        "validation": _event_section(
            "validation", event_types, _VALIDATION_EVENT_TYPES
        ),
        "review": _event_section(
            "independent review", event_types, _REVIEW_EVENT_TYPES
        ),
        "risks": _risks_section(preflight),
        "approval": _approval_section(approvals),
        "shipping": _event_section(
            "shipping", event_types, _SHIPPING_EVENT_TYPES
        ),
        "capsule": _capsule_section(runs_dir, run.run_id),
        "learning": _event_section(
            "learning", event_types, _LEARNING_EVENT_TYPES
        ),
    }

    by_status: dict[str, list[str]] = {"present": [], "missing": [], "stale": [], "mismatched": []}
    for key, section in sections.items():
        by_status[str(section["status"])].append(key)

    return {
        "schema": MISSION_PROOF_SCHEMA,
        "run_id": run.run_id,
        "project_id": run.project_id,
        "generated_at": _now_iso(),
        "binding": {
            "persisted": binding is not None,
            "preflight_persisted": preflight is not None,
        },
        "evidence": sections,
        "summary": {
            "present": by_status["present"],
            "missing": by_status["missing"],
            "stale": by_status["stale"],
            "mismatched": by_status["mismatched"],
            "counts": {
                "present": len(by_status["present"]),
                "missing": len(by_status["missing"]),
                "stale": len(by_status["stale"]),
                "mismatched": len(by_status["mismatched"]),
            },
        },
    }
