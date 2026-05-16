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
5. Write accepted non-policy signals only through the apply path, when consolidation is enabled, not dry-run, and approved.
6. Block policy memory unless explicit instruction, validation threshold, repeat-count threshold, config enablement, and human review or equivalent explicit configuration are present.

By default:

- `enable_task_capsules = True`
- `enable_auto_consolidation = False`
- `auto_consolidation_dry_run = True`

Environment variables:

- `NEST_AGENT_ENABLE_TASK_CAPSULES`
- `NEST_AGENT_ENABLE_AUTO_CONSOLIDATION`
- `NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN`

## Tool and API

Built-in tool:

- `capsule.summarize`
- `capsule.apply`

API route:

- `POST /api/capsules/{run_id}/summarize`
- `POST /api/capsules/{run_id}/apply`

`capsule.summarize` is preview-only and always returns dry-run decisions. `capsule.apply` is high-risk: it requires `enable_auto_consolidation`, then an approval gate, before it writes accepted signals. Policy candidates remain blocked unless all policy gates are satisfied.

Capsule summaries are designed for reviewable consolidation, not automatic policy mutation. Ordinary conversation can become working or episodic evidence only after validation; it must not become policy memory from a single event.
