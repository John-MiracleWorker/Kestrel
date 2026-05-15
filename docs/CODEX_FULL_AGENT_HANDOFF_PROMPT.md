# Codex Full Agent Handoff Prompt

You are implementing a full local-first agent runtime named **Nested MV2 Agent**.

This is not just a RAG layer. The target is an OpenClaw/Hermes-style conversational agent with tools, state, memory, validation, and a nested-learning consolidation pipeline. The storage device for memory must be Memvid v2 `.mv2` files.

## Non-negotiable architecture

The agent is:

```text
LLM provider + chat runtime + tool registry + nested memory + context compiler + event log + consolidation pipeline + eval harness
```

Use one `.mv2` file per nested layer:

```text
working.mv2
episodic.mv2
semantic.mv2
procedural.mv2
policy.mv2
```

Do not use deprecated Memvid v1 QR/video behavior. Do not build around vector databases. Do not replace `.mv2` with SQLite/Postgres/Chroma. JSONL logs are allowed only as audit/debug logs, not as the memory database.

## Immediate baseline

Run:

```bash
pytest -q
python -m compileall -q src tests
nest-agent chat --backend memory --provider mock --message "hello"
```

Do not proceed until these pass.

## Phase 1: Harden Memvid backend

1. Install `memvid-sdk`.
2. Inspect the installed SDK signatures for `create`, `use`, `put`, `find`, `seal`, `verify`, `doctor`, and `close`.
3. Update `src/nested_memvid_agent/backends/memvid_backend.py` to match the installed SDK exactly.
4. Preserve the data-loss rule: never call `create(path)` if the file already exists.
5. Normalize all result shapes into `MemoryHit`.
6. Add `tests/integration/test_memvid_backend_integration.py` gated by `RUN_MEMVID_INTEGRATION=1`.
7. Test write → seal → verify → close → reopen → search.

## Phase 2: Make CLI chat production-usable

Current command:

```bash
nest-agent chat --backend memory --provider mock
```

Add slash commands:

- `/exit`
- `/tools`
- `/context <query>`
- `/memory <query>`
- `/doctor`
- `/session`

Add session persistence:

- default session ID if omitted
- explicit `--session-id`
- session events written to event log and episodic memory

## Phase 3: Provider hardening

Current providers:

- `MockLLMProvider`
- `OpenAIResponsesProvider`

Tasks:

1. Keep `MockLLMProvider` deterministic.
2. Make OpenAI provider chat-capable.
3. Add native tool calling for OpenAI Responses API.
4. Keep JSON-envelope fallback for portability.
5. Add retries, timeout config, and structured error mapping.
6. Add streaming support behind `--stream`.
7. Add mocked provider tests. Do not require real API keys for unit tests.

## Phase 4: Tooling expansion

Existing tools:

- `memory.search`
- `memory.write`
- `file.list`
- `file.read`
- `file.write`
- `shell.run`

Add:

- `repo.search`
- `repo.map`
- `patch.apply`
- `test.run`
- `git.status`
- `git.diff`
- `memvid.verify`
- `memvid.doctor`
- `memory.consolidate`

For every tool:

- define schema
- define risk level
- enforce workspace boundaries
- log execution
- write result/failure to working memory
- unit test success and failure path

High-risk tools must require config enablement first. Later add interactive approval.

## Phase 5: Nested Learning consolidation

Implement the consolidation pipeline as a controlled learning loop.

Candidate extraction:

- scan working memory and recent episodic memory
- identify failures, decisions, corrections, facts, and repeated procedures
- deduplicate by content hash/semantic similarity where Memvid supports it

Promotion rules:

- Working → Episodic for meaningful events and summaries
- Episodic → Semantic for validated facts
- Episodic → Procedural for repeated successful procedures
- Semantic/Procedural → Policy only with explicit permission and extreme confidence

Every promoted record must include:

- source record IDs
- source layer
- destination layer
- evidence refs
- confidence
- validation method
- promotion reason
- timestamp

Tests:

- one random success cannot become a procedure
- one random correction cannot become policy
- repeated validated success becomes procedural memory
- conflicting facts are flagged, not merged silently
- low-confidence memory is not promoted

## Phase 6: Evaluation harness

Build `scripts/run_golden_evals.py` and golden cases under `golden/`.

Required evals:

1. The agent remembers a correction across sessions.
2. The agent retrieves a previous failure before repeating it.
3. The agent uses a procedural recipe after repeated successes.
4. The agent refuses a path escape.
5. The agent blocks shell without enablement.
6. The agent verifies `.mv2` files.
7. The agent compiles useful context under budget.
8. The agent avoids writing policy from an ordinary event.

Each eval should output JSON with pass/fail, latency, memory hits, context chars, and tool count.

## Phase 7: Optional API/UI

After CLI is solid:

- add FastAPI app
- `/chat`
- `/memory/search`
- `/memory/verify`
- `/sessions/{session_id}`
- SSE streaming
- optional Textual/Rich TUI

Do not start here. CLI first. Agent first. Pretty UI later.

## Phase 8: Final acceptance test

The final project should pass:

```bash
pip install -e '.[memvid,openai,dev]'
pytest -q
RUN_MEMVID_INTEGRATION=1 pytest -q tests/integration
nest-agent init --backend memvid --memory-dir .nest/memory
nest-agent chat --backend memvid --provider openai --model <available-model> --message "Remember that I prefer concise answers."
nest-agent chat --backend memvid --provider openai --model <available-model> --message "What do you remember about my answer style?"
```

Expected behavior:

- memory persists across turns
- memory persists across process restarts
- model gets compiled nested context
- relevant memory is used without dumping whole transcript
- `.mv2` files verify successfully
- no high-risk tool runs without permission

## Definition of done

The agent is built when a user can talk to it from CLI, it can call tools, it persists memory in `.mv2` files, it can retrieve and compile nested context, it learns via controlled consolidation, and the test/eval suite proves the behavior.
