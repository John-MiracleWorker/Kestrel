from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from ..platform_primitives import is_link_or_reparse_point
from .models import (
    DEFAULT_QUERY_LIMIT,
    MAX_QUERY_LIMIT,
    MAX_QUERY_OFFSET,
    BuildReport,
    CandidateFile,
    FileRecord,
    Freshness,
    ImportRecord,
    IndexedCandidate,
    IndexLimits,
    IndexQueryResult,
    IndexStatus,
    ReferenceRecord,
    RepositoryChangedDuringIndexingError,
    RepositoryIndexError,
    RepositoryRootMismatchError,
    RepositorySnapshot,
    RootIdentity,
    SymbolRecord,
    TestRelationshipRecord,
)
from .parsers import PARSER_VERSIONS, language_for_path, parse_file, parser_version
from .store import (
    SCHEMA_VERSION,
    RepoIndexStore,
    StoredMetadata,
    StoreQueryPage,
    StoreQuerySnapshot,
)

_PLATFORM_OS: Any = os

_VALID_PROJECT_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_GIT_OBJECT_ID = re.compile(r"\A[0-9a-fA-F]{40,64}\Z")
_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".kestrel",
    ".mypy_cache",
    ".nest",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "target",
    "vendor",
    "venv",
}
_IGNORED_FILE_SUFFIXES = {
    ".db",
    ".mv2",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".wal",
}

ResultT = TypeVar("ResultT")


