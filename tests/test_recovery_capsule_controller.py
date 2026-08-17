from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import subprocess
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import recovery_capsule_controller as subject
from scripts import release_control_receipt as receipts
from scripts import release_promotion_transaction as transaction

SOURCE_SHA = "a" * 40
RUN_ID = 701
ARTIFACT_ID = 5150
REPOSITORY_ID = 501
NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _canonical(value: object) -> bytes:
    return receipts.canonical_json_bytes(value)  # type: ignore[arg-type]


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _authority_input_paths(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "current_recovery_owner_authority_snapshot": tmp_path
        / "current-recovery-owner-authority-snapshot.json",
        "current_recovery_repository_observation": tmp_path
        / "current-recovery-repository-observation.json",
        "current_recovery_immutable_releases_observation": tmp_path
        / "current-recovery-immutable-releases-observation.json",
        "current_recovery_controller_context": tmp_path
        / "current-recovery-controller-context.json",
    }
    for name, path in paths.items():
        path.write_bytes(_canonical({"fixture": name}))
    return paths


def _artifact_archive(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 14, 12, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, raw)
    return stream.getvalue()


class FakeActionsArtifactAPI:
    def __init__(self, *, name: str, workflow_path: str, archive: bytes) -> None:
        created = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        artifact = {
            "id": ARTIFACT_ID,
            "name": name,
            "size_in_bytes": len(archive),
            "expired": False,
            "digest": _sha256(archive),
            "created_at": created,
            "expires_at": expires,
            "workflow_run": {
                "id": RUN_ID,
                "repository_id": REPOSITORY_ID,
                "head_repository_id": REPOSITORY_ID,
                "head_branch": "main",
                "head_sha": SOURCE_SHA,
            },
        }
        self.run = _canonical(
            {
                "id": RUN_ID,
                "workflow_id": 88,
                "path": workflow_path,
                "event": "workflow_dispatch",
                "head_branch": "main",
                "run_attempt": 1,
                "head_sha": SOURCE_SHA,
                "status": "completed",
                "conclusion": "success",
                "repository": {
                    "id": REPOSITORY_ID,
                    "full_name": "John-MiracleWorker/Kestrel",
                },
            }
        )
        self.artifacts = _canonical([{"total_count": 1, "artifacts": [artifact]}])
        self.artifact = _canonical(artifact)
        self.archive = archive
        self.downloads: list[int] = []

    def get_workflow_run(self, run_id: int) -> bytes:
        assert run_id == RUN_ID
        return self.run

    def list_run_artifacts(self, run_id: int) -> bytes:
        assert run_id == RUN_ID
        return self.artifacts

    def get_artifact(self, artifact_id: int) -> bytes:
        assert artifact_id == ARTIFACT_ID
        return self.artifact

    def download_artifact(self, artifact_id: int, destination: Path) -> None:
        assert artifact_id == ARTIFACT_ID
        self.downloads.append(artifact_id)
        destination.write_bytes(self.archive)
        destination.chmod(0o600)


def _artifact_spec(*, name: str, workflow_path: str, archive: bytes) -> subject.ActionsArtifactSpec:
    return subject.ActionsArtifactSpec(
        name=name,
        workflow_path=workflow_path,
        run_id=RUN_ID,
        artifact_id=ARTIFACT_ID,
        api_digest=_sha256(archive),
        source_sha=SOURCE_SHA,
        require_completed_success=True,
    )


def test_acquire_actions_artifact_downloads_the_verified_server_id(
    tmp_path: Path,
) -> None:
    name = f"kestrel-recovery-dependencies-{SOURCE_SHA}"
    workflow = ".github/workflows/recovery-dependency-staging.yml"
    archive = _artifact_archive(
        {"recovery/dependency-staging-receipt.json": _canonical({"complete": True})}
    )
    api = FakeActionsArtifactAPI(name=name, workflow_path=workflow, archive=archive)

    acquired = subject.acquire_actions_artifact(
        api=api,
        specification=_artifact_spec(name=name, workflow_path=workflow, archive=archive),
        output_root=tmp_path / "acquired",
    )

    assert api.downloads == [ARTIFACT_ID]
    assert acquired.root == tmp_path / "acquired" / "contents"
    assert (acquired.root / "recovery" / "dependency-staging-receipt.json").is_file()
    assert acquired.receipt["artifact"]["artifact_id"] == ARTIFACT_ID  # type: ignore[index]
    assert acquired.receipt["artifact"]["api_digest"] == _sha256(archive)  # type: ignore[index]
    evidence = tmp_path / "acquired" / "evidence"
    assert {path.name for path in evidence.iterdir() if path.is_file()} == {
        "actions-artifact-observation.json",
        "artifact-metadata.json",
        "artifact-pages.json",
        "workflow-run.json",
    }


def test_acquire_actions_artifact_resumes_exact_bytes_without_transport(
    tmp_path: Path,
) -> None:
    name = f"kestrel-recovery-dependencies-{SOURCE_SHA}"
    workflow = ".github/workflows/recovery-dependency-staging.yml"
    archive = _artifact_archive({"expected.txt": b"expected"})
    specification = _artifact_spec(
        name=name,
        workflow_path=workflow,
        archive=archive,
    )
    output_root = tmp_path / "acquired"
    first = subject.acquire_actions_artifact(
        api=FakeActionsArtifactAPI(
            name=name,
            workflow_path=workflow,
            archive=archive,
        ),
        specification=specification,
        output_root=output_root,
    )

    class NoTransport:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"resume attempted transport through {name}")

    resumed = subject.acquire_actions_artifact(
        api=NoTransport(),  # type: ignore[arg-type]
        specification=specification,
        output_root=output_root,
    )

    assert resumed.receipt == first.receipt
    (output_root / "artifact.zip").write_bytes(b"substituted")
    with pytest.raises(ValueError, match="digest"):
        subject.acquire_actions_artifact(
            api=NoTransport(),  # type: ignore[arg-type]
            specification=specification,
            output_root=output_root,
        )


def test_controller_local_write_replays_only_exact_bytes(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    raw = _canonical({"exact": True})

    subject._write_exclusive(output, raw)  # noqa: SLF001
    subject._write_exclusive(output, raw)  # noqa: SLF001

    with pytest.raises(ValueError, match="conflict"):
        subject._write_exclusive(output, _canonical({"exact": False}))  # noqa: SLF001


def test_acquire_actions_artifact_rejects_downloaded_byte_substitution(
    tmp_path: Path,
) -> None:
    name = f"kestrel-recovery-dependencies-{SOURCE_SHA}"
    workflow = ".github/workflows/recovery-dependency-staging.yml"
    expected_archive = _artifact_archive({"expected.txt": b"expected"})
    api = FakeActionsArtifactAPI(
        name=name,
        workflow_path=workflow,
        archive=expected_archive,
    )
    api.archive = _artifact_archive({"substituted.txt": b"substituted"})

    with pytest.raises(ValueError, match="digest"):
        subject.acquire_actions_artifact(
            api=api,
            specification=_artifact_spec(
                name=name,
                workflow_path=workflow,
                archive=expected_archive,
            ),
            output_root=tmp_path / "acquired",
        )

    assert not (tmp_path / "acquired").exists()


def test_acquire_actions_artifact_rejects_metadata_substitution_before_download(
    tmp_path: Path,
) -> None:
    name = f"kestrel-recovery-dependencies-{SOURCE_SHA}"
    workflow = ".github/workflows/recovery-dependency-staging.yml"
    archive = _artifact_archive({"expected.txt": b"expected"})
    api = FakeActionsArtifactAPI(name=name, workflow_path=workflow, archive=archive)
    direct = json.loads(api.artifact)
    direct["id"] = ARTIFACT_ID + 1
    api.artifact = _canonical(direct)

    with pytest.raises(ValueError, match="artifact"):
        subject.acquire_actions_artifact(
            api=api,
            specification=_artifact_spec(name=name, workflow_path=workflow, archive=archive),
            output_root=tmp_path / "acquired",
        )

    assert api.downloads == []
    assert not (tmp_path / "acquired").exists()


def test_github_actions_artifact_api_uses_pinned_exact_id_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = tmp_path / "gh"
    gh.write_bytes(b"pinned gh")
    gh.chmod(0o700)
    archive = _artifact_archive({"artifact.txt": b"artifact"})
    responses = iter((b'{"id":701}', b'[{"total_count":0,"artifacts":[]}]', b'{"id":5150}'))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        stdout = kwargs.get("stdout")
        if stdout is not None:
            stdout.write(archive)  # type: ignore[union-attr]
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(command, 0, next(responses), b"")

    monkeypatch.setattr(receipts, "_verify_pinned_gh", lambda _path: "sha256:" + "a" * 64)
    api = subject.GitHubActionsArtifactAPI(
        pinned_gh=gh,
        token=b"github_pat_owner_controller",
        runner=runner,
    )

    assert api.get_workflow_run(RUN_ID) == b'{"id":701}'
    assert api.list_run_artifacts(RUN_ID).startswith(b"[")
    assert api.get_artifact(ARTIFACT_ID) == b'{"id":5150}'
    destination = tmp_path / "artifact.zip"
    api.download_artifact(ARTIFACT_ID, destination)

    assert destination.read_bytes() == archive
    endpoints = [call[0][-1] for call in calls]
    assert endpoints == [
        f"/repos/{subject.REPOSITORY}/actions/runs/{RUN_ID}",
        f"/repos/{subject.REPOSITORY}/actions/runs/{RUN_ID}/artifacts?per_page=100",
        f"/repos/{subject.REPOSITORY}/actions/artifacts/{ARTIFACT_ID}",
        f"/repos/{subject.REPOSITORY}/actions/artifacts/{ARTIFACT_ID}/zip",
    ]
    assert "--paginate" in calls[1][0] and "--slurp" in calls[1][0]
    assert all(
        call[1]["env"]
        == {
            "GH_TOKEN": "github_pat_owner_controller",
            "GH_PROMPT_DISABLED": "1",
            "NO_COLOR": "1",
        }
        for call in calls
    )
    assert all("github_pat_owner_controller" not in " ".join(call[0]) for call in calls)


def test_production_controller_cli_exposes_every_server_and_target_binding() -> None:
    help_text = subject._parser().format_help()  # noqa: SLF001

    for argument in (
        "--candidate-manifest-digest",
        "--promotion-run-id",
        "--authorization-artifact-id",
        "--authorization-artifact-digest",
        "--staging-run-id",
        "--staging-artifact-id",
        "--staging-artifact-digest",
        "--target-workspace-root",
        "--recovery-repository-id",
        "--identity-file",
        "--current-recovery-owner-authority-snapshot",
        "--current-recovery-repository-observation",
        "--current-recovery-immutable-releases-observation",
        "--current-recovery-controller-context",
    ):
        assert argument in help_text


def test_controller_platform_requires_exact_ubuntu_24_04(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subject.sys, "platform", "linux")
    monkeypatch.setattr(subject.platform, "machine", lambda: "x86_64")
    # The controller requires the exact production CPython 3.11.14 runtime. Pin
    # the interpreter identity so this test exercises the Ubuntu 24.04 gate it
    # is named for, regardless of the ambient runner's Python version.
    monkeypatch.setattr(
        subject.platform, "python_implementation", lambda: "CPython"
    )
    monkeypatch.setattr(subject.platform, "python_version", lambda: "3.11.14")

    subject._require_controller_platform(  # noqa: SLF001
        os_release='ID=ubuntu\nVERSION_ID="24.04"\n'
    )
    with pytest.raises(ValueError, match="Ubuntu 24.04"):
        subject._require_controller_platform(  # noqa: SLF001
            os_release='ID=ubuntu\nVERSION_ID="22.04"\n'
        )


def test_controller_paths_reject_output_aliasing_the_work_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    identity = tmp_path / "identity"
    identity.write_bytes(b"private")
    identity.chmod(0o600)
    work_root = tmp_path / "controller-work"
    authority_inputs = _authority_input_paths(tmp_path)
    request = subject.RecoveryControllerRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target_root,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=work_root,
        output=work_root,
        **authority_inputs,
    )
    monkeypatch.setattr(subject, "_require_controller_platform", lambda: None)
    monkeypatch.setattr(subject, "_require_executing_source", lambda _root: None)
    monkeypatch.setattr(subject, "_require_bootstrap_handoff", lambda _root: None)

    def git_output(_source_root: Path, *arguments: str) -> bytes:
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(source_root).encode() + b"\n"
        if arguments == ("rev-parse", "HEAD^{commit}"):
            return SOURCE_SHA.encode() + b"\n"
        assert arguments == (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        return b""

    monkeypatch.setattr(subject, "_git_output", git_output)

    with pytest.raises(ValueError, match="output parent or path"):
        subject._require_controller_paths(request)  # noqa: SLF001


def test_controller_rejects_execution_from_a_different_checkout_before_state_or_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "clean-checkout-b"
    source_root.mkdir()
    target_root = tmp_path / "target"
    target_root.mkdir()
    identity = tmp_path / "identity"
    identity.write_bytes(b"private")
    identity.chmod(0o600)
    work_root = tmp_path / "controller-work"
    authority_inputs = _authority_input_paths(tmp_path)
    request = subject.RecoveryControllerRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target_root,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=work_root,
        output=tmp_path / "controller-receipt.json",
        **authority_inputs,
    )
    monkeypatch.setattr(subject, "_require_controller_platform", lambda: None)
    git_calls: list[tuple[str, ...]] = []

    def git_output(_source_root: Path, *arguments: str) -> bytes:
        git_calls.append(arguments)
        raise AssertionError("Git must not run before the executing checkout is bound")

    monkeypatch.setattr(subject, "_git_output", git_output)

    with pytest.raises(ValueError, match="executing source"):
        subject.run_production_controller(
            request=request,
            actions_api=object(),  # type: ignore[arg-type]
            terminal_api=object(),  # type: ignore[arg-type]
            owner_read_api=object(),  # type: ignore[arg-type]
            recovery_reader_api=object(),  # type: ignore[arg-type]
            recovery_reader_token=b"reader-token",
            capsule_publisher=lambda *_args: None,
            _clock=lambda: NOW,
        )

    assert git_calls == []
    assert not work_root.exists()


def test_controller_rejects_an_imported_kestrel_module_from_another_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unexpected = tmp_path / "checkout-a" / "scripts" / "bootstrap_recovery.py"
    unexpected.parent.mkdir(parents=True)
    unexpected.write_bytes(b"# substituted module\n")
    monkeypatch.setattr(subject.bootstrap_recovery, "__file__", str(unexpected))

    with pytest.raises(ValueError, match="bootstrap_recovery"):
        subject._require_executing_source(subject.ROOT)  # noqa: SLF001


def test_controller_rejects_a_direct_launch_without_the_preimport_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT",
        raising=False,
    )

    with pytest.raises(ValueError, match="bootstrap receipt"):
        subject._require_bootstrap_handoff(subject.ROOT)  # noqa: SLF001


def test_controller_handoff_excludes_prepare_only_from_the_stable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "bootstrap-receipt.json"
    receipt.write_bytes(_canonical({"bootstrap": True}))
    monkeypatch.setenv("KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT", str(receipt))
    stable = ("--source-root", str(subject.ROOT), "--source-sha", SOURCE_SHA)
    observed: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        subject.controller_bootstrap,
        "authorize_inner_gate",
        lambda **kwargs: observed.append(kwargs["controller_arguments"]),
    )
    monkeypatch.setattr(subject.sys, "argv", ["controller", *stable, "--prepare-only"])

    subject._require_bootstrap_handoff(subject.ROOT)  # noqa: SLF001

    assert observed == [stable]


def test_controller_rejects_invalid_scalar_inputs_before_paths_or_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = subject.RecoveryControllerRequest(
        source_root=tmp_path / "missing-source",
        source_sha="not-a-git-sha",
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=tmp_path / "missing-target",
        recovery_repository_id=304,
        identity_file=tmp_path / "missing-identity",
        current_recovery_owner_authority_snapshot=tmp_path / "missing-owner",
        current_recovery_repository_observation=tmp_path / "missing-repository",
        current_recovery_immutable_releases_observation=tmp_path / "missing-immutable",
        current_recovery_controller_context=tmp_path / "missing-context",
        work_root=tmp_path / "controller-work",
        output=tmp_path / "controller-output.json",
    )
    monkeypatch.setattr(
        subject,
        "_require_controller_paths",
        lambda _request: pytest.fail("path preflight ran before scalar validation"),
    )

    with pytest.raises(ValueError, match="source SHA"):
        subject.run_production_controller(
            request=request,
            actions_api=object(),  # type: ignore[arg-type]
            terminal_api=object(),  # type: ignore[arg-type]
            owner_read_api=object(),  # type: ignore[arg-type]
            recovery_reader_api=object(),  # type: ignore[arg-type]
            recovery_reader_token=b"reader-token",
            capsule_publisher=lambda *_args: None,
        )

    assert not request.work_root.exists()


