from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import nested_memvid_agent.repo_index.indexer as repo_indexer
import nested_memvid_agent.repo_index.store as repo_store
from nested_memvid_agent.file_lock import (
    lock_exclusive as real_lock_exclusive,
)
from nested_memvid_agent.repo_index import (
    DEFAULT_QUERY_LIMIT,
    MAX_QUERY_LIMIT,
    Freshness,
    IndexLimits,
    RepositoryChangedDuringIndexingError,
    RepositoryIndex,
    RepositoryIndexError,
    RepositoryRootMismatchError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "repo_index"


def _copy_fixture(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    shutil.copytree(FIXTURE, repository)
    return repository


def _row_ids(index_path: Path, table: str) -> list[tuple[int, int]]:
    with sqlite3.connect(index_path) as connection:
        return [
            (int(row[0]), int(row[1]))
            for row in connection.execute(
                f"""
                SELECT {table}.id, files.id
                FROM {table}
                JOIN files ON files.id = {table}.file_id
                ORDER BY {table}.id
                """
            )
        ]


def test_repository_index_opens_artifacts_in_binary_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = repo_store.RepoIndexStore(tmp_path / "index.sqlite")
    observed: dict[str, object] = {}
    binary_flag = 1 << 29

    def fake_open(path: object, flags: int, mode: int = 0o600) -> int:
        observed.update(path=path, flags=flags, mode=mode)
        return 17

    monkeypatch.setattr(repo_store, "_binary_open_flag", lambda: binary_flag)
    monkeypatch.setattr(repo_store.os, "open", fake_open)

    assert (
        store._open_relative(
            "index.sqlite",
            os.O_RDONLY,
            parent_descriptor=None,
        )
        == 17
    )
    assert int(observed["flags"]) & binary_flag


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_dotted_project_id_matches_project_profile_contract(tmp_path: Path) -> None:
    repository = _copy_fixture(tmp_path)

    index = RepositoryIndex(
        project_id="project.tools",
        repository_root=repository,
    )
    index.rebuild()

    assert index.status().project_id == "project.tools"
    assert index.index_path.name == "project.tools.sqlite"


def _convert_current_sidecar_to_v4(index: RepositoryIndex) -> None:
    with sqlite3.connect(index.index_path) as connection:
        generation_id = str(
            connection.execute(
                """
                SELECT generation_id
                FROM index_generation_checkpoint
                WHERE singleton = 1
                """
            ).fetchone()[0]
        )
        connection.execute("DROP TABLE index_generation_checkpoint")
        connection.execute("PRAGMA user_version = 4")
    generation_path = index.index_path.parent / (
        f".{index.index_path.name}.generation-{generation_id}"
    )
    receipt_path = generation_path.with_name(f"{generation_path.name}.receipt")
    generation_path.unlink()
    receipt_path.unlink()
    os.link(index.index_path, generation_path)
    lock_path = index.index_path.parent / f".{index.index_path.name}.lock"
    lock_payload = lock_path.read_bytes()
    encoded_state = lock_payload[len(repo_store._LOCK_SECRET_PREFIX) :]
    secret_line = encoded_state.splitlines()[0]
    lock_path.write_bytes(
        repo_store._LOCK_SECRET_PREFIX + secret_line + b"\n"
    )
    lock_path.chmod(0o600)


def test_build_records_identity_content_and_multilanguage_relationships(tmp_path: Path) -> None:
    """Removing any required evidence table or parser adapter must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)

    report = index.rebuild()
    status = index.status()

    assert report.changed_files == 10
    assert report.reused_files == 0
    assert report.deleted_files == 0
    assert status.freshness is Freshness.CURRENT
    assert status.project_id == "project-1"
    assert status.repository_root == repository.resolve()
    assert status.aggregate_digest == report.aggregate_digest
    assert status.schema_version == 5
    assert status.parser_versions["python"] == "ast-v1"
    assert status.git_head is None
    assert status.git_tree is None
    assert index.index_path == repository / ".nest" / "repo-index" / "project-1.sqlite"
    assert not list(repository.rglob("*.mv2"))
    if os.name == "posix":
        assert index.index_path.stat().st_mode & 0o777 == 0o600
        assert index.index_path.parent.stat().st_mode & 0o777 == 0o700

    symbols = {
        (record.name, record.kind, record.path.as_posix()) for record in index.symbols().records
    }
    assert {
        ("Widget", "class", "src/widget.py"),
        ("helper", "function", "src/widget.py"),
        ("test_widget_render", "function", "tests/widget_checks.py"),
        ("WebWidget", "class", "web/widget.ts"),
        ("render", "method", "web/widget.ts"),
        ("makeWidget", "function", "web/widget.ts"),
        ("GoWidget", "type", "cmd/widget.go"),
        ("BuildWidget", "function", "cmd/widget.go"),
        ("RustWidget", "struct", "crates/widget.rs"),
        ("show_widget", "function", "crates/widget.rs"),
        ("JavaWidget", "class", "jvm/Widget.java"),
        ("render", "method", "jvm/Widget.java"),
        ("KotlinWidget", "class", "jvm/Widget.kt"),
        ("renderWidget", "function", "jvm/Widget.kt"),
        ("SwiftWidget", "struct", "apple/Widget.swift"),
        ("renderWidget", "function", "apple/Widget.swift"),
    }.issubset(symbols)

    imports = {(record.module, record.path.as_posix()) for record in index.imports().records}
    assert {
        ("collections.deque", "src/widget.py"),
        ("src.widget.Widget", "tests/widget_checks.py"),
        ("./format", "web/widget.ts"),
        ("fmt", "cmd/widget.go"),
        ("std::fmt::Display", "crates/widget.rs"),
        ("java.util.Objects", "jvm/Widget.java"),
        ("kotlin.text.trim", "jvm/Widget.kt"),
        ("Foundation", "apple/Widget.swift"),
    }.issubset(imports)

    references = index.references("helper").records
    assert [(record.path.as_posix(), record.line) for record in references] == [
        ("README.md", 3),
        ("src/widget.py", 6),
        ("tests/widget_checks.py", 1),
        ("tests/widget_checks.py", 5),
    ]

    owned_tests = index.tests_for("Widget").records
    assert [
        (record.symbol_name, record.test_path.as_posix(), record.relationship)
        for record in owned_tests
    ] == [("Widget", "tests/widget_checks.py", "symbol_reference")]


def test_build_records_git_head_and_tree_when_available(tmp_path: Path) -> None:
    """Dropping available Git identity metadata must fail this test."""
    repository = _copy_fixture(tmp_path)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Kestrel Test")
    _git(repository, "config", "user.email", "kestrel@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "fixture")
    expected_head = _git(repository, "rev-parse", "HEAD")
    expected_tree = _git(repository, "rev-parse", "HEAD^{tree}")

    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    report = index.rebuild()
    status = index.status()

    assert (report.git_head, report.git_tree) == (expected_head, expected_tree)
    assert (status.git_head, status.git_tree) == (expected_head, expected_tree)


def test_no_change_rebuild_preserves_file_and_child_rows(tmp_path: Path) -> None:
    """Rewriting unchanged file or symbol rows must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    first = index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        file_rows_before = list(
            connection.execute("SELECT id, path, digest FROM files ORDER BY id")
        )
    symbol_rows_before = _row_ids(index.index_path, "symbols")
    reference_rows_before = _row_ids(index.index_path, "lexical_references")

    second = index.rebuild()

    with sqlite3.connect(index.index_path) as connection:
        file_rows_after = list(connection.execute("SELECT id, path, digest FROM files ORDER BY id"))
    assert second.aggregate_digest == first.aggregate_digest
    assert second.changed_files == 0
    assert second.reused_files == 10
    assert second.deleted_files == 0
    assert file_rows_after == file_rows_before
    assert _row_ids(index.index_path, "symbols") == symbol_rows_before
    assert _row_ids(index.index_path, "lexical_references") == reference_rows_before


def test_change_and_deletion_update_only_affected_rows_and_digest(tmp_path: Path) -> None:
    """Failing to replace changed rows or remove deleted rows must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    first = index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        original_ids = dict(connection.execute("SELECT path, id FROM files"))

    changed = repository / "src" / "widget.py"
    changed.write_text(
        changed.read_text(encoding="utf-8").replace("def helper", "def normalize"),
        encoding="utf-8",
    )
    (repository / "README.md").unlink()
    second = index.rebuild()

    with sqlite3.connect(index.index_path) as connection:
        current_ids = dict(connection.execute("SELECT path, id FROM files"))
    assert second.changed_files == 1
    assert second.reused_files == 8
    assert second.deleted_files == 1
    assert second.aggregate_digest != first.aggregate_digest
    assert "README.md" not in current_ids
    assert current_ids["web/widget.ts"] == original_ids["web/widget.ts"]
    assert current_ids["src/widget.py"] != original_ids["src/widget.py"]
    assert index.symbols("helper").records == ()
    assert [
        (record.name, record.path.as_posix()) for record in index.symbols("normalize").records
    ] == [("normalize", "src/widget.py")]

    clean_rebuild = RepositoryIndex(
        project_id="project-2",
        repository_root=repository,
        index_path=tmp_path / "clean.sqlite",
    )
    assert clean_rebuild.rebuild().aggregate_digest == second.aggregate_digest


def test_queries_label_stale_evidence_and_require_explicit_diagnostics(tmp_path: Path) -> None:
    """Returning stale rows as normal evidence must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()

    (repository / "src" / "widget.py").write_text(
        "class Replacement:\n    pass\n",
        encoding="utf-8",
    )

    status = index.status()
    hidden = index.symbols("Widget")
    diagnostic = index.symbols("Widget", include_stale_diagnostics=True)
    assert status.freshness is Freshness.STALE
    assert hidden.freshness is Freshness.STALE
    assert hidden.authoritative is False
    assert hidden.records == ()
    assert diagnostic.freshness is Freshness.STALE
    assert diagnostic.authoritative is False
    assert ("Widget", "src/widget.py") in {
        (record.name, record.path.as_posix()) for record in diagnostic.records
    }


def test_parser_version_change_marks_stale_and_forces_reparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treating rows from an obsolete parser as current must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    monkeypatch.setitem(repo_indexer.PARSER_VERSIONS, "python", "ast-v2")

    reopened = RepositoryIndex(project_id="project-1", repository_root=repository)
    assert reopened.status().freshness is Freshness.STALE
    rebuilt = reopened.rebuild()
    assert rebuilt.changed_files == 10
    assert rebuilt.reused_files == 0
    assert reopened.status().freshness is Freshness.CURRENT


def test_schema_v1_sidecar_migrates_and_forces_digest_revalidation(tmp_path: Path) -> None:
    """Leaving migrated rows without filesystem identity must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        connection.execute("ALTER TABLE files DROP COLUMN device")
        connection.execute("ALTER TABLE files DROP COLUMN inode")
        connection.execute("DROP TABLE index_generation_checkpoint")
        connection.execute("DROP TABLE index_generations")
        connection.execute("PRAGMA user_version = 1")
        connection.execute("PRAGMA application_id = 0")
    for artifact in index.index_path.parent.glob(f".{index.index_path.name}.generation-*"):
        artifact.unlink()
    # A real v1 sidecar predates the authenticated publication lock. Remove the
    # current-format fixture lock instead of asking migration to overwrite its
    # valid high-water authority with an unrelated legacy lineage.
    (index.index_path.parent / f".{index.index_path.name}.lock").unlink()

    reopened = RepositoryIndex(project_id="project-1", repository_root=repository)
    rebuilt = reopened.rebuild()

    assert reopened.status().schema_version == 5
    assert rebuilt.changed_files == 10
    assert rebuilt.reused_files == 0


def test_root_move_or_replacement_fails_closed(tmp_path: Path) -> None:
    """Following a moved or replacement root by path must fail this test."""
    repository = _copy_fixture(tmp_path)
    index_path = tmp_path / "index.sqlite"
    index = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        index_path=index_path,
    )
    index.rebuild()

    moved = tmp_path / "moved"
    repository.rename(moved)
    with pytest.raises(RepositoryRootMismatchError, match="repository root"):
        index.status()

    repository.mkdir()
    (repository / "new.py").write_text("class NewRoot: ...\n", encoding="utf-8")
    replacement = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        index_path=index_path,
    )
    with pytest.raises(RepositoryRootMismatchError, match="identity"):
        replacement.rebuild()


def test_renamed_root_replaced_by_symlink_to_original_fails_closed(tmp_path: Path) -> None:
    """Following a replacement symlink to the original inode must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        index_path=tmp_path / "index.sqlite",
    )
    index.rebuild()
    moved = tmp_path / "moved"
    repository.rename(moved)
    os.symlink(moved, repository, target_is_directory=True)

    with pytest.raises(RepositoryRootMismatchError, match="symbolic link"):
        index.status()


def test_scan_ignores_symlinks_private_build_vendor_binary_and_oversize(
    tmp_path: Path,
) -> None:
    """Indexing excluded or unsafe candidates must fail this test."""
    repository = _copy_fixture(tmp_path)
    (repository / ".nest" / "private").mkdir(parents=True)
    (repository / ".nest" / "private" / "secret.py").write_text(
        "class Secret: ...\n", encoding="utf-8"
    )
    (repository / "node_modules" / "package").mkdir(parents=True)
    (repository / "node_modules" / "package" / "vendor.ts").write_text(
        "export class Vendor {}\n", encoding="utf-8"
    )
    (repository / "dist").mkdir()
    (repository / "dist" / "bundle.js").write_text("class BuiltArtifact {}\n", encoding="utf-8")
    (repository / "binary.dat").write_bytes(b"\x00\x01\x02")
    (repository / "control.txt").write_bytes(b"\x01\x02\x03printable-text")
    (repository / "oversize.py").write_text(
        "class TooLarge:\n    pass\n" + ("#" * 100), encoding="utf-8"
    )
    os.symlink(repository / "src" / "widget.py", repository / "linked.py")

    index = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        limits=IndexLimits(max_file_bytes=80),
    )
    report = index.rebuild()
    paths = {
        record.path.as_posix()
        for record in index.files(include_stale_diagnostics=True).records
    }

    assert index.status().freshness is Freshness.STALE
    assert index.status().coverage_complete is False
    assert "web/format.ts" in paths
    assert {
        ".nest/private/secret.py",
        "node_modules/package/vendor.ts",
        "dist/bundle.js",
        "binary.dat",
        "control.txt",
        "oversize.py",
        "linked.py",
    }.isdisjoint(paths)
    assert report.skipped_files >= 3


