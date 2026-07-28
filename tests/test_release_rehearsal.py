from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.run_release_rehearsal import (
    _create_ref,
    _publish_exact,
    _publish_finalized_exact,
    run_release_rehearsal,
)


def _candidate_repository(root: Path) -> tuple[Path, str]:
    source = root / "source"
    package = source / "src" / "nested_memvid_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (source / "README.md").write_text("# rehearsal fixture\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools==83.0.0"]
build-backend = "setuptools.build_meta"

[project]
name = "nested-memvid-agent"
version = "1.2.3"
description = "Release rehearsal fixture"
readme = "README.md"
requires-python = ">=3.11"

[tool.setuptools.packages.find]
where = ["src"]
""".lstrip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", source], check=True)
    subprocess.run(
        ["git", "-C", source, "config", "user.email", "rehearsal@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", source, "config", "user.name", "Kestrel Rehearsal"],
        check=True,
    )
    subprocess.run(["git", "-C", source, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", source, "commit", "-q", "-m", "fixture"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", source, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return source, commit


def test_rehearsal_rejects_production_namespace_before_creating_sandbox(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"

    with pytest.raises(ValueError, match="rehearsal-only namespace"):
        run_release_rehearsal(
            source_root=tmp_path,
            sandbox_root=sandbox,
            namespace="John-MiracleWorker/Kestrel",
            commit="a" * 40,
        )

    assert not sandbox.exists()


def test_rehearsal_uses_disposable_refs_and_exact_package_bytes(tmp_path: Path) -> None:
    source, commit = _candidate_repository(tmp_path)
    sandbox = tmp_path / "sandbox"

    report = run_release_rehearsal(
        source_root=source,
        sandbox_root=sandbox,
        namespace="kestrel-rehearsal-ci-12345",
        commit=commit,
    )

    assert report["passed"] is True
    assert report["production_targets_blocked"] is True
    assert report["source"] == {
        "commit": commit,
        "distribution": "nested-memvid-agent",
        "version": "1.2.3",
    }
    assert report["repository"]["refs"] == [
        f"refs/heads/rehearsal/kestrel-rehearsal-ci-12345 {commit}",
        f"refs/tags/rehearsal/kestrel-rehearsal-ci-12345/{commit[:12]} {commit}",
    ]
    assert report["steps"] == [
        "source_verified",
        "disposable_repository_created",
        "rehearsal_refs_created",
        "distributions_built",
        "distribution_identity_verified",
        "draft_created",
        "exact_assets_uploaded",
        "downloaded_assets_verified",
        "package_files_published",
        "release_marked_immutable",
        "exact_replay_verified",
        "conflicting_post_finalization_mutation_rejected",
    ]
    package_root = (
        sandbox / "package-index" / "kestrel-rehearsal-ci-12345" / "nested-memvid-agent" / "1.2.3"
    )
    published = sorted(path.name for path in package_root.iterdir())
    assert any(name.endswith(".whl") for name in published)
    assert any(name.endswith(".tar.gz") for name in published)
    assert all(item["replay"] == "already_exact" for item in report["artifacts"])
    assert report["finalization"]["conflicting_mutation_rejected"] is True


def test_exact_publication_refuses_collision_without_changing_existing_bytes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.whl"
    conflicting = tmp_path / "conflicting.whl"
    target = tmp_path / "index" / "candidate.whl"
    first.write_bytes(b"trusted")
    conflicting.write_bytes(b"different")

    assert _publish_exact(first, target) == "published"
    assert _publish_exact(first, target) == "already_exact"
    with pytest.raises(ValueError, match="publication collision"):
        _publish_exact(conflicting, target)

    assert target.read_bytes() == b"trusted"


def test_rehearsal_ref_creation_is_create_only(tmp_path: Path) -> None:
    repository = tmp_path / "repository.git"
    subprocess.run(["git", "init", "--bare", "-q", repository], check=True)
    ref = "refs/tags/rehearsal/kestrel-rehearsal-test"
    first = (
        subprocess.run(
            ["git", f"--git-dir={repository}", "hash-object", "-w", "--stdin"],
            input=b"first",
            check=True,
            capture_output=True,
            text=False,
        )
        .stdout.decode()
        .strip()
    )
    second = (
        subprocess.run(
            ["git", f"--git-dir={repository}", "hash-object", "-w", "--stdin"],
            input=b"second",
            check=True,
            capture_output=True,
            text=False,
        )
        .stdout.decode()
        .strip()
    )

    _create_ref(repository, ref, first, cwd=tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        _create_ref(repository, ref, second, cwd=tmp_path)

    actual = subprocess.run(
        ["git", f"--git-dir={repository}", "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert actual == first


def test_finalized_publication_allows_exact_replay_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted.whl"
    conflicting = tmp_path / "conflicting.whl"
    target = tmp_path / "index" / "candidate.whl"
    marker = tmp_path / "release" / "FINALIZED.json"
    trusted.write_bytes(b"trusted")
    conflicting.write_bytes(b"different")
    assert _publish_exact(trusted, target) == "published"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "state": "finalized",
                "artifacts": {target.name: hashlib.sha256(trusted.read_bytes()).hexdigest()},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert _publish_finalized_exact(trusted, target, marker) == "already_exact"
    with pytest.raises(ValueError, match="finalized release refuses mutation"):
        _publish_finalized_exact(conflicting, target, marker)

    assert target.read_bytes() == b"trusted"


def test_rehearsal_refuses_dirty_source_before_creating_sandbox(tmp_path: Path) -> None:
    source, commit = _candidate_repository(tmp_path)
    (source / "README.md").write_text("dirty\n", encoding="utf-8")
    sandbox = tmp_path / "sandbox"

    with pytest.raises(ValueError, match="source repository is not clean"):
        run_release_rehearsal(
            source_root=source,
            sandbox_root=sandbox,
            namespace="kestrel-rehearsal-ci-12345",
            commit=commit,
        )

    assert not sandbox.exists()
