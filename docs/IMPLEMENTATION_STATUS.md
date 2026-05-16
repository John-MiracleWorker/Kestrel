# Implementation Status

Last updated: 2026-05-16

This repository is a working local agent scaffold, not a finished Hermes/OpenClaw agent. The status below is intentionally literal so future Codex passes can harden the right layers without treating roadmap items as done.

## Working Now

- CLI chat loop with in-memory and Memvid `.mv2` memory backends.
- Layered memory files for working, episodic, semantic, procedural, and policy layers.
- Context compiler that retrieves nested memory and builds the model prompt.
- MV2 context-frame model and token-aware pseudo-context packer that retrieves summaries first, deduplicates content, flags conflict metadata, and expands raw evidence on demand.
- Deterministic mock provider for fast tests and reproducible golden evals.
- OpenAI Responses provider adapter using the portable JSON tool envelope.
- OpenAI Responses provider now exposes native streaming deltas when the SDK stream surface is available, while preserving the non-streaming fallback path.
- OpenAI-compatible chat completions provider for local/model-server endpoints.
- Codex CLI provider that can use local `codex exec` as the normal response engine.
- Provider capability metadata is exposed on built-in providers, and a retryable-error fallback wrapper can route from a primary provider to a configured secondary provider.
- Built-in tool registry with structured exception boundaries, timeout enforcement, and exact-call approval gates for shell, file writes, patch application, tests, and Codex CLI delegation.
- Self-diagnosis primitives can classify common provider/tool/test/import/permission/MCP/sandbox failures and recall similar procedural/episodic failure lessons before retry.
- Safe self-repair now has branch-isolated repair primitives: `repair.prepare`, `repair.status`, `repair.apply_patch`, `repair.validate`, `repair.orchestrate_validate`, and `repair.rollback`.
- The first diagnosis-gated repair orchestration slice can run validation on an active repair branch, classify failures, recall prior lessons, and block repeated validation retries until the strategy changes.
- Skills now have a first manifest validation gate plus persisted validation/provenance metadata for discovered instruction capsules.
- Local FastAPI control plane with background runs, SSE events, approvals, tools, MCP registry, skills registry, and memory search.
- Multi-channel ingress for Telegram Bot API updates, Discord message/interaction-shaped payloads, and generic/custom webhooks, with CLI and API routes.
- SQLite state store for runs, run steps, approvals, MCP servers, skills, plugins, task nodes, and subagent runs, now initialized through schema version `7`.
- Paper-guided nested learning kernel with context-flow metadata, optimizer traces, conservative continuum-memory routing, and a `memory.learn` tool/API path.
- Memory learning decisions now expose explicit promotion gate metadata so rejected/accepted decisions can explain target layer, observed evidence, repeat-count thresholds, validation thresholds, and explicit-instruction requirements.
- Run-scoped `complete.mv2` task capsules, preview-only capsule summaries, dry-run consolidation decisions, and approval-gated capsule apply.
- MCP server records now track health metadata, tool counts, capabilities, last sync/seen/call/error timestamps, session state, failure counts, and latency.
- MCP server records now persist vetting metadata: transport/network exposure, secret-env requirements, per-tool risk/approval classification, risk reasons, and recommended trust posture.
- MCP stdio servers now have a managed lazy session lifecycle with connect/disconnect/restart/health API routes, bounded operation timeouts, config-change teardown, and approval-by-default tool risk normalization.
- First task-graph and subagent run records exist, with durable task metadata, deterministic starter plan decomposition, in-process planner/worker/reviewer profiles, and UI/API surfaces.
- Task nodes can now persist latest failure diagnosis and retry strategy metadata; failed subagents classify the failure, record a retry gate that requires changed strategy, and emit a diagnosis event tied back to the task.
- The task graph now exposes deterministic `ready_tasks` for scheduler/resume work: only approved queued/approved tasks with completed dependencies are eligible, and failed retry tasks remain blocked until their retry strategy explicitly allows a changed strategy.
- An opt-in autonomous scheduler can execute approved ready task nodes through the normal agent loop, drain bounded dependency cycles until idle, publish task/subagent events, and preserve approval blocking for high-risk tool calls.
- Provider failures emit structured `diagnosis.classified` events so traces can explain the failure category and suggested playbook.
- Repair mutation tools are high-risk, approval-gated, covered by exact-call approvals, disabled unless the matching capability is enabled, and refuse non-repair branches for patch/validate/rollback operations.
- Diagnosis-gated repair validation must remain approval-gated, refuse non-repair branches, recall similar lessons on failure, and block repeated validation retries when prior lessons exist unless a changed strategy is supplied.
- Repair commits on repair branches now require a durable `repair.review` artifact tied to a successful validation result and the current diff hash; `git.commit` refuses repair-branch commits when the review is missing, stale, or for a different branch.
- Background repair/commit goals now seed a repair-specific task DAG that orders inspect → isolation → patch → validation → `repair.review` → `git.commit`, so the durable plan itself encodes the reviewer gate before commit.
- A repair E2E smoke test now proves a seeded failure can flow through isolated repair branch preparation, approved patching, targeted validation, `repair.review` gate creation, and blocked unapproved commit; successful validation points to `create_repair_review_before_commit` instead of implying direct commit.
- Exact-call approved repair commits now stage only the reviewed repair files, complete after the current `repair.review` gate, and return the resulting `commit_sha` for traceability.
- Terminal run records and approval decisions are replay-safe: late duplicate terminal transitions cannot overwrite original run results, and already-decided approval records cannot be flipped by replayed decisions.
- Approval resume flow now records the executed tool result back onto the already-approved approval record without reopening or flipping the decision, and a full-flow smoke test covers run creation, approval blocking, exact-call approval, resume, tool result persistence, traces, task graph, and capsule creation together.
- Skills can now run instruction, Python, and shell-list runtimes from their skill directory with path checks, JSON stdin, timeout bounds, and provenance-backed episodic records; container runtime remains intentionally unavailable.
- `skill.install` provides an approval-gated local upload/install path for new skill capsules under the configured skills directory, with manifest validation and content hashes.
- A first plugin registry slice exists with GitHub source parsing, plugin manifest loading, plugin state records, CLI list/install/inspect/enable/disable/update/remove commands, and materialization of plugin-declared skills/MCP server entries.
- The FastAPI control plane can require bearer/API-key auth via `NEST_AGENT_REQUIRE_API_AUTH=1` and a token environment variable.
- Generic/custom channel endpoints can require HMAC-SHA256 webhook signatures using a per-channel secret environment variable.
- Shell/test/repair validation commands normalize `python`/`python3` to the active interpreter so autonomous validation is stable across local environments.

