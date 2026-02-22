"""
Adaptive Persona System — agents that learn and evolve to match
each user's preferences over time.

Unlike static system prompts, Kestrel's personas are living profiles:
  - Learns coding style preferences (formatting, naming, patterns)
  - Tracks communication tone (concise vs verbose, formal vs casual)
  - Observes tool usage patterns (which tools the user prefers)
  - Notes temporal patterns (when the user works, what they do first)
  - Adjusts behavior based on positive/negative feedback signals

The persona persists across conversations and continuously refines
itself. Users don't need to re-explain their preferences.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("brain.agent.persona")


@dataclass
class PreferenceSignal:
    """A single observed preference signal from user behavior."""
    category: str           # "code_style", "communication", "tools", "workflow"
    key: str                # Specific preference (e.g., "naming_convention")
    value: str              # Observed preference (e.g., "snake_case")
    confidence: float       # 0.0–1.0 how sure we are
    evidence: str = ""      # What behavior led to this inference
    observed_at: str = ""
    source: str = ""        # "explicit" (user told us), "inferred" (we observed)


@dataclass
class UserPreferences:
    """Aggregated user preferences learned over time."""

    # Code style
    naming_convention: str = ""           # snake_case, camelCase, PascalCase
    indentation: str = ""                 # tabs, 2_spaces, 4_spaces
    preferred_languages: list[str] = field(default_factory=list)
    framework_preferences: dict[str, str] = field(default_factory=dict)
    comment_style: str = ""               # minimal, moderate, verbose
    test_preference: str = ""             # tdd, post_hoc, minimal

    # Communication
    verbosity: str = "moderate"           # concise, moderate, verbose
    tone: str = "professional"            # casual, professional, formal, friendly
    explanation_depth: str = "moderate"   # shallow, moderate, deep
    prefers_examples: bool = True
    prefers_alternatives: bool = False
    emoji_usage: str = "moderate"         # none, minimal, moderate, heavy

    # Workflow
    preferred_tools: list[str] = field(default_factory=list)
    avoided_tools: list[str] = field(default_factory=list)
    approval_threshold: str = "medium"    # low (approve everything), medium, high (cautious)
    prefers_checkpoints: bool = True
    batch_vs_incremental: str = "incremental"  # batch, incremental

    # Temporal
    active_hours: list[int] = field(default_factory=list)   # UTC hours when user is active
    peak_productivity_hour: Optional[int] = None
    typical_session_minutes: int = 0

    def to_dict(self) -> dict:
        return {
            "code_style": {
                "naming": self.naming_convention,
                "indent": self.indentation,
                "languages": self.preferred_languages,
                "frameworks": self.framework_preferences,
                "comments": self.comment_style,
                "testing": self.test_preference,
            },
            "communication": {
                "verbosity": self.verbosity,
                "tone": self.tone,
                "depth": self.explanation_depth,
                "examples": self.prefers_examples,
                "alternatives": self.prefers_alternatives,
                "emoji": self.emoji_usage,
            },
            "workflow": {
                "preferred_tools": self.preferred_tools,
                "avoided_tools": self.avoided_tools,
                "approval_threshold": self.approval_threshold,
                "checkpoints": self.prefers_checkpoints,
                "style": self.batch_vs_incremental,
            },
            "temporal": {
                "active_hours": self.active_hours,
                "peak_hour": self.peak_productivity_hour,
                "avg_session_min": self.typical_session_minutes,
            },
        }


class PersonaLearner:
    """
    Learns user preferences from conversations and adapts agent behavior.

    Observation sources:
      - Explicit feedback ("I prefer camelCase")
      - Approval/rejection patterns (what they approve vs reject)
      - Code edits (style patterns in their actual code)
      - Tool choices (which tools they invoke manually)
      - Response reactions (did they ask for more detail or less?)
      - Temporal patterns (when they're active, session length)

    The persona is persisted per user and loaded at conversation start.
    """

    # Minimum confidence to apply a preference
    MIN_CONFIDENCE = 0.6
    # Number of observations before a preference is "established"
    ESTABLISHMENT_THRESHOLD = 3

    def __init__(self, pool):
        self._pool = pool
        self._observations: dict[str, list[PreferenceSignal]] = {}
        self._preferences: dict[str, UserPreferences] = {}

    async def load_persona(self, user_id: str) -> UserPreferences:
        """Load a user's persona from the database."""
        if user_id in self._preferences:
            return self._preferences[user_id]

        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT preferences FROM user_personas WHERE user_id = $1",
                    user_id,
                )

            if row and row["preferences"]:
                prefs = self._deserialize_preferences(row["preferences"])
            else:
                prefs = UserPreferences()

            self._preferences[user_id] = prefs
            return prefs

        except Exception as e:
            logger.error(f"Failed to load persona for {user_id}: {e}")
            prefs = UserPreferences()
            self._preferences[user_id] = prefs
            return prefs

    async def observe(
        self,
        user_id: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.7,
        evidence: str = "",
        source: str = "inferred",
    ) -> None:
        """Record a preference observation."""
        signal = PreferenceSignal(
            category=category,
            key=key,
            value=value,
            confidence=confidence,
            evidence=evidence,
            observed_at=datetime.now(timezone.utc).isoformat(),
            source=source,
        )

        if user_id not in self._observations:
            self._observations[user_id] = []
        self._observations[user_id].append(signal)

        # Check if we should update the preference
        await self._maybe_update_preference(user_id, signal)

    async def observe_code_style(self, user_id: str, code: str) -> None:
        """Infer code style preferences from user's code."""
        # Naming convention
        if "_" in code and not any(c.isupper() for c in code.split("_")[-1][:1]):
            await self.observe(user_id, "code_style", "naming_convention", "snake_case",
                             confidence=0.6, evidence="underscore naming detected")
        elif any(c.isupper() for c in code[1:] if c.isalpha()):
            await self.observe(user_id, "code_style", "naming_convention", "camelCase",
                             confidence=0.6, evidence="camelCase naming detected")

        # Indentation
        lines = code.split("\n")
        for line in lines:
            stripped = line.lstrip()
            if stripped and line != stripped:
                indent = line[:len(line) - len(stripped)]
                if "\t" in indent:
                    await self.observe(user_id, "code_style", "indentation", "tabs",
                                     confidence=0.8, evidence="tab indentation")
                elif indent == "  ":
                    await self.observe(user_id, "code_style", "indentation", "2_spaces",
                                     confidence=0.7, evidence="2-space indentation")
                elif indent == "    ":
                    await self.observe(user_id, "code_style", "indentation", "4_spaces",
                                     confidence=0.7, evidence="4-space indentation")
                break

        # Comment density
        comment_lines = sum(1 for l in lines if l.strip().startswith(("#", "//", "/*", "*")))
        total_lines = max(len(lines), 1)
        ratio = comment_lines / total_lines
        if ratio > 0.2:
            await self.observe(user_id, "code_style", "comment_style", "verbose",
                             confidence=0.5, evidence=f"comment ratio {ratio:.0%}")
        elif ratio < 0.05:
            await self.observe(user_id, "code_style", "comment_style", "minimal",
                             confidence=0.5, evidence=f"comment ratio {ratio:.0%}")

    async def observe_communication(
        self,
        user_id: str,
        user_message: str,
        agent_response: str,
        user_reaction: str = "",
    ) -> None:
        """Infer communication preferences from interaction patterns."""
        msg_len = len(user_message)

        # Verbosity inference from message length
        if msg_len < 50:
            await self.observe(user_id, "communication", "verbosity", "concise",
                             confidence=0.4, evidence="short messages")
        elif msg_len > 300:
            await self.observe(user_id, "communication", "verbosity", "verbose",
                             confidence=0.4, evidence="detailed messages")

        # Tone inference
        msg_lower = user_message.lower()
        if any(w in msg_lower for w in ["please", "thank", "could you", "would you"]):
            await self.observe(user_id, "communication", "tone", "professional",
                             confidence=0.5, evidence="polite language")
        elif any(w in msg_lower for w in ["just", "quick", "asap", "now"]):
            await self.observe(user_id, "communication", "tone", "casual",
                             confidence=0.5, evidence="casual/urgent language")

        # Reaction-based learning
        if user_reaction == "asked_for_more_detail":
            await self.observe(user_id, "communication", "explanation_depth", "deep",
                             confidence=0.8, evidence="user requested more detail",
                             source="explicit")
        elif user_reaction == "asked_for_less":
            await self.observe(user_id, "communication", "explanation_depth", "shallow",
                             confidence=0.8, evidence="user requested less detail",
                             source="explicit")

    async def observe_tool_usage(self, user_id: str, tool_name: str, chosen_by: str = "agent") -> None:
        """Track which tools are used and preferred."""
        if chosen_by == "user":
            await self.observe(user_id, "workflow", "preferred_tool", tool_name,
                             confidence=0.9, evidence="user explicitly chose tool",
                             source="explicit")

    async def observe_approval(self, user_id: str, approved: bool, action_type: str = "") -> None:
        """Learn from approval/rejection patterns."""
        if not approved:
            await self.observe(user_id, "workflow", "approval_threshold", "high",
                             confidence=0.4, evidence=f"rejected {action_type}")
        else:
            await self.observe(user_id, "workflow", "approval_threshold", "low",
                             confidence=0.3, evidence=f"approved {action_type}")

    async def observe_session_timing(self, user_id: str) -> None:
        """Track when users are active."""
        now = datetime.now(timezone.utc)
        await self.observe(user_id, "temporal", "active_hour", str(now.hour),
                         confidence=0.3, evidence=f"active at {now.hour}:00 UTC")

    async def _maybe_update_preference(self, user_id: str, signal: PreferenceSignal) -> None:
        """Update preference if we have enough confidence."""
        observations = self._observations.get(user_id, [])

        # Count matching observations for this key
        matching = [
            o for o in observations
            if o.category == signal.category
            and o.key == signal.key
            and o.value == signal.value
        ]

        # Explicit signals are immediately applied
        if signal.source == "explicit" and signal.confidence >= self.MIN_CONFIDENCE:
            await self._apply_preference(user_id, signal)
            return

        # Inferred signals need multiple observations
        if len(matching) >= self.ESTABLISHMENT_THRESHOLD:
            avg_confidence = sum(o.confidence for o in matching) / len(matching)
            if avg_confidence >= self.MIN_CONFIDENCE:
                await self._apply_preference(user_id, signal)

    async def _apply_preference(self, user_id: str, signal: PreferenceSignal) -> None:
        """Apply a learned preference to the user's profile."""
        prefs = await self.load_persona(user_id)

        # Map signal to preference field
        mapping = {
            ("code_style", "naming_convention"): "naming_convention",
            ("code_style", "indentation"): "indentation",
            ("code_style", "comment_style"): "comment_style",
            ("communication", "verbosity"): "verbosity",
            ("communication", "tone"): "tone",
            ("communication", "explanation_depth"): "explanation_depth",
            ("workflow", "approval_threshold"): "approval_threshold",
        }

        field_name = mapping.get((signal.category, signal.key))
        if field_name and hasattr(prefs, field_name):
            setattr(prefs, field_name, signal.value)
            logger.info(f"Persona updated for {user_id}: {field_name} = {signal.value}")

        # Handle list-type preferences
        if signal.key == "preferred_tool" and signal.value not in prefs.preferred_tools:
            prefs.preferred_tools.append(signal.value)
            prefs.preferred_tools = prefs.preferred_tools[-20:]  # Keep last 20

        if signal.key == "active_hour":
            hour = int(signal.value)
            if hour not in prefs.active_hours:
                prefs.active_hours.append(hour)
                prefs.active_hours.sort()

        # Persist
        await self._save_persona(user_id, prefs)

    async def _save_persona(self, user_id: str, prefs: UserPreferences) -> None:
        """Persist the persona to the database."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_personas (user_id, preferences, updated_at)
                    VALUES ($1, $2::jsonb, $3)
                    ON CONFLICT (user_id) DO UPDATE SET
                        preferences = $2::jsonb, updated_at = $3
                    """,
                    user_id, json.dumps(prefs.to_dict()),
                    datetime.now(timezone.utc),
                )
        except Exception as e:
            logger.error(f"Failed to save persona: {e}")

    def _deserialize_preferences(self, data: Any) -> UserPreferences:
        """Deserialize preferences from JSON storage."""
        if isinstance(data, str):
            data = json.loads(data)

        prefs = UserPreferences()
        if not isinstance(data, dict):
            return prefs

        cs = data.get("code_style", {})
        prefs.naming_convention = cs.get("naming", "")
        prefs.indentation = cs.get("indent", "")
        prefs.preferred_languages = cs.get("languages", [])
        prefs.framework_preferences = cs.get("frameworks", {})
        prefs.comment_style = cs.get("comments", "")
        prefs.test_preference = cs.get("testing", "")

        cm = data.get("communication", {})
        prefs.verbosity = cm.get("verbosity", "moderate")
        prefs.tone = cm.get("tone", "professional")
        prefs.explanation_depth = cm.get("depth", "moderate")
        prefs.prefers_examples = cm.get("examples", True)
        prefs.prefers_alternatives = cm.get("alternatives", False)
        prefs.emoji_usage = cm.get("emoji", "moderate")

        wf = data.get("workflow", {})
        prefs.preferred_tools = wf.get("preferred_tools", [])
        prefs.avoided_tools = wf.get("avoided_tools", [])
        prefs.approval_threshold = wf.get("approval_threshold", "medium")
        prefs.prefers_checkpoints = wf.get("checkpoints", True)
        prefs.batch_vs_incremental = wf.get("style", "incremental")

        tp = data.get("temporal", {})
        prefs.active_hours = tp.get("active_hours", [])
        prefs.peak_productivity_hour = tp.get("peak_hour")
        prefs.typical_session_minutes = tp.get("avg_session_min", 0)

        return prefs

    def format_for_prompt(self, prefs: UserPreferences) -> str:
        """
        Format learned preferences into a prompt context block
        for injection into the agent's system prompt.
        """
        sections = []

        # Code style
        cs_parts = []
        if prefs.naming_convention:
            cs_parts.append(f"naming: {prefs.naming_convention}")
        if prefs.indentation:
            cs_parts.append(f"indentation: {prefs.indentation}")
        if prefs.comment_style:
            cs_parts.append(f"comments: {prefs.comment_style}")
        if prefs.preferred_languages:
            cs_parts.append(f"languages: {', '.join(prefs.preferred_languages[:5])}")
        if cs_parts:
            sections.append("**Code style:** " + " · ".join(cs_parts))

        # Communication
        sections.append(
            f"**Communication:** {prefs.verbosity} verbosity · {prefs.tone} tone · "
            f"{prefs.explanation_depth} depth"
        )

        # Workflow
        if prefs.preferred_tools:
            sections.append(f"**Preferred tools:** {', '.join(prefs.preferred_tools[:8])}")
        sections.append(f"**Approval caution level:** {prefs.approval_threshold}")

        if not sections:
            return ""

        return "## 🦅 User Preferences (learned)\n" + "\n".join(f"- {s}" for s in sections)
