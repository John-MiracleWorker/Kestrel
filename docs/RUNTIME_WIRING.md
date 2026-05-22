# Runtime Wiring

Last updated: 2026-05-20

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
   - available tool specs
   - user message
10. Explicit direct commands such as `/search <query>` can be routed to deterministic tools before the LLM. Otherwise, the LLM provider returns either:
   - final text, or
   - native provider tool calls, or
   - the portable JSON envelope with tool calls.
11. Provider output is validated against the active `ToolSpec` registry before tool execution.
12. Before same-action retries, the retry gate requires a meaningful changed strategy.
13. ToolRegistry validates schemas, enablement, timeout limits, and approval requirements.
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

The FastAPI server exposes the same state through run, event, approval, scheduler, memory, context, skill, MCP, behavior-delta, plugin, product readiness/setup, and channel routes.

Soul/self routes expose the same non-secret runtime model as the CLI: `/api/self`, `/api/self/remember`, `/api/self/propose-change`, `/api/web/search`, and `/api/web/fetch`.

## State Store

`AgentStateStore` is SQLite control-plane storage, currently schema version 11.

It stores:

- runs and run steps
- approval requests, decisions, and executed tool results
- MCP server records and discovered tools
- skill records and validation/provenance metadata
- plugin records and enablement metadata
- promotion ledger and promotion outcome records
- behavior-delta ledger, activation records, and outcome records
- task nodes
- subagent runs
- trace spans

Run records also persist the provider/model selected for the run so the local operator UI can launch and inspect runs without falling back to process-global provider assumptions.

Terminal run records are replay-safe: completed, failed, and cancelled runs are immutable. Approval records are immutable after they leave `pending`, and approved tool results are recorded back onto the approval record without reopening it.

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

The envelope and native provider tool calls both pass through strict schema validation against the current `ToolSpec` registry before execution.

## Trace Spans

The run timeline remains append-only event history. Trace spans add a durable flight-recorder model over that history. Current span types include `run`, `plan`, `llm.request`, `tool.call`, `memory.write`, `approval.wait`, `review`, and `eval`. `/api/runs/{run_id}/trace` returns both the timeline and persisted spans with counts by span type.

## Permission and Approval Model

Low-risk tools can run after argument validation.

High-risk tools require:

1. Capability enablement when the tool has a matching allow flag.
2. Exact-call approval when `require_approval_for_high_risk_tools` is enabled.
3. Matching tool-call ID and unchanged arguments at execution time.

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

`git.commit` never pushes. It refuses protected branches from `NEST_AGENT_PROTECTED_BRANCHES` (`main,master,release/*` by default). On repair branches, it also requires a current `repair.review` artifact tied to successful validation and the current diff hash.

Remote publishing is not part of the default tool lane. `NEST_AGENT_ALLOW_GIT_PUSH=false`, `NEST_AGENT_ALLOW_REMOTE_MUTATION=false`, and `NEST_AGENT_GIT_WRITE_MODE=local_branch` keep self-improvement local by default. The shell tool blocks common remote-mutation escape routes such as `git push`, `git tag`, `git remote set-url`, `gh repo edit`, `gh secret set`, `gh workflow enable`, `rm -rf .git`, and writes to `.git/config` before subprocess execution.

The local-only git lane includes `git.create_local_branch` for approval-gated branch creation and `git.export_patch` for approval-gated patch artifacts under `.kestrel/improvements/`. Neither tool pushes or tracks remotes.

`self.propose_change` is disabled unless `allow_self_modification` is enabled, still requires exact-call approval, and only records the requested self-change. Any actual code edit must use `repair.prepare`, `repair.apply_patch`, `repair.validate`, `repair.review`, and `git.commit`.

`web.search` and `web.fetch` are disabled unless `allow_web` is enabled. They are read-only context tools; `web.fetch` rejects private, local, link-local, multicast, reserved, and unspecified addresses and applies timeout/byte limits.

The plugin registry can fetch public GitHub repositories and materialize plugin-declared skills/MCP servers. CLI/API plugin review, install, update, enable, and sync/materialization routes require `NEST_AGENT_ALLOW_PLUGIN_INSTALL=true` or `--allow-plugin-install`. Review returns provenance, dependency, isolation, warning, unsupported-feature, and enable-blocker metadata without installing or executing plugin code. Agent-initiated `plugin.review` and `plugin.install` use the same enablement gate plus exact-call approval, and installed plugins remain disabled unless explicitly enabled. Plugins with unmanaged declared dependencies or required unavailable isolation can be installed disabled but cannot be enabled.

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

## Behavior Delta Wiring

Behavior-delta runtime wiring is default-off via `NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=0`. When enabled, active relevant deltas are compiled into structured runtime instructions, activation rows are logged once per run/delta, and tool-aware preflight passes advisory instructions through the tool context without bypassing capability flags or exact-call approval gates. Proposed/staged deltas are never auto-activated, and policy-affecting deltas still require MutationGate approval, exact-call review actions, evidence, validation, and rollback metadata.

