# Validation Plan

Last updated: 2026-05-20

## Test Pyramid

### Core Tests

Run:

```bash
python -m pytest -q
```

Coverage goals:

- model validation
- memory layer contracts
- context compiler/packer budget behavior
- consolidation and promotion gates
- provider parsing/fallback behavior
- tool schemas, timeouts, enablement, and approvals
- state-store lifecycle and migration behavior
- CLI and run-manager smoke paths
- scheduler, subagent, skill, repair, task-capsule, behavior-delta, and live-learning slices

### Compile, Lint, and Types

```bash
python -m compileall -q src tests scripts
python -m ruff check scripts src tests
python -m mypy src
```

### Runtime Smoke

```bash
nest-agent doctor --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "hello"
nest-agent run --backend memory --provider mock --json --events "hello run"
```

### Memvid Integration

Run after installing the `memvid` extra:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

This validates temporary `.mv2` creation, writes, sealing, verification, reopening, search, context-frame metadata, and run capsule summaries.

### MCP Integration

```bash
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
```

This validates managed stdio server connection, discovery, invocation, and shutdown.

### Behavior Delta and Live Learning Evals

```bash
python scripts/eval_behavior_deltas.py --scenario tests/evals/behavior_deltas/policy_write_requires_approval.json --fail-on-regression
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memory --output-root /tmp/kestrel-live-learning-memory
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
```

Live commands require provider credentials in environment variables and must use isolated memory directories. The committed mock path remains the default release gate; the Ollama Cloud + `gpt-oss:120b` path has been validated locally for memory and Memvid backends.

### Golden Evals

Fast path:

```bash
python scripts/run_golden_evals.py --backend memory --provider mock
```

Golden evals should stay deterministic under the mock provider and can also be run against live providers. They prove behavior across turns, including recall, prior-failure use, procedural promotion gates, workspace safety, shell blocking, `.mv2` verification, context packing, direct `/search` tool routing, durable plan completion, and policy-write refusal.

## Promotion Validation

Every proposed promotion must include:

- source memory IDs or capsule/run evidence
- source and target layers
- validation score
- repeat count
- confidence
- evidence refs
- provenance
- validation status
- promotion or rejection reason
- context-flow and optimizer-trace metadata where the Nested Learning kernel is used

Policy promotions require explicit instruction, repeat evidence, high validation, config enablement, exact-call human approval, and durable approval/result attestation through `memory.policy_promote` before they may receive system-priority recall.

## Context Validation

Compiled context should be checked for:

- objective present
- relevant memories included
- token/character budget respected
- source/evidence pointers preserved
- confidence and validation metadata present where available
- conflict warnings present for contradictory high-confidence memories
- no full transcript dump by default
- raw evidence expanded only when requested or necessary

## Repair Validation

Repair tools must preserve these gates:

- mutation tools require approval and file-write enablement where applicable
- patch/validate/rollback refuse non-repair branches
- repeated validation retries are blocked unless the strategy changes when prior lessons exist
- successful validation points to `create_repair_review_before_commit`
- repair branch commits require a current `repair.review` artifact and exact-call approval
- `git.commit` never pushes

## Failure Handling

If validation fails:

1. Keep the exact command and error output.
2. Classify the failure when possible.
3. Recall similar procedural/episodic lessons before retrying.
4. Change strategy before repeating the same validation loop.
5. Do not promote new semantic/procedural/self/policy memory from a failed run unless the failure itself is useful evidence.
6. Add or update a focused regression test when the failure reveals a real gap.
