# Implementation Status

Last updated: 2026-05-16

This repository is a working local agent scaffold, not a finished Hermes/OpenClaw agent. The status below is intentionally literal so future Codex passes can harden the right layers without treating roadmap items as done.

## Working Now

- CLI chat loop with in-memory and Memvid `.mv2` memory backends.
- Layered memory files for working, episodic, semantic, procedural, and policy layers.
- Context compiler that retrieves nested memory and builds the model prompt.
- MV2 context-frame model and token-aware pseudo-context packer that retrieves summaries first, deduplicates content, flags conflict metadata, and expands raw evidence on demand.
- Deterministic mock provider for fast tests and reproducible golden evals.
- OpenAI Responses provider adapter using the portable JSON tool envelope.
- OpenAI-compatible chat completions provider for local/model-server endpoints.
- Codex CLI provider that can use local `codex exec` as the normal response engine.
- Built-in tool registry with structured exception boundaries, timeout enforcement, and exact-call approval gates for shell, file writes, patch application, tests, and Codex CLI delegation.
- Local FastAPI control plane with background runs, SSE events, approvals, tools, MCP registry, skills registry, and memory search.
- Multi-channel ingress for Telegram Bot API updates, Discord message/interaction-shaped payloads, and generic/custom webhooks, with CLI and API routes.
- SQLite state store for runs, run steps, approvals, MCP servers, skills, task nodes, and subagent runs, now initialized through schema version `4`.
- Paper-guided nested learning kernel with context-flow metadata, optimizer traces, conservative continuum-memory routing, and a `memory.learn` tool/API path.
- Run-scoped `complete.mv2` task capsules, preview-only capsule summaries, dry-run consolidation decisions, and approval-gated capsule apply.
- MCP server records now track health metadata, tool counts, capabilities, last sync/seen/call/error timestamps, session state, failure counts, and latency.
- MCP stdio servers now have a managed lazy session lifecycle with connect/disconnect/restart/health API routes, bounded operation timeouts, config-change teardown, and approval-by-default tool risk normalization.
- First task-graph and subagent run records exist, with durable task metadata, deterministic starter plan decomposition, in-process planner/worker/reviewer profiles, and UI/API surfaces.

## Partially Implemented

- Streaming: the runtime, CLI, and web run event bus accept stream events. Providers without native streaming use the compatibility wrapper around `generate()`.
- MCP: stdio live sessions are hardened and covered by a flag-gated integration test. SSE and streamable HTTP use the same manager path but still need real transport fixtures and production soak testing.
- Skills: filesystem discovery and skill tool adapters exist. Sandboxed skill execution and richer skill manifests remain incomplete.
- Codex CLI: `codex-cli` can drive responses and `codex.exec` is available as a high-risk approval-gated tool. It is not yet a branch-isolated autonomous repair loop.
- Consolidation: capsule extraction and Nested Learning decisions exist, but auto-consolidation remains disabled by default and validation loops are still basic.
- Self-modification: the runtime can record validated self-improvement signals and policy candidates, but code changes and policy writes still require explicit gates.
- Subagents: local subagent runs can be queued and tracked. True branch/worktree isolation and Codex-backed worker fan-out are still next steps.
- Channels: inbound normalization and dry-run reply payloads are implemented. Production bot identity verification, Discord Gateway reads, and channel-specific rate-limit handling still need hardening.

## Not Done Yet

- Native OpenAI function/tool calling and native streaming deltas.
- Full durable multi-step planner/executor/reviewer loop with resumable goals, retries, and review gates.
- Production authentication, authorization, and user/session isolation for the UI/API.
- Production webhook signature verification and secret rotation for external channel endpoints.
- Robust MCP SSE/streamable HTTP transport fixtures and failure-recovery soak testing.
- Container-grade sandboxed skill execution.
- Autonomous self-improvement with diff review, test gates, rollback, and explicit human approval.
- Comprehensive frontend design for model/provider settings, live tool traces, MCP configuration editing, and skill execution details.

## Current Contract

- High-risk tools require both capability enablement (matching allow flag, where applicable) and explicit approval for the exact tool-call ID and arguments before execution.
- Cancelled runs must not transition to completed, blocked, or failed after cancellation; lifecycle updates should use the guarded state transition helper.
- Tool execution is bounded by `tool_timeout_seconds` / `NEST_AGENT_TOOL_TIMEOUT_SECONDS` and timeout failures are returned as structured tool errors.
- New background runs persist a root task plus a small starter DAG with dependencies, required tools, risk, acceptance criteria, attempt count, and failure reason fields.
- Ordinary conversation and observations must not write policy memory directly.
- The Memvid backend must use `.mv2` files and preserve one file per memory layer.
- `complete.mv2` is a run artifact under `.nest/runs/{run_id}/`, not a permanent memory layer.
- SQLite is control-plane state only. It is not the retrieval memory substrate.
- Mock-provider tests are the default fast validation path; Memvid integration remains behind `RUN_MEMVID_INTEGRATION=1`.

## Validation Commands

```bash
python -m compileall -q src scripts
pytest -q
RUN_MCP_INTEGRATION=1 pytest -q tests/integration
RUN_MEMVID_INTEGRATION=1 pytest -q tests/integration
python scripts/run_golden_evals.py --backend memory --provider mock
npm run test --prefix web
npm run build --prefix web
```
