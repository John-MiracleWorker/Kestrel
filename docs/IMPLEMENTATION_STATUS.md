# Implementation Status

Last updated: 2026-05-15

This repository is a working local agent scaffold, not a finished Hermes/OpenClaw agent. The status below is intentionally literal so future Codex passes can harden the right layers without treating roadmap items as done.

## Working Now

- CLI chat loop with in-memory and Memvid `.mv2` memory backends.
- Layered memory files for working, episodic, semantic, procedural, and policy layers.
- Context compiler that retrieves nested memory and builds the model prompt.
- Deterministic mock provider for fast tests and reproducible golden evals.
- OpenAI Responses provider adapter using the portable JSON tool envelope.
- OpenAI-compatible chat completions provider for local/model-server endpoints.
- Built-in tool registry with approval gates for shell, file writes, patch application, tests, and Codex CLI delegation.
- Local FastAPI control plane with background runs, SSE events, approvals, tools, MCP registry, skills registry, and memory search.
- SQLite state store for runs, run steps, approvals, MCP servers, and skills, now initialized through schema version `1`.
- Paper-guided nested learning kernel with context-flow metadata, optimizer traces, conservative continuum-memory routing, and a `memory.learn` tool/API path.

## Partially Implemented

- Streaming: the runtime, CLI, and web run event bus accept stream events. Providers without native streaming use the compatibility wrapper around `generate()`.
- MCP: server configuration, static tool discovery, and registry exposure exist. Full live MCP session execution still needs hardening per transport.
- Skills: filesystem discovery and skill tool adapters exist. Sandboxed skill execution and richer skill manifests remain incomplete.
- Codex CLI: `codex.exec` is available as a high-risk tool with approval gating. It is not yet a general planner or autonomous repair loop.
- Consolidation: memory consolidation scaffolding exists, but promotion policy, evidence thresholds, and validation loops are still basic.
- Self-modification: the runtime can record validated self-improvement signals and policy candidates, but code changes and policy writes still require explicit gates.

## Not Done Yet

- Native OpenAI function/tool calling and native streaming deltas.
- Durable multi-step planner with resumable goals and explicit task graphs.
- Production authentication, authorization, and user/session isolation for the UI/API.
- Robust MCP stdio/SSE connection lifecycle with per-server capabilities and failure recovery.
- Container-grade sandboxed skill execution.
- Autonomous self-improvement with diff review, test gates, rollback, and explicit human approval.
- Comprehensive frontend design for model/provider settings, live tool traces, MCP configuration editing, and skill execution details.

## Current Contract

- High-risk tools must remain blocked unless the matching allow flag is set or a human approval handler approves the exact tool call.
- Ordinary conversation and observations must not write policy memory directly.
- The Memvid backend must use `.mv2` files and preserve one file per memory layer.
- Mock-provider tests are the default fast validation path; Memvid integration remains behind `RUN_MEMVID_INTEGRATION=1`.

## Validation Commands

```bash
python -m compileall -q src scripts
pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
npm run test --prefix web
npm run build --prefix web
```