## Partially Implemented

- Streaming/provider parity: OpenAI Responses streaming deltas are implemented. OpenAI-compatible/local provider streaming and richer per-provider context/JSON-mode details still need hardening.
- MCP: stdio live sessions are hardened and covered by a flag-gated integration test. SSE and streamable HTTP use the same manager path but still need real transport fixtures and production soak testing.
- Skills: filesystem discovery, manifest validation, provenance metadata, upload/install, and instruction/Python/shell-list runtimes exist. Container-grade isolation and package dependency management remain incomplete.
- Plugins: registry, GitHub fetch, manifest parsing, CLI commands, and skill/MCP materialization exist. Install-path allow-flag enforcement, approval UX, dependency isolation, and network/security review still need hardening before shared use.
- Codex CLI: `codex-cli` can drive responses and `codex.exec` is available as a high-risk approval-gated tool. It is not yet a branch-isolated autonomous repair loop.
- Consolidation: capsule extraction and Nested Learning decisions exist, but auto-consolidation remains disabled by default and validation loops are still basic.
- Self-diagnosis: first-pass classification and memory recall tools exist. A full executor retry gate that forces changed strategy before every retry is still incomplete.
- Self-modification: the runtime can record validated self-improvement signals and policy candidates, but code changes and policy writes still require explicit gates.
- Safe repair: branch preparation, patch application, targeted validation, diagnosis-gated retry assessment, status reporting, and rollback primitives exist. Full autonomous patch proposal, reviewer gating, and approval-before-commit orchestration are still incomplete.
- Subagents: local subagent runs can be queued, tracked, and executed by scheduler runs until idle. True branch/worktree isolation and Codex-backed worker fan-out are still next steps.
- Channels: inbound normalization, dry-run reply payloads, and generic HMAC webhook verification are implemented. Production bot identity verification, Discord Gateway reads, and channel-specific rate-limit handling still need hardening.