Stage 1 autonomous-learning hardening adds read-only observability and kill-switch scaffolding only. `/api/learning/dashboard` and `nest-agent learning dashboard` aggregate existing behavior-delta activation/outcome rows into headline counts (auto-activations, rollbacks, false-positive rate, activations-then-rolled-back, average time-to-rollback) plus per-layer breakdowns. No schema migration or runtime behavior change is introduced by the dashboard.

Autonomous-learning rollout flags currently default off:

- `enable_auto_activate_low_risk_deltas` / `NEST_AGENT_ENABLE_AUTO_ACTIVATE_LOW_RISK_DELTAS=0`: future gate for LOW-risk behavior-delta auto-activation after validation, replay, repeat evidence, and rollback checks. Rollback command when enabled: set `NEST_AGENT_ENABLE_AUTO_ACTIVATE_LOW_RISK_DELTAS=0`, restart, then roll back individual active deltas through the existing delta rollback endpoint/CLI.
- `enable_auto_skill_materialization` / `NEST_AGENT_ENABLE_AUTO_SKILL_MATERIALIZATION=0`: future gate for instruction-runtime skill materialization from repeatedly successful procedures. Rollback command when enabled: set `NEST_AGENT_ENABLE_AUTO_SKILL_MATERIALIZATION=0`, restart, then disable/remove individual skills through the skills panel.
- `enable_auto_consolidation_shadow` / `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_SHADOW=0`: future gate for end-of-run consolidation shadow decisions that do not write memory. Rollback command when enabled: set `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_SHADOW=0` and restart.
- `enable_auto_consolidation_apply` / `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_APPLY=0`: future gate for applying validated consolidation decisions. It does not unlock policy writes. Rollback command when enabled: set `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION_APPLY=0`, restart, then use existing promotion-ledger reversal tooling where available.
- `enable_diagnosis_to_patch` / `NEST_AGENT_ENABLE_DIAGNOSIS_TO_PATCH=0`: future gate for diagnosis-to-patch DAG preparation. It does not auto-commit. Rollback command when enabled: set `NEST_AGENT_ENABLE_DIAGNOSIS_TO_PATCH=0`, restart, then clean up in-flight repair branches through existing `repair.rollback`.

## Scheduler and Subagents

Background runs seed a root task and a small deterministic task DAG. The graph runtime records planner metadata on the root task, executes the chat loop through the executor node, gates completion through the reviewer node, and records recovery metadata when approvals or failures block progress. The scheduler can execute approved ready tasks when `enable_autonomous_scheduler` or `NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER` is enabled.

Scheduler bounds:

- `max_scheduler_tasks` / `NEST_AGENT_MAX_SCHEDULER_TASKS`
- `max_scheduler_cycles` / `NEST_AGENT_MAX_SCHEDULER_CYCLES`

Ready tasks must be queued or approved, have completed dependencies, and pass retry-strategy gates. Tasks requiring approval remain blocked until explicitly approved.

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

Newly discovered MCP tools default to approval-by-default. Manifest `trusted` and `allow_autonomous` fields are ignored unless the operator explicitly configures `risk_policy=trust_manifest`; dangerous tool names or descriptions are promoted to high risk during vetting.

Manual API MCP invocations route through the same tool registry as agent tool calls, so MCP approval and risk policies produce the same `approval_required`, `tool_disabled`, and exact-call approval behavior. MCP GET routes return stored state only; live health checks, discovery, and sync are POST actions.

MCP `secret_env` values are redacted in API responses, included in configuration fingerprints, and resolved from `os.environ` or `secret://...` broker refs only when launching a process. Raw secret-looking keys in MCP `env` are rejected.

## Secret Broker Wiring

`NEST_AGENT_SECRET_STORE_PATH` points to a local Secret Broker vault. `POST /api/secrets` accepts the raw value through the trusted backend flow and returns only metadata. `GET /api/secrets`, channel `env_status`, MCP `secret_env_status`, runtime config, and self-inspection never return raw values. Channels resolve configured env names through the broker at delivery/signature-verification time; MCP stdio servers resolve `secret://...` references into child process environment variables at launch.

`NEST_AGENT_SECRET_BACKEND=json` is the default local-file backend. `NEST_AGENT_SECRET_BACKEND=keyring` or `--secret-backend keyring` stores raw values through the optional OS keyring provider and keeps only metadata in the JSON vault; if the optional `keyring` package cannot be imported, Kestrel falls back to the JSON backend.

Plugin-provided MCP stdio servers carry `connect_requires_approval` vetting metadata. Connect, test, sync, and invoke paths refuse to start the process until `POST /api/mcp/servers/{server_id}/approve-connect` records approval for the current command hash.

SSE and streamable HTTP transports share manager concepts but still need real fixtures and soak testing.
