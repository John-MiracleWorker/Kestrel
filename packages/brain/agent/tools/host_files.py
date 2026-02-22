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
from agent.tools.project_context import (
    save_project_context,
    recall_project_context,
    list_known_projects,
    set_vector_store as _set_project_vs,
)

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


# ── Project-Aware Tree ───────────────────────────────────────────────

# Files that indicate project type / tech stack
PROJECT_MARKERS = {
    "package.json": "Node.js",
    "tsconfig.json": "TypeScript",
    "Cargo.toml": "Rust",
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "requirements.txt": "Python",
    "go.mod": "Go",
    "Gemfile": "Ruby",
    "pom.xml": "Java/Maven",
    "build.gradle": "Java/Gradle",
    "CMakeLists.txt": "C/C++",
    "Makefile": "Make",
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "docker-compose.yaml": "Docker Compose",
    ".env": "Environment Config",
    ".gitignore": "Git",
}

# Directories to always skip in tree
TREE_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", "venv", ".venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", ".cache", "coverage", ".turbo",
    "target",  # Rust/Java
}


async def host_tree(
    path: str,
    depth: int = 4,
    workspace_id: str = "default",
) -> dict:
    """
    Return a full directory tree with project context detection.
    Replaces dozens of sequential host_list calls with one comprehensive view.
    """
    try:
        mounts = _get_host_mounts()
        resolved = _resolve_host_path(path, mounts)

        if not resolved.exists():
            return {"error": f"Path not found: {path}"}
        if not resolved.is_dir():
            return {"error": f"Not a directory: {path}. Use host_read for files."}

        # Detect project markers at root
        detected_tech = []
        for marker, tech in PROJECT_MARKERS.items():
            if (resolved / marker).exists():
                detected_tech.append(tech)

        # Read key project files for context
        project_context = {}
        pkg_json = resolved / "package.json"
        if pkg_json.exists():
            try:
                import json as _json
                pkg = _json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
                project_context["name"] = pkg.get("name", "")
                project_context["description"] = pkg.get("description", "")
                deps = list(pkg.get("dependencies", {}).keys())[:15]
                dev_deps = list(pkg.get("devDependencies", {}).keys())[:10]
                if deps:
                    project_context["dependencies"] = deps
                if dev_deps:
                    project_context["devDependencies"] = dev_deps
                scripts = list(pkg.get("scripts", {}).keys())
                if scripts:
                    project_context["scripts"] = scripts
            except Exception:
                pass

        pyproject = resolved / "pyproject.toml"
        if pyproject.exists() and not project_context:
            try:
                content = pyproject.read_text(encoding="utf-8", errors="replace")
                # Simple extraction without toml parser
                for line in content.split("\n"):
                    if "name" in line and "=" in line and "name" not in project_context:
                        project_context["name"] = line.split("=", 1)[1].strip().strip('"\'')
                        break
            except Exception:
                pass

        # Build tree
        tree_lines = []
        file_count = 0
        dir_count = 0
        max_entries = 500

        def _build_tree(directory: Path, prefix: str, current_depth: int):
            nonlocal file_count, dir_count

            if current_depth > depth or (file_count + dir_count) >= max_entries:
                return

            try:
                entries = sorted(directory.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except PermissionError:
                return

            # Filter entries
            visible = []
            for entry in entries:
                name = entry.name
                if name.startswith(".") and name not in (".env.example",):
                    continue
                if name in TREE_SKIP_DIRS:
                    continue
                if _is_blocked_path(entry) is not None:
                    continue
                visible.append(entry)

            for i, entry in enumerate(visible):
                if (file_count + dir_count) >= max_entries:
                    tree_lines.append(f"{prefix}... (truncated)")
                    return

                is_last = (i == len(visible) - 1)
                connector = "└── " if is_last else "├── "
                extension = "    " if is_last else "│   "

                if entry.is_dir():
                    dir_count += 1
                    # Check for project markers in subdirs
                    sub_markers = []
                    for marker, tech in PROJECT_MARKERS.items():
                        if (entry / marker).exists():
                            sub_markers.append(tech)
                    marker_hint = f"  [{', '.join(sub_markers)}]" if sub_markers else ""
                    tree_lines.append(f"{prefix}{connector}{entry.name}/{marker_hint}")
                    _build_tree(entry, prefix + extension, current_depth + 1)
                else:
                    file_count += 1
                    try:
                        size = entry.stat().st_size
                        if size > 1_000_000:
                            size_str = f" ({size / 1_000_000:.1f}MB)"
                        elif size > 1000:
                            size_str = f" ({size / 1000:.0f}KB)"
                        else:
                            size_str = ""
                    except OSError:
                        size_str = ""
                    tree_lines.append(f"{prefix}{connector}{entry.name}{size_str}")

        _build_tree(resolved, "", 0)

        result = {
            "path": _container_to_host_path(resolved),
            "tree": "\n".join(tree_lines),
            "summary": {
                "files": file_count,
                "directories": dir_count,
                "truncated": (file_count + dir_count) >= max_entries,
            },
        }

        if detected_tech:
            result["tech_stack"] = list(set(detected_tech))
        if project_context:
            result["project"] = project_context

        # Auto-save project context to memory (fire-and-forget)
        try:
            memo_id = await save_project_context(result, workspace_id=workspace_id)
            if memo_id:
                result["_memo"] = f"Project context saved to memory (id={memo_id})"
        except Exception as e:
            logger.debug(f"Failed to auto-memo project: {e}")

        return result

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to build tree: {e}"}


async def host_find(
    pattern: str,
    path: str = ".",
    file_type: str = "any",
    max_results: int = 50,
    workspace_id: str = "default",
) -> dict:
    """Find files by name pattern across the directory tree."""
    try:
        mounts = _get_host_mounts()
        resolved = _resolve_host_path(path, mounts)

        if not resolved.exists():
            return {"error": f"Path not found: {path}"}

        results = []
        search_re = re.compile(pattern, re.IGNORECASE)

        for root_dir, dirs, files in os.walk(resolved):
            # Skip ignored directories
            dirs[:] = [
                d for d in dirs
                if d not in TREE_SKIP_DIRS
                and not d.startswith(".")
                and _is_blocked_path(Path(root_dir) / d) is None
            ]

            items = []
            if file_type in ("any", "file"):
                items.extend((f, "file") for f in files)
            if file_type in ("any", "directory"):
                items.extend((d, "directory") for d in dirs)

            for name, item_type in items:
                if len(results) >= max_results:
                    break

                if search_re.search(name):
                    full_path = Path(root_dir) / name
                    if _is_blocked_path(full_path) is not None:
                        continue

                    entry = {
                        "name": name,
                        "path": _container_to_host_path(full_path),
                        "type": item_type,
                    }
                    if item_type == "file":
                        try:
                            entry["size_bytes"] = full_path.stat().st_size
                        except OSError:
                            pass
                    results.append(entry)

            if len(results) >= max_results:
                break

        return {
            "pattern": pattern,
            "path": _container_to_host_path(resolved),
            "results": results,
            "count": len(results),
            "truncated": len(results) >= max_results,
        }

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to find files: {e}"}


async def project_recall(
    project_name: str,
    workspace_id: str = "default",
) -> dict:
    """Recall saved context for a project by name."""
    try:
        # Try exact recall first
        context = await recall_project_context(project_name, workspace_id)
        if context:
            return {
                "found": True,
                **context,
            }

        # List known projects as suggestions
        known = await list_known_projects(workspace_id)
        if known:
            return {
                "found": False,
                "message": f"No saved context for '{project_name}'.",
                "known_projects": known,
                "hint": "Use host_tree to scan the project first, or try one of the known project names.",
            }

        return {
            "found": False,
            "message": "No projects have been scanned yet. Use host_tree to scan a project directory first.",
        }

    except Exception as e:
        return {"error": str(e)}


def register_host_file_tools(registry, vector_store=None) -> None:
    """Register host filesystem tools."""
    # Inject vector store for project context memoization
    if vector_store:
        _set_project_vs(vector_store)

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

    registry.register(
        definition=ToolDefinition(
            name="host_tree",
            description=(
                "Get a full directory tree of a path on the user's host filesystem. "
                "Returns a visual tree with file sizes and automatically detects project type "
                "(Node.js, Python, Rust, etc.) by scanning for markers like package.json, "
                "pyproject.toml, Cargo.toml. Also extracts project metadata (name, dependencies, scripts). "
                "USE THIS FIRST when exploring a codebase — it replaces many sequential host_list calls."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to tree (absolute or relative to first mount)",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum depth to traverse (default 4, max 8)",
                        "default": 4,
                    },
                },
                "required": ["path"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=30,
            category="host_file",
        ),
        handler=host_tree,
    )

    registry.register(
        definition=ToolDefinition(
            name="host_find",
            description=(
                "Find files by name pattern on the user's host filesystem. "
                "Like the 'fd' or 'find' command — searches recursively for files/directories "
                "matching a regex pattern. Useful for locating specific files across a project."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to match against file/directory names (e.g. '\\.py$' or 'test')",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: first mounted directory)",
                        "default": ".",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Filter by type: 'file', 'directory', or 'any' (default)",
                        "default": "any",
                        "enum": ["file", "directory", "any"],
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["pattern"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=30,
            category="host_file",
        ),
        handler=host_find,
    )

    registry.register(
        definition=ToolDefinition(
            name="project_recall",
            description=(
                "Recall saved context for a previously scanned project. "
                "Returns the project's directory structure, tech stack, dependencies, "
                "and other metadata from a previous host_tree scan. "
                "If no match is found, lists all known projects. "
                "USE THIS BEFORE host_tree to check if the project was already scanned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_name": {
                        "type": "string",
                        "description": "Project name or slug (e.g. 'kestrel', 'my-react-app', 'little bird alt')",
                    },
                },
                "required": ["project_name"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=10,
            category="memory",
        ),
        handler=project_recall,
    )
