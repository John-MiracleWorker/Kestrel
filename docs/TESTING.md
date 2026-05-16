# Testing Guide

Last updated: 2026-05-16

Kestrel's fast test path is deterministic: it uses `InMemoryBackend` plus the mock LLM provider. Memvid, MCP, provider, and platform integrations stay behind explicit environment flags.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[memvid,openai,server,mcp,dev]'
npm install --prefix web
```

## Core Validation

Run these for normal development:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
PYTHONPATH=src python -m nested_memvid_agent.cli chat --backend memory --provider mock --message "hello"
```

`python -m pytest -q` is preferred over a global `pytest` binary so subprocess fixtures use the active interpreter.

## Lint, Types, and Web

```bash
python -m ruff check scripts src tests
python -m mypy src
npm run test --prefix web
npm run build --prefix web
```

`npm run test --prefix web` currently runs the TypeScript build in no-pretty mode. `npm run build --prefix web` runs TypeScript plus the Vite production build.

## Optional Memvid Integration

The Memvid tests require `memvid-sdk` from the `memvid` extra and are skipped unless `RUN_MEMVID_INTEGRATION=1` is set:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

The tests create temporary `.mv2` files, write records, seal, verify, close, reopen, search, round-trip context-frame metadata, validate the Soul/self layer, and read run-scoped `complete.mv2` capsules.

## Optional MCP Integration

Live stdio MCP coverage is gated by `RUN_MCP_INTEGRATION=1`:

```bash
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
```

The fixture server is `tests/integration/fixtures/stdio_mcp_server.py`. It proves managed stdio discovery and invocation through `MCPManager`.

## Optional Provider Integration

Live provider coverage is gated by `RUN_PROVIDER_INTEGRATION=1`:

```bash
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
```

Each provider case also requires its own credentials or endpoint variables. The harness covers OpenAI, Anthropic, Gemini, OpenAI-compatible endpoints, Ollama, OpenRouter, and Codex CLI, and skips cases whose required environment is missing.

## Golden Evals

Golden evals are in `scripts/run_golden_evals.py` and cover agent behavior across turns, safety gates, memory use, consolidation expectations, provider/tool-call accuracy, durable plan completion, repair success, approval correctness, honest failure reporting, latency, cost, and repo-regression guardrails. The output includes per-case scores plus scored category summaries.

Fast path:

```bash
python scripts/run_golden_evals.py --backend memory --provider mock
```

Memvid path:

```bash
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

Each Memvid golden case should use its own memory/log directory to avoid `.mv2` lock contention.

## Release Validation

Use `docs/RELEASE_CHECKLIST.md` before tagging or publishing an alpha build. The checklist includes compile, lint, typecheck, unit tests, golden evals, web build/test, optional Memvid/MCP integration, and packaging/Docker smoke checks.

## Failure Handling

If a validation run fails:

1. Keep the failed command and error text.
2. Do not promote memory from that failed run unless the failure itself is useful episodic evidence.
3. Add or update focused tests before broad refactors.
4. For repair loops, use repair branches and keep `repair.validate`, `repair.review`, and `git.commit` gates intact.
