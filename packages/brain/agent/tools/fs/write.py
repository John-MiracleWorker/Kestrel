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