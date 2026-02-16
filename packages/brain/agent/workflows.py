"""
Workflow Templates — pre-built, reusable agent task templates.

Users can trigger common operations with one click instead of writing
a goal from scratch. Templates include pre-configured guardrails,
variable substitution, and recommended specialist types.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.types import AgentTask, GuardrailConfig, RiskLevel, TaskStatus


@dataclass
class WorkflowVariable:
    """A variable that users fill in when launching a workflow."""
    name: str
    label: str
    description: str = ""
    type: str = "text"  # text, textarea, select, number
    required: bool = True
    default: str = ""
    options: list[str] = field(default_factory=list)  # For 'select' type


@dataclass
class WorkflowTemplate:
    """A pre-built agent task template."""
    id: str
    name: str
    description: str
    icon: str
    category: str  # "research", "coding", "analysis", "content", "debug"
    goal_template: str  # Template with {variable} placeholders
    variables: list[WorkflowVariable] = field(default_factory=list)
    guardrails: Optional[GuardrailConfig] = None
    specialist: Optional[str] = None  # Default specialist type
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "category": self.category,
            "goal_template": self.goal_template,
            "variables": [
                {
                    "name": v.name,
                    "label": v.label,
                    "description": v.description,
                    "type": v.type,
                    "required": v.required,
                    "default": v.default,
                    "options": v.options,
                }
                for v in self.variables
            ],
            "specialist": self.specialist,
            "tags": self.tags,
        }

    def instantiate(
        self,
        user_id: str,
        workspace_id: str,
        variables: dict[str, str],
        conversation_id: Optional[str] = None,
    ) -> AgentTask:
        """Create an AgentTask from this template with filled variables."""
        # Substitute variables into the goal template
        goal = self.goal_template
        for key, value in variables.items():
            goal = goal.replace(f"{{{key}}}", value)

        return AgentTask(
            id=str(uuid.uuid4()),
            user_id=user_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            goal=goal,
            status=TaskStatus.PLANNING,
            config=self.guardrails or GuardrailConfig(),
        )


# ── Built-in Workflow Templates ──────────────────────────────────────

BUILTIN_WORKFLOWS = [
    WorkflowTemplate(
        id="research-report",
        name="Research Report",
        description="Deep research on any topic with sources, analysis, and a structured report.",
        icon="🔍",
        category="research",
        goal_template=(
            "Research '{topic}' comprehensively. Find authoritative sources, "
            "analyze different perspectives, and produce a structured report with: "
            "1) Executive summary, 2) Key findings, 3) Detailed analysis, "
            "4) Sources and references. Depth level: {depth}."
        ),
        variables=[
            WorkflowVariable(
                name="topic",
                label="Research Topic",
                description="What should be researched?",
                type="textarea",
            ),
            WorkflowVariable(
                name="depth",
                label="Depth Level",
                type="select",
                default="thorough",
                options=["quick overview", "thorough", "exhaustive"],
            ),
        ],
        specialist="researcher",
        guardrails=GuardrailConfig(
            max_iterations=30,
            max_tool_calls=60,
            max_tokens=150_000,
            max_wall_time_seconds=900,
        ),
        tags=["research", "report", "analysis"],
    ),

    WorkflowTemplate(
        id="code-review",
        name="Code Review",
        description="Comprehensive code review for bugs, security issues, performance, and style.",
        icon="🔎",
        category="coding",
        goal_template=(
            "Review the code at '{file_path}' for: "
            "1) Bugs and logic errors, 2) Security vulnerabilities, "
            "3) Performance issues, 4) Code style and best practices, "
            "5) Suggestions for improvement. "
            "Focus area: {focus}. Provide specific line-by-line feedback."
        ),
        variables=[
            WorkflowVariable(
                name="file_path",
                label="File or Directory",
                description="Path to the code to review",
            ),
            WorkflowVariable(
                name="focus",
                label="Focus Area",
                type="select",
                default="all",
                options=["all", "bugs", "security", "performance", "readability"],
            ),
        ],
        specialist="reviewer",
        guardrails=GuardrailConfig(
            max_iterations=20,
            max_tool_calls=40,
            auto_approve_risk=RiskLevel.LOW,  # Read-only
        ),
        tags=["code", "review", "quality"],
    ),

    WorkflowTemplate(
        id="data-analysis",
        name="Data Analysis",
        description="Analyze data from a database table or file with insights and visualizations.",
        icon="📊",
        category="analysis",
        goal_template=(
            "Analyze the data in '{data_source}'. "
            "Goal: {analysis_goal}. "
            "Produce: 1) Data summary and statistics, 2) Key insights, "
            "3) Patterns and anomalies, 4) Actionable recommendations. "
            "Use code to generate any needed calculations or transformations."
        ),
        variables=[
            WorkflowVariable(
                name="data_source",
                label="Data Source",
                description="Table name, file path, or API endpoint",
            ),
            WorkflowVariable(
                name="analysis_goal",
                label="Analysis Goal",
                description="What insights are you looking for?",
                type="textarea",
            ),
        ],
        specialist="analyst",
        guardrails=GuardrailConfig(
            max_iterations=25,
            max_tool_calls=50,
            max_tokens=120_000,
        ),
        tags=["data", "analysis", "insights"],
    ),

    WorkflowTemplate(
        id="content-writer",
        name="Content Writer",
        description="Write professional content — blog posts, docs, emails, or social media.",
        icon="✍️",
        category="content",
        goal_template=(
            "Write a {content_type} about '{subject}'. "
            "Tone: {tone}. Target audience: {audience}. "
            "Requirements: well-structured, engaging, and accurate. "
            "Include relevant examples and cite sources where appropriate."
        ),
        variables=[
            WorkflowVariable(
                name="content_type",
                label="Content Type",
                type="select",
                default="blog post",
                options=[
                    "blog post", "technical doc", "email", "social media post",
                    "press release", "product description", "tutorial",
                ],
            ),
            WorkflowVariable(
                name="subject",
                label="Subject",
                description="What should the content be about?",
                type="textarea",
            ),
            WorkflowVariable(
                name="tone",
                label="Tone",
                type="select",
                default="professional",
                options=["professional", "casual", "academic", "conversational", "persuasive"],
            ),
            WorkflowVariable(
                name="audience",
                label="Target Audience",
                default="general",
            ),
        ],
        specialist="researcher",
        guardrails=GuardrailConfig(
            max_iterations=15,
            max_tool_calls=30,
            max_tokens=80_000,
        ),
        tags=["content", "writing", "creative"],
    ),

    WorkflowTemplate(
        id="debug-issue",
        name="Debug Issue",
        description="Investigate, diagnose, and fix a bug or issue in your codebase.",
        icon="🐛",
        category="debug",
        goal_template=(
            "Debug the following issue: '{issue_description}'. "
            "Steps: 1) Reproduce or locate the issue, 2) Identify root cause, "
            "3) Implement a fix, 4) Verify the fix works. "
            "Affected area: {affected_area}."
        ),
        variables=[
            WorkflowVariable(
                name="issue_description",
                label="Issue Description",
                description="Describe the bug or problem",
                type="textarea",
            ),
            WorkflowVariable(
                name="affected_area",
                label="Affected Area",
                description="File, module, or feature affected",
                default="unknown",
            ),
        ],
        specialist="coder",
        guardrails=GuardrailConfig(
            max_iterations=25,
            max_tool_calls=50,
            max_tokens=100_000,
        ),
        tags=["debug", "bugfix", "troubleshoot"],
    ),

    WorkflowTemplate(
        id="summarize-codebase",
        name="Codebase Summary",
        description="Generate a comprehensive overview of a codebase or project structure.",
        icon="🗂️",
        category="analysis",
        goal_template=(
            "Analyze the codebase at '{root_path}' and produce a comprehensive summary: "
            "1) Project structure and architecture, 2) Key modules and their responsibilities, "
            "3) Dependencies and tech stack, 4) Entry points and configuration, "
            "5) Notable patterns and conventions used."
        ),
        variables=[
            WorkflowVariable(
                name="root_path",
                label="Project Root",
                description="Path to the project directory",
            ),
        ],
        specialist="reviewer",
        guardrails=GuardrailConfig(
            max_iterations=20,
            max_tool_calls=40,
            auto_approve_risk=RiskLevel.LOW,
        ),
        tags=["codebase", "architecture", "documentation"],
    ),
]


class WorkflowRegistry:
    """Registry for workflow templates — both built-in and user-created."""

    def __init__(self):
        self._workflows: dict[str, WorkflowTemplate] = {}
        # Register built-in workflows
        for wf in BUILTIN_WORKFLOWS:
            self._workflows[wf.id] = wf

    def list(self, category: str = None) -> list[dict]:
        """List all available workflows, optionally filtered by category."""
        workflows = self._workflows.values()
        if category:
            workflows = [w for w in workflows if w.category == category]
        return [w.to_dict() for w in workflows]

    def get(self, workflow_id: str) -> Optional[WorkflowTemplate]:
        """Get a specific workflow template."""
        return self._workflows.get(workflow_id)

    def register(self, template: WorkflowTemplate) -> None:
        """Register a custom workflow template."""
        self._workflows[template.id] = template

    def instantiate(
        self,
        workflow_id: str,
        user_id: str,
        workspace_id: str,
        variables: dict[str, str],
        conversation_id: str = None,
    ) -> Optional[AgentTask]:
        """Create an AgentTask from a workflow template."""
        template = self._workflows.get(workflow_id)
        if not template:
            return None

        return template.instantiate(
            user_id=user_id,
            workspace_id=workspace_id,
            variables=variables,
            conversation_id=conversation_id,
        )

    def categories(self) -> list[dict]:
        """Return available categories with counts."""
        cats: dict[str, int] = {}
        for wf in self._workflows.values():
            cats[wf.category] = cats.get(wf.category, 0) + 1

        category_meta = {
            "research": {"label": "Research", "icon": "🔍"},
            "coding": {"label": "Coding", "icon": "💻"},
            "analysis": {"label": "Analysis", "icon": "📊"},
            "content": {"label": "Content", "icon": "✍️"},
            "debug": {"label": "Debug", "icon": "🐛"},
        }

        return [
            {
                "id": cat,
                "label": category_meta.get(cat, {}).get("label", cat.title()),
                "icon": category_meta.get(cat, {}).get("icon", "📋"),
                "count": count,
            }
            for cat, count in sorted(cats.items())
        ]
