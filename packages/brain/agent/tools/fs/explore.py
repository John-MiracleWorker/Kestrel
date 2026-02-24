import difflib
import asyncio
import json
import logging
import os
import re
import subprocess
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from agent.types import RiskLevel, ToolDefinition
from agent.tools.project_context import (
    save_project_context,
    recall_project_context,
    list_known_projects,
    set_vector_store as _set_project_vs,
)
from .utils import *

TREE_CACHE_TTL_SECONDS = 60

TREE_CACHE_MAX_ENTRIES = 128

def _build_host_tree_sync(resolved: Path, depth: int) -> dict:
    """Build host tree using scandir for lower syscall overhead."""
    cache_key = (str(resolved), depth)
    now = time.time()
    cached = _tree_cache.get(cache_key)
    try:
        root_mtime_ns = resolved.stat().st_mtime_ns
    except OSError:
        root_mtime_ns = 0

    if cached and (now - cached["created_at"]) <= TREE_CACHE_TTL_SECONDS and cached["root_mtime_ns"] == root_mtime_ns:
        return {**cached["result"], "cache_hit": True}

    tree_lines: list[str] = []
    file_count = 0
    dir_count = 0
    max_entries = 500

    def _build_tree(directory: Path, prefix: str, current_depth: int):
        nonlocal file_count, dir_count

        if current_depth > depth or (file_count + dir_count) >= max_entries:
            return

        try:
            with os.scandir(directory) as scanner:
                entries = sorted(list(scanner), key=lambda x: (not x.is_dir(follow_symlinks=False), x.name.lower()))
        except (PermissionError, OSError):
            return

        visible = []
        for entry in entries:
            name = entry.name
            if name.startswith(".") and name not in (".env.example",):
                continue
            if name in TREE_SKIP_DIRS:
                continue
            entry_path = Path(entry.path)
            if _is_blocked_path(entry_path) is not None:
                continue
            visible.append(entry)

        for i, entry in enumerate(visible):
            if (file_count + dir_count) >= max_entries:
                tree_lines.append(f"{prefix}... (truncated)")
                return

            is_last = (i == len(visible) - 1)
            connector = "└── " if is_last else "├── "
            extension = "    " if is_last else "│   "

            entry_path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                dir_count += 1
                marker_hint = ""
                if current_depth <= 1:
                    sub_markers = []
                    for marker, tech in PROJECT_MARKERS.items():
                        if (entry_path / marker).exists():
                            sub_markers.append(tech)
                    marker_hint = f"  [{', '.join(sub_markers)}]" if sub_markers else ""
                tree_lines.append(f"{prefix}{connector}{entry.name}/{marker_hint}")
                _build_tree(entry_path, prefix + extension, current_depth + 1)
            else:
                file_count += 1
                try:
                    size = entry.stat(follow_symlinks=False).st_size
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
    summary = {
        "files": file_count,
        "directories": dir_count,
        "truncated": (file_count + dir_count) >= max_entries,
    }
    tree = "\n".join(tree_lines)
    _tree_cache[cache_key] = {
        "created_at": now,
        "root_mtime_ns": root_mtime_ns,
        "result": {
            "tree": tree,
            "summary": summary,
        },
    }
    while len(_tree_cache) > TREE_CACHE_MAX_ENTRIES:
        _tree_cache.popitem(last=False)
    return {
        "tree": tree,
        "summary": summary,
        "cache_hit": False,
    }

