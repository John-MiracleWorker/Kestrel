# Testing Guide

Last updated: 2026-07-19

Kestrel's fast test path is deterministic: it uses `InMemoryBackend` plus the mock LLM provider. Memvid, MCP, provider, executable-skill container, and platform integrations stay behind explicit environment flags.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r config/python-build-bootstrap.txt
python -m pip install --no-build-isolation -e '.[memvid,openai,anthropic,gemini,server,mcp,keyring,dev]'
npm ci --prefix web
```

## Core Validation

Run these for normal development:

```bash
python -m compileall -q src tests scripts
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
make golden
python benchmarks/real_agent_learning_benchmark.py --output benchmark_results/agent_learning_gate.json
PYTHONPATH=src python -m nested_memvid_agent.cli chat --backend memory --provider mock --message "hello"
```

`python -m pytest -q` is preferred over a global `pytest` binary so subprocess fixtures use the active interpreter. CI also sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` so local editor or environment plugins cannot leak into deterministic mock-backed tests.

The commands in this section are direct operator/developer commands. They do not imply a host-execution path for agent tools. Agent-invoked `test.run`, `lint.run`, repair validation, and `codex.exec` always use `NEST_AGENT_VALIDATION_CONTAINER_IMAGE` through a private, networkless, secret-free, read-only OCI snapshot and fail closed when it is absent or mutable. The image must already be present locally under its immutable `name@sha256:<64 hex>` reference and contain the exact requested executable and dependencies; Kestrel launches with `--pull=never`. Check the configuration before enabling the master gate:

```bash
export VALIDATION_IMAGE='registry.example/kestrel-validation@sha256:<64 hex>'
docker pull "$VALIDATION_IMAGE"
nest-agent doctor --allow-shell --validation-container-image "$VALIDATION_IMAGE"
```

`doctor` validates reference shape and reports which enabled tools require the image. The actual local image and command dependencies are exercised only by a contained tool run. The pinned Python image below is sufficient for containment fixtures and their tiny scripts, but is not a general project test/lint image. `codex.exec` additionally needs a Codex binary and remains networkless and credential-free, so remote-model delegation is intentionally unavailable through that tool.

The agent learning gate is not a seeded retrieval fixture. Its first task causes a deterministic
tool failure and validated changed-strategy resolution through `NestedMV2Agent`; the runtime must
persist provenance-linked `FailureEpisode` and `LessonCard` records. Its second task must retrieve
that exact lesson as untrusted user-role evidence and improve from control failure to treatment
success. Any missing evidence, validation metadata, exact-call approval, transfer, or expected
outcome exits nonzero. Real-provider learning remains a separate optional evaluation below.

The proactive-routine slice has a smaller deterministic safety gate for scheduler changes:

```bash
python -m pytest -q \
  tests/test_state_store.py \
  tests/test_routines.py \
  tests/test_routine_loop.py \
  tests/test_server_routine_routes.py \
  tests/test_operational_metrics.py \
  tests/test_server_runtime_routes.py \
  tests/test_support_bundle.py
python -m pytest -q \
  tests/test_cli.py::test_routines_cli_creates_enables_ticks_and_reports_history \
  tests/test_cli.py::test_routines_cli_tick_does_not_resume_unrelated_queued_run \
  tests/test_cli.py::test_routines_cli_tick_selectively_recovers_admitted_routine_crash_window
```

These cases cover schema migration, disabled drafts, revision conflicts, raw-secret rejection, minimum intervals, concurrent claims, misfires, overlap, lease-generation fencing, disable/revise/delete races, atomic internally scoped run admission and task-graph initialization, crash-window recovery, persisted provenance, headless approval expiry, owner API auth, scoped CLI dispatch/history, bounded lifecycle shutdown races, readiness/metrics loop health, private SQLite modes/symlink defenses, and prompt-free routine aggregates in support bundles.

Repair and executable-skill containment have focused adversarial gates:

```bash
python -m pytest -q tests/test_repair_integrity.py tests/test_worker_isolation.py
python -m pytest -q tests/test_extension_policy.py tests/test_extension_runner.py tests/test_skill_containment.py
docker pull 'python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
RUN_EXTENSION_SANDBOX_INTEGRATION=1 \
KESTREL_EXTENSION_TEST_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3' \
python -m pytest -q tests/integration/test_extension_container_integration.py
```

