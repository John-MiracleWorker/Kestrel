"""
Host filesystem tools — read/write/search the user's actual filesystem.

Unlike the workspace file tools (files.py) which operate in an isolated
sandbox directory, these tools access user-configured directories on the
real host filesystem.

Security model:
  - Read/list/search → LOW risk, auto-approved
  - Write/edit       → HIGH risk, requires human approval (shows diff)
  - Path traversal   → blocked (can't escape configured mount roots)
  - Sensitive dirs   → blocklist (e.g. ~/.ssh, ~/.gnupg)
"""

import difflib
import logging
import os
import re
from pathlib import Path
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.host_files")


# ── Sensitive Path Blocklist ─────────────────────────────────────────
# These paths are NEVER accessible, even if a parent is mounted.

BLOCKED_PATHS = [
    ".ssh",
    ".gnupg",
    ".gpg",
    ".aws",
    ".azure",
    ".gcloud",
    ".config/gcloud",
    ".kube",
    ".docker",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".env",
    ".env.local",
    ".env.production",
    "node_modules",
]

BLOCKED_EXTENSIONS = {
    ".pem", ".key", ".p12", ".pfx", ".jks",
    ".keystore", ".cert", ".crt",
}


def _get_host_mounts() -> list[str]:
    """Get configured host mount paths from environment."""
    raw = os.getenv("AGENT_HOST_MOUNTS", "")
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


# The host filesystem is mounted into the container at this path.
# e.g., /Users on the host → /host_fs in the container.
_HOST_MOUNT_ROOT = os.getenv("HOST_MOUNT_ROOT", "/Users")
_CONTAINER_MOUNT_POINT = "/host_fs"


def _host_to_container_path(host_path: str) -> Path:
    """Translate a host-absolute path to the container-internal path.

    Example: /Users/tiuni/projects → /host_fs/tiuni/projects
    """
    p = Path(host_path)
    try:
        relative = p.relative_to(_HOST_MOUNT_ROOT)
        return Path(_CONTAINER_MOUNT_POINT) / relative
    except ValueError:
        # Path is not under the mount root — return as-is
        # (will fail later if it doesn't exist)
        return p


def _container_to_host_path(container_path: Path) -> str:
    """Translate a container-internal path back to host-absolute for display.

    Example: /host_fs/tiuni/projects → /Users/tiuni/projects
    """
    try:
        relative = container_path.relative_to(_CONTAINER_MOUNT_POINT)
        return str(Path(_HOST_MOUNT_ROOT) / relative)
    except ValueError:
        return str(container_path)


def _is_blocked_path(path: Path) -> Optional[str]:
    """Check if a path matches the sensitive blocklist."""
    path_str = str(path)
    path_parts = path.parts

    for blocked in BLOCKED_PATHS:
        if blocked in path_parts or f"/{blocked}" in path_str:
            return f"Access denied: '{blocked}' is a sensitive directory"

    if path.suffix.lower() in BLOCKED_EXTENSIONS:
        return f"Access denied: '{path.suffix}' files contain sensitive data"

    return None


def _resolve_host_path(path: str, mounts: list[str]) -> Path:
    """
    Resolve a path against configured mount roots.
    The path can be:
      - Absolute host path (must be under a mount root)
      - Relative (resolved against the first mount root)

    Raises ValueError if the path escapes all mount roots.
    Returns the container-internal path (under /host_fs).
    """
    if not mounts:
        raise ValueError(
            "No host directories are configured. "
            "Set AGENT_HOST_MOUNTS in .env or configure via Settings → Filesystem Access."
        )

    target = Path(path).expanduser()

    # If relative, resolve against first mount
    if not target.is_absolute():
        target = Path(mounts[0]) / path

    # Check that the path is under at least one mount root
    resolved_host = target.resolve()
    for mount in mounts:
        mount_resolved = Path(mount).resolve()
        try:
            resolved_host.relative_to(mount_resolved)

            # Check blocklist
            blocked = _is_blocked_path(resolved_host)
            if blocked:
                raise ValueError(blocked)

            # Translate to container path
            container_path = _host_to_container_path(str(resolved_host))
            return container_path.resolve()
        except ValueError as e:
            if "Access denied" in str(e):
                raise
            continue  # Not under this mount, try next

    raise ValueError(
        f"Path '{path}' is outside configured directories. "
        f"Accessible directories: {', '.join(mounts)}"
    )


# ── Tool Handlers ────────────────────────────────────────────────────


