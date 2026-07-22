from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..security_boundary import assert_path_not_sensitive, redact_text
from .base import AgentTool, ToolContext


class ListFilesTool(AgentTool):
    spec = ToolSpec(
        name="file.list",
        description="List files under the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            path = _safe_path(context.workspace, str(arguments.get("path", ".")))
            _assert_workspace_path_allowed(context, path)
            max_entries = int(arguments.get("max_entries", 80))
            entries = [
                entry
                for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
                if not _workspace_path_is_private(context, entry)
            ][:max_entries]
            data = [{"name": p.name, "type": "dir" if p.is_dir() else "file"} for p in entries]
            return self._result(call, success=True, content=json.dumps(data, indent=2), data={"entries": data})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_list_failed")


class ReadFileTool(AgentTool):
    spec = ToolSpec(
        name="file.read",
        description="Read a text file under the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}},
            "required": ["path"],
        },
        aliases=("read",),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            requested_path = str(arguments.get("path", ""))
            path = _safe_path(context.workspace, requested_path)
            assert_path_not_sensitive(
                context.workspace,
                path,
                requested_path=requested_path,
            )
            _assert_workspace_path_allowed(
                context,
                path,
                requested_path=requested_path,
            )
            max_chars = max(1, min(int(arguments.get("max_chars", 20_000)), 200_000))
            with _open_workspace_regular_file(context, path) as (handle, _):
                text = redact_text(handle.read(max_chars * 4).decode("utf-8", errors="replace")[:max_chars])
            return self._result(call, success=True, content=text, data={"path": str(path), "chars": len(text)})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_read_failed")


class FindFilesTool(AgentTool):
    spec = ToolSpec(
        name="file.find",
        description="Find files or directories under the workspace using a bounded glob pattern.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "type": {"type": "string", "enum": ["any", "file", "dir"]},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["pattern"],
        },
        aliases=("find",),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        pattern = str(arguments.get("pattern", "")).strip()
        if not pattern:
            return self._result(call, success=False, content="Missing pattern", error="missing_pattern")
        try:
            root = _safe_path(context.workspace, str(arguments.get("path", ".")))
            _assert_workspace_path_allowed(context, root)
            workspace = context.workspace.resolve()
            max_results = max(1, min(int(arguments.get("max_results", 100)), 500))
            kind = str(arguments.get("type", "file"))
            if kind not in {"any", "file", "dir"}:
                return self._result(call, success=False, content=f"Unknown type: {kind}", error="bad_type")
            rows: list[dict[str, str]] = []
            for path in sorted(root.rglob(pattern), key=lambda item: item.as_posix()):
                if len(rows) >= max_results:
                    break
                if any(_skip_repo_name(part) for part in path.relative_to(workspace).parts):
                    continue
                if _workspace_path_is_private(context, path):
                    continue
                if kind == "file" and not path.is_file():
                    continue
                if kind == "dir" and not path.is_dir():
                    continue
                rows.append(
                    {
                        "path": path.relative_to(workspace).as_posix(),
                        "type": "dir" if path.is_dir() else "file",
                    }
                )
            return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"matches": rows})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_find_failed")


class FileStatTool(AgentTool):
    spec = ToolSpec(
        name="file.stat",
        description="Return bounded metadata for a workspace file or directory without reading full content.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "hash": {"type": "boolean"},
                "max_hash_bytes": {"type": "integer", "minimum": 1, "maximum": 10000000},
            },
            "required": ["path"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            requested_path = str(arguments.get("path", ""))
            path = _safe_path(context.workspace, requested_path)
            assert_path_not_sensitive(
                context.workspace,
                path,
                requested_path=requested_path,
            )
            _assert_workspace_path_allowed(
                context,
                path,
                requested_path=requested_path,
            )
            if not path.exists():
                return self._result(call, success=False, content=f"Path not found: {arguments.get('path', '')}", error="not_found")
            path_stat = path.lstat()
            raw: bytes | None = None
            if stat.S_ISREG(path_stat.st_mode):
                with _open_workspace_regular_file(context, path) as (handle, opened_stat):
                    path_stat = opened_stat
                    if bool(arguments.get("hash", False)):
                        max_hash_bytes = max(
                            1,
                            min(int(arguments.get("max_hash_bytes", 1_000_000)), 10_000_000),
                        )
                        raw = handle.read(max_hash_bytes)
            rel = path.relative_to(context.workspace.resolve()).as_posix()
            payload: dict[str, Any] = {
                "path": rel,
                "type": "dir" if stat.S_ISDIR(path_stat.st_mode) else "file",
                "bytes": path_stat.st_size,
                "modified_at": path_stat.st_mtime,
            }
            if raw is not None:
                payload["sha256"] = hashlib.sha256(raw).hexdigest()
                payload["hash_truncated"] = path_stat.st_size > len(raw)
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_stat_failed")


class WriteFileTool(AgentTool):
    spec = ToolSpec(
        name="file.write",
        description="Write a text file under workspace. Disabled unless allow_file_write is true.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            requested_path = str(arguments.get("path", ""))
            path = _safe_path(context.workspace, requested_path)
            _assert_workspace_path_allowed(
                context,
                path,
                requested_path=requested_path,
            )
            text = str(arguments.get("content", ""))
            _atomic_workspace_write(context.workspace, path, text)
            return self._result(call, success=True, content=f"Wrote {path}", data={"path": str(path), "chars": len(text)})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_write_failed")


class RepoSearchTool(AgentTool):
    spec = ToolSpec(
        name="repo.search",
        description="Search text files under the configured workspace root without leaving the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_file_bytes": {"type": "integer", "minimum": 256, "maximum": 1000000},
            },
            "required": ["query"],
        },
        aliases=("search",),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            root = _safe_path(context.workspace, str(arguments.get("path", ".")))
            _assert_workspace_path_allowed(context, root)
            max_results = int(arguments.get("max_results", 25))
            max_file_bytes = int(arguments.get("max_file_bytes", 300_000))
            rows: list[dict[str, object]] = []
            query_lower = query.lower()
            for path in _iter_repo_files(root, context, max_file_bytes=max_file_bytes):
                rel = path.relative_to(context.workspace.resolve())
                try:
                    text = _read_workspace_file(context, path, max_file_bytes=max_file_bytes)
                    if text is None:
                        continue
                    for lineno, line in enumerate(text.splitlines(), start=1):
                        if query_lower in line.lower():
                            rows.append({"path": rel.as_posix(), "line": lineno, "text": line[:400]})
                            if len(rows) >= max_results:
                                return self._result(
                                    call,
                                    success=True,
                                    content=json.dumps(rows, indent=2),
                                    data={"matches": rows},
                                )
                except UnicodeDecodeError:
                    continue
            return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"matches": rows})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repo_search_failed")


