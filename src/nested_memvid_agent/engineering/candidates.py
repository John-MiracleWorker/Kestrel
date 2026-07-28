from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..repair_integrity import load_review_receipt, load_validation_receipt
from ..security_boundary import redact_secrets, redact_text
from ..state_store import AgentStateStore, TaskNodeRecord, utc_now
from .schema import ensure_engineering_schema

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")


@dataclass(frozen=True)
class CandidateIsolation:
    candidate_id: str
    task_id: str
    workspace: Path
    branch: str


@dataclass(frozen=True)
class VerifiedCandidateEvidence:
    candidate_digest: str
    validation_id: str
    validation_passed: bool
    validation_evidence_refs: tuple[str, ...]
    review_artifact_refs: tuple[str, ...]
    reviewer_identities: tuple[str, ...]
    reviewer_evidence_refs: tuple[str, ...]
    changed_file_count: int
    changed_line_count: int
    risk_notes: tuple[str, ...]


@dataclass(frozen=True)
class CandidateAttemptRecord:
    candidate_id: str
    fanout_id: str
    run_id: str
    task_id: str
    task_contract_digest: str
    workspace: str
    branch: str
    workspace_identity: str
    status: str
    candidate_digest: str | None
    validation_id: str | None
    validation_passed: bool | None
    validation_evidence_refs: tuple[str, ...]
    review_artifact_refs: tuple[str, ...]
    reviewer_identities: tuple[str, ...]
    reviewer_evidence_refs: tuple[str, ...]
    changed_file_count: int | None
    changed_line_count: int | None
    risk_notes: tuple[str, ...]
    actual_cost_usd: float | None
    latency_seconds: float | None
    evidence_retained: bool
    result: dict[str, Any]
    created_at: str
    finished_at: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "validation_evidence_refs": list(self.validation_evidence_refs),
            "review_artifact_refs": list(self.review_artifact_refs),
            "reviewer_identities": list(self.reviewer_identities),
            "reviewer_evidence_refs": list(self.reviewer_evidence_refs),
            "risk_notes": list(self.risk_notes),
        }


@dataclass(frozen=True)
class CandidateFanoutRecord:
    fanout_id: str
    run_id: str
    source_task_id: str
    task_contract_digest: str
    plan_digest: str
    status: str
    estimated_budget_delta_usd: float
    actor: str
    selected_candidate_id: str | None
    created_at: str
    selected_at: str | None
    candidates: tuple[CandidateAttemptRecord, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "fanout_id": self.fanout_id,
            "run_id": self.run_id,
            "source_task_id": self.source_task_id,
            "task_contract_digest": self.task_contract_digest,
            "plan_digest": self.plan_digest,
            "status": self.status,
            "estimated_budget_delta_usd": self.estimated_budget_delta_usd,
            "actor": self.actor,
            "selected_candidate_id": self.selected_candidate_id,
            "created_at": self.created_at,
            "selected_at": self.selected_at,
            "candidates": [item.to_payload() for item in self.candidates],
        }


@dataclass(frozen=True)
class CandidateSelectionRecord:
    selection_id: str
    fanout_id: str
    selected_candidate_id: str
    actor: str
    ranking: tuple[dict[str, Any], ...]
    ineligible_candidates: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "selection_id": self.selection_id,
            "fanout_id": self.fanout_id,
            "selected_candidate_id": self.selected_candidate_id,
            "actor": self.actor,
            "ranking": list(self.ranking),
            "ineligible_candidates": list(self.ineligible_candidates),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
        }


EvidenceVerifier = Callable[
    [Path, str, tuple[tuple[str, str, str], ...]],
    VerifiedCandidateEvidence,
]