def test_default_sidecar_does_not_follow_private_directory_symlink(
    tmp_path: Path,
) -> None:
    """Writing the index through a repository-controlled symlink must fail this test."""
    repository = _copy_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repository / ".nest", target_is_directory=True)

    with pytest.raises(RepositoryIndexError, match="sidecar"):
        RepositoryIndex(project_id="project-1", repository_root=repository)

    assert not (outside / "repo-index").exists()


def test_sidecar_parent_substitution_is_rejected_on_every_open(tmp_path: Path) -> None:
    """Following a substituted sidecar parent after initialization must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    sidecar_parent = index.index_path.parent
    original_parent = sidecar_parent.with_name("repo-index-original")
    sidecar_parent.rename(original_parent)
    os.symlink(original_parent, sidecar_parent, target_is_directory=True)

    with pytest.raises(RepositoryIndexError, match="sidecar parent"):
        index.status()


def test_sidecar_file_replacement_is_rejected_on_next_open(tmp_path: Path) -> None:
    """Trusting a replacement database with copied metadata must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    replacement = tmp_path / "replacement.sqlite"
    shutil.copy2(index.index_path, replacement)
    os.replace(replacement, index.index_path)

    with pytest.raises(RepositoryIndexError, match="database identity"):
        index.status()


def test_durable_generation_manifest_rejects_replacement_after_restart(
    tmp_path: Path,
) -> None:
    """A copied atomic image must not become trusted merely by reopening the store."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    replacement = tmp_path / "replacement.sqlite"
    shutil.copy2(index.index_path, replacement)
    os.replace(replacement, index.index_path)

    with pytest.raises(RepositoryIndexError, match="database identity"):
        RepositoryIndex(project_id="project-1", repository_root=repository)


def test_schema_v4_with_missing_generation_ledger_cannot_be_readmitted(
    tmp_path: Path,
) -> None:
    """A forged current-format sidecar must not be republished as fresh legacy."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        connection.execute("PRAGMA user_version = 4")
        connection.execute("DROP TABLE IF EXISTS index_generation_checkpoint")
        connection.execute("DELETE FROM index_generations")
        connection.execute(
            """
            UPDATE index_metadata
            SET aggregate_digest = 'forged', project_id = 'project-1'
            WHERE singleton = 1
            """
        )

    with pytest.raises(RepositoryIndexError, match="generation ledger"):
        RepositoryIndex(project_id="project-1", repository_root=repository)


