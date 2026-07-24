# Runtime Wiring

Last updated: 2026-07-16

## Chat Turn Flow

```text
1. CLI/API/channel receives a user message.
2. RunManager creates or resumes a run when using persistent run surfaces.
3. Persistent background runs enter the durable graph runtime: planner, executor, reviewer, recovery, memory-promotion, and finalizer nodes.
4. The executor node keeps using the existing chat loop.
5. Agent writes the user observation to working memory.
6. ContextCompiler delegates to the MV2 context packer; if enabled, BehaviorCompiler adds relevant active behavior-delta instructions.
7. The packer retrieves memory frames, prefers summaries, deduplicates, flags conflicts, and emits a bounded pseudo-context prompt.
8. The default-on agentic failure cycle retrieves prior procedural/episodic failure lessons and injects a `Prior Failure Lessons` section when relevant. Tool-aware behavior-delta preflight can add bounded advisory instructions before tool execution when enabled.
9. Agent builds messages:
   - system prompt
   - compiled nested memory context
   - prior failure lessons when found
   - provider-specific tool protocol; native schemas stay out-of-band
   - user message
10. Explicit direct commands such as `/search <query>` can be routed to deterministic tools before the LLM. Otherwise, the LLM provider returns either:
   - final text, or
   - native provider tool calls, or
   - the portable JSON envelope with tool calls.
11. Provider output is validated against the active `ToolSpec` registry before tool execution.
12. Before same-action retries, the retry gate requires a meaningful changed strategy.
13. ToolRegistry validates schemas, resolves the live configured/effective capability decision, enforces timeout limits, and checks approval requirements.
14. Approved/allowed tools execute and return structured results.
15. Agent writes tool outputs or failures to working memory.
16. Failed tool attempts are classified, linked to recalled lessons, and written as episodic `FailureEpisode` records.
17. A successful validation after a diagnosed failure can write a procedural `LessonCard` with evidence.
18. Agent loops until final answer, approval block, tool-round limit, provider failure, or timeout.
19. Agent writes turn summary to episodic memory and includes a proof-of-work summary in the turn result.
20. Reviewer/recovery nodes decide whether the run can finalize, must pause for approval, or should fail with diagnosis.
21. Task capsule writer may create `.nest/runs/{run_id}/complete.mv2`.
22. Changed memory layers are sealed.
23. Run state, timeline events, and trace spans are persisted.
```

The mock provider and in-memory backend keep this flow deterministic for tests.

## Persistent Run Surfaces

`nest-agent chat` is the conversational surface. `nest-agent run`, `status`, `approvals`, `approve`, and `deny` expose persistent background runs:

```bash
nest-agent run --backend memory --provider mock --json --events "hello run"
nest-agent status <run_id> --backend memory --json --events
nest-agent approvals --backend memory --json
nest-agent approve <approval_id> --backend memory --json
```

The FastAPI server exposes the same state through run, event, approval, scheduler, proactive-routine, memory, context, skill, MCP, capability catalog/mutation/history, behavior-delta, plugin, product readiness/setup/provider-certification/support-bundle, and channel routes.

Soul/self routes expose the same non-secret runtime model as the CLI: `/api/self`, `/api/self/remember`, `/api/self/propose-change`, `/api/web/search`, and `/api/web/fetch`.

## State Store

`AgentStateStore` is SQLite control-plane storage, currently schema version 19. Run rows retain serialized channel source provenance plus turn origin and transcript scope across queueing, recovery, and approval waits; legacy rows default to primary scope. Schema v19 adds durable hashed manual-routine idempotency claims and trigger provenance. Schema v18 gives approved side effects renewable execution claims and preserves the exact scheduler task/subagent binding until the pair reaches its continuation boundary. Schema v17 added revisioned routine definitions and occurrence rows with deterministic identities, UTC schedule instants, claim owner/generation/expiry fencing, request snapshots, run linkage, and terminal history.

