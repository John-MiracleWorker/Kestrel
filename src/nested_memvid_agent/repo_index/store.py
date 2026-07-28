from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .models import (
    MAX_QUERY_LIMIT,
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

SCHEMA_VERSION = 2
_APPLICATION_ID = 0x4B535452


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
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _PathBinding:
    path: Path
    device: int
    inode: int


class RepoIndexStore:
    def __init__(
        self,
        path: Path,
        *,
        managed_directories: tuple[Path, ...] = (),
        custom_parent: bool = False,
    ) -> None:
        self.path = path
        self._managed_directories = managed_directories
        self._custom_parent = custom_parent
        self._parent_bindings: tuple[_PathBinding, ...] = ()
        self._database_binding: _PathBinding | None = None

    def initialize(
        self,
        *,
        project_id: str,
        root_identity: RootIdentity,
        parser_versions: dict[str, str],
    ) -> None:
        self._prepare_sidecar_parent()
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, SCHEMA_VERSION}:
                raise RepositoryIndexError(f"unsupported repository index schema version {version}")
            if version == 0:
                self._create_schema(connection)
            elif version == 1:
                self._migrate_schema_v1(connection)
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if application_id != _APPLICATION_ID:
                raise RepositoryIndexError("repository index database identity marker is invalid")
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
                SELECT id, path, digest, device, inode, size, mtime_ns, ctime_ns
                FROM files
                ORDER BY path
                """
            ).fetchall()
        return {
            str(row["path"]): StoredFileState(
                id=int(row["id"]),
                path=str(row["path"]),
                digest=str(row["digest"]),
                device=int(row["device"]),
                inode=int(row["inode"]),
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
                        path, digest, device, inode, size, mtime_ns, ctime_ns, language,
                        parser_version, is_test
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.candidate.relative_path,
                        item.digest,
                        item.candidate.device,
                        item.candidate.inode,
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

    def files(self, *, limit: int) -> tuple[FileRecord, ...]:
        bounded_limit = _validated_query_limit(limit)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, path, digest, size, language, parser_version, is_test
                FROM files
                ORDER BY path, id
                LIMIT ?
                """,
                (bounded_limit,),
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

    def symbols(self, query: str | None = None, *, limit: int) -> tuple[SymbolRecord, ...]:
        bounded_limit = _validated_query_limit(limit)
        predicate = ""
        parameters: list[object] = []
        if query is not None:
            predicate = (
                "WHERE instr(lower(s.name), lower(?)) > 0 "
                "OR instr(lower(s.qualified_name), lower(?)) > 0"
            )
            parameters.extend((query, query))
        parameters.append(bounded_limit)
        rows = self._select_records(
            f"""
            SELECT s.id, f.path, f.digest, s.name, s.qualified_name, s.kind,
                   s.line, s.column_number
            FROM symbols AS s
            JOIN files AS f ON f.id = s.file_id
            {predicate}
            ORDER BY f.path, s.line, s.column_number, lower(s.name), s.name,
                     s.kind, lower(s.qualified_name), s.qualified_name, s.id
            LIMIT ?
            """,
            tuple(parameters),
        )
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
        )

    def imports(self, query: str | None = None, *, limit: int) -> tuple[ImportRecord, ...]:
        bounded_limit = _validated_query_limit(limit)
        predicate = ""
        parameters: list[object] = []
        if query is not None:
            predicate = "WHERE instr(lower(i.module), lower(?)) > 0"
            parameters.append(query)
        parameters.append(bounded_limit)
        rows = self._select_records(
            f"""
            SELECT i.id, f.path, f.digest, i.module, i.imported_name,
                   i.line, i.column_number
            FROM imports AS i
            JOIN files AS f ON f.id = i.file_id
            {predicate}
            ORDER BY f.path, i.line, i.column_number, lower(i.module), i.module,
                     lower(coalesce(i.imported_name, '')),
                     coalesce(i.imported_name, ''), i.id
            LIMIT ?
            """,
            tuple(parameters),
        )
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
        )

    def references(self, name: str | None = None, *, limit: int) -> tuple[ReferenceRecord, ...]:
        bounded_limit = _validated_query_limit(limit)
        predicate = ""
        parameters: list[object] = []
        if name is not None:
            predicate = "WHERE lower(r.name) = lower(?)"
            parameters.append(name)
        parameters.append(bounded_limit)
        rows = self._select_records(
            f"""
            SELECT r.id, f.path, f.digest, r.name, r.line, r.column_number
            FROM lexical_references AS r
            JOIN files AS f ON f.id = r.file_id
            {predicate}
            ORDER BY f.path, r.line, r.column_number, lower(r.name), r.name, r.id
            LIMIT ?
            """,
            tuple(parameters),
        )
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
        )

    def tests_for(self, symbol_name: str, *, limit: int) -> tuple[TestRelationshipRecord, ...]:
        bounded_limit = _validated_query_limit(limit)
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
                ORDER BY tf.path, sf.path, s.line, tr.evidence_line,
                         lower(s.name), s.name, tr.id
                LIMIT ?
                """,
                (symbol_name, bounded_limit),
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

    def _select_records(self, statement: str, parameters: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(statement, parameters).fetchall()

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
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL,
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
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")

    def _migrate_schema_v1(self, connection: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(files)")}
        if "device" not in columns:
            connection.execute("ALTER TABLE files ADD COLUMN device INTEGER NOT NULL DEFAULT -1")
        if "inode" not in columns:
            connection.execute("ALTER TABLE files ADD COLUMN inode INTEGER NOT NULL DEFAULT -1")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")

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

    def _prepare_sidecar_parent(self) -> None:
        if self._custom_parent:
            if not self.path.parent.exists():
                raise RepositoryIndexError(
                    "custom repository index sidecar parent must already exist"
                )
            self._require_safe_directory(
                self.path.parent,
                label="custom repository index sidecar parent",
            )
        else:
            for directory in self._managed_directories:
                if os.path.lexists(directory):
                    self._require_safe_directory(
                        directory,
                        label="repository index sidecar parent",
                    )
                    continue
                try:
                    directory.mkdir(mode=0o700)
                except OSError as exc:
                    raise RepositoryIndexError(
                        "could not create repository index sidecar parent"
                    ) from exc
                if os.name == "posix":
                    os.chmod(directory, 0o700)
                self._require_safe_directory(
                    directory,
                    label="repository index sidecar parent",
                )
        try:
            resolved_parent = self.path.parent.resolve(strict=True)
        except OSError as exc:
            raise RepositoryIndexError("repository index sidecar parent is inaccessible") from exc
        if resolved_parent != self.path.parent:
            raise RepositoryIndexError(
                "repository index sidecar parent path contains a symbolic link"
            )
        self._parent_bindings = tuple(
            self._directory_binding(component)
            for component in _absolute_components(self.path.parent)
        )

    def _require_safe_directory(self, path: Path, *, label: str) -> None:
        binding = self._directory_binding(path)
        try:
            info = os.lstat(binding.path)
        except OSError as exc:
            raise RepositoryIndexError(f"{label} is inaccessible") from exc
        if os.name == "posix":
            if info.st_uid != os.getuid():
                raise RepositoryIndexError(f"{label} has an unsafe owner")
            if stat.S_IMODE(info.st_mode) & 0o022:
                raise RepositoryIndexError(f"{label} has unsafe write permissions")

    def _directory_binding(self, path: Path) -> _PathBinding:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise RepositoryIndexError(
                "repository index sidecar parent is missing or inaccessible"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise RepositoryIndexError(
                "repository index sidecar parent must not be a symbolic link"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise RepositoryIndexError("repository index sidecar parent must be a directory")
        return _PathBinding(
            path=path,
            device=int(info.st_dev),
            inode=int(info.st_ino),
        )

    def _verify_parent_bindings(self) -> None:
        for expected in self._parent_bindings:
            observed = self._directory_binding(expected.path)
            if observed.device != expected.device or observed.inode != expected.inode:
                raise RepositoryIndexError("repository index sidecar parent identity changed")

    def _database_path_binding(self) -> _PathBinding:
        try:
            info = os.lstat(self.path)
        except OSError as exc:
            raise RepositoryIndexError(
                "repository index database is missing or inaccessible"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise RepositoryIndexError("repository index database must not be a symbolic link")
        if not stat.S_ISREG(info.st_mode):
            raise RepositoryIndexError("repository index database must be a regular file")
        return _PathBinding(
            path=self.path,
            device=int(info.st_dev),
            inode=int(info.st_ino),
        )

    def _bind_or_verify_database(self) -> None:
        observed = self._database_path_binding()
        if self._database_binding is None:
            self._database_binding = observed
            return
        if (
            observed.device != self._database_binding.device
            or observed.inode != self._database_binding.inode
        ):
            raise RepositoryIndexError("repository index database identity changed")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._verify_parent_bindings()
        if self._database_binding is not None:
            self._bind_or_verify_database()
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            self._bind_or_verify_database()
            if os.name == "posix":
                os.chmod(self.path, 0o600)
                self._bind_or_verify_database()
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or str(quick_check[0]) != "ok":
                raise RepositoryIndexError("repository index database integrity check failed")
            with connection:
                yield connection
            self._verify_parent_bindings()
            self._bind_or_verify_database()
        finally:
            connection.close()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _validated_query_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    return limit


def _absolute_components(path: Path) -> tuple[Path, ...]:
    if not path.is_absolute():
        raise RepositoryIndexError("repository index sidecar path must be absolute")
    current = Path(path.anchor)
    components = [current]
    for part in path.parts[1:]:
        current /= part
        components.append(current)
    return tuple(components)