class RepositoryIndex:
    """A rebuildable, project-scoped repository intelligence sidecar."""

    def __init__(
        self,
        *,
        project_id: str,
        repository_root: Path,
        index_path: Path | None = None,
        limits: IndexLimits | None = None,
        create: bool = True,
    ) -> None:
        if _VALID_PROJECT_ID.fullmatch(project_id) is None:
            raise ValueError(
                "project_id must start with a letter or digit and contain only "
                "letters, digits, dots, underscores, and hyphens"
            )
        self.project_id = project_id
        self._read_only = not create
        self.limits = limits or IndexLimits()
        self.repository_root = _canonical_root(repository_root)
        default_index_path = index_path is None
        self.index_path = (
            index_path
            if index_path is not None
            else self.repository_root / ".nest" / "repo-index" / f"{project_id}.sqlite"
        )
        self.index_path = self.index_path.absolute()
        _validate_sidecar_path(
            repository_root=self.repository_root,
            index_path=self.index_path,
            default_path=default_index_path,
        )
        self._parser_versions = {
            **PARSER_VERSIONS,
            "scanner": "descriptor-bounded-stat-v3",
            "scanner_limits": (
                f"files={self.limits.max_files};"
                f"bytes={self.limits.max_file_bytes};"
                f"entries={self.limits.scan_entry_budget}"
            ),
        }
        managed_directories = (
            (
                self.repository_root / ".nest",
                self.repository_root / ".nest" / "repo-index",
            )
            if default_index_path
            else ()
        )
        self._store = RepoIndexStore(
            self.index_path,
            managed_directories=managed_directories,
            custom_parent=not default_index_path,
        )
        self._store.initialize(
            project_id=self.project_id,
            root_identity=self._observed_root_identity(),
            parser_versions=self._parser_versions,
            allow_migration=create,
        )

    def rebuild(self) -> BuildReport:
        if self._read_only:
            raise RepositoryIndexError(
                "repository index was opened read-only and cannot be rebuilt"
            )
        with self._root_descriptor() as (root_descriptor, root_identity):
            self._store.assert_root_identity(root_identity)
            before = self._snapshot(root_descriptor)
            force_reparse = self._store.metadata().parser_versions != self._parser_versions
            stored = self._store.stored_files()
            changed: list[IndexedCandidate] = []
            reused_digests: dict[str, str] = {}
            skipped = before.skipped_files
            coverage_complete = before.coverage_complete

            for candidate in before.candidates:
                previous = stored.get(candidate.relative_path)
                if (
                    previous is not None
                    and not force_reparse
                    and previous.device == candidate.device
                    and previous.inode == candidate.inode
                    and previous.size == candidate.size
                    and previous.mtime_ns == candidate.mtime_ns
                    and previous.ctime_ns == candidate.ctime_ns
                ):
                    reused_digests[candidate.relative_path] = previous.digest
                    continue
                indexed = self._index_candidate(candidate, root_descriptor)
                if indexed is None:
                    skipped += 1
                    coverage_complete = False
                    continue
                changed.append(indexed)

            current_digests = dict(reused_digests)
            current_digests.update((item.candidate.relative_path, item.digest) for item in changed)
            deleted = sorted(set(stored) - set(current_digests))
            aggregate_digest = _aggregate_digest(current_digests)

            after = self._snapshot(root_descriptor)
            if (
                after.fingerprint != before.fingerprint
                or after.coverage_complete != before.coverage_complete
            ):
                raise RepositoryChangedDuringIndexingError(
                    "repository changed while the index snapshot was being built"
                )
            self._store.apply_rebuild(
                observed_root=root_identity,
                changed=changed,
                deleted_paths=deleted,
                aggregate_digest=aggregate_digest,
                freshness_fingerprint=after.fingerprint,
                coverage_complete=coverage_complete and after.coverage_complete,
                indexed_at=datetime.now(UTC).isoformat(),
                parser_versions=self._parser_versions,
                git_head=after.git_head,
                git_tree=after.git_tree,
            )
            return BuildReport(
                aggregate_digest=aggregate_digest,
                changed_files=len(changed),
                reused_files=len(reused_digests),
                deleted_files=len(deleted),
                skipped_files=skipped,
                indexed_files=len(current_digests),
                git_head=after.git_head,
                git_tree=after.git_tree,
            )

    def status(self) -> IndexStatus:
        with self._root_descriptor() as (root_descriptor, observed_root):
            metadata = self._store.metadata()
            self._assert_metadata_root(metadata, observed_root)
            observed = self._snapshot(root_descriptor)
            freshness = (
                Freshness.CURRENT
                if metadata.freshness_fingerprint
                and metadata.freshness_fingerprint == observed.fingerprint
                and metadata.parser_versions == self._parser_versions
                and metadata.coverage_complete
                and observed.coverage_complete
                else Freshness.STALE
            )
            return IndexStatus(
                schema_version=SCHEMA_VERSION,
                project_id=metadata.project_id,
                repository_root=Path(metadata.root_path),
                aggregate_digest=metadata.aggregate_digest,
                freshness=freshness,
                indexed_at=metadata.indexed_at,
                parser_versions=metadata.parser_versions,
                git_head=metadata.git_head,
                git_tree=metadata.git_tree,
                indexed_fingerprint=metadata.freshness_fingerprint,
                observed_fingerprint=observed.fingerprint,
                coverage_complete=(metadata.coverage_complete and observed.coverage_complete),
            )

    def files(
        self,
        *,
        limit: int = DEFAULT_QUERY_LIMIT,
        offset: int = 0,
        include_stale_diagnostics: bool = False,
        path_prefixes: Sequence[str] = (),
    ) -> IndexQueryResult[FileRecord]:
        bounded_limit = _validate_query_limit(limit)
        bounded_offset = _validate_query_offset(offset)
        bounded_prefixes = _validate_path_prefixes(path_prefixes)
        return self._query(
            lambda: self._store.files(
                limit=bounded_limit,
                offset=bounded_offset,
                path_prefixes=bounded_prefixes,
            ),
            include_stale_diagnostics=include_stale_diagnostics,
        )

    def symbols(
        self,
        query: str | None = None,
        *,
        limit: int = DEFAULT_QUERY_LIMIT,
        offset: int = 0,
        include_stale_diagnostics: bool = False,
        path_prefixes: Sequence[str] = (),
    ) -> IndexQueryResult[SymbolRecord]:
        bounded_limit = _validate_query_limit(limit)
        bounded_offset = _validate_query_offset(offset)
        bounded_prefixes = _validate_path_prefixes(path_prefixes)
        return self._query(
            lambda: self._store.symbols(
                query,
                limit=bounded_limit,
                offset=bounded_offset,
                path_prefixes=bounded_prefixes,
            ),
            include_stale_diagnostics=include_stale_diagnostics,
        )

    def imports(
        self,
        query: str | None = None,
        *,
        limit: int = DEFAULT_QUERY_LIMIT,
        offset: int = 0,
        include_stale_diagnostics: bool = False,
        path_prefixes: Sequence[str] = (),
    ) -> IndexQueryResult[ImportRecord]:
        bounded_limit = _validate_query_limit(limit)
        bounded_offset = _validate_query_offset(offset)
        bounded_prefixes = _validate_path_prefixes(path_prefixes)
        return self._query(
            lambda: self._store.imports(
                query,
                limit=bounded_limit,
                offset=bounded_offset,
                path_prefixes=bounded_prefixes,
            ),
            include_stale_diagnostics=include_stale_diagnostics,
        )

    def references(
        self,
        name: str | None = None,
        *,
        limit: int = DEFAULT_QUERY_LIMIT,
        offset: int = 0,
        include_stale_diagnostics: bool = False,
        path_prefixes: Sequence[str] = (),
    ) -> IndexQueryResult[ReferenceRecord]:
        bounded_limit = _validate_query_limit(limit)
        bounded_offset = _validate_query_offset(offset)
        bounded_prefixes = _validate_path_prefixes(path_prefixes)
        return self._query(
            lambda: self._store.references(
                name,
                limit=bounded_limit,
                offset=bounded_offset,
                path_prefixes=bounded_prefixes,
            ),
            include_stale_diagnostics=include_stale_diagnostics,
        )

    def tests_for(
        self,
        symbol_name: str,
        *,
        limit: int = DEFAULT_QUERY_LIMIT,
        offset: int = 0,
        include_stale_diagnostics: bool = False,
        path_prefixes: Sequence[str] = (),
    ) -> IndexQueryResult[TestRelationshipRecord]:
        bounded_limit = _validate_query_limit(limit)
        bounded_offset = _validate_query_offset(offset)
        bounded_prefixes = _validate_path_prefixes(path_prefixes)
        return self._query(
            lambda: self._store.tests_for(
                symbol_name,
                limit=bounded_limit,
                offset=bounded_offset,
                path_prefixes=bounded_prefixes,
            ),
            include_stale_diagnostics=include_stale_diagnostics,
        )

    def _query(
        self,
        load: Callable[[], StoreQuerySnapshot[ResultT]],
        *,
        include_stale_diagnostics: bool,
    ) -> IndexQueryResult[ResultT]:
        with self._root_descriptor() as (root_descriptor, observed_root):
            observed_before = self._snapshot(root_descriptor)
            snapshot = load()
            metadata = snapshot.metadata
            self._assert_metadata_root(metadata, observed_root)
            observed_after = self._snapshot(root_descriptor)
            if (
                observed_after.fingerprint != observed_before.fingerprint
                or observed_after.coverage_complete != observed_before.coverage_complete
            ):
                raise RepositoryChangedDuringIndexingError(
                    "repository changed during repository index query"
                )
            observed = observed_after
        freshness = (
            Freshness.CURRENT
            if metadata.freshness_fingerprint
            and metadata.freshness_fingerprint == observed.fingerprint
            and metadata.parser_versions == self._parser_versions
            and metadata.coverage_complete
            and observed.coverage_complete
            else Freshness.STALE
        )
        authoritative = freshness is Freshness.CURRENT
        page = (
            snapshot.page
            if authoritative or include_stale_diagnostics
            else StoreQueryPage(records=(), truncated=False, next_offset=None)
        )
        return IndexQueryResult(
            records=page.records,
            freshness=freshness,
            authoritative=authoritative,
            index_digest=metadata.aggregate_digest,
            truncated=page.truncated,
            next_offset=page.next_offset,
        )

    @staticmethod
    def _assert_metadata_root(
        metadata: StoredMetadata,
        observed: RootIdentity,
    ) -> None:
        if metadata.root_path != str(observed.path):
            raise RepositoryRootMismatchError("repository root path does not match indexed root")
        if metadata.root_device != observed.device or metadata.root_inode != observed.inode:
            raise RepositoryRootMismatchError(
                "repository root identity does not match indexed root"
            )

    def _snapshot(self, root_descriptor: int | None) -> RepositorySnapshot:
        candidates, skipped, coverage_complete = _scan_candidates(
            self.repository_root,
            self.limits,
            root_descriptor=root_descriptor,
        )
        git_head, git_tree = _git_identity(self.repository_root)
        fingerprint = _freshness_fingerprint(candidates, git_head, git_tree)
        return RepositorySnapshot(
            candidates=tuple(candidates),
            fingerprint=fingerprint,
            git_head=git_head,
            git_tree=git_tree,
            skipped_files=skipped,
            coverage_complete=coverage_complete,
        )

    def _observed_root_identity(self) -> RootIdentity:
        try:
            info = os.lstat(self.repository_root)
        except OSError as exc:
            raise RepositoryRootMismatchError("repository root is missing or inaccessible") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RepositoryRootMismatchError("repository root must not be a symbolic link")
        if not stat.S_ISDIR(info.st_mode):
            raise RepositoryRootMismatchError("repository root is not a directory")
        return RootIdentity(
            path=self.repository_root,
            device=int(info.st_dev),
            inode=int(info.st_ino),
        )

    def _index_candidate(
        self, candidate: CandidateFile, root_descriptor: int | None
    ) -> IndexedCandidate | None:
        content = _read_stable_text(
            candidate,
            self.limits.max_file_bytes,
            root_descriptor=root_descriptor,
        )
        if content is None:
            return None
        language = language_for_path(candidate.path)
        raw = content.encode("utf-8")
        return IndexedCandidate(
            candidate=candidate,
            digest=hashlib.sha256(raw).hexdigest(),
            language=language,
            parser_version=parser_version(language),
            is_test=_is_test_path(Path(candidate.relative_path)),
            parsed=parse_file(candidate.path, content, language),
        )

    @contextmanager
    def _root_descriptor(self) -> Iterator[tuple[int | None, RootIdentity]]:
        before = self._observed_root_identity()
        descriptor: int | None = None
        if hasattr(os, "O_DIRECTORY"):
            flags = os.O_RDONLY | os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= _PLATFORM_OS.O_NOFOLLOW
            try:
                descriptor = os.open(self.repository_root, flags)
            except OSError as exc:
                raise RepositoryRootMismatchError(
                    "repository root could not be opened without following links"
                ) from exc
            opened = os.fstat(descriptor)
            opened_identity = RootIdentity(
                path=self.repository_root,
                device=int(opened.st_dev),
                inode=int(opened.st_ino),
            )
            if opened_identity != before or not stat.S_ISDIR(opened.st_mode):
                os.close(descriptor)
                raise RepositoryRootMismatchError("repository root identity changed while opening")
        try:
            yield descriptor, before
            after = self._observed_root_identity()
            if after != before:
                raise RepositoryRootMismatchError(
                    "repository root identity changed during index operation"
                )
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _canonical_root(path: Path) -> Path:
    expanded = path.expanduser().absolute()
    try:
        if expanded.is_symlink():
            raise RepositoryRootMismatchError("repository root must not be a symbolic link")
        resolved = expanded.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RepositoryRootMismatchError("repository root does not exist") from exc
    if not resolved.is_dir():
        raise RepositoryRootMismatchError("repository root is not a directory")
    return resolved