It stores:

- runs and run steps
- approval requests, decisions, exclusive execution claims, continuation bindings, and executed tool results
- MCP server records and discovered tools
- skill records and validation/provenance metadata
- plugin records and enablement metadata
- revisioned tool/MCP-server/skill capability overrides and append-only change rows
- promotion ledger and promotion outcome records
- behavior-delta ledger, activation records, and outcome records
- task nodes
- subagent runs
- trace spans

Run records also persist the provider/model selected for the run so the local operator UI can launch and inspect runs without falling back to process-global provider assumptions.

Terminal run records are replay-safe: completed, failed, and cancelled runs are immutable. Approval records are immutable after they leave `pending`. Immediately before an approved side effect, the live manager must atomically acquire a durable claim bound to its run lease and, for scheduler work, the exact running task/subagent pair. A heartbeat renews that claim while the tool runs, and only the matching owner and claim ID may finalize the result. The task/subagent binding remains after result persistence until the pair is atomically blocked or terminalized. Startup preserves a demonstrably live claim; an expired/dead claimant is closed as `approval_execution_outcome_unknown`, and stale result-bound continuations are failed closed rather than replayed.

SQLite is not the retrieval memory substrate. Durable retrieval memory remains one Memvid `.mv2` file per nested layer.

## Tool-Call Format

Kestrel still supports the portable JSON envelope:

```json
{
  "message": "brief progress note",
  "tool_calls": [
    {"name": "memory.search", "arguments": {"query": "project auth failure", "k": 5}}
  ]
}
```

Retries of a failed same-action tool call must include a strategy object:

```json
{
  "message": "Retry with a narrower validation target.",
  "tool_calls": [
    {
      "name": "test.run",
      "arguments": {"command": ["pytest", "tests/test_agent_runtime.py::test_case", "-q"]},
      "strategy": {
        "changed_strategy": "Run the focused failing test instead of repeating the full suite.",
        "why_different": "The command target is narrower and tests the suspected path.",
        "expected_signal": "Focused pass/fail output.",
        "fallback_if_fails": "Inspect the assertion and fixture setup before another retry."
      }
    }
  ]
}
```

The envelope and native provider tool calls both pass through strict schema validation against the currently advertised, capability-filtered `ToolSpec` set before execution. Native-capable providers receive schemas only through their function-calling interface; Kestrel does not duplicate those full schemas in system text. Provider adapters can advertise a bounded native-tool limit. Kestrel then ranks only the already-active specs for the objective, keeps exact canonical names, retains `tool.registry` for authoritative discovery, and never uses narrowing to grant enablement or bypass execution-time capability and approval checks. Calls outside the advertised subset and malformed calls fail before execution with non-retryable control taxonomy such as `unknown_tool_call`, `missing_tool_arguments`, or `invalid_tool_argument_type`.

## Trace Spans

The run timeline remains append-only event history. Trace spans add a durable flight-recorder model over that history. Current span types include `run`, `plan`, `llm.request`, `tool.call`, `memory.write`, `approval.wait`, `review`, and `eval`. `/api/runs/{run_id}/trace` returns both the timeline and persisted spans with counts by span type.

## Permission and Approval Model

Low-risk tools can run after argument validation only when their effective capability decision is enabled.

High-risk tools require:

1. An enabled owner switch for the tool and any parent skill/MCP server.
2. Every matching master allow flag and launch-allowlist prerequisite.
3. Exact-call approval; this prerequisite cannot be disabled by the per-capability switch.
4. Matching owner, tool-call ID, arguments, capability revision, and policy/tool-spec/parent digest at execution time.

`GET /api/capabilities` reports owner-desired (`configured_enabled`) and effective state plus blockers. Revision-checked `PUT /api/capabilities/{kind}/{capability_id}` persists the switch and returns HTTP 409 for a stale write; `GET /api/capabilities/history` reads its append-only audit trail. New skills and new dynamic MCP/skill tools start off. New API MCP records are forced off and can only be enabled afterward through that revisioned capability endpoint.

