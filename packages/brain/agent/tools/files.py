"""
File system tools — read, write, and list files within workspace directories.

All file operations are scoped to a workspace-specific base directory
to prevent unauthorized access to the broader file system.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.files")

# Workspace file root (set via env or default)
FILE_ROOT = Path(os.getenv("AGENT_FILE_ROOT", "/tmp/kestrel/workspaces"))


def register_file_tools(registry) -> None:
    """Register file system tools."""

    registry.register(
        definition=ToolDefinition(
            name="file_read",
            description=(
                "Read the contents of a file in the workspace. "
                "Use for reading code, configs, documents, or data files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to workspace root",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum lines to read (default 200)",
                        "default": 200,
                    },
                },
                "required": ["path"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=10,
            category="file",
        ),
        handler=file_read,
    )

    registry.register(
        definition=ToolDefinition(
            name="file_write",
            description=(
                "Write content to a file in the workspace. Creates parent "
                "directories if needed. Use for saving code, documents, "
                "configs, or any text content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to workspace root",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file",
                    },
                    "append": {
                        "type": "boolean",
                        "description": "If true, append instead of overwrite",
                        "default": False,
                    },
                },
                "required": ["path", "content"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=10,
            category="file",
        ),
        handler=file_write,
    )

    registry.register(
        definition=ToolDefinition(
            name="file_list",
            description=(
                "List files and directories in a workspace path. "
                "Shows file names, sizes, and types."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to workspace root (default: root)",
                        "default": ".",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, list recursively (max depth 3)",
                        "default": False,
                    },
                },
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=10,
            category="file",
        ),
        handler=file_list,
    )


def _resolve_path(path: str, workspace_id: str = "default") -> Path:
    """
    Resolve a relative path to an absolute path within the workspace.
    Raises ValueError if the path escapes the workspace root.
    """
    workspace_dir = FILE_ROOT / workspace_id
    workspace_dir.mkdir(parents=True, exist_ok=True)

    resolved = (workspace_dir / path).resolve()

    # Security: ensure the path is within the workspace
    if not str(resolved).startswith(str(workspace_dir.resolve())):
        raise ValueError(
            f"Path traversal blocked: '{path}' resolves outside workspace"
        )

    return resolved


async def file_read(
    path: str,
    max_lines: int = 200,
    workspace_id: str = "default",
) -> dict:
    """Read a file from the workspace."""
    max_lines = min(max_lines, 1000)

    try:
        resolved = _resolve_path(path, workspace_id)

        if not resolved.exists():
            return {"error": f"File not found: {path}"}

        if not resolved.is_file():
            return {"error": f"Not a file: {path}"}

        # Check file size
        size = resolved.stat().st_size
        if size > 1_000_000:  # 1MB
            return {
                "error": f"File too large ({size:,} bytes). "
                         "Consider reading specific sections.",
            }

        content = resolved.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")

        if len(lines) > max_lines:
            content = "\n".join(lines[:max_lines])
            content += f"\n\n... ({len(lines) - max_lines} more lines)"

        return {
            "path": path,
            "content": content,
            "lines": min(len(lines), max_lines),
            "total_lines": len(lines),
            "size_bytes": size,
        }

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to read file: {e}"}


async def file_write(
    path: str,
    content: str,
    append: bool = False,
    workspace_id: str = "default",
) -> dict:
    """Write content to a file in the workspace."""
    try:
        resolved = _resolve_path(path, workspace_id)

        # Create parent directories
        resolved.parent.mkdir(parents=True, exist_ok=True)

        mode = "a" if append else "w"
        resolved.write_text(
            content if not append else content,
            encoding="utf-8",
        )
        # Use proper append mode
        if append:
            with open(resolved, "a", encoding="utf-8") as f:
                f.write(content)
        else:
            resolved.write_text(content, encoding="utf-8")

        size = resolved.stat().st_size

        return {
            "path": path,
            "action": "appended" if append else "written",
            "size_bytes": size,
            "success": True,
        }

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to write file: {e}"}


async def file_list(
    path: str = ".",
    recursive: bool = False,
    workspace_id: str = "default",
) -> dict:
    """List files and directories in a workspace path."""
    try:
        resolved = _resolve_path(path, workspace_id)

        if not resolved.exists():
            return {"error": f"Directory not found: {path}"}

        if not resolved.is_dir():
            return {"error": f"Not a directory: {path}"}

        entries = []
        max_entries = 100

        if recursive:
            for item in sorted(resolved.rglob("*")):
                if len(entries) >= max_entries:
                    break
                # Limit depth to 3
                rel = item.relative_to(resolved)
                if len(rel.parts) > 3:
                    continue
                entries.append(_file_info(item, resolved))
        else:
            for item in sorted(resolved.iterdir()):
                if len(entries) >= max_entries:
                    break
                entries.append(_file_info(item, resolved))

        return {
            "path": path,
            "entries": entries,
            "count": len(entries),
            "truncated": len(entries) >= max_entries,
        }

    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to list directory: {e}"}


def _file_info(item: Path, base: Path) -> dict:
    """Build file info dict."""
    rel_path = str(item.relative_to(base)).replace("\\", "/")
    info = {
        "name": rel_path,
        "type": "directory" if item.is_dir() else "file",
    }
    if item.is_file():
        info["size_bytes"] = item.stat().st_size
        info["extension"] = item.suffix
    return info
