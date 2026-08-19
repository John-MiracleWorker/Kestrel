from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.release_control_receipt import dispatch_binding
from scripts.runtime_reliability_contract import (
    RUNTIME_RELIABILITY_ITERATION_TIMEOUT_SECONDS,
    RUNTIME_RELIABILITY_REQUIRED_REPEATS,
    RUNTIME_RELIABILITY_SCHEDULING_RESERVE_SECONDS,
    RUNTIME_RELIABILITY_TEST_SUCCESS_PATH_BUDGET_SECONDS,
    RUNTIME_RELIABILITY_TESTS,
)

ROOT = Path(__file__).resolve().parents[1]


def _github_workflow_trigger(workflow: dict[object, object]) -> object:
    """Return GitHub's ``on`` key despite PyYAML's YAML 1.1 bool resolver."""

    return workflow.get("on", workflow.get(True))


def test_release_candidate_workflow_has_exact_dispatch_graph_and_permissions() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-candidate.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert workflow["run-name"] == (
        "Kestrel candidate ${{ inputs.version }} @ ${{ inputs.source_sha }} "
        "tx ${{ inputs.transaction_nonce }} bind ${{ inputs.dispatch_binding }}"
    )
    assert workflow["permissions"] == {}
    assert workflow["env"]["CANDIDATE_REPOSITORY_ID"] == "${{ github.repository_id }}"
    trigger = _github_workflow_trigger(workflow)
    assert isinstance(trigger, dict)
    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "version",
        "source_sha",
        "transaction_nonce",
        "dispatch_binding",
    }
    for contract in inputs.values():
        assert contract["required"] is True
        assert contract["type"] == "string"
        assert "default" not in contract

    jobs = workflow["jobs"]
    assert list(jobs) == [
        "candidate-identity",
        "build-release-candidate",
        "cross-platform-exact-wheel",
        "finalize-candidate",
    ]
    assert "needs" not in jobs["candidate-identity"]
    assert jobs["build-release-candidate"]["needs"] == "candidate-identity"
    assert jobs["cross-platform-exact-wheel"]["needs"] == "build-release-candidate"
    assert jobs["finalize-candidate"]["needs"] == "cross-platform-exact-wheel"
    assert {
        name: job["permissions"] for name, job in jobs.items()
    } == {
        "candidate-identity": {"actions": "read", "contents": "read"},
        "build-release-candidate": {"contents": "read"},
        "cross-platform-exact-wheel": {"actions": "read", "contents": "read"},
        "finalize-candidate": {"actions": "read", "contents": "read"},
    }
    assert all("environment" not in job for job in jobs.values())


def test_release_promotion_workflow_has_exact_dispatch_contract() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert workflow["run-name"] == (
        "Kestrel release tx ${{ inputs.transaction_nonce }} "
        "bind ${{ inputs.dispatch_binding }}"
    )
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": "kestrel-release-promotion",
        "cancel-in-progress": False,
    }
    trigger = _github_workflow_trigger(workflow)
    assert isinstance(trigger, dict)
    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "candidate_run_id",
        "candidate_manifest_digest",
        "mode",
        "transaction_nonce",
        "dispatch_binding",
    }
    for contract in inputs.values():
        assert contract["required"] is True
        assert contract["type"] == "string"
        assert "default" not in contract

    assert list(workflow["jobs"]) == ["release-transaction"]
    transaction = workflow["jobs"]["release-transaction"]
    assert transaction == {
        "uses": "./.github/workflows/release-transaction.yml",
        "permissions": {
            "actions": "read",
            "attestations": "write",
            "contents": "write",
            "id-token": "write",
            "packages": "write",
        },
        "secrets": {
            "RELEASE_GUARD_TOKEN": "${{ secrets.RELEASE_GUARD_TOKEN }}",
            "RELEASE_RECOVERY_READER_TOKEN": (
                "${{ secrets.RELEASE_RECOVERY_READER_TOKEN }}"
            ),
        },
        "with": {
            "candidate_run_id": "${{ inputs.candidate_run_id }}",
            "candidate_manifest_digest": "${{ inputs.candidate_manifest_digest }}",
            "mode": "${{ inputs.mode }}",
            "transaction_nonce": "${{ inputs.transaction_nonce }}",
            "dispatch_binding": "${{ inputs.dispatch_binding }}",
        },
    }
    assert "secrets: inherit" not in workflow_path.read_text(encoding="utf-8")


def test_release_transaction_has_exact_seven_job_graph_and_permissions() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-transaction.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert workflow["permissions"] == {}
    trigger = _github_workflow_trigger(workflow)
    assert isinstance(trigger, dict)
    assert set(trigger) == {"workflow_call"}
    workflow_call = trigger["workflow_call"]
    assert workflow_call["secrets"] == {
        "RELEASE_GUARD_TOKEN": {"required": True},
        "RELEASE_RECOVERY_READER_TOKEN": {"required": True},
    }
    inputs = workflow_call["inputs"]
    assert set(inputs) == {
        "candidate_run_id",
        "candidate_manifest_digest",
        "mode",
        "transaction_nonce",
        "dispatch_binding",
    }
    for contract in inputs.values():
        assert contract["required"] is True
        assert contract["type"] == "string"
        assert "default" not in contract

    jobs = workflow["jobs"]
    assert list(jobs) == [
        "identity-admission",
        "authorize-release",
        "prepare-github-ghcr",
        "commit-github-release",
        "verify-github-ghcr",
        "publish-pypi",
        "reconcile-final",
    ]
    assert all(job["runs-on"] == "ubuntu-24.04" for job in jobs.values())
    assert "needs" not in jobs["identity-admission"]
    assert jobs["authorize-release"]["needs"] == "identity-admission"
    assert jobs["prepare-github-ghcr"]["needs"] == "authorize-release"
    assert jobs["commit-github-release"]["needs"] == "prepare-github-ghcr"
    assert jobs["verify-github-ghcr"]["needs"] == "commit-github-release"
    assert jobs["publish-pypi"]["needs"] == "verify-github-ghcr"
    assert jobs["reconcile-final"]["needs"] == [
        "identity-admission",
        "authorize-release",
        "prepare-github-ghcr",
        "commit-github-release",
        "verify-github-ghcr",
        "publish-pypi",
    ]
    assert jobs["reconcile-final"]["if"] == "${{ always() }}"
    assert {
        name: job.get("environment") for name, job in jobs.items()
    } == {
        "identity-admission": None,
        "authorize-release": {"name": "release"},
        "prepare-github-ghcr": {"name": "release-prepare"},
        "commit-github-release": {"name": "release-commit"},
        "verify-github-ghcr": None,
        "publish-pypi": {"name": "pypi"},
        "reconcile-final": None,
    }
    assert {name: job["permissions"] for name, job in jobs.items()} == {
        "identity-admission": {"actions": "read", "contents": "read"},
        "authorize-release": {
            "actions": "read",
            "attestations": "read",
            "contents": "read",
            "packages": "read",
        },
        "prepare-github-ghcr": {
            "actions": "read",
            "contents": "write",
            "packages": "write",
        },
        "commit-github-release": {
            "actions": "read",
            "attestations": "write",
            "contents": "write",
            "id-token": "write",
            "packages": "read",
        },
        "verify-github-ghcr": {
            "actions": "read",
            "attestations": "read",
            "contents": "read",
            "packages": "read",
        },
        "publish-pypi": {
            "actions": "read",
            "contents": "read",
            "id-token": "write",
        },
        "reconcile-final": {
            "actions": "read",
            "attestations": "read",
            "contents": "read",
            "packages": "read",
        },
    }


def test_release_transaction_uses_only_frozen_github_authority_phases() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    scripts_by_job = {
        name: "\n".join(str(step.get("run", "")) for step in job["steps"])
        for name, job in workflow["jobs"].items()
    }
    all_scripts = "\n".join(scripts_by_job.values())

    assert "--expected-phase" not in all_scripts
    assert "--expected-github-authority-phase" not in all_scripts
    for forbidden_stem in (
        "github-authorization-authority",
        "github-preparation-authority",
        "github-verification-authority",
        "github-pypi-boundary-authority",
        "github-final-authority",
    ):
        assert forbidden_stem not in all_scripts

    authorize = scripts_by_job["authorize-release"]
    assert "authority-evidence/github-admission-authority.json" in authorize
    assert "authority-evidence/github-admission-authority-verification.json" in authorize
    assert "--github-admission-authority-verification" in authorize

    commit = scripts_by_job["commit-github-release"]
    assert "github-commit-authority.json" in commit
    assert "github-commit-authority-verification.json" in commit


def test_release_mutations_have_last_moment_current_authority_gates() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(encoding="utf-8")
    )

    expected_predecessors = {
        "prepare-github-ghcr": {
            "Execute only the authorized preparation plan": (
                "Revalidate preparation authority immediately before mutation",
            ),
        },
        "commit-github-release": {
            "Atomically create the marker and publish the exact draft": (
                "Revalidate commit authority immediately before marker publication",
            ),
            "Create only the missing OCI repository custom attestation": (
                "Revalidate commit authority immediately before OCI attestation",
            ),
        },
        "publish-pypi": {
            "Publish only the missing exact PyPI distributions": (
                "Revalidate PyPI and GitHub authority immediately before publication",
            ),
        },
    }
    for job_name, guarded_steps in expected_predecessors.items():
        steps = workflow["jobs"][job_name]["steps"]
        names = [step["name"] for step in steps]
        for mutation, predecessors in guarded_steps.items():
            mutation_index = names.index(mutation)
            assert tuple(names[mutation_index - len(predecessors) : mutation_index]) == predecessors

    prepare_gate = next(
        step
        for step in workflow["jobs"]["prepare-github-ghcr"]["steps"]
        if step["name"] == "Revalidate preparation authority immediately before mutation"
    )["run"]
    for required in (
        "validate_server_authorization",
        "_verified_authority_from_record",
        "_require_current_authority",
        "_require_authorization_admission_authority",
    ):
        assert required in prepare_gate

    for gate_name in (
        "Revalidate commit authority immediately before marker publication",
        "Revalidate commit authority immediately before OCI attestation",
    ):
        gate = next(
            step
            for step in workflow["jobs"]["commit-github-release"]["steps"]
            if step["name"] == gate_name
        )
        assert "verify-github-boundary-binding" in gate["run"]

    pypi_gate = next(
        step
        for step in workflow["jobs"]["publish-pypi"]["steps"]
        if step["name"]
        == "Revalidate PyPI and GitHub authority immediately before publication"
    )
    assert "_require_pypi_authority_binding" in pypi_gate["run"]
    assert "_require_current_authority" in pypi_gate["run"]