def test_schema_v4_with_durable_generation_migrates_to_checkpoint(tmp_path: Path) -> None:
    """A genuine v4 sidecar remains eligible for the one-time v5 migration."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    _convert_current_sidecar_to_v4(index)

    reopened = RepositoryIndex(project_id="project-1", repository_root=repository)

    assert reopened.status().schema_version == 5
    with sqlite3.connect(reopened.index_path) as connection:
        checkpoint = connection.execute(
            """
            SELECT lineage_id, sequence, generation_id, authorization_tag
            FROM index_generation_checkpoint
            WHERE singleton = 1
            """
        ).fetchone()
    assert checkpoint is not None
    assert len(str(checkpoint[0])) == 32
    assert int(checkpoint[1]) > 0
    assert len(str(checkpoint[2])) == 32
    assert len(str(checkpoint[3])) == 64


def test_schema_downgrade_with_generation_manifest_fails_closed(tmp_path: Path) -> None:
    """Durable generation artifacts make a user-version downgrade ineligible."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(RepositoryIndexError, match="legacy"):
        RepositoryIndex(project_id="project-1", repository_root=repository)


def test_generation_identifier_cannot_escape_the_sidecar_parent(tmp_path: Path) -> None:
    """Generation-ledger text must never become an unchecked relative path."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        connection.execute(
            """
            UPDATE index_generations
            SET generation_id = '../../outside'
            WHERE sequence = (SELECT MAX(sequence) FROM index_generations)
            """
        )

    with pytest.raises(RepositoryIndexError, match="generation identifier"):
        index.status()


def test_generation_snapshot_is_not_a_mutable_alias_of_the_canonical_sidecar(
    tmp_path: Path,
) -> None:
    """The durable generation image must not share the canonical SQLite inode."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        generation_id = str(
            connection.execute(
                """
                SELECT generation_id
                FROM index_generation_checkpoint
                WHERE singleton = 1
                """
            ).fetchone()[0]
        )
    snapshot = index.index_path.parent / (f".{index.index_path.name}.generation-{generation_id}")

    canonical_stat = index.index_path.stat()
    snapshot_stat = snapshot.stat()

    assert (canonical_stat.st_dev, canonical_stat.st_ino) != (
        snapshot_stat.st_dev,
        snapshot_stat.st_ino,
    )


