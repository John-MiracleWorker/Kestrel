# Implementation Status

Last updated: 2026-05-23

This repository is a working local agent scaffold, not a finished Hermes/OpenClaw agent. The status below is intentionally literal so future Codex passes can harden the right layers without treating roadmap items as done.

## Working Now

- CLI chat loop with in-memory and Memvid `.mv2` memory backends.
- GitHub curl one-shot installer for the local Memvid-backed agent runtime, with dry-run, Python 3.11+ detection, web build, memory verify, deterministic `mock` smoke checks, default detached server startup, `/api/health` polling, and browser launch for the local web UI.
- Layered memory files for working, episodic, semantic, procedural, self, and policy layers.
- Context compiler that retrieves nested memory and builds the model prompt.
- MV2 context-frame model and token-aware pseudo-context packer that retrieves summaries first, deduplicates content, flags conflict metadata, and expands raw evidence on demand.
- Soul/self memory layer backed by `.nest/memory/self.mv2`, packed after policy and before procedural memory, with `self_model` context frames and conservative promotion gates.
- Deterministic mock provider for fast tests and reproducible golden evals.
- OpenAI Responses provider adapter using the portable JSON tool envelope.
- OpenAI Responses provider now exposes native streaming deltas when the SDK stream surface is available, while preserving the non-streaming fallback path.
- OpenAI-compatible chat completions provider for local/model-server endpoints, with native Chat Completions tool-call normalization and streaming text/tool-call assembly.
- OpenRouter and Ollama provider aliases route through the OpenAI-compatible contract with provider-specific defaults.
- Anthropic Messages and Gemini provider adapters now implement the same strict Kestrel response contract with native tool-use/function-call normalization and SDK-stream surface support.
- Codex CLI provider that can use local `codex exec` as the normal response engine.
- Provider capability metadata is exposed on built-in providers, and a retryable-error fallback wrapper can route from a primary provider to a configured secondary provider.
- Built-in tool registry with structured exception boundaries, timeout enforcement, tool/source/risk introspection, and exact-call approval gates for shell, file writes, patch application, tests, and Codex CLI delegation.
- Self-diagnosis primitives can classify common provider/tool/test/import/permission/MCP/sandbox failures and recall similar procedural/episodic failure lessons before retry.
- The default-on agentic failure cycle retrieves prior failure lessons before tool planning, records failed tool attempts as episodic `FailureEpisode` records, blocks unchanged same-action retries until a meaningful changed strategy is supplied, and returns a structured proof-of-work summary on agent turns. It can be disabled with `NEST_AGENT_DISABLE_AGENTIC_CYCLE=1`.
- Safe self-repair now has branch-isolated repair primitives: `repair.prepare`, `repair.status`, `repair.apply_patch`, `repair.validate`, `repair.orchestrate_validate`, and `repair.rollback`.
- Self-awareness tools now expose non-secret identity, runtime config, provider capability, memory layer, tool, skill, plugin, MCP, and state snapshots through `self.inspect`, `self.reflect`, `self.remember`, and approval-gated `self.propose_change`.
- Gated web context tools now provide read-only `web.search` and `web.fetch`, disabled by default behind `NEST_AGENT_ALLOW_WEB`, with deterministic mock backend support, citations, byte/time/result limits, redaction boundaries, and private/local network URL rejection.
- The first diagnosis-gated repair orchestration slice can run validation on an active repair branch, classify failures, recall prior lessons, and block repeated validation retries until the strategy changes.
- Skills now have a first manifest validation gate plus persisted validation/provenance metadata for discovered instruction capsules, and discovery returns structured counts, empty-directory messages, and validation errors.
- Local FastAPI control plane with background runs, SSE events, approvals, tools, MCP registry, skills registry/discovery reports, Secret Broker metadata routes, memory search, Soul/self routes, gated web routes, non-secret runtime config, channel CRUD, plugin/skill detail, memory inspect, cognition lesson/failure lists, and diagnosis classify/recall routes.
- Local operator web UI now exposes the implemented Kestrel runtime surfaces: provider/model/workspace run controls, real task graph and scheduler controls, approvals/history, Soul/self inspection and memory capture, gated web search, memory/context/lesson/failure views, filterable tool inventory, tool invocation, Secret Broker setup, MCP create/edit/lifecycle/manual invoke, skills discovery status, plugins, channels, observability, and non-secret runtime settings.
- Multi-channel ingress for Telegram Bot API updates, Discord message/interaction-shaped payloads, and generic/custom webhooks, with CLI and API routes.
- SQLite state store for runs, run steps, approvals, MCP servers, skills, plugins, task nodes, subagent runs, trace spans, promotion ledger outcomes, and behavior-delta ledger records, now initialized through schema version `11`.
- Paper-guided nested learning kernel with context-flow metadata, optimizer traces, conservative continuum-memory routing, and a `memory.learn` tool/API path.
- Closed-loop learning instrumentation now records promotion decisions, later outcomes, false-positive rates, never-retrieved compaction, and deterministic operator recommendations through `nest-agent memory ledger`.
- Memory learning decisions now use structured `ValidationEvidence`, expose computed validation scores and observed evidence refs, read active `LayerSpec` validation/repeat thresholds, and mark legacy raw-score inputs as deprecated metadata.
- Backend-neutral memory mutation now supports `upsert`, `tombstone`, `iter_records`, `get_record`, inactive-record filtering, correction frames, conflict-set frames, and audit retrieval with `include_inactive`.
- Lesson cards deduplicate similar same-category procedures, merge validation evidence refs, and update cumulative success/failure repeat counts instead of creating repeated procedural duplicates.
- Retention compaction can summarize and tombstone TTL-eligible working/episodic records through `memory.compact` / `nest-agent memory compact`; it is dry-run by default and skips stable layers by default.
- Hybrid/vector retrieval is config-gated through layer specs and only enables local vector settings explicitly; policy memory remains lexical.
- Run-scoped `complete.mv2` task capsules, preview-only capsule summaries, dry-run consolidation decisions, and approval-gated capsule apply.
- MCP server records now track health metadata, tool counts, capabilities, last sync/seen/call/error timestamps, session state, failure counts, and latency.
- MCP server records now persist vetting metadata: transport/network exposure, secret-env requirements, per-tool risk/approval classification, risk reasons, and recommended trust posture.
- MCP stdio servers now have a managed lazy session lifecycle with connect/disconnect/restart/health API routes, bounded operation timeouts, config-change teardown, and approval-by-default tool risk normalization.
- First task-graph and subagent run records exist, with durable task metadata, deterministic starter plan decomposition, in-process planner/worker/reviewer profiles, and UI/API surfaces.
- Background runs now execute through a durable graph wrapper above the chat loop: `PlannerNode`, `ExecutorNode`, `ReviewerNode`, `RecoveryNode`, `MemoryPromotionNode`, and `FinalizerNode`. The wrapper persists plan metadata, pauses for approval waits, records recovery diagnosis, enforces a reviewer gate before final completion, and keeps the existing chat/tool loop as the executor.
- Task nodes can now persist latest failure diagnosis and retry strategy metadata; failed subagents classify the failure, record a retry gate that requires changed strategy, and emit a diagnosis event tied back to the task.
- The task graph now exposes deterministic `ready_tasks` for scheduler/resume work: only approved queued/approved tasks with completed dependencies are eligible, and failed retry tasks remain blocked until their retry strategy explicitly allows a changed strategy.
- An opt-in autonomous scheduler can execute approved ready task nodes through the normal agent loop, drain bounded dependency cycles until idle, publish task/subagent events, and preserve approval blocking for high-risk tool calls.
- Scheduler and subagent task execution can opt into git worktree isolation, creating durable worker branches under a configured worktree root and recording isolation metadata on task results. Repair/code-modification scheduler tasks now default to git worktree isolation when the workspace supports worktrees, even if general worker isolation is disabled, and repair DAG tasks reuse one coherent git worktree for the run.
- Provider failures emit structured `diagnosis.classified` events so traces can explain the failure category and suggested playbook.
- Run traces now include durable span records for run, plan, `llm.request`, `tool.call`, `memory.write`, `approval.wait`, review, and eval-style recovery work.
- Repair mutation tools are high-risk, approval-gated, covered by exact-call approvals, disabled unless the matching capability is enabled, and refuse non-repair branches for patch/validate/rollback operations.
- Diagnosis-gated repair validation must remain approval-gated, refuse non-repair branches, recall similar lessons on failure, and block repeated validation retries when prior lessons exist unless a changed strategy is supplied.
- Repair commits on repair branches now require a durable `repair.review` artifact tied to a successful validation result and the current diff hash; `git.commit` refuses repair-branch commits when the review is missing, stale, or for a different branch.
- Local self-improvement is separated from remote publishing: `git.create_local_branch` and `git.export_patch` provide approval-gated local primitives, `git.commit` refuses protected branches, remote git/GitHub mutation flags default off, and `shell.run` blocks common publishing escape routes before subprocess execution.
- Background repair/commit goals now seed a repair-specific task DAG that orders inspect → isolation → patch → validation → `repair.review` → `git.commit`, so the durable plan itself encodes the reviewer gate before commit.
- A repair E2E smoke test now proves a seeded failure can flow through isolated repair branch preparation, approved patching, targeted validation, `repair.review` gate creation, and blocked unapproved commit; successful validation points to `create_repair_review_before_commit` instead of implying direct commit.
- Exact-call approved repair commits now stage only the reviewed repair files, complete after the current `repair.review` gate, and return the resulting `commit_sha` for traceability.
- Stale repair reviews now have a rollback proof: if a reviewed diff changes before commit, `git.commit` blocks with `repair_review_stale`, and approval-gated `repair.rollback` restores the repair branch while writing a durable rollback artifact under `.nest/repair_rollbacks/`.
- Terminal run records and approval decisions are replay-safe: late duplicate terminal transitions cannot overwrite original run results, and already-decided approval records cannot be flipped by replayed decisions.
- Approval resume flow now records the executed tool result back onto the already-approved approval record without reopening or flipping the decision, and a full-flow smoke test covers run creation, approval blocking, exact-call approval, resume, tool result persistence, traces, task graph, and capsule creation together.
- Skills can now run instruction, Python, and shell-list runtimes from their skill directory with path checks, JSON stdin, timeout bounds, discoverable validation errors, and provenance-backed episodic records; container runtime remains intentionally unavailable.
- `skill.install` provides an approval-gated local upload/install path for new skill capsules under the configured skills directory, with manifest validation and content hashes.
- A first plugin registry slice exists with public GitHub source parsing/fetching, Kestrel and limited Hermes manifest loading, plugin state records, CLI/API review/install/inspect/enable/disable/update/remove commands, approval-gated `plugin.review` and `plugin.install`, review-first web UX, enable blockers for unmanaged dependencies or required unavailable isolation, and materialization of plugin-declared skills/MCP server entries.
- The FastAPI control plane can require bearer/API-key auth via `NEST_AGENT_REQUIRE_API_AUTH=1` and a token environment variable.
- Generic/custom channel endpoints can require HMAC-SHA256 webhook signatures using a per-channel secret environment variable.
- Shell/test/repair validation commands normalize `python`/`python3` to the active interpreter so autonomous validation is stable across local environments.
- Tool-aware behavior-delta preflight now runs immediately before agent-loop tool execution when `NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=1`. It compiles bounded advisory instructions from relevant active deltas, passes them through the tool context/tool-loop message, and logs activations without changing capability or exact-call approval gates.
- Explicit `/search <query>` and `/memory search <query>` commands route deterministically to `memory.search`, reducing live-provider tool-choice variance for operator/eval search tasks.
- `scripts/run_live_learning_eval.py` provides an isolated live-provider learning/safety E2E harness covering provider smoke, durable memory reopen, correction frames, NestedLearningKernel gates, task-capsule extraction, approval blocking, and behavior-delta activation logging.
- `scripts/eval_learning_architecture.py` provides the integrated learning-architecture eval harness for setup, provider smoke, agent turn, `complete.mv2` capsule, behavior-delta proposal extraction, MutationGate decisions, replay comparison, behavior compilation, tool-aware preflight activation, outcome ledger recording, and rollback verification. Mock runs are deterministic; live OpenAI/OpenAI-compatible runs are skipped by default behind `RUN_LIVE_LEARNING_EVALS=1`, call/cost/time guards, and report redaction.
- Stage 1 autonomous-learning hardening is landed as observability/config scaffolding only: `nest-agent learning dashboard`, `GET /api/learning/dashboard`, and the Advanced web Learning Dashboard render existing behavior-delta activation/outcome aggregates, while new autonomous flags (`enable_auto_activate_low_risk_deltas`, `enable_auto_skill_materialization`, `enable_auto_consolidation_shadow`, `enable_auto_consolidation_apply`, `enable_diagnosis_to_patch`) remain default-off.
- Productization tracking now has a canonical roadmap (`docs/PRODUCTIZATION_ROADMAP.md`) plus a read-only product-readiness report exposed through `nest-agent product readiness` and `GET /api/product/readiness`; it classifies local stability, golden repair, learning, auth/workspaces, sandboxing, provider certification, UX, operations, channels, and metrics as ready/partial/missing without changing runtime behavior.
- First-run setup readiness checks now inspect non-secret local prerequisites through `nest-agent product setup` and `GET /api/product/setup`, covering provider configuration, workspace, memory/state/log paths, dangerous-action gates, worker isolation, and local API auth without mutating setup state or exposing secrets. The guided web setup wizard loads that report and surfaces pass/warn/fail counts plus the highest-priority checks before saving onboarding to Soul memory.
- Provider certification now has a redacted per-provider report through `nest-agent product provider-certification` and `GET /api/product/provider-certification`, covering deterministic mock certification, configured credential/base-url presence, blocked providers, manual host validation, and provider-specific live-validation commands without exposing raw secrets.
- Support bundle export now writes a redacted local diagnostic archive through `nest-agent product support-bundle` and `POST /api/product/support-bundle`, including product/setup readiness, runtime metadata, git status, state-table counts, log file metadata, and a bounded redacted event-log tail.
- `examples/golden_repair_demo` provides a deterministic broken-then-fixed repository fixture for the golden repair workflow, including a failing subtraction test and an `expected_fix.patch`; the main test suite verifies the fixture fails before the patch and passes after it.
- Ollama Cloud with `gpt-oss:120b` has been locally validated through live learning E2E and full golden evals on both memory and Memvid backends.

