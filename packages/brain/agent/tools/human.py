"""
Human interaction tools — ask the user questions and mark tasks complete.

These are control-flow tools that the agent uses to interact with the human
and signal task completion.
"""

import logging
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.human")


def register_human_tools(registry) -> None:
    """Register human interaction and control tools."""

    registry.register(
        definition=ToolDefinition(
            name="ask_human",
            description=(
                "Ask the user a question when you need clarification, "
                "confirmation, or additional information to proceed. "
                "The task will pause until the user responds."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of suggested answers",
                    },
                },
                "required": ["question"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=5,  # The tool itself is instant; pausing happens at loop level
            category="control",
        ),
        handler=ask_human,
    )

    registry.register(
        definition=ToolDefinition(
            name="task_complete",
            description=(
                "Signal that the current step or task is complete. "
                "Call this when you have finished executing a step "
                "and want to report the results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "A comprehensive, user-facing summary of what was accomplished. "
                            "CRITICAL: If the user asked you to find information, read emails, "
                            "or summarize data, you MUST include the actual findings, content, "
                            "and answers in this field. Do not just say 'I finished reading it.' "
                            "Format this with markdown for the user to read."
                        ),
                    },
                    "artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of files or resources created/modified",
                    },
                },
                "required": ["summary"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=5,
            category="control",
        ),
        handler=task_complete,
    )


async def ask_human(
    question: str,
    options: Optional[list[str]] = None,
) -> dict:
    """
    Ask the user a question.
    The actual pausing logic is handled by the agent loop when it sees
    this tool was called — this handler just formats the request.
    """
    result = {
        "question": question,
        "status": "waiting_for_human",
    }
    if options:
        result["options"] = options

    logger.info(f"Agent asking human: {question}")
    return result


async def task_complete(
    summary: str,
    artifacts: Optional[list[str]] = None,
) -> dict:
    """
    Signal that the ENTIRE task goal has been fully achieved.

    Only call this when every part of the user's original request has been
    completed — not when a single intermediate step finishes.  If the current
    step is done but more steps remain, simply return without calling this tool
    and the agent loop will advance to the next step automatically.

    The agent loop checks for this tool call and updates task status.
    """
    result = {
        "summary": summary,
        "status": "complete",
    }
    if artifacts:
        result["artifacts"] = artifacts

    logger.info(f"Step completed: {summary}")
    return result
