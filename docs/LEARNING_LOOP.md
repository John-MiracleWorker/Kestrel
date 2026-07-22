# Learning Loop

Kestrel keeps the core learning gates rule based. That is deliberate: auditable per-decision behavior is more valuable right now than a learned scoring function that is harder to inspect, retrain, and roll back.

This layer closes the loop with instrumentation. The system records what was promoted, what later happened to it, and which gates look mis-tuned. It does not auto-adjust thresholds. Operators read the ledger, decide, edit constants, and commit those changes with evidence.

## The Principle

The nested learning kernel still computes deterministic validation scores and applies explicit threshold gates. The new outcome ledger adds Level 1 feedback: it answers whether previous promotions were later useful, corrected, contradicted, superseded, tombstoned, or never retrieved.

That makes future tuning possible without replacing the engine. If the outcome data later proves the gates are miscalibrated in ways simple threshold changes cannot fix, Kestrel can graduate beyond Level 1. Until then, the rules stay readable.

## The Three Mechanisms

Threshold gates decide whether a signal can enter a target layer. They use `promotion_threshold`, `provisional_threshold`, repeat counts, explicit-instruction requirements, and the source/target layer relationship.

Heuristic detectors keep local behavior deterministic. `SequenceMatcher`, normalized failure signatures, polarity checks, and conflict detection are used for bounded tasks such as lesson dedup and contradicted-memory detection. They are not general truth engines.

Evidence-presence scoring turns objective proof into a fixed validation score. Tests, lint/type checks, repair validation, and review evidence each contribute through `ValidationEvidence`. Human explicitness is a bounded bonus only when objective evidence exists.

## The Graded Tier

`promotion_status` is stored on promoted records:

- `confirmed`: the signal cleared the full gate.
- `provisional`: the signal cleared the provisional gate but missed the full gate.

`provisional_threshold` defaults to `promotion_threshold - 0.13`. Provisional records still need the target layer's repeat-count requirement. They are written with degraded confidence, half retention, and normal retrieval visibility, but they cannot be a source for further promotion.

When later evidence clears the full gate for the same conceptual slot, `LayeredMemorySystem.confirm_provisional()` upgrades the existing provisional record instead of duplicating it. The old provisional promotion gets a `useful` outcome in the ledger, and the record's half-retention expiry is cleared.

## The Ledger

Every promoted record receives:

- `promotion_id`
- `promotion_status`
- source and target layer metadata
- validation score, repeat count, explicit-instruction status
- optimizer trace and decision reason

The SQLite control-plane state stores this in `promotion_ledger`. Outcome events append to `promotion_outcomes`; multiple outcomes are allowed for one promotion.

Outcome meanings:

- `useful`: later evidence confirmed the promotion was valuable.
- `corrected`: a correction frame superseded the record.
- `contradicted`: deterministic conflict detection found an incompatible later record.
- `tombstoned`: the record was marked inactive.
- `superseded`: another record replaced it outside the correction flow.
- `never_retrieved`: retention compaction removed it before any retrieval write-back.

Retrieval updates each hit's `last_retrieved_at` metadata at most once per hour. This is intentionally lightweight and stored on the memory record rather than in a separate retrieval log.

Run:

```bash
nest-agent memory ledger
nest-agent memory ledger --since 90d
nest-agent memory ledger --since 7d --layer procedural
nest-agent memory ledger --outcome corrected
nest-agent memory ledger --json
```

The command reports promoted counts, outcome counts, false-positive rates, and deterministic recommendations. Recommendations are advisory only:

- False-positive rate above 5% suggests raising the gate.
- Never-retrieved rate above 40% suggests the gate is admitting too eagerly.
- Useful rate above 90% on low volume suggests the gate may be too strict.

## ORACLE Shadow Routing

ORACLE, the Outcome-Calibrated Layer Router, is the first bridge from ledger feedback to learned routing. It does not replace the deterministic kernel. The current implementation trains a small serializable utility table from promotion outcomes, extracts the same gate inputs the kernel uses, and records counterfactual predictions in `LearningDecision` payloads when a router is injected.

The default rollout posture is shadow-only:

- The rule-based `NestedLearningKernel` still decides the write target.
- ORACLE can say which admissible target it would have chosen and why it abstained.
- Guardrails block policy writes without explicit repeated validation, block further promotion from provisional records, and keep self/procedural routing behind their hard evidence gates.
- Replay evaluation can estimate expected utility, false-positive rate, never-retrieved rate, useful rate, abstention rate, and gate violations without changing memory writes.