class RepoMapTool(AgentTool):
    spec = ToolSpec(
        name="repo.map",
        description="Return a bounded file tree for the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 500},
                "max_depth": {"type": "integer", "minimum": 0, "maximum": 8},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            root = _safe_path(context.workspace, str(arguments.get("path", ".")))
            _assert_workspace_path_allowed(context, root)
            workspace = context.workspace.resolve()
            max_entries = int(arguments.get("max_entries", 120))
            max_depth = int(arguments.get("max_depth", 3))
            rows: list[dict[str, object]] = []
            for current_root, dirs, files in os.walk(root):
                current = Path(current_root)
                rel_current = current.relative_to(workspace)
                depth = 0 if str(rel_current) == "." else len(rel_current.parts)
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not _skip_repo_name(d)
                    and not (current / d).is_symlink()
                    and not _workspace_path_is_private(context, current / d)
                )
                if depth > max_depth:
                    dirs[:] = []
                    continue
                for dirname in dirs:
                    rows.append(
                        {
                            "path": (current / dirname).relative_to(workspace).as_posix(),
                            "type": "dir",
                        }
                    )
                    if len(rows) >= max_entries:
                        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
                for filename in sorted(files):
                    if _skip_repo_name(filename):
                        continue
                    item = current / filename
                    if _workspace_path_is_private(context, item):
                        continue
                    item_stat = item.lstat()
                    if not stat.S_ISREG(item_stat.st_mode):
                        continue
                    rows.append(
                        {
                            "path": item.relative_to(workspace).as_posix(),
                            "type": "file",
                            "bytes": item_stat.st_size,
                        }
                    )
                    if len(rows) >= max_entries:
                        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
            return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repo_map_failed")


def _safe_path(root: Path, relative: str) -> Path:
    root_resolved = root.resolve()
    path = (root_resolved / relative).resolve()
    if root_resolved not in path.parents and path != root_resolved:
        raise ValueError(f"Path escapes workspace: {relative}")
    return path


def _configured_secret_store_path(context: ToolContext) -> Path:
    configured = Path(context.config.secret_store_path).expanduser()
    if not configured.is_absolute():
        configured = context.workspace.resolve() / configured
    return configured.resolve()


def _workspace_path_is_private(context: ToolContext, path: Path) -> bool:
    try:
        assert_path_not_sensitive(context.workspace, path)
        resolved = path.resolve()
        secret_store = _configured_secret_store_path(context)
    except (OSError, RuntimeError, ValueError):
        return True
    if resolved == secret_store:
        return True
    if secret_store.exists() and secret_store.is_dir() and secret_store in resolved.parents:
        return True
    if resolved.parent == secret_store.parent:
        lock_name = f".{secret_store.name}.lock"
        temporary_prefix = f".{secret_store.name}."
        if resolved.name == lock_name or (
            resolved.name.startswith(temporary_prefix) and resolved.name.endswith(".tmp")
        ):
            return True
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return any(_same_file_if_present(path_stat, candidate) for candidate in _secret_store_artifacts(context))


def _assert_workspace_path_allowed(
    context: ToolContext,
    path: Path,
    *,
    requested_path: str | None = None,
) -> None:
    assert_path_not_sensitive(
        context.workspace,
        path,
        requested_path=requested_path,
    )
    if _workspace_path_is_private(context, path):
        raise ValueError("Access to the configured secret store is not allowed.")


