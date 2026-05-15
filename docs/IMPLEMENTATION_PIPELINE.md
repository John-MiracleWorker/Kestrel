# Implementation Pipeline

## Phase 0 — Baseline verification

Commands:

```bash
pytest -q
python -m compileall -q src tests
```

Expected: pass.

## Phase 1 — Memvid SDK hardening

Goals:

- install `memvid-sdk`
- verify `create`, `use`, `put`, `find`, `seal`, `verify`, `close`
- ensure `create(path)` is never called on existing files
- test read-only mode
- test lock behavior if available
- normalize returned hit shapes

Deliverables:

- hardened `MemvidBackend`
- `tests/integration/test_memvid_backend_integration.py`
- test gated by `RUN_MEMVID_INTEGRATION=1`

## Phase 2 — Chat-capable runtime

Goals:

- confirm `nest-agent chat --backend memory --provider mock` works
- confirm `nest-agent chat --backend memvid --provider openai` works
- add persistent session ID support
- add `/memory`, `/context`, `/tools`, `/exit` chat commands

Deliverables:

- CLI integration tests
- transcript smoke test

## Phase 3 — Native tool calling

Goals:

- upgrade OpenAI provider to native tool/function calling
- keep JSON envelope fallback
- add provider error/retry handling
- add streaming option

Deliverables:

- `OpenAIResponsesProvider` native tool support
- tests with mocked OpenAI response objects

## Phase 4 — Tool expansion

Add tools:

- `repo.search`
- `repo.map`
- `patch.apply`
- `test.run`
- `git.status`
- `git.diff`
- `memory.consolidate`
- `memvid.verify`
- `memvid.doctor`

Every high-risk tool must have:

- schema
- risk classification
- permission gate
- unit tests
- event log entry

## Phase 5 — Consolidation engine

Goals:

- identify candidates from working/episodic memory
- score candidates
- detect duplicate/conflicting memories
- create promotion records
- require evidence refs
- write promoted records to upper layer

Tests:

- failure does not become procedure
- repeated success becomes procedure
- single correction does not become policy
- validated user preference can become semantic memory
- manual high-confidence rule can become policy only when enabled

## Phase 6 — Evaluation harness

Golden scenarios:

1. Remember user correction across sessions.
2. Retrieve relevant prior failure before repeating it.
3. Use procedural recipe after repeated success.
4. Block policy write from a single event.
5. Refuse workspace path escape.
6. Allow shell only with config.
7. Verify `.mv2` files after a turn.
8. Recover context without full transcript stuffing.

Metrics:

- retrieval latency per layer
- context size
- tool-loop count
- memory writes per turn
- promotion precision
- false promotion rate
- user correction adherence

## Phase 7 — Optional server/UI

Implement after CLI is stable:

- FastAPI `/chat`
- `/memory/search`
- `/memory/verify`
- `/sessions/{id}`
- streaming responses
- small web frontend or TUI

## Phase 8 — Packaging

- Dockerfile
- install script
- `.env.example`
- release checklist
- project template
- docs for memory migration/export
