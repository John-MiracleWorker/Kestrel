from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .models import (
    FileRecord,
    ImportRecord,
    IndexedCandidate,
    ReferenceRecord,
    RepositoryIndexError,
    RepositoryRootMismatchError,
    RootIdentity,
    SymbolRecord,
    TestRelationshipRecord,
)

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredMetadata:
    project_id: str
    root_path: str
    root_device: int
    root_inode: int
    aggregate_digest: str
    freshness_fingerprint: str
    indexed_at: str
    parser_versions: dict[str, str]
    git_head: str | None
    git_tree: str | None


@dataclass(frozen=True)
class StoredFileState:
    id: int
    path: str
    digest: str
    size: int
    mtime_ns: int
    ctime_ns: int


class RepoIndexStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(
        self,
        *,
        project_id: str,
        root_identity: RootIdentity,
        parser_versions: dict[str, str],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SCHEMA_VERSION}:
                raise RepositoryIndexError(f"unsupported repository index schema version {version}")
            if version == 0:
                self._create_schema(connection)
            existing = connection.execute(
                "SELECT project_id FROM index_metadata WHERE singleton = 1"
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO index_metadata (
                        singleton, project_id, root_path, root_device, root_inode,
                        aggregate_digest, freshness_fingerprint, indexed_at,
                        parser_versions_json, git_head, git_tree
                    ) VALUES (1, ?, ?, ?, ?, '', '', '', ?, NULL, NULL)
                    """,
                    (
                        project_id,
                        str(root_identity.path),
                        root_identity.device,
                        root_identity.inode,
                        json.dumps(parser_versions, sort_keys=True, separators=(",", ":")),
                    ),
                )
            elif str(existing["project_id"]) != project_id:
                raise RepositoryIndexError("repository index belongs to a different project")

    def metadata(self) -> StoredMetadata:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM index_metadata WHERE singleton = 1").fetchone()
        if row is None:
            raise RepositoryIndexError("repository index metadata is missing")
        raw_versions = json.loads(str(row["parser_versions_json"]))
        if not isinstance(raw_versions, dict):
            raise RepositoryIndexError("repository index parser metadata is invalid")
        return StoredMetadata(
            project_id=str(row["project_id"]),
            root_path=str(row["root_path"]),
            root_device=int(row["root_device"]),
            root_inode=int(row["root_inode"]),
            aggregate_digest=str(row["aggregate_digest"]),
            freshness_fingerprint=str(row["freshness_fingerprint"]),
            indexed_at=str(row["indexed_at"]),
            parser_versions={str(key): str(value) for key, value in raw_versions.items()},
            git_head=_optional_str(row["git_head"]),
            git_tree=_optional_str(row["git_tree"]),
        )

    def assert_root_identity(self, observed: RootIdentity) -> None:
        metadata = self.metadata()
        if metadata.root_path != str(observed.path):
            raise RepositoryRootMismatchError("repository root path does not match indexed root")
        if metadata.root_device != observed.device or metadata.root_inode != observed.inode:
            raise RepositoryRootMismatchError(
                "repository root identity does not match indexed root"
            )

    def stored_files(self) -> dict[str, StoredFileState]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, path, digest, size, mtime_ns, ctime_ns
                FROM files
                ORDER BY path
                """
            ).fetchall()
        return {
            str(row["path"]): StoredFileState(
                id=int(row["id"]),
                path=str(row["path"]),
                digest=str(row["digest"]),
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                ctime_ns=int(row["ctime_ns"]),
            )
            for row in rows
        }

    def apply_rebuild(
        self,
        *,
        observed_root: RootIdentity,
        changed: Sequence[IndexedCandidate],
        deleted_paths: Sequence[str],
        aggregate_digest: str,
        freshness_fingerprint: str,
        indexed_at: str,
        parser_versions: dict[str, str],
        git_head: str | None,
        git_tree: str | None,
    ) -> None:
        metadata = self.metadata()
        if (
            metadata.root_path != str(observed_root.path)
            or metadata.root_device != observed_root.device
            or metadata.root_inode != observed_root.inode
        ):
            raise RepositoryRootMismatchError(
                "repository root identity changed before index commit"
            )
        with self._connection() as connection:
            for path in sorted(deleted_paths):
                connection.execute("DELETE FROM files WHERE path = ?", (path,))
            for item in sorted(changed, key=lambda value: value.candidate.relative_path):
                connection.execute(
                    "DELETE FROM files WHERE path = ?",
                    (item.candidate.relative_path,),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO files (
                        path, digest, size, mtime_ns, ctime_ns, language,
                        parser_version, is_test
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.candidate.relative_path,
                        item.digest,
                        item.candidate.size,
                        item.candidate.mtime_ns,
                        item.candidate.ctime_ns,
                        item.language,
                        item.parser_version,
                        int(item.is_test),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RepositoryIndexError(
                        "repository index did not return an inserted file identifier"
                    )
                file_id = cursor.lastrowid
                connection.executemany(
                    """
                    INSERT INTO symbols (
                        file_id, name, qualified_name, kind, line, column_number
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            file_id,
                            symbol.name,
                            symbol.qualified_name,
                            symbol.kind,
                            symbol.line,
                            symbol.column,
                        )
                        for symbol in item.parsed.symbols
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO imports (
                        file_id, module, imported_name, line, column_number
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            file_id,
                            imported.module,
                            imported.imported_name,
                            imported.line,
                            imported.column,
                        )
                        for imported in item.parsed.imports
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO lexical_references (
                        file_id, name, line, column_number
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            file_id,
                            reference.name,
                            reference.line,
                            reference.column,
                        )
                        for reference in item.parsed.references
                    ],
                )
            if changed or deleted_paths:
                self._rebuild_test_relationships(connection)
            connection.execute(
                """
                UPDATE index_metadata
                SET aggregate_digest = ?,
                    freshness_fingerprint = ?,
                    indexed_at = ?,
                    parser_versions_json = ?,
                    git_head = ?,
                    git_tree = ?
                WHERE singleton = 1
                """,
                (
                    aggregate_digest,
                    freshness_fingerprint,
                    indexed_at,
                    json.dumps(parser_versions, sort_keys=True, separators=(",", ":")),
                    git_head,
                    git_tree,
                ),
            )

    def files(self) -> tuple[FileRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, path, digest, size, language, parser_version, is_test
                FROM files
                ORDER BY path
                """
            ).fetchall()
        return tuple(
            FileRecord(
                id=int(row["id"]),
                path=Path(str(row["path"])),
                digest=str(row["digest"]),
                size=int(row["size"]),
                language=str(row["language"]),
                parser_version=str(row["parser_version"]),
                is_test=bool(row["is_test"]),
            )
            for row in rows
        )

    def symbols(self, query: str | None = None) -> tuple[SymbolRecord, ...]:
        rows = self._select_records(
            """
            SELECT s.id, f.path, f.digest, s.name, s.qualified_name, s.kind,
                   s.line, s.column_number
            FROM symbols AS s
            JOIN files AS f ON f.id = s.file_id
            ORDER BY f.path, s.line, s.column_number, lower(s.name), s.kind
            """
        )
        folded = query.casefold() if query else None
        return tuple(
            SymbolRecord(
                id=int(row["id"]),
                path=Path(str(row["path"])),
                file_digest=str(row["digest"]),
                name=str(row["name"]),
                qualified_name=str(row["qualified_name"]),
                kind=str(row["kind"]),
                line=int(row["line"]),
                column=int(row["column_number"]),
            )
            for row in rows
            if folded is None
            or folded in str(row["name"]).casefold()
            or folded in str(row["qualified_name"]).casefold()
        )

    def imports(self, query: str | None = None) -> tuple[ImportRecord, ...]:
        rows = self._select_records(
            """
            SELECT i.id, f.path, f.digest, i.module, i.imported_name,
                   i.line, i.column_number
            FROM imports AS i
            JOIN files AS f ON f.id = i.file_id
            ORDER BY f.path, i.line, i.column_number, lower(i.module)
            """
        )
        folded = query.casefold() if query else None
        return tuple(
            ImportRecord(
                id=int(row["id"]),
                path=Path(str(row["path"])),
                file_digest=str(row["digest"]),
                module=str(row["module"]),
                imported_name=_optional_str(row["imported_name"]),
                line=int(row["line"]),
                column=int(row["column_number"]),
            )
            for row in rows
            if folded is None or folded in str(row["module"]).casefold()
        )

    def references(self, name: str | None = None) -> tuple[ReferenceRecord, ...]:
        rows = self._select_records(
            """
            SELECT r.id, f.path, f.digest, r.name, r.line, r.column_number
            FROM lexical_references AS r
            JOIN files AS f ON f.id = r.file_id
            ORDER BY f.path, r.line, r.column_number, lower(r.name)
            """
        )
        folded = name.casefold() if name else None
        return tuple(
            ReferenceRecord(
                id=int(row["id"]),
                path=Path(str(row["path"])),
                file_digest=str(row["digest"]),
                name=str(row["name"]),
                line=int(row["line"]),
                column=int(row["column_number"]),
            )
            for row in rows
            if folded is None or folded == str(row["name"]).casefold()
        )

    def tests_for(self, symbol_name: str) -> tuple[TestRelationshipRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT tr.id, s.name AS symbol_name, sf.path AS symbol_path,
                       tf.path AS test_path, tr.relationship, tr.evidence_line
                FROM test_relationships AS tr
                JOIN symbols AS s ON s.id = tr.symbol_id
                JOIN files AS sf ON sf.id = s.file_id
                JOIN files AS tf ON tf.id = tr.test_file_id
                WHERE lower(s.name) = lower(?)
                ORDER BY tf.path, sf.path, s.line, tr.evidence_line
                """,
                (symbol_name,),
            ).fetchall()
        return tuple(
            TestRelationshipRecord(
                id=int(row["id"]),
                symbol_name=str(row["symbol_name"]),
                symbol_path=Path(str(row["symbol_path"])),
                test_path=Path(str(row["test_path"])),
                relationship=str(row["relationship"]),
                evidence_line=int(row["evidence_line"]),
            )
            for row in rows
        )

    def _select_records(self, statement: str) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(statement).fetchall()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE index_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                project_id TEXT NOT NULL,
                root_path TEXT NOT NULL,
                root_device INTEGER NOT NULL,
                root_inode INTEGER NOT NULL,
                aggregate_digest TEXT NOT NULL,
                freshness_fingerprint TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                parser_versions_json TEXT NOT NULL,
                git_head TEXT,
                git_tree TEXT
            );

            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                digest TEXT NOT NULL,
                size INTEGER NOT NULL CHECK (size >= 0),
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                language TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                is_test INTEGER NOT NULL CHECK (is_test IN (0, 1))
            );

            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                line INTEGER NOT NULL CHECK (line > 0),
                column_number INTEGER NOT NULL CHECK (column_number > 0)
            );

            CREATE TABLE imports (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                module TEXT NOT NULL,
                imported_name TEXT,
                line INTEGER NOT NULL CHECK (line > 0),
                column_number INTEGER NOT NULL CHECK (column_number > 0)
            );

            CREATE TABLE lexical_references (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                line INTEGER NOT NULL CHECK (line > 0),
                column_number INTEGER NOT NULL CHECK (column_number > 0)
            );

            CREATE TABLE test_relationships (
                id INTEGER PRIMARY KEY,
                symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
                test_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                relationship TEXT NOT NULL,
                evidence_line INTEGER NOT NULL CHECK (evidence_line > 0),
                UNIQUE(symbol_id, test_file_id, relationship)
            );

            CREATE INDEX symbols_name_idx ON symbols(name);
            CREATE INDEX imports_module_idx ON imports(module);
            CREATE INDEX references_name_idx ON lexical_references(name);
            CREATE INDEX files_test_idx ON files(is_test);
            """
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _rebuild_test_relationships(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM test_relationships")
        connection.execute(
            """
            INSERT INTO test_relationships (
                symbol_id, test_file_id, relationship, evidence_line
            )
            SELECT s.id, tf.id, 'symbol_reference', MIN(r.line)
            FROM symbols AS s
            JOIN lexical_references AS r ON lower(r.name) = lower(s.name)
            JOIN files AS tf ON tf.id = r.file_id AND tf.is_test = 1
            WHERE s.file_id != tf.id
            GROUP BY s.id, tf.id
            ORDER BY tf.path, s.id
            """
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self.path.is_symlink():
            raise RepositoryIndexError("repository index sidecar must not be a symbolic link")
        connection = sqlite3.connect(self.path, timeout=5.0)
        if os.name == "posix":
            os.chmod(self.path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
