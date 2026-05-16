from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureClassification:
    category: str
    confidence: float
    signals: tuple[str, ...]
    retryable: bool
    playbook: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "classification": self.category,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "retryable": self.retryable,
            "playbook": self.playbook,
        }


def classify_failure(failure_text: str, *, source: str = "") -> FailureClassification:
    text = failure_text.strip()
    lowered = f"{source}\n{text}".lower()
    signals: list[str] = []

    if any(marker in lowered for marker in ("modulenotfounderror", "importerror", "no module named")):
        signals.append("python import/dependency error marker")
        return FailureClassification(
            category="missing_dependency",
            confidence=0.86,
            signals=tuple(signals),
            retryable=True,
            playbook=_playbook("missing_dependency"),
        )
    if any(marker in lowered for marker in ("failed tests/", " assertionerror", "pytest", "== failures ==")):
        signals.append("pytest/test failure marker")
        return FailureClassification(
            category="test_failure",
            confidence=0.82,
            signals=tuple(signals),
            retryable=True,
            playbook=_playbook("test_failure"),
        )
    if any(marker in lowered for marker in ("permission denied", "operation not permitted", "approval_required")):
        signals.append("permission/approval marker")
        return FailureClassification(
            category="permission_failure",
            confidence=0.78,
            signals=tuple(signals),
            retryable=False,
            playbook=_playbook("permission_failure"),
        )
    if any(marker in lowered for marker in ("invalid_tool_arguments", "bad_memory_enum", "unknown memory layer")):
        signals.append("tool argument validation marker")
        return FailureClassification(
            category="bad_tool_args",
            confidence=0.8,
            signals=tuple(signals),
            retryable=True,
            playbook=_playbook("bad_tool_args"),
        )
    if any(marker in lowered for marker in ("tool_timeout", "timed out", "timeout")):
        signals.append("timeout marker")
        return FailureClassification(
            category="tool_failure",
            confidence=0.72,
            signals=tuple(signals),
            retryable=True,
            playbook=_playbook("tool_failure"),
        )
    if any(marker in lowered for marker in ("provider error", "rate limit", "openai", "anthropic", "llm.error")):
        signals.append("provider failure marker")
        return FailureClassification(
            category="provider_failure",
            confidence=0.7,
            signals=tuple(signals),
            retryable=True,
            playbook=_playbook("provider_failure"),
        )
    if any(marker in lowered for marker in ("mcp", "json-rpc", "stdio server", "sse")):
        signals.append("mcp/transport marker")
        return FailureClassification(
            category="mcp_failure",
            confidence=0.68,
            signals=tuple(signals),
            retryable=True,
            playbook=_playbook("mcp_failure"),
        )
    if any(marker in lowered for marker in ("path escapes workspace", "sandbox", "outside workspace")):
        signals.append("path/sandbox marker")
        return FailureClassification(
            category="path_sandbox_violation",
            confidence=0.84,
            signals=tuple(signals),
            retryable=False,
            playbook=_playbook("path_sandbox_violation"),
        )

    return FailureClassification(
        category="unknown_failure",
        confidence=0.35,
        signals=tuple(signals),
        retryable=True,
        playbook=_playbook("unknown_failure"),
    )


def _playbook(category: str) -> dict[str, object]:
    playbooks: dict[str, dict[str, object]] = {
        "test_failure": {
            "name": "Test failure playbook",
            "next_actions": [
                "Read the first failing assertion and stack trace completely.",
                "Run the smallest failing test target to reproduce.",
                "Inspect recent changes around the failing code path.",
                "Fix the root cause, then run targeted tests before broader tests.",
            ],
        },
        "missing_dependency": {
            "name": "Import/dependency failure playbook",
            "next_actions": [
                "Identify whether the missing module is first-party or third-party.",
                "For first-party imports, verify PYTHONPATH/package configuration before installing anything.",
                "For third-party imports, update project dependency metadata rather than ad-hoc CI installs.",
            ],
        },
        "provider_failure": {
            "name": "Provider failure playbook",
            "next_actions": [
                "Classify retryable versus configuration/auth failure.",
                "Check provider timeout, rate-limit, and credential settings.",
                "Preserve the provider error taxonomy in the run trace.",
            ],
        },
        "mcp_failure": {
            "name": "MCP failure playbook",
            "next_actions": [
                "Check transport health and last error metadata.",
                "Retry connection once with timeout boundaries.",
                "Do not trust new MCP tools without risk classification and approval.",
            ],
        },
        "bad_tool_args": {
            "name": "Bad tool arguments playbook",
            "next_actions": [
                "Compare arguments to the tool schema.",
                "Correct only the malformed fields before retrying.",
                "Record repeated schema mistakes as procedural candidates after validation.",
            ],
        },
        "tool_failure": {
            "name": "Tool failure playbook",
            "next_actions": [
                "Inspect tool exit code/error and timeout boundary.",
                "Retry only if the strategy changes or the failure is transient.",
                "Prefer narrower commands before broad retries.",
            ],
        },
        "permission_failure": {
            "name": "Permission failure playbook",
            "next_actions": [
                "Do not bypass approval or sandbox policy.",
                "Request the minimum explicit permission needed for the exact action.",
            ],
        },
        "path_sandbox_violation": {
            "name": "Path/sandbox violation playbook",
            "next_actions": [
                "Stop the attempted path escape.",
                "Resolve files relative to the configured workspace.",
                "Ask for explicit scope expansion if the file is truly outside the workspace.",
            ],
        },
        "unknown_failure": {
            "name": "Unknown failure playbook",
            "next_actions": [
                "Gather the complete error text and reproduction command.",
                "Classify the failing component before retrying.",
                "Avoid repeating the same action without a changed hypothesis.",
            ],
        },
    }
    return playbooks[category]