def test_commit_reobserves_exact_annotated_tag_before_release_publication() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    execute = next(
        step["run"]
        for step in workflow["jobs"]["commit-github-release"]["steps"]
        if step.get("name") == "Atomically create the marker and publish the exact draft"
    )

    create_ref = execute.index("/git/refs")
    reread_ref = execute.index("/git/ref/tags/", create_ref)
    classify = execute.index("_classify_commit_tag_observation", reread_ref)
    publish = execute.index('"PATCH"', classify)
    assert create_ref < reread_ref < classify < publish


def test_pypi_public_state_is_reobserved_immediately_before_publisher() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["publish-pypi"]["steps"]
    names = [step.get("name") for step in steps]
    gate_name = "Revalidate PyPI and GitHub authority immediately before publication"
    publish_name = "Publish only the missing exact PyPI distributions"
    publish_index = names.index(publish_name)
    assert names[publish_index - 1] == gate_name
    gate = steps[publish_index - 1]["run"]
    assert "fresh-project.json" in gate
    assert "planned_state" in gate and "fresh_state" in gate
    assert "PyPI state changed after publication planning" in gate
    assert "staged PyPI distribution changed after planning" in gate


def test_final_reconciliation_is_durable_when_live_observation_fails() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
        encoding="utf-8"
    )
    workflow = yaml.safe_load(workflow_text)
    steps = workflow["jobs"]["reconcile-final"]["steps"]
    by_name = {step["name"]: step for step in steps}

    observation = by_name[
        "Observe the active lock, ingress, and every available release surface"
    ]
    assert observation["continue-on-error"] is True
    assert observation["if"] == "${{ always() }}"
    assert by_name["Reconcile the transaction without inventing authority"]["if"] == (
        "${{ always() }}"
    )
    assert "final Release listing requires another page" not in workflow_text
    assert "_boundary_paginated_source" in observation["run"]


def test_final_reconciliation_refreshes_authority_before_a_bounded_observation() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["reconcile-final"]["steps"]
    names = [step["name"] for step in steps]
    fetch_name = "Download the fresh terminal final authority boundary"
    boundary_name = "Capture the final live prerequisite boundary"
    binding_name = "Verify the exact terminal final transaction binding"
    observation_name = (
        "Observe the active lock, ingress, and every available release surface"
    )
    reconcile_name = "Reconcile the transaction without inventing authority"

    assert names.index(fetch_name) < names.index(boundary_name)
    assert names.index(boundary_name) < names.index(binding_name)
    assert names.index(binding_name) < names.index(observation_name)
    assert names.index(observation_name) < names.index(reconcile_name)

    by_name = {step["name"]: step for step in steps}
    assert "observe-final-surfaces" not in by_name[fetch_name]["if"]
    capture = by_name[boundary_name]
    assert "begin_final_reconciliation_freshness_budget" in capture["run"]
    observation = by_name[observation_name]
    assert (
        "remaining_final_reconciliation_observation_seconds" in observation["run"]
    )
    assert "if ! test -e reconciliation/final-freshness-budget.json" in observation[
        "run"
    ]
    assert "begin_final_reconciliation_freshness_budget" in observation["run"]
    assert "timeout --signal=TERM --kill-after=5s" in observation["run"]
    assert '"${observation_budget}s"' in observation["run"]
    assert "kestrel-final-observation" in observation["run"]
    reconcile = by_name[reconcile_name]
    assert "require_final_reconciliation_freshness_budget" in reconcile["run"]
    assert "final_freshness_budget_exhausted" in reconcile["run"]


def test_release_signature_verification_steps_receive_the_qualified_guard_token() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(encoding="utf-8")
    )
    signature_consumers = (
        "verify-runtime-credential",
        "verify-github-authority",
        "verify-github-boundary-binding",
        "verify-pypi-authority",
        "_verified_authority_from_record",
        "record-pypi",
        'reconcile "${arguments[@]}"',
    )

    for job in workflow["jobs"].values():
        for step in job["steps"]:
            script = str(step.get("run", ""))
            if any(command in script for command in signature_consumers):
                assert step.get("env", {}).get("GH_TOKEN") in {
                    "${{ github.token }}",
                    "${{ secrets.RELEASE_GUARD_TOKEN }}",
                }, step["name"]


def test_every_inline_github_release_asset_reader_uses_the_bounded_redirect_client() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(encoding="utf-8")
    )
    identity_steps = {
        step["name"]: str(step.get("run", ""))
        for step in workflow["jobs"]["identity-admission"]["steps"]
    }

    for step_name in (
        "Verify recovery-reader credential scope",
        "Poll and verify owner-signed dispatch admission",
    ):
        script = identity_steps[step_name]
        assert "DirectGitHubReadAPI" in script
        assert "asset_api(" in script

    admission_script = identity_steps["Poll and verify owner-signed dispatch admission"]
    assert '"Authorization": f"Bearer {token}"' not in admission_script.split(
        "for asset in assets:", maxsplit=1
    )[1]


def test_every_ghcr_digest_reader_uses_the_credential_stripping_client() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
        encoding="utf-8"
    )

    assert workflow_text.count("fetch_ghcr_pull_token(") == 8
    assert workflow_text.count("DirectOCIReadAPI(") == 9
    assert workflow_text.count("registry_api.read_digest(") == 8
    assert workflow_text.count("registry_reader.read_digest(") == 2
    assert re.search(r"https://ghcr\.io/v2/[^\n]*\{kind\}", workflow_text) is None


def test_ghcr_publisher_uses_the_pinned_nonredirecting_write_client() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    preparation = next(
        step
        for step in workflow["jobs"]["prepare-github-ghcr"]["steps"]
        if step.get("name") == "Execute only the authorized preparation plan"
    )["run"]

    assert "fetch_ghcr_push_token(" in preparation
    assert "DirectOCIWriteAPI(" in preparation
    assert "registry_writer.upload_blob(" in preparation
    assert "registry_writer.put_manifest(" in preparation
    assert "urllib.request" not in preparation
    assert "token_url" not in preparation


def test_preparation_recovery_mutates_only_missing_exact_objects() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    preparation = next(
        step["run"]
        for step in workflow["jobs"]["prepare-github-ghcr"]["steps"]
        if step.get("name") == "Execute only the authorized preparation plan"
    )

    digest_check = preparation.index("for operation, request in requests.items()")
    first_mutation = preparation.index('if release_operation["action"] == "create"')
    assert digest_check < first_mutation
    assert "_missing_product_release_assets" in preparation
    assert "_missing_ghcr_object_digests" in preparation
    assert 'created_release_id = created_state["release_id"]' in preparation
    assert "_resolve_product_release_asset_upload_target" in preparation
    assert '"api",\n                          "api",' not in preparation
    assert "uploaded Release asset is not exact by Release ID" in preparation
    assert "digest not in missing_oci_digests" in preparation
    assert "uploaded GHCR blob is not observable by digest" in preparation
    assert "uploaded GHCR manifest is not observable by digest" in preparation


def test_attestation_plan_uses_missing_subjects_and_stable_fresh_predicate_identity() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    materialize = next(
        step["run"]
        for step in workflow["jobs"]["commit-github-release"]["steps"]
        if step.get("name")
        == "Materialize the canonical predicate from fresh published surfaces"
    )

    assert "existing_identities" in materialize
    assert "missing_subjects" in materialize
    assert "_promotion_attestation_request_identity" in materialize
    assert 'item for item in missing_subjects if item["kind"] == kind' in materialize
    assert "available_by_digest_at" not in materialize.split(
        "_promotion_attestation_request_identity", 1
    )[1]


def test_file_attestations_use_eight_guarded_singleton_subjects() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["commit-github-release"]["steps"]
    names = [step["name"] for step in steps]
    singleton_steps = [
        step
        for step in steps
        if step["name"].startswith("Create missing file subject ")
    ]

    assert len(singleton_steps) == 8
    for index, attestation in enumerate(singleton_steps):
        assert attestation["id"] == f"attest-file-{index}"
        assert attestation["if"] == (
            f"${{{{ steps.commit-plan.outputs.file_{index}_action == 'create' }}}}"
        )
        assert attestation["with"] == {
            "subject-name": f"${{{{ steps.commit-plan.outputs.file_{index}_name }}}}",
            "subject-digest": f"${{{{ steps.commit-plan.outputs.file_{index}_digest }}}}",
            "predicate-type": "https://kestrel.dev/attestations/release-promotion/v1",
            "predicate-path": "transaction/stages/release-promotion-predicate.json",
        }
        position = names.index(attestation["name"])
        gate = steps[position - 1]
        assert gate["name"] == f"Revalidate commit authority for file subject {index}"
        assert gate["if"] == attestation["if"]
        assert "verify-github-boundary-binding" in gate["run"]

    commit_plan = next(
        step["run"]
        for step in steps
        if step.get("name") == "Observe exact commit surfaces and create the commit plan"
    )
    assert "len(file_subjects) != 8" in commit_plan
    assert "subject-checksums" not in (
        ROOT / ".github" / "workflows" / "release-transaction.yml"
    ).read_text(encoding="utf-8")


def test_every_ghcr_observation_requires_complete_package_and_tag_visibility() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
        encoding="utf-8"
    )

    assert workflow_text.count('"package_present": package_present') == 8
    assert workflow_text.count('"tag_inventory_complete": (') == 8
    assert workflow_text.count("_ghcr_package_is_present(") >= workflow_text.count(
        '"package_present": package_present'
    )


def test_preparation_states_await_fresh_ghcr_read_model_convergence() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["prepare-github-ghcr"]["steps"]

    for step_name, settled_assignment in (
        (
            "Observe and plan draft Release and digest-only GHCR preparation",
            "ghcr = transaction.wait_for_ghcr_digest_convergence",
        ),
        (
            "Record preparation outcome from fresh post-state",
            "post_ghcr = transaction.wait_for_ghcr_digest_convergence",
        ),
    ):
        source = next(step["run"] for step in steps if step.get("name") == step_name)
        poll = source.split("def observe_ghcr():", 1)[1].split(
            settled_assignment, 1
        )[0]
        assert "run_gh_json(" in poll
        assert "subprocess.run(" in source
        assert (
            "users/John-MiracleWorker/packages?package_type=container&per_page=100"
            in poll
        )
        assert (
            "users/John-MiracleWorker/packages/container/kestrel/versions?per_page=100"
            in poll
        )
        assert "registry_api.read_digest(" in poll
        assert "time.sleep(" not in source
        assert "time.monotonic(" not in source


