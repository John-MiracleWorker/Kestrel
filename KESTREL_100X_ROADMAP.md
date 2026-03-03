# Kestrel 100× Architecture & Capability Expansion Plan

**Target Repository**: John-MiracleWorker/Kestrel
**Version**: 1.0
**Author**: Architecture Proposal
**Scope**: Agent Loop, Executor, Memory, Tooling, Routing, Guardrails, Persistence, Observability

## Table of Contents

- [1. Overview](#1-overview)
- [2. Goals](#2-goals)
- [3. Non-Goals](#3-non-goals)
- [4. Current System Constraints](#4-current-system-constraints)
- [5. Architectural Extensions](#5-architectural-extensions)
- [6. The Ten Core Upgrades](#6-the-ten-core-upgrades)
- [7. Cross-Cutting Concerns](#7-cross-cutting-concerns)
- [8. Phased Rollout](#8-phased-rollout)
- [9. Success Definition](#9-success-definition)

## 1. Overview

Kestrel is a modular autonomous AI agent system built around:

- Plan → Execute → Reflect loop
- Parallel tool dispatch
- Model routing and escalation
- Persistent task state
- Evidence tracking
- Memory graph enrichment
- Council deliberation

This document defines architectural upgrades that will increase:

- Throughput
- Reliability
- Parallelism
- Procedural intelligence
- Self-expansion capability
- Verification accuracy
- Measurable performance improvements

The objective is not incremental improvement, but architectural leverage.

## 2. Goals

- **G1 — Reduce LLM round-trips**: Allow batching, multi-tool calls, parallel steps.
- **G2 — Increase correctness**: Add verification gates and evidence binding.
- **G3 — Add procedural memory**: Introduce macros and reusable workflows.
- **G4 — Enable safe concurrency**: DAG-based plan execution.
- **G5 — Enable capability expansion**: Automatic MCP discovery + install flow.
- **G6 — Strengthen reliability**: Enforced state machine transitions + event-driven approvals.
- **G7 — Add measurable evaluation**: Evals harness + metrics dashboard.

## 3. Non-Goals

- Replacing entire architecture
- Removing human approval from high-risk actions
- Self-modifying core code without guardrails
- Breaking backward compatibility with existing tasks

## 4. Current System Constraints

- **C1 — Prompt Limits Tool Usage**: Executor system prompt states: "Call exactly ONE tool per turn". But executor supports parallel tool dispatch and multi-tool batching. This mismatch suppresses throughput.
- **C2 — Linear Plan Structure**: Plans are effectively sequential lists.
- **C3 — Naive Memory Querying**: Goal entity extraction is simplistic.
- **C4 — No System-Level Tool Caching**: Caching depends on agent remembering to call recall tools.
- **C5 — No Formal Verifier Gate**: Completion does not require proof consistency.

## 5. Architectural Extensions

New/Extended Components:

| Component            | Purpose                                   |
| -------------------- | ----------------------------------------- |
| ToolCache            | System-level deterministic tool caching   |
| MacroRegistry        | Composable workflow system                |
| StepScheduler        | DAG-based parallel step execution         |
| VerifierEngine       | Evidence-bound output verification        |
| MemoryQueryEngine    | Structured entity-driven memory retrieval |
| MCPExpansionFlow     | Automatic tool discovery + install        |
| StateMachineEnforcer | Legal status transitions                  |
| EvalRunner           | Quantitative improvement tracking         |

## 6. The Ten Core Upgrades

### 6.1 Upgrade 1 — Multi-Tool Per Turn Execution

**Problem**: Prompt restricts tool calls to one per turn.
**Specification**: Modify AGENT_SYSTEM_PROMPT:
Replace:

```
Call exactly ONE tool per turn.
```

With:

```
You may call up to N tools per turn if:
- They are independent
- They are read-only or low-risk
- They do not require approval
Prefer batching and parallel-safe tools.
```

**New Constants**:

```python
MAX_TOOL_CALLS_PER_TURN = 5
PARALLEL_SAFE_TOOLS = {...}
```

**Acceptance**: Repo analysis tasks batch reads. Iteration count decreases.

### 6.2 Upgrade 2 — Macro / Workflow System

**Purpose**: Procedural reuse of tool sequences.
**New Tool**:

```python
macro_run(name: str, args: dict)
```

**Database Schema**:

```sql
CREATE TABLE macros (
  id UUID PRIMARY KEY,
  workspace_id TEXT,
  name TEXT,
  description TEXT,
  schema_json JSONB,
  steps_json JSONB,
  version INT,
  enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);
```

**Example Macro Definition**:

```json
[
    { "type": "tool", "name": "host_tree", "args": { "path": "{{repo_root}}" } },
    { "type": "tool", "name": "host_search", "args": { "query": "{{query}}" } },
    { "type": "tool", "name": "host_batch_read", "args": { "paths": "{{paths}}" } }
]
```

**Acceptance**: Repeated workflows become macros. Agent prefers macros over raw tool sequences.

### 6.3 Upgrade 3 — Dependency-Based Planning (DAG)

**Extend TaskStep**: Add `depends_on: List[str]`, `produces: List[str]`, `consumes: List[str]`, `parallelizable: bool`.
**Extend TaskPlan**: Add `artifacts: Dict[str, Any]`.
**Acceptance**: Independent steps execute concurrently. Dependency constraints enforced.

### 6.4 Upgrade 4 — Parallel Step Scheduler

**Add new component `StepScheduler`**:

- `get_ready_steps(plan)`
- `run_parallel(limit=N)`

**Rules**: Read-only steps may run concurrently. Approval-required steps must run sequentially.
**Acceptance**: Multi-branch plans reduce wall-clock time.

### 6.5 Upgrade 5 — Verifier Gate

**Trigger Conditions**: Code output, Shell commands, Web-based research claims, Security recommendations.
**Modes**: Deterministic (Validate file references, Validate tool outputs). LLM Verifier (Low temperature, Evidence-bound reasoning, PASS / FAIL / CONFIDENCE).
**Evidence Binding Rule**: Verifier must cite tool outputs.
**Acceptance**: Hallucinated references decrease. FAIL triggers limited repair loop.

### 6.6 Upgrade 6 — System-Level Tool Caching

**Middleware Layer**: Wrap `ToolRegistry.execute`.
**Cache Key**: `hash(tool_name + normalized_args + workspace_id + tool_version)`
**Tool Metadata**: `cache_ttl_seconds`, `cache_scope`
**Storage**: Redis preferred, Postgres fallback.
**Acceptance**: Repeated `host_tree` hits cache. Tool latency decreases.

### 6.7 Upgrade 7 — Structured Memory Querying

**Replace naive word slicing with**: `extract_entities(goal, context)`
**Store**: procedures, pitfalls, preferences, repo fingerprints.
**Acceptance**: Memory recall improves relevance.

### 6.8 Upgrade 8 — MCP Auto-Expansion

**Flow**: Tool gap detected -> `mcp_search` invoked -> Tool evaluated via rubric -> Approval requested -> Tool installed -> Execution continues.
**Guardrails**: Installation always requires approval unless whitelisted.
**Acceptance**: Agent expands capability autonomously.

### 6.9 Upgrade 9 — State Machine Enforcement

**Legal Transition Map**:
| From | To |
|---|---|
| PLANNING | EXECUTING |
| EXECUTING | WAITING_APPROVAL |
| WAITING_APPROVAL | EXECUTING |
| EXECUTING | COMPLETE / FAILED / CANCELLED |
Enforced in persistence layer.
**Approval Handling**: Replace polling with Redis PubSub or Postgres LISTEN/NOTIFY.
**Acceptance**: Restart-safe tasks. Instant resume on approval.

### 6.10 Upgrade 10 — Eval Harness

**Create**: `packages/brain/evals/`
**Metrics**: success_rate, wall_time, token_usage, tool_calls, cost_estimate, verifier_pass_rate.
**DB Table**:

```sql
CREATE TABLE eval_runs (
  id UUID,
  scenario TEXT,
  result_json JSONB,
  metrics_json JSONB,
  git_sha TEXT,
  created_at TIMESTAMP
);
```

**Acceptance**: CI can compare performance across commits.

## 7. Cross-Cutting Concerns

**Security**: Approval system unchanged for side-effect tools. Macros respect guardrails. MCP installs gated.
**Observability Events**: Add: `cache_hit`, `macro_started`, `verifier_started`, `scheduler_parallel_batch`, `mcp_candidate_found`, `state_transition`.

## 8. Phased Rollout

- **Phase 1**: Multi-tool + caching.
- **Phase 2**: Verifier gate.
- **Phase 3**: DAG + parallel scheduler.
- **Phase 4**: Macros.
- **Phase 5**: MCP expansion.
- **Phase 6**: State machine + eval harness.

## 9. Success Definition

Kestrel is 100× more powerful when:

- Fewer LLM iterations per task
- Parallel step execution active
- Verification reduces hallucinations
- Macros reused across tasks
- Tool caching reduces latency
- Agent installs new tools autonomously
- Eval harness shows measurable improvement

### 6.11 Upgrade 11 — The Proactive Heartbeat Engine

**Concept:** Move Kestrel from a purely reactive assistant (waiting for user input) to a proactive autonomous entity.
**Flow:** A background scheduler (e.g., Cron or APScheduler) wakes up the agent loop periodically.
**Features:**

- Assess current state and determine if background work is needed.
- Run maintenance sweeps (e.g., checking for stale PRs, running evaluations).
- Continue long-running tasks without requiring the user to prompt it.
  **Acceptance:** Agent can initiate and complete tasks asynchronously based on time or external events, creating a persistent "always-on" behavior.

### 6.12 Upgrade 12 — Agent-Native Intelligent Router

**Concept:** Move beyond basic API failover into semantic, cost-aware model dispatching (inspired by ClawRouter).
**Features:**

- Trivial read-only tasks (e.g., scraping, basic formatting) are routed to fast, cheap models (e.g., local Ollama, Claude Haiku, Gemini Flash).
- Heavy reasoning, planning, and verification tasks are routed to flagship models (e.g., Claude 3.5 Sonnet, GPT-4o).
  **Acceptance:** Overall latency and LLM API costs drop significantly without a reduction in complex reasoning capabilities.

### 6.13 Upgrade 13 — Headless Workflow Automation (`llm-task`)

**Concept:** Dedicated pipeline for running strict, JSON-only workflow execution without the overhead of the conversational chat layer.
**Features:**

- CI/CD integration or script execution that triggers Kestrel.
- Agent loop bypasses the conversational output and directly yields structured JSON payloads matching a requested schema.
  **Acceptance:** External systems (like GitHub Actions or external scripts) can reliably use Kestrel as a structured data processor without parsing conversational text.

### 6.14 Upgrade 14 — Dual Memory: Markdown-Backed Persistence

**Concept:** Enhance the black-box Vector DB memory with a transparent, human-editable text layer.
**Features:**

- Introduce a `Local Markdown Memory Manager`.
- Persist core knowledge, procedures, and rules into an easily accessible `.agents.md` or dedicated `memory/` folder on disk.
- Bi-directional sync: Edits to the markdown files update the agent's context and vice versa.
  **Acceptance:** Users can physically read and manually edit the agent's long-term memory, increasing trust and steerability.

### 6.15 Upgrade 15 — Unified "Agent OS" Control Plane

**Concept:** Solidify Kestrel's container, executor, and routing into a unified "Operating System for AI."
**Features:**

- Tighten the integration between the Gateway, the Executor Sandbox, and the Core Loop.
- Ensure the agent has full, safe, and observable control over its host environment as a true persistent background system rather than a temporary script.
  **Acceptance:** The system feels like an autonomous service layer (an OS) rather than a CLI tool or simple chatbot wrapper.
