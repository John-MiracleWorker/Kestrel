# Task Capsules

Task capsules capture completed run evidence into a run-scoped Memvid v2 artifact:

```text
.nest/runs/{run_id}/complete.mv2
```

`complete.mv2` is not a permanent memory layer. The durable nested layers remain:

- `working.mv2`
- `episodic.mv2`
- `semantic.mv2`
- `procedural.mv2`
- `self.mv2`
- `policy.mv2`

`run_id` is restricted to one portable filename component (ASCII letters, digits, dots, underscores, and hyphens; starting and ending with a letter or digit). Absolute paths, traversal, and nested paths are rejected before the runs root or any capsule is accessed.

The capsule is a temporary evidence bundle that can be summarized and used to propose learning signals.

## Captured Data

A capsule may include:

- user objective
- selected context
- tool calls and tool outputs
- files touched
- tests run
- errors encountered
- final assistant response
- unresolved questions
- reusable lessons
- candidate facts
- candidate procedures
- candidate corrections
- candidate policy items requiring explicit review

## Consolidation Flow

After a completed run:

1. Write or update `.nest/runs/{run_id}/complete.mv2`.
2. Summarize the capsule.
3. Extract candidate `LearningSignal` objects.
4. Use `NestedLearningKernel` to decide reject, write, or promote.
5. Write accepted non-policy signals only through the apply path, when consolidation is enabled, not dry-run, and approved. A candidate without authenticated validation is written only as `unvalidated_episodic_staging`; the result reports `actual_layer: episodic`, the requested stable layer, and `validation_status: unresolved`. It is not a semantic or procedural promotion.
6. Block policy memory. Policy activation is a separate `memory.policy_promote` operation that requires explicit instruction, high validation, repeated evidence, config enablement, exact-call human approval, and durable result attestation.

By default:

- `enable_task_capsules = True`
- `task_capsule_retention_count = 1000`
- `enable_auto_consolidation = False`
- `auto_consolidation_dry_run = True`

Environment variables:

- `NEST_AGENT_ENABLE_TASK_CAPSULES`
- `NEST_AGENT_TASK_CAPSULE_RETENTION_COUNT`
- `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION`
- `NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN`

## Bounded Artifact Retention

Completed task capsules are derived run evidence; durable run state and promoted
episodic summaries live outside these per-run directories. After a capsule is
sealed and closed, Kestrel writes a private completion marker and may prune the
oldest completed capsule directories beyond the configured count. The default
retains the newest 1000 capsules.

Retention is fail-closed. It always preserves the newest completed capsule and
any run IDs explicitly protected by the caller. A directory is not removed when
it is active, partial, symlinked, contains hard-linked or non-regular artifacts,
has an unsafe owner or run ID, contains an unknown filename, or changes during
the retention pass. Legacy in-memory capsules are eligible only when their
sealed snapshot contains a valid capsule root and every declared child frame;
unmarked legacy Memvid capsules remain untouched. The pass returns a structured
report with retained, deleted, skipped, reclaimed-byte, and cleanup-warning data.

## Tool and API

Built-in tool:

- `capsule.summarize`
- `capsule.apply`

API route:

- `POST /api/capsules/{run_id}/summarize`
- `POST /api/capsules/{run_id}/apply`

`capsule.summarize` is preview-only and always returns dry-run decisions. `capsule.apply` is high-risk: it requires `enable_auto_consolidation`, then an approval gate, before it writes accepted signals. Unvalidated fact, procedure, and correction candidates can only become provenance-bearing episodic staging records with `stable_recall_eligible: false`; they require a later receipt-bound learning or correction flow to enter a stable layer. Policy candidates remain blocked unless all policy gates are satisfied.

Capsule summaries are designed for reviewable consolidation, not automatic policy mutation. Ordinary conversation can become working or episodic evidence only after validation; it must not become policy memory from a single event.