The repair cases cover signed/tamper-evident receipts, redacted validation output, pre/post validation drift, Git-root and `.nest` symlink refusal, literal-byte commits with clean filters disabled, index/HEAD compare-and-swap gates, approval-bound rollback snapshots, and recovery quarantine. The Docker integration verifies the same no-network/read-only/nonroot/resource-bounded runner used by executable skills, including private read snapshots and timeout orphan cleanup, and is a required CI/release gate. Local runs remain explicitly enabled because they require a Docker daemon and the exact pre-pulled digest-pinned image. Once `RUN_EXTENSION_SANDBOX_INTEGRATION=1` is set, a missing Docker executable or invalid image setting fails instead of skipping.

## Lint, Types, and Web

```bash
python -m ruff check scripts src tests
python -m mypy src
npm run test --prefix web
npm run build --prefix web
```

`npm run test --prefix web` runs the TypeScript build in no-pretty mode and the Vitest jsdom suite. `npm run build --prefix web` runs TypeScript plus the Vite production build.

CI runs the web app in its own Node 22 job with `npm ci`, `npm test`, and `npm run build`.
A credential-free foundation job runs the Memvid v2 and stdio MCP fixtures, and a separate Docker-backed job preloads the pinned Python image and runs the executable-skill containment integration on every pull request and supported branch push. The Docker build starts only after Python, web, foundation, and containment jobs pass.

## Installer Validation

Fast installer tests cover shell syntax, help text, dry-run defaults, the Python 3.11-through-3.13 cap, Linux ARM64 refusal, immutable release-SHA/moved-tag checks, safe Memvid/mock commands, opt-in detached server/web UI launch planning, disabled-by-default server behavior, and refusal to overwrite non-git nonempty directories:

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

## Foundational Memvid Integration

The Memvid tests require `memvid-sdk` from the `memvid` extra and are skipped unless
`RUN_MEMVID_INTEGRATION=1` is set. They require no provider credentials and run in the dedicated CI
foundation job:

```bash
VALIDATION_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
docker pull "$VALIDATION_IMAGE"
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_memory_system.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden --validation-container-image "$VALIDATION_IMAGE"
```

The tests create temporary `.mv2` files, write records, seal, verify, close, reopen, search, round-trip context-frame metadata, validate the Soul/self layer, and read run-scoped `complete.mv2` capsules.

## Foundational MCP Integration

The local stdio MCP fixture is gated by `RUN_MCP_INTEGRATION=1`. It uses a bundled fixture process,
requires no network endpoint or credential, and runs in the dedicated CI foundation job:

```bash
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
```

The fixture server is `tests/integration/fixtures/stdio_mcp_server.py`. It proves managed stdio discovery and invocation through `MCPManager`.

## Optional Provider Integration

Live provider coverage is gated by `RUN_PROVIDER_INTEGRATION=1`:

```bash
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
```

Each provider case also requires its own credentials or endpoint variables. The harness covers OpenAI, Anthropic, Gemini, OpenAI-compatible endpoints, Ollama, Ollama Cloud, OpenRouter, and Codex CLI, and skips cases whose required environment is missing. In addition to text generation and streaming, every provider that advertises native tools must return one exact, schema-valid `certification.echo` call; the fixture never executes that call. Ollama Cloud + `gpt-oss:120b` has been locally validated against both memory and Memvid live learning/golden eval paths.

## Live Learning E2E Eval

Use `scripts/run_live_learning_eval.py` when you need to prove a real provider can drive Kestrel's learning and safety surfaces, not just answer a smoke prompt. It uses isolated memory, log, state, workspace, and secret-store paths under the output root and never needs access to the operator's real `.nest` runtime state.

Recommended Ollama Cloud path:

```bash
export OLLAMA_API_KEY=...  # do not commit or paste this into logs
export KESTREL_IT_OLLAMA_CLOUD_MODEL="gpt-oss:120b"
python scripts/run_live_learning_eval.py \
  --provider ollama-cloud \
  --model "$KESTREL_IT_OLLAMA_CLOUD_MODEL" \
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

The live E2E cases cover provider handshake, durable memory retrieval after reopen, correction-frame capture, nested-learning promotion gates, task-capsule learning-signal extraction, unapproved high-risk tool blocking, behavior-delta activation logging, and postflight verification of all six memory layers with zero policy writes. Missing credentials/model configuration are reported by env-var name only; secret values are not printed. Each invocation creates a unique child beneath the supplied output root; never point this harness at an operator's real `.nest` memory.

## Learning Architecture Eval Harness

Use `scripts/eval_learning_architecture.py` when you need the full controlled self-modification loop in one report:

```text
trace/capsule -> proposal -> gate -> replay -> compile -> tool preflight -> activation -> outcome -> rollback
```

Fast deterministic path:

```bash
python scripts/eval_learning_architecture.py \
  --provider mock \
  --backend memory \
  --all \
  --report .nest/evals/mock-learning-report.md