class CandidateFanoutService:
    """Durable isolated candidate registration and evidence-only selection."""

    def __init__(
        self,
        state: AgentStateStore,
        *,
        evidence_verifier: EvidenceVerifier | None = None,
    ) -> None:
        self.state = state
        self._evidence_verifier = evidence_verifier or _verify_repair_evidence
        ensure_engineering_schema(state)

    def preview(
        self,
        *,
        fanout_id: str,
        run_id: str,
        source_task_id: str,
        task_contract_digest: str,
        candidates: tuple[CandidateIsolation, ...],
        estimated_budget_delta_usd: float,
    ) -> dict[str, Any]:
        fanout_key = _id(fanout_id, "fanout_id")
        _id(run_id, "run_id")
        _id(source_task_id, "source_task_id")
        _digest(task_contract_digest, "task_contract_digest")
        budget = _number(
            estimated_budget_delta_usd,
            "estimated_budget_delta_usd",
            maximum=1_000_000,
        )
        if not 2 <= len(candidates) <= 8:
            raise ValueError("candidate fanout requires between two and eight candidates")
        normalized = [_isolation_payload(item) for item in candidates]
        ids = [str(item["candidate_id"]) for item in normalized]
        task_ids = [str(item["task_id"]) for item in normalized]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids must be unique")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("candidate task ids must be unique")
        payload = {
            "schema": "kestrel.candidate_fanout_plan.v1",
            "fanout_id": fanout_key,
            "run_id": run_id,
            "source_task_id": source_task_id,
            "task_contract_digest": task_contract_digest,
            "candidates": normalized,
            "estimated_budget_delta_usd": budget,
            "authority": {
                "new_tools_allowed": False,
                "risk_expansion_allowed": False,
                "exact_plan_approval_required": True,
            },
        }
        payload["plan_digest"] = _hash(payload)
        return payload

    def create_fanout(
        self,
        *,
        fanout_id: str,
        plan: dict[str, Any],
        approved_plan_digest: str,
        actor: str,
        materialize_tasks: bool = False,
    ) -> CandidateFanoutRecord:
        normalized_id = _id(fanout_id, "fanout_id")
        normalized_plan = self.validate_approved_plan(
            fanout_id=normalized_id,
            plan=plan,
            approved_plan_digest=approved_plan_digest,
        )
        computed_digest = str(plan["plan_digest"])
        run_id = _id(normalized_plan.get("run_id"), "run_id")
        source_task_id = _id(
            normalized_plan.get("source_task_id"), "source_task_id"
        )
        contract_digest = _digest(
            normalized_plan.get("task_contract_digest"), "task_contract_digest"
        )
        actor_value = _text(actor, "actor", 160)
        budget = _number(
            normalized_plan.get("estimated_budget_delta_usd"),
            "estimated_budget_delta_usd",
            maximum=1_000_000,
        )
        raw_candidates = normalized_plan.get("candidates")
        if not isinstance(raw_candidates, list) or not 2 <= len(raw_candidates) <= 8:
            raise ValueError("candidate fanout plan has an invalid candidate count")

        run = self.state.get_run(run_id)
        if run.status in {"completed", "failed", "cancelled"}:
            raise ValueError("terminal runs cannot start candidate fanout")
        source = self.state.get_task_node(source_task_id)
        if source.run_id != run_id:
            raise ValueError("source task does not belong to the candidate run")
        source_contract = _task_contract(source)
        prepared: list[dict[str, Any]] = []
        missing_task_ids: set[str] = set()
        identities: set[str] = set()
        paths: set[str] = set()
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                raise ValueError("candidate isolation must be an object")
            candidate_id = _id(raw.get("candidate_id"), "candidate_id")
            task_id = _id(raw.get("task_id"), "candidate.task_id")
            try:
                task = self.state.get_task_node(task_id)
            except KeyError:
                if not materialize_tasks:
                    raise
                missing_task_ids.add(task_id)
            else:
                if task.run_id != run_id:
                    raise ValueError("candidate task does not belong to the run")
                if _task_contract(task) != source_contract:
                    raise ValueError("candidate task contract differs from the source task")
            workspace, identity = _validated_workspace(raw.get("workspace"))
            branch = _branch(raw.get("branch"))
            path_text = str(workspace)
            if identity in identities or path_text in paths:
                raise ValueError("candidate attempts cannot share mutable workspace state")
            identities.add(identity)
            paths.add(path_text)
            prepared.append(
                {
                    "candidate_id": candidate_id,
                    "task_id": task_id,
                    "workspace": path_text,
                    "workspace_identity": identity,
                    "branch": branch,
                }
            )
        if len({item["candidate_id"] for item in prepared}) != len(prepared):
            raise ValueError("candidate ids must be unique")
        if len({item["task_id"] for item in prepared}) != len(prepared):
            raise ValueError("candidate task ids must be unique")

        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM candidate_fanouts WHERE fanout_id = ?",
                (normalized_id,),
            ).fetchone()
            if existing is not None:
                current = _fanout_from_row(existing, candidates=())
                if (
                    current.run_id,
                    current.source_task_id,
                    current.task_contract_digest,
                    current.plan_digest,
                    current.actor,
                ) != (
                    run_id,
                    source_task_id,
                    contract_digest,
                    computed_digest,
                    actor_value,
                ):
                    raise ValueError("candidate_fanout_identity_conflict")
                return self.get_fanout(normalized_id)
            if missing_task_ids:
                if source.status not in {"queued", "approved"}:
                    raise ValueError(
                        "only a queued or approved source task can be fanned out"
                    )
                for item in prepared:
                    if item["task_id"] not in missing_task_ids:
                        continue
                    candidate_plan = dict(source.plan or {})
                    candidate_plan.update(
                        {
                            "fanout_id": normalized_id,
                            "candidate_id": item["candidate_id"],
                            "candidate_contract_digest": contract_digest,
                            "candidate_isolation_worker_id": item["candidate_id"],
                            "replaces_task_id": source_task_id,
                        }
                    )
                    conn.execute(
                        """
                        INSERT INTO task_nodes (
                            task_id, run_id, parent_id, title, goal, profile,
                            status, approved, plan_json, result_json,
                            dependencies_json, required_tools_json, risk,
                            acceptance_criteria_json, attempt_count,
                            failure_reason, diagnosis_json, retry_strategy_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'approved', 1, ?, NULL,
                            ?, ?, ?, ?, 0, '', NULL, NULL, ?, ?)
                        """,
                        (
                            item["task_id"],
                            run_id,
                            source.parent_id,
                            f"Candidate {item['candidate_id']}: {source.title}"[:240],
                            source.goal,
                            source.profile,
                            _json(candidate_plan),
                            _json(source.dependencies),
                            _json(source.required_tools),
                            source.risk,
                            _json(source.acceptance_criteria),
                            now,
                            now,
                        ),
                    )
                conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'cancelled', updated_at = ?
                    WHERE task_id = ? AND run_id = ?
                        AND status IN ('queued', 'approved')
                    """,
                    (now, source_task_id, run_id),
                )
            conn.execute(
                """
                INSERT INTO candidate_fanouts (
                    fanout_id, run_id, source_task_id, task_contract_digest,
                    plan_digest, status, estimated_budget_delta_usd, actor,
                    selected_candidate_id, created_at, selected_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, NULL, ?, NULL)
                """,
                (
                    normalized_id,
                    run_id,
                    source_task_id,
                    contract_digest,
                    computed_digest,
                    budget,
                    actor_value,
                    now,
                ),
            )
            for item in prepared:
                conn.execute(
                    """
                    INSERT INTO candidate_attempts (
                        candidate_id, fanout_id, run_id, task_id,
                        task_contract_digest, workspace, branch, workspace_identity,
                        status, candidate_digest, validation_id, validation_passed,
                        validation_evidence_refs_json, review_artifact_refs_json,
                        reviewer_identities_json, reviewer_evidence_refs_json,
                        changed_file_count, changed_line_count, risk_notes_json,
                        actual_cost_usd, latency_seconds, evidence_retained,
                        result_json, created_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, NULL, NULL,
                        '[]', '[]', '[]', '[]', NULL, NULL, '[]', NULL, NULL, 1,
                        '{}', ?, NULL)
                    """,
                    (
                        item["candidate_id"],
                        normalized_id,
                        run_id,
                        item["task_id"],
                        contract_digest,
                        item["workspace"],
                        item["branch"],
                        item["workspace_identity"],
                        now,
                    ),
                )
        return self.get_fanout(normalized_id)

    def validate_approved_plan(
        self,
        *,
        fanout_id: str,
        plan: dict[str, Any],
        approved_plan_digest: str,
    ) -> dict[str, Any]:
        normalized_id = _id(fanout_id, "fanout_id")
        approved = _digest(approved_plan_digest, "approved_plan_digest")
        normalized_plan = json.loads(_json(plan))
        if not isinstance(normalized_plan, dict):
            raise ValueError("fanout plan must be an object")
        if redact_secrets(normalized_plan) != normalized_plan:
            raise ValueError("fanout plan contains sensitive material")
        supplied_digest = str(normalized_plan.pop("plan_digest", ""))
        computed_digest = _hash(normalized_plan)
        if supplied_digest != computed_digest or approved != computed_digest:
            raise ValueError("approved fanout plan digest does not match the exact plan")
        expected_keys = {
            "schema",
            "fanout_id",
            "run_id",
            "source_task_id",
            "task_contract_digest",
            "candidates",
            "estimated_budget_delta_usd",
            "authority",
        }
        if set(normalized_plan) != expected_keys:
            raise ValueError("candidate fanout plan has unknown or missing fields")
        if normalized_plan.get("schema") != "kestrel.candidate_fanout_plan.v1":
            raise ValueError("unsupported candidate fanout plan schema")
        if _id(normalized_plan.get("fanout_id"), "fanout_id") != normalized_id:
            raise ValueError("candidate fanout id does not match the approved plan")
        run_id = _id(normalized_plan.get("run_id"), "run_id")
        source_task_id = _id(
            normalized_plan.get("source_task_id"),
            "source_task_id",
        )
        source = self.state.get_task_node(source_task_id)
        if source.run_id != run_id:
            raise ValueError("source task does not belong to the candidate run")
        declared_contract = _digest(
            normalized_plan.get("task_contract_digest"),
            "task_contract_digest",
        )
        if declared_contract != _task_contract(source):
            raise ValueError("candidate fanout task contract digest is stale or invalid")
        if normalized_plan.get("authority") != {
            "new_tools_allowed": False,
            "risk_expansion_allowed": False,
            "exact_plan_approval_required": True,
        }:
            raise ValueError("candidate fanout authority contract is invalid")
        candidates = normalized_plan.get("candidates")
        if not isinstance(candidates, list) or not 2 <= len(candidates) <= 8:
            raise ValueError("candidate fanout plan has an invalid candidate count")
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {
                "candidate_id",
                "task_id",
                "workspace",
                "branch",
            }:
                raise ValueError("candidate isolation has unknown or missing fields")
            _id(candidate.get("candidate_id"), "candidate_id")
            _id(candidate.get("task_id"), "candidate.task_id")
            workspace = Path(str(candidate.get("workspace") or "")).expanduser()
            if not workspace.is_absolute():
                raise ValueError("candidate workspace must be an absolute path")
            _branch(candidate.get("branch"))
        return normalized_plan

    def task_contract_digest(self, task_id: str) -> str:
        return _task_contract(self.state.get_task_node(task_id))

    def record_result(
        self,
        *,
        candidate_id: str,
        task_contract_digest: str,
        validation_id: str,
        reviews: tuple[tuple[str, str, str], ...],
        actual_cost_usd: float | None,
        latency_seconds: float | None,
        result: dict[str, Any] | None = None,
    ) -> CandidateAttemptRecord:
        candidate_key = _id(candidate_id, "candidate_id")
        contract_digest = _digest(task_contract_digest, "task_contract_digest")
        validation_key = _text(validation_id, "validation_id", 192)
        normalized_reviews = _reviews(reviews)
        cost = (
            None
            if actual_cost_usd is None
            else _number(actual_cost_usd, "actual_cost_usd", maximum=1_000_000)
        )
        latency = (
            None
            if latency_seconds is None
            else _number(latency_seconds, "latency_seconds", maximum=31_536_000)
        )
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidate_attempts WHERE candidate_id = ?",
                (candidate_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown candidate: {candidate_key}")
        current = _candidate_from_row(row)
        if current.task_contract_digest != contract_digest:
            raise ValueError("candidate result contract digest does not match fanout")
        if current.status not in {"running", "prepared"}:
            raise ValueError("candidate result was already recorded")
        encoded_result = _json(result or {})
        if len(encoded_result) > 128_000:
            raise ValueError("candidate result exceeds the 128 KiB bound")
        safe_result = json.loads(encoded_result)
        if not isinstance(safe_result, dict):
            raise ValueError("candidate result must be an object")
        if redact_secrets(safe_result) != safe_result:
            raise ValueError("candidate result contains sensitive material")
        evidence = self._evidence_verifier(
            Path(current.workspace),
            validation_key,
            normalized_reviews,
        )
        evidence = _validated_evidence(evidence, validation_id=validation_key)
        status = "completed" if evidence.validation_passed else "failed"
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE candidate_attempts
                SET status = ?, candidate_digest = ?, validation_id = ?,
                    validation_passed = ?, validation_evidence_refs_json = ?,
                    review_artifact_refs_json = ?, reviewer_identities_json = ?,
                    reviewer_evidence_refs_json = ?, changed_file_count = ?,
                    changed_line_count = ?, risk_notes_json = ?,
                    actual_cost_usd = ?, latency_seconds = ?, result_json = ?,
                    finished_at = ?
                WHERE candidate_id = ? AND status IN ('running', 'prepared')
                    AND task_contract_digest = ?
                """,
                (
                    status,
                    evidence.candidate_digest,
                    evidence.validation_id,
                    1 if evidence.validation_passed else 0,
                    _json(evidence.validation_evidence_refs),
                    _json(evidence.review_artifact_refs),
                    _json(evidence.reviewer_identities),
                    _json(evidence.reviewer_evidence_refs),
                    evidence.changed_file_count,
                    evidence.changed_line_count,
                    _json(evidence.risk_notes),
                    cost,
                    latency,
                    _json(safe_result),
                    now,
                    candidate_key,
                    contract_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("candidate result lost its durable identity fence")
            updated = conn.execute(
                "SELECT * FROM candidate_attempts WHERE candidate_id = ?",
                (candidate_key,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("candidate_result_write_lost")
        return _candidate_from_row(updated)

    def select(
        self,
        *,
        fanout_id: str,
        actor: str,
    ) -> CandidateSelectionRecord:
        fanout_key = _id(fanout_id, "fanout_id")
        actor_value = _text(actor, "actor", 160)
        fanout = self.get_fanout(fanout_key)
        if fanout.status != "running":
            existing = self.get_selection(fanout_key)
            if existing is not None:
                return existing
            raise ValueError("candidate fanout is not selectable")
        eligible = [
            item
            for item in fanout.candidates
            if item.status == "completed"
            and item.validation_passed is True
            and item.candidate_digest is not None
            and item.validation_evidence_refs
            and item.review_artifact_refs
            and item.reviewer_identities
            and len(item.reviewer_identities) == len(set(item.reviewer_identities))
            and len(item.reviewer_identities) == len(item.reviewer_evidence_refs)
        ]
        ineligible = sorted(
            item.candidate_id
            for item in fanout.candidates
            if item not in eligible
        )
        if not eligible:
            raise ValueError(
                "no candidate has trusted validation and a reviewer artifact"
            )
        ranked = sorted(eligible, key=_candidate_rank_key)
        ranking = tuple(
            {
                "rank": index + 1,
                "candidate_id": item.candidate_id,
                "candidate_digest": item.candidate_digest,
                "reviewer_count": len(item.reviewer_identities),
                "risk_note_count": len(item.risk_notes),
                "changed_file_count": item.changed_file_count,
                "changed_line_count": item.changed_line_count,
                "actual_cost_usd": item.actual_cost_usd,
                "latency_seconds": item.latency_seconds,
                "basis": (
                    "trusted_validation_then_reviewer_diversity_then_lower_risk_"
                    "and_diff_then_known_cost_and_latency"
                ),
            }
            for index, item in enumerate(ranked)
        )
        selected = ranked[0]
        evidence_refs = tuple(
            sorted(
                {
                    *selected.validation_evidence_refs,
                    *selected.review_artifact_refs,
                    *selected.reviewer_evidence_refs,
                }
            )
        )
        now = utc_now()
        selection_id = "selection_" + hashlib.sha256(
            f"{fanout_key}:{selected.candidate_id}:{fanout.plan_digest}".encode()
        ).hexdigest()[:24]
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM candidate_fanouts WHERE fanout_id = ?",
                (fanout_key,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unknown candidate fanout: {fanout_key}")
            if str(current["status"]) != "running":
                raise ValueError("candidate fanout selection raced another decision")
            conn.execute(
                """
                INSERT INTO candidate_selections (
                    selection_id, fanout_id, selected_candidate_id, actor,
                    ranking_json, ineligible_candidates_json, evidence_refs_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    selection_id,
                    fanout_key,
                    selected.candidate_id,
                    actor_value,
                    _json(ranking),
                    _json(ineligible),
                    _json(evidence_refs),
                    now,
                ),
            )
            eligible_ids = {item.candidate_id for item in eligible}
            for item in fanout.candidates:
                status = (
                    "selected"
                    if item.candidate_id == selected.candidate_id
                    else "rejected"
                    if item.candidate_id in eligible_ids
                    else "ineligible"
                )
                conn.execute(
                    "UPDATE candidate_attempts SET status = ? WHERE candidate_id = ?",
                    (status, item.candidate_id),
                )
            conn.execute(
                """
                UPDATE candidate_fanouts
                SET status = 'selected', selected_candidate_id = ?, selected_at = ?
                WHERE fanout_id = ? AND status = 'running'
                """,
                (selected.candidate_id, now, fanout_key),
            )
        selection = self.get_selection(fanout_key)
        if selection is None:
            raise RuntimeError("candidate_selection_write_lost")
        return selection

    def get_fanout(self, fanout_id: str) -> CandidateFanoutRecord:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidate_fanouts WHERE fanout_id = ?",
                (fanout_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown candidate fanout: {fanout_id}")
        return _fanout_from_row(
            row,
            candidates=tuple(self.list_candidates(fanout_id=fanout_id)),
        )

    def list_fanouts(self, *, run_id: str) -> list[CandidateFanoutRecord]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT fanout_id FROM candidate_fanouts
                WHERE run_id = ?
                ORDER BY created_at ASC, fanout_id ASC
                """,
                (run_id,),
            ).fetchall()
        return [self.get_fanout(str(row["fanout_id"])) for row in rows]

    def list_candidates(self, *, fanout_id: str) -> list[CandidateAttemptRecord]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM candidate_attempts
                WHERE fanout_id = ?
                ORDER BY created_at ASC, candidate_id ASC
                """,
                (fanout_id,),
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def get_selection(self, fanout_id: str) -> CandidateSelectionRecord | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidate_selections WHERE fanout_id = ?",
                (fanout_id,),
            ).fetchone()
        return None if row is None else _selection_from_row(row)


def _verify_repair_evidence(
    workspace: Path,
    validation_id: str,
    reviews: tuple[tuple[str, str, str], ...],
) -> VerifiedCandidateEvidence:
    validation = load_validation_receipt(workspace, validation_id)
    snapshot = validation.get("repair_snapshot")
    if validation.get("success") is not True or not isinstance(snapshot, dict):
        raise ValueError("candidate validation receipt is not a trusted success")
    candidate_digest = _digest(snapshot.get("diff_digest"), "candidate_digest")
    changed_files = snapshot.get("changed_files")
    if not isinstance(changed_files, list):
        raise ValueError("candidate validation receipt has no changed-file manifest")
    review_refs: list[str] = []
    reviewer_ids: list[str] = []
    reviewer_evidence: list[str] = []
    risk_notes: list[str] = []
    changed_lines = 0
    for review_id, reviewer_id, evidence_ref in reviews:
        review = load_review_receipt(workspace, review_id)
        review_snapshot = review.get("repair_snapshot")
        if (
            review.get("validation_id") != validation_id
            or not isinstance(review_snapshot, dict)
            or review_snapshot.get("diff_digest") != candidate_digest
            or review.get("commit_gate", {}).get("commit_allowed") is not True
        ):
            raise ValueError("candidate review artifact is stale or mismatched")
        preview = review.get("diff_preview")
        content = preview.get("content") if isinstance(preview, dict) else None
        if isinstance(content, str):
            changed_lines = max(
                changed_lines,
                sum(
                    1
                    for line in content.splitlines()
                    if (line.startswith("+") and not line.startswith("+++"))
                    or (line.startswith("-") and not line.startswith("---"))
                ),
            )
        risks = review.get("risks")
        if isinstance(risks, list):
            risk_notes.extend(str(item) for item in risks if str(item).strip())
        review_refs.append(review_id)
        reviewer_ids.append(reviewer_id)
        reviewer_evidence.append(evidence_ref)
    return VerifiedCandidateEvidence(
        candidate_digest=candidate_digest,
        validation_id=validation_id,
        validation_passed=True,
        validation_evidence_refs=(validation_id,),
        review_artifact_refs=tuple(review_refs),
        reviewer_identities=tuple(reviewer_ids),
        reviewer_evidence_refs=tuple(reviewer_evidence),
        changed_file_count=len(changed_files),
        changed_line_count=changed_lines,
        risk_notes=tuple(sorted(set(risk_notes))),
    )


def _validated_evidence(
    evidence: VerifiedCandidateEvidence,
    *,
    validation_id: str,
) -> VerifiedCandidateEvidence:
    candidate_digest = _digest(evidence.candidate_digest, "candidate_digest")
    if evidence.validation_id != validation_id:
        raise ValueError("candidate evidence validation identity changed")
    if not isinstance(evidence.validation_passed, bool):
        raise ValueError("candidate validation result must be a boolean")
    if (
        isinstance(evidence.changed_file_count, bool)
        or not isinstance(evidence.changed_file_count, int)
        or not 0 <= evidence.changed_file_count <= 1_000_000
        or isinstance(evidence.changed_line_count, bool)
        or not isinstance(evidence.changed_line_count, int)
        or not 0 <= evidence.changed_line_count <= 100_000_000
    ):
        raise ValueError("candidate evidence contains invalid change counts")
    normalized: dict[str, tuple[str, ...]] = {}
    for field, values in (
        ("validation_evidence_refs", evidence.validation_evidence_refs),
        ("review_artifact_refs", evidence.review_artifact_refs),
        ("reviewer_identities", evidence.reviewer_identities),
        ("reviewer_evidence_refs", evidence.reviewer_evidence_refs),
        ("risk_notes", evidence.risk_notes),
    ):
        normalized[field] = _strings(values, field, 64, 1000)
    if len(normalized["review_artifact_refs"]) != len(
        normalized["reviewer_identities"]
    ):
        raise ValueError("each candidate review must identify its reviewer")
    if len(normalized["reviewer_identities"]) != len(
        normalized["reviewer_evidence_refs"]
    ):
        raise ValueError("each candidate reviewer must have provenance")
    return VerifiedCandidateEvidence(
        candidate_digest=candidate_digest,
        validation_id=validation_id,
        validation_passed=evidence.validation_passed,
        validation_evidence_refs=normalized["validation_evidence_refs"],
        review_artifact_refs=normalized["review_artifact_refs"],
        reviewer_identities=normalized["reviewer_identities"],
        reviewer_evidence_refs=normalized["reviewer_evidence_refs"],
        changed_file_count=evidence.changed_file_count,
        changed_line_count=evidence.changed_line_count,
        risk_notes=normalized["risk_notes"],
    )


def _candidate_rank_key(item: CandidateAttemptRecord) -> tuple[Any, ...]:
    return (
        -len(item.reviewer_identities),
        len(item.risk_notes),
        item.changed_file_count if item.changed_file_count is not None else 10**12,
        item.changed_line_count if item.changed_line_count is not None else 10**12,
        item.actual_cost_usd if item.actual_cost_usd is not None else float("inf"),
        item.latency_seconds if item.latency_seconds is not None else float("inf"),
        item.candidate_id,
    )


def _task_contract(task: TaskNodeRecord) -> str:
    return _hash(
        {
            "goal": task.goal,
            "profile": task.profile,
            "dependencies": list(task.dependencies),
            "required_tools": list(task.required_tools),
            "risk": task.risk,
            "acceptance_criteria": list(task.acceptance_criteria),
        }
    )


def _isolation_payload(item: CandidateIsolation) -> dict[str, Any]:
    return {
        "candidate_id": _id(item.candidate_id, "candidate_id"),
        "task_id": _id(item.task_id, "candidate.task_id"),
        "workspace": str(Path(item.workspace).expanduser().resolve()),
        "branch": _branch(item.branch),
    }


def _validated_workspace(value: Any) -> tuple[Path, str]:
    path = Path(str(value or "")).expanduser()
    if path.is_symlink():
        raise ValueError("candidate workspace must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("candidate workspace does not exist") from exc
    if not resolved.is_dir():
        raise ValueError("candidate workspace must be a directory")
    metadata = os.stat(resolved, follow_symlinks=False)
    identity = f"{metadata.st_dev}:{metadata.st_ino}"
    return resolved, identity


def _fanout_from_row(
    row: Any,
    *,
    candidates: tuple[CandidateAttemptRecord, ...],
) -> CandidateFanoutRecord:
    return CandidateFanoutRecord(
        fanout_id=str(row["fanout_id"]),
        run_id=str(row["run_id"]),
        source_task_id=str(row["source_task_id"]),
        task_contract_digest=str(row["task_contract_digest"]),
        plan_digest=str(row["plan_digest"]),
        status=str(row["status"]),
        estimated_budget_delta_usd=float(row["estimated_budget_delta_usd"]),
        actor=str(row["actor"]),
        selected_candidate_id=_optional(row["selected_candidate_id"]),
        created_at=str(row["created_at"]),
        selected_at=_optional(row["selected_at"]),
        candidates=candidates,
    )


def _candidate_from_row(row: Any) -> CandidateAttemptRecord:
    validation_raw = row["validation_passed"]
    return CandidateAttemptRecord(
        candidate_id=str(row["candidate_id"]),
        fanout_id=str(row["fanout_id"]),
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]),
        task_contract_digest=str(row["task_contract_digest"]),
        workspace=str(row["workspace"]),
        branch=str(row["branch"]),
        workspace_identity=str(row["workspace_identity"]),
        status=str(row["status"]),
        candidate_digest=_optional(row["candidate_digest"]),
        validation_id=_optional(row["validation_id"]),
        validation_passed=(
            None if validation_raw is None else bool(validation_raw)
        ),
        validation_evidence_refs=tuple(
            _load(row["validation_evidence_refs_json"], [])
        ),
        review_artifact_refs=tuple(_load(row["review_artifact_refs_json"], [])),
        reviewer_identities=tuple(_load(row["reviewer_identities_json"], [])),
        reviewer_evidence_refs=tuple(
            _load(row["reviewer_evidence_refs_json"], [])
        ),
        changed_file_count=(
            None
            if row["changed_file_count"] is None
            else int(row["changed_file_count"])
        ),
        changed_line_count=(
            None
            if row["changed_line_count"] is None
            else int(row["changed_line_count"])
        ),
        risk_notes=tuple(_load(row["risk_notes_json"], [])),
        actual_cost_usd=(
            None if row["actual_cost_usd"] is None else float(row["actual_cost_usd"])
        ),
        latency_seconds=(
            None if row["latency_seconds"] is None else float(row["latency_seconds"])
        ),
        evidence_retained=bool(row["evidence_retained"]),
        result=dict(_load(row["result_json"], {})),
        created_at=str(row["created_at"]),
        finished_at=_optional(row["finished_at"]),
    )


def _selection_from_row(row: Any) -> CandidateSelectionRecord:
    return CandidateSelectionRecord(
        selection_id=str(row["selection_id"]),
        fanout_id=str(row["fanout_id"]),
        selected_candidate_id=str(row["selected_candidate_id"]),
        actor=str(row["actor"]),
        ranking=tuple(dict(item) for item in _load(row["ranking_json"], [])),
        ineligible_candidates=tuple(
            _load(row["ineligible_candidates_json"], [])
        ),
        evidence_refs=tuple(_load(row["evidence_refs_json"], [])),
        created_at=str(row["created_at"]),
    )


def _reviews(
    value: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[str, str, str], ...]:
    if len(value) > 8:
        raise ValueError("candidate reviews exceed the bounded reviewer count")
    normalized = tuple(
        (
            _text(review_id, "review_id", 192),
            _text(reviewer_id, "reviewer_id", 192),
            _text(evidence_ref, "reviewer_evidence_ref", 512),
        )
        for review_id, reviewer_id, evidence_ref in value
    )
    if len({item[0] for item in normalized}) != len(normalized):
        raise ValueError("candidate review artifacts must be unique")
    if len({item[1] for item in normalized}) != len(normalized):
        raise ValueError("candidate reviewer identities must be distinct")
    return normalized


def _id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"{field} has an invalid identifier")
    return text


def _branch(value: Any) -> str:
    text = str(value or "").strip()
    if (
        _BRANCH.fullmatch(text) is None
        or ".." in text
        or text.endswith("/")
        or text.startswith("/")
    ):
        raise ValueError("candidate branch has an invalid Git ref")
    return text


def _digest(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{field} must be a SHA-256 digest")
    return text


def _text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ValueError(f"{field} is required and bounded to {limit} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(f"{field} contains invalid control characters")
    if redact_text(text) != text:
        raise ValueError(f"{field} contains sensitive material")
    return text


def _strings(
    values: tuple[str, ...],
    field: str,
    limit: int,
    item_limit: int,
) -> tuple[str, ...]:
    if len(values) > limit:
        raise ValueError(f"{field} exceeds its item bound")
    return tuple(_text(item, field, item_limit) for item in values)


def _number(value: Any, field: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if not 0 <= number <= maximum:
        raise ValueError(f"{field} must be between zero and {maximum}")
    return number


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("candidate value must be finite JSON") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value is None else json.loads(str(value))


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)
