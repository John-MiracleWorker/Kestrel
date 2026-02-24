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

logger = logging.getLogger("brain.agent.tools.host_files")

# Explicitly export underscore-prefixed names for `from .utils import *`
# (Python's wildcard import skips _-prefixed names by default)
__all__ = [
    "BLOCKED_PATHS",
    "BLOCKED_EXTENSIONS",
    "PROJECT_MARKERS",
    "TREE_SKIP_DIRS",
    "_get_host_mounts",
    "_HOST_MOUNT_ROOT",
    "_CONTAINER_MOUNT_POINT",
    "_host_to_container_path",
    "_container_to_host_path",
    "_is_blocked_path",
    "_resolve_host_path",
    "_host_file_info",
    # OrderedDict caches used by submodules
    "_tree_cache",
    "_read_cache",
]

BLOCKED_PATHS = frozenset([
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
])

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

_HOST_MOUNT_ROOT = os.getenv("HOST_MOUNT_ROOT", "/Users")

_CONTAINER_MOUNT_POINT = "/host_fs"

# Shared caches (imported by explore.py and read.py via wildcard)
_tree_cache: OrderedDict = OrderedDict()
_read_cache: OrderedDict = OrderedDict()

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
    # O(1) average-case set intersection instead of O(n) linear scan
    hit = set(path.parts) & BLOCKED_PATHS
    if hit:
        return f"Access denied: '{next(iter(hit))}' is a sensitive directory"

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

TREE_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", "venv", ".venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", ".cache", "coverage", ".turbo",
    "target",  # Rust/Java
}