def test_same_inode_metadata_tampering_fails_content_authentication_after_restart(
    tmp_path: Path,
) -> None:
    """A forged aggregate digest must not regain authority after reopening."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    canonical_inode = index.index_path.stat().st_ino
    with sqlite3.connect(index.index_path) as connection:
        connection.execute(
            """
            UPDATE index_metadata
            SET aggregate_digest = 'forged-authoritative-digest'
            WHERE singleton = 1
            """
        )
    assert index.index_path.stat().st_ino == canonical_inode

    with pytest.raises(RepositoryIndexError, match="content"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            create=False,
        )


def test_forged_semantic_row_cannot_be_returned_as_authoritative_after_restart(
    tmp_path: Path,
) -> None:
    """Checkpoint authentication must cover semantic repository-index content."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        connection.execute(
            """
            UPDATE symbols
            SET name = 'forged_authority', qualified_name = 'forged_authority'
            WHERE id = (SELECT MIN(id) FROM symbols)
            """
        )

    with pytest.raises(RepositoryIndexError, match="content"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            create=False,
        )


def test_fresh_process_rejects_sidecar_rollback_below_lock_high_water(
    tmp_path: Path,
) -> None:
    """Restoring an older valid generation must not roll back durable authority."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        generation_id = str(
            connection.execute(
                """
                SELECT generation_id
                FROM index_generation_checkpoint
                WHERE singleton = 1
                """
            ).fetchone()[0]
        )
    generation_name = f".{index.index_path.name}.generation-{generation_id}"
    snapshot = index.index_path.parent / generation_name
    receipt = snapshot.with_name(f"{snapshot.name}.receipt")
    old_canonical = tmp_path / "old-canonical.sqlite"
    old_snapshot = tmp_path / "old-snapshot.sqlite"
    old_receipt = tmp_path / "old-receipt.json"
    shutil.copy2(index.index_path, old_canonical)
    shutil.copy2(snapshot, old_snapshot)
    shutil.copy2(receipt, old_receipt)

    changed = repository / "src" / "widget.py"
    changed.write_text(
        changed.read_text(encoding="utf-8").replace("def helper", "def normalize"),
        encoding="utf-8",
    )
    index.rebuild()

    shutil.copy2(old_canonical, index.index_path)
    shutil.copy2(old_snapshot, snapshot)
    shutil.copy2(old_receipt, receipt)

    with pytest.raises(RepositoryIndexError, match="rollback"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            create=False,
        )


def test_reopening_an_unchanged_index_preserves_the_bound_database_inode(
    tmp_path: Path,
) -> None:
    """A read-only initialization must not invalidate an existing inode binding."""
    repository = _copy_fixture(tmp_path)
    first = RepositoryIndex(project_id="project-1", repository_root=repository)
    first.rebuild()
    inode_before = first.index_path.stat().st_ino

    RepositoryIndex(project_id="project-1", repository_root=repository)

    assert first.index_path.stat().st_ino == inode_before
    assert first.status().project_id == "project-1"


def test_create_false_opens_only_an_existing_current_sidecar(tmp_path: Path) -> None:
    """Read-only consumers must never create or migrate repository indexes."""
    repository = _copy_fixture(tmp_path)
    missing_path = tmp_path / "missing.sqlite"

    with pytest.raises(RepositoryIndexError, match="lock|current repository index"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            index_path=missing_path,
            create=False,
        )
    assert not missing_path.exists()
    assert not (tmp_path / ".missing.sqlite.lock").exists()

    built = RepositoryIndex(project_id="project-1", repository_root=repository)
    built.rebuild()
    opened = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        create=False,
    )
    assert opened.status().freshness is Freshness.CURRENT

    _convert_current_sidecar_to_v4(built)
    with pytest.raises(RepositoryIndexError, match="creation and migration are disabled"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            create=False,
        )
    with sqlite3.connect(built.index_path) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 4
        checkpoint = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'index_generation_checkpoint'
            """
        ).fetchone()
    assert checkpoint is None


def test_create_false_instance_cannot_rebuild_or_publish(tmp_path: Path) -> None:
    """A query-only index handle must reject every write path."""
    repository = _copy_fixture(tmp_path)
    built = RepositoryIndex(project_id="project-1", repository_root=repository)
    built.rebuild()
    opened = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        create=False,
    )
    before = built.index_path.read_bytes()

    with pytest.raises(RepositoryIndexError, match="read-only"):
        opened.rebuild()

    assert built.index_path.read_bytes() == before


def test_max_files_truncation_never_reports_current_or_authoritative(
    tmp_path: Path,
) -> None:
    """A coverage-limited index must not authorize an empty navigation result."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "a.py").write_text("class Indexed: ...\n", encoding="utf-8")
    (repository / "b.py").write_text("class Omitted: ...\n", encoding="utf-8")
    index = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        limits=IndexLimits(max_files=1),
    )
    index.rebuild()

    status = index.status()
    result = index.symbols("Omitted")

    assert status.freshness is Freshness.STALE
    assert result.authoritative is False
    assert result.records == ()


@pytest.mark.parametrize(
    "payload",
    [
        b"class OmittedBecauseLarge:\n    pass\n" + (b"#" * 256),
        b"class OmittedBecauseBinary:\n\x00\xff\n",
        b"class OmittedBecauseEncoding:\n\xff\xfe\n",
    ],
)
def test_omitted_supported_source_marks_coverage_incomplete(
    tmp_path: Path,
    payload: bytes,
) -> None:
    """A skipped source file must never authorize a negative symbol answer."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "visible.py").write_text("class Visible: ...\n", encoding="utf-8")
    (repository / "omitted.py").write_bytes(payload)
    index = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        limits=IndexLimits(max_file_bytes=96),
    )

    index.rebuild()
    status = index.status()
    result = index.symbols("Omitted")

    assert status.coverage_complete is False
    assert status.freshness is Freshness.STALE
    assert result.authoritative is False
    assert result.records == ()


