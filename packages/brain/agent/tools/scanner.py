"""
Sub-Agent Codebase Scanner — spawns specialist sub-agents to deeply analyze
codebases, repos, and file systems using LLM-powered reasoning.

Unlike the static AST scanner (self_improve), this system uses sub-agents
that READ files and REASON about them — understanding architecture, patterns,
dependencies, and issues at a semantic level rather than just syntactic.

Architecture:
  scan_codebase(path, goal)
    → Phase 1: Discovery — host_tree to map the project structure
    → Phase 2: Partition — split into scan regions by module/directory
    → Phase 3: Dispatch — spawn scanner sub-agents in parallel
    → Phase 4: Synthesis — merge findings into a structured report
    → Phase 5: Memory — store analysis in the memory graph

Each scanner sub-agent:
  1. Reads files in its assigned region
  2. Uses LLM reasoning to understand what it reads
  3. Produces structured findings: architecture, patterns, issues, suggestions
  4. Reports back to the orchestrator
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.scanner")


# ── Scan Region Partitioning ────────────────────────────────────────

# Priority files to always include in analysis (regardless of region)
PRIORITY_FILES = {
    "README.md", "readme.md", "README",
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "tsconfig.json", "setup.py", "setup.cfg",
}

# Directories to treat as independent scan regions
MODULE_INDICATORS = {
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "setup.py", "__init__.py", "mod.rs", "index.ts", "index.js",
}

# Directories that should not become their own scan region
SKIP_REGION_DIRS = {
    "node_modules", "__pycache__", ".git", "dist", "build", ".next",
    ".nuxt", ".cache", "coverage", ".turbo", "target", "venv", ".venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".eggs", "egg-info",
}

# Maximum files a single sub-agent should handle
MAX_FILES_PER_REGION = 25

# Maximum concurrent scanner sub-agents
MAX_PARALLEL_SCANNERS = 5

# Scan goal presets that map to focused analysis prompts
SCAN_PRESETS = {
    "architecture": (
        "Analyze the architecture and design patterns. Identify:\n"
        "- Overall architecture pattern (MVC, microservices, monolith, etc.)\n"
        "- Key abstractions and their relationships\n"
        "- Data flow and control flow patterns\n"
        "- Entry points and API boundaries\n"
        "- How components communicate"
    ),
    "security": (
        "Perform a security review. Look for:\n"
        "- Input validation gaps\n"
        "- Authentication/authorization issues\n"
        "- Injection vulnerabilities (SQL, command, XSS)\n"
        "- Hardcoded secrets or credentials\n"
        "- Insecure configurations\n"
        "- Missing rate limiting or access controls"
    ),
    "quality": (
        "Assess code quality and maintainability. Check for:\n"
        "- Code duplication and DRY violations\n"
        "- Overly complex functions (high cyclomatic complexity)\n"
        "- Missing error handling or silent failures\n"
        "- Dead code and unused imports\n"
        "- Inconsistent naming or style\n"
        "- Missing tests for critical paths"
    ),
    "understand": (
        "Build a deep understanding of this codebase. Document:\n"
        "- What this project does (purpose and functionality)\n"
        "- How the code is organized (modules, packages, layers)\n"
        "- Key data models and their relationships\n"
        "- External dependencies and integrations\n"
        "- Build and deployment process\n"
        "- How a new developer would navigate this codebase"
    ),
    "plan": (
        "Analyze the codebase to create a detailed implementation plan. Identify:\n"
        "- Current capabilities and gaps\n"
        "- Extension points and integration surfaces\n"
        "- Risks and dependencies that affect implementation\n"
        "- Recommended implementation order\n"
        "- Testing strategy for changes\n"
        "- Files and modules that would need modification"
    ),
}


@dataclass
class ScanRegion:
    """A partition of the codebase assigned to a single scanner sub-agent."""
    name: str
    path: str
    files: list[str] = field(default_factory=list)
    description: str = ""
    priority: int = 0  # Higher = more important to scan


@dataclass
class ScanFinding:
    """A single finding from a scanner sub-agent."""
    category: str       # architecture, pattern, issue, suggestion, dependency
    severity: str       # info, low, medium, high, critical
    title: str
    description: str
    file: str = ""
    line: int = 0
    suggestion: str = ""
    region: str = ""


@dataclass
class ScanReport:
    """The full synthesized report from a codebase scan."""
    path: str
    goal: str
    regions_scanned: int
    files_analyzed: int
    scan_time_ms: int
    tech_stack: list[str] = field(default_factory=list)
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    architecture: dict = field(default_factory=dict)
    implementation_plan: list[dict] = field(default_factory=list)
    raw_agent_outputs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "goal": self.goal,
            "regions_scanned": self.regions_scanned,
            "files_analyzed": self.files_analyzed,
            "scan_time_ms": self.scan_time_ms,
            "tech_stack": self.tech_stack,
            "summary": self.summary,
            "findings": self.findings,
            "architecture": self.architecture,
            "implementation_plan": self.implementation_plan,
        }


# ── Partitioning Logic ──────────────────────────────────────────────

def _discover_regions(tree_data: dict, base_path: str) -> list[ScanRegion]:
    """
    Partition a project tree into scan regions for parallel sub-agents.

    Strategy:
    - Each top-level directory becomes a region
    - If a directory has MODULE_INDICATORS, it becomes its own region
    - Very large directories are split into sub-regions
    - Priority files (README, config) get highest priority
    """
    tree_text = tree_data.get("tree", "")
    if not tree_text:
        return [ScanRegion(name="root", path=base_path, description="Entire project")]

    regions: list[ScanRegion] = []
    root_files: list[str] = []

    # Parse tree lines to identify top-level structure
    for line in tree_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # Extract the file/dir name from tree-formatted line
        # Lines look like: "├── src/" or "│   ├── main.py"
        name = stripped
        for prefix in ("├── ", "└── ", "│   ", "    "):
            name = name.replace(prefix, "")
        name = name.strip()

        # Determine nesting depth by counting tree prefixes
        depth = 0
        temp = stripped
        while temp.startswith(("│   ", "    ", "├── ", "└── ")):
            depth += 1
            for prefix in ("│   ", "    "):
                if temp.startswith(prefix):
                    temp = temp[len(prefix):]
                    break
            else:
                # It's a connector (├── or └──), count it and strip
                for prefix in ("├── ", "└── "):
                    if temp.startswith(prefix):
                        temp = temp[len(prefix):]
                        break
                break

        # Top-level directories become regions
        if depth == 0 and name.endswith("/"):
            dir_name = name.rstrip("/").split("[")[0].strip()
            if dir_name in SKIP_REGION_DIRS:
                continue
            regions.append(ScanRegion(
                name=dir_name,
                path=f"{base_path}/{dir_name}",
                description=f"Module: {dir_name}",
                priority=5,
            ))
        elif depth == 0:
            # Top-level file
            clean_name = name.split("(")[0].strip()  # Remove size annotations
            root_files.append(clean_name)

    # Add root files as a region (config, README, etc.)
    if root_files:
        regions.insert(0, ScanRegion(
            name="root",
            path=base_path,
            files=root_files,
            description="Project root files (config, documentation, entry points)",
            priority=10,  # Highest priority — provides project context
        ))

    # Sort by priority (highest first)
    regions.sort(key=lambda r: r.priority, reverse=True)

    return regions


def _build_scan_goal(base_goal: str, region: ScanRegion) -> str:
    """Build the specific goal for a scanner sub-agent assigned to a region."""
    # Resolve preset if the goal is a known preset name
    expanded_goal = SCAN_PRESETS.get(base_goal, base_goal)

    return (
        f"Deeply analyze the '{region.name}' section of this codebase.\n\n"
        f"Region path: {region.path}\n"
        f"Region description: {region.description}\n"
        f"{'Files to focus on: ' + ', '.join(region.files[:15]) if region.files else ''}\n\n"
        f"Analysis goal:\n{expanded_goal}\n\n"
        f"Instructions:\n"
        f"1. Use host_tree to understand the structure of this region\n"
        f"2. Use host_batch_read to read the most important files (prioritize entry points, "
        f"   main modules, configuration files, and files relevant to the analysis goal)\n"
        f"3. Use host_search if you need to trace specific patterns or references\n"
        f"4. REASON deeply about what you read — don't just list files, explain the WHY\n"
        f"5. Produce a structured JSON report with your findings\n\n"
        f"Your report MUST be a JSON object with these keys:\n"
        f'{{"region": "{region.name}",\n'
        f'  "files_analyzed": ["list of files you read"],\n'
        f'  "summary": "2-3 sentence summary of this region",\n'
        f'  "architecture": {{"pattern": "...", "key_abstractions": [...], "data_flow": "..."}},\n'
        f'  "findings": [{{"category": "architecture|pattern|issue|suggestion|dependency",\n'
        f'                 "severity": "info|low|medium|high|critical",\n'
        f'                 "title": "...", "description": "...",\n'
        f'                 "file": "path", "suggestion": "..."}}],\n'
        f'  "dependencies": ["external deps this region relies on"],\n'
        f'  "recommendations": ["actionable recommendations"]}}'
    )


# ── Synthesis Logic ─────────────────────────────────────────────────

def _parse_agent_output(raw_output: str, region_name: str) -> dict:
    """Parse a scanner sub-agent's output into structured data."""
    # Try to extract JSON from the output
    try:
        # Look for JSON block in the output
        start = raw_output.find("{")
        if start < 0:
            return {"region": region_name, "raw": raw_output[:2000], "parse_error": "No JSON found"}

        # Find matching closing brace
        depth = 0
        end = start
        for i, ch in enumerate(raw_output[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        json_str = raw_output[start:end]
        parsed = json.loads(json_str)
        parsed["region"] = region_name
        return parsed
    except (json.JSONDecodeError, ValueError):
        # Fallback: return the raw text as a summary
        return {
            "region": region_name,
            "summary": raw_output[:2000],
            "parse_error": "Could not parse JSON from agent output",
        }


def _synthesize_reports(
    agent_outputs: list[dict],
    tree_data: dict,
    goal: str,
    scan_time_ms: int,
    base_path: str,
) -> ScanReport:
    """Merge findings from multiple scanner sub-agents into a unified report."""
    report = ScanReport(
        path=base_path,
        goal=goal,
        regions_scanned=len(agent_outputs),
        files_analyzed=0,
        scan_time_ms=scan_time_ms,
        tech_stack=tree_data.get("tech_stack", []),
    )

    all_findings = []
    all_summaries = []
    architecture_parts = {}
    all_recommendations = []
    all_dependencies = []

    for output in agent_outputs:
        region = output.get("region", "unknown")

        # Aggregate files analyzed
        files = output.get("files_analyzed", [])
        if isinstance(files, list):
            report.files_analyzed += len(files)

        # Collect summaries
        summary = output.get("summary", "")
        if summary:
            all_summaries.append(f"**{region}**: {summary}")

        # Collect findings
        findings = output.get("findings", [])
        if isinstance(findings, list):
            for f in findings:
                if isinstance(f, dict):
                    f["region"] = region
                    all_findings.append(f)

        # Merge architecture info
        arch = output.get("architecture", {})
        if isinstance(arch, dict) and arch:
            architecture_parts[region] = arch

        # Collect recommendations
        recs = output.get("recommendations", [])
        if isinstance(recs, list):
            all_recommendations.extend(recs)

        # Collect dependencies
        deps = output.get("dependencies", [])
        if isinstance(deps, list):
            all_dependencies.extend(deps)

        report.raw_agent_outputs.append(output)

    # Sort findings by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    all_findings.sort(key=lambda f: severity_order.get(f.get("severity", "info"), 4))

    report.findings = all_findings
    report.architecture = {
        "regions": architecture_parts,
        "dependencies": list(set(all_dependencies)),
    }

    # Build overall summary
    severity_counts = {}
    for f in all_findings:
        s = f.get("severity", "info")
        severity_counts[s] = severity_counts.get(s, 0) + 1

    summary_parts = [f"Scanned {report.regions_scanned} regions, {report.files_analyzed} files"]
    if severity_counts:
        counts = []
        for sev in ("critical", "high", "medium", "low", "info"):
            if sev in severity_counts:
                counts.append(f"{severity_counts[sev]} {sev}")
        summary_parts.append(f"Found {len(all_findings)} findings: {', '.join(counts)}")

    if all_summaries:
        summary_parts.append("\n" + "\n".join(all_summaries))

    report.summary = "\n".join(summary_parts)

    # Build implementation plan from recommendations
    if all_recommendations:
        report.implementation_plan = [
            {"step": i + 1, "action": rec}
            for i, rec in enumerate(dict.fromkeys(all_recommendations))  # dedupe preserving order
        ]

    return report


# ── Main Scan Orchestrator ──────────────────────────────────────────

async def scan_codebase(
    path: str,
    goal: str = "understand",
    depth: int = 4,
    max_regions: int = MAX_PARALLEL_SCANNERS,
    workspace_id: str = "default",
) -> dict:
    """
    Orchestrate a multi-agent codebase scan.

    Spawns scanner sub-agents in parallel to analyze different regions
    of a codebase, then synthesizes their findings into a unified report.

    Args:
        path: Path to the codebase root (host filesystem path)
        goal: Scanning goal — either a preset name (architecture, security,
              quality, understand, plan) or a custom analysis prompt
        depth: Tree traversal depth for discovery
        max_regions: Maximum number of parallel scanner sub-agents
        workspace_id: Workspace scope for memory storage
    """
    start = time.monotonic()

    # ── Phase 1: Discovery ──────────────────────────────────────
    logger.info(f"Scan starting: path={path}, goal={goal}")

    # Import host_tree for project structure discovery
    from agent.tools.fs.explore import host_tree

    tree_data = await host_tree(
        path=path,
        depth=depth,
        workspace_id=workspace_id,
    )

    if "error" in tree_data:
        return {"error": f"Discovery failed: {tree_data['error']}"}

    # ── Phase 2: Partition ──────────────────────────────────────
    regions = _discover_regions(tree_data, path)

    if not regions:
        return {"error": "No scannable regions found in the project"}

    # Cap the number of parallel agents
    regions = regions[:max_regions]

    logger.info(f"Discovered {len(regions)} scan regions: {[r.name for r in regions]}")

    # ── Phase 3: Dispatch sub-agents ────────────────────────────
    # Get coordinator from the tool registry (set by agent loop)
    from agent.tools import ToolRegistry
    coordinator = None
    current_task = None

    # Try to get coordinator from the module-level registry reference
    try:
        import agent.tools as tools_module
        registries = [
            v for v in vars(tools_module).values()
            if isinstance(v, ToolRegistry)
        ]
        for reg in registries:
            if hasattr(reg, '_coordinator') and reg._coordinator:
                coordinator = reg._coordinator
                current_task = getattr(reg, '_current_task', None)
                break
    except Exception:
        pass

    if coordinator and current_task:
        # Use the real coordinator for parallel sub-agent dispatch
        subtasks = [
            {
                "goal": _build_scan_goal(goal, region),
                "specialist": "scanner",
            }
            for region in regions
        ]

        try:
            agent_results = await coordinator.delegate_parallel(
                parent_task=current_task,
                subtasks=subtasks,
            )
        except Exception as e:
            logger.error(f"Parallel scanner dispatch failed: {e}")
            return {"error": f"Scanner dispatch failed: {e}"}

        # Parse agent outputs
        agent_outputs = []
        for i, result in enumerate(agent_results):
            region_name = regions[i].name if i < len(regions) else f"region_{i}"
            parsed = _parse_agent_output(str(result), region_name)
            agent_outputs.append(parsed)
    else:
        # Fallback: run scanning without sub-agents (direct file analysis)
        logger.warning("Coordinator not available — falling back to direct scan")
        agent_outputs = await _direct_scan_fallback(regions, goal, workspace_id)

    # ── Phase 4: Synthesis ──────────────────────────────────────
    elapsed_ms = int((time.monotonic() - start) * 1000)

    report = _synthesize_reports(
        agent_outputs=agent_outputs,
        tree_data=tree_data,
        goal=goal,
        scan_time_ms=elapsed_ms,
        base_path=path,
    )

    # ── Phase 5: Memory persistence ─────────────────────────────
    try:
        from agent.tools.project_context import save_project_context
        scan_context = {
            "path": path,
            "scan_goal": goal,
            "summary": report.summary[:1000],
            "findings_count": len(report.findings),
            "tech_stack": report.tech_stack,
            "architecture": report.architecture,
        }
        await save_project_context(scan_context, workspace_id=workspace_id)
    except Exception as e:
        logger.debug(f"Failed to persist scan to memory: {e}")

    result = report.to_dict()

    # Add helpful hints
    result["_hints"] = {
        "view_findings": "Findings are sorted by severity (critical first)",
        "implementation": "Check 'implementation_plan' for recommended actions",
        "deep_dive": "Use delegate_task with goal='<specific question>' and specialist='scanner' for focused analysis",
        "presets": list(SCAN_PRESETS.keys()),
    }

    logger.info(
        f"Scan complete: {report.regions_scanned} regions, "
        f"{report.files_analyzed} files, {len(report.findings)} findings "
        f"in {elapsed_ms}ms"
    )

    return result


async def _direct_scan_fallback(
    regions: list[ScanRegion],
    goal: str,
    workspace_id: str,
) -> list[dict]:
    """
    Fallback scanning when the coordinator is not available.
    Uses direct file reading + basic analysis instead of sub-agents.
    """
    from agent.tools.fs.explore import host_tree, host_find
    from agent.tools.fs.read import host_batch_read

    outputs = []

    for region in regions:
        try:
            # Get tree for this region
            tree = await host_tree(
                path=region.path,
                depth=3,
                workspace_id=workspace_id,
            )

            # Find key files to read
            files_to_read = list(region.files)  # Start with specified files

            # If no specific files, find the most important ones
            if not files_to_read:
                find_result = await host_find(
                    pattern=r"\.(py|ts|js|rs|go|java)$",
                    path=region.path,
                    file_type="file",
                    max_results=MAX_FILES_PER_REGION,
                    workspace_id=workspace_id,
                )
                if "results" in find_result:
                    files_to_read = [
                        r["path"] for r in find_result["results"]
                        if r.get("path")
                    ]

            # Read the files
            file_contents = {}
            if files_to_read:
                read_result = await host_batch_read(
                    paths=files_to_read[:20],
                    max_lines_per_file=100,
                    workspace_id=workspace_id,
                )
                if "files" in read_result:
                    for f in read_result["files"]:
                        if "content" in f:
                            file_contents[f.get("path", "")] = f["content"]

            # Basic analysis without LLM
            findings = []
            for fpath, content in file_contents.items():
                lines = content.split("\n")
                for i, line in enumerate(lines, 1):
                    stripped = line.strip()
                    if any(m in stripped for m in ("TODO", "FIXME", "HACK", "XXX")):
                        if stripped.startswith(("#", "//")):
                            findings.append({
                                "category": "issue",
                                "severity": "low",
                                "title": f"TODO marker in {Path(fpath).name}",
                                "description": stripped[:200],
                                "file": fpath,
                                "line": i,
                            })

            outputs.append({
                "region": region.name,
                "files_analyzed": list(file_contents.keys()),
                "summary": f"Direct scan of {region.name}: {len(file_contents)} files read, {len(findings)} issues found",
                "findings": findings,
                "architecture": {
                    "files_count": len(file_contents),
                    "tree": tree.get("tree", "")[:500],
                },
                "recommendations": [],
                "dependencies": [],
            })

        except Exception as e:
            logger.warning(f"Direct scan of region {region.name} failed: {e}")
            outputs.append({
                "region": region.name,
                "error": str(e),
                "summary": f"Scan failed: {e}",
            })

    return outputs


# ── Tool Registration ───────────────────────────────────────────────

def register_scanner_tools(registry) -> None:
    """Register the sub-agent scanner tool."""

    registry.register(
        definition=ToolDefinition(
            name="scan_codebase",
            description=(
                "Deep codebase analysis using sub-agent scanning. Spawns multiple "
                "scanner sub-agents IN PARALLEL to analyze different parts of a "
                "codebase, then synthesizes their findings into a comprehensive report.\n\n"
                "Goal presets: 'architecture' (design patterns), 'security' (vulnerabilities), "
                "'quality' (code health), 'understand' (deep comprehension), 'plan' (implementation planning).\n\n"
                "Or pass a custom goal string for focused analysis.\n\n"
                "Each sub-agent READS and REASONS about code — this is LLM-powered semantic "
                "analysis, not just pattern matching."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the codebase root (host filesystem path)",
                    },
                    "goal": {
                        "type": "string",
                        "description": (
                            "Scanning goal: a preset name (architecture, security, quality, "
                            "understand, plan) or a custom analysis prompt"
                        ),
                        "default": "understand",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Tree traversal depth for project discovery (default 4)",
                        "default": 4,
                    },
                    "max_regions": {
                        "type": "integer",
                        "description": "Maximum parallel scanner sub-agents (default 5, max 8)",
                        "default": 5,
                    },
                },
                "required": ["path"],
            },
            risk_level=RiskLevel.MEDIUM,
            requires_approval=False,
            timeout_seconds=600,  # 10 minutes — scanning is inherently slow
            category="analysis",
        ),
        handler=scan_codebase,
    )

    registry.register(
        definition=ToolDefinition(
            name="scan_region",
            description=(
                "Scan a SPECIFIC region/directory of a codebase with a focused goal. "
                "Use this for targeted deep dives into a single module or directory "
                "rather than scanning the entire project. Spawns a single scanner "
                "sub-agent for the specified region."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the specific directory to scan",
                    },
                    "goal": {
                        "type": "string",
                        "description": "What to analyze in this region",
                    },
                },
                "required": ["path", "goal"],
            },
            risk_level=RiskLevel.MEDIUM,
            requires_approval=False,
            timeout_seconds=300,
            category="analysis",
        ),
        handler=scan_region,
    )


async def scan_region(
    path: str,
    goal: str,
    workspace_id: str = "default",
) -> dict:
    """
    Scan a single region/directory with a focused goal.
    Spawns one scanner sub-agent for targeted deep analysis.
    """
    return await scan_codebase(
        path=path,
        goal=goal,
        depth=3,
        max_regions=1,
        workspace_id=workspace_id,
    )
