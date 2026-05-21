# Controlled Self-Modification Demo

Last updated: 2026-05-20

This demo gives Kestrel a deterministic, operator-friendly proof of the controlled self-modification loop:

```text
capsule → proposal → mutation gate → replay → activation → outcome → rollback
```

It is intentionally local and isolated. It does not use a live model, does not mutate the project’s durable `.nest/memory`, and does not activate behavior in the normal runtime.

## Command

```bash
python scripts/demo_controlled_self_modification.py \
  --output-dir tmp-controlled-self-modification-demo \
  --json
```

Optional Memvid capsule smoke:

```bash
python scripts/demo_controlled_self_modification.py \
  --backend memvid \
  --output-dir tmp-controlled-self-modification-demo-memvid \
  --json
```

## What it proves

The script performs one complete, auditable loop:

1. Writes an isolated run capsule at `runs/demo_controlled_self_modification/complete.mv2`.
2. Extracts a `BehaviorDeltaKind.TOOL_HEURISTIC` proposal from repeated failed `shell.run` attempts.
3. Runs the `MutationGate` before replay and verifies the delta is only staged because replay evidence is missing.
4. Runs deterministic behavior-delta replay and verifies the delta improves behavior without gate violations.
5. Re-runs the `MutationGate` with replay evidence and activates the delta.
6. Compiles the active delta into runtime behavior instructions and records one activation.
7. Records a useful outcome.
8. Rolls the delta back and records a rollback outcome.
9. Compiles again and verifies no rolled-back delta is active.
10. Writes a JSON payload and Markdown report.

## Artifacts

The script writes:

```text
<output-dir>/state.db
<output-dir>/runs/demo_controlled_self_modification/complete.mv2
<output-dir>/controlled_self_modification_demo.json
<output-dir>/controlled_self_modification_demo.md
```

The JSON is machine-checkable. The Markdown is suitable for screenshots, release notes, or demo walkthroughs.

## Safety properties

- Isolated SQLite control-plane state under `--output-dir`.
- No writes to the user’s durable Kestrel memory directory.
- No live provider calls.
- No automatic policy behavior.
- No hidden prompt mutation.
- Rollback preserves the ledger audit trail.

## Validation

```bash
python -m pytest -q tests/test_controlled_self_modification_demo.py
python -m compileall -q src tests scripts
```

The test asserts the full loop, generated artifacts, activation logging, outcome reporting, rollback, and post-rollback inactive compilation.
