"""
Natural Language Automation Builder — describe workflows in plain English,
Kestrel generates, validates, and saves as persistent cron jobs.

Converts: "Every weekday at 8am check my GitHub repos for new issues"
Into:     CronJob(cron_expression="0 8 * * 1-5", goal="Check GitHub repos...")
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("brain.agent.nl_automation")


# ── NL-to-Cron Mapping ──────────────────────────────────────────────

# Common natural language patterns → cron expressions
NL_CRON_PATTERNS = [
    # Every X minutes/hours
    (r"every\s+(\d+)\s+minutes?", lambda m: f"*/{m.group(1)} * * * *"),
    (r"every\s+(\d+)\s+hours?", lambda m: f"0 */{m.group(1)} * * *"),
    (r"every\s+hour", lambda _: "0 * * * *"),

    # Daily
    (r"every\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
     lambda m: _daily_cron(m)),
    (r"daily\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
     lambda m: _daily_cron(m)),
    (r"every\s+morning", lambda _: "0 8 * * *"),
    (r"every\s+evening", lambda _: "0 18 * * *"),
    (r"every\s+night", lambda _: "0 22 * * *"),

    # Weekday patterns
    (r"every\s+weekday\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
     lambda m: _weekday_cron(m)),
    (r"every\s+monday", lambda _: "0 9 * * 1"),
    (r"every\s+tuesday", lambda _: "0 9 * * 2"),
    (r"every\s+wednesday", lambda _: "0 9 * * 3"),
    (r"every\s+thursday", lambda _: "0 9 * * 4"),
    (r"every\s+friday", lambda _: "0 9 * * 5"),
    (r"every\s+saturday", lambda _: "0 10 * * 6"),
    (r"every\s+sunday", lambda _: "0 10 * * 0"),

    # Weekly
    (r"every\s+week", lambda _: "0 9 * * 1"),
    (r"weekly", lambda _: "0 9 * * 1"),
]


def _parse_hour(match) -> int:
    """Extract hour from regex match accounting for AM/PM."""
    hour = int(match.group(1))
    ampm = match.group(3) if match.lastindex >= 3 else None
    if ampm and ampm.lower() == "pm" and hour < 12:
        hour += 12
    elif ampm and ampm.lower() == "am" and hour == 12:
        hour = 0
    return hour


def _parse_minute(match) -> int:
    """Extract minute from regex match."""
    if match.lastindex >= 2 and match.group(2):
        return int(match.group(2))
    return 0


def _daily_cron(match) -> str:
    return f"{_parse_minute(match)} {_parse_hour(match)} * * *"


def _weekday_cron(match) -> str:
    return f"{_parse_minute(match)} {_parse_hour(match)} * * 1-5"


def extract_cron_from_nl(text: str) -> Optional[str]:
    """
    Try to extract a cron expression from natural language.
    Returns None if no pattern matches.
    """
    lower = text.lower().strip()
    for pattern, builder in NL_CRON_PATTERNS:
        match = re.search(pattern, lower)
        if match:
            return builder(match)
    return None


# ── LLM-Powered Builder ─────────────────────────────────────────────

BUILD_PROMPT = """\
You are an automation builder. Given a natural language description of a
recurring task, extract the following JSON:

{{
  "name": "short_snake_case_name",
  "description": "One-line human-readable description",
  "cron_expression": "standard 5-field cron expression",
  "goal": "Detailed goal the agent should execute each time this triggers"
}}

User request: {request}

Rules:
- The "goal" should be a complete, standalone instruction for an autonomous AI agent.
- Include context the agent will need (repos, APIs, channels, etc).
- Output ONLY valid JSON, no markdown fences.
"""


@dataclass
class AutomationSpec:
    """Parsed automation specification from natural language."""
    name: str
    description: str
    cron_expression: str
    goal: str
    raw_request: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "cron_expression": self.cron_expression,
            "goal": self.goal,
        }


class AutomationBuilder:
    """
    Converts natural language automation descriptions into CronJob specs.

    Two-tier approach:
      1. Fast regex extraction for common patterns (no LLM call)
      2. LLM fallback for complex descriptions
    """

    def __init__(self, llm_provider=None, model: str = ""):
        self._provider = llm_provider
        self._model = model

    async def build(self, request: str) -> AutomationSpec:
        """
        Parse a natural language automation request into an AutomationSpec.

        Args:
            request: e.g. "Every weekday at 8am check my top GitHub repos"

        Returns:
            AutomationSpec with name, description, cron_expression, goal
        """
        # Tier 1: fast regex extraction
        cron = extract_cron_from_nl(request)
        if cron:
            # Build a simple spec from the regex match
            name = re.sub(r"[^a-z0-9]+", "_", request.lower()[:40]).strip("_")
            return AutomationSpec(
                name=name,
                description=request[:100],
                cron_expression=cron,
                goal=request,
                raw_request=request,
            )

        # Tier 2: LLM extraction
        if not self._provider:
            raise ValueError(
                "Cannot parse complex automation request without LLM provider. "
                "Try using common patterns like 'every weekday at 8am...' "
                "or 'every 30 minutes...'."
            )

        prompt = BUILD_PROMPT.format(request=request)
        response = await self._provider.generate(
            messages=[{"role": "user", "content": prompt}],
            model=self._model,
            temperature=0.2,
            max_tokens=1024,
        )

        text = response.get("content", "")
        # Strip markdown fences if present
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*$", "", text)

        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM returned invalid JSON for automation: {e}")

        return AutomationSpec(
            name=data.get("name", "custom_automation"),
            description=data.get("description", request[:100]),
            cron_expression=data.get("cron_expression", ""),
            goal=data.get("goal", request),
            raw_request=request,
        )

    async def validate(self, spec: AutomationSpec) -> list[str]:
        """
        Validate an AutomationSpec. Returns list of issues (empty = valid).
        """
        issues = []

        if not spec.cron_expression:
            issues.append("Missing cron expression")
        else:
            # Validate cron field count
            parts = spec.cron_expression.strip().split()
            if len(parts) != 5:
                issues.append(
                    f"Cron expression must have 5 fields, got {len(parts)}: "
                    f"'{spec.cron_expression}'"
                )

        if not spec.goal:
            issues.append("Missing automation goal")

        if not spec.name:
            issues.append("Missing automation name")

        return issues

    async def preview(self, spec: AutomationSpec) -> dict:
        """
        Generate a human-readable preview of what the automation will do.
        """
        from cron_parser import describe_cron
        try:
            schedule_desc = describe_cron(spec.cron_expression)
        except Exception:
            schedule_desc = spec.cron_expression

        return {
            "name": spec.name,
            "schedule": schedule_desc,
            "what_it_does": spec.goal,
            "cron_expression": spec.cron_expression,
        }
