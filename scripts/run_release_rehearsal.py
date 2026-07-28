#!/usr/bin/env python3
"""Rehearse Kestrel release publication in a disposable local-only namespace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess  # nosec B404 - fixed local git and Python build commands
import sys
import tomllib
from pathlib import Path
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_release_payload import (  # noqa: E402
    DEFAULT_DISTRIBUTION,
    _verify_sdist_identity,
    _verify_wheel_identity,
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_NAMESPACE_RE = re.compile(r"^kestrel-rehearsal-[a-z0-9][a-z0-9-]{4,79}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if os.name != "nt":
        environment["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return environment


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"command failed ({command[0]}): {detail}")
    return completed.stdout.strip()


def _git(arguments: list[str], *, cwd: Path) -> str:
    return _run(["git", *arguments], cwd=cwd, environment=_git_environment())


def _validate_namespace(namespace: str) -> None:
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(
            "release rehearsal requires a rehearsal-only namespace matching "
            "'kestrel-rehearsal-<lowercase-id>'"
        )


def _validate_source(source_root: Path, commit: str) -> tuple[Path, str, str]:
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError(f"invalid exact source commit: {commit!r}")
    if source_root.is_symlink():
        raise ValueError("source repository root must not be a symlink")
    source_root = source_root.expanduser().resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError(f"source repository is not a directory: {source_root}")
    actual_commit = _git(["rev-parse", "HEAD^{commit}"], cwd=source_root)
    if actual_commit != commit:
        raise ValueError(f"source commit mismatch: {actual_commit} != {commit}")
    dirty = _git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=source_root,
    )
    if dirty:
        raise ValueError("source repository is not clean")
    metadata = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml has no project table")
    distribution = str(project.get("name", ""))
    version = str(project.get("version", ""))
    if distribution != DEFAULT_DISTRIBUTION or not version:
        raise ValueError(
            f"unexpected candidate identity: distribution={distribution!r}, version={version!r}"
        )
    return source_root, distribution, version


def _validate_sandbox(source_root: Path, sandbox_root: Path) -> Path:
    sandbox_root = sandbox_root.expanduser().resolve(strict=False)
    if sandbox_root.exists() or sandbox_root.is_symlink():
        raise ValueError(f"rehearsal sandbox must not already exist: {sandbox_root}")
    parent = sandbox_root.parent.resolve(strict=True)
    sandbox_root = parent / sandbox_root.name
    if sandbox_root == source_root or sandbox_root.is_relative_to(source_root):
        raise ValueError("rehearsal sandbox must be outside the source repository")
    if source_root.is_relative_to(sandbox_root):
        raise ValueError("rehearsal sandbox must not contain the source repository")
    return sandbox_root


def _publish_exact(source: Path, target: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"publication source is not a regular file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"publication target is not a regular file: {target}")
        target_stat = target.stat()
        if target_stat.st_nlink != 1:
            raise ValueError(f"publication target has unsafe link count: {target}")
        if _sha256(source) != _sha256(target):
            raise ValueError(f"publication collision for {target.name}")
        return "already_exact"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            descriptor = -1
            for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        target.unlink(missing_ok=True)
        raise
    return "published"


def _publish_finalized_exact(source: Path, target: Path, marker: Path) -> str:
    """Verify an exact replay without permitting any finalized-state mutation."""

    if marker.is_symlink() or not marker.is_file():
        raise ValueError("release is not finalized by a regular marker")
    if marker.stat().st_nlink != 1:
        raise ValueError("release finalization marker has an unsafe link count")
    try:
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("release finalization marker is invalid") from exc
    if not isinstance(marker_payload, dict) or marker_payload.get("state") != "finalized":
        raise ValueError("release finalization marker does not declare finalized state")
    manifest = marker_payload.get("artifacts")
    if not isinstance(manifest, dict):
        raise ValueError("release finalization marker has no artifact manifest")
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"publication source is not a regular file: {source}")
    if target.is_symlink() or not target.is_file():
        raise ValueError(
            f"finalized release refuses mutation for missing or unsafe target: {target}"
        )
    if target.stat().st_nlink != 1:
        raise ValueError(f"finalized release refuses mutation for linked target: {target}")
    declared_digest = manifest.get(target.name)
    source_digest = _sha256(source)
    target_digest = _sha256(target)
    if (
        not isinstance(declared_digest, str)
        or declared_digest != source_digest
        or source_digest != target_digest
    ):
        raise ValueError(f"finalized release refuses mutation for {target.name}")
    return "already_exact"


def _create_ref(repository: Path, ref: str, commit: str, *, cwd: Path) -> None:
    """Create one rehearsal ref only when it does not already exist."""

    try:
        _git(
            [
                f"--git-dir={repository}",
                "update-ref",
                ref,
                commit,
                "0" * 40,
            ],
            cwd=cwd,
        )
    except ValueError as exc:
        raise ValueError(f"rehearsal ref already exists or is invalid: {ref}") from exc


def _write_finalization_marker(
    marker: Path,
    *,
    commit: str,
    namespace: str,
    tag_ref: str,
    artifacts: list[Path],
) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": "finalized",
        "commit": commit,
        "namespace": namespace,
        "tag_ref": tag_ref,
        "artifacts": {artifact.name: _sha256(artifact) for artifact in artifacts},
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(marker, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        marker.unlink(missing_ok=True)
        raise


def _build_distributions(
    checkout: Path,
    output_root: Path,
    *,
    commit: str,
    distribution: str,
    version: str,
) -> list[Path]:
    output_root.mkdir()
    source_epoch = _git(["show", "-s", "--format=%ct", commit], cwd=checkout)
    environment = os.environ.copy()
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "SOURCE_DATE_EPOCH": source_epoch,
        }
    )
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            str(output_root),
        ],
        cwd=checkout,
        environment=environment,
    )
    entries = sorted(output_root.iterdir(), key=lambda path: path.name)
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ValueError("build output contains a non-regular artifact")
    wheels = [path for path in entries if path.name.endswith(".whl")]
    sdists = [path for path in entries if path.name.endswith(".tar.gz")]
    if len(entries) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            f"expected exactly one wheel and one sdist, got {[path.name for path in entries]}"
        )
    _verify_wheel_identity(
        wheels[0],
        expected_distribution=distribution,
        expected_version=version,
    )
    _verify_sdist_identity(
        sdists[0],
        expected_distribution=distribution,
        expected_version=version,
    )
    return entries


def _verify_exact_set(source_root: Path, downloaded_root: Path) -> None:
    source = {
        path.name: _sha256(path)
        for path in source_root.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    downloaded = {
        path.name: _sha256(path)
        for path in downloaded_root.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if source != downloaded:
        raise ValueError(
            "downloaded rehearsal asset set differs from the candidate: "
            f"source={sorted(source)}, downloaded={sorted(downloaded)}"
        )


def run_release_rehearsal(
    *,
    source_root: Path,
    sandbox_root: Path,
    namespace: str,
    commit: str,
) -> dict[str, Any]:
    _validate_namespace(namespace)
    source_root, distribution, version = _validate_source(source_root, commit)
    sandbox_root = _validate_sandbox(source_root, sandbox_root)
    sandbox_root.mkdir(mode=0o700)
    steps = ["source_verified"]

    remote = sandbox_root / "repository.git"
    _git(["init", "--bare", "-q", str(remote)], cwd=sandbox_root)
    _git(
        [
            "-c",
            "protocol.file.allow=always",
            f"--git-dir={remote}",
            "fetch",
            "--no-tags",
            str(source_root),
            commit,
        ],
        cwd=sandbox_root,
    )
    fetched = _git([f"--git-dir={remote}", "rev-parse", "FETCH_HEAD^{commit}"], cwd=sandbox_root)
    if fetched != commit:
        raise ValueError(f"disposable repository fetched the wrong commit: {fetched}")
    steps.append("disposable_repository_created")

    head_ref = f"refs/heads/rehearsal/{namespace}"
    tag_ref = f"refs/tags/rehearsal/{namespace}/{commit[:12]}"
    for ref in (head_ref, tag_ref):
        _create_ref(remote, ref, commit, cwd=sandbox_root)
    refs_text = _git(
        [
            f"--git-dir={remote}",
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ],
        cwd=sandbox_root,
    )
    refs = sorted(line for line in refs_text.splitlines() if line)
    expected_refs = sorted([f"{head_ref} {commit}", f"{tag_ref} {commit}"])
    if refs != expected_refs or any(line.startswith("refs/tags/v") for line in refs):
        raise ValueError(f"disposable repository contains unexpected refs: {refs}")
    steps.append("rehearsal_refs_created")

    checkout = sandbox_root / "checkout"
    _git(
        [
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--no-hardlinks",
            "--no-tags",
            str(remote),
            str(checkout),
        ],
        cwd=sandbox_root,
    )
    _git(["checkout", "--detach", commit], cwd=checkout)
    if _git(["rev-parse", "HEAD^{commit}"], cwd=checkout) != commit:
        raise ValueError("disposable checkout does not match the candidate commit")

    candidate_root = sandbox_root / "candidate"
    artifacts = _build_distributions(
        checkout,
        candidate_root,
        commit=commit,
        distribution=distribution,
        version=version,
    )
    steps.extend(["distributions_built", "distribution_identity_verified"])

    release_root = sandbox_root / "release" / namespace
    release_assets = release_root / "draft" / "assets"
    release_assets.mkdir(parents=True)
    steps.append("draft_created")
    upload_status = {
        artifact.name: _publish_exact(artifact, release_assets / artifact.name)
        for artifact in artifacts
    }
    steps.append("exact_assets_uploaded")

    downloaded_root = sandbox_root / "downloaded"
    downloaded_root.mkdir()
    for artifact in sorted(release_assets.iterdir(), key=lambda path: path.name):
        _publish_exact(artifact, downloaded_root / artifact.name)
    _verify_exact_set(candidate_root, downloaded_root)
    steps.append("downloaded_assets_verified")

    package_root = sandbox_root / "package-index" / namespace / distribution / version
    package_status = {
        artifact.name: _publish_exact(artifact, package_root / artifact.name)
        for artifact in artifacts
    }
    steps.append("package_files_published")
    mutation_probe = sandbox_root / "conflicting-post-finalization-probe"
    mutation_probe.write_bytes(b"kestrel-finalized-mutation-probe")
    finalization_marker = release_root / "FINALIZED.json"
    _write_finalization_marker(
        finalization_marker,
        commit=commit,
        namespace=namespace,
        tag_ref=tag_ref,
        artifacts=artifacts,
    )
    steps.append("release_marked_immutable")
    replay_status = {
        artifact.name: _publish_finalized_exact(
            artifact,
            package_root / artifact.name,
            finalization_marker,
        )
        for artifact in artifacts
    }
    if set(replay_status.values()) != {"already_exact"}:
        raise ValueError(f"rehearsal replay was not an exact no-op: {replay_status}")
    _verify_exact_set(candidate_root, package_root)
    steps.append("exact_replay_verified")
    conflicting_target = package_root / artifacts[0].name
    before_conflict = _sha256(conflicting_target)
    try:
        _publish_finalized_exact(
            mutation_probe,
            conflicting_target,
            finalization_marker,
        )
    except ValueError as exc:
        if "finalized release refuses mutation" not in str(exc):
            raise
    else:
        raise ValueError("finalized rehearsal unexpectedly permitted conflicting mutation")
    if _sha256(conflicting_target) != before_conflict:
        raise ValueError("finalized rehearsal mutation probe changed published bytes")
    steps.append("conflicting_post_finalization_mutation_rejected")

    artifact_reports = [
        {
            "name": artifact.name,
            "sha256": _sha256(artifact),
            "release_upload": upload_status[artifact.name],
            "package_publish": package_status[artifact.name],
            "replay": replay_status[artifact.name],
        }
        for artifact in artifacts
    ]
    return {
        "schema": "kestrel.release_rehearsal_report.v1",
        "source": {
            "commit": commit,
            "distribution": distribution,
            "version": version,
        },
        "namespace": namespace,
        "repository": {
            "head_ref": head_ref,
            "tag_ref": tag_ref,
            "refs": refs,
        },
        "artifacts": artifact_reports,
        "finalization": {
            "marker": finalization_marker.relative_to(sandbox_root).as_posix(),
            "conflicting_mutation_rejected": True,
        },
        "steps": steps,
        "production_targets_blocked": True,
        "passed": True,
    }


def _write_json(path: Path, payload: object) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--sandbox-root", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run_release_rehearsal(
            source_root=args.source_root,
            sandbox_root=args.sandbox_root,
            namespace=args.namespace,
            commit=args.commit,
        )
        _write_json(args.output, report)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
