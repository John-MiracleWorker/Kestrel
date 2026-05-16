# Full Agent Specification

Last updated: 2026-05-16

## Product Goal

Kestrel is a local-first, memory-native engineering agent. It should safely work on codebases, use tools, learn from repeated validated outcomes, and explain what it remembers and why.

The differentiator is not a bigger prompt. It is a controlled memory/runtime architecture:

```text
LLM provider
+ chat/runtime loop
+ tool registry
+ permission and approval gates
+ nested .mv2 memory layers
+ context compiler and pseudo-context packer
+ event and state logs
+ consolidation and task capsules
+ eval harness
+ CLI/API/web control surfaces
```

## Non-Negotiable Design Rules

1. Preserve the Nested Learning memory architecture.
2. Use Memvid v2 `.mv2` files as the primary persistent memory backend.
3. Keep one permanent `.mv2` file per nested layer unless tests prove a better layout.
4. Never call `create(path)` on an existing `.mv2` file.
5. Do not replace `.mv2` memory with Chroma, SQLite, Postgres, FAISS, or JSON logs.
6. Use SQLite only for control-plane state.
7. Keep the agent local-first by default.
8. Keep mock backend/provider tests deterministic.
9. Do not let a single ordinary event become semantic, procedural, or policy memory.
10. Do not run high-risk tools without config enablement where applicable and exact-call approval.
11. Do not dump full raw transcripts into context by default.
12. Do not claim a feature is production-ready until it is implemented and tested.

## Current Core Components

### Agent Runtime

Class: `NestedMV2Agent`.

Responsibilities:

- receive user input from CLI/API/channel surfaces
- write current turn observations into working memory
- compile relevant nested memory context
- call an LLM provider
- parse final answers or portable JSON tool envelopes
- execute tools through `ToolRegistry`
- write tool results/failures to working memory
- continue until final answer, approval block, failure, or tool-round limit
- write turn summaries to episodic memory
- seal memory layers

### Run Manager

Class: `RunManager`.

Responsibilities:

- create and track background runs
- persist run steps and timeline events
- coordinate approvals and approval resume
- create task graphs and subagent records
- expose scheduler steps/runs
- write task capsules on completion
- protect terminal run transitions and immutable approval decisions

### LLM Providers

Current providers:

- deterministic mock provider
- OpenAI Responses provider
- OpenAI-compatible chat completions provider
- local Codex CLI provider

Current provider support:

- provider capability metadata
- retryable fallback wrapper
- portable JSON tool envelope
- OpenAI Responses streaming deltas when available

Remaining provider hardening:

- native tool-calling parity across providers
- streaming parity for OpenAI-compatible/local providers
- broader live integration tests
- richer provider-specific context and JSON-mode handling

### Tool System

Tools execute through schemas, workspace/path checks, timeout boundaries, capability gates, and exact-call approvals.

Current tool families:

- memory and context tools
- task capsule tools
- file and shell tools
- repo search/map tools
- patch/test/lint tools
- git status/diff/branch/commit tools
- Memvid verify/doctor/stats tools
- diagnosis and failure-recall tools
- safe repair tools
- skill install/runtime tools
- plugin registry and materialization commands
- Codex CLI delegation
- MCP-adapted tools

High-risk tools are blocked unless enabled where applicable and approved for the exact call ID and arguments. `git.commit` never pushes. Repair branch commits require `repair.review`.

### Nested Memory Layers

| Layer | File | Purpose | Write threshold | Promotion behavior |
|---|---|---|---:|---|
| Working | `.nest/memory/working.mv2` | current task state, observations, tool results | low | expires/compacts; feeds episodic |
| Episodic | `.nest/memory/episodic.mv2` | events, failures, decisions, summaries | medium | feeds semantic/procedural |
| Semantic | `.nest/memory/semantic.mv2` | stable facts and preferences | high | corrected rather than casually overwritten |
| Procedural | `.nest/memory/procedural.mv2` | reusable recipes and failure playbooks | very high | formed after repeated validated success |
| Policy | `.nest/memory/policy.mv2` | slow behavior and safety constraints | extreme | explicit/reviewed only |

Run-scoped capsules live at `.nest/runs/{run_id}/complete.mv2`.

### Context Compiler and Packer

The compiler delegates to the MV2 context packer. The packer should render:

- current objective
- policy constraints
- relevant procedures
- stable facts
- recent episodic/task state
- working memory
- confidence and validation metadata
- conflict warnings
- evidence pointers
- retrieval telemetry
- next-step instruction

It should prefer summaries and expand raw evidence through `context.expand` only when needed.

### Consolidation Pipeline

The consolidation pipeline maps Nested Learning concepts onto external agent memory:

- `ContextFlow` describes source layers, target layers, update frequency, objective, compression, and retention.
- `OptimizerTrace` records surprise, validation score, repeat count, compression ratio, confidence delta, and effective confidence.
- `NestedLearningKernel` decides reject/write/promote.
- `memory.learn`, `memory.consolidate`, `capsule.summarize`, and `capsule.apply` expose the flow.

Every accepted promotion must include evidence, provenance, confidence, validation status, and gate metadata. Policy memory is rare and strongly gated.

### API and Web

The FastAPI control plane and React/Vite workbench expose:

- background runs and SSE events
- approval list/decision flow
- memory search, verify, doctor, inspect
- context pack/expand/conflict utilities
- MCP registry, health, connect/disconnect/restart, sync, invoke
- skill registry and install flow
- task graph, subagent, and scheduler surfaces
- channel ingress
- plugin registry state where wired

API auth can be enabled through `NEST_AGENT_REQUIRE_API_AUTH=1` plus a token environment variable.

Plugin installation is an alpha/high-risk surface. The registry and CLI exist, but install-path allow-flag enforcement, review UX, dependency isolation, and shared-runtime security review remain hardening work.

### Channels

Inbound normalization exists for Telegram Bot API updates, Discord-shaped payloads, and generic/custom webhooks. Outbound delivery is disabled by default and must be explicitly configured.

Generic/custom webhooks can require HMAC-SHA256 signatures.

## Acceptance Criteria

The alpha runtime should continue to pass:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
npm run test --prefix web
npm run build --prefix web
```

Optional integrations should pass when dependencies are installed:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
```

The runtime is production-ready only when the remaining gaps in `docs/IMPLEMENTATION_STATUS.md` are closed, especially provider parity, production auth/isolation, MCP non-stdio transport fixtures, container-grade skill isolation, and isolated worker orchestration.
