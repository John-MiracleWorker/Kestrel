# Runtime Wiring

Last updated: 2026-05-16

## Chat Turn Flow

```text
1. CLI/API/channel receives a user message.
2. RunManager creates or resumes a run when using persistent run surfaces.
3. Agent writes the user observation to working memory.
4. ContextCompiler delegates to the MV2 context packer.
5. The packer retrieves memory frames, prefers summaries, deduplicates, flags conflicts, and emits a bounded pseudo-context prompt.
6. Agent builds messages:
   - system prompt
   - compiled nested memory context
   - available tool specs
   - user message
7. LLM provider returns either:
   - final text, or
   - the portable JSON envelope with tool calls.
8. ToolRegistry validates schemas, enablement, timeout limits, and approval requirements.
9. Approved/allowed tools execute and return structured results.
10. Agent writes tool outputs or failures to working memory.
11. Agent loops until final answer, approval block, tool-round limit, provider failure, or timeout.
12. Agent writes turn summary to episodic memory.
13. Task capsule writer may create `.nest/runs/{run_id}/complete.mv2`.
14. Changed memory layers are sealed.
15. Run state and timeline events are persisted.
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

## State Store

`AgentStateStore` is SQLite control-plane storage, currently schema version 7.

It stores:

- runs and run steps
- approval requests, decisions, and executed tool results
- MCP server records and discovered tools
- skill records and validation/provenance metadata
- plugin records and enablement metadata
- task nodes
- subagent runs

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

This keeps providers portable while native provider-specific tool calling continues to harden.

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

`git.commit` never pushes. On repair branches, it also requires a current `repair.review` artifact tied to successful validation and the current diff hash.

The plugin registry can fetch public GitHub repositories and materialize plugin-declared skills/MCP servers. CLI/API plugin installs are operator actions; agent-initiated `plugin.install` requires `NEST_AGENT_ALLOW_PLUGIN_INSTALL=true` or `--allow-plugin-install` plus exact-call approval, and installed plugins remain disabled unless explicitly enabled.

## Memory Wiring

Permanent memory layers:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
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

Background runs seed a root task and a small deterministic task DAG. The scheduler can execute approved ready tasks when `enable_autonomous_scheduler` or `NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER` is enabled.

Scheduler bounds:

- `max_scheduler_tasks` / `NEST_AGENT_MAX_SCHEDULER_TASKS`
- `max_scheduler_cycles` / `NEST_AGENT_MAX_SCHEDULER_CYCLES`

Ready tasks must be queued or approved, have completed dependencies, and pass retry-strategy gates. Tasks requiring approval remain blocked until explicitly approved.

Subagents are currently in-process planner/worker/reviewer profiles with durable records. True branch/worktree isolation and Codex-backed fan-out remain future hardening.

## Plugin Wiring

Plugins are stored under `.nest/plugins` by default and persisted in the `plugin_registry` table. The CLI exposes list/install/inspect/enable/disable/update/remove commands. Enabled plugins can materialize namespaced skills and MCP server records into the same registry surfaces used by native Kestrel skills and MCP servers.

Plugin installation is a high-risk surface because it fetches repository content. Treat dependency isolation, install approval, and shared-runtime security review as remaining hardening work.

## MCP Wiring

MCP stdio servers use a managed lazy lifecycle:

- add server record
- connect on demand
- discover tools
- normalize tool risk and approval requirements
- reuse the session for invocations
- disconnect/restart on request
- tear down on config changes, delete, or shutdown

Newly discovered MCP tools default to approval-by-default unless the server is explicitly configured to trust its manifest. Dangerous tool names or descriptions are promoted to high risk during vetting.

SSE and streamable HTTP transports share manager concepts but still need real fixtures and soak testing.
