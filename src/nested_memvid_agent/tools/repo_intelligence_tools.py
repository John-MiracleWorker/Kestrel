from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
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
            result = index.symbols(query, limit=limit, offset=offset)
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
            result = index.references(name, limit=limit, offset=offset)
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
            result = index.imports(query, limit=limit, offset=offset)
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
            result = index.tests_for(symbol, limit=limit, offset=offset)
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
            definitions = index.symbols(symbol, limit=limit)
            references = index.references(symbol, limit=limit)
            tests = index.tests_for(symbol, limit=limit)
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
            symbols = index.symbols(query, limit=_MAX_CONTEXT_EVIDENCE)
            references = index.references(query, limit=_MAX_CONTEXT_EVIDENCE)
            authoritative, digest, freshness = _combined_query_authority(
                symbols,
                references,
            )
            candidates: list[tuple[Path, int, str, str]] = []
            if authoritative:
                candidates.extend(
                    (record.path, record.line, record.file_digest, f"definition:{record.name}")
                    for record in symbols.records
                    if _record_path_allowed(context, record.path)
                )
                candidates.extend(
                    (record.path, record.line, record.file_digest, f"reference:{record.name}")
                    for record in references.records
                    if _record_path_allowed(context, record.path)
                )
            evidence: list[dict[str, Any]] = []
            blocks: list[str] = []
            remaining = max_chars
            seen: set[tuple[str, int]] = set()
            read_drift = False
            for path, line, file_digest, relation in candidates:
                identity = (path.as_posix(), line)
                if identity in seen or len(evidence) >= _MAX_CONTEXT_EVIDENCE:
                    continue
                seen.add(identity)
                snippet, observed_digest = _verified_snippet(
                    context,
                    path,
                    line=line,
                    line_window=line_window,
                )
                if observed_digest != file_digest:
                    read_drift = True
                    continue
                block = (
                    f"### {path.as_posix()}:{line} [{relation}]\n"
                    f"file_sha256={file_digest}\n{snippet}"
                )
                if len(block) > remaining:
                    break
                blocks.append(block)
                remaining -= len(block)
                evidence.append(
                    {
                        "path": path.as_posix(),
                        "line": line,
                        "relation": relation,
                        "file_digest": file_digest,
                    }
                )
            if read_drift:
                authoritative = False
                evidence = []
                blocks = []
            payload = {
                "query": query,
                "context": "\n\n".join(blocks),
                "evidence": evidence,
                "freshness": freshness,
                "authoritative": authoritative,
                "index_digest": digest,
                "truncated": (
                    symbols.truncated
                    or references.truncated
                    or len(evidence) < len(seen)
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
    try:
        return RepositoryIndex(
            project_id=project_id,
            repository_root=root,
            create=False,
        )
    except RepositoryIndexError as exc:
        raise _RepoToolFailure(
            "repo_index_rebuild_required",
            "The repository index requires an explicit rebuild before it can be queried.",
        ) from exc


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
