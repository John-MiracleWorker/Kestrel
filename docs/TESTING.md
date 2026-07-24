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

## Provider Certification Evidence

Provider support has three separate meanings:

- **Implementation** means an adapter and capability declaration exist in the source tree.
- **Readiness** means this machine appears configured to try the adapter. It is based only on
  redacted prerequisite checks such as environment-variable presence, a configured base URL, or a
  required local executable.
- **Assurance** means accepted evidence for a specific provider, model, code subject, test profile,
  and time window passed the certification policy. Readiness never raises assurance.

`nest-agent product provider-certification --json` and
`GET /api/product/provider-certification` return the public
`kestrel.provider_certification.v2` matrix under
`kestrel.provider_certification_policy.v1`. The rows cover `mock`, `lm-studio`, `ollama`, `openai`,
`openai-compatible`, `ollama-cloud`, `openrouter`, `deepseek`, `kimi`, `anthropic`, `grok`,
`gemini`, and `codex-cli`. A row contains these assurance dimensions:

| Dimension | Meaning |
|---|---|
| `generate` | One exact non-streaming response passed. |
| `stream` | Streaming completed with the exact expected response, or the adapter explicitly does not support streaming. |
| `native_tools` | One exact schema-valid native tool call passed, or native tools are explicitly unsupported. |
| `tool_normalization` | The provider result passed Kestrel's canonical tool-call normalization contract. This is mandatory release evidence. |
| `learning_e2e` | The learning evaluation required by the evidence profile passed; release evidence requires both memory and Memvid learning plus an unchanged-policy result. |

Every dimension is one of `pass`, `fail`, `not_run`, `not_supported`, or `stale`. A skipped test is
`not_run`, never `pass`. For release certification, `not_supported` is accepted only for `stream`
or `native_tools` when Kestrel's adapter capability contract declares that feature unsupported; it
cannot excuse an advertised capability or the mandatory tool-normalization contract.

Each provider has one of these certification states:

| State | Claim |
|---|---|
| `implemented` | The normal adapter exists, but no stronger accepted receipt is available. |
| `mock_tested` | Deterministic mock-backed contract evidence passed for the exact subject. |
| `credential_free_integration_tested` | A qualifying credential-free endpoint or local integration passed. |
| `locally_live_tested` | A real provider/model passed qualifying local live evidence. |
| `release_certified` | Trusted, authenticated, fresh release-profile evidence passed every mandatory case for the exact subject. |
| `experimental` | The surface exists but is intentionally below the normal implemented-support contract. |

The report also records `tested_models`, `tested_profiles`, `last_tested`,
`missing_requirements`, and evidence IDs.
`last_tested` is `null` without an exact-scoped receipt; report generation time is never substituted
for test evidence.
Evidence is bound to the exact Git commit and tree digest, provider, model, profile, configuration
digest, runner kind, and start/completion timestamps. Wrong-subject, unauthenticated, untrusted, or
expired evidence cannot upgrade a row. Evidence for a shared adapter does not transfer between
provider names: for example, an `openai-compatible` receipt cannot certify OpenRouter, DeepSeek,
Kimi, Grok, or an unrelated compatible endpoint.

Live provider cases remain opt-in:

```bash
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
```

Each provider case requires its documented model plus credentials or endpoint configuration. The
harness covers all twelve real-provider rows. It tests exact generation and streaming, and every
provider that advertises native tools must return one exact, schema-valid `certification.echo`
call; the fixture never executes that call. A missing prerequisite produces a skip and therefore
cannot become certification evidence.

### Collect, Build, and Check

The evidence runner consumes exact `kestrel.provider_certification_cases.v1` and
`kestrel.live_learning_eval.v1` JSON, plus pytest JUnit XML from the exact live-provider test
module. It never infers a pass from console text. The JUnit parser selects only the requested
provider's three known parameterized cases. A failure or error becomes `fail`; a missing case or a
skip of an advertised capability becomes `not_run`; and a skip becomes `not_supported` only when
the implementation registry also declares that capability unsupported. The native-tool case
supplies both `native_tools` and `tool_normalization`, so neither can pass from an unrelated test.

Create the structured sources before collecting a release receipt:

```bash
RELEASE_COMMIT_SHA="$(git rev-parse 'HEAD^{commit}')"
RELEASE_TREE_DIGEST="$(git archive --format=tar "$RELEASE_COMMIT_SHA" | python -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
ARTIFACT_DIR="$(mktemp -d)"
STARTED_AT="$(python -c 'from datetime import UTC,datetime; print(datetime.now(UTC).isoformat().replace("+00:00", "Z"))')"

test -n "${OLLAMA_API_KEY:-}"  # inject it through the controlled environment
export KESTREL_IT_OLLAMA_CLOUD_MODEL='gpt-oss:120b'
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q \
  'tests/integration/test_provider_live_integration.py::test_live_provider_generate_smoke[ollama-cloud]' \
  'tests/integration/test_provider_live_integration.py::test_live_provider_stream_smoke[ollama-cloud]' \
  'tests/integration/test_provider_live_integration.py::test_live_provider_native_tool_call_certification[ollama-cloud]' \
  --junitxml="$ARTIFACT_DIR/provider-cases.xml"

python scripts/run_live_learning_eval.py \
  --provider ollama-cloud --model gpt-oss:120b --backend memory \
  --output-root /tmp/kestrel-live-learning-memory \
  > "$ARTIFACT_DIR/live-learning-memory.json"
python scripts/run_live_learning_eval.py \
  --provider ollama-cloud --model gpt-oss:120b --backend memvid \
  --output-root /tmp/kestrel-live-learning-memvid \
  > "$ARTIFACT_DIR/live-learning-memvid.json"
COMPLETED_AT="$(python -c 'from datetime import UTC,datetime; print(datetime.now(UTC).isoformat().replace("+00:00", "Z"))')"

python scripts/run_provider_certification.py collect \
  --provider ollama-cloud \
  --model gpt-oss:120b \
  --profile release \
  --level release \
  --source "$ARTIFACT_DIR/provider-cases.xml" \
  --source "$ARTIFACT_DIR/live-learning-memory.json" \
  --source "$ARTIFACT_DIR/live-learning-memvid.json" \
  --commit "$RELEASE_COMMIT_SHA" \
  --tree-digest "$RELEASE_TREE_DIGEST" \
  --runner-kind release_ci \
  --trusted-runner \
  --started-at "$STARTED_AT" \
  --completed-at "$COMPLETED_AT" \
  --output "$ARTIFACT_DIR/receipt.json"
EVIDENCE_ID="$(python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["evidence_id"])' "$ARTIFACT_DIR/receipt.json")"

python scripts/run_provider_certification.py build \
  --commit "$RELEASE_COMMIT_SHA" \
  --tree-digest "$RELEASE_TREE_DIGEST" \
  --evidence "$ARTIFACT_DIR/receipt.json" \
  --authenticated-evidence-id "$EVIDENCE_ID" \
  --release-target ollama-cloud \
  --max-evidence-age-hours 168 \
  --output "$ARTIFACT_DIR/provider-certification.json"

python scripts/run_provider_certification.py check \
  --report "$ARTIFACT_DIR/provider-certification.json" \
  --provider ollama-cloud \
  --require-state release_certified
```

`collect` also accepts `--config`, `--base-url`, and `--api-key-env` when the source profile needs
them; reports retain only non-secret configuration metadata and environment-variable names or
presence, never credential values. `build` accepts multiple `--evidence`,
`--authenticated-evidence-id`, and `--release-target` arguments plus an explicit `--now` for
deterministic freshness checks. When a tested model, endpoint, or API-key environment differs from
the built-in defaults, pass the same redacted `--config` to `collect` and `build` so the
configuration digest remains exact. `check` defaults to `release_certified`. Exit status `0`
means the requested gate passed, `1` means a valid report is below that gate, and `2` means input
or prerequisites were invalid or missing.

Each receipt requires a sorted, unique `source_digests` list containing the SHA-256 digest of every
raw source consumed by `collect`. The core derives the full canonical evidence ID from those
digests and all remaining receipt content. There is no caller-selected ID; source or receipt drift
therefore changes the ID that the caller-side release channel must authenticate.

The evidence level, profile, and runner kind must use one of these exact combinations:

| Evidence level | Eligible profiles | Eligible runner kinds |
|---|---|---|
| `mock` | `default`, `mock`, `release` | `mock`, `ci`, `local`, `release_ci` |
| `credential_free` | `default`, `credential_free`, `release` | `ci`, `local`, `release_ci` |
| `live` | `default`, `live`, `release` | `local`, `release_ci` |
| `release` | `release` | `release_ci` |

This permits staged lower-level checks inside release CI without combining evidence across
unrelated profiles. A release-level receipt still needs the trusted `release_ci` claim and exact-ID
authentication before it can raise assurance.

Collection normalizes already-persisted results and does not require a credential value to remain
in the environment after the provider call finishes. It still requires structural identity such as
a non-empty model and, for a generic `openai-compatible` target, an explicit base URL. `build`
captures the redacted readiness snapshot, but `check` evaluates the evidence-backed assurance gate
without turning current credential presence into certification authority. Use `product setup
--check` separately when the deployment itself must be ready to call the provider. Failure output
contains only requirement or environment-variable names, never their values.

Release certification requires one fresh receipt that claims the release level and `release`
profile for the exact provider, tested model, configuration digest, commit, and tree digest. Its
runner must be trusted `release_ci`, and the builder must separately authenticate its evidence ID.
`generate`, `tool_normalization`, `learning_memory`, `learning_memvid`, and `policy_unchanged` must
all pass. `stream` and `native_tools` must pass when advertised and may be `not_supported` only when
the registry declares them unsupported. A missing, failed, skipped, stale, wrong-scope, untrusted,
or unauthenticated mandatory case blocks `release_certified`.

JUnit is a conservative transport, not authentication. `--trusted-runner` records a claim in the
receipt, while `build --authenticated-evidence-id` is the separate caller-side authentication
step. If the controlled release job does not supply both, a release-level receipt does not raise
assurance or silently downgrade itself; a separate eligible lower-level receipt is required for a
lower state.

The repository does not commit a matrix that claims to describe the current tree. Adding such a
file would itself change the tree digest and could make the claim stale. Keep receipts and built
reports as exact-subject CI or release artifacts; commit only clearly synthetic test fixtures.

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

The pinned image is part of the golden evidence: the procedural-promotion case exercises real no-network OCI repair validation and intentionally fails closed when the image is absent or mutable. The 45-second per-case ceiling is for credential-free mock-provider release jobs with the validation image already pulled; it leaves headroom for the OCI cold-start case while still catching hangs or severe regressions. Each Memvid golden case should use its own memory/log directory to avoid `.mv2` lock contention. Live-provider latency depends on model, network, and quota conditions, so measure a representative baseline and set an explicit environment-appropriate ceiling rather than reusing the mock threshold. Live golden evals support provider/model arguments, but their result is not a current certification claim unless it is captured in a fresh accepted receipt for the exact subject.

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
