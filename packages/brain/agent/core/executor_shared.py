import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

from agent.types import (
    AgentTask,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalTier,
    RiskLevel,
    StepStatus,
    TaskEvent,
    TaskEventType,
    TaskStatus,
    ToolCall,
    ToolResult,
)
from agent.guardrails import Guardrails
from agent.observability import MetricsCollector
from agent.evidence import EvidenceChain
from agent.model_router import ModelRouter
from agent.core.verifier import VerifierEngine
from agent.diagnostics import DiagnosticTracker, classify_error, ErrorCategory
from agent.tool_cache import ToolCache
from agent.mcp_expansion import MCPExpansionEngine

logger = logging.getLogger("brain.agent.core.executor")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*$", re.DOTALL)


def _strip_think(text: str) -> str:
    """Remove <think>…</think> and unclosed <think> blocks from LLM output."""
    if not text or "<think>" not in text:
        return text or ""
    cleaned = _THINK_RE.sub("", text)
    cleaned = _THINK_UNCLOSED_RE.sub("", cleaned)
    return cleaned.strip()


# ── Constants ────────────────────────────────────────────────────────
MAX_PARALLEL_TOOLS = 5       # Max concurrent tool executions per turn (default fallback)
RETRY_MAX_ATTEMPTS = 3       # Max retries for transient tool failures
RETRY_BASE_DELAY_S = 1.0     # Base delay for exponential backoff

# ── Error categories that should never be retried ────────────────────
_NO_RETRY_CATEGORIES = frozenset({
    ErrorCategory.AUTH,
    ErrorCategory.NOT_FOUND,
    ErrorCategory.DEPENDENCY,
    ErrorCategory.SEMANTIC,
    ErrorCategory.IMPOSSIBLE,
})

# ── System Prompt for the Reasoning LLM ──────────────────────────────
AGENT_SYSTEM_PROMPT = """\
You are Kestrel, an autonomous AI agent. You are executing a multi-step task.

Current goal: {goal}
Current step: {step_description}

Instructions:
1. Analyze the current situation and decide which tool to call next.
2. You may call up to 5 tools per turn if they are independent, read-only/low-risk, and do not require approval. Prefer batching and parallel-safe tools. Wait for all results before proceeding.
3. ONLY call `task_complete` when the ENTIRE original goal has been fully achieved — not just the current step. If the current step is done but there are more steps remaining in the plan, do NOT call `task_complete`. Instead, just finish your tool calls for this step and the system will automatically advance to the next step.
4. Before calling `task_complete`, review the original goal and verify every part of it has been addressed. For example, if asked to "summarize 10 emails", do not complete until all 10 have been fetched AND summarized.
5. If you need clarification from the user, call `ask_human` with your question.
6. Think step-by-step. Explain your reasoning before acting.

Error Recovery Protocol:
- When a tool fails, DIAGNOSE before retrying. Read the error message carefully.
- NEVER retry the exact same tool call with identical arguments — it will fail again.
- If an error is about a missing file/command, verify the path exists first.
- If an error is about dependencies, install them before retrying the operation.
- If an error is about auth/permissions, check credentials before retrying.
- If a server/process crashed, check stderr output and requirements before reconnecting.
- After 3 failures on the same step, STOP and use diagnostic tools (system_health, host_read, host_list) to gather information, or call `ask_human` to ask the user for help.

Verification & Evidence Rules:
- Before calling `task_complete`, you MUST verify that the ENTIRE original goal is satisfied, not just the current step.
- You MUST explicitly cite the tool outputs that prove ALL parts of the goal were accomplished in your summary.
- Your final summary will be strictly evaluated by an independent Verifier Engine against your tool execution history.
- If you make unsupported claims, hallucinate actions, or claim completion when parts of the goal are unfinished, your completion will be REJECTED.
- If you are on step {step_index} of {total_steps} and earlier steps remain, you almost certainly should NOT be calling `task_complete` yet.

Host Filesystem Strategy:
- Use project_recall(name) FIRST to check for cached project context.
- Use host_tree(path) ONCE for full directory tree — never call host_list repeatedly.
- Use host_find(pattern) or host_search(query, path) before broad reads to narrow scope quickly.
- Use host_batch_read(paths) for grouped file reads instead of repeated host_read calls.
- host_write requires human approval.

Self-Repair & Self-Improvement:
- You are NOT in an isolated sandbox. Your source code is mounted at /project inside the container.
- You CAN read and write your own source files using host_read and host_write.
- The `self_improve` tool provides: scan (analyze code), propose (generate fixes), apply (patch + test + hot-reload), rollback, and history.
- If one of your own tools is broken, DO NOT tell the user you cannot fix it. Instead, diagnose the error, read the source file, and use host_write or self_improve to patch and fix it.
- After editing a Python file, use the self_improve tool's hot-reload capability or ask the user to rebuild if needed.
- NEVER say you are in a sandbox or that you cannot modify your own code — you can and should fix issues proactively.

Progress: Step {step_index}/{total_steps} | Iteration {iteration}/{max_iterations}
{diagnostic_context}
Previous observations for this step:
{observations}
"""

# ── Compact system prompt for local models (~300 tokens) ─────────────
AGENT_SYSTEM_PROMPT_LOCAL = """\
You are Kestrel, an autonomous AI agent.

Goal: {goal}
Step: {step_description}

Rules:
- Call the appropriate tool to accomplish the current step.
- Call `task_complete` ONLY when the ENTIRE goal is done, not just this step.
- Call `ask_human` if you need clarification.
- If a tool fails, read the error and try a different approach. Never retry identical calls.
- After 3 failures, call `ask_human` for help.

Progress: Step {step_index}/{total_steps} | Iteration {iteration}/{max_iterations}
{diagnostic_context}
{observations}
"""