def test_release_transaction_wires_role_specific_transaction_stages() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-transaction.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]

    assert "Fail closed until" not in workflow_text
    required_commands = {
        "identity-admission": (
            "create-dispatch-identity",
            "verify-runtime-credential",
            "verify_dispatch_admission",
            "release_candidate_manifest verify",
        ),
        "authorize-release": (
            "verify-runtime-credential",
            "verify-github-authority",
            "inspect-prerequisites",
            "release_promotion_transaction authorize",
        ),
        "prepare-github-ghcr": (
            "verify-recovery-capsule",
            "inspect-prerequisites",
            "plan-preparation",
            "record-preparation",
        ),
        "commit-github-release": (
            "verify-recovery-capsule",
            "verify-github-authority",
            "inspect-prerequisites",
            "plan-commit",
            "record-commit",
        ),
        "verify-github-ghcr": (
            "verify-recovery-capsule",
            "verify-github-ghcr",
        ),
        "publish-pypi": (
            "verify-pypi-authority",
            "pypi-attestations verify pypi --offline",
            "record-pypi",
        ),
        "reconcile-final": ("release_promotion_transaction reconcile",),
    }
    for job_name, commands in required_commands.items():
        job_text = "\n".join(str(step.get("run", "")) for step in jobs[job_name]["steps"])
        for command in commands:
            assert command in job_text, f"{job_name} does not invoke {command}"

    authorize_text = json.dumps(jobs["authorize-release"], sort_keys=True)
    assert "release-authorization.json" in authorize_text
    assert "release-execution-authorization.json" in authorize_text
    assert "authorization.json" not in authorize_text.replace(
        "release-authorization.json", ""
    ).replace("release-execution-authorization.json", "")

    for job in jobs.values():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str):
                assert "${{ inputs." not in run
                assert "${{ github.event.inputs" not in run


def test_release_transaction_uses_mode_specific_authorization_artifact_names() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )

    expected_name = "${{ env.PROMOTION_AUTHORIZATION_ARTIFACT_NAME }}"
    assert workflow["env"]["PROMOTION_AUTHORIZATION_ARTIFACT_NAME"] == (
        "${{ inputs.mode == 'initiate' && "
        "format('kestrel-release-transaction-authorization-{0}-1', github.run_id) || "
        "format('kestrel-release-execution-authorization-{0}-1', github.run_id) }}"
    )

    upload = next(
        step
        for step in workflow["jobs"]["authorize-release"]["steps"]
        if step.get("name") == "Upload role-specific authorization"
    )
    restore = next(
        step
        for step in workflow["jobs"]["prepare-github-ghcr"]["steps"]
        if step.get("name") == "Restore role-specific authorization"
    )
    assert upload["with"]["name"] == expected_name
    assert restore["with"]["name"] == expected_name

    workflow_text = (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
        encoding="utf-8"
    )
    assert "kestrel-release-authorization-${{ github.run_id }}-1" not in workflow_text


def test_release_transaction_recaptures_prerequisites_at_every_live_boundary() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]
    expected = {
        "authorize-release": "authorization-boundary",
        "prepare-github-ghcr": "preparation-boundary",
        "commit-github-release": "commit-boundary",
        "verify-github-ghcr": "verification-boundary",
        "publish-pypi": "pypi-boundary",
    }
    external_observations = (
        "repository-observation.json",
        "repository-collaborators-observation.json",
        "repository-invitations-observation.json",
        "deploy-keys-observation.json",
        "actions-workflow-permissions-observation.json",
        "owner-signing-keys-observation.json",
        "main-branch-observation.json",
        "immutable-releases-observation.json",
        "rulesets-observation.json",
        "tag-ruleset-detail-observation.json",
        "ingress-ruleset-detail-observation.json",
        "workflow-observation.json",
        "default-branch-workflow-contents.json",
        "candidate-workflow-contents.json",
        "recovery-repository-observation.json",
        "recovery-immutable-releases-observation.json",
        "environment-release-observation.json",
        "environment-release-prepare-observation.json",
        "environment-release-commit-observation.json",
        "environment-pypi-observation.json",
        "environment-release-policies-observation.json",
        "environment-release-prepare-policies-observation.json",
        "environment-release-commit-policies-observation.json",
        "environment-pypi-policies-observation.json",
    )

    for job_name, boundary in expected.items():
        steps = jobs[job_name]["steps"]
        capture_index, capture = next(
            (index, step)
            for index, step in enumerate(steps)
            if "capture-prerequisite-boundary" in str(step.get("run", ""))
        )
        capture_run = capture["run"]
        assert f"transaction/{boundary}" in capture_run
        assert capture["env"] == {
            "GH_TOKEN": "${{ secrets.RELEASE_GUARD_TOKEN }}",
            "RELEASE_RECOVERY_READER_TOKEN_BYTES": (
                "${{ secrets.RELEASE_RECOVERY_READER_TOKEN }}"
            ),
        }
        inspect_index, inspect = next(
            (index, step)
            for index, step in enumerate(steps)
            if "inspect-prerequisites" in str(step.get("run", ""))
        )
        assert capture_index < inspect_index
        inspect_run = inspect["run"]
        for filename in external_observations:
            assert f"transaction/{boundary}/{filename}" in inspect_run


def test_recovery_reader_captures_authenticated_paginated_owner_keys() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    step = next(
        step
        for step in workflow["jobs"]["identity-admission"]["steps"]
        if step.get("name") == "Verify recovery-reader credential scope"
    )
    run = step["run"]

    assert '"pages": keys_pages' in run
    assert '"request_url": keys_request_url' in run
    assert '"response_headers": keys_headers' in run
    assert "authenticated=False" not in run
    assert "--identity-observation" in run
    assert "transaction-identity/recovery-reader/identity-probe.json" in run


def test_release_transaction_rebootstraps_capsules_after_artifact_transport() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]

    for producer in (
        "prepare-github-ghcr",
        "commit-github-release",
        "verify-github-ghcr",
    ):
        upload = next(
            step
            for step in jobs[producer]["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
            and "transaction" in str(step.get("with", {}).get("path", ""))
        )
        assert "!transaction/capsule/**" in upload["with"]["path"]

    for consumer, step_name in (
        ("commit-github-release", "Reverify the immutable recovery capsule before commit"),
        (
            "verify-github-ghcr",
            "Reverify the immutable recovery capsule before surface verification",
        ),
        ("publish-pypi", "Reverify the immutable recovery capsule before PyPI admission"),
    ):
        step = next(step for step in jobs[consumer]["steps"] if step.get("name") == step_name)
        run = step["run"]
        bootstrap = run.index("transaction/capsule-download/recovery-bootstrap.py")
        executable_check = run.index("recovery-runtime/environment/bin/python")
        assert "test ! -e transaction/capsule" in run[:bootstrap]
        assert bootstrap < executable_check


def test_recovery_capsule_locator_comes_from_the_committed_publication_receipt() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    prepare = workflow["jobs"]["prepare-github-ghcr"]["steps"]
    download = next(
        step
        for step in prepare
        if step.get("name") == "Download signed capsule verification and immutable capsule"
    )["run"]
    bootstrap = next(
        step
        for step in prepare
        if step.get("name") == "Bootstrap and verify the immutable recovery capsule"
    )["run"]

    downstream = [
        next(
            step["run"]
            for step in workflow["jobs"][job_name]["steps"]
            if step.get("name") == step_name
        )
        for job_name, step_name in (
            (
                "commit-github-release",
                "Reverify the immutable recovery capsule before commit",
            ),
            (
                "verify-github-ghcr",
                "Reverify the immutable recovery capsule before surface verification",
            ),
            (
                "publish-pypi",
                "Reverify the immutable recovery capsule before PyPI admission",
            ),
        )
    ]

    for run in (download, bootstrap, *downstream):
        assert "recovery-${PROMOTION_RUN_ID}-1" not in run
        assert 'publication["tag"]' in run
        assert "recovery-capsule-publication.json" in run


def test_recovery_candidate_and_original_authorization_are_capsule_only() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    identity = workflow["jobs"]["identity-admission"]["steps"]
    initiate_only = {
        "Select exact release candidate",
        "Download exact release candidate",
    }
    for step in identity:
        if step.get("name") in initiate_only:
            assert step.get("if") == "${{ inputs.mode == 'initiate' }}"

    recovery = next(
        step
        for step in identity
        if step.get("name")
        == "Restore committed release candidate from the immutable capsule"
    )
    assert recovery.get("if") == "${{ inputs.mode == 'recover_committed' }}"
    run = recovery["run"]
    for required in (
        "recovery-capsule-verification.json",
        "release-authorization.json",
        "candidate-archive.tar",
        "transaction-identity/recovery-capsule-download/recovery-bootstrap.py",
        "scripts/recovery_launcher.py",
        "materialize-candidate",
        "PROMOTION_CANDIDATE_MANIFEST_DIGEST",
        "PROMOTION_REF_NAME",
    ):
        assert required in run
    assert "actions/download-artifact" not in run
    assert "recovery-${PROMOTION_RUN_ID}-1" not in run
    assert "--destination transaction/capsule" in run
    assert 'capsule_root="$GITHUB_WORKSPACE/transaction/capsule"' in run
    assert "--destination transaction-identity/recovery-capsule" not in run

    verify = next(
        step
        for step in identity
        if step.get("name") == "Verify exact release candidate"
    )["run"]
    assert 'mode = os.environ["PROMOTION_MODE"]' in verify
    assert (
        'if mode == "initiate":\n'
        '    run = json.loads(Path("transaction-identity/candidate-run.json").read_bytes())'
    ) in verify
    assert 'elif mode == "recover_committed":' in verify

    authorize = next(
        step
        for step in workflow["jobs"]["authorize-release"]["steps"]
        if step.get("name") == "Create role-specific server authorization"
    )["run"]
    assert "transaction-identity/recovery/release-authorization.json" in authorize
    assert "cmp --silent" in authorize


def test_capsule_verification_never_reintroduces_the_checkout_with_pythonpath() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
        encoding="utf-8"
    )

    assert "PYTHONPATH=" not in workflow_text
    assert workflow_text.count("transaction/capsule/scripts/release_control_receipt.py") == 4
    assert workflow_text.count("runpy.run_path(target,run_name=\"__main__\")") >= 5
    assert workflow_text.count('"$capsule_root/scripts/recovery_launcher.py" \\') == 18
    assert '"$capsule_root" "$capsule_receipts" verify-' not in workflow_text
    assert workflow_text.count("--executable python") >= 4
    assert (
        workflow_text.count(
            'release verify-asset "$recovery_tag" \\\n'
            '              "transaction/capsule-download/$capsule_asset"'
        )
        == 4
    )
    assert "sys.path.insert(0,root)" not in workflow_text
    assert (
        'sys.path[:]=json.load(open(root+"/recovery-execution-closure.json"))["sys_path"]'
        not in workflow_text
    )
    assert (
        workflow_text.count(
            'import runpy,sys;sys.argv.pop(1);target=sys.argv.pop(1);'
            'runpy.run_path(target,run_name="__main__")'
        )
        == 11
    )