def test_max_files_bounds_candidate_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small file budget must not stat every entry in a large directory."""
    repository = tmp_path / "repository"
    repository.mkdir()
    for index in range(500):
        (repository / f"{index:04d}.py").write_text(
            f"class Symbol{index}: ...\n",
            encoding="utf-8",
        )
    inspected = 0
    original = repo_indexer._candidate_from_stat

    def counted_candidate(**kwargs: Any) -> Any:
        nonlocal inspected
        inspected += 1
        return original(**kwargs)

    monkeypatch.setattr(repo_indexer, "_candidate_from_stat", counted_candidate)
    index = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        limits=IndexLimits(max_files=1),
    )

    index.rebuild()

    # Rebuild fences the repository with two scans. Each scan may inspect only
    # a bounded handful of entries before declaring partial coverage.
    assert inspected <= 8
    assert index.status().freshness is Freshness.STALE


def test_authenticated_file_bindings_avoid_full_database_hash_per_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal fresh tool handles must not hash two complete sidecars per call."""
    repository = _copy_fixture(tmp_path)
    RepositoryIndex(project_id="project-1", repository_root=repository).rebuild()
    digested: list[str] = []
    original = repo_store.RepoIndexStore._digest_relative_file

    def counted_digest(
        self: repo_store.RepoIndexStore,
        name: str,
        **kwargs: Any,
    ) -> str:
        digested.append(name)
        return original(self, name, **kwargs)

    monkeypatch.setattr(
        repo_store.RepoIndexStore,
        "_digest_relative_file",
        counted_digest,
    )

    first = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        create=False,
    ).symbols("Widget")
    second = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        create=False,
    ).symbols("Widget")

    assert first.authoritative and second.authoritative
    assert digested == []


def test_database_snapshot_read_rejects_oversized_sidecar_before_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    monkeypatch.setattr(repo_store, "_MAX_DATABASE_BYTES", 1)

    with pytest.raises(RepositoryIndexError, match="bounded size"):
        index._store._read_database_snapshot(  # noqa: SLF001 - integrity regression
            None,
            allow_missing=False,
        )


def test_interrupted_publication_recovers_authenticated_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after canonical publish must not brick read or rebuild paths."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    changed = repository / "src" / "widget.py"
    changed.write_text(
        changed.read_text(encoding="utf-8").replace("def helper", "def normalize"),
        encoding="utf-8",
    )
    original = repo_store.RepoIndexStore._persist_lock_authority

    def interrupted(
        self: repo_store.RepoIndexStore,
        receipt: Any,
    ) -> None:
        del self, receipt
        raise OSError("injected authority persistence interruption")

    monkeypatch.setattr(
        repo_store.RepoIndexStore,
        "_persist_lock_authority",
        interrupted,
    )
    with pytest.raises(OSError, match="injected"):
        index.rebuild()
    monkeypatch.setattr(
        repo_store.RepoIndexStore,
        "_persist_lock_authority",
        original,
    )

    lock_path = index.index_path.parent / f".{index.index_path.name}.lock"
    before_read = lock_path.read_bytes()
    before_metadata = lock_path.stat()
    with pytest.raises(RepositoryIndexError, match="writable open"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            create=False,
        )
    after_metadata = lock_path.stat()
    assert lock_path.read_bytes() == before_read
    assert (
        after_metadata.st_size,
        after_metadata.st_mtime_ns,
    ) == (
        before_metadata.st_size,
        before_metadata.st_mtime_ns,
    )
    writable = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
    )
    recovered = writable.status()
    assert recovered.freshness is Freshness.CURRENT
    reopened = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        create=False,
    )
    assert reopened.status().aggregate_digest == recovered.aggregate_digest


def test_interrupted_initial_publication_requires_writable_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-generation crash is recoverable without a read-side mutation."""
    repository = _copy_fixture(tmp_path)
    original = repo_store.RepoIndexStore._persist_lock_authority

    def interrupted(
        self: repo_store.RepoIndexStore,
        receipt: Any,
    ) -> None:
        del self, receipt
        raise OSError("injected initial authority persistence interruption")

    monkeypatch.setattr(
        repo_store.RepoIndexStore,
        "_persist_lock_authority",
        interrupted,
    )
    with pytest.raises(OSError, match="initial authority"):
        RepositoryIndex(project_id="project-1", repository_root=repository)
    monkeypatch.setattr(
        repo_store.RepoIndexStore,
        "_persist_lock_authority",
        original,
    )

    index_path = repository / ".nest" / "repo-index" / "project-1.sqlite"
    lock_path = index_path.parent / f".{index_path.name}.lock"
    before_read = lock_path.read_bytes()
    before_metadata = lock_path.stat()
    with pytest.raises(RepositoryIndexError, match="authority is missing"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            create=False,
        )
    after_metadata = lock_path.stat()
    assert lock_path.read_bytes() == before_read
    assert (
        after_metadata.st_size,
        after_metadata.st_mtime_ns,
    ) == (
        before_metadata.st_size,
        before_metadata.st_mtime_ns,
    )

    recovered = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
    )
    rebuilt = recovered.rebuild()
    assert rebuilt.indexed_files == 10
    assert recovered.status().freshness is Freshness.CURRENT


def test_exact_max_files_keeps_complete_coverage_for_both_scanners(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "only.py"
    source.write_text("def only() -> None:\n    pass\n", encoding="utf-8")
    limits = IndexLimits(max_files=1)

    candidates, skipped, complete = repo_indexer._scan_candidates_from_path(
        repository,
        limits,
    )
    assert [candidate.relative_path for candidate in candidates] == ["only.py"]
    assert skipped == 0
    assert complete is True

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(repository, flags)
    try:
        candidates, skipped, complete = (
            repo_indexer._scan_candidates_from_descriptor(
                repository,
                limits,
                root_descriptor=descriptor,
            )
        )
    finally:
        os.close(descriptor)
    assert [candidate.relative_path for candidate in candidates] == ["only.py"]
    assert skipped == 0
    assert complete is True

    index = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        limits=limits,
    )
    assert index.rebuild().indexed_files == 1
    assert index.status().coverage_complete is True
    assert index.status().freshness is Freshness.CURRENT


def test_interrupted_lock_authority_append_preserves_last_committed_record(
    tmp_path: Path,
) -> None:
    """A torn append must not erase the previous durable high-water record."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    report = index.rebuild()
    lock_path = index.index_path.parent / f".{index.index_path.name}.lock"
    with lock_path.open("ab") as handle:
        handle.write(b'{"authority":{"generation_id":"torn')
        handle.flush()
        os.fsync(handle.fileno())

    reopened = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        create=False,
    )

    assert reopened.status().aggregate_digest == report.aggregate_digest
    assert reopened.status().freshness is Freshness.CURRENT


