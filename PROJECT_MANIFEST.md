# Project Manifest — Nested MV2 Full Agent Scaffold

Generated: 2026-05-15

## Purpose

This repository is a concrete handoff scaffold for a full conversational agent runtime built around Nested Learning-inspired memory layers and Memvid v2 `.mv2` storage.

It answers the scope gap from the earlier memory-only scaffold: this version includes the chat runtime, LLM provider abstraction, tool registry, permission gates, event logging, CLI chat, optional API scaffold, validation docs, and Codex build pipeline.

## Important files

- `README.md` — quick start and current status
- `AGENTS.md` — concise Codex instructions
- `docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md` — primary prompt to hand to Codex
- `docs/FULL_AGENT_SPEC.md` — full product/system specification
- `docs/RUNTIME_WIRING.md` — one-turn and multi-turn wiring
- `docs/IMPLEMENTATION_PIPELINE.md` — ordered build phases
- `docs/TEST_MATRIX.md` — unit/integration/golden test plan
- `src/nested_memvid_agent/agent.py` — complete agent loop scaffold
- `src/nested_memvid_agent/cli.py` — CLI including `chat`
- `src/nested_memvid_agent/tools/` — tool registry and built-ins
- `src/nested_memvid_agent/llm/` — mock and OpenAI provider scaffolds
- `src/nested_memvid_agent/backends/memvid_backend.py` — `.mv2` backend adapter
- `tests/test_agent_runtime.py` — runtime loop tests
- `tests/test_tools.py` — tool safety and memory tests
- `tests/integration/test_memvid_backend_integration.py` — gated Memvid integration test

## Verified locally in this environment

```bash
pytest -q
# result: 17 passed, 1 skipped

python -m compileall -q src tests scripts
# result: passed

PYTHONPATH=src python scripts/run_agent_smoke.py
# result: mock chat + memory tool smoke passed
```

`ruff` and `mypy` are listed in dev dependencies, but they were not installed in this execution environment. Codex should install dev extras and run:

```bash
ruff check .
mypy src
```

## Current capability

Works now with:

```bash
nest-agent chat --backend memory --provider mock --message "hello"
```

Expected next hardening:

```bash
nest-agent chat --backend memvid --provider openai --model <available-model>
```

## Definition of done for Codex

The agent is done when a user can talk to it, it can safely use tools, it persists memory in `.mv2` files, it retrieves/compiles nested memory context, it promotes validated memories through the consolidation pipeline, and the full test/eval suite passes.
