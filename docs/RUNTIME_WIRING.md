# Runtime Wiring

Last updated: 2026-05-16

## Chat Turn Flow

```text
1. CLI/API/channel receives a user message.
2. RunManager creates or resumes a run when using persistent run surfaces.
3. Persistent background runs enter the durable graph runtime: planner, executor, reviewer, recovery, memory-promotion, and finalizer nodes.
4. The executor node keeps using the existing chat loop.
5. Agent writes the user observation to working memory.
6. ContextCompiler delegates to the MV2 context packer.
7. The packer retrieves memory frames, prefers summaries, deduplicates, flags conflicts, and emits a bounded pseudo-context prompt.
8. The default-on agentic failure cycle retrieves prior procedural/episodic failure lessons and injects a `Prior Failure Lessons` section when relevant.
9. Agent builds messages:
   - system prompt
   - compiled nested memory context
   - prior failure lessons when found
   - available tool specs
   - user message
10. LLM provider returns either:
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

The FastAPI server exposes the same state through run, event, approval, scheduler, memory, context, skill, MCP, and channel routes.

Soul/self routes expose the same non-secret runtime model as the CLI: `/api/self`, `/api/self/remember`, `/api/self/propose-change`, `/api/web/search`, and `/api/web/fetch`.

## State Store

`AgentStateStore` is SQLite control-plane storage, currently schema version 9.

It stores:

- runs and run steps
- approval requests, decisions, and executed tool results
- MCP server records and discovered tools
- skill records and validation/provenance metadata
- plugin records and enablement metadata
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

`git.commit` never pushes. On repair branches, it also requires a current `repair.review` artifact tied to successful validation and the current diff hash.

`self.propose_change` is disabled unless `allow_self_modification` is enabled, still requires exact-call approval, and only records the requested self-change. Any actual code edit must use `repair.prepare`, `repair.apply_patch`, `repair.validate`, `repair.review`, and `git.commit`.

`web.search` and `web.fetch` are disabled unless `allow_web` is enabled. They are read-only context tools; `web.fetch` rejects private, local, link-local, multicast, reserved, and unspecified addresses and applies timeout/byte limits.

The plugin registry can fetch public GitHub repositories and materialize plugin-declared skills/MCP servers. CLI/API plugin install, update, enable, and sync/materialization routes require `NEST_AGENT_ALLOW_PLUGIN_INSTALL=true` or `--allow-plugin-install`. Agent-initiated `plugin.install` uses the same enablement gate plus exact-call approval, and installed plugins remain disabled unless explicitly enabled.

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

MCP `secret_env` values are redacted in API responses, included in configuration fingerprints, and resolved from `os.environ` only when launching a process. Raw secret-looking keys in MCP `env` are rejected.

SSE and streamable HTTP transports share manager concepts but still need real fixtures and soak testing.