def test_query_fingerprint_fence_rejects_repository_mutation_during_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows loaded across a repository mutation must never be labeled current."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    original_symbols = index._store.symbols

    def mutate_then_load(
        query: str | None = None,
        *,
        limit: int,
        offset: int = 0,
        path_prefixes: tuple[str, ...] | None = None,
    ) -> Any:
        changed = repository / "src" / "widget.py"
        changed.write_text("class ChangedDuringQuery: ...\n", encoding="utf-8")
        return original_symbols(
            query,
            limit=limit,
            offset=offset,
            path_prefixes=path_prefixes,
        )

    monkeypatch.setattr(index._store, "symbols", mutate_then_load)

    with pytest.raises(RepositoryChangedDuringIndexingError, match="query"):
        index.symbols("Widget", include_stale_diagnostics=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-link contract")
def test_lock_hardlink_is_rejected_before_linked_victim_is_modified(tmp_path: Path) -> None:
    """Lock initialization must not chmod or write through an existing hard link."""
    repository = _copy_fixture(tmp_path)
    index_path = tmp_path / "index.sqlite"
    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    victim.chmod(0o600)
    lock_path = tmp_path / ".index.sqlite.lock"
    os.link(victim, lock_path)

    with pytest.raises(RepositoryIndexError, match="lock.*link"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            index_path=index_path,
        )

    assert victim.read_bytes() == b""
    assert victim.stat().st_mode & 0o777 == 0o600
    assert not index_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-link contract")
def test_content_receipt_hardlink_is_rejected(tmp_path: Path) -> None:
    """A linked receipt must not be trusted as immutable generation authority."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        generation_id = str(
            connection.execute(
                """
                SELECT generation_id
                FROM index_generation_checkpoint
                WHERE singleton = 1
                """
            ).fetchone()[0]
        )
    receipt = index.index_path.parent / (
        f".{index.index_path.name}.generation-{generation_id}.receipt"
    )
    victim = tmp_path / "receipt-victim"
    shutil.copy2(receipt, victim)
    receipt.unlink()
    os.link(victim, receipt)

    with pytest.raises(RepositoryIndexError, match="receipt.*link"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            create=False,
        )


def test_repository_index_uses_cross_platform_file_lock_abstraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creation and read paths must route through the Windows-capable lock API."""
    calls: list[str] = []

    def tracked_exclusive(handle: Any, *, blocking: bool = True) -> None:
        calls.append("exclusive")
        real_lock_exclusive(handle, blocking=blocking)

    monkeypatch.setattr(
        repo_store,
        "lock_exclusive",
        tracked_exclusive,
        raising=False,
    )
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        create=False,
    )

    assert "exclusive" in calls


def test_long_lived_instances_accept_lock_coordinated_generation_advances(
    tmp_path: Path,
) -> None:
    """A valid atomic generation advance must not strand another live store."""
    repository = _copy_fixture(tmp_path)
    first = RepositoryIndex(project_id="project-1", repository_root=repository)
    first.rebuild()
    second = RepositoryIndex(project_id="project-1", repository_root=repository)

    changed = repository / "src" / "widget.py"
    changed.write_text(
        changed.read_text(encoding="utf-8").replace("def helper", "def normalize"),
        encoding="utf-8",
    )
    first.rebuild()

    assert second.status().freshness is Freshness.CURRENT
    assert [record.name for record in second.symbols("normalize").records] == ["normalize"]

    changed.write_text(
        changed.read_text(encoding="utf-8").replace("def normalize", "def canonicalize"),
        encoding="utf-8",
    )
    second.rebuild()

    assert first.status().freshness is Freshness.CURRENT
    assert [record.name for record in first.symbols("canonicalize").records] == ["canonicalize"]


