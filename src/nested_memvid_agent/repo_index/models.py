from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Generic, TypeVar

DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 1_000
MAX_QUERY_OFFSET = 1_000_000_000


class Freshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class RepositoryIndexError(RuntimeError):
    """Base error for repository index failures."""


class RepositoryRootMismatchError(RepositoryIndexError):
    """The configured repository path no longer names the indexed root."""


class RepositoryChangedDuringIndexingError(RepositoryIndexError):
    """The repository changed while a candidate snapshot was being indexed."""


@dataclass(frozen=True)
class IndexLimits:
    max_file_bytes: int = 1_000_000
    max_files: int = 20_000

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        if self.max_files <= 0:
            raise ValueError("max_files must be positive")


@dataclass(frozen=True)
class RootIdentity:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class IndexStatus:
    schema_version: int
    project_id: str
    repository_root: Path
    aggregate_digest: str
    freshness: Freshness
    indexed_at: str
    parser_versions: dict[str, str]
    git_head: str | None
    git_tree: str | None
    indexed_fingerprint: str
    observed_fingerprint: str
    coverage_complete: bool


@dataclass(frozen=True)
class BuildReport:
    aggregate_digest: str
    changed_files: int
    reused_files: int
    deleted_files: int
    skipped_files: int
    indexed_files: int
    git_head: str | None
    git_tree: str | None


@dataclass(frozen=True)
class FileRecord:
    id: int
    path: Path
    digest: str
    size: int
    language: str
    parser_version: str
    is_test: bool


@dataclass(frozen=True)
class SymbolRecord:
    id: int
    path: Path
    file_digest: str
    name: str
    qualified_name: str
    kind: str
    line: int
    column: int


@dataclass(frozen=True)
class ImportRecord:
    id: int
    path: Path
    file_digest: str
    module: str
    imported_name: str | None
    line: int
    column: int


@dataclass(frozen=True)
class ReferenceRecord:
    id: int
    path: Path
    file_digest: str
    name: str
    line: int
    column: int


@dataclass(frozen=True)
class TestRelationshipRecord:
    id: int
    symbol_name: str
    symbol_path: Path
    test_path: Path
    relationship: str
    evidence_line: int


RecordT = TypeVar("RecordT")


@dataclass(frozen=True)
class IndexQueryResult(Generic[RecordT]):
    records: tuple[RecordT, ...]
    freshness: Freshness
    authoritative: bool
    index_digest: str
    truncated: bool = False
    next_offset: int | None = None


@dataclass(frozen=True)
class ParsedSymbol:
    name: str
    qualified_name: str
    kind: str
    line: int
    column: int


@dataclass(frozen=True)
class ParsedImport:
    module: str
    imported_name: str | None
    line: int
    column: int


@dataclass(frozen=True)
class ParsedReference:
    name: str
    line: int
    column: int


@dataclass(frozen=True)
class ParsedFile:
    symbols: tuple[ParsedSymbol, ...] = ()
    imports: tuple[ParsedImport, ...] = ()
    references: tuple[ParsedReference, ...] = ()


@dataclass(frozen=True)
class CandidateFile:
    path: Path
    relative_path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class IndexedCandidate:
    candidate: CandidateFile
    digest: str
    language: str
    parser_version: str
    is_test: bool
    parsed: ParsedFile


@dataclass(frozen=True)
class RepositorySnapshot:
    candidates: tuple[CandidateFile, ...]
    fingerprint: str
    git_head: str | None
    git_tree: str | None
    skipped_files: int = field(default=0)
    coverage_complete: bool = field(default=True)