Turning a capability off denies later invocations even through a stale registry, revokes affected pending approvals, and closes a disabled MCP server's managed session. It applies at the future-invocation boundary and does not guarantee cancellation of an arbitrary built-in subprocess already past dispatch. Turning on does not rewrite an active run's snapshotted config or bypass a parent, master flag, resource-integrity check, or exact-call approval.

Non-secret runtime settings use a separate content revision. `GET /api/runtime/config` returns the current persisted-settings snapshot; `PUT /api/runtime/settings` requires that snapshot's `expected_revision` and returns HTTP 409 on a stale writer. The store serializes read/merge, candidate-path validation, owner-only atomic persistence, live activation, and revocation of approvals affected by a newly disabled master flag. Activation failure restores both the prior persisted file and live configuration. Telegram's confirmed `max_tool_rounds` admin action uses the same transaction with a bounded conflict retry. API authentication itself is launch-controlled and excluded from persisted overrides.

Examples of high-risk tools:

- `shell.run`
- `file.write`
- `patch.apply`
- `test.run`
- `lint.run`
- `codex.exec`
- `repair.prepare`
- `repair.apply_patch`
- `repair.validate`
- `repair.orchestrate_validate`
- `repair.rollback`
- `git.commit`
- `memory.import`
- `skill.install`
- `capsule.apply`
- `self.propose_change`

`git.commit` never pushes. It refuses protected branches from `NEST_AGENT_PROTECTED_BRANCHES` (`main,master,release/*` by default). On repair branches, it requires a process-signed current `repair.review` artifact tied to successful pre/post-stable validation and the current content manifest. Commit construction hashes literal reviewed bytes into a private temporary index, bypasses repository filters/hooks/signing, and compare-and-swaps the branch. On other branches, exact-call approval must include the current staged-tree SHA exposed by `git.status`; a later index change is rejected.

Remote publishing is not part of the default tool lane. `NEST_AGENT_ALLOW_GIT_PUSH=false`, `NEST_AGENT_ALLOW_REMOTE_MUTATION=false`, and `NEST_AGENT_GIT_WRITE_MODE=local_branch` keep self-improvement local by default. The shell tool blocks common remote-mutation escape routes such as `git push`, `git tag`, `git remote set-url`, `gh repo edit`, `gh secret set`, `gh workflow enable`, `rm -rf .git`, and writes to `.git/config` before subprocess execution.

The local-only git lane includes `git.create_local_branch` for approval-gated branch creation and `git.export_patch` for approval-gated patch artifacts under `.kestrel/improvements/`. Neither tool pushes or tracks remotes.

`self.propose_change` is disabled unless `allow_self_modification` is enabled, still requires exact-call approval, and only records the requested self-change. Any actual code edit must use `repair.prepare`, `repair.apply_patch`, `repair.validate`, `repair.review`, and `git.commit`.

`test.run`, `lint.run`, `repair.validate`, `repair.orchestrate_validate`, and read-only `codex.exec` never execute candidate code on the host. `NEST_AGENT_VALIDATION_CONTAINER_IMAGE` must name a preloaded digest-pinned OCI image containing the requested command and the project's validation dependencies. The runner copies the exact tracked-plus-untracked, non-ignored Git candidate into a bounded private snapshot, excludes `.git`, `.nest`, secrets, receipts, and the live workspace, and runs it with network disabled, a read-only source/root filesystem, nonroot identity, dropped capabilities, resource limits, and no host fallback. Kestrel uses `--pull=never`; operators must preload the exact `name@sha256:<64 hex>` reference. `nest-agent doctor` reports whether a configured reference has the required shape and whether enabled master gates require it, but defers local-image and dependency checks until execution. Validation receipts bind the isolation evidence. `codex.exec` also requires a Codex binary in the image; because this boundary exposes no credentials or network, remote-model delegation is unavailable. The separate `codex-cli` response provider remains a host-process surface and fails closed when same-account-readable secret or repair trust domains exist.