def test_wrapped_sqlite_connect_aba_is_rejected_before_returning_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening SQLite by pathname after verification must fail this ABA repro."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    real_connect = sqlite3.connect
    parked = tmp_path / "parked.sqlite"
    decoy = tmp_path / "decoy.sqlite"
    shutil.copy2(index.index_path, decoy)
    with sqlite3.connect(decoy) as connection:
        connection.execute(
            "UPDATE index_metadata SET aggregate_digest = 'decoy' WHERE singleton = 1"
        )

    def aba_connect(
        database: str | Path,
        timeout: float = 5.0,
        *,
        uri: bool = False,
        factory: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> sqlite3.Connection:
        assert uri is True
        assert "immutable=1" in str(database)
        os.replace(index.index_path, parked)
        os.replace(decoy, index.index_path)
        connection = real_connect(
            database,
            timeout=timeout,
            uri=uri,
            factory=factory,
        )
        os.replace(index.index_path, decoy)
        os.replace(parked, index.index_path)
        return connection

    monkeypatch.setattr(
        "nested_memvid_agent.repo_index.store.sqlite3.connect",
        aba_connect,
    )

    with pytest.raises(RepositoryIndexError, match="changed during read"):
        index.symbols()


def test_bounded_query_opens_a_pinned_immutable_generation_without_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limit-one read must not deserialize the full sidecar into memory."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    real_connect = sqlite3.connect
    opened: list[tuple[str, bool]] = []

    def tracking_connect(
        database: str | Path,
        timeout: float = 5.0,
        *,
        uri: bool = False,
        factory: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> sqlite3.Connection:
        opened.append((str(database), uri))
        return real_connect(database, timeout=timeout, uri=uri, factory=factory)

    monkeypatch.setattr(
        "nested_memvid_agent.repo_index.store.sqlite3.connect",
        tracking_connect,
    )

    result = index.symbols(limit=1)

    assert len(result.records) == 1
    assert opened
    assert all(database != ":memory:" for database, _uri in opened)
    assert all(uri and "immutable=1" in database for database, uri in opened)


def test_current_schema_construction_and_bounded_query_are_read_only_pinned_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reopening a current sidecar must not copy or quick-check the database."""
    repository = _copy_fixture(tmp_path)
    built = RepositoryIndex(project_id="project-1", repository_root=repository)
    built.rebuild()
    real_connect = sqlite3.connect
    opened: list[tuple[str, bool]] = []
    statements: list[str] = []

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            statements.append(sql)
            return super().execute(sql, parameters)

    def tracking_connect(
        database: str | Path,
        timeout: float = 5.0,
        *,
        uri: bool = False,
        factory: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> sqlite3.Connection:
        opened.append((str(database), uri))
        return real_connect(
            database,
            timeout=timeout,
            uri=uri,
            factory=TrackingConnection,
        )

    monkeypatch.setattr(
        "nested_memvid_agent.repo_index.store.sqlite3.connect",
        tracking_connect,
    )

    reopened = RepositoryIndex(project_id="project-1", repository_root=repository)
    result = reopened.symbols(limit=1)

    assert len(result.records) == 1
    assert opened
    assert all(database != ":memory:" for database, _uri in opened)
    assert all(uri and "immutable=1" in database for database, uri in opened)
    assert "PRAGMA quick_check" not in statements


def test_query_metadata_and_rows_do_not_cross_a_publication_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing between freshness capture and row load must fail authority closed."""
    repository = _copy_fixture(tmp_path)
    reader = RepositoryIndex(project_id="project-1", repository_root=repository)
    reader.rebuild()
    writer = RepositoryIndex(project_id="project-1", repository_root=repository)
    original_symbols = reader._store.symbols

    def publish_then_load(
        query: str | None = None,
        *,
        limit: int,
        offset: int = 0,
        path_prefixes: tuple[str, ...] | None = None,
    ) -> Any:
        changed = repository / "src" / "widget.py"
        changed.write_text(
            changed.read_text(encoding="utf-8").replace(
                "def helper",
                "def generation_safe",
            ),
            encoding="utf-8",
        )
        writer.rebuild()
        return original_symbols(
            query,
            limit=limit,
            offset=offset,
            path_prefixes=path_prefixes,
        )

    monkeypatch.setattr(reader._store, "symbols", publish_then_load)

    with pytest.raises(RepositoryChangedDuringIndexingError, match="query"):
        reader.symbols(
            "generation_safe",
            include_stale_diagnostics=True,
        )


def test_compacted_checkpoint_advances_a_reader_older_than_retained_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning recent rows must not strand a valid long-lived reader."""
    monkeypatch.setattr(
        "nested_memvid_agent.repo_index.store._GENERATION_HISTORY_LIMIT",
        4,
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "module.py"
    source.write_text("VALUE = 0\n", encoding="utf-8")
    reader = RepositoryIndex(project_id="project-1", repository_root=repository)
    reader.rebuild()
    writer = RepositoryIndex(project_id="project-1", repository_root=repository)

    for value in range(1, 9):
        source.write_text(f"VALUE = {value}\n", encoding="utf-8")
        writer.rebuild()

    assert reader.status().freshness is Freshness.CURRENT
    with sqlite3.connect(reader.index_path) as connection:
        retained = int(connection.execute("SELECT COUNT(*) FROM index_generations").fetchone()[0])
        checkpoint_sequence = int(
            connection.execute(
                "SELECT sequence FROM index_generation_checkpoint WHERE singleton = 1"
            ).fetchone()[0]
        )
        oldest_retained = int(
            connection.execute("SELECT MIN(sequence) FROM index_generations").fetchone()[0]
        )

    assert retained <= 4
    assert checkpoint_sequence - oldest_retained >= 3


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_custom_sidecar_parent_is_validated_without_chmod(tmp_path: Path) -> None:
    """Mutating a caller-owned custom parent mode must fail this test."""
    repository = _copy_fixture(tmp_path)
    custom_parent = tmp_path / "custom-index"
    custom_parent.mkdir(mode=0o755)
    custom_parent.chmod(0o755)

    index = RepositoryIndex(
        project_id="project-1",
        repository_root=repository,
        index_path=custom_parent / "index.sqlite",
    )

    assert custom_parent.stat().st_mode & 0o777 == 0o755
    index.rebuild()
    assert custom_parent.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_world_writable_custom_sidecar_parent_is_rejected_without_chmod(
    tmp_path: Path,
) -> None:
    """Silently repairing an unsafe caller-owned directory must fail this test."""
    repository = _copy_fixture(tmp_path)
    custom_parent = tmp_path / "unsafe-index"
    custom_parent.mkdir()
    custom_parent.chmod(0o777)

    with pytest.raises(RepositoryIndexError, match="unsafe"):
        RepositoryIndex(
            project_id="project-1",
            repository_root=repository,
            index_path=custom_parent / "index.sqlite",
        )

    assert custom_parent.stat().st_mode & 0o777 == 0o777


def test_structural_parsers_ignore_comments_and_literals_and_keep_offsets(
    tmp_path: Path,
) -> None:
    """Treating comment or string examples as code symbols must fail this test."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "sample.ts").write_text(
        "/* class CommentGhost {} */\n"
        "const example = 'function StringGhost() {}; import \"string-module\"';\n"
        '// import "comment-module";\n'
        'import { real } from "./real";\n'
        "export class RealWidget {\n"
        "  render(): string { return real(); }\n"
        "}\n",
        encoding="utf-8",
    )
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()

    assert [(record.name, record.line) for record in index.symbols().records] == [
        ("RealWidget", 5),
        ("render", 6),
    ]
    assert [(record.module, record.line) for record in index.imports().records] == [("./real", 4)]
    reference_names = {record.name for record in index.references().records}
    assert {
        "CommentGhost",
        "StringGhost",
        "comment",
        "module",
    }.isdisjoint(reference_names)


def test_python_nested_functions_are_not_classified_as_methods(tmp_path: Path) -> None:
    """Using any enclosing scope as evidence of a method must fail this test."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "nested.py").write_text(
        "def outer():\n"
        "    def inner():\n"
        "        return None\n"
        "    return inner()\n\n"
        "class Container:\n"
        "    def method(self):\n"
        "        def local():\n"
        "            return None\n"
        "        return local()\n",
        encoding="utf-8",
    )
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()

    assert [(record.qualified_name, record.kind) for record in index.symbols().records] == [
        ("outer", "function"),
        ("outer.inner", "function"),
        ("Container", "class"),
        ("Container.method", "method"),
        ("Container.method.local", "function"),
    ]


def test_query_limits_are_bounded_and_filter_before_limiting(tmp_path: Path) -> None:
    """Unbounded loads or applying a limit before a filter must fail this test."""
    repository = tmp_path / "repository"
    repository.mkdir()
    content = "".join(f"def noise_{number}(): ...\n" for number in range(120))
    content += "".join(f"def wanted_{number}(): ...\n" for number in range(12))
    (repository / "many.py").write_text(content, encoding="utf-8")
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()

    assert len(index.symbols().records) == DEFAULT_QUERY_LIMIT == 100
    assert [record.name for record in index.symbols("wanted", limit=5).records] == [
        "wanted_0",
        "wanted_1",
        "wanted_2",
        "wanted_3",
        "wanted_4",
    ]
    with pytest.raises(ValueError, match="limit"):
        index.symbols(limit=MAX_QUERY_LIMIT + 1)


def test_query_pages_report_truncation_and_continue_deterministically(
    tmp_path: Path,
) -> None:
    """Silently truncating a bounded result without continuation must fail this test."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "symbols.py").write_text(
        "".join(f"def item_{number:02d}(): ...\n" for number in range(8)),
        encoding="utf-8",
    )
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()

    first = index.symbols("item", limit=3)
    second = index.symbols("item", limit=3, offset=first.next_offset or 0)
    third = index.symbols("item", limit=3, offset=second.next_offset or 0)

    assert [record.name for record in first.records] == ["item_00", "item_01", "item_02"]
    assert first.truncated is True
    assert first.next_offset == 3
    assert [record.name for record in second.records] == ["item_03", "item_04", "item_05"]
    assert second.truncated is True
    assert second.next_offset == 6
    assert [record.name for record in third.records] == ["item_06", "item_07"]
    assert third.truncated is False
    assert third.next_offset is None