def test_controller_cli_rejects_invalid_scalars_before_api_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned_gh = tmp_path / "gh"
    pinned_gh.write_bytes(b"gh")
    monkeypatch.setenv("KESTREL_PINNED_GH", str(pinned_gh))
    monkeypatch.setenv("GH_TOKEN", "owner-token")
    monkeypatch.setenv("RELEASE_RECOVERY_READER_TOKEN", "reader-token")
    monkeypatch.setattr(
        subject,
        "GitHubActionsArtifactAPI",
        lambda **_kwargs: pytest.fail("API constructed before scalar validation"),
    )
    target = tmp_path / "target"
    target.mkdir()
    identity = tmp_path / "identity"
    identity.write_bytes(b"identity")
    authority_paths = []
    for name in ("owner", "repository", "immutable", "context"):
        path = tmp_path / f"{name}.json"
        path.write_bytes(b"{}")
        authority_paths.append(path)

    with pytest.raises(SystemExit):
        subject.main(
            [
                "--source-root",
                str(subject.ROOT),
                "--source-sha",
                "not-a-git-sha",
                "--candidate-manifest-digest",
                "sha256:" + "c" * 64,
                "--promotion-run-id",
                str(RUN_ID),
                "--authorization-artifact-id",
                str(ARTIFACT_ID),
                "--authorization-artifact-digest",
                "sha256:" + "d" * 64,
                "--staging-run-id",
                str(RUN_ID + 1),
                "--staging-artifact-id",
                str(ARTIFACT_ID + 1),
                "--staging-artifact-digest",
                "sha256:" + "e" * 64,
                "--target-workspace-root",
                str(target),
                "--recovery-repository-id",
                "304",
                "--identity-file",
                str(identity),
                "--current-recovery-owner-authority-snapshot",
                str(authority_paths[0]),
                "--current-recovery-repository-observation",
                str(authority_paths[1]),
                "--current-recovery-immutable-releases-observation",
                str(authority_paths[2]),
                "--current-recovery-controller-context",
                str(authority_paths[3]),
                "--work-root",
                str(tmp_path / "work"),
                "--output",
                str(tmp_path / "output.json"),
            ]
        )


def test_controller_workspace_resumes_only_the_exact_immutable_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    identity = tmp_path / "identity"
    identity.write_bytes(b"identity")
    authority_inputs = _authority_input_paths(tmp_path)
    bootstrap_receipt = tmp_path / "bootstrap-receipt.json"
    bootstrap_receipt.write_bytes(_canonical({"bootstrap": True}))
    monkeypatch.setenv(
        "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT",
        str(bootstrap_receipt),
    )
    request = subject.RecoveryControllerRequest(
        source_root=subject.ROOT,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=tmp_path / "controller-work",
        output=tmp_path / "controller-output.json",
        **authority_inputs,
    )
    scalars = subject._validate_controller_request_scalars(request)  # noqa: SLF001

    assert subject._open_controller_workspace(request, scalars) is False  # noqa: SLF001
    journal = request.work_root / "controller-request.json"
    initial = journal.read_bytes()
    assert subject._open_controller_workspace(request, scalars) is True  # noqa: SLF001
    assert journal.read_bytes() == initial

    identity.write_bytes(b"substituted identity")
    with pytest.raises(ValueError, match="request journal conflicts"):
        subject._open_controller_workspace(request, scalars)  # noqa: SLF001
    identity.write_bytes(b"identity")

    renewable_input = request.current_recovery_controller_context
    renewable_input.write_bytes(b"renewed current authority input")
    assert subject._open_controller_workspace(request, scalars) is True  # noqa: SLF001

    changed = replace(
        request,
        staging_artifact_digest="sha256:" + "f" * 64,
    )
    with pytest.raises(ValueError, match="request journal conflicts"):
        subject._open_controller_workspace(  # noqa: SLF001
            changed,
            subject._validate_controller_request_scalars(changed),  # noqa: SLF001
        )

    raced = replace(
        request,
        work_root=tmp_path / "raced-controller-work",
        output=tmp_path / "raced-controller-output.json",
    )
    conflicting_journal = _canonical({"request": "owner-conflict"})
    real_rename_noreplace = receipts._rename_noreplace  # noqa: SLF001

    def install_conflicting_root(source_path: Path, target_path: Path) -> None:
        if target_path != raced.work_root:
            real_rename_noreplace(source_path, target_path)
            return
        target_path.mkdir()
        (target_path / "controller-request.json").write_bytes(conflicting_journal)
        raise FileExistsError("simulated controller workspace race")

    monkeypatch.setattr(
        receipts,
        "_rename_noreplace",
        install_conflicting_root,
    )
    with pytest.raises(ValueError, match="request journal conflicts"):
        subject._open_controller_workspace(  # noqa: SLF001
            raced,
            subject._validate_controller_request_scalars(raced),  # noqa: SLF001
        )
    assert (raced.work_root / "controller-request.json").read_bytes() == (conflicting_journal)


