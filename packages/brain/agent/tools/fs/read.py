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

READ_CACHE_MAX_ENTRIES = 256

def _read_text_cached(path: Path) -> tuple[str, os.stat_result, bool]:
    """Read UTF-8 text with a small LRU cache keyed by path+mtime+size."""
    stat = path.stat()
    key = str(path)
    cached = _read_cache.get(key)
    if cached and cached["mtime_ns"] == stat.st_mtime_ns and cached["size"] == stat.st_size:
        _read_cache.move_to_end(key)
        return cached["content"], stat, True

    content = path.read_text(encoding="utf-8", errors="replace")
    _read_cache[key] = {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "content": content,
    }
    _read_cache.move_to_end(key)
    while len(_read_cache) > READ_CACHE_MAX_ENTRIES:
        _read_cache.popitem(last=False)
    return content, stat, False

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

        content, stat, cache_hit = await asyncio.to_thread(_read_text_cached, resolved)
        size = stat.st_size
        if size > 2_000_000:  # 2MB
            return {
                "error": f"File too large ({size:,} bytes). "
                         "Consider reading specific line ranges with start_line/end_line.",
            }

        lines = content.split("\n")
        total_lines = len(lines)

        # Apply line range
        start_idx = max(0, start_line - 1)
        if end_line:
            end_idx = min(end_line, total_lines)
        else:
            end_idx = min(start_idx + max_lines, total_lines)

        selected_lines = lines[start_idx:end_idx]
        content_slice = "\n".join(selected_lines)

        return {
            "path": _container_to_host_path(resolved),
            "content": content_slice,
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "total_lines": total_lines,
            "size_bytes": size,
            "cache_hit": cache_hit,
        }

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to read file: {e}"}

async def host_batch_read(
    paths: list,
    max_lines_per_file: int = 150,
    workspace_id: str = "default",
) -> dict:
    """Read multiple files in a single call. Returns all contents together."""
    MAX_FILES = 20
    MAX_BYTES_PER_FILE = 50_000
    MAX_TOTAL_BYTES = 500_000

    if not paths:
        return {"error": "No paths provided. Pass a list of file paths."}

    if len(paths) > MAX_FILES:
        return {"error": f"Too many files ({len(paths)}). Maximum is {MAX_FILES}."}

    mounts = _get_host_mounts()

    async def _read_one(file_path: str) -> dict:
        entry = {"path": file_path}
        try:
            resolved = _resolve_host_path(file_path, mounts)

            if not resolved.exists():
                entry["error"] = "File not found"
                return entry

            if not resolved.is_file():
                entry["error"] = "Not a file"
                return entry

            content, stat, cache_hit = await asyncio.to_thread(_read_text_cached, resolved)
            size = stat.st_size
            if size > MAX_BYTES_PER_FILE:
                entry["error"] = f"File too large ({size:,} bytes, max {MAX_BYTES_PER_FILE:,})"
                entry["size_bytes"] = size
                return entry

            lines = content.split("\n")
            if len(lines) > max_lines_per_file:
                content = "\n".join(lines[:max_lines_per_file])
                entry["truncated_at_line"] = max_lines_per_file
                entry["total_lines"] = len(lines)

            entry["content"] = content
            entry["lines"] = min(len(lines), max_lines_per_file)
            entry["path"] = _container_to_host_path(resolved)
            entry["size_bytes"] = size
            entry["cache_hit"] = cache_hit
        except ValueError as e:
            entry["error"] = str(e)
        except Exception as e:
            entry["error"] = f"Read failed: {e}"
        return entry

    pending = await asyncio.gather(*[_read_one(file_path) for file_path in paths])

    results = []
    total_bytes = 0
    for entry in pending:
        content = entry.get("content")
        if content is not None:
            content_bytes = len(content.encode("utf-8"))
            if total_bytes + content_bytes > MAX_TOTAL_BYTES:
                entry.pop("content", None)
                entry["error"] = f"Total batch size exceeded ({MAX_TOTAL_BYTES:,} bytes)"
            else:
                total_bytes += content_bytes
        results.append(entry)

    return {
        "files": results,
        "count": len(results),
        "successful": sum(1 for r in results if "content" in r),
        "total_bytes": total_bytes,
        "limits": {
            "max_files": MAX_FILES,
            "max_bytes_per_file": MAX_BYTES_PER_FILE,
            "max_total_bytes": MAX_TOTAL_BYTES,
        },
    }