async def host_list(
    path: str = ".",
    recursive: bool = False,
    pattern: str = "",
    depth_limit: int = 4,
    workspace_id: str = "default",
) -> dict:
    """List files and directories in a host path."""
    try:
        mounts = _get_host_mounts()

        # Special case: list configured mounts
        if path in (".", "", "/"):
            return {
                "mounted_directories": mounts,
                "hint": "Use host_tree(path) instead of host_list for a full project overview with tech stack detection.",
            }

        resolved = _resolve_host_path(path, mounts)

        if not resolved.exists():
            return {"error": f"Directory not found: {path}"}

        if resolved.is_file():
            return {"error": f"Not a directory: {path}. Use host_read to read files."}

        entries = []
        max_entries = 200

        def _should_skip(item: Path) -> bool:
            name = item.name
            if name.startswith(".") and name not in (".", ".."):
                return True
            if name in TREE_SKIP_DIRS:
                return True
            return _is_blocked_path(item) is not None

        if recursive:
            # DFS with a list-stack (O(1) pop) and os.scandir (fewer syscalls than iterdir)
            stack = [(resolved, 0)]
            while stack and len(entries) < max_entries:
                current, level = stack.pop()
                try:
                    with os.scandir(current) as scanner:
                        scan_entries = sorted(
                            scanner,
                            key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()),
                        )
                except (PermissionError, OSError):
                    continue

                subdirs = []
                for entry in scan_entries:
                    if len(entries) >= max_entries:
                        break
                    item = Path(entry.path)
                    if _should_skip(item):
                        continue

                    rel = item.relative_to(resolved)
                    if pattern and not re.search(pattern, str(rel), re.IGNORECASE):
                        # Still descend into directories even if name doesn't match pattern
                        if entry.is_dir(follow_symlinks=False) and level < depth_limit:
                            subdirs.append((item, level + 1))
                        continue

                    entries.append(_host_file_info(item, resolved))
                    if entry.is_dir(follow_symlinks=False) and level < depth_limit:
                        subdirs.append((item, level + 1))

                # Push in reverse so leftmost dirs are processed first
                stack.extend(reversed(subdirs))
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
            "depth_limit": depth_limit,
            "_hint": "TIP: Use host_tree(path) for a full recursive tree with project context detection. Use host_find(pattern) to search for files.",
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

        search_root = resolved if resolved.is_dir() else resolved.parent
        file_re = re.compile(file_pattern, re.IGNORECASE) if file_pattern else None

        cmd = [
            "rg",
            "--json",
            "--line-number",
            "--ignore-case",
            "--fixed-strings",
            "--max-columns", "220",
            "--max-columns-preview",
            "-g", "!**/.git/**",
            "-g", "!**/node_modules/**",
            "-g", "!**/__pycache__/**",
            "-g", "!**/.venv/**",
            "-g", "!**/venv/**",
            query,
            ".",
        ]

        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(search_root),
            capture_output=True,
            text=True,
            timeout=8,
        )

        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr.strip() or f"ripgrep failed with exit code {proc.returncode}")

        results = []
        files_seen = set()
        for line in proc.stdout.splitlines():
            if len(results) >= max_results:
                break
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "match":
                continue

            data = payload.get("data", {})
            rel_path = data.get("path", {}).get("text")
            if not rel_path:
                continue

            full_path = (search_root / rel_path).resolve()
            if _is_blocked_path(full_path) is not None:
                continue

            if file_re and not file_re.search(full_path.name):
                continue

            line_number = data.get("line_number")
            content = (data.get("lines", {}).get("text") or "").strip()
            results.append({
                "file": _container_to_host_path(full_path),
                "line": line_number,
                "content": content[:200],
            })
            files_seen.add(str(full_path))

        return {
            "query": query,
            "path": _container_to_host_path(resolved),
            "results": results,
            "count": len(results),
            "files_searched": len(files_seen),
            "truncated": len(results) >= max_results,
            "backend": "ripgrep",
        }

    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired):
        # Fallback: Python scanner if ripgrep is unavailable or timed out.
        try:
            results = []
            files_searched = 0
            search_re = re.compile(re.escape(query), re.IGNORECASE)
            search_root = resolved if resolved.is_dir() else resolved.parent

            for root_dir, dirs, files in os.walk(search_root):
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith(".")
                    and d not in TREE_SKIP_DIRS
                    and _is_blocked_path(Path(root_dir) / d) is None
                ]

                for fname in files:
                    if len(results) >= max_results:
                        break

                    fpath = Path(root_dir) / fname
                    if _is_blocked_path(fpath) is not None:
                        continue
                    if file_pattern and not re.search(file_pattern, fname, re.IGNORECASE):
                        continue

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
                    for i, text_line in enumerate(content.split("\n"), 1):
                        if search_re.search(text_line):
                            results.append({
                                "file": _container_to_host_path(fpath),
                                "line": i,
                                "content": text_line.strip()[:200],
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
                "backend": "python-fallback",
            }
        except Exception as e:
            return {"error": f"Failed to search: {e}"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to search: {e}"}

async def host_tree(
    path: str,
    depth: int = 4,
    start_after: int = 0,
    max_tree_lines: int = 500,
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
                pkg = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
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
                for line in content.split("\n"):
                    if "name" in line and "=" in line and "name" not in project_context:
                        project_context["name"] = line.split("=", 1)[1].strip().strip("\"'")
                        break
            except Exception:
                pass

        tree_payload = await asyncio.to_thread(_build_host_tree_sync, resolved, depth)
        tree_lines = tree_payload["tree"].split("\n") if tree_payload["tree"] else []

        start_idx = max(0, start_after)
        end_idx = min(len(tree_lines), start_idx + max_tree_lines)
        tree_page = "\n".join(tree_lines[start_idx:end_idx])

        result = {
            "path": _container_to_host_path(resolved),
            "tree": tree_page,
            "summary": tree_payload["summary"],
            "cache_hit": tree_payload.get("cache_hit", False),
            "paging": {
                "start_after": start_idx,
                "returned_lines": max(0, end_idx - start_idx),
                "total_lines": len(tree_lines),
                "next_cursor": end_idx if end_idx < len(tree_lines) else None,
            },
        }

        if detected_tech:
            result["tech_stack"] = list(set(detected_tech))
        if project_context:
            result["project"] = project_context

        # Auto-save project context to memory (fire-and-forget)
        try:
            memo_result = {**result, "tree": "\n".join(tree_lines[:1200])}
            memo_id = await save_project_context(memo_result, workspace_id=workspace_id)
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

        # ── Fast path: fd (fast-find) ─────────────────────────────────
        # fd is 10-50x faster than os.walk for large repos.
        try:
            cmd = [
                "fd",
                "--absolute-path",
                "--no-ignore",          # consistent behavior regardless of .gitignore
                "--regex", pattern,
                "--max-results", str(max_results),
            ]
            if file_type == "file":
                cmd += ["--type", "f"]
            elif file_type == "directory":
                cmd += ["--type", "d"]
            # Exclude the same dirs that os.walk skips
            for skip in TREE_SKIP_DIRS:
                cmd += ["--exclude", skip]
            cmd += [".", str(resolved)]

            proc = await asyncio.to_thread(
                subprocess.run, cmd,
                capture_output=True, text=True, timeout=10,
            )

            if proc.returncode in (0, 1):
                results = []
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    fpath = Path(line)
                    if fpath.name.startswith("."):
                        continue
                    if _is_blocked_path(fpath) is not None:
                        continue
                    is_dir = fpath.is_dir()
                    entry = {
                        "name": fpath.name,
                        "path": _container_to_host_path(fpath),
                        "type": "directory" if is_dir else "file",
                    }
                    if not is_dir:
                        try:
                            entry["size_bytes"] = fpath.stat().st_size
                        except OSError:
                            pass
                    results.append(entry)

                return {
                    "pattern": pattern,
                    "path": _container_to_host_path(resolved),
                    "results": results,
                    "count": len(results),
                    "truncated": len(results) >= max_results,
                    "backend": "fd",
                }
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # fd not available — fall through to os.walk

        # ── Fallback: os.walk ─────────────────────────────────────────
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
            "backend": "os.walk",
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