`web.search` and `web.fetch` are disabled unless `allow_web` is enabled. They are read-only context tools; `web.fetch` rejects private, local, link-local, multicast, reserved, and unspecified addresses and applies timeout/byte limits.

The plugin registry can fetch public GitHub repositories and materialize plugin-declared skills/MCP servers. CLI/API plugin review, install, update, enable, and sync/materialization routes require `NEST_AGENT_ALLOW_PLUGIN_INSTALL=true` or `--allow-plugin-install`. Review returns provenance, dependency, isolation, warning, unsupported-feature, and enable-blocker metadata without installing or executing plugin code. Agent-initiated `plugin.review` and `plugin.install` use the same enablement gate plus exact-call approval, and installed plugins remain disabled unless explicitly enabled. Plugins with unmanaged declared dependencies or required unavailable isolation can be installed disabled but cannot be enabled.

Instruction skills do not execute code. Executable skill manifests fail closed for host `python` and `shell` runtimes; the only executable path is an explicitly enabled, exact-approved, digest-pinned OCI image. Immediately before launch, Kestrel verifies a bounded symlink-free skill-tree snapshot and canonical default-deny workspace scopes. The Docker runner supplies `--pull=never`, `--network=none`, a read-only root filesystem, nonroot identity, dropped capabilities, no-new-privileges, PID/CPU/memory/ulimit/tmpfs bounds, bounded output, hard timeout/process-group cleanup, and no host fallback. The current scope contract grants no secrets and no outbound network.

## Memory Wiring

