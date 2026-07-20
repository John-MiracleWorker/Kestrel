# Implementation Pipeline

Last updated: 2026-07-19

This file records what has landed and what should be hardened next. `docs/IMPLEMENTATION_STATUS.md` is the authoritative current truth table.

## Phase 0 - Baseline Verification

Status: implemented.

Current fast baseline:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "hello"
```

## Phase 1 - Memvid SDK Hardening

Status: implemented for the current adapter.

Landed:

- Lazy `memvid_sdk` import.
- Safe existing-file open path through `use(...)`.
- Missing-file creation only.
- Lexical-first defaults to avoid accidental embedding/API-key calls.
- Hit normalization into `MemoryHit`.
- `seal`, `verify`, `doctor`, `stats`, and `close` surfaces.
- Context-frame and run-capsule Memvid integration coverage.
- Gated tests behind `RUN_MEMVID_INTEGRATION=1`.

Remaining:

- Re-check SDK signatures on each dependency upgrade.
- Add coverage for encrypted `.mv2e` if that becomes a supported runtime target.

## Phase 2 - Conversational CLI Runtime

Status: implemented.

Landed:

- `nest-agent chat` one-shot and interactive modes.
- `--session-id`.
- Slash commands: `/exit`, `/tools`, `/context`, `/memory`, `/doctor`, `/session`, plus deterministic `/search` and `/memory search` tool routing.
- Background run/status/approval CLI surfaces.
- Deterministic mock provider path.

Remaining:

- Improve human-facing transcript rendering for long tool traces.
- Add richer resumable multi-turn CLI ergonomics.

## Phase 3 - Provider Hardening

Status: partially implemented.

Landed:

- Mock provider.
- OpenAI Responses provider.
- OpenAI-compatible chat completions provider.
- Anthropic Messages, Gemini, OpenRouter/Ollama aliases, native Ollama Cloud provider, and Codex CLI provider.
- Provider capability metadata.
- Retryable provider fallback wrapper.
- OpenAI Responses/OpenAI-compatible/Anthropic/Gemini streaming deltas when the SDK/API stream surface is available.
- Portable JSON tool envelope.

Remaining:

- Broader native tool-calling parity across every provider.
- Richer provider-specific streaming/context/JSON-mode handling.
- Broader live integration tests for real provider variants; Ollama Cloud + `gpt-oss:120b` has passed local live golden and live-learning E2E validation.
- Richer context/JSON-mode handling per provider.

## Phase 4 - Tool Expansion and Safety

Status: implemented for the first serious local-agent slice.

Landed built-ins include:

- `memory.search`
- `memory.write`
- `memory.consolidate`
- `memory.learn`
- `memory.conflicts`
- `memory.inspect`
- `memory.export`
- `memory.import`
- `context.pack`
- `context.expand`
- `capsule.summarize`
- `capsule.apply`
- `file.list`
- `file.read`
- `file.write`
- `shell.run`
- `repo.search`
- `repo.map`
- `patch.apply`
- `test.run`
- `lint.run`
- `git.status`
- `git.diff`
- `git.branch`
- `git.commit`
- `memvid.verify`
- `memvid.doctor`
- `memvid.stats`
- `skill.install`
- `diagnosis.classify`
- `diagnosis.recall`
- `codex.exec`
- repair tools

Landed safety behavior:

- Workspace/path boundaries.
- Timeout enforcement.
- Config enablement gates.
- Exact-call approval gates.
- Repair branch reviewer gate before repair commits.
- Structured failures at tool boundaries.

Remaining:

- More production-grade UX around approval review.
- Stronger sandboxing for skill runtimes and tool side effects.

## Phase 5 - Consolidation and Nested Learning

Status: partially implemented.

Landed:

- `NestedLearningKernel`.
- Context-flow and optimizer-trace metadata.
- Promotion gate metadata for accepted/rejected decisions.
- `memory.learn` and `memory.consolidate`.
- Run-scoped task capsules.
- `capsule.summarize` preview path.
- `capsule.apply` high-risk, config-gated, approval-gated path.
- Policy memory constraints for explicit instruction, high validation, repeat evidence, config enablement, and review.

Remaining:

- Stronger validation loops before auto-consolidation.
- Richer conflict/correction lifecycle.
- Better operator review UI for proposed learning signals.

## Phase 6 - Evaluation Harness

Status: implemented for mock, optional Memvid, behavior-delta replay, and opt-in live-provider learning paths.

Current commands:

```bash
python scripts/run_golden_evals.py --backend memory --provider mock
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
python scripts/eval_behavior_deltas.py --scenario tests/evals/behavior_deltas/policy_write_requires_approval.json
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memory --output-root /tmp/kestrel-live-learning-memory
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
```

Remaining:

- Add provider-specific golden suites for the full matrix; the Ollama Cloud path is stable locally.
- Add long-running regression fixtures for scheduler/repair flows.

## Phase 7 - API, Web, Channels, MCP, Skills

Status: partially implemented.

Landed:

- FastAPI app and local React/Vite workbench.
- SSE run timeline events.
- Approval routes.
- Memory/context routes.
- Skills registry and upload/install path.
- Behavior-delta review API, approval-gated actions, web review panel, read-only Learning Dashboard aggregates, and read-only product-readiness report API.
- Managed stdio MCP sessions and gated integration fixture.
- Multi-channel ingress and generic HMAC webhook verification.
- Local bearer/API-key auth option.
- Digest-pinned OCI execution for executable skills with default-deny read-snapshot-only scopes, host-runtime refusal, resource bounds, verified cleanup, and a required real-Docker integration gate.

Remaining:

- Production multi-user auth and isolation.
- MCP SSE/streamable HTTP fixtures and soak tests.
- Production bot identity verification and rate-limit behavior.
- Managed skill dependencies, portable non-Docker engines, richer explicit network grants, and containment soak coverage.

## Phase 8 - Scheduler, Subagents, and Safe Repair

Status: partially implemented.

Landed:

- Durable task graph records.
- Deterministic starter DAG.
- Ready-task filtering.
- Bounded opt-in autonomous scheduler.
- In-process planner/worker/reviewer profiles.
- Diagnosis metadata on failed task/subagent records.
- Coherent repair worktree preparation and task-DAG artifact handoff.
- Repair patch/validate/orchestrate/rollback tools.
- Process-signed repair validation/review artifacts, literal-tree commit gate, exact-digest rollback snapshots, and recovery quarantine.
- Disabled-by-default proactive routines with revisioned owner controls, durable occurrence history, workbench editing, and idempotent manual run-now.

Remaining:

- Dynamic plan revision and multi-candidate reviewer selection across isolated workers.
- Codex-backed worker orchestration.
- Fully autonomous patch synthesis beyond the existing explicit approval, review, test, commit, and rollback controls.

## Phase 9 - Packaging and Release

Status: implemented for alpha packaging.

Landed:

- `Makefile` validation/package targets.
- Dockerfile.
- Docker Compose.
- `.env.example`.
- Deployment, memory operations, security, and release checklist docs.

Release validation lives in `docs/RELEASE_CHECKLIST.md`.
