# Kestrel Architecture Document

This document provides a comprehensive, high-detail overview of **Kestrel**, the autonomous agent engine that powers the **Libre Bird** platform. It synthesizes the technical structure, core loops, memory systems, tool capabilities, and multi-channel communication strategies that define Kestrel's architecture.

## 1. System Overview

**Libre Bird** is the overarching multi-channel AI platform, providing a Vue/Vite frontend ("aurora design system"), native macOS integration via `pywebview`, voice interactivity, and connections to both Local (Moshi, MLX) and Cloud LLMs (Gemini, OpenAI, Anthropic).

**Kestrel** is the brain of Libre Bird—a deeply autonomous, proactive agent operating system (Agent OS) tailored for software development, code analysis, self-improvement, and complex task orchestration. Kestrel acts across a highly structured "Plan → Execute → Reflect" loop with multi-agent coordination.

### Key Architectural Tenets

- **Autonomy with Guardrails:** Kestrel executes in a sandboxed Docker container environment with resource limits and audit logging, requiring human approval for high-risk operations.
- **Observability:** Everything the agent does—memory recall, plan creation, council debates, tool execution—is emitted as structured typed events (`AGENT_ACTIVITY`) streamed to the frontend via WebSockets.
- **Modularity:** Core systems (Memory, Routing, Tools, Channels) are decoupled.
- **Extensibility:** First-class support for the **Model Context Protocol (MCP)** allows Kestrel to dynamically discover, evaluate, and install new tools at runtime.

---

## 2. Core Agent Intelligence

### 2.1 The Agent Loop (`brain/agent/loop.py`)

At the heart of Kestrel is a state-machine-driven loop that dynamically cycles through tasks:

1. **Phase 0 (Initialization & Context):** Retrieves semantic memory, recent entity terms, and past lessons tailored to the user's current goal.
2. **Phase 1 (Planning):** Deconstructs goals into structured sequential or DAG-based steps.
3. **Phase 2 (Execution):** Dispatches tool requests (bash commands, code searches, file edits) in a sandboxed environment.
4. **Phase 3 (Reflection & Verification):** Reflects on the output, checks for hallucinations (evidence-bound), and determines if the plan needs alteration.

### 2.2 Multi-Agent Council System (`brain/agent/council.py`)

For complex decisions, Kestrel spins up a "Council."

- **Roles:** Specialized personas like _Architect_, _Security Reviewer_, and _Code Critic_.
- **Debate & Consensus:** Members analyze proposals, cast weighted votes, and arrive at a consensus or request further data.
- **Events:** Emits `council_started`, `opinion`, `debate`, and `verdict` for live UI tracking.

### 2.3 Memory Graph Engine (`brain/agent/memory_graph.py`)

Kestrel's memory is semantic, relational, and temporal.

- **Vector Retrieval:** Powered by ChromaDB, mapping conversational turns into embeddings via background processing.
- **Relevance & Decay:** Utilizes a time-weighted scoring system (e.g., 10% decay per week) so old facts fade while new facts are prioritized.
- **Entity Management:** Extracts procedures, pitfalls, repository fingerprints, and user preferences, dynamically injecting them into the system prompt context.

### 2.4 Self-Improvement & Reflection (`brain/agent/reflection.py` & `tools/self_improve.py`)

- **Red Teaming:** A built-in critique framework runs against generated plans and code implementation to flag logic flaws or security vulnerabilities before execution.
- **Persona Learning:** Adapts to user style preferences, coding conventions, and workflow patterns via an explicit `approval_memory.py` pipeline (learning from human approvals/rejections).

---

## 3. Execution & Tooling

### 3.1 Sandboxed Execution (`hands/executor.py`)

To ensure host safety, untrusted code and complex bash scripts are routed to a containerized sandbox.

- **Isolation:** Workspace-level isolation with strict network and resource constraints.
- **Risk Assessment:** `GuardrailsSystem` evaluates the expected payload of tools. Dangerous actions trigger a multi-channel approval request.

### 3.2 Dynamic Tool Registry & MCP (`brain/agent/tools/mcp.py`)

Kestrel features a massive footprint of core native tools (trafilatura for web, MFlux for local imagery, MLX text-to-speech) mapped into a unified `ToolRegistry`.

- **Auto-Expansion via MCP:** Kestrel uses tools like `mcp_search`, `mcp_install`, and `mcp_list_installed` to dynamically browse MCP catalogs (e.g., Smithery), evaluate tool utility via a rubric, request human approval, and persist tools to the `installed_tools` database.

### 3.3 The Verification Gate (`brain/agent/evidence.py`)

All claims and output files must pass an evidence-binding verification check before being returned to the user. Outputs must be anchored to tool results, drastically reducing pure LLM hallucinations.

---

## 4. Communication & Data Flow

### 4.1 Cross-Channel Router (`gateway/src/channels/registry.ts`)

Users can interface with Kestrel simultaneously across multiple platforms.

- **Channels:** Web, Telegram, Discord, WhatsApp.
- **Identity Resolution:** Links accounts across platforms.
- **Routing Strategies:** Uses preference-based routing (`same_channel`, `all_channels`, `prefer_web`).
- **Streaming:** Highly optimized custom token streaming pipeline linking the Core Engine Provider (OpenAI/Anthropic) to the WebSocket client via Gateway.

### 4.2 Live Developer HUD

A primary UX pillar constraint is full transparency:

- The `LiveCanvas` UI subscribes to Kestrel's event bus (`asyncio.Queue` -> `WebChannelAdapter` -> Frontend WebSocket).
- Displays real-time metrics (tokens/sec, time, costs), color-coded council votes, tool activity feeds, and a "Process Bar" (Memories -> Plan -> Tools -> Council -> Confidence -> Tokens).

---

## 5. Persistence & State

Kestrel heavily leverages a Postgres relational backend layered with Vector indexing:

- **`workspace_members` / `workspace_invites`**: Multi-user collaboration with Admin/Member/Viewer roles.
- **`installed_tools`**: JSONB schema mappings for dynamically loaded MCP servers.
- **`notifications`**: Async agent notifications (Task Complete, Mentions, Errors) pushed cross-channel.
- **`message_feedback`**: UI feedback loops (👍/👎 with comments) driving Kestrel's learning process.

---

## 6. Target 100x Architecture (Evolution Roadmap)

Kestrel is aggressively evolving toward higher capacity, proactive autonomy, and throughput via the 15-point `KESTREL_100X_ROADMAP` (which heavily incorporates OpenClaw "Agent OS" principles):

1. **Parallel Step Scheduler (DAG):** Upgrading the linear plan to a Dependency Graph to execute independent read-only tools concurrently.
2. **System-Level Tool Caching:** Redis-backed `hash(tool_name + args + workspace + version)` to bypass redundant tool executions (e.g., repeated `host_tree` scans).
3. **Procedural Macros:** The ability to save frequently executed tool sequences (e.g., "Deep Repo Checkout -> Grep -> Read") as compiled `macro_run` calls to dramatically decrease token costs and round-trips.
4. **State Machine Enforcement:** Strict legal state transitions (`PLANNING -> EXECUTING -> WAITING_APPROVAL -> COMPLETE`) synced directly to the DB, enabling instant task resumption after server restarts or mobile approval.

---

_Generated from Kestrel Root Documentation, Agent Workflows, and Internal Roadmap context._
