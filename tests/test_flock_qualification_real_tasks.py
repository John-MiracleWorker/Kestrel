from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import NamedTuple

import pytest

from nested_memvid_agent.control_plane_integrity import ControlPlaneIntegrity
from nested_memvid_agent.projects import ProjectRecord
from nested_memvid_agent.routing.qualification_real_tasks import (
    REPEATABILITY_CLASSES,
    RealTaskCorpusImporter,
)
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord

TREE_DIGEST = sha256(b"project-a-tree").hexdigest()


def _receipt_payload(
    project: ProjectRecord,
    *,
    task_id: str,
    run_id: str,
    tree_digest: str = TREE_DIGEST,
    verdict: str = "pass",
    receipt_type: str = "test",
) -> dict[str, str]:
    return {
        "receipt_type": receipt_type,
        "task_id": task_id,
        "run_id": run_id,
        "project_id": project.project_id,
        "repository_path": project.repository_path,
        "tree_digest": tree_digest,
        "verdict": verdict,
    }


class _Harness(NamedTuple):
    store: AgentStateStore
    importer: RealTaskCorpusImporter
    integrity: ControlPlaneIntegrity
    project_a: ProjectRecord
    project_b: ProjectRecord


def _make_project(store: AgentStateStore, root: Path, project_id: str) -> ProjectRecord:
    root.mkdir()
    return store.create_project(
        project_id=project_id,
        display_name=project_id,
        repository_path=root,
        allowed_paths=("src", "tests"),
        privacy_class="local_required",
    )


def _make_task(
    store: AgentStateStore,
    *,
    task_id: str,
    run_id: str,
    status: str = "completed",
    risk: str = "low",
    goal: str = "Keep the routing ledger deterministic.",
    result: dict | None = None,
) -> TaskNodeRecord:
    store.create_task_node(
        task_id=task_id,
        run_id=run_id,
        title=f"Task {task_id}",
        goal=goal,
        profile="coder",
        status=status,
        risk=risk,
        required_tools=("tool:test.run",),
        acceptance_criteria=("focused tests pass", "no guardrail violations"),
    )
    if result is not None:
        store.update_task_node(task_id, result=result)
    return store.get_task_node(task_id)


@pytest.fixture()
def harness(tmp_path: Path) -> _Harness:
    store = AgentStateStore(tmp_path / "state.db")
    integrity = ControlPlaneIntegrity(tmp_path / "integrity")
    project_a = _make_project(store, tmp_path / "project_a", "project_a")
    project_b = _make_project(store, tmp_path / "project_b", "project_b")
    store.create_run(
        run_id="run_a",
        message="run a",
        session_id="sess_a",
        workspace=project_a.repository_path,
        model="mock",
        project_id="project_a",
    )
    store.create_run(
        run_id="run_b",
        message="run b",
        session_id="sess_b",
        workspace=project_b.repository_path,
        model="mock",
        project_id="project_b",
    )
    _make_task(
        store,
        task_id="task_from_project_b",
        run_id="run_b",
        result={"outcome": "success", "summary": "self reported"},
    )
    _make_task(
        store,
        task_id="task_without_validation",
        run_id="run_a",
        result={"outcome": "success", "summary": "self reported success"},
    )
    _make_task(
        store,
        task_id="task_validated",
        run_id="run_a",
        result={
            "outcome": "success",
            "validation_receipts": [
                dict(
                    integrity.sign(
                        _receipt_payload(project_a, task_id="task_validated", run_id="run_a")
                    )
                )
            ],
        },
    )
    importer = RealTaskCorpusImporter(
        store,
        integrity=integrity,
        approved_privacy_classes=("local_required",),
        repeatability="read_only",
    )
    return _Harness(store, importer, integrity, project_a, project_b)


@pytest.fixture()
def importer(harness: _Harness) -> RealTaskCorpusImporter:
    return harness.importer


def test_cross_project_task_cannot_enter_corpus(
    importer: RealTaskCorpusImporter,
) -> None:
    with pytest.raises(ValueError, match="selected project"):
        importer.import_tasks(
            project_id="project_a",
            task_ids=["task_from_project_b"],
        )


def test_untrusted_or_self_reported_success_is_diagnostic_only(
    importer: RealTaskCorpusImporter,
) -> None:
    item = importer.import_tasks(
        project_id="project_a",
        task_ids=["task_without_validation"],
    )[0]
    assert item.actionable is False
    assert item.exclusion_reasons == ("trusted_acceptance_evidence_missing",)