def test_capsule_activation_binds_exact_host_source_before_host_actuation() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]
    activations = (
        (
            "identity-admission",
            "Restore committed release candidate from the immutable capsule",
        ),
        (
            "prepare-github-ghcr",
            "Bootstrap and verify the immutable recovery capsule",
        ),
        (
            "commit-github-release",
            "Reverify the immutable recovery capsule before commit",
        ),
        (
            "verify-github-ghcr",
            "Reverify the immutable recovery capsule before surface verification",
        ),
        (
            "publish-pypi",
            "Reverify the immutable recovery capsule before PyPI admission",
        ),
    )

    for job_name, step_name in activations:
        source = next(
            step["run"]
            for step in jobs[job_name]["steps"]
            if step.get("name") == step_name
        )
        capsule_authority = source.index('"$capsule_root/scripts/recovery_launcher.py"')
        binding = source.index("bind-host-actuator")
        validation = source.index("offline-authority-host-actuator-binding")
        assert capsule_authority < binding < validation
        assert 'mv -- "$GITHUB_WORKSPACE/scripts"' not in source
        assert 'ln -s -- "$capsule_root/scripts"' not in source
        assert 'printf \'KESTREL_PYTHON=%s\\n\' "$capsule_python"' not in source


@pytest.mark.skipif(os.name == "nt", reason="recovery capsule host actuation uses POSIX path separators")
def test_recovery_capsule_authority_is_offline_and_host_actuation_is_explicitly_bound() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    workflow_text = (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
        encoding="utf-8"
    )

    assert 'ln -s -- "$capsule_root/scripts" "$GITHUB_WORKSPACE/scripts"' not in workflow_text
    assert "bind-host-actuator" in workflow_text
    assert workflow_text.count("bind-host-actuator") == 6
    assert "offline-authority-host-actuator-binding" in workflow_text
    assert 'printf \'KESTREL_PYTHON=%s\\n\' "$capsule_python"' not in workflow_text

    activations = (
        (
            "identity-admission",
            "Restore committed release candidate from the immutable capsule",
            "transaction-identity/recovery/host-actuator-binding.json",
        ),
        (
            "prepare-github-ghcr",
            "Bootstrap and verify the immutable recovery capsule",
            "transaction/host-actuator/prepare/host-actuator-binding.json",
        ),
        (
            "commit-github-release",
            "Reverify the immutable recovery capsule before commit",
            "transaction/host-actuator/commit/host-actuator-binding.json",
        ),
        (
            "verify-github-ghcr",
            "Reverify the immutable recovery capsule before surface verification",
            "transaction/host-actuator/verify/host-actuator-binding.json",
        ),
        (
            "publish-pypi",
            "Reverify the immutable recovery capsule before PyPI admission",
            "transaction/host-actuator/pypi/host-actuator-binding.json",
        ),
        (
            "reconcile-final",
            "Activate immutable recovery capsule for reconciliation",
            "reconciliation/host-actuator/host-actuator-binding.json",
        ),
    )
    for job_name, step_name, binding_path in activations:
        source = next(
            step["run"]
            for step in workflow["jobs"][job_name]["steps"]
            if step.get("name") == step_name
        )
        assert "bind-host-actuator" in source
        assert "--host-root \"$GITHUB_WORKSPACE\"" in source
        assert 'install -m 0500 "$KESTREL_PYTHON" "$actuator_input_dir/python"' in source
        assert 'install -m 0500 "$PINNED_GH" "$actuator_input_dir/gh"' in source
        assert '--host-python "$actuator_input_dir/python"' in source
        assert '--host-gh "$actuator_input_dir/gh"' in source
        assert '--output "$host_binding"' in source
        assert "--executable python" in source
        assert f'host_binding="$GITHUB_WORKSPACE/{binding_path}"' in source
        binding_parent = str(Path(binding_path).parent)
        assert (
            f'actuator_input_dir="$GITHUB_WORKSPACE/{binding_parent}/inputs"'
            in source
        )
        assert 'install -d -m 0700 "$actuator_input_dir"' in source
        assert 'test ! -e "$host_binding"' in source
        assert "offline-authority-host-actuator-binding" in source

    for job_name, boundary, output_dir, binding_dir in (
        (
            "prepare-github-ghcr",
            "prepare",
            "transaction/preparation-authority",
            "transaction/host-actuator/prepare/",
        ),
        (
            "commit-github-release",
            "commit",
            "transaction/commit-authority",
            "transaction/host-actuator/commit/",
        ),
        (
            "verify-github-ghcr",
            "verify",
            "transaction/verification-authority",
            "transaction/host-actuator/verify/",
        ),
        (
            "publish-pypi",
            "pypi",
            "transaction/pypi-authority",
            "transaction/host-actuator/pypi/",
        ),
    ):
        fetch = next(
            step["run"]
            for step in workflow["jobs"][job_name]["steps"]
            if "fetch-github-boundary-authority" in str(step.get("run", ""))
        )
        assert f"--boundary {boundary}" in fetch
        assert f"--output-dir {output_dir}" in fetch
        assert binding_dir not in fetch


def test_final_reconciliation_binds_an_offline_materialized_host_actuator() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["reconcile-final"]["steps"]
    names = [step.get("name") for step in steps]
    by_name = {step.get("name"): step for step in steps}

    checkouts = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1
    assert checkouts[0]["continue-on-error"] is True
    assert checkouts[0]["with"] == {
        "fetch-depth": 0,
        "persist-credentials": False,
        "ref": "${{ github.sha }}",
    }
    assert "Install the pinned reconciliation runtime" not in names

    initialize_name = "Initialize fail-closed reconciliation evidence"
    download_name = "Download every preserved transaction artifact"
    activation_name = "Activate immutable recovery capsule for reconciliation"
    bootstrap_name = "Bootstrap pinned GitHub CLI"
    normalize_name = "Normalize unique role-specific reconciliation inputs"
    assert names.index("Check out the dispatch-pinned transaction source") < names.index(
        initialize_name
    )
    assert names.index(initialize_name) < names.index("Set up Python 3.11")
    assert names.index(bootstrap_name) < names.index(download_name)
    assert names.index(download_name) < names.index(activation_name)
    assert names.index(activation_name) < names.index(normalize_name)
    assert by_name[bootstrap_name]["continue-on-error"] is True
    assert "reconciliation/reconciliation-fallback.json" in by_name[initialize_name][
        "run"
    ]

    activation = by_name[activation_name]
    assert activation["id"] == "activate-reconciliation-capsule"
    assert activation["continue-on-error"] is True
    source = activation["run"]
    for required in (
        "recovery-bootstrap.py",
        "recovery-capsule-manifest.json",
        "recovery-capsule-publication.json",
        "recovery-capsule-verification.json",
        "recovery-capsule.tar",
        "kestrel.recovery_capsule_verification.v1",
        "len(assets) != 3",
            '"$recovery_tcb_python" -I -S -B',
            "reconciliation/capsule-input/recovery-bootstrap.py",
            "--destination transaction/capsule",
            "recovery-runtime/environment/bin/python",
            'host_venv="${RUNNER_TEMP}/kestrel-reconcile-actuator-venv"',
            "python -I -m venv --copies",
            "--no-index",
            "bind-host-actuator",
            "offline-authority-host-actuator-binding",
            "host-actuator-binding.json",
            "PYPI_ATTESTATIONS",
            "active=1",
    ):
        assert required in source

    active_gate = (
        "steps.activate-reconciliation-capsule.outputs.active == '1'"
    )
    assert active_gate in by_name[normalize_name]["if"]
    assert active_gate in by_name[
        "Download the fresh terminal final authority boundary"
    ]["if"]

    observation = by_name[
        "Observe the active lock, ingress, and every available release surface"
    ]["run"]
    reconcile = by_name["Reconcile the transaction without inventing authority"]["run"]
    for source in (observation, reconcile):
        gate = source.index("activate-reconciliation-capsule.outputs.active")
        first_release_import = source.index("from scripts")
        assert gate < first_release_import
    assert "recovery_capsule_unavailable" in reconcile
    assert "reconciliation/reconciliation-fallback.json" in reconcile
    assert "reconciliation/release-reconciliation.json" in reconcile

    final_text = "\n".join(str(step.get("run", "")) for step in steps)
    assert "--registry transaction/capsule/release-control-source-registry.json" in (
        final_text
    )
    assert 'Path("transaction/capsule/release-control-source-registry.json")' in (
        final_text
    )


def test_transaction_jobs_pin_the_exact_recovery_python_patch() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )

    setup_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert len(setup_steps) == 7
    assert all(step["with"] == {"python-version": "3.11.14"} for step in setup_steps)


def test_transaction_jobs_verify_the_pinned_python_before_first_use() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-transaction.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    expected_digest = (
        "dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8"
    )

    assert "/usr/bin/python3" not in workflow_text
    for job_name, job in workflow["jobs"].items():
        setup_index = next(
            index
            for index, step in enumerate(job["steps"])
            if str(step.get("uses", "")).startswith("actions/setup-python@")
        )
        verification = job["steps"][setup_index + 1]
        assert verification["name"] == "Verify the pinned recovery Python identity"
        assert expected_digest in verification["run"], job_name
        assert 'readlink -f -- "$(command -v python)"' in verification["run"], job_name
        assert 'python_executable="$(command -v python)"' not in verification["run"], job_name
        assert "python --version" in verification["run"]


def test_recovery_bootstrap_executes_only_the_immutable_release_asset() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
        encoding="utf-8"
    )

    assert "-B scripts/bootstrap_recovery.py" not in workflow_text
    assert workflow_text.count("--pattern recovery-bootstrap.py") == 1
    invocations = re.findall(
        r"^\s+(transaction(?:-identity)?/[^\s]*recovery-bootstrap\.py) \\$",
        workflow_text,
        flags=re.MULTILINE,
    )
    assert invocations == [
        "transaction-identity/recovery-capsule-download/recovery-bootstrap.py",
        *(["transaction/capsule-download/recovery-bootstrap.py"] * 4),
    ]
    assert (
        "transaction-identity/recovery-capsule-download/recovery-bootstrap.py"
        in workflow_text
    )


