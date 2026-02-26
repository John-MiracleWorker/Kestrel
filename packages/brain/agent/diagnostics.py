"""
Diagnostic Tracker — classifies errors, tracks attempts, and detects repetition.

Injected into the executor's reasoning loop so the LLM gets structured
feedback about what failed and why, instead of just raw error strings.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("brain.agent.diagnostics")


class ErrorCategory(str, Enum):
    TRANSIENT = "transient"          # Network, timeout, rate limit — will likely resolve on retry
    AUTH = "auth"                    # Missing credentials, expired token, permission denied
    NOT_FOUND = "not_found"         # File, command, resource doesn't exist
    DEPENDENCY = "dependency"        # Missing package, module, service not running
    SEMANTIC = "semantic"           # Wrong arguments, invalid input, logical error
    SERVER_CRASH = "server_crash"   # Subprocess died, connection closed unexpectedly
    IMPOSSIBLE = "impossible"        # Logically impossible request, contradictory
    UNKNOWN = "unknown"


# Pattern → category mapping (checked in order, first match wins)
_ERROR_PATTERNS: list[tuple[str, ErrorCategory, str]] = [
    # Transient
    (r"timeout|timed?\s*out", ErrorCategory.TRANSIENT, "The operation timed out — may succeed on retry"),
    (r"rate.?limit|429|too many requests", ErrorCategory.TRANSIENT, "Rate limited — wait before retrying"),
    (r"503|502|temporarily unavailable|service unavailable", ErrorCategory.TRANSIENT, "Service temporarily down"),
    (r"connection\s*(refused|reset|error)|ECONNREFUSED|ECONNRESET", ErrorCategory.TRANSIENT, "Connection failed — check if service is running"),
    # Auth
    (r"401|403|unauthorized|forbidden|permission denied|access denied", ErrorCategory.AUTH, "Authentication or permission issue"),
    (r"invalid.*(token|key|credential|api.?key)", ErrorCategory.AUTH, "Invalid credentials"),
    (r"expired.*(token|session|credential)", ErrorCategory.AUTH, "Credentials expired"),
    # Not found
    (r"404|not found|no such file|does not exist|FileNotFoundError", ErrorCategory.NOT_FOUND, "Resource not found"),
    (r"command not found|No such file or directory", ErrorCategory.NOT_FOUND, "Command or path not found"),
    # Dependency
    (r"ModuleNotFoundError|ImportError|No module named", ErrorCategory.DEPENDENCY, "Missing Python dependency — needs pip install"),
    (r"Cannot find module|MODULE_NOT_FOUND", ErrorCategory.DEPENDENCY, "Missing Node.js dependency — needs npm install"),
    (r"is not installed|not found in PATH", ErrorCategory.DEPENDENCY, "Required program not installed"),
    # Server crash
    (r"Server closed|process.*(died|exited|terminated|crashed)", ErrorCategory.SERVER_CRASH, "Server process crashed — check stderr/logs"),
    (r"broken pipe|EPIPE|SIGPIPE", ErrorCategory.SERVER_CRASH, "Process pipe broken — server likely crashed"),
    (r"Initialize failed.*Server closed", ErrorCategory.SERVER_CRASH, "MCP server crashed during handshake"),
    # Semantic
    (r"invalid.*argument|TypeError|ValueError|bad request|400", ErrorCategory.SEMANTIC, "Invalid arguments — check the parameters"),
    (r"parse error|JSON.*error|SyntaxError", ErrorCategory.SEMANTIC, "Malformed input — fix the format"),
    (r"Method not found|tool.*not found|unknown (tool|method)", ErrorCategory.SEMANTIC, "Wrong tool or method name"),
]

_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), cat, hint) for p, cat, hint in _ERROR_PATTERNS]


def classify_error(error_text: str) -> tuple[ErrorCategory, str]:
    """Classify an error string into a category and return a diagnostic hint."""
    if not error_text:
        return ErrorCategory.UNKNOWN, "No error details available"

    for pattern, category, hint in _COMPILED_PATTERNS:
        if pattern.search(error_text):
            return category, hint

    return ErrorCategory.UNKNOWN, "Unrecognized error — try a different approach"


def _fingerprint(tool_name: str, args: dict) -> str:
    """Create a compact fingerprint for a tool call to detect repetition."""
    raw = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
    return hashlib.md5(raw.encode()).hexdigest()[:10]


@dataclass
class Attempt:
    tool: str
    args: dict
    error: Optional[str]
    success: bool
    category: Optional[ErrorCategory] = None
    hint: Optional[str] = None
    fingerprint: str = ""


@dataclass
class DiagnosticTracker:
    """Tracks tool attempts for a single step, classifies errors, detects repetition."""

    attempts: list[Attempt] = field(default_factory=list)
    _fingerprints: dict[str, int] = field(default_factory=dict)  # fingerprint → count

    def record(self, tool_name: str, args: dict, result_output: str, success: bool, error: Optional[str] = None):
        """Record a tool call attempt with classification."""
        fp = _fingerprint(tool_name, args)
        category, hint = (None, None)
        if not success and error:
            category, hint = classify_error(error)

        self.attempts.append(Attempt(
            tool=tool_name,
            args=args,
            error=error if not success else None,
            success=success,
            category=category,
            hint=hint,
            fingerprint=fp,
        ))

        self._fingerprints[fp] = self._fingerprints.get(fp, 0) + 1

    @property
    def failure_count(self) -> int:
        return sum(1 for a in self.attempts if not a.success)

    @property
    def has_repetition(self) -> bool:
        """True if any identical tool+args combination was tried more than once."""
        return any(count > 1 for count in self._fingerprints.values())

    @property
    def repeated_calls(self) -> list[tuple[str, int]]:
        """Return (tool_name, count) for repeated calls."""
        fp_to_tool = {}
        for a in self.attempts:
            fp_to_tool[a.fingerprint] = a.tool
        return [
            (fp_to_tool[fp], count)
            for fp, count in self._fingerprints.items()
            if count > 1
        ]

    @property
    def dominant_error_category(self) -> Optional[ErrorCategory]:
        """Return the most common error category, if any."""
        cats = [a.category for a in self.attempts if a.category]
        if not cats:
            return None
        from collections import Counter
        return Counter(cats).most_common(1)[0][0]

    def build_diagnostic_prompt(self) -> str:
        """
        Build a compact diagnostic summary for injection into the LLM prompt.
        Only generated when there are failures — returns empty string if all OK.
        """
        if self.failure_count == 0:
            return ""

        lines = []
        lines.append("⚠ DIAGNOSTIC CONTEXT (from previous attempts in this step):")

        # Error summary by category
        from collections import Counter
        cat_counts = Counter(a.category.value for a in self.attempts if a.category)
        if cat_counts:
            lines.append(f"  Error breakdown: {dict(cat_counts)}")

        # Dominant category guidance
        dominant = self.dominant_error_category
        if dominant == ErrorCategory.TRANSIENT:
            lines.append("  → Transient errors detected. A brief wait or retry may help, but don't retry more than twice.")
        elif dominant == ErrorCategory.AUTH:
            lines.append("  → Authentication/permission errors. Check credentials, API keys, or token validity before retrying.")
        elif dominant == ErrorCategory.NOT_FOUND:
            lines.append("  → Resource not found. Verify the path, filename, or URL exists before trying again.")
        elif dominant == ErrorCategory.DEPENDENCY:
            lines.append("  → Missing dependency. Install the required package/module before retrying the operation.")
        elif dominant == ErrorCategory.SERVER_CRASH:
            lines.append("  → Server process crashed. Check requirements, environment variables, and stderr output before reconnecting.")
        elif dominant == ErrorCategory.SEMANTIC:
            lines.append("  → Input/argument errors. Review the expected format and fix your arguments before retrying.")
        elif dominant == ErrorCategory.IMPOSSIBLE:
            lines.append("  → This may not be achievable. Consider asking the user for clarification or reporting the limitation.")

        # Repetition warning
        if self.has_repetition:
            repeated = self.repeated_calls
            rep_str = ", ".join(f"{tool}({count}x)" for tool, count in repeated)
            lines.append(f"  ⛔ REPETITION DETECTED: {rep_str} — do NOT retry the same call. Try a fundamentally different approach.")

        # Failed attempts (compact, last 5)
        failed = [a for a in self.attempts if not a.success]
        if failed:
            lines.append(f"  Failed attempts ({len(failed)} total):")
            for a in failed[-5:]:
                cat_label = f"[{a.category.value}]" if a.category else "[?]"
                error_snippet = (a.error or "")[:120]
                lines.append(f"    - {a.tool} {cat_label}: {error_snippet}")
                if a.hint:
                    lines.append(f"      Hint: {a.hint}")

        # Guidance
        if self.failure_count >= 3:
            lines.append("  📋 You have failed 3+ times. STOP and diagnose the root cause before making another tool call.")
            lines.append("     Use diagnostic tools (system_health, host_list, host_read) to gather information first.")

        return "\n".join(lines)