def _validate_sidecar_path(
    *,
    repository_root: Path,
    index_path: Path,
    default_path: bool,
) -> None:
    if index_path.is_symlink():
        raise RepositoryIndexError("repository index sidecar must not be a symbolic link")
    if not default_path:
        return
    for directory in (
        repository_root / ".nest",
        repository_root / ".nest" / "repo-index",
    ):
        if directory.is_symlink():
            raise RepositoryIndexError(
                "repository index sidecar directory must not be a symbolic link"
            )
        if directory.exists() and not directory.is_dir():
            raise RepositoryIndexError("repository index sidecar parent must be a directory")


def _scan_candidates(
    repository_root: Path,
    limits: IndexLimits,
    *,
    root_descriptor: int | None,
) -> tuple[list[CandidateFile], int, bool]:
    if root_descriptor is not None and hasattr(os, "fwalk"):
        return _scan_candidates_from_descriptor(
            repository_root,
            limits,
            root_descriptor=root_descriptor,
        )
    return _scan_candidates_from_path(repository_root, limits)


def _scan_candidates_from_descriptor(
    repository_root: Path,
    limits: IndexLimits,
    *,
    root_descriptor: int,
) -> tuple[list[CandidateFile], int, bool]:
    """Traverse from a pinned root without materializing whole directories.

    Ordering only affects partial, non-authoritative snapshots. Complete
    snapshots are sorted before fingerprinting, so filesystem enumeration order
    cannot affect authoritative results.
    """

    candidates: list[CandidateFile] = []
    skipped = 0
    coverage_complete = True
    inspected_entries = 0
    stop = False

    def visit(directory_descriptor: int, relative_parent: Path) -> None:
        nonlocal coverage_complete, inspected_entries, skipped, stop
        if stop:
            return
        try:
            iterator = os.scandir(directory_descriptor)
        except OSError:
            coverage_complete = False
            return
        with iterator:
            for entry in iterator:
                inspected_entries += 1
                if inspected_entries > limits.scan_entry_budget:
                    coverage_complete = False
                    stop = True
                    return
                name = entry.name
                relative_path = relative_parent / name
                display_path = repository_root / relative_path
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped += 1
                    coverage_complete = False
                    continue
                if is_link_or_reparse_point(info):
                    skipped += 1
                    continue
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    if name.casefold() in _IGNORED_DIRECTORIES:
                        continue
                    flags = os.O_RDONLY
                    if hasattr(os, "O_DIRECTORY"):
                        flags |= os.O_DIRECTORY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    try:
                        child_descriptor = os.open(
                            name,
                            flags,
                            dir_fd=directory_descriptor,
                        )
                    except OSError:
                        coverage_complete = False
                        continue
                    try:
                        visit(child_descriptor, relative_path)
                    finally:
                        os.close(child_descriptor)
                    if stop:
                        return
                    continue
                if _ignored_file(display_path):
                    skipped += 1
                    continue
                candidate = _candidate_from_stat(
                    path=display_path,
                    relative_path=relative_path.as_posix(),
                    info=info,
                    limits=limits,
                    candidate_count=len(candidates),
                )
                if candidate is None:
                    skipped += 1
                    if stat.S_ISREG(info.st_mode):
                        coverage_complete = False
                    if (
                        len(candidates) >= limits.max_files
                        and stat.S_ISREG(info.st_mode)
                    ):
                        stop = True
                        return
                    continue
                candidates.append(candidate)

    visit(root_descriptor, Path())
    candidates.sort(key=lambda item: item.relative_path)
    return candidates, skipped, coverage_complete