Permanent memory layers:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
.nest/memory/self.mv2
.nest/memory/policy.mv2
```

Run capsules:

```text
.nest/runs/{run_id}/complete.mv2
```

Memvid writes must be sealed. The backend opens existing `.mv2` files with `use(...)` and only creates missing files. Never call `create(path)` on an existing `.mv2` file.

## Context Packing

The context compiler now uses the MV2 context packer. The packer emits:

- objective
- policy constraints
- Soul/self model
- relevant procedures
- stable facts
- recent episodic/task state
- working memory
- conflict warnings
- evidence refs
- retrieval telemetry
- next-step instruction

Raw expansion happens through `context.expand` when needed rather than dumping full transcripts into every prompt.

Persistent recall is split by trust. General packed memory, failure lessons, display labels, and free-form Soul preferences are JSON-encoded user-role evidence. Only the fixed persona preset selected through the authenticated onboarding route enters the Soul system context. Policy memory enters a system message only after its `memory.policy_promote` attestation matches the durable exact-call owner approval, approved argument digest, and recorded policy record ID; caller-asserted `memory.learn` fields never satisfy this check.

Exact transcript replay is a separate continuity path. Working-memory user and assistant frames carry durable `turn_origin` and `transcript_scope` metadata. Only completed `primary_user`/`primary` and `channel_user`/`channel` pairs replay with native roles; scheduler tasks, subagents, and approval continuations are marked `internal`. Approved tool continuation content is JSON-wrapped as untrusted runtime data before the model sees it.

## Behavior Delta Wiring

Behavior-delta runtime wiring is default-off via `NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=0`. When enabled, active relevant deltas are compiled into structured runtime instructions, activation rows are logged once per run/delta, and tool-aware preflight passes advisory instructions through the tool context without bypassing capability flags or exact-call approval gates. Proposed/staged low-risk deltas can auto-activate only when `NEST_AGENT_ENABLE_AUTO_ACTIVATE_LOW_RISK_DELTAS=1` and `MutationGate` evidence requirements pass; policy-affecting deltas still require MutationGate approval, exact-call review actions, evidence, validation, and rollback metadata.

Stage 1 autonomous-learning hardening adds observability plus the default-off low-risk auto-activation path. `/api/learning/dashboard` and `nest-agent learning dashboard` aggregate behavior-delta activation/outcome rows into headline counts (auto-activations, rollbacks, false-positive rate, activations-then-rolled-back, average time-to-rollback) plus per-layer breakdowns.

Autonomous-learning rollout flags currently default off:

- `enable_auto_activate_low_risk_deltas` / `NEST_AGENT_ENABLE_AUTO_ACTIVATE_LOW_RISK_DELTAS=0`: enables LOW-risk behavior-delta auto-activation after explicit validation metadata, replay, repeat evidence, and rollback checks pass through `MutationGate`. Rollback command when enabled: set `NEST_AGENT_ENABLE_AUTO_ACTIVATE_LOW_RISK_DELTAS=0`, restart, then roll back individual active deltas through the existing delta rollback endpoint/CLI.
- `enable_auto_skill_materialization` / `NEST_AGENT_ENABLE_AUTO_SKILL_MATERIALIZATION=0`: future gate for instruction-runtime skill materialization from repeatedly successful procedures. Rollback command when enabled: set `NEST_AGENT_ENABLE_AUTO_SKILL_MATERIALIZATION=0`, restart, then disable/remove individual skills through the skills panel.
- `enable_auto_consolidation_shadow` / `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_SHADOW=0`: future gate for end-of-run consolidation shadow decisions that do not write memory. Rollback command when enabled: set `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_SHADOW=0` and restart.
- `enable_auto_consolidation_apply` / `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_APPLY=0`: future gate for applying validated consolidation decisions. It does not unlock policy writes. Rollback command when enabled: set `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_APPLY=0`, restart, then use existing promotion-ledger reversal tooling where available.
- `enable_diagnosis_to_patch` / `NEST_AGENT_ENABLE_DIAGNOSIS_TO_PATCH=0`: future gate for diagnosis-to-patch DAG preparation. It does not auto-commit. Rollback command when enabled: set `NEST_AGENT_ENABLE_DIAGNOSIS_TO_PATCH=0`, restart, then clean up in-flight repair branches through existing `repair.rollback`.

## Scheduler and Subagents

Background runs seed a root task and a small deterministic task DAG. When `enable_semantic_orchestration` / `NEST_AGENT_ENABLE_SEMANTIC_ORCHESTRATION=1` is explicitly enabled with a non-mock provider that advertises JSON support, the planner makes a bounded structured call that can refine the semantic objective, acceptance criteria, and advisory guidance for existing task IDs. The provider cannot add tasks or alter persisted dependencies, required tools, risk, approval state, or capability gates. Invalid or unavailable structured planning falls back to an explicitly labeled `deterministic_task_graph` plan.

The executor remains one chat/tool turn before any optional scheduler work; Kestrel does not claim that the provider dynamically rewrites the task DAG. The reviewer now records a schema-versioned artifact containing a decision, evaluator, per-criterion status, evidence references, provenance, confidence, and remaining risks. JSON-capable real providers may perform the semantic judgment, but only references to supplied runtime evidence are accepted. Mock mode and invalid/unavailable provider reviews use an explicitly labeled deterministic evidence gate. Approval continuation passes through the same reviewer gate before the run can complete, and unresolved final tool failures fail review. Recovery metadata is recorded when approvals or failures block progress.

The scheduler can execute approved ready tasks when `enable_autonomous_scheduler` or `NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER` is enabled. Task-level acceptance validation no longer marks every criterion satisfied from a single generic pass bit: successful tool calls and proof-of-work validation entries carry evidence IDs, while deterministic mock tool bypasses are persisted as `not_verified_mock` rather than fabricated success.

The root tracking task is reconciled with the terminal run state. For a terminal non-scheduler primary turn, unexecuted child skeleton tasks are marked `skipped` with a durable disposition instead of being left `queued` under a completed root. Scheduler child aggregation merges `child_statuses` into the existing root result so the primary `orchestration_review` artifact is not overwritten.

Semantic orchestration is separate from `enable_agentic_cycle`: the older flag controls failure lessons/proof-of-work behavior inside the chat loop, while `enable_semantic_orchestration` controls extra provider calls around that loop. It defaults off because a normally completed run adds up to two model requests (one plan and one review). Deterministic evidence review remains active without either extra request.

Scheduler bounds:

- `max_scheduler_tasks` / `NEST_AGENT_MAX_SCHEDULER_TASKS`
- `max_scheduler_cycles` / `NEST_AGENT_MAX_SCHEDULER_CYCLES`

Ready tasks must be queued or approved, have completed dependencies, and pass retry-strategy gates. Tasks requiring approval remain blocked until explicitly approved.

## Proactive Routine Wiring

Time-based proactive routines are separate from the per-run task scheduler. `RoutineService` claims due definitions from SQLite, while `RoutineLoop` provides an optional non-overlapping polling thread controlled by `enable_proactive_routines` / `NEST_AGENT_ENABLE_PROACTIVE_ROUTINES`. The loop stops and joins before `RunManager` cancels/joins owned work and before channel or MCP resources close.

Routine creation is forced disabled. Owner updates, enable/disable, and tombstone deletion require the current revision; API mutation additionally fails closed unless the global shared-token API-auth gate is configured. The first schedule contract is deliberately small: timezone-aware instants are normalized to UTC, `once` and `interval` are supported, fixed intervals are at least 60 seconds, late intervals skip backlog beyond their grace window, and at most `max_routines_per_tick` occurrences are claimed per tick.

Each occurrence has a deterministic identity over routine ID, routine revision, and scheduled UTC instant. Claim owner, generation, and expiry are revalidated in the same `BEGIN IMMEDIATE` transaction that inserts the deterministic run and moves the occurrence to `running`. Disabling, revising, or deleting before that transaction fences the stale claimant. Expired claims can move to a higher generation; a former owner cannot transition the reclaimed occurrence. A nonterminal occurrence, including a run blocked for exact-call approval, suppresses a new overlapping interval. Routine ticks also expire overdue exact-call approvals and reconcile the linked occurrence, so a headless approval cannot suppress the schedule forever.

Scheduled runs persist `turn_origin=scheduled_routine`, `transcript_scope=internal`, a routine-scoped session ID, and authoritative routine/occurrence provenance in their configuration snapshot. Their initial root/task graph is created in one SQLite transaction. Startup recreates that graph when a process dies after atomic run admission but before graph initialization, then resumes the queued run. The one-shot CLI uses a narrower recovery path that accepts only linked, internally scoped scheduled runs and leaves unrelated queued work untouched. Scheduled runs otherwise use ordinary run capacity, leases, graph execution, capability decisions, exact-call approvals, cancellation, and publication fences. Enabling the routine is not approval for any tool call.

The occurrence/run admission boundary is duplicate-fenced. The owner workbench uses revision-checked definition mutations and selected occurrence history. Its manual `POST /api/routines/{routine_id}/actions/run-now` action requires API auth, proactive-routine enablement, the current routine revision, and a client idempotency UUID. The server stores only a hash of that key, transactionally claims/replays/reclaims it, preserves the schedule, and applies overlap suppression. Kestrel does not claim exactly-once arbitrary tool or external connector side effects after dispatch. Cron, named timezone/DST calendar behavior, and automatic channel delivery are not implemented.

Routine bounds:

- `routine_poll_interval_seconds` / `NEST_AGENT_ROUTINE_POLL_INTERVAL_SECONDS`: 1-3,600 seconds.
- `routine_claim_ttl_seconds` / `NEST_AGENT_ROUTINE_CLAIM_TTL_SECONDS`: 1-3,600 seconds.
- `max_routines_per_tick` / `NEST_AGENT_MAX_ROUTINES_PER_TICK`: 1-100 claims.
- Owner-authored `interval_seconds`: 60-31,536,000 seconds (one year).
- Owner-authored `misfire_grace_seconds`: 0-604,800 seconds (seven days).
- Low-level occurrence-history and reconciliation reads are capped at 500 and 1,000 rows respectively.

Subagents are currently in-process planner/worker/reviewer profiles with durable records. When `enable_worker_isolation` or `NEST_AGENT_ENABLE_WORKER_ISOLATION` is enabled, scheduler/subagent execution prepares a git worktree from the run workspace, creates a worker branch using `worker_branch_prefix`, switches the task agent workspace to that worktree, and records the isolation metadata on the task result.

Worker isolation paths are controlled by `worker_worktree_dir` / `NEST_AGENT_WORKER_WORKTREE_DIR`; relative paths are resolved against the run workspace. Codex-backed fan-out plus automated merge/review handling across worker branches remain future hardening.

## Plugin Wiring

Plugins are stored under `.nest/plugins` by default and persisted in the `plugin_registry` table. The CLI exposes list/install/inspect/enable/disable/update/remove commands. Enabled plugins can materialize namespaced skills and MCP server records into the same registry surfaces used by native Kestrel skills and MCP servers.

Plugin installation is a high-risk surface because it fetches repository content. Manifest ID drift is rejected on update, plugin-provided MCP trust flags cannot downgrade the default approval policy, and read-only API routes do not materialize plugin state.

## MCP Wiring

MCP stdio servers use a managed lazy lifecycle:

- add server record
- connect on demand
- discover tools
- normalize tool risk and approval requirements
- reuse the session for invocations
- disconnect/restart on request
- tear down on config changes, delete, or shutdown
- serialize stdio process launch against secret/repair-key publication and quiesce every registered local stdio session before either trust domain is created

Newly discovered MCP tools default to approval-by-default. Manifest `trusted` and `allow_autonomous` fields are ignored unless the operator explicitly configures `risk_policy=trust_manifest`; dangerous tool names or descriptions are promoted to high risk during vetting.

Manual API MCP invocations route through the same tool registry as agent tool calls, so MCP approval and risk policies produce the same `approval_required`, `tool_disabled`, and exact-call approval behavior. MCP GET routes return stored state only; live health checks, discovery, and sync are POST actions.

MCP `secret_env` values are redacted in API responses, included in configuration fingerprints, and resolved from `os.environ` or `secret://...` broker refs only when launching a process. Raw secret-looking keys in MCP `env` are rejected.

