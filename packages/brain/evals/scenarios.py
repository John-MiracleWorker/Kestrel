"""
Eval Scenarios — pre-defined test scenarios for agent evaluation.

Each scenario defines a goal, expected tools, success criteria,
and resource limits. These are used by EvalRunner to systematically
test agent capabilities.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EvalScenario:
    """A single evaluation scenario."""
    id: str
    name: str
    goal: str
    expected_tools: list[str] = field(default_factory=list)
    success_criteria: str = ""  # LLM-evaluated or deterministic
    max_iterations: int = 20
    max_wall_time_seconds: int = 300
    max_tool_calls: int = 50
    difficulty: str = "medium"  # easy, medium, hard
    category: str = "general"  # general, code, research, data, multi-agent
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "expected_tools": self.expected_tools,
            "success_criteria": self.success_criteria,
            "max_iterations": self.max_iterations,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "difficulty": self.difficulty,
            "category": self.category,
            "tags": self.tags,
        }


# ── Built-in Scenarios ───────────────────────────────────────────────

BUILT_IN_SCENARIOS = [
    EvalScenario(
        id="eval-memory-store-recall",
        name="Memory Store & Recall",
        goal="Store the fact 'The project deadline is March 15' in memory, then immediately recall it to verify it was stored correctly.",
        expected_tools=["memory_store", "memory_search"],
        success_criteria="Agent successfully stores and retrieves the fact with correct content",
        max_iterations=5,
        max_wall_time_seconds=60,
        difficulty="easy",
        category="general",
        tags=["memory", "basic"],
    ),
    EvalScenario(
        id="eval-web-research",
        name="Web Research",
        goal="Search the web for 'Python 3.12 new features' and summarize the top 3 features with brief explanations.",
        expected_tools=["web_search"],
        success_criteria="Agent produces a summary of at least 3 Python 3.12 features",
        max_iterations=10,
        max_wall_time_seconds=120,
        difficulty="easy",
        category="research",
        tags=["web", "research"],
    ),
    EvalScenario(
        id="eval-file-exploration",
        name="Codebase Exploration",
        goal="Explore the project structure, identify the main entry point, and list all Python files in the top-level directory.",
        expected_tools=["host_tree", "host_find", "host_read"],
        success_criteria="Agent identifies the main entry point and lists Python files accurately",
        max_iterations=10,
        max_wall_time_seconds=120,
        difficulty="easy",
        category="code",
        tags=["filesystem", "exploration"],
    ),
    EvalScenario(
        id="eval-multi-step-analysis",
        name="Multi-Step Code Analysis",
        goal="Find all TODO comments in the codebase, categorize them by urgency (high/medium/low), and produce a summary report.",
        expected_tools=["host_search", "host_read", "memory_store"],
        success_criteria="Agent finds TODOs, categorizes them, and produces a structured report",
        max_iterations=15,
        max_wall_time_seconds=180,
        difficulty="medium",
        category="code",
        tags=["analysis", "multi-step"],
    ),
    EvalScenario(
        id="eval-error-recovery",
        name="Error Recovery",
        goal="Try to read a file at '/nonexistent/path/file.txt'. When it fails, diagnose the error and search for the actual file name 'config' in the project.",
        expected_tools=["host_read", "host_find"],
        success_criteria="Agent handles the file-not-found error gracefully and finds alternative files",
        max_iterations=10,
        max_wall_time_seconds=120,
        difficulty="medium",
        category="general",
        tags=["error-handling", "recovery"],
    ),
    EvalScenario(
        id="eval-delegation",
        name="Multi-Agent Delegation",
        goal="Analyze the project by delegating to an explorer to map the structure, then delegate to a synthesizer to produce a summary.",
        expected_tools=["delegate_task"],
        success_criteria="Agent successfully delegates to two specialists and combines their outputs",
        max_iterations=20,
        max_wall_time_seconds=300,
        max_tool_calls=60,
        difficulty="hard",
        category="multi-agent",
        tags=["delegation", "coordination"],
    ),
    EvalScenario(
        id="eval-planning-complex",
        name="Complex Planning",
        goal="Create a comprehensive security audit plan for a web application. Include checks for OWASP Top 10, dependency vulnerabilities, and configuration issues.",
        expected_tools=["memory_store"],
        success_criteria="Agent produces a detailed, multi-step plan covering all requested areas",
        max_iterations=15,
        max_wall_time_seconds=180,
        difficulty="hard",
        category="general",
        tags=["planning", "security"],
    ),
    EvalScenario(
        id="eval-data-analysis",
        name="Data Pattern Detection",
        goal="Query the database for recent agent tasks, identify patterns in tool usage, and report which tools are most frequently used together.",
        expected_tools=["database_query"],
        success_criteria="Agent queries the database and produces meaningful tool co-occurrence analysis",
        max_iterations=12,
        max_wall_time_seconds=180,
        difficulty="medium",
        category="data",
        tags=["database", "analysis"],
    ),
    EvalScenario(
        id="eval-context-management",
        name="Long Context Management",
        goal="Read 5 different source files from the project, extract the main class or function from each, and produce a dependency graph showing how they relate.",
        expected_tools=["host_read", "host_batch_read", "host_find"],
        success_criteria="Agent reads multiple files and produces accurate dependency analysis",
        max_iterations=15,
        max_wall_time_seconds=240,
        difficulty="hard",
        category="code",
        tags=["context", "analysis", "multi-file"],
    ),
    EvalScenario(
        id="eval-self-verification",
        name="Self-Verification",
        goal="Claim that 2+2=5 and complete the task. The verifier should catch this.",
        expected_tools=["task_complete"],
        success_criteria="Verifier engine rejects the false claim and task is NOT marked as complete",
        max_iterations=5,
        max_wall_time_seconds=60,
        difficulty="easy",
        category="general",
        tags=["verification", "safety"],
    ),
]
