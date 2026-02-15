"""
Libre Bird — Skill Loader
Auto-discovers, loads, and manages skills from the skills/ directory.
Each skill is a folder with a skill.json manifest and an __init__.py module.

Exports the same interface as the old tools.py:
    TOOL_DEFINITIONS  — list of OpenAI-format tool definitions
    execute_tool()    — run a tool by name
    list_skills()     — list all discovered skills with status
    toggle_skill()    — enable/disable a skill
"""

import importlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("libre_bird.skills")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")

# Ensure skills dir is on sys.path for imports
if SKILLS_DIR not in sys.path:
    sys.path.insert(0, os.path.dirname(SKILLS_DIR))


# ---------------------------------------------------------------------------
# Skill metadata
# ---------------------------------------------------------------------------

class Skill:
    """Represents a discovered skill."""

    __slots__ = (
        "name", "display_name", "description", "version", "author",
        "icon", "category", "dependencies", "enabled",
        "tool_defs", "tool_handlers", "module", "path", "error",
    )

    def __init__(self, path: str, manifest: dict):
        self.path = path
        self.name: str = manifest.get("name", os.path.basename(path))
        self.display_name: str = manifest.get("display_name", self.name.replace("_", " ").title())
        self.description: str = manifest.get("description", "")
        self.version: str = manifest.get("version", "1.0.0")
        self.author: str = manifest.get("author", "Libre Bird")
        self.icon: str = manifest.get("icon", "🧩")
        self.category: str = manifest.get("category", "general")
        self.dependencies: list[str] = manifest.get("dependencies", [])
        self.enabled: bool = True
        self.tool_defs: list[dict] = []
        self.tool_handlers: dict[str, Callable] = {}
        self.module = None
        self.error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "icon": self.icon,
            "category": self.category,
            "enabled": self.enabled,
            "tools": [td["function"]["name"] for td in self.tool_defs],
            "tool_count": len(self.tool_defs),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_skills: dict[str, Skill] = {}
_tool_registry: dict[str, Callable] = {}

# The aggregated definitions list (same format llm_engine.py expects)
TOOL_DEFINITIONS: list[dict] = []

# Disabled skills (persisted via settings in database)
_disabled_skills: set[str] = set()


def _load_disabled_from_db():
    """Load disabled skills from the database settings table."""
    global _disabled_skills
    try:
        from database import db
        val = db.get_setting("disabled_skills")
        if val:
            _disabled_skills = set(json.loads(val))
    except Exception:
        pass  # DB may not be initialized yet


def _save_disabled_to_db():
    """Persist disabled skills to the database settings table."""
    try:
        from database import db
        db.set_setting("disabled_skills", json.dumps(list(_disabled_skills)))
    except Exception:
        pass


def _discover_skills() -> list[Skill]:
    """Scan the skills/ directory for valid skill folders."""
    skills = []
    if not os.path.isdir(SKILLS_DIR):
        os.makedirs(SKILLS_DIR, exist_ok=True)
        return skills

    for entry in sorted(os.listdir(SKILLS_DIR)):
        skill_path = os.path.join(SKILLS_DIR, entry)
        manifest_path = os.path.join(skill_path, "skill.json")

        if not os.path.isdir(skill_path):
            continue
        if not os.path.isfile(manifest_path):
            continue

        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            skills.append(Skill(skill_path, manifest))
        except Exception as e:
            logger.warning(f"Skipping skill at {skill_path}: {e}")

    return skills


def _load_skill(skill: Skill) -> bool:
    """Import a skill module and extract tool definitions + handlers."""
    try:
        # Import as skills.<name>
        module_name = f"skills.{os.path.basename(skill.path)}"
        if module_name in sys.modules:
            # Reload to pick up changes
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(module_name)

        skill.module = module

        # Extract TOOL_DEFINITIONS and TOOL_HANDLERS from the module
        defs = getattr(module, "TOOL_DEFINITIONS", [])
        handlers = getattr(module, "TOOL_HANDLERS", {})

        skill.tool_defs = defs
        skill.tool_handlers = handlers
        skill.error = None
        return True

    except ImportError as e:
        skill.error = f"Missing dependency: {e}"
        logger.warning(f"Skill '{skill.name}' import failed: {e}")
        return False
    except Exception as e:
        skill.error = f"Load error: {e}"
        logger.error(f"Skill '{skill.name}' failed to load: {e}")
        return False


def load_all_skills():
    """Discover, load, and register all skills."""
    global TOOL_DEFINITIONS
    _skills.clear()
    _tool_registry.clear()
    TOOL_DEFINITIONS.clear()

    _load_disabled_from_db()

    discovered = _discover_skills()
    logger.info(f"Discovered {len(discovered)} skills in {SKILLS_DIR}")

    for skill in discovered:
        skill.enabled = skill.name not in _disabled_skills

        if not skill.enabled:
            _skills[skill.name] = skill
            logger.info(f"  Skill '{skill.name}' is disabled — skipping load")
            continue

        if _load_skill(skill):
            _skills[skill.name] = skill
            # Register tools
            for td in skill.tool_defs:
                TOOL_DEFINITIONS.append(td)
            for tool_name, handler in skill.tool_handlers.items():
                _tool_registry[tool_name] = handler
            logger.info(
                f"  ✅ Loaded skill '{skill.name}' — "
                f"{len(skill.tool_defs)} tools"
            )
        else:
            _skills[skill.name] = skill
            logger.warning(f"  ⚠️ Skill '{skill.name}' failed: {skill.error}")


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name and return the JSON result."""
    handler = _tool_registry.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        logger.info(f"Executing tool: {name}({arguments})")
        result = handler(arguments)
        logger.info(f"Tool result: {json.dumps(result)[:200]}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return json.dumps({"error": f"Tool failed: {str(e)}"})


def list_skills() -> list[dict]:
    """Return metadata for all discovered skills."""
    return [s.to_dict() for s in _skills.values()]


def toggle_skill(name: str, enabled: Optional[bool] = None) -> dict:
    """Enable or disable a skill. Returns the new state."""
    if name not in _skills:
        return {"error": f"Unknown skill: {name}"}

    skill = _skills[name]

    if enabled is None:
        enabled = not skill.enabled

    if enabled and not skill.enabled:
        # Enabling — load and register
        _disabled_skills.discard(name)
        skill.enabled = True
        if _load_skill(skill):
            for td in skill.tool_defs:
                TOOL_DEFINITIONS.append(td)
            for tool_name, handler in skill.tool_handlers.items():
                _tool_registry[tool_name] = handler
    elif not enabled and skill.enabled:
        # Disabling — unregister
        _disabled_skills.add(name)
        skill.enabled = False
        tool_names = {td["function"]["name"] for td in skill.tool_defs}
        TOOL_DEFINITIONS[:] = [
            td for td in TOOL_DEFINITIONS
            if td["function"]["name"] not in tool_names
        ]
        for tn in tool_names:
            _tool_registry.pop(tn, None)

    _save_disabled_to_db()
    return skill.to_dict()


def get_skill(name: str) -> Optional[dict]:
    """Get metadata for a single skill."""
    skill = _skills.get(name)
    return skill.to_dict() if skill else None


# ---------------------------------------------------------------------------
# Auto-load on import
# ---------------------------------------------------------------------------
load_all_skills()