## Partially Implemented

- Streaming/provider parity: OpenAI Responses, OpenAI-compatible, Anthropic, and Gemini streaming surfaces are implemented where the SDK exposes them. OpenRouter/Ollama aliases, Ollama Cloud, Anthropic, and Gemini adapters have mocked contract tests, a flag-gated live provider integration harness exists, and Ollama Cloud + `gpt-oss:120b` has passed local live learning/golden evals. Broader credentialed CI/release coverage across all providers and richer per-provider context/JSON-mode details still need hardening.
- MCP: stdio live sessions are hardened and covered by a flag-gated integration test. SSE and streamable HTTP use the same manager path but still need real transport fixtures and production soak testing.
- Skills: filesystem discovery, manifest validation, provenance metadata, upload/install, and instruction/Python/shell-list runtimes exist. Review metadata can expose declared plugin dependencies and isolation requirements, but container-grade isolation and package dependency management remain incomplete.
- Plugins: registry, public GitHub fetch, manifest parsing, CLI/API review/install/update/enable commands, exact-call approval for agent-initiated review/install, review-first web UX, enable blockers for unmanaged dependencies or required unavailable isolation, and skill/MCP materialization exist. Managed dependency installation, real container isolation, richer compatibility with executable Hermes hooks, and broader network/security review still need hardening before shared use.
- Codex CLI: `codex-cli` can drive responses and `codex.exec` is available as a high-risk approval-gated tool. It is not yet a branch-isolated autonomous repair loop.
- Consolidation: capsule extraction and Nested Learning decisions exist, but auto-consolidation remains disabled by default and validation loops are still basic.
- Controlled self-modification: behavior-delta schema, SQLite ledger persistence, proposal-only task capsule extraction, repeated failed tool-call heuristic extraction, `nest-agent memory deltas propose --dry-run`, rule-based mutation-gate decisions, a default-off behavior compiler, runtime context integration behind `NEST_AGENT_ENABLE_BEHAVIOR_DELTAS`, tool-aware preflight for active relevant deltas before tool execution, deterministic compiler replay, full mock-agent replay, integrated learning-architecture evals, live learning E2E coverage, `nest-agent memory deltas ledger` JSON reporting with advisory recommendations, preview-only skill-candidate rendering, ORACLE shadow-only behavior-delta counterfactual reporting, FastAPI behavior-delta review routes, approval-gated activate/reject/rollback review endpoints, a deterministic review/action E2E test, and an Advanced web Behavior Deltas Review panel exist for visible operator review. Approval-gated skill install from candidates and broader live-provider replay matrices remain future phases.
- Self-diagnosis: first-pass classification, memory recall tools, and the default chat-loop retry gate exist. Hybrid LLM diagnosis, reviewer-confirmed diagnosis, and cross-run retry-state matching are still next steps.
- Self-modification: the runtime can inspect itself, record validated Soul/self memories, and capture approval-gated self-change requests. Actual code changes still have to flow through existing repair and commit gates; policy writes still require explicit policy gates.
- Safe repair: branch preparation, patch application, targeted validation, diagnosis-gated retry assessment, status reporting, and rollback primitives exist. Full autonomous patch proposal, reviewer gating, and approval-before-commit orchestration are still incomplete.
- Subagents: local subagent runs can be queued, tracked, and executed by scheduler runs until idle. The graph runtime can assign work to task/subagent records, and scheduler/subagent task execution can opt into git worktree isolation. Codex-backed worker fan-out, merge/review handling for worker branches, and fully dynamic DAG rewriting are still next steps.
- Channels: inbound normalization, dry-run reply payloads, and generic HMAC webhook verification are implemented. Production bot identity verification, Discord Gateway reads, and channel-specific rate-limit handling still need hardening.