def test_unknown_project_is_rejected(importer: RealTaskCorpusImporter) -> None:
    with pytest.raises(ValueError, match="selected project"):
        importer.import_tasks(project_id="project_missing", task_ids=["task_validated"])


def test_unknown_task_is_rejected(importer: RealTaskCorpusImporter) -> None:
    with pytest.raises(ValueError, match="unknown task"):
        importer.import_tasks(project_id="project_a", task_ids=["task_missing"])


def test_unbound_run_task_cannot_enter_corpus(harness: _Harness) -> None:
    harness.store.create_run(
        run_id="run_unbound",
        message="unbound",
        session_id="sess_u",
        workspace=str(harness.project_a.repository_path),
        model="mock",
    )
    _make_task(harness.store, task_id="task_unbound", run_id="run_unbound")
    with pytest.raises(ValueError, match="selected project"):
        harness.importer.import_tasks(project_id="project_a", task_ids=["task_unbound"])


def test_validated_repeatable_task_is_actionable_real_project_evidence(
    importer: RealTaskCorpusImporter,
) -> None:
    (item,) = importer.import_tasks(project_id="project_a", task_ids=["task_validated"])
    assert item.actionable is True
    assert item.exclusion_reasons == ()
    assert item.evidence_kind == "real_project"
    assert item.item_id == "real:project_a:task_validated"
    assert item.risk == "low"
    assert item.task_family == "coder"
    assert item.capabilities == ("tool:test.run",)
    assert len(item.task_contract_digest) == 64
    assert len(item.acceptance_plan_digest) == 64


def test_import_is_deterministic_across_importer_instances(
    harness: _Harness,
) -> None:
    first = harness.importer.import_tasks(
        project_id="project_a",
        task_ids=["task_validated", "task_without_validation"],
    )
    second = RealTaskCorpusImporter(
        harness.store,
        integrity=harness.integrity,
        approved_privacy_classes=("local_required",),
        repeatability="read_only",
    ).import_tasks(
        project_id="project_a",
        task_ids=["task_validated", "task_without_validation"],
    )
    assert first == second


def test_import_does_not_mutate_runs_or_tasks(harness: _Harness) -> None:
    before_task = harness.store.get_task_node("task_validated")
    before_run = harness.store.get_run("run_a")
    harness.importer.import_tasks(project_id="project_a", task_ids=["task_validated"])
    assert harness.store.get_task_node("task_validated") == before_task
    assert harness.store.get_run("run_a") == before_run


def test_tampered_receipt_is_diagnostic_only(harness: _Harness) -> None:
    envelope = dict(
        harness.integrity.sign(
            _receipt_payload(harness.project_a, task_id="task_tampered", run_id="run_a")
        )
    )
    envelope["tag"] = "0" * 64
    _make_task(
        harness.store,
        task_id="task_tampered",
        run_id="run_a",
        result={"outcome": "success", "validation_receipts": [envelope]},
    )
    (item,) = harness.importer.import_tasks(project_id="project_a", task_ids=["task_tampered"])
    assert item.actionable is False
    assert item.exclusion_reasons == ("trusted_acceptance_evidence_missing",)


def test_receipt_bound_to_another_project_is_rejected(harness: _Harness) -> None:
    envelope = dict(
        harness.integrity.sign(
            _receipt_payload(harness.project_b, task_id="task_smuggled", run_id="run_a")
        )
    )
    _make_task(
        harness.store,
        task_id="task_smuggled",
        run_id="run_a",
        result={"outcome": "success", "validation_receipts": [envelope]},
    )
    (item,) = harness.importer.import_tasks(project_id="project_a", task_ids=["task_smuggled"])
    assert item.actionable is False
    assert item.exclusion_reasons == ("trusted_acceptance_evidence_missing",)


def test_failed_verdict_is_not_acceptance_evidence(harness: _Harness) -> None:
    envelope = dict(
        harness.integrity.sign(
            _receipt_payload(
                harness.project_a,
                task_id="task_failed_validation",
                run_id="run_a",
                verdict="fail",
            )
        )
    )
    _make_task(
        harness.store,
        task_id="task_failed_validation",
        run_id="run_a",
        result={"outcome": "success", "validation_receipts": [envelope]},
    )
    (item,) = harness.importer.import_tasks(
        project_id="project_a", task_ids=["task_failed_validation"]
    )
    assert item.actionable is False
    assert item.exclusion_reasons == ("trusted_acceptance_evidence_missing",)