## Secret Broker Wiring

`NEST_AGENT_SECRET_STORE_PATH` points to a local Secret Broker vault. `POST /api/secrets` accepts the raw value through the trusted backend flow and returns only metadata. `GET /api/secrets`, channel `env_status`, MCP `secret_env_status`, runtime config, and self-inspection never return raw values. Channels resolve configured env names through the broker at delivery/signature-verification time; MCP stdio servers resolve `secret://...` references into child process environment variables at launch.

Telegram admin mode remains a channel-local bridge, not production auth. The Telegram channel can be configured with `settings.admin_enabled=true`, `settings.owner_user_ids`, and `settings.signature_provider=telegram`; owner natural-language admin writes are staged behind inline confirmation callbacks, while raw secret values are refused and must be entered through local Secret Broker surfaces.

`NEST_AGENT_SECRET_BACKEND=json` is the default local-file backend. Its vault mutations are cross-process locked and atomically replace the live file from a mode-`0600` temporary file. `NEST_AGENT_SECRET_BACKEND=keyring` or `--secret-backend keyring` stores raw values through the optional OS keyring provider and keeps only metadata in the JSON vault. The default release includes the dedicated `keyring` extra, but the host must still expose a usable credential service. Explicit keyring selection fails closed when the package or backend is unavailable; it never silently falls back to plaintext JSON. Backend changes require a new empty metadata path and deliberate secret rotation/re-entry; Kestrel refuses to reinterpret a populated JSON vault as keyring metadata or vice versa.