## Not Done Yet

- Broader provider integration tests for OpenRouter/Anthropic/Ollama-style adapters and native streaming for non-OpenAI providers.
- Planner/executor/reviewer loop that can revise plans dynamically, enforce reviewer gates across repair branches, and coordinate isolated workers instead of only draining the deterministic starter DAG.
- Production authorization and user/session isolation for the UI/API beyond the local shared-token gate.
- Bot-platform-native signature/identity verification and secret rotation workflows for external channel endpoints.
- Robust MCP SSE/streamable HTTP transport fixtures and failure-recovery soak testing.
- Container-grade sandboxed skill execution.
- Autonomous self-improvement with diff review, test gates, rollback, and explicit human approval.
- Comprehensive frontend design for model/provider settings, live tool traces, MCP configuration editing, and skill execution details.

## Current Contract

- High-risk tools require both capability enablement (matching allow flag, where applicable) and explicit approval for the exact tool-call ID and arguments before execution.
- Autonomous scheduling is opt-in and bounded by `max_scheduler_tasks` / `NEST_AGENT_MAX_SCHEDULER_TASKS` per cycle and `max_scheduler_cycles` / `NEST_AGENT_MAX_SCHEDULER_CYCLES` per drain run.
- Cancelled runs must not transition to completed, blocked, or failed after cancellation; lifecycle updates should use the guarded state transition helper.
- Completed, failed, and cancelled runs are immutable even for repeated same-status transition attempts; approval requests are immutable after leaving `pending`.
- Tool execution is bounded by `tool_timeout_seconds` / `NEST_AGENT_TOOL_TIMEOUT_SECONDS` and timeout failures are returned as structured tool errors.
- New background runs persist a root task plus a small starter DAG with dependencies, required tools, risk, acceptance criteria, attempt count, failure reason, diagnosis, and retry-strategy fields.
- Ordinary conversation and observations must not write policy memory directly.
- The Memvid backend must use `.mv2` files and preserve one file per memory layer.
- `complete.mv2` is a run artifact under `.nest/runs/{run_id}/`, not a permanent memory layer.
- SQLite is control-plane state only. It is not the retrieval memory substrate.
- Mock-provider tests are the default fast validation path; Memvid integration remains behind `RUN_MEMVID_INTEGRATION=1`.
- Provider fallback only runs for `ProviderError(retryable=True)` failures; non-retryable errors fail fast and preserve the original provider error.
- MCP tools remain approval-by-default unless a server is explicitly configured to trust its manifest; dangerous tool names/descriptions such as file writes, deletes, shell execution, patching, committing, or secrets are promoted to high risk during vetting.
- Invalid skill manifests are rejected during discovery; accepted skill manifests record validation status plus manifest/SKILL.md content hashes for provenance.
- Memory promotion decisions must carry auditable gate metadata; one-off procedural successes and ordinary events can explain why they were not promoted into procedural/policy memory.

## Validation Commands

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
PYTHONPATH=src python -m nested_memvid_agent.cli chat --backend memory --provider mock --message "hello"
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
npm run test --prefix web
npm run build --prefix web
```

Use `python -m pytest` instead of a global `pytest` binary for optional integration tests so the fixture subprocesses inherit the same environment and installed extras (`mcp`, `memvid-sdk`).
