from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from nested_memvid_agent.repo_index import (
    Freshness,
    IndexLimits,
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


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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
    assert status.schema_version == 1
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


def test_parser_version_change_marks_stale_and_forces_reparse(tmp_path: Path) -> None:
    """Treating rows from an obsolete parser as current must fail this test."""
    repository = _copy_fixture(tmp_path)
    index = RepositoryIndex(project_id="project-1", repository_root=repository)
    index.rebuild()
    with sqlite3.connect(index.index_path) as connection:
        connection.execute(
            "UPDATE index_metadata SET parser_versions_json = ? WHERE singleton = 1",
            ('{"python":"ast-obsolete"}',),
        )

    reopened = RepositoryIndex(project_id="project-1", repository_root=repository)
    assert reopened.status().freshness is Freshness.STALE
    rebuilt = reopened.rebuild()
    assert rebuilt.changed_files == 10
    assert rebuilt.reused_files == 0
    assert reopened.status().freshness is Freshness.CURRENT


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
    paths = {record.path.as_posix() for record in index.files().records}

    assert {
        ".nest/private/secret.py",
        "node_modules/package/vendor.ts",
        "dist/bundle.js",
        "binary.dat",
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