async def host_read(
    path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    max_lines: int = 300,
    workspace_id: str = "default",
) -> dict:
    """Read a file from the user's host filesystem."""
    try:
        mounts = _get_host_mounts()
        resolved = _resolve_host_path(path, mounts)

        if not resolved.exists():
            return {"error": f"File not found: {path}"}

        if not resolved.is_file():
            return {"error": f"Not a file: {path}"}

        size = resolved.stat().st_size
        if size > 2_000_000:  # 2MB
            return {
                "error": f"File too large ({size:,} bytes). "
                         "Consider reading specific line ranges with start_line/end_line.",
            }

        content = resolved.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        total_lines = len(lines)

        # Apply line range
        start_idx = max(0, start_line - 1)
        if end_line:
            end_idx = min(end_line, total_lines)
        else:
            end_idx = min(start_idx + max_lines, total_lines)

        selected_lines = lines[start_idx:end_idx]
        content = "\n".join(selected_lines)

        return {
            "path": _container_to_host_path(resolved),
            "content": content,
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "total_lines": total_lines,
            "size_bytes": size,
        }

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to read file: {e}"}


async def host_list(
    path: str = ".",
    recursive: bool = False,
    pattern: str = "",
    workspace_id: str = "default",
) -> dict:
    """List files and directories in a host path."""
    try:
        mounts = _get_host_mounts()

        # Special case: list configured mounts
        if path in (".", "", "/"):
            return {
                "mounted_directories": mounts,
                "hint": "Use a specific path within these directories, e.g. host_list(path='/Users/john/projects')",
            }

        resolved = _resolve_host_path(path, mounts)

        if not resolved.exists():
            return {"error": f"Directory not found: {path}"}

        if not resolved.is_file():
            pass  # It's a directory, proceed
        else:
            return {"error": f"Not a directory: {path}. Use host_read to read files."}

        entries = []
        max_entries = 200

        def _should_skip(item: Path) -> bool:
            """Skip hidden dirs, node_modules, etc."""
            name = item.name
            if name.startswith(".") and name not in (".", ".."):
                return True
            if name in ("node_modules", "__pycache__", ".git", "venv", ".venv"):
                return True
            return _is_blocked_path(item) is not None

        if recursive:
            for item in sorted(resolved.rglob("*")):
                if len(entries) >= max_entries:
                    break
                rel = item.relative_to(resolved)
                if len(rel.parts) > 4:
                    continue
                if any(_should_skip(Path(p)) for p in rel.parts):
                    continue
                if pattern and not re.search(pattern, str(rel), re.IGNORECASE):
                    continue
                entries.append(_host_file_info(item, resolved))
        else:
            for item in sorted(resolved.iterdir()):
                if len(entries) >= max_entries:
                    break
                if _should_skip(item):
                    continue
                if pattern and not re.search(pattern, item.name, re.IGNORECASE):
                    continue
                entries.append(_host_file_info(item, resolved))

        return {
            "path": _container_to_host_path(resolved),
            "entries": entries,
            "count": len(entries),
            "truncated": len(entries) >= max_entries,
        }

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to list directory: {e}"}


async def host_search(
    query: str,
    path: str = ".",
    file_pattern: str = "",
    max_results: int = 30,
    workspace_id: str = "default",
) -> dict:
    """Search for text within files on the host filesystem."""
    try:
        mounts = _get_host_mounts()
        resolved = _resolve_host_path(path, mounts)

        if not resolved.exists():
            return {"error": f"Path not found: {path}"}

        results = []
        files_searched = 0
        search_re = re.compile(re.escape(query), re.IGNORECASE)

        # Walk the directory
        search_root = resolved if resolved.is_dir() else resolved.parent

        for root_dir, dirs, files in os.walk(search_root):
            # Skip hidden/ignored directories
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in ("node_modules", "__pycache__", "venv", ".venv", ".git")
                and _is_blocked_path(Path(root_dir) / d) is None
            ]

            for fname in files:
                if len(results) >= max_results:
                    break

                fpath = Path(root_dir) / fname

                # Skip blocked
                if _is_blocked_path(fpath) is not None:
                    continue

                # File pattern filter
                if file_pattern and not re.search(file_pattern, fname, re.IGNORECASE):
                    continue

                # Skip binary/large files
                try:
                    size = fpath.stat().st_size
                    if size > 500_000:
                        continue
                except OSError:
                    continue

                try:
                    content = fpath.read_text(encoding="utf-8", errors="strict")
                except (UnicodeDecodeError, OSError):
                    continue

                files_searched += 1

                for i, line in enumerate(content.split("\n"), 1):
                    if search_re.search(line):
                        results.append({
                            "file": _container_to_host_path(fpath),
                            "line": i,
                            "content": line.strip()[:200],
                        })
                        if len(results) >= max_results:
                            break

        return {
            "query": query,
            "path": _container_to_host_path(resolved),
            "results": results,
            "count": len(results),
            "files_searched": files_searched,
            "truncated": len(results) >= max_results,
        }

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to search: {e}"}


