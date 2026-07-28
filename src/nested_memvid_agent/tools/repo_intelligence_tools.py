from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..repo_index import RepositoryIndex, RepositoryIndexError
from ..repo_index.models import IndexQueryResult
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..security_boundary import assert_path_not_sensitive, redact_text
from .base import AgentTool, ToolContext
from .workspace_tools import (
    _assert_workspace_path_allowed,
    _open_workspace_regular_file,
    _safe_path,
)

_MAX_TOOL_QUERY_LIMIT = 100
_MAX_CONTEXT_CHARS = 50_000
_MAX_CONTEXT_FILE_BYTES = 1_000_000
_MAX_CONTEXT_EVIDENCE = 24
_VALID_PROJECT_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REPOSITORY_QUERY_TOKEN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:(?:::|[.:-])[A-Za-z0-9_]+)*"
)
_REPOSITORY_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "are",
        "at",
        "be",
        "by",
        "can",
        "class",
        "code",
        "defined",
        "definition",
        "definitions",
        "dependency",
        "dependencies",
        "does",
        "exercise",
        "exercises",
        "file",
        "files",
        "find",
        "for",
        "from",
        "function",
        "get",
        "give",
        "implementation",
        "implementations",
        "import",
        "imported",
        "imports",
        "in",
        "is",
        "it",
        "locate",
        "method",
        "of",
        "please",
        "reference",
        "referenced",
        "references",
        "show",
        "source",
        "symbol",
        "test",
        "tests",
        "the",
        "to",
        "type",
        "uses",
        "using",
        "what",
        "where",
        "which",
        "who",
    }
)
_DEFINITION_INTENT = frozenset(
    {"class", "defined", "definition", "definitions", "function", "implementation", "type"}
)
_REFERENCE_INTENT = frozenset(
    {"call", "calls", "reference", "referenced", "references", "use", "uses", "using"}
)
_IMPORT_INTENT = frozenset({"dependency", "dependencies", "import", "imported", "imports"})
_TEST_INTENT = frozenset(
    {"coverage", "exercise", "exercises", "spec", "specs", "test", "tested", "tests"}
)


@dataclass(frozen=True)
class _ContextCandidate:
    score: int
    term_rank: int
    path: Path
    line: int
    expected_digest: str | None
    relation: str
    label: str
    query_term: str