def test_non_test_review_validation_receipt_is_not_acceptance_evidence(
    harness: _Harness,
) -> None:
    envelope = dict(
        harness.integrity.sign(
            _receipt_payload(
                harness.project_a,
                task_id="task_note_receipt",
                run_id="run_a",
                receipt_type="note",
            )
        )
    )
    _make_task(
        harness.store,
        task_id="task_note_receipt",
        run_id="run_a",
        result={"outcome": "success", "validation_receipts": [envelope]},
    )
    (item,) = harness.importer.import_tasks(project_id="project_a", task_ids=["task_note_receipt"])
    assert item.actionable is False
    assert item.exclusion_reasons == ("trusted_acceptance_evidence_missing",)


def test_without_integrity_no_receipt_is_trusted(harness: _Harness) -> None:
    importer = RealTaskCorpusImporter(
        harness.store,
        integrity=None,
        approved_privacy_classes=("local_required",),
        repeatability="read_only",
    )
    (item,) = importer.import_tasks(project_id="project_a", task_ids=["task_validated"])
    assert item.actionable is False
    assert item.exclusion_reasons == ("trusted_acceptance_evidence_missing",)


def test_high_risk_task_is_diagnostic_only(harness: _Harness) -> None:
    envelope = dict(
        harness.integrity.sign(
            _receipt_payload(harness.project_a, task_id="task_risky", run_id="run_a")
        )
    )
    _make_task(
        harness.store,
        task_id="task_risky",
        run_id="run_a",
        risk="high",
        result={"outcome": "success", "validation_receipts": [envelope]},
    )
    (item,) = harness.importer.import_tasks(project_id="project_a", task_ids=["task_risky"])
    assert item.actionable is False
    assert item.exclusion_reasons == ("risk_not_actionable",)


def test_unfinished_task_is_diagnostic_only(harness: _Harness) -> None:
    _make_task(
        harness.store,
        task_id="task_unfinished",
        run_id="run_a",
        status="failed",
        result={"outcome": "failed"},
    )
    (item,) = harness.importer.import_tasks(project_id="project_a", task_ids=["task_unfinished"])
    assert item.actionable is False
    assert item.exclusion_reasons == (
        "task_not_completed",
        "trusted_acceptance_evidence_missing",
    )


def test_privacy_class_without_owner_approval_is_diagnostic_only(
    harness: _Harness,
) -> None:
    importer = RealTaskCorpusImporter(
        harness.store,
        integrity=harness.integrity,
        approved_privacy_classes=(),
        repeatability="read_only",
    )
    (item,) = importer.import_tasks(project_id="project_a", task_ids=["task_validated"])
    assert item.actionable is False
    assert item.exclusion_reasons == ("privacy_exposure_not_approved",)


def test_registered_secret_in_task_contract_is_diagnostic_only(
    harness: _Harness,
) -> None:
    secret = "task5-secret-token-9f27c1"
    register_secret_value(secret)
    envelope = dict(
        harness.integrity.sign(
            _receipt_payload(harness.project_a, task_id="task_secret", run_id="run_a")
        )
    )
    _make_task(
        harness.store,
        task_id="task_secret",
        run_id="run_a",
        goal=f"Rotate the credential {secret} safely.",
        result={"outcome": "success", "validation_receipts": [envelope]},
    )
    (item,) = harness.importer.import_tasks(project_id="project_a", task_ids=["task_secret"])
    assert item.actionable is False
    assert item.exclusion_reasons == ("secret_material_present",)


def test_repeatability_classification_is_required(harness: _Harness) -> None:
    assert REPEATABILITY_CLASSES == (
        "read_only",
        "isolated_worktree",
        "qualified_containment",
    )
    with pytest.raises(ValueError, match="repeatability"):
        RealTaskCorpusImporter(
            harness.store,
            integrity=harness.integrity,
            approved_privacy_classes=("local_required",),
            repeatability="networked",
        )


def test_items_immutable(importer: RealTaskCorpusImporter) -> None:
    import dataclasses

    (item,) = importer.import_tasks(project_id="project_a", task_ids=["task_validated"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.actionable = False  # type: ignore[misc]