OS keyring storage protects secrets at rest, not from another process running as the same account. Local MCP stdio lifecycle operations therefore fail closed when the raw file vault exists, keyring metadata contains records, or repair signing/receipt material exists. Before `POST /api/secrets` stores the first value, or repair integrity creates/rotates its v2 signing key, the owning runtime closes all registered local stdio sessions under the same transition lock used by launch; failure to verify closure aborts publication with `mcp_stdio_quiesce_failed`. This closes the managed active-session race but is not containment for hostile descendants or a second independent Kestrel process. Use a remote authenticated MCP endpoint or a separately contained process in those cases. An absent raw vault or genuinely empty keyring metadata remains usable for deterministic credential-free tests; Kestrel inspects metadata only and never resolves keyring values to make this decision.

Successful isolated validation writes schema-v2 authorization receipts and rotates `.nest/repair_receipt_signing.v2.key` only after the untrusted container exits. A review must link a currently valid v2 validation receipt for the same branch, HEAD, and diff. Legacy schema-v1 receipts and `.nest/repair_receipt_signing.key` are rejected for review and commit authorization.

Plugin-provided MCP stdio servers carry `connect_requires_approval` vetting metadata. Connect, test, sync, and invoke paths refuse to start the process until `POST /api/mcp/servers/{server_id}/approve-connect` records approval for the current command hash.