def test_recovery_bootstrap_is_byte_bound_before_every_execution() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    activations = (
        (
            "identity-admission",
            "Restore committed release candidate from the immutable capsule",
            "transaction-identity/recovery-capsule-download/recovery-bootstrap.py",
        ),
        (
            "prepare-github-ghcr",
            "Bootstrap and verify the immutable recovery capsule",
            "transaction/capsule-download/recovery-bootstrap.py",
        ),
        (
            "commit-github-release",
            "Reverify the immutable recovery capsule before commit",
            "transaction/capsule-download/recovery-bootstrap.py",
        ),
        (
            "verify-github-ghcr",
            "Reverify the immutable recovery capsule before surface verification",
            "transaction/capsule-download/recovery-bootstrap.py",
        ),
        (
            "publish-pypi",
            "Reverify the immutable recovery capsule before PyPI admission",
            "transaction/capsule-download/recovery-bootstrap.py",
        ),
        (
            "reconcile-final",
            "Activate immutable recovery capsule for reconciliation",
            "reconciliation/capsule-input/recovery-bootstrap.py",
        ),
    )

    for job_name, step_name, bootstrap_path in activations:
        source = next(
            step["run"]
            for step in workflow["jobs"][job_name]["steps"]
            if step.get("name") == step_name
        )
        invocation = source.index(f"{bootstrap_path} \\")
        checkout_binding = source.index("cmp --silent scripts/bootstrap_recovery.py")
        assert source.index(f'"{bootstrap_path}"', checkout_binding) < invocation
        digest_binding = source.index("sha256sum", checkout_binding)
        assert source.index(f'"{bootstrap_path}"', digest_binding) < invocation
        assert checkout_binding < digest_binding < invocation


def test_release_transaction_bootstraps_pinned_tools_and_actions() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-transaction.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)

    for job_name, job in workflow["jobs"].items():
        uses = [step["uses"] for step in job.get("steps", []) if "uses" in step]
        for action in uses:
            assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", action), (
                job_name,
                action,
            )
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
                assert step["with"]["ref"] == "${{ github.sha }}"

    for job_name in (
        "identity-admission",
        "authorize-release",
        "prepare-github-ghcr",
        "commit-github-release",
        "verify-github-ghcr",
        "reconcile-final",
    ):
        steps = workflow["jobs"][job_name]["steps"]
        bootstrap_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Bootstrap pinned GitHub CLI"
        )
        authoritative_indexes = [
            index
            for index, step in enumerate(steps)
            if any(
                token in str(step.get("name", ""))
                for token in ("Release", "asset", "attestation", "surface", "capsule")
            )
            and "Bootstrap" not in str(step.get("name", ""))
            and step.get("name")
            != "Activate immutable recovery capsule for reconciliation"
        ]
        assert all(bootstrap_index < index for index in authoritative_indexes)

    assert "scripts/bootstrap_workflow_tools.sh" in workflow_text
    assert "scripts/bootstrap_uv.py" in workflow_text
    assert "astral-sh/setup-uv@" not in workflow_text
    assert "actions/attest-build-provenance@" not in workflow_text
    assert "push-to-registry" not in workflow_text
    assert "skip-existing" not in workflow_text
    assert "--clobber" not in workflow_text
    assert re.search(r"(?m)(^|[;&|]\s*)gh\s", workflow_text) is None


def test_release_transaction_preserves_failure_evidence_and_reconciles_always() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]

    for job_name, outcome_name in (
        ("prepare-github-ghcr", "release-preparation-outcome.json"),
        ("commit-github-release", "release-commit-outcome.json"),
        ("publish-pypi", "release-pypi-outcome.json"),
    ):
        steps = jobs[job_name]["steps"]
        record = next(step for step in steps if outcome_name in str(step.get("run", "")))
        assert record["if"] == "${{ always() }}"
        upload = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
            and outcome_name in str(step.get("with", {}).get("path", ""))
        )
        assert upload["if"] == "${{ always() }}"
        propagate = steps[-1]
        assert propagate["name"] == "Propagate the preserved stage result"
        assert propagate["if"] == "${{ always() }}"

    reconcile = jobs["reconcile-final"]
    reconcile_text = "\n".join(str(step.get("run", "")) for step in reconcile["steps"])
    assert reconcile["if"] == "${{ always() }}"
    assert '"lock_release_permitted":false' in reconcile_text
    assert "release-reconciliation.json" in reconcile_text
    assert any(
        step.get("if") == "${{ always() }}"
        and str(step.get("uses", "")).startswith("actions/upload-artifact@")
        for step in reconcile["steps"]
    )