The replay harness requires an existing regular, non-symlink SQLite state database and evaluates a consistent private SQLite backup. It never initializes, migrates, or enables WAL mode on the source database.

Run the offline replay harness against the existing control-plane state:

```bash
python scripts/eval_memory_router.py \
  --state-db .nest/state/agent.db \
  --mode replay \
  --baseline rule \
  --candidate oracle \
  --json
```

Activation beyond shadow mode should remain narrow and reversible. Low-risk gates such as working-to-episodic or episodic-to-semantic can be evaluated first; policy routing remains recommendation-only unless a future explicit enablement gate is added.

## Learning Architecture Eval Harness

`scripts/eval_learning_architecture.py` is the integrated eval for the behavior-learning loop. It is separate from the promotion-ledger replay harness: instead of only asking whether a memory signal should be admitted, it asks whether a whole controlled learning cycle stays safe and auditable.

Stages:

1. setup: isolated workspace, `.nest/memory`, SQLite control-plane state, seeded memory, and seeded active behavior deltas.
2. provider smoke: deterministic mock response or one guarded live provider call.
3. agent run: a normal Kestrel runtime turn with shell/web/dangerous tools disabled.
4. capsule/trace extraction: run-scoped `complete.mv2` capsule plus behavior-delta proposal extraction.
5. mutation gate: rule-based staged/rejected/active decision under supplied evidence.
6. replay validation: baseline score, delta score, and improvement without activating proposed deltas.
7. behavior compilation: active relevant deltas compile into bounded future context.
8. tool-aware preflight: active relevant deltas compile before a matching tool call and log one tool activation.
9. outcome ledger: useful/ignored/failure/rollback outcomes are appended and summarized.
10. rollback: configured active test deltas become `rolled_back`, stop compiling, and keep audit history.

Mock evals are the normal path:

```bash
python scripts/eval_learning_architecture.py --provider mock --backend memory --all --json
```

Live provider evals are skipped unless explicitly enabled:

```bash
RUN_LIVE_LEARNING_EVALS=1 \
OPENAI_API_KEY=... \
python scripts/eval_learning_architecture.py \
  --provider openai \
  --model "${NEST_AGENT_EVAL_MODEL:-gpt-5-mini}" \
  --backend memory \
  --scenario live_provider_smoke_learning_loop \
  --max-llm-calls 3 \
  --max-cost-usd 0.50
```

Interpret failures by stage. A provider-smoke failure means provider setup or credentials are wrong. A capsule/proposal failure means the trace did not contain specific enough evidence. A mutation-gate failure means the expected gate status did not match the supplied evidence. A replay failure means the proposed behavior does not improve the deterministic expectation or trips a forbidden behavior. A preflight failure means the active delta did not match the tool context or activation dedupe broke. A rollback failure means audit history or future compilation semantics regressed.

The harness does not auto-tune thresholds, does not grant policy-write authority, does not rewrite hidden system prompts, and does not replace `.mv2` as canonical memory. Low-risk proposed or staged deltas can auto-activate only in the runtime path when the default-off low-risk auto-activation flag is enabled and `MutationGate` evidence requirements pass.

## Tuning Playbook

Every quarter:

1. Run `nest-agent memory ledger --since 90d`.
2. Review each gate's promoted volume, false-positive rate, never-retrieved rate, and useful rate.
3. If a gate's false-positive rate is above 5% across two quarters, raise that gate's `promotion_threshold` by `0.03`.
4. If a gate's never-retrieved rate is above 40%, raise that gate's `promotion_threshold` by `0.03`.
5. If volume is very low and useful rate is above 90%, consider lowering that gate's threshold by `0.03`.
6. Commit threshold changes with a note naming the ledger period that motivated the change.

Do not auto-apply recommendations. The operator reads, decides, edits, tests, and commits.

## When To Graduate Beyond Level 1

After at least one year of outcome data, consider Level 2 threshold tuning only if the ledger consistently shows gate miscalibration that manual threshold edits cannot fix.

Do not jump to a learned scoring function without at least two years of ledger data, a clear retraining strategy, a rollback plan, and tests that prove deterministic mock behavior remains stable.