## Provider Certification and Support Bundle Wiring

`nest-agent product provider-certification` and `GET /api/product/provider-certification` expose a
redacted `kestrel.provider_certification.v2` matrix in deterministic provider order. Legacy
`status`, credential/base-URL presence, host checks, `next_action`, and validation-command fields
remain configuration-readiness diagnostics; they are not assurance. The separate
`certification_state` and `generate`, `stream`, `native_tools`, `tool_normalization`, and
`learning_e2e` dimensions are derived only from accepted evidence for the report's exact commit and
tree digest. A no-receipt runtime report therefore describes implemented or experimental surfaces
and current readiness without silently claiming mock, live, or release certification.

The HTTP route never accepts a caller-selected evidence path. Exact-schema evidence is handled by
the offline `scripts/run_provider_certification.py collect`, `build`, and `check` workflow so an
API request cannot turn an arbitrary local file into authority. Built reports retain tested models,
profile, evidence IDs, latest exact-scoped receipt time, freshness, and missing requirements. They
expose only credential environment-variable names or presence and pass through the normal
redaction path; raw API keys are never returned.

Receipt collection can safely occur after a credentialed job has removed its ephemeral secret:
the collector normalizes persisted results and requires only structural provider identity. Report
building captures the redacted readiness snapshot, while the checker gates evidence-backed
assurance without requiring an ephemeral credential to remain present. Evidence history therefore
survives secret cleanup without masquerading as current setup readiness; callers must run the
separate product setup/readiness gate when the deployed profile needs to call that provider.

`nest-agent product support-bundle` and `POST /api/product/support-bundle` write a local zip archive under the requested output path or `.nest/support-bundles/` by default. The archive is diagnostic only: it includes product/setup readiness, runtime metadata with environment-variable presence only, git status, SQLite table counts, log file metadata, and a bounded `events.jsonl` tail. Event-tail sanitization is default-deny for strings: only explicitly allowlisted operational identifiers, timestamps, statuses, categories, and tool/provider labels survive; prompts, messages, proof objectives, commands, diagnoses, strategies, errors, and other arbitrary nested text become `<redacted>`. The archive does not include raw Secret Broker vault contents, raw environment variable values, or `.mv2` memory files.

SSE and streamable HTTP transports share manager concepts but still need real fixtures and soak testing.