## Not Done Yet

- Credentialed CI/release validation across the full provider matrix. Local release validation has passed for Ollama Cloud + `gpt-oss:120b`; OpenAI, OpenRouter, Anthropic, Gemini, local Ollama/OpenAI-compatible endpoints, and Codex CLI still need routine credentialed matrix runs.
- Fully dynamic plan rewriting with LLM-proposed DAG changes, reviewer gates across real repair branches, and Codex-backed worker fan-out/merge/review across isolated worker branches.
- Production authorization and user/session isolation for the UI/API beyond the local shared-token gate.
- Bot-platform-native signature/identity verification and secret rotation workflows for external channel endpoints.
- Robust MCP SSE/streamable HTTP transport fixtures and failure-recovery soak testing.
- Container-grade sandboxed skill execution.
- Autonomous self-improvement beyond self-change proposal capture, repair diff review, test gates, rollback, and explicit human approval.
- Hosted multi-user UI behavior, production authorization, and role-scoped operator permissions.

## Current Contract

- High-risk tools require both capability enablement (matching allow flag, where applicable) and explicit approval for the exact tool-call ID and arguments before execution.
- `NEST_AGENT_ALLOW_WEB` / `--allow-web` enables read-only web tools; web fetches reject private, local, link-local, multicast, reserved, and unspecified addresses.
- `NEST_AGENT_ALLOW_SELF_MODIFICATION` / `--allow-self-modification` only enables the high-risk `self.propose_change` request path. It does not bypass exact-call approval or the repair/commit gates.
- The agentic failure cycle is default-on through `enable_agentic_cycle`; disable it only for debugging with `NEST_AGENT_DISABLE_AGENTIC_CYCLE=1`.
- Autonomous scheduling is opt-in and bounded by `max_scheduler_tasks` / `NEST_AGENT_MAX_SCHEDULER_TASKS` per cycle and `max_scheduler_cycles` / `NEST_AGENT_MAX_SCHEDULER_CYCLES` per drain run.
- Automatic memory compaction is opt-in through `NEST_AGENT_ENABLE_AUTO_COMPACT=1` and dry-run unless `NEST_AGENT_AUTO_COMPACT_APPLY=1`.
- Git worktree isolation for scheduler/subagent execution is opt-in through `enable_worker_isolation` / `NEST_AGENT_ENABLE_WORKER_ISOLATION`; isolated worker branches use `worker_branch_prefix` and `worker_worktree_dir`.
- Cancelled runs must not transition to completed, blocked, or failed after cancellation; lifecycle updates should use the guarded state transition helper.
- Completed, failed, and cancelled runs are immutable even for repeated same-status transition attempts; approval requests are immutable after leaving `pending`.
- Tool execution is bounded by `tool_timeout_seconds` / `NEST_AGENT_TOOL_TIMEOUT_SECONDS` and timeout failures are returned as structured tool errors.
- Behavior-delta runtime/preflight compilation is default-off through `NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=0`. When enabled, it only compiles active relevant deltas, logs activations, and remains advisory; proposed/staged deltas are not auto-activated, policy-write gates are unchanged, and broad automatic blocking is not performed beyond existing deterministic retry blocking.
- New background runs persist a root task plus a small starter DAG with dependencies, required tools, risk, acceptance criteria, attempt count, failure reason, diagnosis, retry-strategy fields, and graph-runtime plan metadata.
- Ordinary conversation and observations must not write policy memory directly.
- The Memvid backend must use `.mv2` files and preserve one file per memory layer, including `.nest/memory/self.mv2`.
- `complete.mv2` is a run artifact under `.nest/runs/{run_id}/`, not a permanent memory layer.
- SQLite is control-plane state only. It is not the retrieval memory substrate.
- Support bundle exports are diagnostic artifacts only: they report environment-variable presence, redact common secret patterns, summarize state tables, and do not include raw broker vault contents or `.mv2` memory files.
- Mock-provider tests are the default fast validation path; Memvid integration remains behind `RUN_MEMVID_INTEGRATION=1`.
- Provider fallback only runs for `ProviderError(retryable=True)` failures; non-retryable errors fail fast and preserve the original provider error.
- MCP tools remain approval-by-default unless a server is explicitly configured to trust its manifest; dangerous tool names/descriptions such as file writes, deletes, shell execution, patching, committing, or secrets are promoted to high risk during vetting.
- Invalid skill manifests are rejected during discovery; accepted skill manifests record validation status plus manifest/SKILL.md content hashes for provenance.
- Memory promotion decisions must carry auditable gate metadata, validation evidence refs, provenance, confidence, and validation status; one-off procedural successes and ordinary events can explain why they were not promoted into procedural/policy memory.
- Plugin review metadata can block enablement; it is not a package installer or container runtime.

## Validation Commands

```bash
python -m compileall -q src tests scripts
python -m pytest -q
PYTHONPATH=src python -m nested_memvid_agent.cli product setup --backend memory --provider mock --json
PYTHONPATH=src python -m nested_memvid_agent.cli product provider-certification --backend memory --provider mock --json
PYTHONPATH=src python -m nested_memvid_agent.cli product support-bundle --backend memory --provider mock --output /tmp/kestrel-support.zip --json
python scripts/eval_learning_architecture.py --provider mock --backend memory --all --json
python scripts/run_golden_evals.py --backend memory --provider mock
PYTHONPATH=src python -m nested_memvid_agent.cli chat --backend memory --provider mock --message "hello"
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memory --output-root /tmp/kestrel-live-learning-memory
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
npm run test --prefix web
npm run build --prefix web
bash -n install.sh
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_DRY_RUN=1 KESTREL_START_SERVER=0 bash install.sh
```

Use `python -m pytest` instead of a global `pytest` binary for optional integration tests so the fixture subprocesses inherit the same environment and installed extras (`mcp`, `memvid-sdk`).