class _RepoToolFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RepoSymbolsTool(AgentTool):
    spec = ToolSpec(
        name="repo.symbols",
        description=(
            "Query the current project index for definitions with exact path, line, "
            "file digest, index digest, and freshness evidence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_TOOL_QUERY_LIMIT},
                "offset": {"type": "integer", "minimum": 0},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            index = _existing_project_index(context)
            limit, offset = _pagination(arguments)
            query = _optional_query(arguments.get("query"))
            result = index.symbols(
                query,
                limit=limit,
                offset=offset,
                path_prefixes=_allowed_index_prefixes(context),
            )
            rows = [
                {
                    "name": record.name,
                    "qualified_name": record.qualified_name,
                    "kind": record.kind,
                    "path": record.path.as_posix(),
                    "line": record.line,
                    "column": record.column,
                    "file_digest": record.file_digest,
                }
                for record in result.records
                if _record_path_allowed(context, record.path)
            ]
            return _query_execution(call, result, rows)
        except _RepoToolFailure as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except (OSError, RepositoryIndexError, sqlite3.Error, ValueError) as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repo_index_query_failed",
            )


class RepoReferencesTool(AgentTool):
    spec = ToolSpec(
        name="repo.references",
        description=(
            "Find lexical references to a symbol in the current project index with "
            "path, line, file digest, index digest, and freshness evidence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_TOOL_QUERY_LIMIT},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["name"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            name = _required_query(arguments.get("name"), field="name")
            index = _existing_project_index(context)
            limit, offset = _pagination(arguments)
            result = index.references(
                name,
                limit=limit,
                offset=offset,
                path_prefixes=_allowed_index_prefixes(context),
            )
            rows = [
                {
                    "name": record.name,
                    "path": record.path.as_posix(),
                    "line": record.line,
                    "column": record.column,
                    "file_digest": record.file_digest,
                }
                for record in result.records
                if _record_path_allowed(context, record.path)
            ]
            return _query_execution(call, result, rows)
        except _RepoToolFailure as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except (OSError, RepositoryIndexError, sqlite3.Error, ValueError) as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repo_index_query_failed",
            )


class RepoDependenciesTool(AgentTool):
    spec = ToolSpec(
        name="repo.dependencies",
        description=(
            "Query parsed import dependencies by module or imported name in the current "
            "project index. Results are evidence, not package-manager authority."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_TOOL_QUERY_LIMIT},
                "offset": {"type": "integer", "minimum": 0},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            index = _existing_project_index(context)
            limit, offset = _pagination(arguments)
            query = _optional_query(arguments.get("query"))
            result = index.imports(
                query,
                limit=limit,
                offset=offset,
                path_prefixes=_allowed_index_prefixes(context),
            )
            rows = [
                {
                    "relation": "import",
                    "module": record.module,
                    "imported_name": record.imported_name,
                    "path": record.path.as_posix(),
                    "line": record.line,
                    "column": record.column,
                    "file_digest": record.file_digest,
                }
                for record in result.records
                if _record_path_allowed(context, record.path)
            ]
            return _query_execution(call, result, rows)
        except _RepoToolFailure as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except (OSError, RepositoryIndexError, sqlite3.Error, ValueError) as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repo_index_query_failed",
            )


class RepoTestsForTool(AgentTool):
    spec = ToolSpec(
        name="repo.tests_for",
        description=(
            "Return index-derived test ownership evidence for a symbol. A relationship "
            "identifies a targeted-test candidate, not proof that the test is sufficient."
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "minLength": 1, "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_TOOL_QUERY_LIMIT},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["symbol"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            symbol = _required_query(arguments.get("symbol"), field="symbol")
            index = _existing_project_index(context)
            limit, offset = _pagination(arguments)
            result = index.tests_for(
                symbol,
                limit=limit,
                offset=offset,
                path_prefixes=_allowed_index_prefixes(context),
            )
            rows = [
                {
                    "symbol": record.symbol_name,
                    "symbol_path": record.symbol_path.as_posix(),
                    "test_path": record.test_path.as_posix(),
                    "relationship": record.relationship,
                    "evidence_line": record.evidence_line,
                }
                for record in result.records
                if _record_path_allowed(context, record.symbol_path)
                and _record_path_allowed(context, record.test_path)
            ]
            return _query_execution(call, result, rows)
        except _RepoToolFailure as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except (OSError, RepositoryIndexError, sqlite3.Error, ValueError) as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repo_index_query_failed",
            )


class RepoImpactTool(AgentTool):
    spec = ToolSpec(
        name="repo.impact",
        description=(
            "Assemble bounded definition, reference, and test-ownership evidence for one "
            "symbol. Mixed generations or stale indexes fail closed as non-authoritative."
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "minLength": 1, "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_TOOL_QUERY_LIMIT},
            },
            "required": ["symbol"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            symbol = _required_query(arguments.get("symbol"), field="symbol")
            limit, _ = _pagination(arguments)
            index = _existing_project_index(context)
            path_prefixes = _allowed_index_prefixes(context)
            definitions = index.symbols(
                symbol,
                limit=limit,
                path_prefixes=path_prefixes,
            )
            references = index.references(
                symbol,
                limit=limit,
                path_prefixes=path_prefixes,
            )
            tests = index.tests_for(
                symbol,
                limit=limit,
                path_prefixes=path_prefixes,
            )
            authoritative, digest, freshness = _combined_query_authority(
                definitions,
                references,
                tests,
            )
            rows: list[dict[str, Any]] = []
            if authoritative:
                rows.extend(
                    {
                        "relation": "definition",
                        "name": record.name,
                        "qualified_name": record.qualified_name,
                        "kind": record.kind,
                        "path": record.path.as_posix(),
                        "line": record.line,
                        "column": record.column,
                        "file_digest": record.file_digest,
                    }
                    for record in definitions.records
                    if record.name.casefold() == symbol.casefold()
                    and _record_path_allowed(context, record.path)
                )
                rows.extend(
                    {
                        "relation": "reference",
                        "name": record.name,
                        "path": record.path.as_posix(),
                        "line": record.line,
                        "column": record.column,
                        "file_digest": record.file_digest,
                    }
                    for record in references.records
                    if _record_path_allowed(context, record.path)
                )
                rows.extend(
                    {
                        "relation": "test",
                        "symbol": record.symbol_name,
                        "symbol_path": record.symbol_path.as_posix(),
                        "test_path": record.test_path.as_posix(),
                        "evidence_line": record.evidence_line,
                        "relationship": record.relationship,
                    }
                    for record in tests.records
                    if _record_path_allowed(context, record.symbol_path)
                    and _record_path_allowed(context, record.test_path)
                )
            payload = {
                "records": rows[:limit],
                "freshness": freshness,
                "authoritative": authoritative,
                "index_digest": digest,
                "truncated": (
                    len(rows) > limit
                    or definitions.truncated
                    or references.truncated
                    or tests.truncated
                ),
                "next_offset": None,
            }
            return self._result(
                call,
                success=True,
                content=json.dumps(payload, indent=2),
                data=payload,
            )
        except _RepoToolFailure as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except (OSError, RepositoryIndexError, sqlite3.Error, ValueError) as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repo_index_query_failed",
            )


class RepoContextPackTool(AgentTool):
    spec = ToolSpec(
        name="repo.context_pack",
        description=(
            "Build a bounded repository context pack from current symbol/reference "
            "evidence and digest-verified source snippets. It never reads stale rows."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 512},
                "max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": _MAX_CONTEXT_CHARS,
                },
                "line_window": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            query = _required_query(arguments.get("query"), field="query")
            max_chars = _bounded_int(
                arguments.get("max_chars", 12_000),
                minimum=1_000,
                maximum=_MAX_CONTEXT_CHARS,
                field="max_chars",
            )
            line_window = _bounded_int(
                arguments.get("line_window", 5),
                minimum=1,
                maximum=20,
                field="line_window",
            )
            index = _existing_project_index(context)
            path_prefixes = _allowed_index_prefixes(context)
            query_terms = _repository_query_terms(query)
            query_intent = {
                token.casefold() for token in _REPOSITORY_QUERY_TOKEN.findall(query)
            }
            candidates, results = _context_candidates(
                index,
                context=context,
                query_terms=query_terms,
                query_intent=query_intent,
                path_prefixes=path_prefixes,
            )
            authoritative, digest, freshness = _combined_query_authority(*results)
            evidence: list[dict[str, Any]] = []
            blocks: list[str] = []
            remaining = max_chars
            seen: set[tuple[str, int]] = set()
            read_drift = False
            ranked_candidates = _rank_context_candidates(candidates) if authoritative else []
            candidate_locations = {
                (candidate.path.as_posix(), candidate.line)
                for candidate in ranked_candidates
            }
            for candidate in ranked_candidates:
                identity = (candidate.path.as_posix(), candidate.line)
                if identity in seen or len(evidence) >= _MAX_CONTEXT_EVIDENCE:
                    continue
                seen.add(identity)
                snippet, observed_digest = _verified_snippet(
                    context,
                    candidate.path,
                    line=candidate.line,
                    line_window=line_window,
                )
                if not observed_digest or (
                    candidate.expected_digest is not None
                    and not hmac.compare_digest(observed_digest, candidate.expected_digest)
                ):
                    read_drift = True
                    continue
                block = (
                    f"### {candidate.path.as_posix()}:{candidate.line} "
                    f"[{candidate.relation}:{candidate.label}]\n"
                    f"file_sha256={observed_digest}\n{snippet}"
                )
                if len(block) > remaining:
                    break
                blocks.append(block)
                remaining -= len(block)
                evidence.append(
                    {
                        "path": candidate.path.as_posix(),
                        "line": candidate.line,
                        "relation": candidate.relation,
                        "label": candidate.label,
                        "query_term": candidate.query_term,
                        "match_score": candidate.score,
                        "file_digest": observed_digest,
                    }
                )
            if authoritative:
                final_status = index.status()
                if (
                    final_status.freshness.value != "current"
                    or not hmac.compare_digest(final_status.aggregate_digest, digest)
                ):
                    read_drift = True
            if read_drift:
                authoritative = False
                evidence = []
                blocks = []
            payload = {
                "query": query,
                "query_terms": list(query_terms),
                "context": "\n\n".join(blocks),
                "evidence": evidence,
                "freshness": freshness,
                "authoritative": authoritative,
                "index_digest": digest,
                "truncated": (
                    any(result.truncated for result in results)
                    or len(evidence) < len(candidate_locations)
                    or read_drift
                ),
                "read_drift_detected": read_drift,
            }
            return self._result(
                call,
                success=True,
                content=json.dumps(payload, indent=2),
                data=payload,
            )
        except _RepoToolFailure as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except (OSError, RepositoryIndexError, sqlite3.Error, UnicodeError, ValueError) as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repo_index_query_failed",
            )


def _repository_query_terms(query: str) -> tuple[str, ...]:
    tokens = [
        token.strip("._:-")
        for token in _REPOSITORY_QUERY_TOKEN.findall(query)
        if token.strip("._:-")
    ]
    selected: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        folded = token.casefold()
        if folded in _REPOSITORY_QUERY_STOPWORDS or folded in seen:
            continue
        if len(token) == 1 and token != token.upper():
            continue
        selected.append(token)
        seen.add(folded)
        if len(selected) == 8:
            break
    if selected:
        return tuple(selected)
    for token in sorted(tokens, key=lambda value: (-len(value), value.casefold(), value)):
        folded = token.casefold()
        if folded not in seen:
            return (token,)
    return (query,)


def _context_candidates(
    index: RepositoryIndex,
    *,
    context: ToolContext,
    query_terms: tuple[str, ...],
    query_intent: set[str],
    path_prefixes: tuple[str, ...],
) -> tuple[list[_ContextCandidate], tuple[IndexQueryResult[Any], ...]]:
    candidates: list[_ContextCandidate] = []
    results: list[IndexQueryResult[Any]] = []
    definition_bonus = 20 if query_intent & _DEFINITION_INTENT else 0
    reference_bonus = 55 if query_intent & _REFERENCE_INTENT else 0
    import_bonus = 70 if query_intent & _IMPORT_INTENT else 0
    test_bonus = 70 if query_intent & _TEST_INTENT else 0

    for term_rank, term in enumerate(query_terms):
        symbols = index.symbols(
            term,
            limit=_MAX_CONTEXT_EVIDENCE,
            path_prefixes=path_prefixes,
        )
        references = index.references(
            term,
            limit=_MAX_CONTEXT_EVIDENCE,
            path_prefixes=path_prefixes,
        )
        imports = index.imports(
            term,
            limit=_MAX_CONTEXT_EVIDENCE,
            path_prefixes=path_prefixes,
        )
        tests = index.tests_for(
            term,
            limit=_MAX_CONTEXT_EVIDENCE,
            path_prefixes=path_prefixes,
        )
        results.extend((symbols, references, imports, tests))

        candidates.extend(
            _ContextCandidate(
                score=_definition_match_score(
                    term,
                    name=record.name,
                    qualified_name=record.qualified_name,
                )
                + definition_bonus,
                term_rank=term_rank,
                path=record.path,
                line=record.line,
                expected_digest=record.file_digest,
                relation="definition",
                label=record.qualified_name or record.name,
                query_term=term,
            )
            for record in symbols.records
            if _record_path_allowed(context, record.path)
        )
        candidates.extend(
            _ContextCandidate(
                score=85 + reference_bonus,
                term_rank=term_rank,
                path=record.path,
                line=record.line,
                expected_digest=record.file_digest,
                relation="reference",
                label=record.name,
                query_term=term,
            )
            for record in references.records
            if _record_path_allowed(context, record.path)
        )
        candidates.extend(
            _ContextCandidate(
                score=70 + import_bonus,
                term_rank=term_rank,
                path=record.path,
                line=record.line,
                expected_digest=record.file_digest,
                relation="import",
                label=record.imported_name or record.module,
                query_term=term,
            )
            for record in imports.records
            if _record_path_allowed(context, record.path)
        )
        candidates.extend(
            _ContextCandidate(
                score=90 + test_bonus,
                term_rank=term_rank,
                path=record.test_path,
                line=record.evidence_line,
                expected_digest=None,
                relation="test",
                label=record.symbol_name,
                query_term=term,
            )
            for record in tests.records
            if _record_path_allowed(context, record.symbol_path)
            and _record_path_allowed(context, record.test_path)
        )

    return candidates, tuple(results)


def _definition_match_score(term: str, *, name: str, qualified_name: str) -> int:
    folded_term = term.casefold()
    folded_name = name.casefold()
    folded_qualified = qualified_name.casefold()
    if folded_name == folded_term:
        return 120
    if folded_qualified == folded_term:
        return 118
    if folded_qualified.endswith((f".{folded_term}", f"::{folded_term}")):
        return 115
    if folded_name.startswith(folded_term) or folded_name.endswith(folded_term):
        return 100
    return 90


def _rank_context_candidates(
    candidates: list[_ContextCandidate],
) -> list[_ContextCandidate]:
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -candidate.score,
            candidate.term_rank,
            candidate.path.as_posix().casefold(),
            candidate.path.as_posix(),
            candidate.line,
            candidate.relation,
            candidate.label.casefold(),
            candidate.label,
        ),
    )
    selected: list[_ContextCandidate] = []
    seen: set[tuple[str, int]] = set()
    for candidate in ranked:
        identity = (candidate.path.as_posix(), candidate.line)
        if identity in seen:
            continue
        selected.append(candidate)
        seen.add(identity)
    return selected


def _existing_project_index(context: ToolContext) -> RepositoryIndex:
    project_id = str(context.project_id or "").strip()
    if not project_id:
        raise _RepoToolFailure(
            "project_context_required",
            "Repository intelligence requires a project-bound run.",
        )
    if _VALID_PROJECT_ID.fullmatch(project_id) is None:
        raise _RepoToolFailure(
            "project_context_invalid",
            "The project identity is invalid.",
        )
    requested_root = Path(context.workspace)
    if requested_root.is_symlink():
        raise _RepoToolFailure(
            "project_context_invalid",
            "The project workspace root must not be a symbolic link.",
        )
    root = requested_root.resolve(strict=True)
    sidecar = root / ".nest" / "repo-index" / f"{project_id}.sqlite"
    try:
        metadata = sidecar.lstat()
    except FileNotFoundError as exc:
        raise _RepoToolFailure(
            "repo_index_missing",
            "No repository index exists for this project; request an explicit rebuild.",
        ) from exc
    if sidecar.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise _RepoToolFailure(
            "repo_index_untrusted",
            "The project repository-index sidecar is not a trusted regular file.",
        )
    if os.name == "posix" and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise _RepoToolFailure(
            "repo_index_untrusted",
            "The project repository-index sidecar has an unsafe owner or mode.",
        )
    expected_digest = str(context.project_baseline_index_digest or "").strip()
    if (
        context.project_revision is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        raise _RepoToolFailure(
            "repo_index_rebuild_required",
            "The project has no current repository-index baseline binding.",
        )
    try:
        index = RepositoryIndex(
            project_id=project_id,
            repository_root=root,
            create=False,
        )
        status = index.status()
    except RepositoryIndexError as exc:
        raise _RepoToolFailure(
            "repo_index_rebuild_required",
            "The repository index requires an explicit rebuild before it can be queried.",
        ) from exc
    if not hmac.compare_digest(status.aggregate_digest, expected_digest):
        raise _RepoToolFailure(
            "repo_index_rebuild_required",
            "The repository index is not bound to the current project baseline.",
        )
    return index


def _pagination(arguments: dict[str, Any]) -> tuple[int, int]:
    return (
        _bounded_int(
            arguments.get("limit", 50),
            minimum=1,
            maximum=_MAX_TOOL_QUERY_LIMIT,
            field="limit",
        ),
        _bounded_int(
            arguments.get("offset", 0),
            minimum=0,
            maximum=1_000_000_000,
            field="offset",
        ),
    )


def _bounded_int(value: object, *, minimum: int, maximum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _RepoToolFailure("invalid_tool_arguments", f"{field} must be an integer")
    parsed = value
    if not minimum <= parsed <= maximum:
        raise _RepoToolFailure(
            "invalid_tool_arguments",
            f"{field} must be between {minimum} and {maximum}",
        )
    return parsed


def _required_query(value: object, *, field: str) -> str:
    query = _optional_query(value)
    if query is None:
        raise _RepoToolFailure(
            "invalid_tool_arguments",
            f"{field} must be a non-empty string",
        )
    return query


def _optional_query(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _RepoToolFailure("invalid_tool_arguments", "query must be a string")
    query = value.strip()
    if not query:
        return None
    if len(query) > 512 or any(
        ord(character) < 32 or ord(character) == 127 for character in query
    ):
        raise _RepoToolFailure("invalid_tool_arguments", "query is invalid or too long")
    return query


def _record_path_allowed(context: ToolContext, relative: Path) -> bool:
    try:
        requested = relative.as_posix()
        candidate = _safe_path(context.workspace, requested)
        assert_path_not_sensitive(
            context.workspace,
            candidate,
            requested_path=requested,
        )
        _assert_workspace_path_allowed(
            context,
            candidate,
            requested_path=requested,
        )
        return True
    except (OSError, PermissionError, ValueError):
        return False


def _allowed_index_prefixes(context: ToolContext) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in context.allowed_paths:
        value = str(raw).strip().strip("/")
        if value in {"", "."}:
            return ()
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts or "\\" in value:
            raise _RepoToolFailure(
                "project_context_invalid",
                "The project allowed-path ceiling is invalid.",
            )
        normalized.add(pure.as_posix())
    return tuple(sorted(normalized))


def _query_execution(
    call: ToolCall,
    result: IndexQueryResult[Any],
    records: list[dict[str, Any]],
) -> ToolExecution:
    safe_records = records if result.authoritative else []
    payload = {
        "records": safe_records,
        "freshness": result.freshness.value,
        "authoritative": result.authoritative,
        "index_digest": result.index_digest,
        "truncated": result.truncated or len(safe_records) != len(result.records),
        "next_offset": result.next_offset,
    }
    return ToolExecution(
        call=call,
        success=True,
        content=json.dumps(payload, indent=2),
        data=payload,
    )


def _combined_query_authority(
    *results: IndexQueryResult[Any],
) -> tuple[bool, str, str]:
    digests = {result.index_digest for result in results}
    freshness_values = {result.freshness.value for result in results}
    authoritative = (
        bool(results)
        and all(result.authoritative for result in results)
        and len(digests) == 1
        and freshness_values == {"current"}
    )
    digest = next(iter(digests)) if len(digests) == 1 else ""
    freshness = next(iter(freshness_values)) if len(freshness_values) == 1 else "mixed"
    return authoritative, digest, freshness


def _verified_snippet(
    context: ToolContext,
    relative: Path,
    *,
    line: int,
    line_window: int,
) -> tuple[str, str]:
    requested = relative.as_posix()
    path = _safe_path(context.workspace, requested)
    assert_path_not_sensitive(
        context.workspace,
        path,
        requested_path=requested,
    )
    _assert_workspace_path_allowed(
        context,
        path,
        requested_path=requested,
    )
    with _open_workspace_regular_file(context, path) as (handle, metadata):
        if metadata.st_size > _MAX_CONTEXT_FILE_BYTES:
            return "", ""
        payload = handle.read(_MAX_CONTEXT_FILE_BYTES + 1)
    if len(payload) > _MAX_CONTEXT_FILE_BYTES:
        return "", ""
    observed_digest = hashlib.sha256(payload).hexdigest()
    text = payload.decode("utf-8")
    lines = text.splitlines()
    start = max(0, line - line_window - 1)
    end = min(len(lines), line + line_window)
    snippet = "\n".join(
        f"{position + 1}: {lines[position]}"
        for position in range(start, end)
    )
    return redact_text(snippet), observed_digest