def test_reference_point_lookup_uses_nocase_index(tmp_path: Path) -> None:
    """Scanning the lexical-reference table for an exact lookup must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT id
            FROM lexical_references
            WHERE name = ? COLLATE NOCASE
            ORDER BY name COLLATE NOCASE, name, id
            LIMIT ? OFFSET ?
            """,
            ("helper", 11, 0),
        ).fetchall()

    assert any("references_name_nocase_idx" in str(row[3]) for row in plan)
    assert [record.name for record in index.references("HELPER").records] == [
        "helper",
        "helper",
        "helper",
        "helper",
    ]


@pytest.mark.parametrize("control_byte", [b"\x01", b"\x7f"])
def test_single_disallowed_control_byte_is_binary(
    tmp_path: Path,
    control_byte: bytes,
) -> None:
    """Accepting a low-density forbidden control byte as source must fail this test."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "mostly-text.txt").write_bytes(b"a" * 4_096 + control_byte)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)

    report = index.rebuild()

    assert index.files().records == ()
    assert index.status().freshness is Freshness.STALE
    assert index.status().coverage_complete is False
    assert report.skipped_files == 1


def test_rust_lifetimes_do_not_mask_following_symbols(tmp_path: Path) -> None:
    """Treating Rust lifetimes as quoted strings must fail this test."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "lib.rs").write_text(
        "pub fn borrow<'a>(value: &'a str) -> &'a str { value }\n"
        'pub fn after_lifetime() -> &\'static str { "ready" }\n',
        encoding="utf-8",
    )
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()

    assert [(record.name, record.line) for record in index.symbols().records] == [
        ("borrow", 1),
        ("after_lifetime", 2),
    ]


def test_parser_and_database_order_is_hash_seed_independent(tmp_path: Path) -> None:
    """Set iteration or incomplete tie keys must fail this subprocess test."""
    worktree = Path(__file__).parents[1]
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from nested_memvid_agent.repo_index import RepositoryIndex\n"
        "root = Path(sys.argv[1]); root.mkdir()\n"
        "(root / 'case.py').write_text('import Alpha, alpha\\n', encoding='utf-8')\n"
        "index = RepositoryIndex(project_id='case', repository_root=root)\n"
        "index.rebuild()\n"
        "print(json.dumps({\n"
        "  'imports': [(r.id, r.module) for r in index.imports().records],\n"
        "  'references': [(r.id, r.name) for r in index.references().records],\n"
        "}, sort_keys=True))\n"
    )
    outputs: list[str] = []
    for seed in ("1", "2", "3", "5", "8"):
        root = tmp_path / f"seed-{seed}"
        environment = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": str(worktree / "src"),
        }
        result = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            cwd=worktree,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(result.stdout.strip())

    assert len(set(outputs)) == 1
    assert json.loads(outputs[0]) == {
        "imports": [[1, "Alpha"], [2, "alpha"]],
        "references": [[1, "Alpha"], [2, "alpha"]],
    }


def test_connection_closes_when_serialize_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving a connection open after snapshot serialization failure must fail."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    class FailingSerializeConnection(sqlite3.Connection):
        def serialize(self, *args: Any, **kwargs: Any) -> bytes:
            raise sqlite3.OperationalError("injected serialize failure")

    def tracking_connect(
        database: str | Path,
        timeout: float = 5.0,
        *,
        uri: bool = False,
        factory: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> sqlite3.Connection:
        connection = real_connect(
            database,
            timeout=timeout,
            factory=FailingSerializeConnection,
            uri=uri,
        )
        opened.append(connection)
        return connection

    monkeypatch.setattr(
        "nested_memvid_agent.repo_index.store.sqlite3.connect",
        tracking_connect,
    )

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        index.rebuild()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[-1].execute("SELECT 1")


def test_connection_closes_when_pragma_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving a connection open after PRAGMA failure must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    class FailingPragmaConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "PRAGMA foreign_keys = ON":
                raise sqlite3.OperationalError("injected PRAGMA failure")
            return super().execute(sql, parameters)

    def failing_connect(
        database: str | Path,
        timeout: float = 5.0,
        *,
        uri: bool = False,
        factory: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> sqlite3.Connection:
        connection = real_connect(
            database,
            timeout=timeout,
            factory=FailingPragmaConnection,
            uri=uri,
        )
        opened.append(connection)
        return connection

    monkeypatch.setattr(
        "nested_memvid_agent.repo_index.store.sqlite3.connect",
        failing_connect,
    )

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        index.status()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[-1].execute("SELECT 1")


def test_routine_queries_do_not_run_full_database_integrity_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a full-database quick check to every bounded read must fail."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    real_connect = sqlite3.connect
    statements: list[str] = []

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            statements.append(sql)
            return super().execute(sql, parameters)

    def tracking_connect(
        database: str | Path,
        timeout: float = 5.0,
        *,
        uri: bool = False,
        factory: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> sqlite3.Connection:
        return real_connect(
            database,
            timeout=timeout,
            factory=TrackingConnection,
            uri=uri,
        )

    monkeypatch.setattr(
        "nested_memvid_agent.repo_index.store.sqlite3.connect",
        tracking_connect,
    )

    index.symbols("Widget")

    assert "PRAGMA quick_check" not in statements


def test_results_have_deterministic_tie_order(tmp_path: Path) -> None:
    """Depending on traversal or SQLite insertion order must fail this test."""
    repository = _copy_fixture(tmp_path)
    (repository / "z.py").write_text("def duplicate(): ...\n", encoding="utf-8")
    (repository / "a.py").write_text(
        "\n\ndef duplicate(): ...\n\ndef duplicate_again():\n    return duplicate()\n",
        encoding="utf-8",
    )
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()

    assert [
        (record.path.as_posix(), record.line, record.name)
        for record in index.symbols("duplicate").records
    ] == [
        ("a.py", 3, "duplicate"),
        ("a.py", 5, "duplicate_again"),
        ("z.py", 1, "duplicate"),
    ]