async def host_write(
    path: str,
    content: str,
    create_dirs: bool = False,
    workspace_id: str = "default",
) -> dict:
    """
    Write content to a file on the host filesystem.
    This is a HIGH-risk operation — the agent loop will require
    human approval before executing this tool.
    """
    try:
        mounts = _get_host_mounts()
        resolved = _resolve_host_path(path, mounts)

        # Generate diff if file already exists
        diff_preview = ""
        if resolved.exists() and resolved.is_file():
            try:
                old_content = resolved.read_text(encoding="utf-8", errors="replace")
                diff_lines = list(difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{resolved.name}",
                    tofile=f"b/{resolved.name}",
                    lineterm="",
                ))
                diff_preview = "\n".join(diff_lines[:100])
                if len(diff_lines) > 100:
                    diff_preview += f"\n... ({len(diff_lines) - 100} more diff lines)"
            except Exception:
                diff_preview = "(could not generate diff)"

        # Size check
        if len(content.encode("utf-8")) > 500_000:
            return {"error": "Content too large (max 500KB for host writes)"}

        # Create parent dirs if requested
        if create_dirs:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        elif not resolved.parent.exists():
            return {
                "error": f"Parent directory does not exist: {resolved.parent}. "
                         "Set create_dirs=true to create it."
            }

        resolved.write_text(content, encoding="utf-8")
        size = resolved.stat().st_size

        result = {
            "path": _container_to_host_path(resolved),
            "action": "written",
            "size_bytes": size,
            "success": True,
        }
        if diff_preview:
            result["diff_preview"] = diff_preview

        return result

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to write file: {e}"}


# ── Helpers ──────────────────────────────────────────────────────────


def _host_file_info(item: Path, base: Path) -> dict:
    """Build file info dict for a host file."""
    rel_path = str(item.relative_to(base)).replace("\\", "/")
    info = {
        "name": rel_path,
        "type": "directory" if item.is_dir() else "file",
    }
    if item.is_file():
        try:
            stat = item.stat()
            info["size_bytes"] = stat.st_size
            info["extension"] = item.suffix
        except OSError:
            pass
    return info


# ── Registration ─────────────────────────────────────────────────────


def register_host_file_tools(registry) -> None:
    """Register host filesystem tools."""

    registry.register(
        definition=ToolDefinition(
            name="host_read",
            description=(
                "Read a file from the user's host filesystem. "
                "Operates on directories the user has configured for access. "
                "Use for reading source code, configs, documents, or data files "
                "from the user's actual machine."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute file path or path relative to first mounted directory",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start reading from this line (1-indexed, default 1)",
                        "default": 1,
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Stop reading at this line (inclusive). Omit to read max_lines from start.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum lines to read (default 300)",
                        "default": 300,
                    },
                },
                "required": ["path"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=15,
            category="host_file",
        ),
        handler=host_read,
    )

    registry.register(
        definition=ToolDefinition(
            name="host_list",
            description=(
                "List files and directories on the user's host filesystem. "
                "Shows file names, sizes, and types. Can filter by pattern. "
                "Use path='.' to see which directories are mounted/accessible."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (absolute or relative to first mount). Use '.' to list mounts.",
                        "default": ".",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, list recursively (max depth 4)",
                        "default": False,
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to filter results (e.g. '\\.py$' for Python files)",
                    },
                },
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=15,
            category="host_file",
        ),
        handler=host_list,
    )

    registry.register(
        definition=ToolDefinition(
            name="host_search",
            description=(
                "Search for text within files on the user's host filesystem. "
                "Performs a case-insensitive grep across files in the given path. "
                "Returns matching lines with file paths and line numbers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text to search for (case-insensitive)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: first mounted directory)",
                        "default": ".",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Regex to filter filenames (e.g. '\\.ts$' for TypeScript)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return (default 30)",
                        "default": 30,
                    },
                },
                "required": ["query"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=30,
            category="host_file",
        ),
        handler=host_search,
    )

    registry.register(
        definition=ToolDefinition(
            name="host_write",
            description=(
                "Write content to a file on the user's host filesystem. "
                "⚠️ This is a HIGH-RISK operation — it will modify files on the "
                "user's actual machine and requires human approval before execution. "
                "A diff preview is shown to the user for review."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute file path or path relative to first mounted directory",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to write to the file",
                    },
                    "create_dirs": {
                        "type": "boolean",
                        "description": "Create parent directories if they don't exist",
                        "default": False,
                    },
                },
                "required": ["path", "content"],
            },
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            timeout_seconds=15,
            category="host_file",
        ),
        handler=host_write,
    )
