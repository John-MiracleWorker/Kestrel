# Architecture

Last updated: 2026-05-20

## Goal

Kestrel is a local-first agent runtime where memory is not a flat RAG index. It uses nested memory layers with separate update rules, confidence gates, retention behavior, and Memvid v2 `.mv2` files.

The LLM context window is still finite. Kestrel treats it as a compiled workbench: retrieve relevant memory, compress it into structured context frames, expand raw evidence only when needed, and pass a bounded pseudo-context prompt to the model.

## Runtime Shape

```text
CLI / API / Web / Channels
        ↓
RunManager / EventBus / StateStore
        ↓
NestedMV2Agent
        ↓
ContextCompiler + MV2 ContextPacker + BehaviorCompiler ← layered .mv2 retrieval
        ↓
LLM Provider
        ↓
ToolRegistry / MCP / Skills / Built-ins
        ↓
Tool results, approvals, diagnosis, repair gates
        ↓
Working memory + run events + task capsule
        ↓
NestedLearningKernel / Consolidator / MutationGate
        ↓
Episodic → Semantic/Procedural/Self → Policy .mv2 layers
```

## Memory Layers

```text
.nest/memory/working.mv2      active task state and observations
.nest/memory/episodic.mv2     events, failures, decisions, summaries
.nest/memory/semantic.mv2     validated facts and preferences
.nest/memory/procedural.mv2   reusable recipes and failure playbooks
.nest/memory/self.mv2         identity, capability, workflow, and self-change records
.nest/memory/policy.mv2       rare, explicit behavior/safety constraints
.nest/runs/{run_id}/complete.mv2  run-scoped evidence bundle
.nest/state/agent.db          control-plane state, not retrieval memory
.nest/logs/                   local audit/debug logs
```

## Why One `.mv2` Per Layer?

Memvid can carry multiple labels/tracks, but Kestrel keeps one file per permanent layer because:

- Working memory changes frequently; policy memory changes rarely.
- Each layer has different validation and promotion gates.
- Policy and procedural memory need stronger review boundaries.
- Small layer files are easier to verify, doctor, back up, and reason about.
- Tests can isolate `.mv2` lock contention by isolating memory directories.

A distribution/export optimization can be explored later, but the source-of-truth layout is one permanent `.mv2` file per layer.

## Nested Learning Mapping

| Nested Learning concept | Kestrel implementation |
|---|---|
| Context flow | User turns, tool outputs, validation results, event logs, retrieved evidence |
| Fast memory | Working `.mv2` plus current run/task state |
| Slower memory | Episodic and semantic `.mv2` layers |
| Continuum memory | Multiple layers with different write frequency and trust thresholds |
| Self-modifying module | Controlled consolidation into procedural/self/policy memory after validation |
| Expressive optimizer | Promotion gates, optimizer traces, correction/conflict handling, retrieval feedback |

This is an agent-runtime analogue of Nested Learning. It is not neural weight-level HOPE training or unrestricted self-modification.

## Read Path

1. Start with objective, run state, and current user message.
2. Build retrieval queries from the objective and active task context.
3. Search relevant layers under layer-appropriate budgets.
4. Convert hits into `MV2ContextFrame` records.
5. Rank by layer priority, relevance, confidence, importance, and validation metadata.
6. Deduplicate by content hash and overlap.
7. Emit conflict warnings instead of smoothing disagreements away.
8. Pack selected summaries/evidence under the token budget.
9. Use `context.expand` for exact raw evidence when needed.

## Write Path

1. User messages and tool results become working-memory observations.
2. Final turn summaries and meaningful outcomes become episodic evidence.
3. Validated stable facts can become semantic memory.
4. Repeated validated workflows can become procedural memory.
5. Validated identity, capability, workflow preference, and self-change signals can become self memory.
6. Policy memory remains rare and requires explicit instruction, high validation, repeat evidence, config enablement, and review or equivalent explicit configuration.
7. Promotions carry evidence, provenance, confidence, validation status, context-flow metadata, and optimizer traces.

## Control Plane

The SQLite state store tracks operational state:

- runs and run steps
- approvals and tool results
- MCP servers/tools
- skills and validation/provenance hashes
- plugins and plugin enablement metadata
- task nodes
- subagent runs
- promotion ledger and outcome rows
- behavior-delta ledger, activation rows, and outcome rows

Terminal run statuses and approval decisions are replay-safe. State migrations currently initialize schema version 11.

## Tooling Boundary

Tool execution goes through `ToolRegistry`:

- argument validation
- workspace/path safety
- timeout enforcement
- capability enablement gates
- exact-call approvals for high-risk tools
- structured success/failure results
- failure diagnosis where applicable

MCP tools are adapted into the same registry surface and default to approval-by-default unless explicitly trusted.

## Controlled Self-Modification

Behavior deltas are the current runtime-level self-modification primitive. They are typed, evidence-backed proposals stored in the SQLite control plane, not hidden prompt rewrites. `MutationGate` decides whether a delta can be staged/activated, `BehaviorCompiler` compiles only active relevant deltas, and tool-aware preflight can inject bounded advisory instructions when `NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=1`. Proposed/staged deltas do not run automatically, policy deltas remain approval-gated, and rollback/outcome rows preserve audit history.

Explicit direct commands such as `/search <query>` and `/memory search <query>` route deterministically to `memory.search` so operator/eval tool selection does not depend on model guesswork.

## Anti-Patterns

- Do not save everything as policy.
- Do not summarize away primary evidence without keeping provenance.
- Do not use SQLite as the primary memory store.
- Do not call `create(path)` on an existing `.mv2` file.
- Do not treat retrieved memory as unquestioned truth.
- Do not run high-risk tools without enablement and exact-call approval.
- Do not dump full transcripts into context by default.
