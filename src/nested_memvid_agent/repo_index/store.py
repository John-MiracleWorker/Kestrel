from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from .models import (
    MAX_QUERY_LIMIT,
    MAX_QUERY_OFFSET,
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

SCHEMA_VERSION = 5
_APPLICATION_ID = 0x4B535452
_GENERATION_HISTORY_LIMIT = 64
_LOCK_SECRET_PREFIX = b"KESTREL-REPO-INDEX-LOCK-V1\n"
StoreRecordT = TypeVar("StoreRecordT")


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


@dataclass(frozen=True)
class _FileBinding:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _DirectorySnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _GenerationState:
    generation_id: str
    sequence: int
    binding: _FileBinding
    lineage_id: str | None
    authorization_tag: str | None
    previous_generation_id: str | None


@dataclass(frozen=True)
class StoreQueryPage(Generic[StoreRecordT]):
    records: tuple[StoreRecordT, ...]
    truncated: bool
    next_offset: int | None


@dataclass(frozen=True)
class StoreQuerySnapshot(Generic[StoreRecordT]):
    metadata: StoredMetadata
    page: StoreQueryPage[StoreRecordT]


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
        self._generation: _GenerationState | None = None
        self._lock_binding: _FileBinding | None = None
        self._lock_secret: bytes | None = None
        self._thread_lock = threading.RLock()

    def initialize(
        self,
        *,
        project_id: str,
        root_identity: RootIdentity,
        parser_versions: dict[str, str],
        allow_migration: bool = True,
    ) -> None:
        self._prepare_sidecar_parent(allow_create=allow_migration)
        if self._initialize_current_read_only(
            project_id=project_id,
            allow_lock_creation=allow_migration,
        ):
            return
        if not allow_migration:
            raise RepositoryIndexError(
                "a current repository index is required; creation and migration are disabled"
            )
        with self._connection(write=True, integrity_check=True) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, 2, 3, 4, SCHEMA_VERSION}:
                raise RepositoryIndexError(f"unsupported repository index schema version {version}")
            if version == 0:
                self._create_schema(connection)
            elif version == 1:
                self._migrate_schema_v1(connection)
                self._migrate_schema_v2(connection)
                self._migrate_schema_v3(connection)
                self._migrate_schema_v4(connection)
            elif version == 2:
                self._migrate_schema_v2(connection)
                self._migrate_schema_v3(connection)
                self._migrate_schema_v4(connection)
            elif version == 3:
                self._migrate_schema_v3(connection)
                self._migrate_schema_v4(connection)
            elif version == 4:
                self._migrate_schema_v4(connection)
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

    def _initialize_current_read_only(
        self,
        *,
        project_id: str,
        allow_lock_creation: bool,
    ) -> bool:
        """Bind a current sidecar without copying or rewriting its database image."""
        with self._locked_parent(allow_create=allow_lock_creation) as parent_descriptor:
            parent_snapshot = self._directory_snapshot(parent_descriptor)
            binding = self._relative_file_binding(
                self.path.name,
                parent_descriptor=parent_descriptor,
                allow_missing=True,
            )
            if binding is None:
                return False
            descriptor, opened_binding = self._open_database_descriptor(parent_descriptor)
            connection: sqlite3.Connection | None = None
            try:
                if opened_binding != binding:
                    raise RepositoryIndexError(
                        "repository index database changed during initialization"
                    )
                connection = self._connect_pinned_read(descriptor)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA foreign_keys = ON")
                self._verify_snapshot_unchanged(
                    parent_descriptor,
                    parent_snapshot=parent_snapshot,
                    database_binding=binding,
                    operation="initialization",
                )
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version == SCHEMA_VERSION:
                    self._validate_and_bind_generation(
                        connection,
                        binding=binding,
                        parent_descriptor=parent_descriptor,
                    )
                    self._assert_project_id(connection, project_id=project_id)
                    self._verify_snapshot_unchanged(
                        parent_descriptor,
                        parent_snapshot=parent_snapshot,
                        database_binding=binding,
                        operation="initialization",
                    )
                    if _file_binding(os.fstat(descriptor)) != binding:
                        raise RepositoryIndexError(
                            "repository index database changed during initialization"
                        )
                    return True
                self._validate_legacy_admission(
                    connection,
                    version=version,
                    binding=binding,
                    parent_descriptor=parent_descriptor,
                )
                self._verify_snapshot_unchanged(
                    parent_descriptor,
                    parent_snapshot=parent_snapshot,
                    database_binding=binding,
                    operation="initialization",
                )
                if _file_binding(os.fstat(descriptor)) != binding:
                    raise RepositoryIndexError(
                        "repository index database changed during initialization"
                    )
                return False
            except sqlite3.DatabaseError as exc:
                raise RepositoryIndexError(
                    "repository index database could not be inspected"
                ) from exc
            finally:
                if connection is not None:
                    connection.close()
                os.close(descriptor)

    def _assert_project_id(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
    ) -> None:
        existing = connection.execute(
            "SELECT project_id FROM index_metadata WHERE singleton = 1"
        ).fetchone()
        if existing is None:
            raise RepositoryIndexError("repository index metadata is missing")
        if str(existing["project_id"]) != project_id:
            raise RepositoryIndexError("repository index belongs to a different project")

    def metadata(self) -> StoredMetadata:
        with self._connection() as connection:
            return self._metadata_from_connection(connection)

    def _metadata_from_connection(
        self,
        connection: sqlite3.Connection,
    ) -> StoredMetadata:
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
        with self._connection(write=True) as connection:
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

    def files(
        self,
        *,
        limit: int,
        offset: int = 0,
    ) -> StoreQuerySnapshot[FileRecord]:
        bounded_limit = _validated_query_limit(limit)
        bounded_offset = _validated_query_offset(offset)
        metadata, rows = self._select_records(
            """
            SELECT id, path, digest, size, language, parser_version, is_test
            FROM files
            ORDER BY path, id
            LIMIT ? OFFSET ?
            """,
            (bounded_limit + 1, bounded_offset),
        )
        records = tuple(
            FileRecord(
                id=int(row["id"]),
                path=Path(str(row["path"])),
                digest=str(row["digest"]),
                size=int(row["size"]),
                language=str(row["language"]),
                parser_version=str(row["parser_version"]),
                is_test=bool(row["is_test"]),
            )
            for row in rows[:bounded_limit]
        )
        return StoreQuerySnapshot(
            metadata=metadata,
            page=_query_page(
                records,
                row_count=len(rows),
                limit=bounded_limit,
                offset=bounded_offset,
            ),
        )

    def symbols(
        self,
        query: str | None = None,
        *,
        limit: int,
        offset: int = 0,
    ) -> StoreQuerySnapshot[SymbolRecord]:
        bounded_limit = _validated_query_limit(limit)
        bounded_offset = _validated_query_offset(offset)
        predicate = ""
        parameters: list[object] = []
        if query is not None:
            predicate = (
                "WHERE instr(lower(s.name), lower(?)) > 0 "
                "OR instr(lower(s.qualified_name), lower(?)) > 0"
            )
            parameters.extend((query, query))
        parameters.extend((bounded_limit + 1, bounded_offset))
        metadata, rows = self._select_records(
            f"""
            SELECT s.id, f.path, f.digest, s.name, s.qualified_name, s.kind,
                   s.line, s.column_number
            FROM symbols AS s
            JOIN files AS f ON f.id = s.file_id
            {predicate}
            ORDER BY f.path, s.line, s.column_number, lower(s.name), s.name,
                     s.kind, lower(s.qualified_name), s.qualified_name, s.id
            LIMIT ? OFFSET ?
            """,
            tuple(parameters),
        )
        records = tuple(
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
            for row in rows[:bounded_limit]
        )
        return StoreQuerySnapshot(
            metadata=metadata,
            page=_query_page(
                records,
                row_count=len(rows),
                limit=bounded_limit,
                offset=bounded_offset,
            ),
        )

    def imports(
        self,
        query: str | None = None,
        *,
        limit: int,
        offset: int = 0,
    ) -> StoreQuerySnapshot[ImportRecord]:
        bounded_limit = _validated_query_limit(limit)
        bounded_offset = _validated_query_offset(offset)
        predicate = ""
        parameters: list[object] = []
        if query is not None:
            predicate = "WHERE instr(lower(i.module), lower(?)) > 0"
            parameters.append(query)
        parameters.extend((bounded_limit + 1, bounded_offset))
        metadata, rows = self._select_records(
            f"""
            SELECT i.id, f.path, f.digest, i.module, i.imported_name,
                   i.line, i.column_number
            FROM imports AS i
            JOIN files AS f ON f.id = i.file_id
            {predicate}
            ORDER BY f.path, i.line, i.column_number, lower(i.module), i.module,
                     lower(coalesce(i.imported_name, '')),
                     coalesce(i.imported_name, ''), i.id
            LIMIT ? OFFSET ?
            """,
            tuple(parameters),
        )
        records = tuple(
            ImportRecord(
                id=int(row["id"]),
                path=Path(str(row["path"])),
                file_digest=str(row["digest"]),
                module=str(row["module"]),
                imported_name=_optional_str(row["imported_name"]),
                line=int(row["line"]),
                column=int(row["column_number"]),
            )
            for row in rows[:bounded_limit]
        )
        return StoreQuerySnapshot(
            metadata=metadata,
            page=_query_page(
                records,
                row_count=len(rows),
                limit=bounded_limit,
                offset=bounded_offset,
            ),
        )

    def references(
        self,
        name: str | None = None,
        *,
        limit: int,
        offset: int = 0,
    ) -> StoreQuerySnapshot[ReferenceRecord]:
        bounded_limit = _validated_query_limit(limit)
        bounded_offset = _validated_query_offset(offset)
        predicate = ""
        parameters: list[object] = []
        if name is not None:
            predicate = "WHERE r.name = ? COLLATE NOCASE"
            parameters.append(name)
        parameters.extend((bounded_limit + 1, bounded_offset))
        metadata, rows = self._select_records(
            f"""
            SELECT r.id, f.path, f.digest, r.name, r.line, r.column_number
            FROM lexical_references AS r
            JOIN files AS f ON f.id = r.file_id
            {predicate}
            ORDER BY f.path, r.line, r.column_number, lower(r.name), r.name, r.id
            LIMIT ? OFFSET ?
            """,
            tuple(parameters),
        )
        records = tuple(
            ReferenceRecord(
                id=int(row["id"]),
                path=Path(str(row["path"])),
                file_digest=str(row["digest"]),
                name=str(row["name"]),
                line=int(row["line"]),
                column=int(row["column_number"]),
            )
            for row in rows[:bounded_limit]
        )
        return StoreQuerySnapshot(
            metadata=metadata,
            page=_query_page(
                records,
                row_count=len(rows),
                limit=bounded_limit,
                offset=bounded_offset,
            ),
        )

    def tests_for(
        self,
        symbol_name: str,
        *,
        limit: int,
        offset: int = 0,
    ) -> StoreQuerySnapshot[TestRelationshipRecord]:
        bounded_limit = _validated_query_limit(limit)
        bounded_offset = _validated_query_offset(offset)
        metadata, rows = self._select_records(
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
            LIMIT ? OFFSET ?
            """,
            (symbol_name, bounded_limit + 1, bounded_offset),
        )
        records = tuple(
            TestRelationshipRecord(
                id=int(row["id"]),
                symbol_name=str(row["symbol_name"]),
                symbol_path=Path(str(row["symbol_path"])),
                test_path=Path(str(row["test_path"])),
                relationship=str(row["relationship"]),
                evidence_line=int(row["evidence_line"]),
            )
            for row in rows[:bounded_limit]
        )
        return StoreQuerySnapshot(
            metadata=metadata,
            page=_query_page(
                records,
                row_count=len(rows),
                limit=bounded_limit,
                offset=bounded_offset,
            ),
        )

    def _select_records(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> tuple[StoredMetadata, list[sqlite3.Row]]:
        with self._connection() as connection:
            metadata = self._metadata_from_connection(connection)
            rows = connection.execute(statement, parameters).fetchall()
        return metadata, rows

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

            CREATE TABLE index_generations (
                sequence INTEGER PRIMARY KEY CHECK (sequence > 0),
                generation_id TEXT NOT NULL UNIQUE,
                previous_generation_id TEXT,
                CHECK (
                    (sequence = 1 AND previous_generation_id IS NULL)
                    OR (sequence > 1 AND previous_generation_id IS NOT NULL)
                )
            );

            CREATE TABLE index_generation_checkpoint (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                lineage_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                generation_id TEXT NOT NULL,
                authorization_tag TEXT NOT NULL
            );

            CREATE INDEX symbols_name_idx ON symbols(name);
            CREATE INDEX imports_module_idx ON imports(module);
            CREATE INDEX references_name_nocase_idx
                ON lexical_references(name COLLATE NOCASE);
            CREATE INDEX files_test_idx ON files(is_test);
            """
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")

    def _migrate_schema_v4(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE index_generation_checkpoint (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                lineage_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                generation_id TEXT NOT NULL,
                authorization_tag TEXT NOT NULL
            )
            """
        )
        row = connection.execute(
            """
            SELECT sequence, generation_id, previous_generation_id
            FROM index_generations
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
        if row is not None:
            sequence = int(row["sequence"])
            generation_id = str(row["generation_id"])
            previous_generation_id = _optional_str(row["previous_generation_id"])
            self._validate_generation_row_identifiers(
                generation_id=generation_id,
                previous_generation_id=previous_generation_id,
            )
            lineage_id = secrets.token_hex(16)
            authorization_tag = self._generation_authorization_tag(
                connection,
                lineage_id=lineage_id,
                sequence=sequence,
                generation_id=generation_id,
                previous_generation_id=previous_generation_id,
            )
            connection.execute(
                """
                INSERT INTO index_generation_checkpoint (
                    singleton, lineage_id, sequence, generation_id,
                    authorization_tag
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (lineage_id, sequence, generation_id, authorization_tag),
            )
            self._compact_generation_history(connection, current_sequence=sequence)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")

    def _migrate_schema_v1(self, connection: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(files)")}
        if "device" not in columns:
            connection.execute("ALTER TABLE files ADD COLUMN device INTEGER NOT NULL DEFAULT -1")
        if "inode" not in columns:
            connection.execute("ALTER TABLE files ADD COLUMN inode INTEGER NOT NULL DEFAULT -1")
        connection.execute("PRAGMA user_version = 2")
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")

    def _migrate_schema_v2(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS references_name_nocase_idx
            ON lexical_references(name COLLATE NOCASE)
            """
        )
        connection.execute("PRAGMA user_version = 3")
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")

    def _migrate_schema_v3(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS index_generations (
                sequence INTEGER PRIMARY KEY CHECK (sequence > 0),
                generation_id TEXT NOT NULL UNIQUE,
                previous_generation_id TEXT,
                CHECK (
                    (sequence = 1 AND previous_generation_id IS NULL)
                    OR (sequence > 1 AND previous_generation_id IS NOT NULL)
                )
            )
            """
        )
        connection.execute("PRAGMA user_version = 4")
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

    def _prepare_sidecar_parent(self, *, allow_create: bool) -> None:
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
                if not allow_create:
                    raise RepositoryIndexError("repository index sidecar parent does not exist")
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

    @contextmanager
    def _locked_parent(self, *, allow_create: bool = True) -> Iterator[int | None]:
        """Pin the sidecar parent and serialize snapshot publication."""
        with self._thread_lock:
            self._verify_parent_bindings()
            parent_descriptor = self._open_parent_descriptor()
            lock_descriptor: int | None = None
            try:
                try:
                    lock_descriptor = self._open_relative(
                        f".{self.path.name}.lock",
                        (
                            os.O_RDWR | os.O_CREAT | _no_follow_flag()
                            if allow_create
                            else os.O_RDONLY | _no_follow_flag()
                        ),
                        mode=0o600,
                        parent_descriptor=parent_descriptor,
                    )
                except OSError as exc:
                    raise RepositoryIndexError(
                        "repository index lock is missing or inaccessible"
                    ) from exc
                lock_info = os.fstat(lock_descriptor)
                if not stat.S_ISREG(lock_info.st_mode):
                    raise RepositoryIndexError("repository index lock must be a regular file")
                if os.name == "posix" and allow_create:
                    os.fchmod(lock_descriptor, 0o600)
                    lock_info = os.fstat(lock_descriptor)
                observed_lock = _file_binding(lock_info)
                if self._lock_binding is None:
                    self._lock_binding = observed_lock
                elif (
                    observed_lock.device != self._lock_binding.device
                    or observed_lock.inode != self._lock_binding.inode
                ):
                    raise RepositoryIndexError("repository index lock identity changed")
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(
                        lock_descriptor,
                        fcntl.LOCK_EX if allow_create else fcntl.LOCK_SH,
                    )
                observed_secret = self._load_or_create_lock_secret(
                    lock_descriptor,
                    allow_create=allow_create,
                )
                if self._lock_secret is None:
                    self._lock_secret = observed_secret
                elif not hmac.compare_digest(self._lock_secret, observed_secret):
                    raise RepositoryIndexError("repository index lock authorization changed")
                self._verify_parent_descriptor(parent_descriptor)
                yield parent_descriptor
            finally:
                if lock_descriptor is not None:
                    if os.name == "posix":
                        import fcntl

                        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                    os.close(lock_descriptor)
                if parent_descriptor is not None:
                    os.close(parent_descriptor)

    def _load_or_create_lock_secret(
        self,
        descriptor: int,
        *,
        allow_create: bool,
    ) -> bytes:
        info = os.fstat(descriptor)
        if info.st_size == 0:
            if not allow_create:
                raise RepositoryIndexError("repository index lock authorization is missing")
            secret = secrets.token_bytes(32)
            payload = _LOCK_SECRET_PREFIX + secret.hex().encode("ascii") + b"\n"
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            return secret
        if info.st_size > 256:
            raise RepositoryIndexError("repository index lock authorization is invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = os.read(descriptor, int(info.st_size))
        expected_size = len(_LOCK_SECRET_PREFIX) + 65
        if len(payload) != expected_size or not payload.startswith(_LOCK_SECRET_PREFIX):
            raise RepositoryIndexError("repository index lock authorization is invalid")
        encoded = payload[len(_LOCK_SECRET_PREFIX) : -1]
        try:
            secret = bytes.fromhex(encoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RepositoryIndexError("repository index lock authorization is invalid") from exc
        if len(secret) != 32:
            raise RepositoryIndexError("repository index lock authorization is invalid")
        return secret

    def _open_parent_descriptor(self) -> int | None:
        if not hasattr(os, "O_DIRECTORY"):
            return None
        flags = os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag()
        try:
            descriptor = os.open(self.path.parent, flags)
        except OSError as exc:
            raise RepositoryIndexError(
                "repository index sidecar parent could not be pinned"
            ) from exc
        try:
            self._verify_parent_descriptor(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _verify_parent_descriptor(self, descriptor: int | None) -> None:
        self._verify_parent_bindings()
        if descriptor is None:
            return
        info = os.fstat(descriptor)
        expected = self._parent_bindings[-1]
        if (
            not stat.S_ISDIR(info.st_mode)
            or int(info.st_dev) != expected.device
            or int(info.st_ino) != expected.inode
        ):
            raise RepositoryIndexError("repository index sidecar parent identity changed")

    def _directory_snapshot(self, parent_descriptor: int | None) -> _DirectorySnapshot:
        info = (
            os.fstat(parent_descriptor)
            if parent_descriptor is not None
            else os.lstat(self.path.parent)
        )
        return _DirectorySnapshot(
            device=int(info.st_dev),
            inode=int(info.st_ino),
            size=int(info.st_size),
            mtime_ns=int(info.st_mtime_ns),
            ctime_ns=int(info.st_ctime_ns),
        )

    def _read_database_snapshot(
        self,
        parent_descriptor: int | None,
        *,
        allow_missing: bool,
    ) -> tuple[bytes, _FileBinding | None]:
        try:
            descriptor = self._open_relative(
                self.path.name,
                os.O_RDONLY | _no_follow_flag(),
                parent_descriptor=parent_descriptor,
            )
        except FileNotFoundError:
            if allow_missing:
                return b"", None
            raise RepositoryIndexError(
                "repository index database is missing or inaccessible"
            ) from None
        except OSError as exc:
            raise RepositoryIndexError(
                "repository index database is missing or inaccessible"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise RepositoryIndexError("repository index database must be a regular file")
            observed = _file_binding(before)
            chunks: list[bytes] = []
            remaining = observed.size
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    raise RepositoryIndexError("repository index database changed while being read")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if _file_binding(after) != observed:
                raise RepositoryIndexError("repository index database changed while being read")
            if observed.size == 0:
                raise RepositoryIndexError("repository index database is empty")
            return b"".join(chunks), observed
        finally:
            os.close(descriptor)

    def _verify_snapshot_unchanged(
        self,
        parent_descriptor: int | None,
        *,
        parent_snapshot: _DirectorySnapshot,
        database_binding: _FileBinding | None,
        operation: str,
    ) -> None:
        self._verify_parent_descriptor(parent_descriptor)
        if self._directory_snapshot(parent_descriptor) != parent_snapshot:
            raise RepositoryIndexError(f"repository index sidecar changed during {operation}")
        observed = self._relative_file_binding(
            self.path.name,
            parent_descriptor=parent_descriptor,
            allow_missing=database_binding is None,
        )
        if observed != database_binding:
            raise RepositoryIndexError(f"repository index database changed during {operation}")

    def _open_database_descriptor(
        self,
        parent_descriptor: int | None,
    ) -> tuple[int, _FileBinding]:
        try:
            descriptor = self._open_relative(
                self.path.name,
                os.O_RDONLY | _no_follow_flag(),
                parent_descriptor=parent_descriptor,
            )
        except OSError as exc:
            raise RepositoryIndexError(
                "repository index database is missing or inaccessible"
            ) from exc
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise RepositoryIndexError("repository index database must be a regular file")
        return descriptor, _file_binding(info)

    def _connect_pinned_read(self, descriptor: int) -> sqlite3.Connection:
        if os.name == "posix" and Path("/dev/fd").is_dir():
            database_uri = f"file:/dev/fd/{descriptor}?mode=ro&immutable=1"
        else:
            database_uri = f"{self.path.as_uri()}?mode=ro&immutable=1"
        return sqlite3.connect(database_uri, timeout=5.0, uri=True)

    def _read_generation_state(
        self,
        connection: sqlite3.Connection,
        *,
        binding: _FileBinding,
        allow_empty: bool = False,
    ) -> _GenerationState | None:
        try:
            checkpoint = connection.execute(
                """
                SELECT lineage_id, sequence, generation_id, authorization_tag
                FROM index_generation_checkpoint
                WHERE singleton = 1
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise RepositoryIndexError("repository index generation checkpoint is missing") from exc
        if checkpoint is None:
            if allow_empty:
                return None
            raise RepositoryIndexError("repository index generation checkpoint is empty")
        lineage_id = str(checkpoint["lineage_id"])
        sequence = int(checkpoint["sequence"])
        checkpoint_generation_id = str(checkpoint["generation_id"])
        authorization_tag = str(checkpoint["authorization_tag"])
        if not _valid_generation_id(lineage_id):
            raise RepositoryIndexError("repository index generation lineage identifier is invalid")
        if not _valid_generation_id(checkpoint_generation_id):
            raise RepositoryIndexError("repository index generation identifier is invalid")
        if not _valid_authorization_tag(authorization_tag):
            raise RepositoryIndexError(
                "repository index generation checkpoint authorization is invalid"
            )
        row = connection.execute(
            """
            SELECT generation_id, previous_generation_id
            FROM index_generations
            WHERE sequence = ?
            """,
            (sequence,),
        ).fetchone()
        if row is None:
            raise RepositoryIndexError(
                "repository index generation ledger does not contain its checkpoint"
            )
        generation_id = str(row["generation_id"])
        previous_generation_id = _optional_str(row["previous_generation_id"])
        self._validate_generation_row_identifiers(
            generation_id=generation_id,
            previous_generation_id=previous_generation_id,
        )
        if generation_id != checkpoint_generation_id:
            raise RepositoryIndexError(
                "repository index generation checkpoint does not match its ledger"
            )
        expected_tag = self._generation_authorization_tag(
            connection,
            lineage_id=lineage_id,
            sequence=sequence,
            generation_id=generation_id,
            previous_generation_id=previous_generation_id,
        )
        if not hmac.compare_digest(authorization_tag, expected_tag):
            raise RepositoryIndexError(
                "repository index generation checkpoint authorization is invalid"
            )
        observed = _GenerationState(
            generation_id=generation_id,
            sequence=sequence,
            binding=binding,
            lineage_id=lineage_id,
            authorization_tag=authorization_tag,
            previous_generation_id=previous_generation_id,
        )
        self._validate_recent_generation_history(
            connection,
            current=observed,
            enforce_limit=True,
        )
        return observed

    def _read_legacy_generation_state(
        self,
        connection: sqlite3.Connection,
        *,
        binding: _FileBinding,
    ) -> _GenerationState:
        try:
            row = connection.execute(
                """
                SELECT sequence, generation_id, previous_generation_id
                FROM index_generations
                ORDER BY sequence DESC
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise RepositoryIndexError("repository index generation ledger is missing") from exc
        if row is None:
            raise RepositoryIndexError("repository index generation ledger is empty")
        generation_id = str(row["generation_id"])
        previous_generation_id = _optional_str(row["previous_generation_id"])
        self._validate_generation_row_identifiers(
            generation_id=generation_id,
            previous_generation_id=previous_generation_id,
        )
        observed = _GenerationState(
            generation_id=generation_id,
            sequence=int(row["sequence"]),
            binding=binding,
            lineage_id=None,
            authorization_tag=None,
            previous_generation_id=previous_generation_id,
        )
        self._validate_recent_generation_history(
            connection,
            current=observed,
            enforce_limit=False,
        )
        return observed

    def _validate_and_bind_generation(
        self,
        connection: sqlite3.Connection,
        *,
        binding: _FileBinding,
        parent_descriptor: int | None,
    ) -> _GenerationState:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id != _APPLICATION_ID or version != SCHEMA_VERSION:
            raise RepositoryIndexError("repository index database identity marker is invalid")
        observed = self._read_generation_state(
            connection,
            binding=binding,
        )
        if observed is None:
            raise RepositoryIndexError("repository index generation checkpoint is empty")
        self._validate_generation_manifest(
            observed,
            parent_descriptor=parent_descriptor,
            allow_missing=False,
        )
        self._bind_observed_generation(connection, observed=observed)
        return observed

    def _validate_write_source(
        self,
        connection: sqlite3.Connection,
        *,
        binding: _FileBinding,
        parent_descriptor: int | None,
    ) -> _GenerationState | None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == SCHEMA_VERSION:
            return self._validate_and_bind_generation(
                connection,
                binding=binding,
                parent_descriptor=parent_descriptor,
            )
        return self._validate_legacy_admission(
            connection,
            version=version,
            binding=binding,
            parent_descriptor=parent_descriptor,
        )

    def _validate_legacy_admission(
        self,
        connection: sqlite3.Connection,
        *,
        version: int,
        binding: _FileBinding,
        parent_descriptor: int | None,
    ) -> _GenerationState | None:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if version == 4:
            if application_id != _APPLICATION_ID:
                raise RepositoryIndexError(
                    "legacy repository index database identity marker is invalid"
                )
            if self._table_exists(connection, "index_generation_checkpoint"):
                raise RepositoryIndexError(
                    "legacy repository index contains current generation artifacts"
                )
            observed = self._read_legacy_generation_state(
                connection,
                binding=binding,
            )
            self._validate_generation_manifest(
                observed,
                parent_descriptor=parent_descriptor,
                allow_missing=False,
            )
            self._bind_observed_generation(connection, observed=observed)
            return observed
        if version not in {0, 1, 2, 3}:
            raise RepositoryIndexError(f"unsupported repository index schema version {version}")
        if application_id not in {0, _APPLICATION_ID}:
            raise RepositoryIndexError(
                "legacy repository index database identity marker is invalid"
            )
        if (
            self._table_exists(connection, "index_generations")
            or self._table_exists(connection, "index_generation_checkpoint")
            or self._generation_manifest_names(parent_descriptor)
        ):
            raise RepositoryIndexError(
                "legacy repository index contains durable generation artifacts"
            )
        if self._generation is not None:
            raise RepositoryIndexError("repository index database identity changed")
        return None

    def _bind_observed_generation(
        self,
        connection: sqlite3.Connection,
        *,
        observed: _GenerationState,
    ) -> None:
        expected = self._generation
        if expected is None:
            self._generation = observed
            return
        if observed.binding == expected.binding:
            if (
                observed.generation_id != expected.generation_id
                or observed.sequence != expected.sequence
                or observed.lineage_id != expected.lineage_id
            ):
                raise RepositoryIndexError("repository index generation changed in place")
            return
        if (
            observed.binding.device == expected.binding.device
            and observed.binding.inode == expected.binding.inode
        ):
            raise RepositoryIndexError("repository index generation changed in place")
        self._validate_generation_lineage(
            connection,
            expected=expected,
            observed=observed,
        )
        self._generation = observed

    def _validate_generation_manifest(
        self,
        generation: _GenerationState,
        *,
        parent_descriptor: int | None,
        allow_missing: bool,
    ) -> None:
        manifest_binding = self._relative_file_binding(
            self._generation_filename(generation.generation_id),
            parent_descriptor=parent_descriptor,
            allow_missing=allow_missing,
        )
        if manifest_binding is None and allow_missing:
            return
        if manifest_binding != generation.binding:
            raise RepositoryIndexError(
                "repository index database identity changed: generation manifest mismatch"
            )

    def _generation_filename(self, generation_id: str) -> str:
        if not _valid_generation_id(generation_id):
            raise RepositoryIndexError("repository index generation identifier is invalid")
        return f".{self.path.name}.generation-{generation_id}"

    def _validate_generation_lineage(
        self,
        connection: sqlite3.Connection,
        *,
        expected: _GenerationState,
        observed: _GenerationState,
    ) -> None:
        if (
            observed.sequence <= expected.sequence
            or expected.lineage_id is None
            or observed.lineage_id is None
            or observed.lineage_id != expected.lineage_id
        ):
            raise RepositoryIndexError("repository index database identity changed")
        rows = self._recent_generation_rows(connection, enforce_limit=True)
        if not rows:
            raise RepositoryIndexError("repository index generation ledger is empty")
        oldest_sequence = int(rows[0]["sequence"])
        if expected.sequence < oldest_sequence:
            return
        expected_row = next(
            (row for row in rows if int(row["sequence"]) == expected.sequence),
            None,
        )
        if expected_row is None or str(expected_row["generation_id"]) != expected.generation_id:
            raise RepositoryIndexError("repository index database identity changed")

    def _validate_recent_generation_history(
        self,
        connection: sqlite3.Connection,
        *,
        current: _GenerationState,
        enforce_limit: bool,
    ) -> None:
        rows = self._recent_generation_rows(
            connection,
            enforce_limit=enforce_limit,
        )
        if not rows:
            raise RepositoryIndexError("repository index generation ledger is empty")
        previous_id: str | None = None
        previous_sequence: int | None = None
        for row in rows:
            sequence = int(row["sequence"])
            generation_id = str(row["generation_id"])
            previous_generation_id = _optional_str(row["previous_generation_id"])
            self._validate_generation_row_identifiers(
                generation_id=generation_id,
                previous_generation_id=previous_generation_id,
            )
            if sequence == 1:
                if previous_generation_id is not None:
                    raise RepositoryIndexError("repository index generation lineage is invalid")
            elif previous_generation_id is None:
                raise RepositoryIndexError("repository index generation lineage is invalid")
            if previous_sequence is not None:
                if sequence != previous_sequence + 1:
                    raise RepositoryIndexError(
                        "repository index generation lineage is not contiguous"
                    )
                if previous_generation_id != previous_id:
                    raise RepositoryIndexError("repository index generation lineage is invalid")
            previous_sequence = sequence
            previous_id = generation_id
        if previous_sequence != current.sequence or previous_id != current.generation_id:
            raise RepositoryIndexError("repository index generation lineage does not reach current")

    def _recent_generation_rows(
        self,
        connection: sqlite3.Connection,
        *,
        enforce_limit: bool,
    ) -> list[sqlite3.Row]:
        limit = _generation_history_limit()
        rows = connection.execute(
            """
            SELECT sequence, generation_id, previous_generation_id
            FROM index_generations
            ORDER BY sequence DESC
            LIMIT ?
            """,
            (limit + 1,),
        ).fetchall()
        if len(rows) > limit:
            if enforce_limit:
                raise RepositoryIndexError(
                    "repository index generation ledger exceeds its bounded history"
                )
            rows = rows[:limit]
        rows.reverse()
        return rows

    def _validate_generation_row_identifiers(
        self,
        *,
        generation_id: str,
        previous_generation_id: str | None,
    ) -> None:
        if not _valid_generation_id(generation_id):
            raise RepositoryIndexError("repository index generation identifier is invalid")
        if previous_generation_id is not None and not _valid_generation_id(previous_generation_id):
            raise RepositoryIndexError("repository index generation lineage identifier is invalid")

    def _generation_authorization_tag(
        self,
        connection: sqlite3.Connection,
        *,
        lineage_id: str,
        sequence: int,
        generation_id: str,
        previous_generation_id: str | None,
    ) -> str:
        secret = self._lock_secret
        if secret is None:
            raise RepositoryIndexError("repository index lock authorization is unavailable")
        row = connection.execute(
            """
            SELECT project_id, root_path, root_device, root_inode
            FROM index_metadata
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise RepositoryIndexError("repository index metadata is missing")
        payload = "\0".join(
            (
                "kestrel-repo-index-generation-v1",
                str(row["project_id"]),
                str(row["root_path"]),
                str(int(row["root_device"])),
                str(int(row["root_inode"])),
                lineage_id,
                str(sequence),
                generation_id,
                previous_generation_id or "",
            )
        ).encode("utf-8")
        return hmac.new(secret, payload, hashlib.sha256).hexdigest()

    def _table_exists(self, connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = ?
                LIMIT 1
                """,
                (table,),
            ).fetchone()
            is not None
        )

    def _generation_manifest_names(
        self,
        parent_descriptor: int | None,
    ) -> tuple[str, ...]:
        prefix = f".{self.path.name}.generation-"
        try:
            names = (
                os.listdir(parent_descriptor)
                if parent_descriptor is not None
                else os.listdir(self.path.parent)
            )
        except OSError as exc:
            raise RepositoryIndexError(
                "repository index generation artifacts could not be inspected"
            ) from exc
        return tuple(sorted(name for name in names if name.startswith(prefix)))

    def _append_generation(
        self,
        connection: sqlite3.Connection,
        previous: _GenerationState | None,
    ) -> tuple[str, int, str, str, str | None]:
        generation_id = secrets.token_hex(16)
        sequence = 1 if previous is None else previous.sequence + 1
        lineage_id = secrets.token_hex(16) if previous is None else previous.lineage_id
        if lineage_id is None:
            raise RepositoryIndexError("repository index generation lineage is unavailable")
        previous_generation_id = None if previous is None else previous.generation_id
        connection.execute(
            """
            INSERT INTO index_generations (
                sequence, generation_id, previous_generation_id
            ) VALUES (?, ?, ?)
            """,
            (
                sequence,
                generation_id,
                previous_generation_id,
            ),
        )
        authorization_tag = self._generation_authorization_tag(
            connection,
            lineage_id=lineage_id,
            sequence=sequence,
            generation_id=generation_id,
            previous_generation_id=previous_generation_id,
        )
        connection.execute(
            """
            INSERT INTO index_generation_checkpoint (
                singleton, lineage_id, sequence, generation_id,
                authorization_tag
            ) VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                lineage_id = excluded.lineage_id,
                sequence = excluded.sequence,
                generation_id = excluded.generation_id,
                authorization_tag = excluded.authorization_tag
            """,
            (lineage_id, sequence, generation_id, authorization_tag),
        )
        self._compact_generation_history(
            connection,
            current_sequence=sequence,
        )
        return (
            generation_id,
            sequence,
            lineage_id,
            authorization_tag,
            previous_generation_id,
        )

    def _compact_generation_history(
        self,
        connection: sqlite3.Connection,
        *,
        current_sequence: int,
    ) -> None:
        first_retained = max(1, current_sequence - _generation_history_limit() + 1)
        connection.execute(
            "DELETE FROM index_generations WHERE sequence < ?",
            (first_retained,),
        )

    def _publish_database(
        self,
        parent_descriptor: int | None,
        *,
        payload: bytes,
        expected_binding: _FileBinding | None,
        generation_id: str,
        previous_generation_id: str | None,
    ) -> _FileBinding:
        temporary_name = f".{self.path.name}.{secrets.token_hex(12)}.tmp"
        generation_name = self._generation_filename(generation_id)
        link_name = f".{self.path.name}.{secrets.token_hex(12)}.link"
        temporary_descriptor: int | None = None
        generation_staged = False
        canonical_published = False
        try:
            temporary_descriptor = self._open_relative(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                mode=0o600,
                parent_descriptor=parent_descriptor,
            )
            if os.name == "posix":
                os.fchmod(temporary_descriptor, 0o600)
            temporary_binding = _file_binding(os.fstat(temporary_descriptor))
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(temporary_descriptor, view[written:])
                if count == 0:
                    raise RepositoryIndexError("repository index snapshot write made no progress")
                written += count
            os.fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = None

            staged_binding = self._relative_file_binding(
                temporary_name,
                parent_descriptor=parent_descriptor,
                allow_missing=False,
            )
            if (
                staged_binding is None
                or staged_binding.device != temporary_binding.device
                or staged_binding.inode != temporary_binding.inode
            ):
                raise RepositoryIndexError("repository index snapshot changed before publication")
            current = self._relative_file_binding(
                self.path.name,
                parent_descriptor=parent_descriptor,
                allow_missing=expected_binding is None,
            )
            if current != expected_binding:
                raise RepositoryIndexError("repository index database changed during write")
            if (
                self._relative_file_binding(
                    generation_name,
                    parent_descriptor=parent_descriptor,
                    allow_missing=True,
                )
                is not None
            ):
                raise RepositoryIndexError("repository index generation manifest already exists")
            self._replace_relative(
                temporary_name,
                generation_name,
                parent_descriptor=parent_descriptor,
            )
            generation_staged = True
            self._link_relative(
                generation_name,
                link_name,
                parent_descriptor=parent_descriptor,
            )
            current = self._relative_file_binding(
                self.path.name,
                parent_descriptor=parent_descriptor,
                allow_missing=expected_binding is None,
            )
            if current != expected_binding:
                raise RepositoryIndexError("repository index database changed during write")
            self._replace_relative(
                link_name,
                self.path.name,
                parent_descriptor=parent_descriptor,
            )
            canonical_published = True
            published_binding = self._relative_file_binding(
                self.path.name,
                parent_descriptor=parent_descriptor,
                allow_missing=False,
            )
            if (
                published_binding is None
                or published_binding.device != temporary_binding.device
                or published_binding.inode != temporary_binding.inode
            ):
                raise RepositoryIndexError("repository index database changed during publication")
            manifest_binding = self._relative_file_binding(
                generation_name,
                parent_descriptor=parent_descriptor,
                allow_missing=False,
            )
            if manifest_binding != published_binding:
                raise RepositoryIndexError(
                    "repository index generation manifest publication failed"
                )
            if parent_descriptor is not None:
                os.fsync(parent_descriptor)
            self._verify_parent_descriptor(parent_descriptor)
            if previous_generation_id is not None:
                self._unlink_relative(
                    self._generation_filename(previous_generation_id),
                    parent_descriptor=parent_descriptor,
                )
                if parent_descriptor is not None:
                    os.fsync(parent_descriptor)
            return published_binding
        finally:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if not generation_staged:
                self._unlink_relative(
                    temporary_name,
                    parent_descriptor=parent_descriptor,
                )
            self._unlink_relative(
                link_name,
                parent_descriptor=parent_descriptor,
            )
            if generation_staged and not canonical_published:
                self._unlink_relative(
                    generation_name,
                    parent_descriptor=parent_descriptor,
                )

    def _relative_file_binding(
        self,
        name: str,
        *,
        parent_descriptor: int | None,
        allow_missing: bool,
    ) -> _FileBinding | None:
        try:
            info = (
                os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
                if parent_descriptor is not None
                else os.lstat(self.path.parent / name)
            )
        except FileNotFoundError:
            if allow_missing:
                return None
            raise RepositoryIndexError(
                "repository index database is missing or inaccessible"
            ) from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RepositoryIndexError("repository index database must be a regular file")
        return _file_binding(info)

    def _open_relative(
        self,
        name: str,
        flags: int,
        *,
        mode: int = 0o600,
        parent_descriptor: int | None,
    ) -> int:
        if parent_descriptor is not None:
            return os.open(name, flags, mode, dir_fd=parent_descriptor)
        return os.open(self.path.parent / name, flags, mode)

    def _replace_relative(
        self,
        source: str,
        destination: str,
        *,
        parent_descriptor: int | None,
    ) -> None:
        if parent_descriptor is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            return
        os.replace(self.path.parent / source, self.path.parent / destination)

    def _link_relative(
        self,
        source: str,
        destination: str,
        *,
        parent_descriptor: int | None,
    ) -> None:
        if parent_descriptor is not None:
            os.link(
                source,
                destination,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            return
        os.link(
            self.path.parent / source,
            self.path.parent / destination,
            follow_symlinks=False,
        )

    def _unlink_relative(self, name: str, *, parent_descriptor: int | None) -> None:
        try:
            if parent_descriptor is not None:
                os.unlink(name, dir_fd=parent_descriptor)
            else:
                os.unlink(self.path.parent / name)
        except FileNotFoundError:
            pass

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._locked_parent(allow_create=False) as parent_descriptor:
            parent_snapshot = self._directory_snapshot(parent_descriptor)
            descriptor, database_binding = self._open_database_descriptor(parent_descriptor)
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect_pinned_read(descriptor)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA foreign_keys = ON")
                self._verify_snapshot_unchanged(
                    parent_descriptor,
                    parent_snapshot=parent_snapshot,
                    database_binding=database_binding,
                    operation="read",
                )
                self._validate_and_bind_generation(
                    connection,
                    binding=database_binding,
                    parent_descriptor=parent_descriptor,
                )
                yield connection
                self._verify_snapshot_unchanged(
                    parent_descriptor,
                    parent_snapshot=parent_snapshot,
                    database_binding=database_binding,
                    operation="read",
                )
                if _file_binding(os.fstat(descriptor)) != database_binding:
                    raise RepositoryIndexError("repository index database changed during read")
            finally:
                if connection is not None:
                    connection.close()
                os.close(descriptor)

    @contextmanager
    def _connection(
        self,
        *,
        write: bool = False,
        integrity_check: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        if not write:
            with self._read_connection() as connection:
                yield connection
            return
        with self._locked_parent() as parent_descriptor:
            parent_snapshot = self._directory_snapshot(parent_descriptor)
            payload, database_binding = self._read_database_snapshot(
                parent_descriptor,
                allow_missing=self._generation is None,
            )
            connection = sqlite3.connect(":memory:", timeout=5.0)
            try:
                self._verify_snapshot_unchanged(
                    parent_descriptor,
                    parent_snapshot=parent_snapshot,
                    database_binding=database_binding,
                    operation="write" if write else "read",
                )
                if payload:
                    connection.deserialize(payload)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                current_generation = (
                    self._validate_write_source(
                        connection,
                        binding=database_binding,
                        parent_descriptor=parent_descriptor,
                    )
                    if database_binding is not None
                    else None
                )
                if integrity_check:
                    quick_check = connection.execute("PRAGMA quick_check").fetchone()
                    if quick_check is None or str(quick_check[0]) != "ok":
                        raise RepositoryIndexError(
                            "repository index database integrity check failed"
                        )
                with connection:
                    yield connection
                self._verify_snapshot_unchanged(
                    parent_descriptor,
                    parent_snapshot=parent_snapshot,
                    database_binding=database_binding,
                    operation="write",
                )
                serialized = connection.serialize()
                if database_binding is None or current_generation is None or serialized != payload:
                    append_previous = (
                        self._read_generation_state(
                            connection,
                            binding=database_binding,
                            allow_empty=True,
                        )
                        if database_binding is not None
                        else None
                    )
                    with connection:
                        (
                            generation_id,
                            sequence,
                            lineage_id,
                            authorization_tag,
                            previous_generation_id,
                        ) = self._append_generation(
                            connection,
                            append_previous,
                        )
                    published_binding = self._publish_database(
                        parent_descriptor,
                        payload=connection.serialize(),
                        expected_binding=database_binding,
                        generation_id=generation_id,
                        previous_generation_id=previous_generation_id,
                    )
                    self._generation = _GenerationState(
                        generation_id=generation_id,
                        sequence=sequence,
                        binding=published_binding,
                        lineage_id=lineage_id,
                        authorization_tag=authorization_tag,
                        previous_generation_id=previous_generation_id,
                    )
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


def _validated_query_offset(offset: int) -> int:
    if isinstance(offset, bool) or not 0 <= offset <= MAX_QUERY_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_QUERY_OFFSET}")
    return offset


def _query_page(
    records: tuple[StoreRecordT, ...],
    *,
    row_count: int,
    limit: int,
    offset: int,
) -> StoreQueryPage[StoreRecordT]:
    truncated = row_count > limit
    return StoreQueryPage(
        records=records,
        truncated=truncated,
        next_offset=offset + len(records) if truncated else None,
    )


def _file_binding(info: os.stat_result) -> _FileBinding:
    return _FileBinding(
        device=int(info.st_dev),
        inode=int(info.st_ino),
        size=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
        ctime_ns=int(info.st_ctime_ns),
    )


def _no_follow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _valid_generation_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _valid_authorization_tag(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _generation_history_limit() -> int:
    if _GENERATION_HISTORY_LIMIT < 2:
        raise RepositoryIndexError("repository index generation history limit must be at least two")
    return _GENERATION_HISTORY_LIMIT


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count == 0:
            raise RepositoryIndexError("repository index snapshot write made no progress")
        written += count


def _absolute_components(path: Path) -> tuple[Path, ...]:
    if not path.is_absolute():
        raise RepositoryIndexError("repository index sidecar path must be absolute")
    current = Path(path.anchor)
    components = [current]
    for part in path.parts[1:]:
        current /= part
        components.append(current)
    return tuple(components)
