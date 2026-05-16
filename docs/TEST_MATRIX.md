# Test Matrix

Last updated: 2026-05-16

## Unit and Contract Tests

Core unit coverage includes:

- model and dataclass validation
- layered memory thresholds and retrieval ranking
- in-memory backend contract
- Memvid backend contract without requiring `memvid-sdk`
- context compiler and pseudo-context packer budget behavior
- context-frame metadata and conflict handling
- consolidation and Nested Learning promotion gates
- task capsule summary/apply behavior
- provider parser, capability metadata, fallback, and streaming surfaces
- tool schemas, path safety, timeout enforcement, enablement gates, and exact-call approvals
- diagnosis classification and failure-memory recall
- repair branch primitives, validation gates, reviewer artifacts, and commit gates
- skill manifest validation, install gates, and instruction/Python/shell-list runtimes
- CLI subcommands for chat, context, tools, approvals, memory, doctor, run, and status
- state-store migrations, terminal transition immutability, approval immutability, and replay safety

Run:

```bash
python -m pytest -q
```

## Runtime Tests

Runtime tests cover:

- one-turn mock chat
- portable JSON tool-call loop
- malformed tool-call handling
- max tool rounds stop
- user/tool/failure/final-summary memory writes
- event log writes
- background run creation, status, events, cancellation, and approval blocking
- approval resume with tool result persistence
- task graph creation and ready-task filtering
- bounded autonomous scheduler drains
- subagent records and failure diagnosis metadata
- task capsule creation after completed runs
- full approval-to-capsule smoke flow

## Integration Tests

Gated by env vars:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
```

Memvid integration must:

- create temp `.mv2` files only when missing
- write records
- seal
- verify
- close and reopen
- search again and confirm persistence
- round-trip context-frame metadata
- summarize run-scoped `complete.mv2` capsules

MCP integration must:

- launch the stdio fixture
- connect through `MCPManager`
- discover remote tools
- invoke a remote tool
- shut down the managed session

OpenAI/provider live integration remains a future opt-in suite. Unit tests should mock provider responses by default.

## Golden Evals

Golden evals are executable today:

```bash
python scripts/run_golden_evals.py --backend memory --provider mock
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

The current golden set checks behavior such as:

- remembering a correction across turns
- retrieving a previous failure before repeating it
- using a procedural recipe only after repeated validated success
- refusing workspace path escape
- blocking shell without enablement
- verifying `.mv2` files
- compiling useful context under budget
- avoiding policy writes from ordinary events

Golden evals should report pass/fail, relevant diagnostics, memory hits, context size, tool count, and failure reasons. They are behavioral checks across turns, not simple unit tests.

## Web Validation

```bash
npm run test --prefix web
npm run build --prefix web
```

The web package uses Vite, React, and TypeScript. The test command currently performs a TypeScript build; the build command also produces the static assets mounted by the FastAPI server.

## Packaging Validation

```bash
python -m pip install -e '.[memvid,openai,server,mcp,dev]'
nest-agent doctor --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "packaging smoke"
docker build -t kestrel-agent:local .
docker run --rm kestrel-agent:local nest-agent doctor --backend memory --memory-dir /tmp/kestrel-memory --provider mock
```