def _assert_opened_file_is_not_secret_store(
    context: ToolContext,
    opened_stat: os.stat_result,
) -> None:
    for candidate in _secret_store_artifacts(context):
        if _same_file_if_present(opened_stat, candidate):
            raise ValueError("Access to the configured secret store is not allowed.")


def _secret_store_artifacts(context: ToolContext) -> tuple[Path, ...]:
    secret_store = _configured_secret_store_path(context)
    candidates = [
        secret_store,
        secret_store.with_name(f".{secret_store.name}.lock"),
    ]
    try:
        candidates.extend(secret_store.parent.glob(f".{secret_store.name}.*.tmp"))
    except OSError:
        pass
    return tuple(candidates)


def _same_file_if_present(path_stat: os.stat_result, candidate: Path) -> bool:
    try:
        candidate_stat = os.stat(candidate, follow_symlinks=False)
    except OSError:
        return False
    return os.path.samestat(path_stat, candidate_stat)


@contextmanager
def _open_workspace_regular_file(
    context: ToolContext,
    path: Path,
) -> Iterator[tuple[IO[bytes], os.stat_result]]:
    _assert_workspace_path_allowed(context, path)
    root = context.workspace.resolve()
    relative = path.relative_to(root)
    if not relative.parts or relative == Path("."):
        raise ValueError("A file path is required.")
    if sys.platform == "win32":
        current = root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise ValueError("Workspace read path contains a symbolic link.")
        with current.open("rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise ValueError("Workspace read target must be a regular file.")
            _assert_opened_file_is_not_secret_store(context, opened_stat)
            yield handle, opened_stat
        return

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(root, directory_flags)
    file_fd = -1
    try:
        for component in relative.parts[:-1]:
            child_fd = os.open(component, directory_flags | nofollow, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        file_fd = os.open(
            relative.name,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        opened_stat = os.fstat(file_fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError("Workspace read target must be a regular file.")
        _assert_opened_file_is_not_secret_store(context, opened_stat)
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = -1
            yield handle, opened_stat
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def _atomic_workspace_write(workspace: Path, path: Path, text: str) -> None:
    root = workspace.resolve()
    relative = path.relative_to(root)
    if not relative.name or relative == Path("."):
        raise ValueError("A file path is required.")
    if sys.platform == "win32":
        _atomic_workspace_write_portable(root, relative, text)
        return

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(root, directory_flags)
    temporary_name = f".kestrel-write-{uuid4().hex}"
    try:
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(component, directory_flags | nofollow, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        mode = 0o600
        try:
            existing = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError("Refusing to replace a non-regular workspace path.")
            mode = existing.st_mode & 0o777
        except FileNotFoundError:
            pass
        file_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            mode,
            dir_fd=parent_fd,
        )
        try:
            with os.fdopen(file_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(file_fd)
            except OSError:
                pass
            raise
        os.replace(
            temporary_name,
            relative.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _atomic_workspace_write_portable(root: Path, relative: Path, text: str) -> None:
    parent = root
    for component in relative.parts[:-1]:
        parent /= component
        if parent.is_symlink():
            raise ValueError("Workspace write path contains a symbolic link.")
        parent.mkdir(mode=0o700, exist_ok=True)
    target = parent / relative.name
    temporary = parent / f".kestrel-write-{uuid4().hex}"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _skip_repo_name(name: str) -> bool:
    return name in {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"} or name.startswith(".")


def _read_workspace_file(
    context: ToolContext,
    path: Path,
    *,
    max_file_bytes: int,
) -> str | None:
    try:
        with _open_workspace_regular_file(context, path) as (handle, opened_stat):
            if opened_stat.st_size > max_file_bytes:
                return None
            raw = handle.read(max_file_bytes + 1)
        return redact_text(raw.decode("utf-8", errors="replace"))
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return None


def _iter_repo_files(
    root: Path,
    context: ToolContext,
    *,
    max_file_bytes: int,
) -> list[Path]:
    workspace_root = context.workspace.resolve()
    files: list[Path] = []
    root_stat = root.lstat()
    if stat.S_ISREG(root_stat.st_mode):
        if root_stat.st_size <= max_file_bytes and not _workspace_path_is_private(context, root):
            return [root]
        return []
    if not stat.S_ISDIR(root_stat.st_mode):
        return []
    for current_root, dirs, filenames in os.walk(root):
        current = Path(current_root)
        dirs[:] = sorted(
            dirname
            for dirname in dirs
            if not _skip_repo_name(dirname)
            and not (current / dirname).is_symlink()
            and not _workspace_path_is_private(context, current / dirname)
        )
        for filename in sorted(filenames):
            if _skip_repo_name(filename):
                continue
            path = current / filename
            try:
                path.relative_to(workspace_root)
            except ValueError:
                continue
            path_stat = path.lstat()
            if (
                stat.S_ISREG(path_stat.st_mode)
                and path_stat.st_size <= max_file_bytes
                and not _workspace_path_is_private(context, path)
            ):
                files.append(path)
    return files
