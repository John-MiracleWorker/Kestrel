"""
Project-aware codebase memory — auto-saves and recalls project structure.

When the agent scans a project with host_tree, the structure and context
are automatically memoized in the vector store, keyed by project name.
On subsequent interactions, the agent can recall project context instantly
without re-scanning.

Storage model:
  - source_type = "project_map"
  - source_id   = project slug (e.g. "kestrel", "my-react-app")
  - metadata    = { project_name, path, tech_stack, scanned_at }
  - content     = condensed project summary (tree + deps + structure)
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("brain.agent.tools.project_context")

# Module-level reference, set during registration
_vector_store = None


def set_vector_store(vs):
    """Called during tool registration to inject the vector store."""
    global _vector_store
    _vector_store = vs


def _slugify(name: str) -> str:
    """Convert project name to a url-safe slug."""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug or "unknown-project"


def _detect_project_name(tree_result: dict) -> str:
    """
    Extract project name from host_tree result.
    Priority: package.json name > pyproject name > directory name
    """
    project = tree_result.get("project", {})
    if project.get("name"):
        return project["name"]

    # Fall back to directory name from path
    path = tree_result.get("path", "")
    if path:
        parts = path.rstrip("/").split("/")
        return parts[-1] if parts else "unknown"

    return "unknown"


def _build_project_summary(tree_result: dict) -> str:
    """
    Build a condensed, LLM-friendly project summary from host_tree output.
    This is what gets stored in memory and recalled later.
    """
    name = _detect_project_name(tree_result)
    path = tree_result.get("path", "unknown")
    tech = tree_result.get("tech_stack", [])
    project = tree_result.get("project", {})
    summary_info = tree_result.get("summary", {})
    tree = tree_result.get("tree", "")

    lines = [
        f"# Project: {name}",
        f"Path: {path}",
    ]

    if tech:
        lines.append(f"Tech stack: {', '.join(tech)}")

    if project.get("description"):
        lines.append(f"Description: {project['description']}")

    if project.get("dependencies"):
        lines.append(f"Dependencies: {', '.join(project['dependencies'][:15])}")

    if project.get("devDependencies"):
        lines.append(f"Dev deps: {', '.join(project['devDependencies'][:10])}")

    if project.get("scripts"):
        lines.append(f"Scripts: {', '.join(project['scripts'])}")

    file_count = summary_info.get("files", 0)
    dir_count = summary_info.get("directories", 0)
    lines.append(f"Size: {file_count} files, {dir_count} directories")

    # Include a condensed tree (first 80 lines max)
    if tree:
        tree_lines = tree.split("\n")
        if len(tree_lines) > 80:
            tree_preview = "\n".join(tree_lines[:80])
            tree_preview += f"\n... ({len(tree_lines) - 80} more entries)"
        else:
            tree_preview = tree
        lines.append(f"\nStructure:\n{tree_preview}")

    return "\n".join(lines)


async def save_project_context(
    tree_result: dict,
    workspace_id: str = "default",
) -> Optional[str]:
    """
    Auto-save project context from a host_tree result.
    Upserts by project slug — replaces previous scan for same project.

    Returns the memory ID if saved, None if store unavailable.
    """
    if not _vector_store:
        logger.debug("Vector store not available, skipping project memo")
        return None

    name = _detect_project_name(tree_result)
    slug = _slugify(name)
    path = tree_result.get("path", "")
    tech = tree_result.get("tech_stack", [])

    summary = _build_project_summary(tree_result)

    # Delete previous entry for this project (upsert pattern)
    try:
        pool = _vector_store._pool
        if pool:
            await pool.execute(
                """DELETE FROM memory_embeddings
                   WHERE workspace_id = $1
                   AND source_type = 'project_map'
                   AND source_id = $2""",
                workspace_id, slug,
            )
    except Exception as e:
        logger.warning(f"Failed to delete old project map for {slug}: {e}")

    # Store new entry
    try:
        memory_id = await _vector_store.store(
            workspace_id=workspace_id,
            content=summary,
            source_type="project_map",
            source_id=slug,
            metadata={
                "project_name": name,
                "project_slug": slug,
                "path": path,
                "tech_stack": tech,
                "file_count": tree_result.get("summary", {}).get("files", 0),
                "dir_count": tree_result.get("summary", {}).get("directories", 0),
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info(f"Saved project map for '{name}' (slug={slug}, id={memory_id})")
        return memory_id
    except Exception as e:
        logger.warning(f"Failed to save project map for {slug}: {e}")
        return None


async def recall_project_context(
    project_name: str,
    workspace_id: str = "default",
) -> Optional[dict]:
    """
    Recall saved context for a specific project by name or slug.
    Returns the stored summary and metadata, or None if not found.
    """
    if not _vector_store:
        return None

    slug = _slugify(project_name)

    try:
        pool = _vector_store._pool
        if not pool:
            return None

        row = await pool.fetchrow(
            """SELECT content, metadata, created_at
               FROM memory_embeddings
               WHERE workspace_id = $1
               AND source_type = 'project_map'
               AND source_id = $2
               ORDER BY created_at DESC
               LIMIT 1""",
            workspace_id, slug,
        )

        if not row:
            # Try fuzzy match via semantic search
            results = await _vector_store.search(
                workspace_id=workspace_id,
                query=f"project map for {project_name}",
                limit=3,
            )
            # Filter to project_map entries only
            for r in results:
                if r.get("source_type") == "project_map" and r.get("similarity", 0) > 0.5:
                    return {
                        "project": r.get("metadata", {}).get("project_name", project_name),
                        "summary": r.get("content", ""),
                        "metadata": r.get("metadata", {}),
                        "match_type": "semantic",
                    }
            return None

        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        return {
            "project": metadata.get("project_name", project_name),
            "summary": row["content"],
            "metadata": metadata,
            "scanned_at": str(row["created_at"]),
            "match_type": "exact",
        }

    except Exception as e:
        logger.warning(f"Failed to recall project context for {project_name}: {e}")
        return None


async def list_known_projects(
    workspace_id: str = "default",
) -> list[dict]:
    """List all projects that have been scanned and memoized."""
    if not _vector_store:
        return []

    try:
        pool = _vector_store._pool
        if not pool:
            return []

        rows = await pool.fetch(
            """SELECT source_id, metadata, created_at
               FROM memory_embeddings
               WHERE workspace_id = $1
               AND source_type = 'project_map'
               ORDER BY created_at DESC""",
            workspace_id,
        )

        projects = []
        for row in rows:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            projects.append({
                "slug": row["source_id"],
                "name": metadata.get("project_name", row["source_id"]),
                "path": metadata.get("path", ""),
                "tech_stack": metadata.get("tech_stack", []),
                "scanned_at": str(row["created_at"]),
            })

        return projects

    except Exception as e:
        logger.warning(f"Failed to list known projects: {e}")
        return []