def _scan_candidates_from_path(
    repository_root: Path,
    limits: IndexLimits,
) -> tuple[list[CandidateFile], int, bool]:
    """Portable bounded traversal for platforms without directory descriptors."""

    candidates: list[CandidateFile] = []
    skipped = 0
    coverage_complete = True
    inspected_entries = 0
    stop = False

    def visit(current_path: Path, relative_parent: Path) -> None:
        nonlocal coverage_complete, inspected_entries, skipped, stop
        if stop:
            return
        try:
            iterator = os.scandir(current_path)
        except OSError:
            coverage_complete = False
            return
        with iterator:
            for entry in iterator:
                inspected_entries += 1
                if inspected_entries > limits.scan_entry_budget:
                    coverage_complete = False
                    stop = True
                    return
                name = entry.name
                path = current_path / name
                relative_path = relative_parent / name
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped += 1
                    coverage_complete = False
                    continue
                if is_link_or_reparse_point(info):
                    skipped += 1
                    continue
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    if name.casefold() not in _IGNORED_DIRECTORIES:
                        visit(path, relative_path)
                    if stop:
                        return
                    continue
                if _ignored_file(path):
                    skipped += 1
                    continue
                candidate = _candidate_from_stat(
                    path=path,
                    relative_path=relative_path.as_posix(),
                    info=info,
                    limits=limits,
                    candidate_count=len(candidates),
                )
                if candidate is None:
                    skipped += 1
                    if stat.S_ISREG(info.st_mode):
                        coverage_complete = False
                    if (
                        len(candidates) >= limits.max_files
                        and stat.S_ISREG(info.st_mode)
                    ):
                        stop = True
                        return
                    continue
                candidates.append(candidate)

    visit(repository_root, Path())
    candidates.sort(key=lambda item: item.relative_path)
    return candidates, skipped, coverage_complete