def test_release_candidate_preflights_before_checkout_and_verifies_final_upload() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-candidate.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    identity_steps = jobs["candidate-identity"]["steps"]
    preflight_index = next(
        index
        for index, step in enumerate(identity_steps)
        if step.get("name") == "Preflight the literal candidate dispatch envelope"
    )
    checkout_index = next(
        index
        for index, step in enumerate(identity_steps)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert preflight_index < checkout_index
    assert "from scripts" not in identity_steps[preflight_index]["run"]

    final_steps = jobs["finalize-candidate"]["steps"]
    upload_index = next(
        index for index, step in enumerate(final_steps) if step.get("id") == "candidate-upload"
    )
    verification = final_steps[upload_index + 1]
    assert verification["name"] == "Verify the unique sealed candidate artifact identity"
    assert verification["env"] == {
        "CANDIDATE_ARTIFACT_ID": "${{ steps.candidate-upload.outputs.artifact-id }}",
        "CANDIDATE_ARTIFACT_DIGEST": "${{ steps.candidate-upload.outputs.artifact-digest }}",
        "CANDIDATE_ARTIFACT_URL": "${{ steps.candidate-upload.outputs.artifact-url }}",
        "GH_TOKEN": "${{ github.token }}",
    }
    assert 'if len(matches) != 1:' in verification["run"]
    assert 'artifact.get("id") != int(os.environ["CANDIDATE_ARTIFACT_ID"])' in verification[
        "run"
    ]
    assert 're.fullmatch(r"[0-9a-f]{64}", upload_digest)' in verification["run"]
    assert 'expected_api_digest = f"sha256:{upload_digest}"' in verification["run"]
    assert 'artifact.get("digest") != expected_api_digest' in verification["run"]
    assert "observed_retention not in {configured_retention, configured_retention - 1}" in verification[
        "run"
    ]
    assert 'run.get("run_attempt") != 1' in verification["run"]


def test_release_candidate_final_upload_verifier_executes_and_checks_retention(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["finalize-candidate"]["steps"]
    verification = next(
        step
        for step in steps
        if step.get("name") == "Verify the unique sealed candidate artifact identity"
    )
    source = verification["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]

    source_sha = "a" * 40
    upload_digest = "c" * 64
    artifact_id = 404
    run_id = 707
    repository = "John-MiracleWorker/Kestrel"
    version = "0.6.0"
    artifact_name = f"kestrel-release-candidate-{version}-{source_sha}"
    artifact = {
        "id": artifact_id,
        "name": artifact_name,
        "size_in_bytes": 4096,
        "digest": f"sha256:{upload_digest}",
        "expired": False,
        "created_at": "2026-08-13T20:00:00Z",
        "expires_at": "2026-09-12T20:00:00Z",
        "archive_download_url": "https://api.github.test/artifacts/404/zip",
        "workflow_run": {"id": run_id, "head_sha": source_sha},
    }
    run = {
        "id": run_id,
        "run_attempt": 1,
        "head_sha": source_sha,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "path": ".github/workflows/release-candidate.yml@refs/heads/main",
        "repository": {"id": 303, "full_name": repository},
    }
    artifact_path = tmp_path / "artifact.json"
    pages_path = tmp_path / "artifact-pages.json"
    run_path = tmp_path / "run.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    pages_path.write_text(json.dumps([{"artifacts": [artifact]}]), encoding="utf-8")
    run_path.write_text(json.dumps(run), encoding="utf-8")
    environment = {
        **os.environ,
        "CANDIDATE_ARTIFACT_ID": str(artifact_id),
        "CANDIDATE_ARTIFACT_DIGEST": upload_digest,
        "CANDIDATE_ARTIFACT_URL": (
            f"https://github.com/{repository}/actions/runs/{run_id}/artifacts/{artifact_id}"
        ),
        "CANDIDATE_VERSION": version,
        "CANDIDATE_SOURCE_SHA": source_sha,
        "CANDIDATE_REPOSITORY_ID": "303",
        "GITHUB_REPOSITORY": repository,
        "GITHUB_RUN_ID": str(run_id),
        "CANDIDATE_ARTIFACT_OBSERVATION": str(artifact_path),
        "CANDIDATE_ARTIFACT_PAGES": str(pages_path),
        "CANDIDATE_RUN_OBSERVATION": str(run_path),
    }

    completed = subprocess.run(
        [sys.executable, "-c", source],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    artifact["expires_at"] = "2026-09-11T20:00:00Z"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, "-c", source],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "retention is not exactly 30 days" in rejected.stderr


def test_release_candidate_matrix_uses_explicit_cross_platform_shells() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["cross-platform-exact-wheel"]["steps"]
    by_name = {step.get("name"): step for step in steps if step.get("name")}

    exact_wheel = by_name["Verify and install exact wheel payload"]
    assert exact_wheel["shell"] == "bash"
    assert '--expected-version "$RELEASE_VERSION"' in exact_wheel["run"]
    matrix_record = by_name["Record the successful exact-wheel matrix cell"]
    assert matrix_record["shell"] == "bash"
    assert "python - <<'PY'" in matrix_record["run"]


def test_release_candidate_embedded_python_is_syntax_valid() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-candidate.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    compiled_blocks: list[str] = []

    for job_name, job in workflow["jobs"].items():
        for step_index, step in enumerate(job.get("steps", [])):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            lines = run.splitlines()
            line_index = 0
            while line_index < len(lines):
                if "python - <<'PY'" not in lines[line_index]:
                    line_index += 1
                    continue
                try:
                    terminator_index = lines.index("PY", line_index + 1)
                except ValueError:
                    pytest.fail(
                        f"{job_name} step {step_index} has an unterminated Python heredoc"
                    )
                label = f"{job_name}:step-{step_index}:heredoc-{len(compiled_blocks) + 1}"
                source = "\n".join(lines[line_index + 1 : terminator_index]) + "\n"
                compile(source, label, "exec")
                compiled_blocks.append(label)
                line_index = terminator_index + 1

    assert len(compiled_blocks) == workflow_text.count("python - <<'PY'")
    assert compiled_blocks


def test_release_transaction_embedded_python_is_syntax_valid() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-transaction.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    compiled_blocks: list[str] = []

    for job_name, job in workflow["jobs"].items():
        for step_index, step in enumerate(job.get("steps", [])):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            lines = run.splitlines()
            line_index = 0
            while line_index < len(lines):
                if "<<'PY'" not in lines[line_index]:
                    line_index += 1
                    continue
                try:
                    terminator_index = lines.index("PY", line_index + 1)
                except ValueError:
                    pytest.fail(
                        f"{job_name} step {step_index} has an unterminated Python heredoc"
                    )
                label = (
                    f"{job_name}:step-{step_index}:"
                    f"heredoc-{len(compiled_blocks) + 1}"
                )
                source = "\n".join(lines[line_index + 1 : terminator_index]) + "\n"
                compile(source, label, "exec")
                compiled_blocks.append(label)
                line_index = terminator_index + 1

    assert len(compiled_blocks) == workflow_text.count("<<'PY'")
    assert compiled_blocks


def test_release_transaction_candidate_admission_uses_real_rest_and_v1_manifest_shapes(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["identity-admission"]["steps"]
    by_name = {step.get("name"): step for step in steps if step.get("name")}

    select = by_name["Select exact release candidate"]["run"]
    assert '".github/workflows/release-candidate.yml@main"' in select

    verify = by_name["Verify exact release candidate"]["run"]
    assert 'manifest["candidate_run"]' in verify
    assert 'manifest["workflow"]' not in verify
    assert '".github/workflows/release-candidate.yml@main"' in verify

    source_sha = "a" * 40
    manifest = {
        "candidate_run": {
            "workflow_id": 606,
            "workflow_ref": "refs/heads/main",
            "workflow_sha": source_sha,
            "run_id": 1000,
            "run_attempt": 1,
        },
        "source": {"commit_sha": source_sha},
        "tag": "v0.6.0",
    }
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    identity = tmp_path / "transaction-identity"
    identity.mkdir()
    (candidate / "candidate-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    select_source = select.split('$KESTREL_PYTHON" - <<\'PY\'\n', 1)[1].rsplit(
        "\nPY", 1
    )[0]
    github_output = tmp_path / "github-output"
    selected = subprocess.run(
        [sys.executable, "-c", select_source],
        cwd=tmp_path,
        env={
            **os.environ,
            "RUN_JSON": json.dumps(
                {
                    "id": 1000,
                    "workflow_id": 606,
                    "run_attempt": 1,
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "head_sha": source_sha,
                    "path": ".github/workflows/release-candidate.yml@main",
                    "repository": {"full_name": "John-MiracleWorker/Kestrel"},
                }
            ),
            "ARTIFACTS_JSON": json.dumps(
                [
                    {
                        "artifacts": [
                            {
                                "name": f"kestrel-release-candidate-0.6.0-{source_sha}",
                                "expired": False,
                                "workflow_run": {"id": 1000},
                            }
                        ]
                    }
                ]
            ),
            "PROMOTION_CANDIDATE_RUN_ID": "1000",
            "PROMOTION_REPOSITORY": "John-MiracleWorker/Kestrel",
            "GITHUB_OUTPUT": str(github_output),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert selected.returncode == 0, selected.stderr
    assert "artifact_name=kestrel-release-candidate-0.6.0-" in github_output.read_text(
        encoding="utf-8"
    )
    source = verify.split("$KESTREL_PYTHON\" - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env={
            **os.environ,
            "PROMOTION_CANDIDATE_RUN_ID": "1000",
            "PROMOTION_MODE": "initiate",
            "PROMOTION_SHA": source_sha,
            "PROMOTION_REF_NAME": "main",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_release_transaction_downloads_commit_authority_before_commit_planning() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["commit-github-release"]["steps"]
    names = [step.get("name") for step in steps]
    plan_index = names.index("Observe exact commit surfaces and create the commit plan")
    for prerequisite in (
        "Download exact owner-signed commit authority",
        "Verify both injected credentials at the commit boundary",
        "Verify owner-signed GitHub commit authority",
        "Require exact cumulative commit approvals",
    ):
        assert names.index(prerequisite) < plan_index


def test_release_transaction_never_interpolates_secrets_into_run_bodies() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(encoding="utf-8")
    )
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str):
                assert "secrets." not in run


def test_release_candidate_identity_steps_execute_against_the_closed_schema(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["candidate-identity"]["steps"]
    by_name = {step.get("name"): step for step in steps if step.get("name")}
    source_sha = "a" * 40
    nonce = "b" * 64
    version = "0.6.0"
    binding = dispatch_binding(
        short_ref="main",
        inputs_without_binding={
            "source_sha": source_sha,
            "transaction_nonce": nonce,
            "version": version,
        },
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "CANDIDATE_SOURCE_SHA": source_sha,
        "CANDIDATE_VERSION": version,
        "CANDIDATE_TRANSACTION_NONCE": nonce,
        "CANDIDATE_DISPATCH_BINDING": binding,
        "CANDIDATE_GITHUB_REF": "refs/heads/main",
        "CANDIDATE_GITHUB_REF_NAME": "main",
        "CANDIDATE_GITHUB_SHA": source_sha,
        "CANDIDATE_REPOSITORY": "John-MiracleWorker/Kestrel",
        "CANDIDATE_REPOSITORY_ID": "303",
        "CANDIDATE_WORKFLOW": "Release Candidate",
        "CANDIDATE_WORKFLOW_REF": (
            "John-MiracleWorker/Kestrel/.github/workflows/"
            "release-candidate.yml@refs/heads/main"
        ),
        "CANDIDATE_WORKFLOW_SHA": source_sha,
        "CANDIDATE_ACTOR": "John-MiracleWorker",
        "CANDIDATE_ACTOR_ID": "606",
        "CANDIDATE_TRIGGERING_ACTOR": "John-MiracleWorker",
        "GITHUB_RUN_ID": "707",
        "GITHUB_RUN_ATTEMPT": "1",
    }

    for name in (
        "Preflight the literal candidate dispatch envelope",
        "Create the canonical candidate dispatch identity",
    ):
        run = by_name[name]["run"]
        source = run.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        completed = subprocess.run(
            [sys.executable, "-c", source],
            cwd=tmp_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    identity = json.loads(
        (tmp_path / "candidate-identity" / "kestrel-dispatch-identity.json").read_text(
            encoding="utf-8"
        )
    )
    assert identity["schema"] == "kestrel.dispatch_identity.v1"
    assert identity["dispatch_binding"] == binding
    assert identity["sha"] == source_sha
    assert identity["provenance"]["producer"] == "scripts/release_control_receipt.py"

    schema = json.loads(
        (ROOT / "schemas" / "kestrel.dispatch_identity.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    actor_pattern = schema["properties"]["actor"]["pattern"]
    triggering_actor_pattern = schema["properties"]["triggering_actor"]["pattern"]
    for pattern in (actor_pattern, triggering_actor_pattern):
        assert re.fullmatch(pattern, "John-MiracleWorker")
        assert re.fullmatch(pattern, "kestrel-release-dispatcher[bot]")
        assert re.fullmatch(pattern, "other-user") is None


_PREREQUISITE_WORKFLOWS = {
    "protected-main-ci": ".github/workflows/ci.yml",
    "release-rehearsal": ".github/workflows/release-rehearsal.yml",
    "runtime-reliability-qualification": ".github/workflows/determinism.yml",
}


def _candidate_prerequisite_run(
    name: str, *, run_id: int, updated_at: str, **overrides: object
) -> dict[str, object]:
    run: dict[str, object] = {
        "id": run_id,
        "head_sha": "a" * 40,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "updated_at": updated_at,
        "path": _PREREQUISITE_WORKFLOWS[name],
    }
    run.update(overrides)
    return run


def _run_candidate_prerequisite_selector(
    tmp_path: Path,
    runs: dict[str, list[dict[str, object]]],
) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
            encoding="utf-8"
        )
    )
    step = next(
        step
        for step in workflow["jobs"]["candidate-identity"]["steps"]
        if step.get("name")
        == "Prove the exact protected-main source and select attempt-one prerequisites"
    )
    marker = 'SOURCE_TREE="$source_tree" python - <<\'PY\'\n'
    source = step["run"].split(marker, 1)[1].split("\nPY\n", 1)[0]
    raw = tmp_path / "candidate-prerequisites" / "raw"
    raw.mkdir(parents=True)
    (raw / "candidate-run.json").write_text(
        json.dumps({"created_at": "2026-07-28T12:00:00Z"}), encoding="utf-8"
    )
    for name, values in runs.items():
        (raw / f"{name}-runs.json").write_text(
            json.dumps([{"workflow_runs": values}]), encoding="utf-8"
        )
    environment = {
        **os.environ,
        "CANDIDATE_SOURCE_SHA": "a" * 40,
        "GITHUB_RUN_ID": "707",
        "SOURCE_TREE": "b" * 40,
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def test_determinism_lane_runs_twenty_seeded_repeats_and_always_uploads_report() -> None:
    workflow = (ROOT / ".github" / "workflows" / "determinism.yml").read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert "scripts/run_determinism_evals.py" in workflow
    assert "--repeats 20" in workflow
    assert "--seed 1729" in workflow
    assert '--source-commit "${SOURCE_COMMIT}"' in workflow
    assert "--case-timeout-seconds 60" in workflow
    assert "--iteration-timeout-seconds 1500" in workflow
    assert 'PYTHONHASHSEED: "1729"' in workflow
    assert "if: always()" in workflow
    assert (
        "kestrel-determinism-${{ matrix.backend }}-${{ env.SOURCE_COMMIT }}" in workflow
    )
    assert "packages: write" not in workflow
    assert "id-token: write" not in workflow


def test_determinism_lane_binds_pr_evidence_to_the_exact_head_commit() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )

    assert workflow["env"]["SOURCE_COMMIT"] == (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    for job_name in (
        "everyday-golden-determinism",
        "runtime-reliability-qualification",
        "flock-qualification-determinism",
    ):
        checkout = next(
            step
            for step in workflow["jobs"][job_name]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == "${{ env.SOURCE_COMMIT }}"
        assert checkout["with"]["persist-credentials"] is False

    golden = workflow["jobs"]["everyday-golden-determinism"]
    golden_invocation = next(
        step["run"]
        for step in golden["steps"]
        if step.get("name") == "Run twenty identical everyday golden evaluations"
    )
    assert '--source-commit "${SOURCE_COMMIT}"' in golden_invocation
    golden_upload = next(
        step
        for step in golden["steps"]
        if step.get("name") == "Upload the machine-readable flake report"
    )
    assert golden_upload["with"]["name"] == (
        "kestrel-determinism-${{ matrix.backend }}-${{ env.SOURCE_COMMIT }}"
    )

    flock = workflow["jobs"]["flock-qualification-determinism"]
    flock_invocation = next(
        step["run"]
        for step in flock["steps"]
        if step.get("name") == "Run twenty identical flock qualification journeys"
    )
    assert '--source-commit "${SOURCE_COMMIT}"' in flock_invocation
    flock_upload = next(
        step
        for step in flock["steps"]
        if step.get("name") == "Upload the flock qualification determinism report"
    )
    assert flock_upload["with"]["name"] == (
        "kestrel-flock-qualification-determinism-${{ env.SOURCE_COMMIT }}"
    )


def test_golden_determinism_matrix_runs_twenty_memory_and_memvid_repeats() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]
    golden = jobs["everyday-golden-determinism"]

    assert golden["strategy"] == {
        "fail-fast": False,
        "matrix": {"backend": ["memory", "memvid"]},
    }
    install = next(
        step["run"]
        for step in golden["steps"]
        if step.get("name") == "Install deterministic evaluation dependencies"
    )
    assert ".[dev,memvid]" in install
    invocation = next(
        step["run"]
        for step in golden["steps"]
        if step.get("name") == "Run twenty identical everyday golden evaluations"
    )
    assert "--backend ${{ matrix.backend }}" in invocation
    assert "--repeats 20" in invocation
    assert "--seed 1729" in invocation
    assert '--source-commit "${SOURCE_COMMIT}"' in invocation
    upload = next(
        step
        for step in golden["steps"]
        if step.get("name") == "Upload the machine-readable flake report"
    )
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == (
        "kestrel-determinism-${{ matrix.backend }}-${{ env.SOURCE_COMMIT }}"
    )
    assert "iteration-receipt.json" in upload["with"]["path"]
    assert "golden-report.json" in upload["with"]["path"]

    flock = jobs["flock-qualification-determinism"]
    assert "strategy" not in flock
    flock_runs = "\n".join(
        str(step.get("run", "")) for step in flock["steps"]
    )
    assert "run_flock_qualification_determinism.py" in flock_runs
    assert "run_determinism_evals.py" not in flock_runs


def test_determinism_jobs_install_hash_locked_dependency_closures() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )
    expected_commands = {
        "everyday-golden-determinism": [
            "python -m pip install --require-hashes --only-binary=:all: "
            "-r config/python-build-bootstrap.txt",
            "uv export --frozen --no-dev --no-emit-local --extra dev --extra memvid "
            '--format requirements.txt --output-file "${RUNNER_TEMP}/requirements-determinism.txt"',
            "python -m pip install --require-hashes --only-binary=:all: "
            '-r "${RUNNER_TEMP}/requirements-determinism.txt"',
            "python -m pip install --no-build-isolation --no-deps -e '.[dev,memvid]'",
            "python -m pip check",
        ],
        "flock-qualification-determinism": [
            "python -m pip install --require-hashes --only-binary=:all: "
            "-r config/python-build-bootstrap.txt",
            "uv export --frozen --no-dev --no-emit-local --extra dev "
            '--format requirements.txt --output-file "${RUNNER_TEMP}/requirements-flock-determinism.txt"',
            "python -m pip install --require-hashes --only-binary=:all: "
            '-r "${RUNNER_TEMP}/requirements-flock-determinism.txt"',
            "python -m pip install --no-build-isolation --no-deps -e '.[dev]'",
            "python -m pip check",
        ],
    }

    for job_name, commands in expected_commands.items():
        steps = workflow["jobs"][job_name]["steps"]
        setup_uv = next(
            step for step in steps if step.get("name") == "Install pinned uv"
        )
        assert setup_uv == {
            "name": "Install pinned uv",
            "uses": "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b",
            "with": {"version": "0.11.16"},
        }
        install = next(
            step["run"]
            for step in steps
            if step.get("name") == "Install deterministic evaluation dependencies"
        )
        logical_commands: list[str] = []
        continued = ""
        for line in install.splitlines():
            stripped = line.strip()
            continued = f"{continued} {stripped}".strip()
            if continued.endswith("\\"):
                continued = continued[:-1].rstrip()
                continue
            logical_commands.append(continued)
            continued = ""

        assert not continued
        assert logical_commands == commands


def test_runtime_reliability_matrix_runs_twenty_fresh_process_repeats_on_all_hosts() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )
    runtime = workflow["jobs"]["runtime-reliability"]

    assert runtime["runs-on"] == "${{ matrix.os }}"
    assert runtime["timeout-minutes"] == 330
    assert runtime["defaults"] == {"run": {"shell": "bash"}}
    assert runtime["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "os": ["ubuntu-latest", "macos-latest", "windows-latest"],
            "python-version": ["3.11"],
        },
    }
    checkout = next(
        step
        for step in runtime["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["uses"] == (
        "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5"
    )
    assert checkout["with"] == {
        "persist-credentials": False,
        "ref": "${{ env.SOURCE_COMMIT }}",
    }
    setup = next(
        step
        for step in runtime["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert setup["uses"] == (
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
    )
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"
    setup_uv = next(
        step for step in runtime["steps"] if step.get("name") == "Install pinned uv"
    )
    assert setup_uv == {
        "name": "Install pinned uv",
        "uses": "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b",
        "with": {"version": "0.11.16"},
    }
    install = next(
        step["run"]
        for step in runtime["steps"]
        if step.get("name") == "Install runtime reliability dependencies"
    )
    logical_commands: list[str] = []
    continued = ""
    for line in install.splitlines():
        stripped = line.strip()
        continued = f"{continued} {stripped}".strip()
        if continued.endswith("\\"):
            continued = continued[:-1].rstrip()
            continue
        logical_commands.append(continued)
        continued = ""

    assert not continued
    assert logical_commands == [
        "python -m pip install --require-hashes --only-binary=:all: "
        "-r config/python-build-bootstrap.txt",
        "uv export --frozen --no-dev --no-emit-local --extra dev "
        '--format requirements.txt --output-file "${RUNNER_TEMP}/requirements-runtime-reliability.txt"',
        "python -m pip install --require-hashes --only-binary=:all: "
        '-r "${RUNNER_TEMP}/requirements-runtime-reliability.txt"',
        "python -m pip install --no-build-isolation --no-deps -e '.[dev]'",
        "python -m pip check",
    ]
    invocation = next(
        step["run"]
        for step in runtime["steps"]
        if step.get("name") == "Run twenty fresh-process runtime reliability repetitions"
    )
    assert "scripts/run_runtime_reliability.py" in invocation
    assert '--source-commit "${SOURCE_COMMIT}"' in invocation
    assert '--run-root "${RUNNER_TEMP}/kestrel-runtime-reliability-runs"' in invocation
    assert '--output "${RUNNER_TEMP}/kestrel-runtime-reliability-report.json"' in invocation
    assert '--workspace "."' in invocation
    tokens = invocation.split()
    repeats = int(tokens[tokens.index("--repeats") + 1])
    assert repeats == RUNTIME_RELIABILITY_REQUIRED_REPEATS
    iteration_timeout = int(tokens[tokens.index("--iteration-timeout-seconds") + 1])
    assert iteration_timeout == RUNTIME_RELIABILITY_ITERATION_TIMEOUT_SECONDS == 900.0
    assert tuple(RUNTIME_RELIABILITY_TEST_SUCCESS_PATH_BUDGET_SECONDS) == (
        RUNTIME_RELIABILITY_TESTS
    )
    assert iteration_timeout >= (
        sum(RUNTIME_RELIABILITY_TEST_SUCCESS_PATH_BUDGET_SECONDS.values())
        + RUNTIME_RELIABILITY_SCHEDULING_RESERVE_SECONDS
    )
    assert repeats * iteration_timeout <= runtime["timeout-minutes"] * 60 - 600
    run_scripts = "\n".join(
        str(step["run"]) for step in runtime["steps"] if "run" in step
    )
    assert "${{ env.SOURCE_COMMIT }}" not in run_scripts
    assert "${{ runner.temp }}" not in run_scripts
    upload = next(
        step
        for step in runtime["steps"]
        if step.get("name") == "Upload runtime reliability receipts"
    )
    assert upload["if"] == "always()"
    assert upload["uses"] == (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert upload["with"]["name"] == (
        "kestrel-runtime-reliability-${{ runner.os }}-${{ env.SOURCE_COMMIT }}"
    )
    assert upload["with"]["path"].splitlines() == [
        "${{ runner.temp }}/kestrel-runtime-reliability-report.json",
        "${{ runner.temp }}/kestrel-runtime-reliability-runs/repeat-*/iteration-receipt.json",
    ]
    assert "pytest-results.xml" not in upload["with"]["path"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == 14
    assert "github.sha" not in json.dumps(runtime, sort_keys=True)


def test_determinism_lane_builds_one_attempt_one_self_contained_five_cell_qualification() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )
    qualification = workflow["jobs"]["runtime-reliability-qualification"]

    assert qualification["needs"] == [
        "runtime-reliability",
        "everyday-golden-determinism",
    ]
    assert qualification["if"] == "github.run_attempt == 1"
    assert "flock" not in json.dumps(qualification, sort_keys=True).lower()

    pinned_download = (
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    )
    downloads = [
        step
        for step in qualification["steps"]
        if step.get("uses") == pinned_download
    ]
    assert [step["with"] for step in downloads] == [
        {
            "name": "kestrel-runtime-reliability-Linux-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-runtime-reliability-Linux-${{ env.SOURCE_COMMIT }}"
            ),
        },
        {
            "name": "kestrel-runtime-reliability-macOS-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-runtime-reliability-macOS-${{ env.SOURCE_COMMIT }}"
            ),
        },
        {
            "name": "kestrel-runtime-reliability-Windows-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-runtime-reliability-Windows-${{ env.SOURCE_COMMIT }}"
            ),
        },
        {
            "name": "kestrel-determinism-memory-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-determinism-memory-${{ env.SOURCE_COMMIT }}"
            ),
        },
        {
            "name": "kestrel-determinism-memvid-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-determinism-memvid-${{ env.SOURCE_COMMIT }}"
            ),
        },
    ]
    assert len(
        [
            step
            for step in qualification["steps"]
            if str(step.get("uses", "")).startswith("actions/download-artifact@")
        ]
    ) == 5

    build = next(
        step["run"]
        for step in qualification["steps"]
        if step.get("name") == "Build the five-cell runtime reliability qualification"
    )
    assert "scripts/aggregate_runtime_reliability_receipts.py build" in build
    assert '--source-commit "${SOURCE_COMMIT}"' in build
    assert '--workflow-run-id "${GITHUB_RUN_ID}"' in build
    assert '--workflow-run-attempt "${GITHUB_RUN_ATTEMPT}"' in build
    assert (
        '--artifact-root "${RUNNER_TEMP}/kestrel-runtime-reliability-qualification"'
        in build
    )

    upload = next(
        step
        for step in qualification["steps"]
        if step.get("name") == "Upload the five-cell runtime reliability qualification"
    )
    assert upload["uses"] == (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert upload.get("if") != "always()"
    assert upload["with"] == {
        "name": (
            "kestrel-runtime-reliability-qualification-"
            "${{ env.SOURCE_COMMIT }}"
        ),
        "path": "${{ runner.temp }}/kestrel-runtime-reliability-qualification",
        "if-no-files-found": "error",
        "retention-days": 14,
    }


def test_release_rehearsal_lane_is_repeatable_and_has_no_publication_authority() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-rehearsal.yml").read_text(
        encoding="utf-8"
    )

    assert "push:\n    branches: [main]" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "scripts/run_release_rehearsal.py" in workflow
    assert "git ls-remote --tags" not in workflow
    assert "production tag already exists" not in workflow
    assert (
        "kestrel-rehearsal-${GITHUB_REPOSITORY_ID}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
        in workflow
    )
    assert "if: always()" in workflow
    for forbidden in (
        "packages: write",
        "contents: write",
        "id-token: write",
        "secrets.",
        "gh release",
        "docker push",
        "pypa/gh-action-pypi-publish",
    ):
        assert forbidden not in workflow


def test_testing_guide_determinism_command_binds_backend_and_source_subject() -> None:
    guide = (ROOT / "docs" / "TESTING.md").read_text(encoding="utf-8")
    command_start = guide.index('DETERMINISM_PARENT="$(mktemp -d)"')
    command_end = guide.index("```", command_start)
    command = guide[command_start:command_end]

    assert "--backend memory" in command
    assert 'WORKTREE_STATUS="$(git status --porcelain=v1 --untracked-files=normal)"' in command
    assert 'if test -n "$WORKTREE_STATUS"; then' in command
    assert "exit 1" in command
    assert 'SOURCE_COMMIT="$(git rev-parse --verify HEAD)"' in command
    assert '--source-commit "$SOURCE_COMMIT"' in command


def test_release_candidate_requires_exact_sha_prerequisite_receipts_before_build() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]
    identity = jobs["candidate-identity"]
    assert jobs["build-release-candidate"]["needs"] == "candidate-identity"
    gate = next(
        step
        for step in identity["steps"]
        if step.get("name")
        == "Prove the exact protected-main source and select attempt-one prerequisites"
    )["run"]

    for name, path in _PREREQUISITE_WORKFLOWS.items():
        assert f"'{name}:{Path(path).name}'" in gate
        assert f'"{name}": "{path}"' in gate
        assert f'"{name}": "kestrel.check.{name}.v1"' in gate
    assert "gh api --paginate --slurp" in gate
    assert "type(run_attempt) is int" in gate
    assert 'run.get("head_sha") == os.environ["CANDIDATE_SOURCE_SHA"]' in gate
    assert 'run.get("head_branch") == "main"' in gate
    assert 'run.get("event") == "push"' in gate
    assert 'run.get("status") == "completed"' in gate
    assert 'run.get("conclusion") == "success"' in gate
    assert "updated < candidate_created" in gate
    assert 'artifact.get("expired") is not False' in gate
    assert 're.fullmatch(r"sha256:[0-9a-f]{64}", str(api_digest))' in gate
    assert 'job.get("conclusion") not in {"success", "skipped"}' in gate
    assert "candidate-prerequisites/qualification/receipts" in gate

    finalize = "\n".join(
        str(step.get("run", "")) for step in jobs["finalize-candidate"]["steps"]
    )
    assert "python -m scripts.release_candidate_manifest create" in finalize
    assert "python -m scripts.release_candidate_manifest verify" in finalize


def test_release_candidate_prerequisite_selector_chooses_latest_exact_runs(
    tmp_path: Path,
) -> None:
    runs = {
        name: [
            _candidate_prerequisite_run(
                name, run_id=index, updated_at="2026-07-28T11:00:00Z"
            ),
            _candidate_prerequisite_run(
                name, run_id=index + 100, updated_at="2026-07-28T11:59:59Z"
            ),
        ]
        for index, name in enumerate(_PREREQUISITE_WORKFLOWS, start=10)
    }

    completed = _run_candidate_prerequisite_selector(tmp_path, runs)

    assert completed.returncode == 0, completed.stderr
    selection = json.loads(
        (tmp_path / "candidate-prerequisites" / "selection.json").read_text(
            encoding="utf-8"
        )
    )
    assert selection == {
        "schema": "kestrel.candidate_prerequisite_selection.v1",
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "candidate_run_id": 707,
        "candidate_run_attempt": 1,
        "runs": {
            name: index + 100
            for index, name in enumerate(_PREREQUISITE_WORKFLOWS, start=10)
        },
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "b" * 40),
        ("head_branch", "feature"),
        ("event", "workflow_dispatch"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("run_attempt", 2),
        ("run_attempt", True),
        ("path", ".github/workflows/other.yml"),
        ("updated_at", "2026-07-28T12:00:00Z"),
        ("updated_at", "2026-07-28T12:00:01Z"),
    ],
)
def test_release_candidate_prerequisite_selector_rejects_nonqualifying_runs(
    tmp_path: Path, field: str, value: object
) -> None:
    runs: dict[str, list[dict[str, object]]] = {}
    for index, name in enumerate(_PREREQUISITE_WORKFLOWS, start=10):
        updated_at = (
            value
            if name == "release-rehearsal" and field == "updated_at"
            else "2026-07-28T11:59:59Z"
        )
        assert isinstance(updated_at, str)
        overrides = (
            {field: value}
            if name == "release-rehearsal" and field != "updated_at"
            else {}
        )
        runs[name] = [
            _candidate_prerequisite_run(
                name, run_id=index, updated_at=updated_at, **overrides
            )
        ]

    completed = _run_candidate_prerequisite_selector(tmp_path, runs)

    assert completed.returncode != 0
    assert "no exact successful attempt-one prerequisite: release-rehearsal" in (
        completed.stderr
    )


def test_release_rehearsal_battery_lane_rehearses_twenty_without_publication_authority() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "release-rehearsal-battery.yml"
    ).read_text(encoding="utf-8")

    assert "push:\n    branches: [main]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "scripts/run_release_rehearsal_battery.py" in workflow
    assert "--repeats 20" in workflow
    assert "--commit \"$BATTERY_SOURCE_SHA\"" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "if: always()" in workflow
    for forbidden in (
        "packages: write",
        "contents: write",
        "id-token: write",
        "secrets.",
        "gh release",
        "docker push",
        "pypa/gh-action-pypi-publish",
        "git push",
    ):
        assert forbidden not in workflow


