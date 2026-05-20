# Testing Guide

Last updated: 2026-05-17

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
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
PYTHONPATH=src python -m nested_memvid_agent.cli chat --backend memory --provider mock --message "hello"
```

`python -m pytest -q` is preferred over a global `pytest` binary so subprocess fixtures use the active interpreter. CI also sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` so local editor or environment plugins cannot leak into deterministic mock-backed tests.

## Lint, Types, and Web

```bash
python -m ruff check scripts src tests
python -m mypy src
npm run test --prefix web
npm run build --prefix web
```

`npm run test --prefix web` currently runs the TypeScript build in no-pretty mode. `npm run build --prefix web` runs TypeScript plus the Vite production build.

CI runs the web app in its own Node 22 job with `npm ci`, `npm test`, and `npm run build`, then runs a Docker build smoke job after Python and web checks pass.

## Installer Validation

Fast installer tests cover shell syntax, help text, dry-run defaults, Python 3.11+ detection, safe Memvid/mock commands, default detached server/web UI launch planning, opt-out server launch behavior, and refusal to overwrite non-git nonempty directories:

```bash
python -m pytest -q tests/test_install_script.py
bash -n install.sh
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_DRY_RUN=1 KESTREL_START_SERVER=0 bash install.sh
```

The local-clone installer smoke is optional because it installs dependencies into a temporary checkout and initializes real Memvid `.mv2` files. The test disables server auto-start so it does not leave a detached process running:

```bash
RUN_MEMVID_INTEGRATION=1 RUN_INSTALLER_INTEGRATION=1 python -m pytest -q tests/test_install_script.py::test_install_from_local_repo_smoke_with_memvid
```

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

## Live Learning E2E Eval

Use `scripts/run_live_learning_eval.py` when you need to prove a real provider can drive Kestrel's learning and safety surfaces, not just answer a smoke prompt. It uses isolated memory, log, state, workspace, and secret-store paths under the output root and never needs access to the operator's real `.nest` runtime state.

Recommended Ollama Cloud path:

```bash
export OLLAMA_API_KEY=...  # do not commit or paste this into logs
export KESTREL_IT_OLLAMA_CLOUD_MODEL="gpt-oss:120b"
python scripts/run_live_learning_eval.py \
  --provider ollama-cloud \
  --backend memory \
  --output-root ./tmp-live-kestrel/memory-live \
  --timeout-seconds 180
```

Full substrate path:

```bash
python scripts/run_live_learning_eval.py \
  --provider ollama-cloud \
  --model "$KESTREL_IT_OLLAMA_CLOUD_MODEL" \
  --backend memvid \
  --output-root ./tmp-live-kestrel/memvid-live \
  --timeout-seconds 180
```

The live E2E cases cover provider handshake, durable memory retrieval after reopen, correction-frame capture, nested-learning promotion gates, task-capsule learning-signal extraction, unapproved high-risk tool blocking, and behavior-delta activation logging. Missing credentials/model configuration are reported by env-var name only; secret values are not printed.

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