```

JSON path for automation:

```bash
python scripts/eval_learning_architecture.py --provider mock --backend memory --all --json
```

Live OpenAI smoke path:

```bash
RUN_LIVE_LEARNING_EVALS=1 \
OPENAI_API_KEY=... \
python scripts/eval_learning_architecture.py \
  --provider openai \
  --model "${NEST_AGENT_EVAL_MODEL:-gpt-5-mini}" \
  --backend memory \
  --scenario live_provider_smoke_learning_loop \
  --max-llm-calls 3 \
  --max-cost-usd 0.50 \
  --report .nest/evals/live-learning-report.md
```

Rules:

- Live providers are skipped unless `RUN_LIVE_LEARNING_EVALS=1`.
- `provider=openai` also requires `OPENAI_API_KEY`.
- `provider=openai-compatible` requires `--base-url`, `NEST_AGENT_BASE_URL`, or `OPENAI_COMPATIBLE_BASE_URL`.
- `--model` overrides `NEST_AGENT_EVAL_MODEL`; mock defaults to `mock`.
- Call, tool, cost, and timeout guards fail before the next guarded action would exceed the configured limit.
- Reports redact API-key-like strings, bearer tokens, auth headers, and secret/token fields.
- Normal CI should run only mock evals. The live integration test is skipped unless both `RUN_LIVE_LEARNING_EVALS=1` and `OPENAI_API_KEY` are present:

```bash
RUN_LIVE_LEARNING_EVALS=1 OPENAI_API_KEY=... python -m pytest -q tests/integration/test_live_learning_architecture_eval.py
```

The harness proves integration of existing gates and ledgers. It does not prove broad live-model quality, exact natural-language output, all provider pricing, UI behavior, automatic policy activation, or autonomous code mutation.

## Golden Evals

Golden evals are in `scripts/run_golden_evals.py` and cover agent behavior across turns, safety gates, memory use, consolidation expectations, provider/tool-call accuracy, durable plan completion, repair success, approval correctness, honest failure reporting, latency, and repo-regression guardrails. The output includes per-case scores plus category summaries. Wall-clock latency is measured for every case and becomes a top-level fail-closed gate only when `--max-case-latency-ms` is configured. Provider usage and pricing are not currently supplied by every case, so cost is reported as `unmeasured` or `partially_measured`, with a `null` acceptance result; zero is never presented as a passing cost measurement.

Fast path:

```bash
VALIDATION_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
docker pull "$VALIDATION_IMAGE"
python scripts/run_golden_evals.py --backend memory --provider mock --validation-container-image "$VALIDATION_IMAGE" --max-case-latency-ms 45000
```

Memvid path:

```bash
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden --validation-container-image "$VALIDATION_IMAGE" --max-case-latency-ms 45000
```

The pinned image is part of the golden evidence: the procedural-promotion case exercises real no-network OCI repair validation and intentionally fails closed when the image is absent or mutable. The 45-second per-case ceiling is for credential-free mock-provider release jobs with the validation image already pulled; it leaves headroom for the OCI cold-start case while still catching hangs or severe regressions. Each Memvid golden case should use its own memory/log directory to avoid `.mv2` lock contention. Live-provider latency depends on model, network, and quota conditions, so measure a representative baseline and set an explicit environment-appropriate ceiling rather than reusing the mock threshold. Live golden evals also support provider/model args; the Ollama Cloud `gpt-oss:120b` memory and Memvid paths have passed locally after deterministic `/search` routing and the durable plan wait-window hardening.

## Release Validation

Use `docs/RELEASE_CHECKLIST.md` before tagging or publishing a build. The checklist includes
compile, metadata alignment, lint, typecheck, unit tests, golden evals, the deterministic
end-to-end agent learning gate, web build/test, required
credential-free Memvid/MCP integration, executable-skill OCI containment, and packaging/Docker smoke checks.

## Failure Handling

If a validation run fails:

1. Keep the failed command and error text.
2. Do not promote memory from that failed run unless the failure itself is useful episodic evidence.
3. Add or update focused tests before broad refactors.
4. For repair loops, use repair branches and keep `repair.validate`, `repair.review`, and `git.commit` gates intact.