def test_controller_resume_cleans_only_an_unpublished_interrupted_local_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    identity = tmp_path / "identity"
    identity.write_bytes(b"identity")
    authority_inputs = _authority_input_paths(tmp_path)
    bootstrap_receipt = tmp_path / "bootstrap-receipt.json"
    bootstrap_receipt.write_bytes(_canonical({"bootstrap": True}))
    monkeypatch.setenv(
        "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT",
        str(bootstrap_receipt),
    )
    request = subject.RecoveryControllerRequest(
        source_root=subject.ROOT,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=tmp_path / "controller-work",
        output=tmp_path / "controller-output.json",
        **authority_inputs,
    )
    scalars = subject._validate_controller_request_scalars(request)  # noqa: SLF001
    subject._open_controller_workspace(request, scalars)  # noqa: SLF001
    interrupted_runtime = target / "transaction" / "recovery-runtime"
    interrupted_runtime.mkdir(parents=True)
    (interrupted_runtime / "partial").write_bytes(b"partial")
    partial_capsule_input = request.work_root / "recovery-execution-closure.json"
    partial_capsule_input.write_bytes(b"partial")

    subject._recover_interrupted_local_stage(request, resuming=True)  # noqa: SLF001

    assert list(target.iterdir()) == []
    assert not partial_capsule_input.exists()

    (target / "transaction").mkdir()
    subject._recover_interrupted_local_stage(request, resuming=True)  # noqa: SLF001
    assert list(target.iterdir()) == []

    for name in (
        "recovery-execution-closure.json",
        "recovery-authority.json",
        "recovery-authority.json.sig",
    ):
        (request.work_root / name).write_bytes(b"partial")
    partial_capsule = request.work_root / "capsule-source"
    partial_capsule.mkdir()
    (partial_capsule / "partial").write_bytes(b"partial")
    interrupted_binding = request.work_root / "capsule-authority-binding"
    interrupted_binding.mkdir()
    (interrupted_binding / "old-authority").write_bytes(b"expired")
    interrupted_evidence = request.work_root / "normalized-evidence"
    interrupted_evidence.mkdir()
    (interrupted_evidence / "old-evidence").write_bytes(b"expired")
    interrupted_normalization = request.work_root / ".normalized-evidence-interrupted"
    interrupted_normalization.mkdir()
    interrupted_prepare = request.work_root / ".prepare-capsule-authority-interrupted"
    interrupted_prepare.mkdir()
    interrupted_write = request.work_root / ".recovery-execution-closure.json.tmp-interrupted"
    interrupted_write.write_bytes(b"partial")

    subject._recover_interrupted_local_stage(request, resuming=True)  # noqa: SLF001

    assert not partial_capsule.exists()
    assert not interrupted_binding.exists()
    assert not interrupted_evidence.exists()
    assert not interrupted_normalization.exists()
    assert not interrupted_prepare.exists()
    assert not interrupted_write.exists()
    assert not any(
        (request.work_root / name).exists()
        for name in (
            "recovery-execution-closure.json",
            "recovery-authority.json",
            "recovery-authority.json.sig",
        )
    )

    interrupted_runtime.mkdir(parents=True)
    (interrupted_runtime / "partial").write_bytes(b"partial")
    (request.work_root / "recovery-capsule-publication.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="published|publication"):
        subject._recover_interrupted_local_stage(  # noqa: SLF001
            request,
            resuming=True,
        )
    assert interrupted_runtime.exists()


def test_completed_controller_output_resumes_before_any_transport(
    tmp_path: Path,
) -> None:
    authority_inputs = _authority_input_paths(tmp_path)
    request = subject.RecoveryControllerRequest(
        source_root=subject.ROOT,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=tmp_path / "target",
        recovery_repository_id=304,
        identity_file=tmp_path / "identity",
        work_root=tmp_path / "work",
        output=tmp_path / "output.json",
        **authority_inputs,
    )
    result = {
        "schema": "kestrel.recovery_capsule_controller.v1",
        "source_sha": SOURCE_SHA,
        "candidate_manifest_digest": "sha256:" + "c" * 64,
        "promotion_run_id": RUN_ID,
        "target_workspace_root": str(request.target_workspace_root),
        "authorization_artifact": {
            "artifact_id": ARTIFACT_ID,
            "api_digest": "sha256:" + "d" * 64,
        },
        "dependency_artifact": {
            "run_id": RUN_ID + 1,
            "artifact_id": ARTIFACT_ID + 1,
            "api_digest": "sha256:" + "e" * 64,
        },
        "recovery_repository": {
            "full_name": subject.RECOVERY_REPOSITORY,
            "id": 304,
        },
        "capsule": {
            "tag": f"recovery-{RUN_ID}-1",
            "manifest_digest": "sha256:" + "1" * 64,
            "publication_receipt_digest": "sha256:" + "2" * 64,
            "verification_digest": "sha256:" + "3" * 64,
        },
        "prepare_authority": {
            "tag": f"release-prepare-authority-{RUN_ID}-1",
            "release_id": 4200,
            "publication_digest": "sha256:" + "4" * 64,
        },
        "evidence": {
            "source_bundle_digest": "sha256:" + "5" * 64,
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/recovery_capsule_controller.py",
            "provider": "owner-controller",
            "method": "exact-artifact-authority-bound-recovery-publication",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    request.output.write_bytes(_canonical(result))

    observed = subject._load_completed_controller_result(  # noqa: SLF001
        request,
        subject._validate_controller_request_scalars(request),  # noqa: SLF001
    )

    assert observed == result
    result["promotion_run_id"] = RUN_ID + 1
    request.output.write_bytes(_canonical(result))
    with pytest.raises(ValueError, match="conflict"):
        subject._load_completed_controller_result(  # noqa: SLF001
            request,
            subject._validate_controller_request_scalars(request),  # noqa: SLF001
        )


def test_authorization_artifact_replays_approval_at_authorized_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _identity, public_key = _signing_identity(tmp_path)
    approval = {
        "records": [
            {
                "environment": {"name": "release", "id": 9},
                "reviewer": {
                    "login": receipts.SIGNING_PRINCIPAL,
                    "id": 58918509,
                    "type": "User",
                },
                "state": "approved",
                "observed_record_digest": "sha256:" + "5" * 64,
            }
        ],
        "complete_response_digest": "sha256:" + "6" * 64,
    }
    transaction_raw = _transaction_authorization(approval)
    transaction_value = json.loads(transaction_raw)
    candidate = transaction_value["candidate"]
    root = tmp_path / "authorization"
    (root / "authority-evidence").mkdir(parents=True)
    (root / "candidate").mkdir()
    (root / "transaction-identity").mkdir()
    manifest_raw = _canonical({"candidate": "fixture"})
    (root / "candidate" / "candidate-manifest.json").write_bytes(manifest_raw)
    (root / "release-authorization.json").write_bytes(transaction_raw)
    (root / "authority-evidence" / "approval-history-observation.json").write_bytes(
        _source_observation(
            "approval-history-observation",
            approval,
            public_key=public_key,
        )
    )
    for relative in (
        "transaction-identity/dispatch-admission.json",
        "transaction-identity/dispatch-admission.json.sig",
        "transaction-identity/dispatch-admission-verification.json",
        "authority-evidence/github-admission-authority-verification.json",
        "authority-evidence/recovery-authority-verification.json",
    ):
        (root / relative).write_bytes(b"fixture")
    monkeypatch.setattr(
        receipts,
        "_candidate_from_manifest",
        lambda _raw: (candidate, REPOSITORY_ID),
    )
    monkeypatch.setattr(
        subject.candidates,
        "verify_candidate_bundle",
        lambda *_args, **_kwargs: {
            **candidate,
            "candidate_run": {
                "run_id": candidate["candidate_run_id"],
                "run_attempt": 1,
            },
        },
    )

    material = subject.validate_authorization_artifact(
        root=root,
        source_root=subject.ROOT,
        source_sha=SOURCE_SHA,
        promotion_run_id=RUN_ID,
        candidate_manifest_digest="sha256:" + "c" * 64,
        _clock=lambda: NOW + timedelta(days=1),
    )

    assert material.transaction == transaction_value


def _recovery_authority_verification_record(
    *, observed_at: datetime, expires_at: datetime
) -> bytes:
    authority = {
        "schema": receipts.RECOVERY_AUTHORITY_SCHEMA,
        "repository": {"full_name": subject.RECOVERY_REPOSITORY, "id": 304},
        "observed_at": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    receipt = _canonical(authority)
    signature = b"signed recovery authority"
    owner_keys = b"embedded owner signing keys"
    return _canonical(
        {
            "schema": "kestrel.recovery_repository_authority_verification.v1",
            "authority_schema": receipts.RECOVERY_AUTHORITY_SCHEMA,
            "authority": authority,
            "receipt_digest": _sha256(receipt),
            "signature_digest": _sha256(signature),
            "receipt_base64": base64.b64encode(receipt).decode("ascii"),
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "owner_signing_keys_observation_base64": base64.b64encode(owner_keys).decode("ascii"),
            "signing_key_fingerprint": "sha256:" + "f" * 64,
            "verified_at": (observed_at + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "validation_status": "validated",
        }
    )


def test_recovery_authority_replays_at_transaction_authorization_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = NOW - timedelta(minutes=1)
    expires_at = NOW + timedelta(minutes=4)
    record = _recovery_authority_verification_record(
        observed_at=observed_at,
        expires_at=expires_at,
    )
    monkeypatch.setattr(
        receipts,
        "validate_recovery_repository_authority",
        lambda value: dict(value),
    )
    monkeypatch.setattr(
        receipts,
        "_offline_owner_signing_key",
        lambda *_args, **_kwargs: ("owner key", "sha256:" + "f" * 64),
    )
    monkeypatch.setattr(receipts, "verify_detached_signature", lambda **_kwargs: True)
    monkeypatch.setattr(
        receipts,
        "source_observation_body_for_contract",
        lambda *_args, **_kwargs: b"{}",
    )

    material = subject._validate_recovery_authority_record(  # noqa: SLF001
        raw=record,
        fresh_owner_signing_keys_observation=b"fresh owner signing keys",
        expected_repository_id=304,
        source_registry={},
        authorization_time=NOW,
        _clock=lambda: NOW + timedelta(days=1),
    )

    assert material.authority["repository"]["id"] == 304  # type: ignore[index]


def test_recovery_authority_rejects_transaction_outside_its_signed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = NOW - timedelta(minutes=1)
    expires_at = NOW + timedelta(minutes=4)
    record = _recovery_authority_verification_record(
        observed_at=observed_at,
        expires_at=expires_at,
    )
    monkeypatch.setattr(
        receipts,
        "validate_recovery_repository_authority",
        lambda value: dict(value),
    )
    monkeypatch.setattr(
        receipts,
        "_offline_owner_signing_key",
        lambda *_args, **_kwargs: ("owner key", "sha256:" + "f" * 64),
    )
    monkeypatch.setattr(receipts, "verify_detached_signature", lambda **_kwargs: True)
    monkeypatch.setattr(
        receipts,
        "source_observation_body_for_contract",
        lambda *_args, **_kwargs: b"{}",
    )

    with pytest.raises(ValueError, match="authorization time"):
        subject._validate_recovery_authority_record(  # noqa: SLF001
            raw=record,
            fresh_owner_signing_keys_observation=b"fresh owner signing keys",
            expected_repository_id=304,
            source_registry={},
            authorization_time=expires_at,
            _clock=lambda: NOW + timedelta(days=1),
        )


def _current_recovery_authority_sources(
    *,
    public_key: str,
    mutation: str | None = None,
    captured_at: datetime = NOW,
) -> dict[str, bytes]:
    vectors = json.loads(
        (
            subject.ROOT / "tests/fixtures/release-control/v3/positive-contract-vectors.json"
        ).read_bytes()
    )
    expected = next(
        vector["record"]
        for vector in vectors["vectors"]
        if vector["name"] == "recovery-repository-authority"
    )
    timestamp = captured_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    owner_snapshot = {
        "schema": "kestrel.recovery_repository_authority_owner.v1",
        "repository": expected["repository"],
        "owner": expected["owner"],
        "collaborators": expected["collaborators"],
        "invitations": expected["invitations"],
        "deploy_keys": expected["deploy_keys"],
        "installed_apps": expected["installed_apps"],
        "workflows": expected["workflows"],
        "packages": expected["packages"],
        "credentials": expected["credentials"],
        "captured_at": timestamp,
        "complete": True,
    }
    owner_snapshot["credentials"][0]["expires_at"] = (captured_at + timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if mutation == "collaborator":
        owner_snapshot["collaborators"].append(
            {
                "login": "unexpected-writer",
                "id": 999,
                "node_id": "U_unexpected",
                "type": "User",
                "role_name": "write",
                "permissions": {
                    "admin": False,
                    "maintain": False,
                    "push": True,
                    "triage": True,
                    "pull": True,
                },
            }
        )
    controller_context = {
        "schema": "kestrel.recovery_repository_authority_controller_context.v1",
        "owner": {
            "id": expected["owner"]["id"],
            "login": expected["owner"]["login"],
        },
        "acknowledgement": {
            "acknowledged_by_id": expected["owner"]["id"],
            "acknowledged_by_login": expected["owner"]["login"],
            "begins_at": (captured_at - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (captured_at + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "statement": (
                "I hold exclusive owner control of the immutable recovery "
                "repository through this bounded publication window."
            ),
        },
        "captured_at": timestamp,
        "complete": True,
    }
    bodies = {
        "recovery-owner-dashboard": owner_snapshot,
        "recovery-repository-rest": {
            "id": expected["repository"]["id"],
            "full_name": expected["repository"]["full_name"],
            "owner": expected["owner"],
        },
        "recovery-immutable-releases-rest": expected["immutable_releases"],
        "controller-context": controller_context,
    }
    registry = json.loads((subject.ROOT / "release-control-source-registry.json").read_bytes())
    identity = _canonical({"login": receipts.SIGNING_PRINCIPAL})
    sources = {
        name: _canonical(
            receipts.capture_source(
                registry=registry,
                receipt_schema=receipts.RECOVERY_AUTHORITY_SCHEMA,
                phase="authority",
                mode="operational",
                name=name,
                raw_input=receipts.canonical_external_json_bytes(body),
                identity_observation=identity,
                _clock=lambda: captured_at,
            )
        )
        for name, body in bodies.items()
    }
    sources["owner-signing-keys-observation"] = _source_observation(
        "owner-signing-keys-observation",
        {},
        public_key=public_key,
        captured_at=captured_at,
    )
    return sources


def test_current_recovery_authority_is_signed_by_the_registered_owner_key(
    tmp_path: Path,
) -> None:
    identity, public_key = _signing_identity(tmp_path)
    sources = _current_recovery_authority_sources(public_key=public_key)

    material = subject.create_current_recovery_authority(
        owner_authority_snapshot=sources["recovery-owner-dashboard"],
        repository_observation=sources["recovery-repository-rest"],
        immutable_releases_observation=sources["recovery-immutable-releases-rest"],
        controller_context=sources["controller-context"],
        identity_file=identity,
        fresh_owner_signing_keys_observation=sources["owner-signing-keys-observation"],
        expected_repository_id=304,
        source_registry=subject._source_registry(subject.ROOT),  # noqa: SLF001
        _clock=lambda: NOW,
    )

    assert material.authority["repository"] == {
        "full_name": subject.RECOVERY_REPOSITORY,
        "id": 304,
    }
    assert receipts.signature_public_key_fingerprint(material.signature) == (
        receipts.ssh_public_key_fingerprint(public_key)
    )


def test_recovery_authority_generation_copies_renewable_slots_and_preserves_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _isolated_source_root(tmp_path)
    identity, public_key = _signing_identity(tmp_path)
    owner_keys = _source_observation("owner-signing-keys-observation", {}, public_key=public_key)
    target = tmp_path / "target"
    target.mkdir()
    authority_paths = _authority_input_paths(tmp_path)

    def replace_slots(sources: dict[str, bytes]) -> None:
        names = {
            "current_recovery_owner_authority_snapshot": "recovery-owner-dashboard",
            "current_recovery_repository_observation": "recovery-repository-rest",
            "current_recovery_immutable_releases_observation": ("recovery-immutable-releases-rest"),
            "current_recovery_controller_context": "controller-context",
        }
        for argument, path in authority_paths.items():
            path.write_bytes(sources[names[argument]])

    replace_slots(_current_recovery_authority_sources(public_key=public_key))
    bootstrap_receipt = tmp_path / "bootstrap-receipt.json"
    bootstrap_receipt.write_bytes(_canonical({"bootstrap": True}))
    monkeypatch.setenv(
        "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT",
        str(bootstrap_receipt),
    )
    request = subject.RecoveryControllerRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=tmp_path / "work",
        output=tmp_path / "output.json",
        **authority_paths,
    )
    subject._open_controller_workspace(  # noqa: SLF001
        request,
        subject._validate_controller_request_scalars(request),  # noqa: SLF001
    )

    generation = subject._load_or_create_recovery_authority_generation(  # noqa: SLF001
        request=request,
        expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
        current_owner_signing_keys_observation=owner_keys,
        expected_repository_id=304,
        _clock=lambda: NOW,
    )

    assert generation.authority.authority["repository"]["id"] == 304  # type: ignore[index]
    copied_context = generation.root / "inputs/controller-context.json"
    expected_context = copied_context.read_bytes()
    request.current_recovery_controller_context.write_bytes(b"substituted slot")
    assert copied_context.read_bytes() == expected_context

    replace_slots(
        _current_recovery_authority_sources(public_key=public_key, mutation="collaborator")
    )
    with pytest.raises(ValueError, match="writer|authority|collaborator"):
        subject._load_or_create_recovery_authority_generation(  # noqa: SLF001
            request=request,
            expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
            current_owner_signing_keys_observation=owner_keys,
            expected_repository_id=304,
            _clock=lambda: NOW,
        )
    failed_roots = tuple((request.work_root / "recovery-authority-generations/failed").iterdir())
    assert len(failed_roots) == 1
    assert (failed_roots[0] / "failure.json").is_file()
    assert (failed_roots[0] / "inputs/recovery-owner-dashboard.json").is_file()


def test_recovery_authority_generation_renews_after_the_five_minute_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _isolated_source_root(tmp_path)
    identity, public_key = _signing_identity(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    authority_paths = _authority_input_paths(tmp_path)
    bootstrap_receipt = tmp_path / "bootstrap-receipt.json"
    bootstrap_receipt.write_bytes(_canonical({"bootstrap": True}))
    monkeypatch.setenv(
        "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT",
        str(bootstrap_receipt),
    )
    request = subject.RecoveryControllerRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=tmp_path / "work",
        output=tmp_path / "output.json",
        **authority_paths,
    )
    subject._open_controller_workspace(  # noqa: SLF001
        request,
        subject._validate_controller_request_scalars(request),  # noqa: SLF001
    )

    def replace_slots(values: dict[str, bytes]) -> None:
        mapping = {
            request.current_recovery_owner_authority_snapshot: values["recovery-owner-dashboard"],
            request.current_recovery_repository_observation: values["recovery-repository-rest"],
            request.current_recovery_immutable_releases_observation: values[
                "recovery-immutable-releases-rest"
            ],
            request.current_recovery_controller_context: values["controller-context"],
        }
        for path, raw in mapping.items():
            path.write_bytes(raw)

    initial = _current_recovery_authority_sources(public_key=public_key, captured_at=NOW)
    replace_slots(initial)
    first = subject._load_or_create_recovery_authority_generation(  # noqa: SLF001
        request=request,
        expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
        current_owner_signing_keys_observation=initial["owner-signing-keys-observation"],
        expected_repository_id=304,
        _clock=lambda: NOW,
    )
    first_receipt = first.authority.receipt
    renewed_at = NOW + timedelta(minutes=6)
    renewed = _current_recovery_authority_sources(public_key=public_key, captured_at=renewed_at)
    replace_slots(renewed)
    second = subject._load_or_create_recovery_authority_generation(  # noqa: SLF001
        request=request,
        expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
        current_owner_signing_keys_observation=renewed["owner-signing-keys-observation"],
        expected_repository_id=304,
        _clock=lambda: renewed_at,
    )

    assert second.generation_id != first.generation_id
    assert first.authority.receipt == first_receipt
    assert (first.root / "authority.json").read_bytes() == first_receipt
    assert (second.root / "authority.json").read_bytes() == second.authority.receipt


def test_stage_mutation_grant_stays_within_snapshot_and_exact_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _isolated_source_root(tmp_path)
    identity, public_key = _signing_identity(tmp_path)
    owner_keys = _source_observation("owner-signing-keys-observation", {}, public_key=public_key)
    vectors = json.loads(
        (
            subject.ROOT / "tests/fixtures/release-control/v3/positive-contract-vectors.json"
        ).read_bytes()
    )
    authority = next(
        json.loads(json.dumps(vector["record"]))
        for vector in vectors["vectors"]
        if vector["name"] == "recovery-repository-authority"
    )
    authority["observed_at"] = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    authority["expires_at"] = (NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    authority["maintenance_window_acknowledgement"]["expires_at"] = (
        NOW + timedelta(minutes=30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    authority["maintenance_window_acknowledgement"]["begins_at"] = (
        NOW - timedelta(minutes=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    for snapshot in authority["source_snapshots"]:
        snapshot["captured_at"] = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    generation_root = tmp_path / "generation"
    generation_root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    authority_paths = _authority_input_paths(tmp_path)
    bootstrap_receipt = tmp_path / "bootstrap-receipt.json"
    bootstrap_receipt.write_bytes(_canonical({"bootstrap": True}))
    monkeypatch.setenv(
        "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT",
        str(bootstrap_receipt),
    )
    request = subject.RecoveryControllerRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=tmp_path / "work",
        output=tmp_path / "output.json",
        **authority_paths,
    )
    subject._open_controller_workspace(  # noqa: SLF001
        request,
        subject._validate_controller_request_scalars(request),  # noqa: SLF001
    )
    token = b"reader-token"
    scope_authority = _canonical(
        {
            "credential_id": "credential-recovery-reader",
            "purpose": "recovery_reader",
            "token_fingerprint": _sha256(token),
            "expires_at": (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    historical_runtime = b"historical runtime"
    reader_material = subject.RecoveryReaderMaterial(
        scope_authority=scope_authority,
        scope_signature=b"signature",
        owner_signing_keys_observation=owner_keys,
        identity_probe=b"identity",
        endpoint_probes=b"probes",
        runtime_verification=historical_runtime,
    )
    runtime = {
        "schema": receipts.RUNTIME_CREDENTIAL_SCHEMA,
        "credential_id": "credential-recovery-reader",
        "purpose": "recovery_reader",
        "token_fingerprint": _sha256(token),
        "scope_authority_digest": _sha256(scope_authority),
        "validation_status": "validated",
    }
    credential = authority["credentials"][0]
    credential.update(
        {
            "id": "credential-recovery-reader",
            "scope_authority_digest": _sha256(scope_authority),
            "runtime_verification_digest": _sha256(historical_runtime),
            "expires_at": (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    authority_raw = _canonical(authority)
    authority_signature = receipts.sign_receipt_detached(
        receipt=authority_raw,
        identity_file=identity,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    generation = subject.RecoveryAuthorityGeneration(
        generation_id="sha256:" + "1" * 64,
        root=generation_root,
        authority=subject.RecoveryAuthorityMaterial(
            authority=authority,
            receipt=authority_raw,
            signature=authority_signature,
        ),
        input_digests={},
    )
    transaction_raw = _canonical({"transaction": "exact"})
    scope = {
        "stage": "capsule_publish",
        "release": {
            "repository": subject.RECOVERY_REPOSITORY,
            "repository_id": 304,
            "tag": f"recovery-{RUN_ID}-1",
            "name": f"Kestrel recovery capsule recovery-{RUN_ID}-1",
            "body_sha256": _sha256(
                (
                    f"Kestrel recovery capsule recovery-{RUN_ID}-1\n\n"
                    "Kestrel-Recovery-Capsule: sha256:" + "9" * 64
                ).encode()
            ),
        },
        "assets": [
            {
                "name": "recovery-bootstrap.py",
                "size_bytes": 8,
                "sha256": "sha256:" + "8" * 64,
            },
            {
                "name": "recovery-capsule-manifest.json",
                "size_bytes": 9,
                "sha256": "sha256:" + "9" * 64,
            },
            {
                "name": "recovery-capsule.tar",
                "size_bytes": 10,
                "sha256": "sha256:" + "a" * 64,
            },
        ],
        "allowed_operations": [
            "create_draft_release",
            "publish_immutable_release",
            "upload:recovery-bootstrap.py",
            "upload:recovery-capsule-manifest.json",
            "upload:recovery-capsule.tar",
        ],
    }

    grant = subject._load_or_create_stage_mutation_grant(  # noqa: SLF001
        request=request,
        expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
        generation=generation,
        stage_scope=scope,
        transaction_authorization=transaction_raw,
        reader_material=reader_material,
        bound_reader_runtime_verification=runtime,
        recovery_reader_token=token,
        current_owner_signing_keys_observation=owner_keys,
        _clock=lambda: NOW,
    )
    owner_keys_at_four = _source_observation(
        "owner-signing-keys-observation",
        {},
        public_key=public_key,
        captured_at=NOW + timedelta(minutes=4),
    )

    subject._validate_stage_mutation_grant(  # noqa: SLF001
        material=grant,
        request=request,
        expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
        generation=generation,
        stage_scope=scope,
        transaction_authorization=transaction_raw,
        reader_material=reader_material,
        bound_reader_runtime_verification=runtime,
        recovery_reader_token=token,
        current_owner_signing_keys_observation=owner_keys_at_four,
        _clock=lambda: NOW + timedelta(minutes=4),
    )
    changed_scope = json.loads(json.dumps(scope))
    changed_scope["assets"][0]["size_bytes"] = 11
    with pytest.raises(ValueError, match="scope|grant"):
        subject._validate_stage_mutation_grant(  # noqa: SLF001
            material=grant,
            request=request,
            expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
            generation=generation,
            stage_scope=changed_scope,
            transaction_authorization=transaction_raw,
            reader_material=reader_material,
            bound_reader_runtime_verification=runtime,
            recovery_reader_token=token,
            current_owner_signing_keys_observation=owner_keys_at_four,
            _clock=lambda: NOW + timedelta(minutes=4),
        )
    with pytest.raises(ValueError, match="expired"):
        subject._validate_stage_mutation_grant(  # noqa: SLF001
            material=grant,
            request=request,
            expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
            generation=generation,
            stage_scope=scope,
            transaction_authorization=transaction_raw,
            reader_material=reader_material,
            bound_reader_runtime_verification=runtime,
            recovery_reader_token=token,
            current_owner_signing_keys_observation=_source_observation(
                "owner-signing-keys-observation",
                {},
                public_key=public_key,
                captured_at=NOW + timedelta(minutes=6),
            ),
            _clock=lambda: NOW + timedelta(minutes=6),
        )


def test_current_recovery_authority_rejects_a_new_writer_before_publication(
    tmp_path: Path,
) -> None:
    identity, public_key = _signing_identity(tmp_path)
    sources = _current_recovery_authority_sources(
        public_key=public_key,
        mutation="collaborator",
    )

    with pytest.raises(ValueError, match="writer"):
        subject.create_current_recovery_authority(
            owner_authority_snapshot=sources["recovery-owner-dashboard"],
            repository_observation=sources["recovery-repository-rest"],
            immutable_releases_observation=sources["recovery-immutable-releases-rest"],
            controller_context=sources["controller-context"],
            identity_file=identity,
            fresh_owner_signing_keys_observation=sources["owner-signing-keys-observation"],
            expected_repository_id=304,
            source_registry=subject._source_registry(subject.ROOT),  # noqa: SLF001
            _clock=lambda: NOW,
        )


def test_current_recovery_authority_rejects_an_unregistered_private_key(
    tmp_path: Path,
) -> None:
    identity, _unused_public_key = _signing_identity(tmp_path)
    registered_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 0, -1)))
    registered_public_key = (
        registered_key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        .decode("ascii")
    )
    sources = _current_recovery_authority_sources(public_key=registered_public_key)

    with pytest.raises(ValueError, match="signature|fingerprint"):
        subject.create_current_recovery_authority(
            owner_authority_snapshot=sources["recovery-owner-dashboard"],
            repository_observation=sources["recovery-repository-rest"],
            immutable_releases_observation=sources["recovery-immutable-releases-rest"],
            controller_context=sources["controller-context"],
            identity_file=identity,
            fresh_owner_signing_keys_observation=sources["owner-signing-keys-observation"],
            expected_repository_id=304,
            source_registry=subject._source_registry(subject.ROOT),  # noqa: SLF001
            _clock=lambda: NOW,
        )


def test_current_recovery_authority_resumes_only_a_fresh_exact_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "work"
    root.mkdir()
    authority = {"schema": receipts.RECOVERY_AUTHORITY_SCHEMA, "value": "exact"}
    receipt = _canonical(authority)
    signature = b"signature"
    (root / "current-recovery-authority.json").write_bytes(receipt)
    (root / "current-recovery-authority.json.sig").write_bytes(signature)

    def verify(**kwargs: object) -> tuple[receipts.JSONObject, datetime, str]:
        assert kwargs["receipt"] == receipt
        if kwargs["signature"] != signature:
            raise receipts.ReleaseControlError("authority signature conflicts")
        assert kwargs["owner_signing_keys_observation"] == b"owner keys"
        return authority, NOW, "sha256:" + "f" * 64

    monkeypatch.setattr(receipts, "_verify_signed_authority", verify)

    material = subject._load_current_recovery_authority(  # noqa: SLF001
        work_root=root,
        owner_signing_keys_observation=b"owner keys",
        _clock=lambda: NOW,
    )

    assert material.receipt == receipt
    assert material.signature == signature
    (root / "current-recovery-authority.json.sig").write_bytes(b"substituted")
    with pytest.raises(ValueError, match="signature|conflict"):
        subject._load_current_recovery_authority(  # noqa: SLF001
            work_root=root,
            owner_signing_keys_observation=b"owner keys",
            _clock=lambda: NOW,
        )


def test_current_recovery_authority_joins_the_live_reader_proof() -> None:
    scope = {
        "schema": receipts.CREDENTIAL_SCOPE_SCHEMA,
        "credential_id": "credential-recovery-reader",
        "purpose": "recovery_reader",
        "token_fingerprint": "sha256:" + "a" * 64,
        "expires_at": (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    historical_runtime = {
        "schema": receipts.RUNTIME_CREDENTIAL_SCHEMA,
        "credential_id": scope["credential_id"],
        "purpose": "recovery_reader",
        "token_fingerprint": scope["token_fingerprint"],
        "scope_authority_digest": _sha256(_canonical(scope)),
        "verified_at": (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "validation_status": "validated",
    }
    current_runtime = {
        **historical_runtime,
        "verified_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    material = subject.RecoveryReaderMaterial(
        scope_authority=_canonical(scope),
        scope_signature=b"signature",
        owner_signing_keys_observation=b"owner keys",
        identity_probe=b"identity",
        endpoint_probes=b"probes",
        runtime_verification=_canonical(historical_runtime),
    )
    authority = {
        "credentials": [
            {
                "kind": "pat",
                "id": scope["credential_id"],
                "name": "release recovery reader",
                "purpose": "recovery_reader",
                "capabilities": ["repository_read"],
                "active": True,
                "expires_at": scope["expires_at"],
                "scope_authority_digest": _sha256(material.scope_authority),
                "runtime_verification_digest": _sha256(material.runtime_verification),
            }
        ]
    }

    subject._require_current_reader_authority_binding(  # noqa: SLF001
        recovery_authority=authority,
        reader_material=material,
        current_runtime_verification=current_runtime,
        _clock=lambda: NOW,
    )

    authority["credentials"][0]["runtime_verification_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="runtime verification"):
        subject._require_current_reader_authority_binding(  # noqa: SLF001
            recovery_authority=authority,
            reader_material=material,
            current_runtime_verification=current_runtime,
            _clock=lambda: NOW,
        )


def test_mutation_gate_rechecks_authority_before_source_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = tmp_path / "identity"
    identity.write_bytes(b"private")
    identity.chmod(0o600)
    request = subject.RecoveryControllerRequest(
        source_root=subject.ROOT,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=tmp_path / "target",
        recovery_repository_id=304,
        identity_file=identity,
        current_recovery_owner_authority_snapshot=tmp_path / "owner",
        current_recovery_repository_observation=tmp_path / "repository",
        current_recovery_immutable_releases_observation=tmp_path / "immutable",
        current_recovery_controller_context=tmp_path / "context",
        work_root=tmp_path / "work",
        output=tmp_path / "output",
    )
    material = subject.RecoveryReaderMaterial(
        scope_authority=b"scope",
        scope_signature=b"signature",
        owner_signing_keys_observation=b"historical owner keys",
        identity_probe=b"identity",
        endpoint_probes=b"probes",
        runtime_verification=b"historical runtime",
    )
    events: list[str] = []
    monkeypatch.setattr(
        subject,
        "capture_fresh_owner_signing_keys",
        lambda **_kwargs: events.append("owner-keys") or b"current owner keys",
    )
    monkeypatch.setattr(
        subject,
        "verify_recovery_reader_credential",
        lambda **_kwargs: events.append("reader-proof") or {},
    )

    def reject_expired(**_kwargs: object) -> object:
        events.append("authority-freshness")
        raise receipts.ReleaseControlError("authority receipt is not currently fresh")

    monkeypatch.setattr(subject, "_load_or_create_recovery_authority_generation", reject_expired)
    monkeypatch.setattr(
        subject,
        "_require_clean_source_identity",
        lambda _request: pytest.fail("source check ran after a failed authority gate"),
    )

    with pytest.raises(ValueError, match="not currently fresh"):
        subject._authorize_current_stage_mutation(  # noqa: SLF001
            request=request,
            expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
            stage_scope={},
            reader_material=material,
            transaction_authorization=b"transaction",
            transaction_authorization_record={},
            expected_repository_id=304,
            owner_read_api=object(),  # type: ignore[arg-type]
            recovery_reader_api=object(),  # type: ignore[arg-type]
            recovery_reader_token=b"reader-token",
            _clock=lambda: NOW,
        )

    assert events == [
        "owner-keys",
        "reader-proof",
        "authority-freshness",
    ]


def test_mutation_gate_uses_a_new_live_reader_proof_for_the_stage_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = tmp_path / "identity"
    identity.write_bytes(b"private")
    identity.chmod(0o600)
    request = subject.RecoveryControllerRequest(
        source_root=subject.ROOT,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=tmp_path / "target",
        recovery_repository_id=304,
        identity_file=identity,
        current_recovery_owner_authority_snapshot=tmp_path / "owner",
        current_recovery_repository_observation=tmp_path / "repository",
        current_recovery_immutable_releases_observation=tmp_path / "immutable",
        current_recovery_controller_context=tmp_path / "context",
        work_root=tmp_path / "work",
        output=tmp_path / "output",
    )
    material = subject.RecoveryReaderMaterial(
        scope_authority=b"scope",
        scope_signature=b"signature",
        owner_signing_keys_observation=b"historical owner keys",
        identity_probe=b"identity",
        endpoint_probes=b"probes",
        runtime_verification=b"historical runtime",
    )
    current_runtime = {"proof": "new-live-proof"}
    generation = subject.RecoveryAuthorityGeneration(
        generation_id="sha256:" + "1" * 64,
        root=tmp_path / "generation",
        authority=subject.RecoveryAuthorityMaterial(
            authority={"repository": {"id": 304}},
            receipt=b"authority",
            signature=b"signature",
        ),
        input_digests={},
    )
    events: list[str] = []
    monkeypatch.setattr(
        subject,
        "capture_fresh_owner_signing_keys",
        lambda **_kwargs: events.append("owner") or b"new owner keys",
    )
    monkeypatch.setattr(
        subject,
        "verify_recovery_reader_credential",
        lambda **_kwargs: events.append("reader") or current_runtime,
    )
    monkeypatch.setattr(
        subject,
        "_load_or_create_recovery_authority_generation",
        lambda **_kwargs: events.append("generation") or generation,
    )

    def join(**kwargs: object) -> None:
        assert kwargs["current_runtime_verification"] is current_runtime
        events.append("join")

    monkeypatch.setattr(subject, "_require_current_reader_authority_binding", join)
    monkeypatch.setattr(
        subject,
        "_require_clean_source_identity",
        lambda _request: events.append("source"),
    )
    sentinel = object()

    def grant(**kwargs: object) -> object:
        assert kwargs["bound_reader_runtime_verification"] is current_runtime
        assert kwargs["current_owner_signing_keys_observation"] == b"new owner keys"
        assert kwargs["generation"] is generation
        events.append("grant")
        return sentinel

    monkeypatch.setattr(subject, "_load_or_create_stage_mutation_grant", grant)

    result = subject._authorize_current_stage_mutation(  # noqa: SLF001
        request=request,
        expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
        stage_scope={"stage": "exact"},
        reader_material=material,
        transaction_authorization=b"transaction",
        transaction_authorization_record={},
        expected_repository_id=304,
        owner_read_api=object(),  # type: ignore[arg-type]
        recovery_reader_api=object(),  # type: ignore[arg-type]
        recovery_reader_token=b"reader-token",
        _clock=lambda: NOW,
    )

    assert result is sentinel
    assert events == ["owner", "reader", "generation", "join", "source", "grant"]


def test_mutation_gate_rejects_signing_identity_substitution_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = tmp_path / "identity"
    identity.write_bytes(b"private")
    identity.chmod(0o600)
    request = subject.RecoveryControllerRequest(
        source_root=subject.ROOT,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=tmp_path / "target",
        recovery_repository_id=304,
        identity_file=identity,
        current_recovery_owner_authority_snapshot=tmp_path / "owner",
        current_recovery_repository_observation=tmp_path / "repository",
        current_recovery_immutable_releases_observation=tmp_path / "immutable",
        current_recovery_controller_context=tmp_path / "context",
        work_root=tmp_path / "work",
        output=tmp_path / "output",
    )
    expected_identity_digest = subject._path_sha256(identity)  # noqa: SLF001
    identity.write_bytes(b"substituted")
    monkeypatch.setattr(
        subject,
        "capture_fresh_owner_signing_keys",
        lambda **_kwargs: pytest.fail("transport ran after identity substitution"),
    )

    with pytest.raises(ValueError, match="signing identity.*changed"):
        subject._authorize_current_stage_mutation(  # noqa: SLF001
            request=request,
            expected_identity_digest=expected_identity_digest,
            stage_scope={},
            reader_material=subject.RecoveryReaderMaterial(
                scope_authority=b"scope",
                scope_signature=b"signature",
                owner_signing_keys_observation=b"owner",
                identity_probe=b"identity",
                endpoint_probes=b"probes",
                runtime_verification=b"runtime",
            ),
            transaction_authorization=b"transaction",
            transaction_authorization_record={},
            expected_repository_id=304,
            owner_read_api=object(),  # type: ignore[arg-type]
            recovery_reader_api=object(),  # type: ignore[arg-type]
            recovery_reader_token=b"reader-token",
            _clock=lambda: NOW,
        )


def test_signing_identity_owner_privacy_is_posix_only() -> None:
    # Regression guard for F3: on Windows CPython reports group/other read bits
    # set for every regular file (NTFS ACLs, not POSIX modes, govern access, and
    # chmod(0o600) is a no-op for those bits). The owner-privacy check must be
    # enforced only on POSIX, or every legitimate Windows identity file is
    # falsely rejected with "signing identity bytes changed".
    windows_like_st_mode = 0o100000 | 0o644  # regular file, group/other readable
    assert (
        subject._owner_privacy_group_or_other_bits(  # noqa: SLF001
            windows_like_st_mode, platform_name="nt"
        )
        == 0
    )
    assert (
        subject._owner_privacy_group_or_other_bits(  # noqa: SLF001
            windows_like_st_mode, platform_name="posix"
        )
        == 0o644 & 0o077
    )


def test_fresh_recovery_sources_resume_only_an_exact_validated_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fresh-sources"
    root.mkdir()
    repository = {
        "id": 304,
        "full_name": subject.RECOVERY_REPOSITORY,
        "private": True,
        "visibility": "private",
        "archived": False,
        "disabled": False,
        "owner": {
            "login": receipts.SIGNING_PRINCIPAL,
            "id": 58918509,
            "type": "User",
        },
    }
    owner_keys = _canonical({"owner": "keys"})
    repository_observation = _canonical({"repository": "observation"})
    (root / "owner-signing-keys-observation.json").write_bytes(owner_keys)
    (root / "recovery-repository-observation.json").write_bytes(repository_observation)
    (root / "recovery-repository.json").write_bytes(_canonical(repository))
    monkeypatch.setattr(
        receipts,
        "source_observation_body_for_contract",
        lambda *_args, **_kwargs: b"{}",
    )

    resumed = subject._load_fresh_recovery_sources(  # noqa: SLF001
        source_root=subject.ROOT,
        output_root=root,
        _clock=lambda: NOW,
    )

    assert resumed.repository_id == 304
    assert resumed.owner_signing_keys_observation == owner_keys
    (root / "unexpected.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="inventory"):
        subject._load_fresh_recovery_sources(  # noqa: SLF001
            source_root=subject.ROOT,
            output_root=root,
            _clock=lambda: NOW,
        )


def test_unsigned_verification_recaptures_stale_signing_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "signing-sources"
    root.mkdir()
    (root / "stale.json").write_bytes(b"stale")
    owner_keys = b"fresh owner keys"
    observed: list[str] = []

    def capture(**kwargs: object) -> subject.FreshRecoverySources:
        output_root = kwargs["output_root"]
        assert isinstance(output_root, Path)
        assert not output_root.exists()
        observed.append("recaptured")
        output_root.mkdir()
        return subject.FreshRecoverySources(
            repository_id=304,
            repository_body=b"repository",
            repository_observation=b"observation",
            owner_signing_keys_observation=owner_keys,
        )

    monkeypatch.setattr(subject, "capture_fresh_recovery_sources", capture)

    sources = subject._capture_unsigned_signing_sources(  # noqa: SLF001
        source_root=subject.ROOT,
        output_root=root,
        expected_repository_id=304,
        api=object(),  # type: ignore[arg-type]
        _clock=lambda: NOW + timedelta(minutes=10),
    )

    assert observed == ["recaptured"]
    assert sources.owner_signing_keys_observation == owner_keys


def test_local_capsule_stage_resumes_only_exact_authority_bound_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root = tmp_path / "work"
    work_root.mkdir()
    capsule_root = work_root / "capsule-source"
    capsule_root.mkdir()
    capsule_scripts = capsule_root / "scripts"
    capsule_scripts.mkdir()
    (capsule_scripts / "bootstrap_recovery.py").write_bytes(b"bootstrap")
    capsule_payload = capsule_root / "payload"
    capsule_payload.write_bytes(b"payload")
    preparation_root = work_root / "production-preparation"
    preparation_root.mkdir()
    closure_raw = _canonical(
        {
            "schema": "kestrel.recovery_execution_closure.v1",
            "validation_status": "validated",
        }
    )
    manifest_raw = _canonical(
        {
            "recovery_repository": {
                "authority_receipt_digest": _sha256(b"authority"),
                "authority_signature_digest": _sha256(b"signature"),
            }
        }
    )
    for name, raw in (
        ("recovery-execution-closure.json", closure_raw),
        ("recovery-authority.json", b"authority"),
        ("recovery-authority.json.sig", b"signature"),
    ):
        (work_root / name).write_bytes(raw)
    (preparation_root / "candidate-archive.tar").write_bytes(b"candidate")
    environment_raw = _canonical({"environment": True})
    (preparation_root / "recovery-environment-manifest.json").write_bytes(environment_raw)
    (preparation_root / "controller-preparation.json").write_bytes(
        _canonical({"preparation": True})
    )
    preparation = subject.PreparedProductionCapsule(
        root=preparation_root,
        candidate_archive=preparation_root / "candidate-archive.tar",
        environment_manifest_raw=environment_raw,
        environment_manifest={"environment": True},
        sys_path=("/target/site-packages",),
        runtime={"implementation": "CPython", "version": "3.11.14", "abi": "cp311"},
        python_sha256="sha256:" + "1" * 64,
        base_library_root=Path("/target/base/lib"),
        receipt=_canonical({"preparation": True}),
    )
    authority = subject.RecoveryAuthorityMaterial(
        authority={"repository": {"id": 304}},
        receipt=b"authority",
        signature=b"signature",
    )
    sources = subject.FreshRecoverySources(
        repository_id=304,
        repository_body=b"repository",
        repository_observation=b"observation",
        owner_signing_keys_observation=b"owner keys",
    )
    monkeypatch.setattr(
        receipts,
        "_validate_schema",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        receipts,
        "_offline_owner_signing_key",
        lambda *_args, **_kwargs: ("key", "sha256:" + "f" * 64),
    )
    monkeypatch.setattr(
        receipts,
        "verify_recovery_capsule_root",
        lambda *_args, **_kwargs: ({}, manifest_raw),
    )
    monkeypatch.setattr(
        receipts,
        "deterministic_recovery_capsule_archive",
        lambda root: b"archive:" + (root / "payload").read_bytes(),
    )
    stage = subject._capsule_stage_record(  # noqa: SLF001
        capsule_root=capsule_root,
        manifest_raw=manifest_raw,
        closure_raw=closure_raw,
        recovery_authority=authority,
    )
    (work_root / "capsule-stage.json").write_bytes(_canonical(stage))

    observed_root, observed_manifest, observed_closure = subject._load_production_capsule(  # noqa: SLF001
        work_root=work_root,
        preparation=preparation,
        fresh_sources=sources,
        recovery_authority=authority,
    )

    assert observed_root == capsule_root
    assert observed_manifest == manifest_raw
    assert observed_closure == closure_raw
    frozen_scope = subject._capsule_publish_stage_scope(  # noqa: SLF001
        capsule_root=capsule_root,
        manifest_raw=manifest_raw,
        tag=f"recovery-{RUN_ID}-1",
        repository_id=304,
    )
    assert (
        subject._require_current_capsule_publish_scope(  # noqa: SLF001
            work_root=work_root,
            preparation=preparation,
            fresh_sources=sources,
            recovery_authority=authority,
            tag=f"recovery-{RUN_ID}-1",
            repository_id=304,
            frozen_scope=frozen_scope,
        )
        == frozen_scope
    )
    capsule_payload.write_bytes(b"substituted payload")
    with pytest.raises(ValueError, match="stage receipt|scope changed"):
        subject._require_current_capsule_publish_scope(  # noqa: SLF001
            work_root=work_root,
            preparation=preparation,
            fresh_sources=sources,
            recovery_authority=authority,
            tag=f"recovery-{RUN_ID}-1",
            repository_id=304,
            frozen_scope=frozen_scope,
        )
    capsule_payload.write_bytes(b"payload")
    (work_root / "recovery-authority.json").write_bytes(b"substituted")
    with pytest.raises(ValueError, match="authority"):
        subject._load_production_capsule(  # noqa: SLF001
            work_root=work_root,
            preparation=preparation,
            fresh_sources=sources,
            recovery_authority=authority,
        )


def test_prepare_mutation_scope_rejects_asset_substitution(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    work_root.mkdir()
    assets = {
        name: _canonical({"name": name, "value": "original"})
        for name in subject.PREPARE_AUTHORITY_ASSETS
    }
    asset_root = subject._load_or_create_prepare_authority_assets(  # noqa: SLF001
        work_root=work_root,
        assets=assets,
    )
    frozen_scope = subject._prepare_publish_stage_scope(  # noqa: SLF001
        asset_root=asset_root,
        promotion_run_id=RUN_ID,
        repository_id=304,
    )

    assert (
        subject._require_current_prepare_publish_scope(  # noqa: SLF001
            asset_root=asset_root,
            promotion_run_id=RUN_ID,
            repository_id=304,
            frozen_scope=frozen_scope,
        )
        == frozen_scope
    )
    changed_name = sorted(subject.PREPARE_AUTHORITY_ASSETS)[0]
    (asset_root / changed_name).write_bytes(
        _canonical({"name": changed_name, "value": "substituted"})
    )
    with pytest.raises(ValueError, match="scope changed"):
        subject._require_current_prepare_publish_scope(  # noqa: SLF001
            asset_root=asset_root,
            promotion_run_id=RUN_ID,
            repository_id=304,
            frozen_scope=frozen_scope,
        )


@pytest.mark.skipif(os.name == "nt", reason="recovery capsule controller requires POSIX (fchmod, bubblewrap)")
def test_production_preparation_replays_the_slow_environment_without_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _isolated_source_root(tmp_path)
    identity = tmp_path / "identity"
    identity.write_bytes(b"private")
    identity.chmod(0o600)
    target = tmp_path / "target"
    target.mkdir()
    authority_paths = _authority_input_paths(tmp_path)
    bootstrap_receipt = tmp_path / "bootstrap-receipt.json"
    bootstrap_receipt.write_bytes(_canonical({"bootstrap": True}))
    monkeypatch.setenv(
        "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT",
        str(bootstrap_receipt),
    )
    request = subject.RecoveryControllerRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=tmp_path / "work",
        output=tmp_path / "output.json",
        **authority_paths,
    )
    subject._open_controller_workspace(  # noqa: SLF001
        request,
        subject._validate_controller_request_scalars(request),  # noqa: SLF001
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "payload.txt").write_text("candidate", encoding="utf-8")
    authorization_root = tmp_path / "authorization"
    authorization_root.mkdir()
    authorization = subject.AuthorizationMaterial(
        root=authorization_root,
        candidate_root=candidate,
        candidate={"candidate_manifest_digest": request.candidate_manifest_digest},
        candidate_manifest=_canonical({"candidate": True}),
        transaction_authorization=_canonical({"transaction": "exact"}),
        transaction={"authorized_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")},
        approval_history_observation=_canonical({"approval": True}),
    )
    dependency_root = tmp_path / "dependency"
    (dependency_root / "recovery").mkdir(parents=True)
    (dependency_root / "recovery/dependency-staging-receipt.json").write_bytes(
        _canonical({"dependency": "exact"})
    )
    environment: dict[str, object] = {
        "schema": "kestrel.recovery_environment.v1",
        "platform": "ubuntu-24.04-x86_64",
        "python_version": "3.11.14",
        "python_abi": "cp311",
        "environment_root": str(target / "transaction/recovery-runtime/environment"),
        "site_packages_path": str(
            target / "transaction/recovery-runtime/environment/lib/python3.11/site-packages"
        ),
        "site_packages_tree_sha256": "sha256:" + "1" * 64,
        "site_packages_file_count": 7,
        "site_packages_total_size_bytes": 700,
    }
    probe_calls = 0

    def probe(
        **kwargs: object,
    ) -> tuple[list[str], dict[str, str], str, Path, dict[str, object]]:
        nonlocal probe_calls
        probe_calls += 1
        assert kwargs["destination"] == target / "transaction/capsule"
        assert kwargs["dependency_root"] == dependency_root
        return (
            [str(environment["site_packages_path"])],
            {"implementation": "CPython", "version": "3.11.14", "abi": "cp311"},
            "sha256:" + "2" * 64,
            target / "transaction/recovery-runtime/base/lib",
            environment,
        )

    monkeypatch.setattr(subject, "_probe_final_python", probe)
    first = subject._load_or_create_production_preparation(  # noqa: SLF001
        request=request,
        authorization=authorization,
        dependency_root=dependency_root,
    )
    monkeypatch.setattr(
        subject,
        "_probe_final_python",
        lambda **_kwargs: pytest.fail("exact preparation replay rebuilt the environment"),
    )
    second = subject._load_or_create_production_preparation(  # noqa: SLF001
        request=request,
        authorization=authorization,
        dependency_root=dependency_root,
    )

    assert probe_calls == 1
    assert second == first
    assert first.candidate_archive.read_bytes() == (
        subject.receipts.deterministic_recovery_capsule_archive(candidate)
    )
    assert first.environment_manifest == environment
    assert first.python_sha256 == "sha256:" + "2" * 64
    assert {path.name for path in first.root.iterdir()} == {
        "candidate-archive.tar",
        "controller-preparation.json",
        "recovery-environment-manifest.json",
    }


def _recovery_reader_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: bytes,
) -> subject.RecoveryReaderMaterial:
    identity, public_key = _signing_identity(tmp_path)
    fingerprint = receipts.ssh_public_key_fingerprint(public_key)
    policy = json.loads((subject.ROOT / "release-control-credential-policy.json").read_bytes())
    purpose = next(item for item in policy["purposes"] if item["purpose"] == "recovery_reader")
    principal = {
        "login": receipts.SIGNING_PRINCIPAL,
        "id": 58918509,
        "type": "User",
    }
    repositories = [{"full_name": subject.RECOVERY_REPOSITORY, "id": 304}]
    grants = [
        {
            "repository_full_name": item["repository_full_name"],
            "repository_id": 304,
            "permission": item["permission"],
            "level": item["level"],
        }
        for item in purpose["grants"]
    ]
    scope = receipts.create_credential_scope_authority(
        purpose="recovery_reader",
        credential_id="credential-recovery-reader",
        principal_observation=_canonical(principal),
        grants_snapshot=_canonical(
            {
                "schema": "kestrel.credential_grants_snapshot.v1",
                "repositories": repositories,
                "grants": grants,
                "endpoint_allowlist": purpose["endpoint_allowlist"],
                "captured_at": (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "complete": True,
            }
        ),
        token_fingerprint=_sha256(token),
        controller_context=_canonical(
            {
                "schema": "kestrel.credential_controller_context.v1",
                "issuer": receipts.SIGNING_PRINCIPAL,
                "signing_key_fingerprint": fingerprint,
                "issued_at": (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "captured_at": (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "complete": True,
            }
        ),
    )
    scope_raw = _canonical(scope)
    signature = receipts.sign_receipt_detached(
        receipt=scope_raw,
        identity_file=identity,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    owner_keys = _source_observation(
        "owner-signing-keys-observation",
        {},
        public_key=public_key,
    )
    normalized_key = {"id": 404, "key": public_key, "title": "Owner"}
    monkeypatch.setattr(
        receipts,
        "_fetch_owner_signing_keys_from_github",
        lambda _principal: [normalized_key],
    )
    results = [
        {
            "endpoint": endpoint,
            "http_status": 200,
            "response_digest": _sha256(endpoint.encode()),
        }
        for endpoint in scope["endpoint_allowlist"]
    ]
    results.append(
        {
            "endpoint": "GET /repos/John-MiracleWorker/Kestrel/actions/permissions",
            "http_status": 403,
            "response_digest": "sha256:" + "f" * 64,
        }
    )
    results.sort(key=lambda item: item["endpoint"])
    probes = _canonical(
        {
            "schema": "kestrel.credential_endpoint_probes.v1",
            "credential_id": scope["credential_id"],
            "results": results,
            "captured_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "complete": True,
        }
    )
    identity_probe = _canonical(principal)
    runtime = receipts.verify_runtime_credential(
        scope_authority=scope_raw,
        scope_authority_signature=signature,
        owner_signing_keys_observation=owner_keys,
        identity_probe=identity_probe,
        endpoint_probe_observations=probes,
        token_bytes=token,
        _clock=lambda: NOW,
    )
    return subject.RecoveryReaderMaterial(
        scope_authority=scope_raw,
        scope_signature=signature,
        owner_signing_keys_observation=owner_keys,
        identity_probe=identity_probe,
        endpoint_probes=probes,
        runtime_verification=_canonical(runtime),
    )


def test_capsule_authority_binding_replays_offline_and_joins_generation_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _isolated_source_root(tmp_path)
    identity, public_key = _signing_identity(tmp_path)
    token = b"reader-token"
    reader_material = _recovery_reader_material(tmp_path, monkeypatch, token=token)
    current_runtime = json.loads(reader_material.runtime_verification)
    authority_paths = _authority_input_paths(tmp_path)
    sources = _current_recovery_authority_sources(public_key=public_key)
    replacements = {
        "current_recovery_owner_authority_snapshot": "recovery-owner-dashboard",
        "current_recovery_repository_observation": "recovery-repository-rest",
        "current_recovery_immutable_releases_observation": ("recovery-immutable-releases-rest"),
        "current_recovery_controller_context": "controller-context",
    }
    for attribute, name in replacements.items():
        authority_paths[attribute].write_bytes(sources[name])
    target = tmp_path / "target"
    target.mkdir()
    bootstrap_receipt = tmp_path / "bootstrap-receipt.json"
    bootstrap_receipt.write_bytes(_canonical({"bootstrap": True}))
    monkeypatch.setenv(
        "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT",
        str(bootstrap_receipt),
    )
    request = subject.RecoveryControllerRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest="sha256:" + "c" * 64,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest="sha256:" + "d" * 64,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest="sha256:" + "e" * 64,
        target_workspace_root=target,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=tmp_path / "work",
        output=tmp_path / "output.json",
        **authority_paths,
    )
    subject._open_controller_workspace(  # noqa: SLF001
        request,
        subject._validate_controller_request_scalars(request),  # noqa: SLF001
    )
    reader = FakeCaptureReader(public_key=public_key)
    monkeypatch.setattr(
        subject,
        "verify_recovery_reader_credential",
        lambda **_kwargs: current_runtime,
    )
    monkeypatch.setattr(
        subject,
        "capture_fresh_owner_signing_keys",
        lambda **_kwargs: sources["owner-signing-keys-observation"],
    )
    monkeypatch.setattr(
        subject,
        "_require_current_reader_authority_binding",
        lambda **_kwargs: None,
    )

    created = subject._load_or_create_capsule_authority_binding(  # noqa: SLF001
        request=request,
        expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
        reader_material=reader_material,
        transaction_authorization={"authorized_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")},
        expected_repository_id=304,
        owner_read_api=reader,
        recovery_reader_api=reader,
        recovery_reader_token=token,
        _clock=lambda: NOW,
    )
    replayed = subject._load_capsule_authority_binding(  # noqa: SLF001
        request=request,
        reader_material=reader_material,
    )

    assert replayed.authority_generation_id == created.authority_generation_id
    generation_root = (
        request.work_root
        / "recovery-authority-generations"
        / created.authority_generation_id.removeprefix("sha256:")
    )
    assert (generation_root / "generation.json").is_file()
    assert replayed.authority.receipt == (generation_root / "authority.json").read_bytes()

    stale_evidence = request.work_root / "normalized-evidence"
    stale_evidence.mkdir()
    (stale_evidence / "old-binding-derived-evidence").write_bytes(b"expired")
    renewed_at = NOW + timedelta(minutes=6)
    current_runtime = {
        **current_runtime,
        "verified_at": renewed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    sources = _current_recovery_authority_sources(
        public_key=public_key,
        captured_at=renewed_at,
    )
    for attribute, name in replacements.items():
        authority_paths[attribute].write_bytes(sources[name])

    subject._recover_interrupted_local_stage(request, resuming=True)  # noqa: SLF001

    assert not created.root.exists()
    assert not stale_evidence.exists()
    renewed = subject._load_or_create_capsule_authority_binding(  # noqa: SLF001
        request=request,
        expected_identity_digest=subject._path_sha256(identity),  # noqa: SLF001
        reader_material=reader_material,
        transaction_authorization={"authorized_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")},
        expected_repository_id=304,
        owner_read_api=reader,
        recovery_reader_api=reader,
        recovery_reader_token=token,
        _clock=lambda: renewed_at,
    )
    assert renewed.authority_generation_id != created.authority_generation_id
    assert renewed.authority.receipt != created.authority.receipt
    created = renewed

    unexpected = created.root / "unexpected.json"
    unexpected.write_bytes(b"{}")
    with pytest.raises(ValueError, match="inventory"):
        subject._load_capsule_authority_binding(  # noqa: SLF001
            request=request,
            reader_material=reader_material,
        )
    unexpected.unlink()
    metadata_path = created.root / "binding.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["authority_generation_id"] = "sha256:" + "f" * 64
    metadata_path.write_bytes(_canonical(metadata))
    with pytest.raises((OSError, ValueError), match="generation|No such file"):
        subject._load_capsule_authority_binding(  # noqa: SLF001
            request=request,
            reader_material=reader_material,
        )


class FakeScopedRecoveryReader:
    def __init__(self, *, forbidden_status: int = 403) -> None:
        self.forbidden_status = forbidden_status
        self.calls: list[str] = []
        self.asset = b"signed dispatch admission"

    def __call__(self, request_target: str, *, accept: str) -> transaction.GitHubReadExchange:
        self.calls.append(request_target)
        if request_target == f"GET /repos/{subject.RECOVERY_REPOSITORY}":
            return transaction.GitHubReadExchange(
                200,
                (),
                _canonical(
                    {
                        "id": 304,
                        "full_name": subject.RECOVERY_REPOSITORY,
                        "private": True,
                        "owner": {
                            "login": receipts.SIGNING_PRINCIPAL,
                            "id": 58918509,
                            "type": "User",
                        },
                    }
                ),
            )
        if request_target.startswith(
            f"GET /repos/{subject.RECOVERY_REPOSITORY}/releases/tags/dispatch-admission-"
        ):
            return transaction.GitHubReadExchange(
                200,
                (),
                _canonical(
                    {
                        "tag_name": "dispatch-admission-" + "0" * 64,
                        "draft": False,
                        "prerelease": False,
                        "immutable": True,
                        "assets": [
                            {
                                "id": 5101,
                                "name": "dispatch-admission.json",
                                "size": len(self.asset),
                            },
                            {
                                "id": 5102,
                                "name": "dispatch-admission.json.sig",
                                "size": 128,
                            },
                        ],
                    }
                ),
            )
        if request_target.endswith("/releases/assets/5101"):
            assert accept == "application/octet-stream"
            return transaction.GitHubReadExchange(200, (), self.asset)
        if request_target == "GET /user":
            return transaction.GitHubReadExchange(
                200,
                (),
                _canonical(
                    {
                        "login": receipts.SIGNING_PRINCIPAL,
                        "id": 58918509,
                        "type": "User",
                    }
                ),
            )
        if request_target == ("GET /repos/John-MiracleWorker/Kestrel/actions/permissions"):
            return transaction.GitHubReadExchange(
                self.forbidden_status,
                (),
                _canonical({"message": "forbidden"}),
            )
        raise AssertionError(f"unexpected scoped reader request: {request_target}")


def test_recovery_reader_material_requires_the_exact_authorization_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery-reader"
    root.mkdir()
    expected = {
        "recovery-reader-scope-authority.json": b"scope",
        "recovery-reader-scope-authority.json.sig": b"signature",
        "owner-signing-keys-observation.json": b"owner keys",
        "owner-signing-keys-raw.json": b"owner keys raw",
        "identity-probe.json": b"identity",
        "endpoint-probes.json": b"probes",
        "runtime-verification.json": b"runtime",
    }
    for name, raw in expected.items():
        (root / name).write_bytes(raw)

    material = subject.load_recovery_reader_material(root)

    assert material.scope_authority == b"scope"
    assert material.scope_signature == b"signature"
    assert material.runtime_verification == b"runtime"
    (root / "unexpected.json").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="inventory"):
        subject.load_recovery_reader_material(root)


def test_initial_reader_proof_resumes_the_exact_bound_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "reader-proof"
    root.mkdir()
    owner_keys = _canonical({"owner": "keys"})
    material = subject.RecoveryReaderMaterial(
        scope_authority=_canonical({"scope": "authority"}),
        scope_signature=b"signature",
        owner_signing_keys_observation=b"historical keys",
        identity_probe=b"identity",
        endpoint_probes=b"probes",
        runtime_verification=b"historical runtime",
    )
    runtime = {
        "schema": receipts.RUNTIME_CREDENTIAL_SCHEMA,
        "purpose": "recovery_reader",
        "scope_authority_digest": _sha256(material.scope_authority),
        "verified_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "validation_status": "validated",
    }
    (root / "owner-signing-keys-observation.json").write_bytes(owner_keys)
    (root / "runtime-verification.json").write_bytes(_canonical(runtime))
    monkeypatch.setattr(
        receipts,
        "_validate_schema",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        receipts,
        "source_observation_body_for_contract",
        lambda *_args, **_kwargs: b"{}",
    )

    observed_owner_keys, observed_runtime = subject._load_initial_reader_proof(  # noqa: SLF001
        source_root=subject.ROOT,
        root=root,
        reader_material=material,
    )

    assert observed_owner_keys == owner_keys
    assert observed_runtime == runtime
    runtime["scope_authority_digest"] = "sha256:" + "f" * 64
    (root / "runtime-verification.json").write_bytes(_canonical(runtime))
    with pytest.raises(ValueError, match="scope"):
        subject._load_initial_reader_proof(  # noqa: SLF001
            source_root=subject.ROOT,
            root=root,
            reader_material=material,
        )


def test_reader_credentials_must_be_distinct_before_any_transport() -> None:
    with pytest.raises(ValueError, match="distinct"):
        subject._require_distinct_credentials(  # noqa: SLF001
            b"same-owner-and-reader-token",
            b"same-owner-and-reader-token",
        )


def test_recovery_reader_rejects_the_wrong_token_before_live_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = b"exact-recovery-reader-token-123456"
    material = _recovery_reader_material(tmp_path, monkeypatch, token=token)
    reader = FakeScopedRecoveryReader()

    with pytest.raises(ValueError, match="fingerprint"):
        subject.verify_recovery_reader_credential(
            material=material,
            token_bytes=b"different-recovery-reader-token-654321",
            transaction_authorization={
                "authorized_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "promotion_run": {"transaction_nonce": "0" * 64},
            },
            expected_repository_id=304,
            api=reader,
            _clock=lambda: NOW + timedelta(minutes=1),
        )

    assert reader.calls == []


def test_recovery_reader_fresh_probe_rejects_forbidden_endpoint_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = b"exact-recovery-reader-token-123456"
    material = _recovery_reader_material(tmp_path, monkeypatch, token=token)
    reader = FakeScopedRecoveryReader(forbidden_status=200)

    with pytest.raises(ValueError, match="forbidden"):
        subject.verify_recovery_reader_credential(
            material=material,
            token_bytes=token,
            transaction_authorization={
                "authorized_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "promotion_run": {"transaction_nonce": "0" * 64},
            },
            expected_repository_id=304,
            api=reader,
            _clock=lambda: NOW + timedelta(minutes=1),
        )


def test_recovery_reader_replays_scope_then_proves_current_read_only_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = b"exact-recovery-reader-token-123456"
    material = _recovery_reader_material(tmp_path, monkeypatch, token=token)
    reader = FakeScopedRecoveryReader()

    verification = subject.verify_recovery_reader_credential(
        material=material,
        token_bytes=token,
        transaction_authorization={
            "authorized_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "promotion_run": {"transaction_nonce": "0" * 64},
        },
        expected_repository_id=304,
        api=reader,
        _clock=lambda: NOW + timedelta(minutes=1),
    )

    assert verification["purpose"] == "recovery_reader"
    assert reader.calls == [
        f"GET /repos/{subject.RECOVERY_REPOSITORY}",
        (f"GET /repos/{subject.RECOVERY_REPOSITORY}/releases/tags/dispatch-admission-{'0' * 64}"),
        f"GET /repos/{subject.RECOVERY_REPOSITORY}/releases/assets/5101",
        "GET /user",
        "GET /repos/John-MiracleWorker/Kestrel/actions/permissions",
    ]


def test_production_controller_wires_one_exact_authority_bound_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _isolated_source_root(tmp_path)
    target_root = tmp_path / "target"
    target_root.mkdir()
    identity = tmp_path / "identity"
    identity.write_bytes(b"private")
    identity.chmod(0o600)
    work_root = tmp_path / "controller-work"
    output = tmp_path / "controller-receipt.json"
    candidate_digest = "sha256:" + "c" * 64
    authorization_digest = "sha256:" + "d" * 64
    staging_digest = "sha256:" + "e" * 64
    authority_inputs = _authority_input_paths(tmp_path)
    request = subject.RecoveryControllerRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        candidate_manifest_digest=candidate_digest,
        promotion_run_id=RUN_ID,
        authorization_artifact_id=ARTIFACT_ID,
        authorization_artifact_digest=authorization_digest,
        staging_run_id=RUN_ID + 1,
        staging_artifact_id=ARTIFACT_ID + 1,
        staging_artifact_digest=staging_digest,
        target_workspace_root=target_root,
        recovery_repository_id=304,
        identity_file=identity,
        work_root=work_root,
        output=output,
        **authority_inputs,
    )
    events: list[str] = []
    artifact_specs: list[subject.ActionsArtifactSpec] = []

    monkeypatch.setattr(
        subject,
        "_require_controller_paths",
        lambda _request: events.append("preflight"),
    )
    workspace_states = iter((False, True))
    monkeypatch.setattr(
        subject,
        "_open_controller_workspace",
        lambda _request, _scalars: events.append("open-workspace") or next(workspace_states),
    )
    monkeypatch.setattr(
        subject,
        "_recover_interrupted_local_stage",
        lambda _request, *, resuming: events.append(f"recover-local-{str(resuming).lower()}"),
    )
    monkeypatch.setattr(
        subject,
        "_require_clean_source_identity",
        lambda _request: events.append("recheck-source"),
        raising=False,
    )
    monkeypatch.setattr(
        subject,
        "_require_target_workspace_empty",
        lambda _request: events.append("recheck-target-empty"),
        raising=False,
    )
    mutation_gate_calls = 0

    def require_mutation_authority(**kwargs: object) -> None:
        nonlocal mutation_gate_calls
        mutation_gate_calls += 1
        assert kwargs["request"] == request
        assert kwargs["recovery_reader_token"] == b"reader-token"
        events.append(f"mutation-gate-{mutation_gate_calls}")

    monkeypatch.setattr(
        subject,
        "_authorize_current_stage_mutation",
        require_mutation_authority,
        raising=False,
    )

    acquired_artifacts: dict[str, subject.AcquiredActionsArtifact] = {}

    def acquire_artifact(
        *, api: object, specification: subject.ActionsArtifactSpec, output_root: Path
    ) -> subject.AcquiredActionsArtifact:
        del api
        artifact_specs.append(specification)
        label = "authorization" if not specification.require_completed_success else "dependency"
        events.append(f"acquire-{label}")
        if label in acquired_artifacts:
            return acquired_artifacts[label]
        root = output_root / "contents"
        evidence = output_root / "evidence"
        root.mkdir(parents=True)
        evidence.mkdir()
        (evidence / "transport.json").write_bytes(_canonical({"label": label}))
        if label == "authorization":
            authority = root / "authority-evidence"
            authority.mkdir()
            (authority / "recovery-authority-verification.json").write_bytes(
                _canonical({"authority": True})
            )
            (authority / "github-admission-authority-verification.json").write_bytes(
                _canonical({"admission": True})
            )
            candidate = root / "candidate"
            candidate.mkdir()
        else:
            (root / "recovery-smoke-report.json").write_bytes(_canonical({"smoke": True}))
        acquired = subject.AcquiredActionsArtifact(
            root=root,
            archive_path=output_root / "artifact.zip",
            evidence_root=evidence,
            receipt={
                "artifact": {
                    "artifact_id": specification.artifact_id,
                    "api_digest": specification.api_digest,
                }
            },
        )
        acquired_artifacts[label] = acquired
        return acquired

    monkeypatch.setattr(subject, "acquire_actions_artifact", acquire_artifact)
    transaction_raw = _canonical({"transaction": True})
    approval_raw = _canonical({"approval": True})

    def validate_authorization(**kwargs: object) -> subject.AuthorizationMaterial:
        events.append("validate-authorization")
        root = kwargs["root"]
        assert isinstance(root, Path)
        return subject.AuthorizationMaterial(
            root=root,
            candidate_root=root / "candidate",
            candidate={"candidate_manifest_digest": candidate_digest},
            candidate_manifest=_canonical({"candidate": True}),
            transaction_authorization=transaction_raw,
            transaction={
                "authorization_kind": "transaction",
                "authorized_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            approval_history_observation=approval_raw,
        )

    monkeypatch.setattr(subject, "validate_authorization_artifact", validate_authorization)
    smoke_raw = _canonical({"smoke": True})

    def validate_staging(root: Path, *, source_sha: str) -> bytes:
        assert root.name == "contents" and source_sha == SOURCE_SHA
        events.append("validate-staging")
        return smoke_raw

    monkeypatch.setattr(subject, "_validate_staging_artifact", validate_staging)
    environment_raw = _canonical({"environment": True})

    def prepare_local(**_kwargs: object) -> subject.PreparedProductionCapsule:
        events.append("prepare-local")
        root = work_root / "production-preparation"
        root.mkdir(exist_ok=True)
        candidate = root / "candidate-archive.tar"
        if not candidate.exists():
            candidate.write_bytes(b"candidate")
            (root / "recovery-environment-manifest.json").write_bytes(environment_raw)
        receipt = _canonical({"preparation": True})
        if not (root / "controller-preparation.json").exists():
            (root / "controller-preparation.json").write_bytes(receipt)
        return subject.PreparedProductionCapsule(
            root=root,
            candidate_archive=candidate,
            environment_manifest_raw=environment_raw,
            environment_manifest={"environment": True},
            sys_path=("/target/site-packages",),
            runtime={
                "implementation": "CPython",
                "version": "3.11.14",
                "abi": "cp311",
            },
            python_sha256="sha256:" + "1" * 64,
            base_library_root=Path("/target/base/lib"),
            receipt=receipt,
        )

    monkeypatch.setattr(subject, "_load_or_create_production_preparation", prepare_local)
    owner_keys = _canonical({"owner_keys": True})
    reader_material = subject.RecoveryReaderMaterial(
        scope_authority=_canonical({"scope": True}),
        scope_signature=b"scope signature",
        owner_signing_keys_observation=_canonical({"historical_owner_keys": True}),
        identity_probe=_canonical({"identity": True}),
        endpoint_probes=_canonical({"probes": True}),
        runtime_verification=_canonical({"historical_runtime": True}),
    )

    def capture_owner_keys(**kwargs: object) -> bytes:
        assert kwargs["source_root"] == source_root
        events.append("capture-owner-keys")
        return owner_keys

    def load_reader_material(root: Path) -> subject.RecoveryReaderMaterial:
        assert root == (
            work_root / "authorization-artifact/contents/transaction-identity/recovery-reader"
        )
        events.append("load-reader-material")
        return reader_material

    reader_verification = {
        "schema": receipts.RUNTIME_CREDENTIAL_SCHEMA,
        "purpose": "recovery_reader",
        "validation_status": "validated",
    }

    def verify_reader(**kwargs: object) -> receipts.JSONObject:
        assert kwargs["material"] == reader_material
        assert kwargs["token_bytes"] == b"reader-token"
        assert kwargs["current_owner_signing_keys_observation"] == owner_keys
        events.append("verify-reader")
        return reader_verification

    monkeypatch.setattr(subject, "capture_fresh_owner_signing_keys", capture_owner_keys)
    monkeypatch.setattr(subject, "load_recovery_reader_material", load_reader_material)
    monkeypatch.setattr(subject, "verify_recovery_reader_credential", verify_reader)
    fresh_calls = 0

    def capture_fresh(**kwargs: object) -> subject.FreshRecoverySources:
        nonlocal fresh_calls
        fresh_calls += 1
        events.append(f"capture-fresh-{fresh_calls}")
        output_root = kwargs["output_root"]
        assert isinstance(output_root, Path)
        output_root.mkdir()
        repository_body = _canonical({"id": 304})
        for name, raw in (
            ("owner-signing-keys-observation.json", owner_keys),
            ("recovery-repository-observation.json", _canonical({"source": True})),
            ("recovery-repository.json", repository_body),
        ):
            (output_root / name).write_bytes(raw)
        return subject.FreshRecoverySources(
            repository_id=304,
            repository_body=repository_body,
            repository_observation=_canonical({"source": True}),
            owner_signing_keys_observation=owner_keys,
        )

    monkeypatch.setattr(subject, "capture_fresh_recovery_sources", capture_fresh)

    def validate_recovery_authority(**kwargs: object) -> subject.RecoveryAuthorityMaterial:
        assert kwargs["expected_repository_id"] == 304
        events.append("validate-recovery-authority")
        return subject.RecoveryAuthorityMaterial(
            authority={"repository": {"id": 304}},
            receipt=_canonical({"recovery_authority": True}),
            signature=b"signature",
        )

    monkeypatch.setattr(subject, "_validate_recovery_authority_record", validate_recovery_authority)
    current_authority = subject.RecoveryAuthorityMaterial(
        authority={"repository": {"id": 304}},
        receipt=_canonical({"current_recovery_authority": True}),
        signature=b"current signature",
    )

    def freeze_capsule_authority(**_kwargs: object) -> subject.CapsuleAuthorityBinding:
        events.append("freeze-capsule-authority")
        root = work_root / "capsule-authority-binding"
        fresh_root = root / "fresh-sources"
        reader_root = root / "reader-credential"
        fresh_root.mkdir(parents=True)
        reader_root.mkdir()
        repository_body = _canonical({"id": 304})
        for name, raw in (
            ("owner-signing-keys-observation.json", owner_keys),
            ("recovery-repository-observation.json", _canonical({"source": True})),
            ("recovery-repository.json", repository_body),
        ):
            (fresh_root / name).write_bytes(raw)
        (reader_root / "owner-signing-keys-observation.json").write_bytes(owner_keys)
        (reader_root / "runtime-verification.json").write_bytes(_canonical(reader_verification))
        (root / "current-recovery-authority.json").write_bytes(current_authority.receipt)
        (root / "current-recovery-authority.json.sig").write_bytes(current_authority.signature)
        (root / "binding.json").write_bytes(_canonical({"binding": True}))
        return subject.CapsuleAuthorityBinding(
            root=root,
            authority_generation_id="sha256:" + "2" * 64,
            authority=current_authority,
            fresh_sources=subject.FreshRecoverySources(
                repository_id=304,
                repository_body=repository_body,
                repository_observation=_canonical({"source": True}),
                owner_signing_keys_observation=owner_keys,
            ),
            owner_signing_keys_observation=owner_keys,
            reader_runtime_verification=reader_verification,
        )

    monkeypatch.setattr(
        subject,
        "_load_or_create_capsule_authority_binding",
        freeze_capsule_authority,
    )

    def create_current_authority(**kwargs: object) -> subject.RecoveryAuthorityMaterial:
        assert kwargs["identity_file"] == identity
        assert kwargs["expected_repository_id"] == 304
        assert kwargs["fresh_owner_signing_keys_observation"] == owner_keys
        assert (
            kwargs["owner_authority_snapshot"]
            == authority_inputs["current_recovery_owner_authority_snapshot"].read_bytes()
        )
        assert (
            kwargs["repository_observation"]
            == authority_inputs["current_recovery_repository_observation"].read_bytes()
        )
        assert (
            kwargs["immutable_releases_observation"]
            == authority_inputs["current_recovery_immutable_releases_observation"].read_bytes()
        )
        assert (
            kwargs["controller_context"]
            == authority_inputs["current_recovery_controller_context"].read_bytes()
        )
        events.append("create-current-authority")
        return current_authority

    monkeypatch.setattr(subject, "create_current_recovery_authority", create_current_authority)

    def require_reader_binding(**kwargs: object) -> None:
        assert kwargs["recovery_authority"] == current_authority.authority
        assert kwargs["reader_material"] == reader_material
        assert kwargs["current_runtime_verification"] == reader_verification
        events.append("join-reader-authority")

    monkeypatch.setattr(
        subject,
        "_require_current_reader_authority_binding",
        require_reader_binding,
    )
    manifest_raw = _canonical({"manifest": True})
    closure_raw = _canonical({"closure": True})

    def create_capsule(**kwargs: object) -> tuple[Path, bytes, bytes]:
        assert kwargs["recovery_authority"] == current_authority
        events.append("create-capsule")
        destination = kwargs["destination"]
        assert destination == target_root / "transaction" / "capsule"
        capsule = work_root / "capsule-source"
        capsule.mkdir()
        return capsule, manifest_raw, closure_raw

    monkeypatch.setattr(subject, "_create_production_capsule", create_capsule)
    monkeypatch.setattr(
        subject,
        "_capsule_publish_stage_scope",
        lambda **_kwargs: {"stage": "capsule_publish"},
    )
    monkeypatch.setattr(
        subject,
        "_require_current_capsule_publish_scope",
        lambda **kwargs: kwargs["frozen_scope"],
    )
    monkeypatch.setattr(
        subject,
        "_require_current_prepare_publish_scope",
        lambda **kwargs: kwargs["frozen_scope"],
    )

    def publish_capsule(
        capsule_root: Path,
        tag: str,
        expected_repository_id: int,
        publication_path: Path,
        mutation_guard: object,
    ) -> None:
        assert capsule_root == work_root / "capsule-source"
        assert tag == f"recovery-{RUN_ID}-1"
        assert expected_repository_id == 304
        assert callable(mutation_guard)
        mutation_guard()
        events.append("publish-capsule")
        publication_path.write_bytes(
            _canonical(
                {
                    "schema": "kestrel.recovery_capsule_publication.v1",
                    "repository": subject.RECOVERY_REPOSITORY,
                    "repository_id": 304,
                    "tag": tag,
                    "release_id": 4100,
                    "manifest_digest": _sha256(manifest_raw),
                    "archive_digest": "sha256:" + "a" * 64,
                    "immutable": True,
                    "validation_status": "validated",
                }
            )
        )

    remote_records = {
        "recovery-repository": _canonical({"repository": True}),
        "recovery-release": _canonical({"release": True}),
        "recovery-release-assets": _canonical({"assets": True}),
    }

    def capture_remote(**kwargs: object) -> subject.RemoteCapsuleSources:
        assert kwargs["expected_release_id"] == 4100
        events.append("capture-remote")
        output_root = kwargs["output_root"]
        assert isinstance(output_root, Path)
        output_root.mkdir()
        return subject.RemoteCapsuleSources(
            repository_body=_canonical({"repository": True}),
            release_body=_canonical({"release": True}),
            assets_body=_canonical([]),
            source_records=remote_records,
        )

    monkeypatch.setattr(subject, "capture_remote_capsule_sources", capture_remote)
    verification_claim = {
        "schema": "kestrel.recovery_capsule_verification_claim.v1",
        "verified": True,
    }

    def verify_capsule(**kwargs: object) -> receipts.JSONObject:
        assert kwargs["capsule_manifest"] == manifest_raw
        assert kwargs["execution_closure"] == closure_raw
        assert kwargs["remote_source_records"] == remote_records
        events.append("verify-capsule")
        return verification_claim

    monkeypatch.setattr(subject.transaction, "verify_recovery_capsule", verify_capsule)

    def sign_verification(**kwargs: object) -> receipts.JSONObject:
        assert kwargs["verification_claim"] == verification_claim
        assert kwargs["identity_file"] == identity
        assert kwargs["owner_signing_keys_observation"] == owner_keys
        events.append("sign-verification")
        return {
            "schema": "kestrel.recovery_capsule_verification.v1",
            "validation_status": "validated",
        }

    def publish_prepare(**kwargs: object) -> receipts.JSONObject:
        assert kwargs["promotion_run_id"] == RUN_ID
        assert kwargs["candidate_manifest_digest"] == candidate_digest
        assert kwargs["transaction_authorization"] == transaction_raw
        assert kwargs["owner_signing_keys_observation"] == owner_keys
        assert kwargs["source_root"] == source_root
        asset_root = kwargs["asset_root"]
        assert isinstance(asset_root, Path)
        assert {path.name for path in asset_root.iterdir()} == set(subject.PREPARE_AUTHORITY_ASSETS)
        mutation_guard = kwargs["mutation_guard"]
        assert callable(mutation_guard)
        mutation_guard()
        events.append("publish-prepare")
        return {
            "tag_name": f"release-prepare-authority-{RUN_ID}-1",
            "release_id": 4200,
            "validation_status": "validated",
        }

    monkeypatch.setattr(subject, "publish_prepare_capsule_authority", publish_prepare)

    prepared = subject.run_production_controller(
        request=request,
        actions_api=object(),  # type: ignore[arg-type]
        terminal_api=object(),  # type: ignore[arg-type]
        owner_read_api=object(),  # type: ignore[arg-type]
        recovery_reader_api=object(),  # type: ignore[arg-type]
        recovery_reader_token=b"reader-token",
        capsule_publisher=publish_capsule,
        verification_signer=sign_verification,
        prepare_only=True,
        _clock=lambda: NOW,
    )
    assert prepared == {"preparation": True}
    assert events == [
        "preflight",
        "open-workspace",
        "recover-local-false",
        "acquire-authorization",
        "acquire-dependency",
        "validate-authorization",
        "validate-staging",
        "prepare-local",
    ]

    result = subject.run_production_controller(
        request=request,
        actions_api=object(),  # type: ignore[arg-type]
        terminal_api=object(),  # type: ignore[arg-type]
        owner_read_api=object(),  # type: ignore[arg-type]
        recovery_reader_api=object(),  # type: ignore[arg-type]
        recovery_reader_token=b"reader-token",
        capsule_publisher=publish_capsule,
        verification_signer=sign_verification,
        _clock=lambda: NOW,
    )

    assert events == [
        "preflight",
        "open-workspace",
        "recover-local-false",
        "acquire-authorization",
        "acquire-dependency",
        "validate-authorization",
        "validate-staging",
        "prepare-local",
        "preflight",
        "open-workspace",
        "recover-local-true",
        "acquire-authorization",
        "acquire-dependency",
        "validate-authorization",
        "validate-staging",
        "prepare-local",
        "load-reader-material",
        "validate-recovery-authority",
        "freeze-capsule-authority",
        "recheck-source",
        "recheck-target-empty",
        "create-capsule",
        "mutation-gate-1",
        "publish-capsule",
        "capture-remote",
        "verify-capsule",
        "capture-fresh-1",
        "sign-verification",
        "mutation-gate-2",
        "publish-prepare",
    ]
    expected_artifact_specs = [
        subject.ActionsArtifactSpec(
            name=f"kestrel-release-transaction-authorization-{RUN_ID}-1",
            workflow_path=".github/workflows/release.yml",
            run_id=RUN_ID,
            artifact_id=ARTIFACT_ID,
            api_digest=authorization_digest,
            source_sha=SOURCE_SHA,
            require_completed_success=False,
        ),
        subject.ActionsArtifactSpec(
            name=f"kestrel-recovery-dependencies-{SOURCE_SHA}",
            workflow_path=".github/workflows/recovery-dependency-staging.yml",
            run_id=RUN_ID + 1,
            artifact_id=ARTIFACT_ID + 1,
            api_digest=staging_digest,
            source_sha=SOURCE_SHA,
            require_completed_success=True,
        ),
    ]
    assert artifact_specs == [*expected_artifact_specs, *expected_artifact_specs]
    assert result["validation_status"] == "validated"
    assert result["prepare_authority"] == {
        "tag": f"release-prepare-authority-{RUN_ID}-1",
        "release_id": 4200,
        "publication_digest": _sha256(
            _canonical(
                {
                    "tag_name": f"release-prepare-authority-{RUN_ID}-1",
                    "release_id": 4200,
                    "validation_status": "validated",
                }
            )
        ),
    }
    assert output.read_bytes() == _canonical(result)


def test_normalized_evidence_inventory_rejects_unbound_top_level_entry(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "normalized-evidence"
    evidence_root.mkdir()
    for name in (
        "authorization-artifact",
        "dependency-artifact",
        "fresh-recovery-sources",
        "reader-credential",
    ):
        (evidence_root / name).mkdir()
    for name in (
        "recovery-smoke-report.json",
        "recovery-authority-verification.json",
        "github-admission-authority-verification.json",
        "current-recovery-authority.json",
        "current-recovery-authority.json.sig",
        "controller-inputs.json",
    ):
        (evidence_root / name).write_bytes(b"fixture")

    subject._require_normalized_evidence_inventory(evidence_root)  # noqa: SLF001

    (evidence_root / "unbound.json").write_bytes(b"not admitted")
    with pytest.raises(ValueError, match="inventory"):
        subject._require_normalized_evidence_inventory(  # noqa: SLF001
            evidence_root
        )


class FakeTerminalReleaseAPI:
    def __init__(self) -> None:
        self.releases: list[transaction.TerminalRelease] = []
        self.asset_bytes: dict[int, bytes] = {}
        self.create_calls = 0
        self.upload_calls: list[str] = []
        self.publish_calls = 0

    def list_releases(self, repository: str) -> transaction.TerminalReleaseListing:
        assert repository == subject.RECOVERY_REPOSITORY
        return transaction.TerminalReleaseListing(tuple(self.releases), complete=True)

    def claim_terminal_kind(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("prepare capsule authority does not claim dispatch state")

    def create_draft(self, repository: str, *, tag_name: str, name: str, body: str) -> int:
        assert repository == subject.RECOVERY_REPOSITORY
        self.create_calls += 1
        self.releases.append(
            transaction.TerminalRelease(
                release_id=4101,
                tag_name=tag_name,
                name=name,
                body=body,
                draft=True,
                prerelease=False,
                immutable=False,
                html_url=f"https://github.invalid/{tag_name}",
                assets=(),
            )
        )
        return 4101

    def upload_asset(
        self,
        repository: str,
        *,
        release_id: int,
        name: str,
        media_type: str,
        content: bytes,
    ) -> None:
        assert repository == subject.RECOVERY_REPOSITORY
        release = self.releases[0]
        asset_id = 5101 + len(self.upload_calls)
        self.upload_calls.append(name)
        self.asset_bytes[asset_id] = content
        asset = transaction.TerminalReleaseAsset(
            asset_id=asset_id,
            name=name,
            size_bytes=len(content),
            digest=_sha256(content),
            media_type=media_type,
        )
        self.releases[0] = transaction.TerminalRelease(
            release_id=release.release_id,
            tag_name=release.tag_name,
            name=release.name,
            body=release.body,
            draft=release.draft,
            prerelease=release.prerelease,
            immutable=release.immutable,
            html_url=release.html_url,
            assets=tuple(sorted((*release.assets, asset), key=lambda item: item.name)),
        )

    def publish_immutable(self, repository: str, *, release_id: int) -> None:
        assert repository == subject.RECOVERY_REPOSITORY
        release = self.releases[0]
        assert release.release_id == release_id
        self.publish_calls += 1
        self.releases[0] = transaction.TerminalRelease(
            release_id=release.release_id,
            tag_name=release.tag_name,
            name=release.name,
            body=release.body,
            draft=False,
            prerelease=False,
            immutable=True,
            html_url=release.html_url,
            assets=release.assets,
        )


class FakeRecoveryReader:
    def __init__(self, terminal: FakeTerminalReleaseAPI) -> None:
        self.terminal = terminal

    def __call__(self, request_target: str, *, accept: str) -> transaction.GitHubReadExchange:
        if request_target == f"GET /repos/{subject.RECOVERY_REPOSITORY}":
            body = _canonical(
                {
                    "id": 304,
                    "full_name": subject.RECOVERY_REPOSITORY,
                    "private": True,
                    "visibility": "private",
                    "archived": False,
                    "disabled": False,
                    "owner": {
                        "login": receipts.SIGNING_PRINCIPAL,
                        "id": 58918509,
                        "type": "User",
                    },
                }
            )
            return transaction.GitHubReadExchange(200, (), body)
        if "/releases/tags/" in request_target:
            if not self.terminal.releases:
                return transaction.GitHubReadExchange(404, (), b"{}")
            release = self.terminal.releases[0]
            body = _canonical(
                {
                    "id": release.release_id,
                    "tag_name": release.tag_name,
                    "name": release.name,
                    "body": release.body,
                    "draft": release.draft,
                    "prerelease": release.prerelease,
                    "immutable": release.immutable,
                    "html_url": release.html_url,
                    "assets": [
                        {
                            "id": item.asset_id,
                            "name": item.name,
                            "size": item.size_bytes,
                            "digest": item.digest,
                            "content_type": item.media_type,
                        }
                        for item in release.assets
                    ],
                }
            )
            return transaction.GitHubReadExchange(200, (), body)
        if "/releases/assets/" in request_target:
            asset_id = int(request_target.rsplit("/", 1)[1])
            return transaction.GitHubReadExchange(200, (), self.terminal.asset_bytes[asset_id])
        raise AssertionError(f"unexpected recovery read: {request_target} {accept}")


class FakeCaptureReader:
    def __init__(self, *, public_key: str) -> None:
        self.calls: list[str] = []
        self.repository = {
            "id": 304,
            "full_name": subject.RECOVERY_REPOSITORY,
            "private": True,
            "visibility": "private",
            "archived": False,
            "disabled": False,
            "owner": {
                "login": receipts.SIGNING_PRINCIPAL,
                "id": 58918509,
                "type": "User",
                "avatar_url": "https://avatars.invalid/owner",
                "site_admin": False,
            },
        }
        self.release = {
            "id": 4100,
            "tag_name": f"recovery-{RUN_ID}-1",
            "name": f"recovery-{RUN_ID}-1",
            "draft": False,
            "prerelease": False,
            "immutable": True,
        }
        self.assets = [
            {"id": 5001, "name": "recovery-bootstrap.py"},
            {"id": 5002, "name": "recovery-capsule-manifest.json"},
            {"id": 5003, "name": "recovery-capsule.tar"},
        ]
        self.public_key = public_key

    def __call__(self, request_target: str, *, accept: str) -> transaction.GitHubReadExchange:
        assert accept == "application/vnd.github+json"
        self.calls.append(request_target)
        if request_target == "GET /user":
            return transaction.GitHubReadExchange(
                200,
                (),
                _canonical(
                    {
                        "login": receipts.SIGNING_PRINCIPAL,
                        "id": 58918509,
                        "type": "User",
                    }
                ),
            )
        if request_target.endswith("ssh_signing_keys?per_page=100&page=2"):
            return transaction.GitHubReadExchange(
                200,
                (),
                _canonical([{"id": 405, "key": self.public_key, "title": "Owner backup"}]),
            )
        if request_target.endswith("ssh_signing_keys?per_page=100"):
            next_url = (
                "https://api.github.com/users/John-MiracleWorker/"
                "ssh_signing_keys?per_page=100&page=2"
            )
            return transaction.GitHubReadExchange(
                200,
                (("Link", f'<{next_url}>; rel="next"'),),
                _canonical([{"id": 404, "key": self.public_key, "title": "Owner"}]),
            )
        if request_target == f"GET /repos/{subject.RECOVERY_REPOSITORY}":
            return transaction.GitHubReadExchange(200, (), _canonical(self.repository))
        if request_target == (
            f"GET /repos/{subject.RECOVERY_REPOSITORY}/releases/tags/recovery-{RUN_ID}-1"
        ):
            return transaction.GitHubReadExchange(200, (), _canonical(self.release))
        if request_target.endswith("/assets?per_page=100&page=2"):
            return transaction.GitHubReadExchange(200, (), _canonical(self.assets[2:]))
        if request_target.endswith("/assets?per_page=100"):
            next_url = (
                f"https://api.github.com/repos/{subject.RECOVERY_REPOSITORY}/"
                f"releases/4100/assets?per_page=100&page=2"
            )
            return transaction.GitHubReadExchange(
                200,
                (("Link", f'<{next_url}>; rel="next"'),),
                _canonical(self.assets[:2]),
            )
        raise AssertionError(f"unexpected capture read: {request_target} {accept}")


def _signing_identity(tmp_path: Path) -> tuple[Path, str]:
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    identity = tmp_path / "owner-key"
    identity.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    identity.chmod(0o600)
    public_key = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        .decode("ascii")
    )
    return identity, public_key


def _isolated_source_root(tmp_path: Path) -> Path:
    source_root = tmp_path / "source"
    source_root.mkdir()
    registry = Path(subject.__file__).resolve().parents[1] / (
        "release-control-source-registry.json"
    )
    (source_root / registry.name).write_bytes(registry.read_bytes())
    return source_root


def test_capture_fresh_recovery_sources_uses_explicit_registry_and_full_owner_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _identity, public_key = _signing_identity(tmp_path)
    reader = FakeCaptureReader(public_key=public_key)
    source_root = _isolated_source_root(tmp_path)
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    captured = subject.capture_fresh_recovery_sources(
        source_root=source_root,
        output_root=tmp_path / "fresh",
        api=reader,
        _clock=lambda: NOW,
    )

    assert captured.repository_id == 304
    assert captured.repository_body == _canonical(reader.repository)
    assert {path.name for path in (tmp_path / "fresh").iterdir()} == {
        "owner-signing-keys-observation.json",
        "recovery-repository-observation.json",
        "recovery-repository.json",
    }
    owner = json.loads(captured.owner_signing_keys_observation)
    assert owner["authenticated_as"] == receipts.SIGNING_PRINCIPAL
    assert owner["page_count"] == 2
    assert owner["record_count"] == 2
    owner_body = json.loads(
        receipts.source_observation_body(
            captured.owner_signing_keys_observation,
            expected_name="owner-signing-keys-observation",
        )
    )
    assert [page["number"] for page in owner_body["pages"]] == [1, 2]
    assert owner_body["pages"][1]["request_url"].endswith("ssh_signing_keys?per_page=100&page=2")
    repository = json.loads(captured.repository_observation)
    assert repository["record_count"] == 1
    assert repository["authenticated_as"] == receipts.SIGNING_PRINCIPAL


def test_capture_remote_capsule_sources_flattens_complete_asset_pagination(
    tmp_path: Path,
) -> None:
    _identity, public_key = _signing_identity(tmp_path)
    reader = FakeCaptureReader(public_key=public_key)
    source_root = _isolated_source_root(tmp_path)
    interrupted = tmp_path / ".remote.staging-interrupted"
    interrupted.mkdir()
    (interrupted / "partial.json").write_bytes(b"partial")

    captured = subject.capture_remote_capsule_sources(
        source_root=source_root,
        recovery_tag=f"recovery-{RUN_ID}-1",
        expected_release_id=4100,
        expected_repository_id=304,
        output_root=tmp_path / "remote",
        api=reader,
        _clock=lambda: NOW,
    )

    assert captured.repository_body == _canonical(reader.repository)
    assert captured.release_body == _canonical(reader.release)
    assert captured.assets_body == _canonical(reader.assets)
    assert set(captured.source_records) == {
        "recovery-repository",
        "recovery-release",
        "recovery-release-assets",
    }
    assert {path.name for path in (tmp_path / "remote").iterdir()} == {
        "recovery-assets-observation.json",
        "recovery-release-observation.json",
        "recovery-repository-observation.json",
    }
    assets = json.loads(captured.source_records["recovery-release-assets"])
    assert assets["authenticated_as"] == receipts.SIGNING_PRINCIPAL
    assert assets["page_count"] == 2
    assert assets["record_count"] == 3
    assert reader.calls.count("GET /user") == 1
    assert not interrupted.exists()

    class NoReader:
        def __call__(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("remote capsule resume attempted transport")

    resumed = subject.capture_remote_capsule_sources(
        source_root=source_root,
        recovery_tag=f"recovery-{RUN_ID}-1",
        expected_release_id=4100,
        expected_repository_id=304,
        output_root=tmp_path / "remote",
        api=NoReader(),  # type: ignore[arg-type]
        _clock=lambda: NOW + timedelta(days=1),
    )

    assert resumed.repository_body == captured.repository_body
    assert resumed.release_body == captured.release_body
    assert resumed.assets_body == captured.assets_body


def _source_observation(
    name: str,
    body: object,
    *,
    public_key: str,
    captured_at: datetime = NOW,
) -> bytes:
    registry = json.loads(Path("release-control-source-registry.json").read_bytes())
    raw = receipts.canonical_external_json_bytes(body)  # type: ignore[arg-type]
    if name == "owner-signing-keys-observation":
        raw = receipts.canonical_external_json_bytes(
            {
                "pages": [
                    {
                        "number": 1,
                        "request_url": (
                            "GET /users/John-MiracleWorker/ssh_signing_keys?per_page=100"
                        ),
                        "response_headers": [],
                        "body": [{"id": 404, "key": public_key, "title": "Owner"}],
                    }
                ]
            }
        )
    captured = receipts.capture_source(
        registry=registry,
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name=name,
        raw_input=raw,
        identity_observation=_canonical({"login": receipts.SIGNING_PRINCIPAL}),
        _clock=lambda: captured_at,
    )
    return _canonical(captured)


def _transaction_authorization(approval: dict[str, object]) -> bytes:
    return _canonical(
        {
            "schema": transaction.SERVER_AUTHORIZATION_SCHEMA,
            "authorization_kind": "transaction",
            "mode": "initiate",
            "candidate": {
                "candidate_manifest_digest": "sha256:" + "c" * 64,
                "artifact_set_digest": "sha256:" + "a" * 64,
                "version": "0.6.0",
                "tag": "v0.6.0",
                "source_sha": SOURCE_SHA,
                "source_tree": "b" * 40,
                "candidate_run_id": 601,
                "candidate_run_attempt": 1,
            },
            "promotion_run": {
                "repository_id": REPOSITORY_ID,
                "workflow_id": 88,
                "workflow_path": ".github/workflows/release.yml",
                "run_id": RUN_ID,
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "ref": "refs/heads/main",
                "head_sha": SOURCE_SHA,
                "workflow_sha": SOURCE_SHA,
                "actor": {"login": "release-dispatcher[bot]", "id": 702},
                "triggering_actor": {
                    "login": "release-dispatcher[bot]",
                    "id": 702,
                },
                "transaction_nonce": "0" * 64,
                "rest_observation_digest": "sha256:" + "1" * 64,
                "context_observation_digest": "sha256:" + "2" * 64,
            },
            "environment": {
                "name": "release",
                "id": 9,
                "policies_digest": "sha256:" + "3" * 64,
            },
            "approval_history": approval,
            "admission_authority": {
                "receipt_digest": "sha256:" + "4" * 64,
                "signature_digest": "sha256:" + "5" * 64,
                "verification_digest": "sha256:" + "6" * 64,
            },
            "repository_state": {
                "repository_writers_observation_digest": "sha256:" + "7" * 64,
                "actions_authority_digest": "sha256:" + "8" * 64,
                "immutable_releases_observation_digest": "sha256:" + "9" * 64,
                "tag_ruleset_observation_digest": "sha256:" + "d" * 64,
                "ingress_observation_digest": "sha256:" + "e" * 64,
            },
            "bindings": {
                "transaction_authorization_digest": None,
                "recovery_capsule_manifest_digest": None,
                "commit_marker_digest": None,
            },
            "authorized_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evidence": {
                "source_bundle_digest": "sha256:" + "f" * 64,
                "canonicalization_vector_digest": (receipts.canonicalization_vector_digest()),
            },
            "provenance": {
                "producer": "scripts/release_promotion_transaction.py",
                "provider": "github.com",
                "method": "server-observation-after-protected-environment",
            },
            "confidence": 1,
            "validation_status": "validated",
        }
    )


def _prepare_assets(
    tmp_path: Path, *, signing_time: datetime = NOW
) -> tuple[Path, bytes, bytes, str]:
    identity, public_key = _signing_identity(tmp_path)
    owner_keys = _source_observation(
        "owner-signing-keys-observation",
        {},
        public_key=public_key,
        captured_at=signing_time,
    )
    candidate_digest = "sha256:" + "c" * 64
    fingerprint = receipts.ssh_public_key_fingerprint(public_key)
    approval = {
        "records": [
            {
                "environment": {"name": "release", "id": 9},
                "reviewer": {
                    "login": receipts.SIGNING_PRINCIPAL,
                    "id": 58918509,
                    "type": "User",
                },
                "state": "approved",
                "observed_record_digest": "sha256:" + "5" * 64,
            }
        ],
        "complete_response_digest": "sha256:" + "6" * 64,
    }
    transaction_raw = _transaction_authorization(approval)
    claim = {
        "schema": "kestrel.recovery_capsule_verification_claim.v1",
        "capsule_manifest_digest": "sha256:" + "f" * 64,
        "candidate_manifest_digest": candidate_digest,
        "transaction_authorization_digest": _sha256(transaction_raw),
        "execution_closure_digest": "sha256:" + "1" * 64,
        "repository": {
            "full_name": subject.RECOVERY_REPOSITORY,
            "id": 304,
            "private": True,
        },
        "release": {"id": 4100, "tag": f"recovery-{RUN_ID}-1", "immutable": True},
        "assets": [
            {
                "id": 5001,
                "name": "recovery-bootstrap.py",
                "size_bytes": 1,
                "sha256": "sha256:" + "2" * 64,
            },
            {
                "id": 5002,
                "name": "recovery-capsule-manifest.json",
                "size_bytes": 1,
                "sha256": "sha256:" + "f" * 64,
            },
            {
                "id": 5003,
                "name": "recovery-capsule.tar",
                "size_bytes": 1,
                "sha256": "sha256:" + "3" * 64,
            },
        ],
        "owner_signing_keys_observation_digest": _sha256(owner_keys),
        "signing_principal": receipts.SIGNING_PRINCIPAL,
        "signing_key_fingerprint": fingerprint,
        "verified_at": signing_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence": {
            "source_bundle_digest": "sha256:" + "4" * 64,
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "immutable-recovery-capsule-verification",
        },
        "verified": True,
        "confidence": 1,
        "validation_status": "validated",
    }
    receipt = _canonical(claim)
    signature = receipts.sign_receipt_detached(
        receipt=receipt,
        identity_file=identity,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    signed = _canonical(
        {
            "schema": "kestrel.recovery_capsule_verification.v1",
            "verification": claim,
            "receipt_digest": _sha256(receipt),
            "signature_digest": _sha256(signature),
            "receipt_base64": base64.b64encode(receipt).decode("ascii"),
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "validation_status": "validated",
        }
    )
    publication = _canonical(
        {
            "schema": "kestrel.recovery_capsule_publication.v1",
            "repository": subject.RECOVERY_REPOSITORY,
            "repository_id": 304,
            "tag": f"recovery-{RUN_ID}-1",
            "release_id": 4100,
            "manifest_digest": "sha256:" + "f" * 64,
            "archive_digest": "sha256:" + "3" * 64,
            "immutable": True,
            "validation_status": "validated",
        }
    )
    approval_observation = _source_observation(
        "approval-history-observation", approval, public_key=public_key
    )
    root = tmp_path / "assets"
    root.mkdir()
    (root / "approval-history-observation.json").write_bytes(approval_observation)
    (root / "recovery-capsule-publication.json").write_bytes(publication)
    (root / "recovery-capsule-verification.json").write_bytes(signed)
    return root, owner_keys, transaction_raw, candidate_digest


def test_prepare_capsule_authority_rejects_approval_from_another_transaction(
    tmp_path: Path,
) -> None:
    assets, owner_keys, transaction_raw, candidate_digest = _prepare_assets(tmp_path)
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    _identity, public_key = _signing_identity(replacement_root)
    replacement_approval = {
        "records": [
            {
                "environment": {"name": "release", "id": 9},
                "reviewer": {
                    "login": receipts.SIGNING_PRINCIPAL,
                    "id": 58918509,
                    "type": "User",
                },
                "state": "approved",
                "observed_record_digest": "sha256:" + "5" * 64,
            }
        ],
        "complete_response_digest": "sha256:" + "7" * 64,
    }
    (assets / "approval-history-observation.json").write_bytes(
        _source_observation(
            "approval-history-observation",
            replacement_approval,
            public_key=public_key,
        )
    )

    with pytest.raises(ValueError, match="approval history differs"):
        subject._validate_prepare_capsule_assets(  # noqa: SLF001
            promotion_run_id=RUN_ID,
            assets=subject._prepare_authority_asset_bytes(assets),  # noqa: SLF001
            candidate_manifest_digest=candidate_digest,
            transaction_authorization=transaction_raw,
            owner_signing_keys_observation=owner_keys,
            source_registry=subject._source_registry(subject.ROOT),  # noqa: SLF001
            _clock=lambda: NOW,
        )


def test_prepare_capsule_authority_replays_durable_approval_after_wall_clock_window(
    tmp_path: Path,
) -> None:
    assets, owner_keys, transaction_raw, candidate_digest = _prepare_assets(
        tmp_path,
        signing_time=NOW + timedelta(days=1),
    )

    digests = subject._validate_prepare_capsule_assets(  # noqa: SLF001
        promotion_run_id=RUN_ID,
        assets=subject._prepare_authority_asset_bytes(assets),  # noqa: SLF001
        candidate_manifest_digest=candidate_digest,
        transaction_authorization=transaction_raw,
        owner_signing_keys_observation=owner_keys,
        source_registry=subject._source_registry(subject.ROOT),  # noqa: SLF001
        _clock=lambda: NOW + timedelta(days=1),
    )

    assert digests["approval_history"] == _sha256(
        (assets / "approval-history-observation.json").read_bytes()
    )


def test_publish_prepare_capsule_authority_is_exact_and_idempotent(
    tmp_path: Path,
) -> None:
    assets, owner_keys, transaction_raw, candidate_digest = _prepare_assets(tmp_path)
    api = FakeTerminalReleaseAPI()
    reader = FakeRecoveryReader(api)
    journal = tmp_path / "journal.json"
    mutation_guards: list[str] = []

    first = subject.publish_prepare_capsule_authority(
        promotion_run_id=RUN_ID,
        asset_root=assets,
        journal_path=journal,
        candidate_manifest_digest=candidate_digest,
        transaction_authorization=transaction_raw,
        owner_signing_keys_observation=owner_keys,
        api=api,
        recovery_reader_api=reader,
        mutation_guard=lambda: mutation_guards.append("guard"),
        _clock=lambda: NOW,
    )
    second = subject.publish_prepare_capsule_authority(
        promotion_run_id=RUN_ID,
        asset_root=assets,
        journal_path=journal,
        candidate_manifest_digest=candidate_digest,
        transaction_authorization=transaction_raw,
        owner_signing_keys_observation=owner_keys,
        api=api,
        recovery_reader_api=reader,
        mutation_guard=lambda: mutation_guards.append("guard"),
        _clock=lambda: NOW,
    )

    assert first == second
    assert first["tag_name"] == f"release-prepare-authority-{RUN_ID}-1"
    assert first["asset_names"] == sorted(subject.PREPARE_AUTHORITY_ASSETS)
    assert api.create_calls == 1
    assert api.upload_calls == sorted(subject.PREPARE_AUTHORITY_ASSETS)
    assert api.publish_calls == 1
    assert mutation_guards == ["guard"] * 5


@pytest.mark.parametrize(
    ("race_at", "guard_ordinal"),
    (("create", 1), ("upload", 2), ("publish", 5)),
)
def test_publish_prepare_capsule_authority_reconciles_after_guard_before_mutation(
    tmp_path: Path,
    race_at: str,
    guard_ordinal: int,
) -> None:
    assets, owner_keys, transaction_raw, candidate_digest = _prepare_assets(tmp_path)
    api = FakeTerminalReleaseAPI()
    reader = FakeRecoveryReader(api)
    journal = tmp_path / "journal.json"
    guard_calls = 0

    def mutation_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls != guard_ordinal:
            return
        if race_at == "create":
            api.releases.append(
                transaction.TerminalRelease(
                    release_id=4999,
                    tag_name=f"release-prepare-authority-{RUN_ID}-1",
                    name="owner-conflict",
                    body="owner-conflict",
                    draft=True,
                    prerelease=False,
                    immutable=False,
                    html_url="https://github.invalid/owner-conflict",
                    assets=(),
                )
            )
            return
        release = api.releases[0]
        conflict = transaction.TerminalReleaseAsset(
            asset_id=5999,
            name="zzz-owner-conflict.json",
            size_bytes=1,
            digest=_sha256(b"x"),
            media_type="application/json",
        )
        api.releases[0] = transaction.TerminalRelease(
            release_id=release.release_id,
            tag_name=release.tag_name,
            name=release.name,
            body=release.body,
            draft=release.draft,
            prerelease=release.prerelease,
            immutable=release.immutable,
            html_url=release.html_url,
            assets=tuple(sorted((*release.assets, conflict), key=lambda item: item.name)),
        )

    with pytest.raises(ValueError, match="conflict|unexpected|ambiguous"):
        subject.publish_prepare_capsule_authority(
            promotion_run_id=RUN_ID,
            asset_root=assets,
            journal_path=journal,
            candidate_manifest_digest=candidate_digest,
            transaction_authorization=transaction_raw,
            owner_signing_keys_observation=owner_keys,
            api=api,
            recovery_reader_api=reader,
            mutation_guard=mutation_guard,
            _clock=lambda: NOW,
        )

    if race_at == "create":
        assert api.create_calls == 0
    elif race_at == "upload":
        assert api.upload_calls == []
    else:
        assert api.publish_calls == 0
        assert api.releases[0].draft is True


def test_publish_prepare_capsule_authority_rejects_conflicting_replay(
    tmp_path: Path,
) -> None:
    assets, owner_keys, transaction_raw, candidate_digest = _prepare_assets(tmp_path)
    api = FakeTerminalReleaseAPI()
    reader = FakeRecoveryReader(api)
    journal = tmp_path / "journal.json"
    subject.publish_prepare_capsule_authority(
        promotion_run_id=RUN_ID,
        asset_root=assets,
        journal_path=journal,
        candidate_manifest_digest=candidate_digest,
        transaction_authorization=transaction_raw,
        owner_signing_keys_observation=owner_keys,
        api=api,
        recovery_reader_api=reader,
        _clock=lambda: NOW,
    )
    publication_path = assets / "recovery-capsule-publication.json"
    changed = json.loads(publication_path.read_bytes())
    changed["archive_digest"] = "sha256:" + "9" * 64
    publication_path.write_bytes(_canonical(changed))

    with pytest.raises(ValueError, match="conflict|binding|journal"):
        subject.publish_prepare_capsule_authority(
            promotion_run_id=RUN_ID,
            asset_root=assets,
            journal_path=journal,
            candidate_manifest_digest=candidate_digest,
            transaction_authorization=transaction_raw,
            owner_signing_keys_observation=owner_keys,
            api=api,
            recovery_reader_api=reader,
            _clock=lambda: NOW,
        )
