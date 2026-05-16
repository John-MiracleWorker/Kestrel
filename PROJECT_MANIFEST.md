# Project Manifest - Kestrel

Last updated: 2026-05-16

## Purpose

Kestrel is a concrete local-first agent runtime built around Nested Learning-inspired memory layers and Memvid v2 `.mv2` storage.

The repo now goes beyond the original scaffold: it includes a conversational CLI, provider adapters, deterministic mock mode, a tool/approval system, Soul/self memory, gated web context, a FastAPI and web workbench, managed MCP stdio sessions, skill capsules, task graphs, repair gates, task capsules, golden evals, packaging, and deployment docs.

## Important Files

- `README.md` - quick start, current capabilities, safety model, and validation commands.
- `AGENTS.md` - concise Codex build instructions and non-negotiables.
- `docs/IMPLEMENTATION_STATUS.md` - current working/partial/not-done truth table.
- `docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md` - primary Codex handoff and next-hardening guide.
- `docs/CODEX_PRODUCTION_READY_AGENT_PROMPT.md` - larger production-hardening brief.
- `docs/FULL_AGENT_SPEC.md` - product/system specification.
- `docs/RUNTIME_WIRING.md` - current run, approval, scheduler, and memory wiring.
- `docs/IMPLEMENTATION_PIPELINE.md` - implemented phases and remaining hardening sequence.
- `docs/TEST_MATRIX.md` - unit, runtime, integration, and golden validation matrix.
- `docs/MEMVID_INTEGRATION.md` - current Memvid v2 adapter contract.
- `docs/MV2_CONTEXT_PACKING.md` - pseudo-context frame and packer contract.
- `docs/TASK_CAPSULES.md` - run-scoped `complete.mv2` lifecycle.
- `docs/MEMORY_OPERATIONS.md` - `.mv2` backup, restore, verify, doctor, and migration guidance.
- `docs/SECURITY.md` - local-first defaults, API auth, webhook signatures, and tool gates.
- `src/nested_memvid_agent/agent.py` - agent loop.
- `src/nested_memvid_agent/run_manager.py` - persistent runs, approvals, scheduler, subagents, and resume flow.
- `src/nested_memvid_agent/cli.py` - `nest-agent` CLI.
- `src/nested_memvid_agent/server.py` - FastAPI control plane.
- `src/nested_memvid_agent/backends/memvid_backend.py` - Memvid v2 `.mv2` backend adapter.
- `src/nested_memvid_agent/mcp_manager.py` - managed MCP server sessions and tool adapters.
- `src/nested_memvid_agent/plugin_manager.py` - alpha GitHub plugin registry and skill/MCP materialization.
- `src/nested_memvid_agent/tools/builtin.py` - built-in tools and high-risk gates.
- `src/nested_memvid_agent/state_store.py` - SQLite control-plane state, currently schema version 9.
- `scripts/run_golden_evals.py` - deterministic golden eval harness.
- `tests/integration/test_memvid_backend_integration.py` - gated Memvid backend integration.
- `tests/integration/test_memvid_context_frames.py` - gated Memvid context/capsule integration.
- `tests/integration/test_mcp_stdio_integration.py` - gated live stdio MCP integration.
- `web/` - local React/Vite workbench.

## Current Capability

Works now:

```bash
nest-agent chat --backend memory --provider mock --message "hello"
nest-agent chat --backend memory --provider mock --message "who are you?"
nest-agent chat --backend memory --provider mock --allow-web --web-backend mock --message "/web Kestrel Soul"
nest-agent run --backend memory --provider mock --json --events "hello run"
nest-agent server --backend memory --provider mock --host 127.0.0.1 --port 8765
```

Memvid path:

```bash
nest-agent init --backend memvid --memory-dir .nest/memory
nest-agent memory verify --backend memvid --memory-dir .nest/memory
nest-agent chat --backend memvid --memory-dir .nest/memory --provider openai --model <available-model>
```

The mock backend and mock LLM are deterministic. Memvid and MCP live tests are opt-in behind environment variables.

Permanent memory now includes `.nest/memory/self.mv2` for the user-facing Soul layer. It stores identity, capability snapshots, user/workflow preferences, self-change requests, and validation metadata with evidence/provenance requirements.

## Validation

Fast core validation:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "hello"
```

Full local validation when dev extras and Node dependencies are installed:

```bash
python -m ruff check scripts src tests
python -m mypy src
npm run test --prefix web
npm run build --prefix web
```

Optional integration validation:

```bash
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

## Non-Negotiables

- Use Memvid v2 `.mv2` files only.
- Keep one `.mv2` file per permanent nested memory layer unless a test proves a better layout.
- Never call `create(path)` on an existing `.mv2` file.
- Keep CLI conversation working before optional UI work.
- Keep the mock backend and mock LLM deterministic.
- No policy memory writes from a single ordinary event.
- High-risk tools require explicit config enablement where applicable and exact-call approval.
- Every memory promotion needs evidence, provenance, confidence, and validation status.
- Add Memvid integration coverage behind `RUN_MEMVID_INTEGRATION=1`.
