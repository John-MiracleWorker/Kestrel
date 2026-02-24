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

from .fs.read import host_read, host_batch_read
from .fs.write import host_write
from .fs.explore import host_list, host_search, host_tree, host_find, project_recall

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
            name="host_batch_read",
            description=(
                "Read MULTIPLE files from the host filesystem in a single call. "
                "Pass a list of paths and get all contents back at once. "
                "USE THIS instead of calling host_read multiple times — "
                "it's 10x faster for reading multiple files (e.g. during code audits). "
                "Maximum 20 files per call, 50KB per file, 500KB total."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to read (absolute paths)",
                    },
                    "max_lines_per_file": {
                        "type": "integer",
                        "description": "Max lines per file (default 150)",
                        "default": 150,
                    },
                },
                "required": ["paths"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=30,
            category="host_file",
        ),
        handler=host_batch_read,
    )

    registry.register(
        definition=ToolDefinition(
            name="host_list",
            description=(
                "[PREFER host_tree instead] List a single directory on the host filesystem. "
                "For exploring a codebase, use host_tree(path) which returns the full recursive "
                "tree with tech stack detection in ONE call. Only use host_list for quick "
                "single-directory checks."
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