def test_release_rehearsal_battery_script_enforces_unique_namespaces_without_retry() -> None:
    script = (ROOT / "scripts" / "run_release_rehearsal_battery.py").read_text(
        encoding="utf-8"
    )

    assert "kestrel.release_rehearsal_battery.v1" in script
    assert "for index, namespace in enumerate(namespaces, start=1)" in script
    assert "failed on first attempt" in script
    assert "zero_flaky_failures" in script
    assert "aggregate_digest" in script


def test_installed_artifact_mission_matrix_covers_every_supported_os_python_cell() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "installed-artifact-mission.yml").read_text(
            encoding="utf-8"
        )
    )
    matrix = workflow["jobs"]["installed-mission"]["strategy"]["matrix"]["include"]
    cells = {(row["os"], row["python"], row["machine"]) for row in matrix}

    expected = {
        (os_name, python, machine)
        for os_name, machines in (
            ("ubuntu-latest", "x86_64"),
            ("macos-latest", "arm64"),
            ("windows-latest", "AMD64"),
        )
        for python in ("3.11", "3.12", "3.13")
        for machine in (machines,)
    }
    assert cells == expected
    assert len(cells) == 9
    assert workflow["jobs"]["installed-mission"]["strategy"]["fail-fast"] is False
    assert workflow["jobs"]["installed-mission"]["needs"] == "build-payload"
    steps_text = "\n".join(
        str(step.get("run", "")) for step in workflow["jobs"]["installed-mission"]["steps"]
    )
    assert "python -m scripts.run_installed_artifact_mission dist" in steps_text
    assert "platform.machine().casefold()" in steps_text
    assert "kestrel.installed_artifact_mission_cell.v1" in steps_text
    for forbidden in (
        "contents: write",
        "packages: write",
        "id-token: write",
        "secrets.",
        "gh release",
        "docker push",
        "pypa/gh-action-pypi-publish",
        "git push",
    ):
        assert forbidden not in workflow["jobs"]["installed-mission"]
        assert forbidden not in workflow["jobs"]["build-payload"]


def test_installed_artifact_mission_payload_is_exact_and_self_checksummed() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "installed-artifact-mission.yml"
    ).read_text(encoding="utf-8")

    assert "python -m build --no-isolation --wheel --sdist --outdir dist" in workflow
    assert "uv export --frozen --no-dev --no-emit-local" in workflow
    assert "SHA256SUMS" in workflow
    assert "python scripts/verify_release_payload.py dist" in workflow
    assert "--require-hashes" in workflow
    assert "kestrel-mission-payload-${{ env.MISSION_SOURCE_SHA }}" in workflow
    assert "persist-credentials: false" in workflow


def test_installed_artifact_mission_runner_verifies_readiness_mission_and_cleanup() -> None:
    script = (ROOT / "scripts" / "run_installed_artifact_mission.py").read_text(
        encoding="utf-8"
    )

    assert "kestrel.installed_artifact_mission.v1" in script
    assert "/api/health/ready" in script
    assert "expected 401" in script
    assert "POST" in script
    assert "/api/runs" in script
    assert "terminal_status" in script
    assert "SIGTERM" in script
    assert "port_released" in script
    assert "lock_released" in script
    assert "verify_exact_wheel_install" in script