def _candidate_from_stat(
    *,
    path: Path,
    relative_path: str,
    info: os.stat_result,
    limits: IndexLimits,
    candidate_count: int,
) -> CandidateFile | None:
    if (
        is_link_or_reparse_point(info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > limits.max_file_bytes
        or candidate_count >= limits.max_files
    ):
        return None
    stable_info = info
    if getattr(_PLATFORM_OS, "name", os.name) == "nt":
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RepositoryChangedDuringIndexingError(
                f"repository file could not be pinned during scan: {relative_path}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            visible = os.lstat(path)
        except OSError as exc:
            raise RepositoryChangedDuringIndexingError(
                f"repository file changed during scan: {relative_path}"
            ) from exc
        if (
            is_link_or_reparse_point(visible)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(visible.st_mode)
            or (
                visible.st_dev,
                visible.st_ino,
                visible.st_size,
                visible.st_mtime_ns,
                visible.st_ctime_ns,
            )
            != (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            or opened.st_size != info.st_size
            or opened.st_mtime_ns != info.st_mtime_ns
        ):
            raise RepositoryChangedDuringIndexingError(
                f"repository file changed during scan: {relative_path}"
            )
        stable_info = opened
    return CandidateFile(
        path=path,
        relative_path=relative_path,
        device=int(stable_info.st_dev),
        inode=int(stable_info.st_ino),
        size=int(stable_info.st_size),
        mtime_ns=int(stable_info.st_mtime_ns),
        ctime_ns=int(stable_info.st_ctime_ns),
    )


def _ignored_file(path: Path) -> bool:
    lower_name = path.name.casefold()
    if lower_name.endswith(("-journal", "-shm", "-wal")):
        return True
    return path.suffix.casefold() in _IGNORED_FILE_SUFFIXES


def _read_stable_text(
    candidate: CandidateFile,
    max_bytes: int,
    *,
    root_descriptor: int | None,
) -> str | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = _open_candidate(
            candidate,
            flags=flags,
            root_descriptor=root_descriptor,
        )
    except OSError as exc:
        raise RepositoryChangedDuringIndexingError(
            f"repository file could not be opened for indexing: {candidate.relative_path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != candidate.device
            or before.st_ino != candidate.inode
            or before.st_size != candidate.size
            or before.st_mtime_ns != candidate.mtime_ns
            or before.st_ctime_ns != candidate.ctime_ns
        ):
            raise RepositoryChangedDuringIndexingError(
                f"repository file changed before indexing: {candidate.relative_path}"
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise RepositoryChangedDuringIndexingError(
                f"repository file changed while indexing: {candidate.relative_path}"
            )
    finally:
        os.close(descriptor)
    if len(payload) > max_bytes or b"\x00" in payload or _has_binary_control_density(payload):
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _open_candidate(
    candidate: CandidateFile,
    *,
    flags: int,
    root_descriptor: int | None,
) -> int:
    if root_descriptor is None:
        return os.open(candidate.path, flags)
    parts = Path(candidate.relative_path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("invalid repository-relative candidate path")
    parent_descriptor = os.dup(root_descriptor)
    try:
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        for part in parts[:-1]:
            next_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        return os.open(parts[-1], flags, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _has_binary_control_density(payload: bytes) -> bool:
    allowed_controls = {9, 10, 12, 13}
    return any((value < 32 and value not in allowed_controls) or value == 127 for value in payload)


def _freshness_fingerprint(
    candidates: list[CandidateFile],
    git_head: str | None,
    git_tree: str | None,
) -> str:
    digest = hashlib.sha256()
    digest.update(f"git-head:{git_head or '-'}\n".encode())
    digest.update(f"git-tree:{git_tree or '-'}\n".encode())
    for candidate in candidates:
        digest.update(
            (
                f"{candidate.relative_path}\0{candidate.device}\0{candidate.inode}\0"
                f"{candidate.size}\0"
                f"{candidate.mtime_ns}\0{candidate.ctime_ns}\n"
            ).encode()
        )
    return digest.hexdigest()


def _aggregate_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, file_digest in sorted(files.items()):
        digest.update(f"{path}\0{file_digest}\n".encode())
    return digest.hexdigest()


def _git_identity(repository_root: Path) -> tuple[str | None, str | None]:
    head = _git_rev_parse(repository_root, "HEAD")
    tree = _git_rev_parse(repository_root, "HEAD^{tree}") if head else None
    return head, tree


def _git_rev_parse(repository_root: Path, revision: str) -> str | None:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", revision],
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or _GIT_OBJECT_ID.fullmatch(value) is None:
        return None
    return value.lower()


def _is_test_path(path: Path) -> bool:
    folded_parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    return (
        "test" in folded_parts
        or "tests" in folded_parts
        or "spec" in folded_parts
        or "specs" in folded_parts
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.go")
    )


def _validate_query_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    return limit


def _validate_query_offset(offset: int) -> int:
    if isinstance(offset, bool) or not 0 <= offset <= MAX_QUERY_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_QUERY_OFFSET}")
    return offset


def _validate_path_prefixes(prefixes: Sequence[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in prefixes:
        if not isinstance(raw, str):
            raise ValueError("path prefixes must be strings")
        value = raw.strip().strip("/")
        if value in {"", "."}:
            return ()
        pure = PurePosixPath(value)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("path prefix is invalid")
        normalized.add(pure.as_posix())
    return tuple(sorted(normalized))
