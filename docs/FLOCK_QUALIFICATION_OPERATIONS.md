# Flock Qualification Operations

This document defines the operational contract for Adaptive Flock
qualification evidence: the deterministic mock gate, the explicit
non-default live-provider qualification runner, and the release evidence
report that a production activation claim must be built on.

## Evidence tiers

| Tier | Producer | What it proves | What it never proves |
|---|---|---|---|
| Deterministic mock | `scripts/run_flock_qualification_determinism.py` (Task 22) | The qualification pipeline is deterministic: twenty identical receipt projections, zero guardrail violations, bound to the source commit and a configuration digest. | Production provider qualification. Mock evidence cannot certify it. |
| Live installed artifact | `scripts/run_flock_live_qualification.py` (Task 23) | A completed live run against at least two real eligible targets on the exact installed artifact bytes, bound to the authenticated terminal receipt. | Nothing by itself activates anything. Activation is a separate owner GUI/API action. |

## Live runner boundary rules (inviolable)

- The runner is explicit and non-default. Every invocation requires
  `--run-id`, `--expected-receipt-id`, `--expected-receipt-digest`,
  `--installed-artifact-digest`, `--output`, and the explicit
  `--confirm-live-qualification` flag. Missing confirmation aborts before
  any state is read.
- The runner NEVER activates learned routing. The report records
  `activation_performed: false`; activation remains a separate owner GUI/API
  action with its own grant.
- There are no raw credential CLI arguments. Provider secrets stay behind
  Secret Broker references inside the authenticated local runtime; the
  runner reads only the durable local ledger and receipt.
- The report is redacted through the standard secret-redaction pass. Raw
  secrets and source content must never appear in it; preserve the report
  and the receipt, not the corpus content.

## Live evidence collection

Run the live qualification itself from the owner GUI/API against the exact
installed sidecar, with owner-configured real providers, a real project
corpus, and containment. After the run reaches a terminal `completed` state,
collect the evidence report:

```bash
python scripts/run_flock_live_qualification.py \
  --run-id "$RUN_ID" \
  --expected-receipt-id "$RECEIPT_ID" \
  --expected-receipt-digest "$RECEIPT_DIGEST" \
  --installed-artifact-digest "$ARTIFACT_SHA256" \
  --project-digest "$PROJECT_DIGEST" \
  --tree-digest "$TREE_DIGEST" \
  --state-dir .nest/state \
  --output "$ARTIFACT_DIR/flock-live-qualification.json" \
  --confirm-live-qualification
```

The runner fetches the immutable terminal receipt from the local ledger,
authenticates it against the control-plane integrity key, verifies the
receipt digest matches the owner-attested expectation, projects the receipt
into the evidence report, and verifies the report before writing it
atomically.

## Release evidence contract

Report schema: `kestrel.flock_live_qualification.v1`. The report binds:

- `source_commit` — the exact 40-hex source commit;
- `installed_artifact_digest` — SHA-256 of the exact installed bytes;
- `platform` / `architecture` — where the artifact ran;
- per-target provider profile/model subject digests and target digests, with
  `evidence_kind: real_provider` and eligibility;
- `project_digest` / `tree_digest` — the owner-attested real project corpus;
- `receipt_id` / `receipt_digest` — the authenticated terminal receipt;
- `grants` — the exact attempt grants (one entry per terminal attempt);
- `costs` — total, cap, and unresolved micros (unresolved must be zero;
  missing usage is never zero);
- `replay` — twenty repeats with exactly one unique projection digest;
- `guardrail_violations` — must be zero.

`verify_live_report` fails closed on the first violated invariant:

1. any mock-kind target evidence → "mock evidence cannot certify production
   provider qualification";
2. fewer than two real eligible targets → "live qualification requires at
   least two real eligible targets";
3. installed artifact digest mismatch → the report is not bound to the
   expected installed bytes;
4. receipt digest mismatch → the report is not bound to the receipt;
5. non-`completed` terminal status;
6. replay drift or fewer than twenty repeats;
7. any guardrail violation;
8. any unresolved cost;
9. missing exact attempt grants.

## Gated integration test

`tests/integration/test_flock_live_qualification.py` skips cleanly unless
`RUN_FLOCK_LIVE_QUALIFICATION=1` is set. With the flag set it additionally
requires the owner-configured coordinates
(`KESTREL_FLOCK_LIVE_STATE_DIR`, `KESTREL_FLOCK_LIVE_RUN_ID`,
`KESTREL_FLOCK_LIVE_RECEIPT_ID`, `KESTREL_FLOCK_LIVE_RECEIPT_DIGEST`,
`KESTREL_FLOCK_LIVE_ARTIFACT_DIGEST`, `KESTREL_FLOCK_LIVE_PROJECT_DIGEST`,
`KESTREL_FLOCK_LIVE_TREE_DIGEST`, `KESTREL_FLOCK_LIVE_OUTPUT`, optional
`KESTREL_FLOCK_LIVE_SOURCE_COMMIT`) and fails closed when any is missing.
This gate stays pending-operator until the owner configures real targets; do
not run it against shared or production providers without explicit
authorization.
