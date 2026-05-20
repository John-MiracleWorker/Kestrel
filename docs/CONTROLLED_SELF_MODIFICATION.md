# Controlled Self-Modification

Kestrel's controlled self-modification work is intentionally conservative. The goal is not to let an LLM rewrite itself. The goal is to let Kestrel propose bounded behavior changes, persist them with evidence, validate them, activate them under gates, measure whether they help, and roll them back without destroying the audit trail.

Core principle:

```text
Memory is not storage. Memory is a controlled, evidence-backed behavior-change system.
```

This document is the repo-local foundation for the remaining controlled self-modification phases. It reflects the current Kestrel contracts after the initial behavior-delta schema, ledger, proposal-extraction, mutation-gate, behavior-compiler, and replay-validation slices.

## Current baseline

Implemented foundation:

- `src/nested_memvid_agent/behavior_delta.py`
  - Typed behavior-delta schema.
  - Safe defaults: proposed status, disable-able rollback plan, zero activation stats.
  - Serialization to and from `MemoryRecord.metadata["behavior_delta"]`-style payloads.
  - Mapping to existing `MemoryKind` values without extending `MemoryKind`.

- `src/nested_memvid_agent/behavior_delta_ledger.py`
  - SQLite control-plane persistence for behavior deltas.
  - Append-only activation records.
  - Append-only outcome records.
  - Summary rates for useful/failure/rollback/never-activated outcomes.
  - Operator reporting rows with per-delta activation counts, outcome rates, reporting-window filters, and advisory recommendations.
  - Guard against activating terminal deltas without a future explicit override path.

- `src/nested_memvid_agent/behavior_delta_extractor.py`
  - Proposal-only extraction from task capsule payloads and learning signals.
  - Initial support for policy candidates, procedural candidates, correction rules, reusable lessons, and repeated failed tool-call heuristics.
  - Vague candidates are rejected before they become `BehaviorDelta` objects.
  - Optional ledger recording is explicit; dry-run remains the CLI default for safe review.

- `src/nested_memvid_agent/mutation_gate.py`
  - Rule-based activation gate for proposed behavior deltas.
  - Low-risk deltas stage without automatic activation.
  - Medium-risk deltas require validation/replay before activation.
  - Policy and approval-gate deltas require explicit instruction or reviewed-rule evidence, policy-layer targeting, policy activation enablement, replay pass, approval, exact-call approval, and rollback support.
  - Critical deltas remain staged/recommendation-only unless explicitly enabled by a future caller path.

- `src/nested_memvid_agent/behavior_compiler.py`
  - Default-off compiler for active, relevant, evidence-backed behavior deltas.
  - Structured sections for policy constraints, self-model rules, procedures, tool heuristics, context-packing rules, retrieval priorities, corrections, skill candidates, and delta evidence.
  - Relevance matching across objective/query, task type, tool names, memory layers, risk tags, and path globs.
  - Priority order: policy > self > procedural > semantic > episodic > working.
  - Activation logging is idempotent per run/delta when enabled.

- `src/nested_memvid_agent/behavior_delta_skill.py`
  - Renders instruction-only skill-candidate previews from `BehaviorDeltaKind.SKILL_CANDIDATE` deltas.
  - Produces a validated skill manifest plus `SKILL.md` preview sections for trigger, procedure, verification, pitfalls, evidence, and safety.
  - Ignores executable runtime metadata from learned deltas and never writes skill files or installs code.

- `src/nested_memvid_agent/config.py`
  - Adds default-off `NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=0`.
  - Adds `NEST_AGENT_MAX_ACTIVE_DELTAS_PER_RUN=8`.

- `scripts/eval_behavior_deltas.py`
  - Deterministic replay evaluator for behavior-delta scenarios.
  - Compares baseline text against BehaviorCompiler output for active deltas.
  - Supports `--mode agent` to run a full mock-provider `NestedMV2Agent` turn through the normal runtime context path and verify activation logging.
  - Emits structured JSON with baseline score, delta score, improvement, expected-behavior hits, gate violations, and pass/fail.
  - Includes initial fixtures for policy approval gates, `.mv2` canonical memory, and repeated validation retry strategy.

- `src/nested_memvid_agent/learned_routing.py` / `scripts/eval_memory_router.py`
  - Existing ORACLE memory-routing eval remains rule/ledger calibrated.
  - Adds behavior-delta shadow examples from the behavior-delta ledger: kind, target layer, risk, validation score, repeat count, explicit instruction signal, activation count, useful/failure/rollback/never-activated rates, and trigger specificity.
  - `scripts/eval_memory_router.py --include-behavior-deltas --json` includes advisory counterfactuals only; authority is explicitly `shadow_only`, gate authority remains `mutation_gate`, and policy-write authority is `false`.

- `src/nested_memvid_agent/server_behavior_delta_routes.py`
  - Adds read-only review API routes for listing behavior deltas, showing one delta with activation/outcome history, and rendering skill-candidate previews.
  - Mutating review actions (`activate`, `reject`, `rollback`) deliberately return `405 read_only_review_api` in this first UI/API slice.
  - Integrated into the FastAPI server under `/api/memory/deltas*` without adding activation, rollback, or skill-install side effects.

- `src/nested_memvid_agent/skill_validation.py`
  - Holds shared skill-manifest validation so behavior-delta skill previews can validate manifests without importing the full skill/tool/plugin runtime graph.

- `src/nested_memvid_agent/state_store.py`
  - Schema version `11` adds:
    - `behavior_delta_ledger`
    - `behavior_delta_activations`
    - `behavior_delta_outcomes`

Still intentionally not implemented:

- No automatic behavior-delta activation.
- No live provider replay validation.
- No policy-promotion behavior changes.
- No hidden system-prompt rewrite path.
- No replacement or weakening of the `.mv2` durable-memory contract.

## Non-negotiable invariants

These invariants apply to every future phase:

1. `.mv2` remains the canonical durable memory substrate.
2. SQLite remains control-plane state only, not retrieval memory.
3. Existing `MemoryKind` should not be extended for behavior deltas unless a later test proves it is unavoidable.
4. A behavior delta without evidence cannot become active.
5. A behavior delta without rollback metadata cannot become active.
6. Ordinary conversation, ordinary observation, or a single run success cannot create active policy behavior.
7. Policy-affecting deltas require explicit instruction or reviewed rule evidence, high validation, replay checks, config enablement, exact-call approval, and rollback support.
8. The LLM may propose behavior changes; Kestrel gates, validates, activates, logs, measures, and rolls them back.
9. ORACLE or learned routing may recommend or shadow-evaluate behavior-delta utility, but must not gain policy-write authority.
10. Skills generated from behavior deltas must remain proposals until manifest validation and approval-gated install.

## Conceptual pipeline

```text
Run trace / complete.mv2 capsule
    -> Proposal extractor
    -> BehaviorDelta(PROPOSED)
    -> BehaviorDeltaLedger
    -> MutationGate
    -> STAGED / REJECTED / needs replay / needs approval
    -> Replay validation
    -> ACTIVE delta if gates pass
    -> BehaviorCompiler
    -> Structured runtime instructions only when relevant
    -> Activation log
    -> Outcome ledger
    -> Operator reporting and shadow recommendations
```

The first two implementation slices created the typed proposal object and the ledger. Future work should connect the pipeline in small, test-first steps.

## Data model responsibilities

### `BehaviorDelta`

A `BehaviorDelta` represents a proposed or active behavior change. It must answer:

- What future condition activates this?
- What behavior changes when it activates?
- Which memory layer owns the behavior?
- What evidence supports it?
- How risky is it?
- What validation is required?
- How do we know if it helped?
- How can it be disabled or rolled back?

The schema module owns only type validation and serialization. It must not decide activation.

### `BehaviorDeltaLedger`

The ledger owns durable control-plane state:

- proposed/staged/active/rejected/rolled-back/expired delta records,
- activation history,
- outcome history,
- summary reporting.

The ledger must stay append-friendly. Rejections and failures are training data, not garbage.

### `MutationGate`

The mutation gate evaluates whether a behavior delta can remain staged, must be rejected, or has enough evidence to be considered active by a future caller. It sits above `NestedLearningKernel`, not in place of it.

`NestedLearningKernel` answers:

```text
Can this learning signal become a memory record?
```

`MutationGate` answers:

```text
Can this behavior delta become active runtime behavior?
```

### `BehaviorCompiler`

The behavior compiler transforms selected active deltas into structured runtime instructions. It remains separate from `ContextPacker` and is wired into the chat runtime only behind the default-off `enable_behavior_deltas` flag.

`ContextPacker` retrieves and packs memory context.

`BehaviorCompiler` compiles validated behavior deltas into bounded instructions such as:

```text
ACTIVE POLICY CONSTRAINTS:
- Preserve .mv2 as canonical memory. Vector sidecars must be rebuildable and keyed to .mv2 records.

ACTIVE PROCEDURES:
- Before retrying a failed validation command, compare the previous command and require a changed strategy.

DELTA EVIDENCE:
- delta_example: explicit user directive / replay scenario pass
```

## Feature flags

Future runtime activation must be default-off until tests prove safety.

Recommended flags:

```text
NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=0
NEST_AGENT_MAX_ACTIVE_DELTAS_PER_RUN=8
NEST_AGENT_ALLOW_POLICY_DELTAS=0
```

Rules:

- Disabled behavior deltas must produce identical runtime context to the current compiler path.
- Enabled behavior deltas must still activate only relevant, active, evidence-backed deltas.
- Policy-delta activation needs a separate explicit flag and exact-call approval path.

## Risk model

- `LOW`
  - Formatting preferences, small context hints, low-impact procedural reminders.
  - Can be staged with evidence; activation still requires tests once compilation exists.

- `MEDIUM`
  - Tool heuristics, debugging checklists, retrieval/context-packing preferences.
  - Requires validation or replay before activation.

- `HIGH`
  - Approval behavior, repair workflow constraints, policy-like runtime behavior.
  - Requires explicit approval, replay, and rollback support.

- `CRITICAL`
  - Policy writes, self-modification rules, code mutation authority, system-prompt affecting behavior.
  - Recommendation-only unless an explicit future policy path enables, validates, approves, and logs the exact activation.

## Phase roadmap

### Phase 3: Proposal extraction

Status: first backend slice implemented. Kestrel can now produce proposal-only deltas from task capsule payloads, summarized capsule learning signals, and repeated failed tool attempts. CLI dry-run review is available through:

```bash
nest-agent memory deltas propose --run-id <run_id> --dry-run
```

Remaining Phase 3 hardening: richer extraction sources, additional deterministic capsule fixtures, and broader CLI/API review paths.

Goal: produce `BehaviorDelta(PROPOSED)` records from evidence without activation.

Files likely involved:

- Create: `src/nested_memvid_agent/behavior_delta_extractor.py`
- Modify only if needed: `src/nested_memvid_agent/task_capsule.py`
- Add tests: `tests/test_behavior_delta_extractor.py`

Initial accepted sources:

- explicit user instruction,
- task capsule reusable lessons,
- repeated failed tool attempts,
- repeated successful procedures,
- repair review artifacts,
- user corrections.

Extractor requirements:

- Never activates a delta.
- Rejects vague proposals such as `be more careful`.
- Requires non-empty trigger, behavior change, evidence refs, target layer, risk, validation plan, and rollback plan.
- Writes proposals to the ledger only through an explicit caller path or dry-run command.

Validation target:

```bash
python -m compileall -q src tests scripts
python -m pytest -q tests/test_behavior_delta.py tests/test_behavior_delta_ledger.py tests/test_behavior_delta_extractor.py
```

### Phase 4: Mutation gate

Status: first backend slice implemented. `MutationGate` evaluates proposed deltas by risk, evidence, validation score, replay status, approval metadata, policy enablement, exact-call approval, and rollback support. It returns a `MutationDecision` only; it does not write the ledger, compile behavior, or alter runtime context.

Goal: evaluate behavior deltas by risk, evidence, validation, approval, and rollback requirements.

Files likely involved:

- Create: `src/nested_memvid_agent/mutation_gate.py`
- Add tests: `tests/test_mutation_gate.py`

Core API shape:

```python
@dataclass(frozen=True)
class MutationDecision:
    accepted: bool
    status: BehaviorDeltaStatus
    reason: str
    requires_replay: bool
    requires_human_approval: bool
    requires_exact_call_approval: bool
    blocked_by: tuple[str, ...] = ()
```

Mutation gate requirements:

- Low-risk deltas can stage with evidence.
- Medium-risk deltas require validation or replay before activation.
- High-risk deltas require explicit approval and replay.
- Critical deltas remain recommendation-only unless a future explicit policy path permits them.
- Policy and approval-gate deltas cannot become active without explicit instruction/review evidence, target layer `policy`, high validation score, replay pass, config enablement, exact-call approval, and rollback support.

Validation target:

```bash
python -m compileall -q src tests scripts
python -m pytest -q tests/test_behavior_delta.py tests/test_behavior_delta_ledger.py tests/test_mutation_gate.py
```

### Phase 5: Behavior compiler

Status: first runtime slice implemented. `BehaviorCompiler` can compile active, relevant, evidence-backed deltas from the ledger into structured sections behind a default-off config flag. It can log activations once per run/delta, and `NestedMV2Agent.chat` appends those sections to the compiled memory context only when behavior deltas are explicitly enabled.

Goal: compile relevant active deltas into bounded runtime instructions behind a default-off flag.

Files likely involved:

- Create: `src/nested_memvid_agent/behavior_compiler.py`
- Modify: context compiler integration point only after disabled-flag parity tests exist.
- Add tests: `tests/test_behavior_compiler.py`

Compiler requirements:

- Include only active deltas.
- Include only relevant deltas for the current task/objective/tool/path/layer context.
- Cap active deltas per run.
- Deduplicate overlapping instructions.
- Prioritize policy > self > procedural > semantic > episodic > working.
- Log one activation per run per delta.
- Refuse silent policy activation without evidence.
- Disabled flag must preserve byte-for-byte or semantically equivalent current behavior.

Validation target:

```bash
NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=0 python -m pytest -q tests/test_context_compiler.py tests/test_behavior_compiler.py
NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=1 python -m pytest -q tests/test_behavior_compiler.py
```

### Phase 6: Replay validation

Status: deterministic replay plus first full-agent replay slice implemented. `scripts/eval_behavior_deltas.py` can load JSON scenarios, compile active fixture deltas through `BehaviorCompiler`, compare baseline vs delta scores, fail on gate violations, and emit JSON. With `--mode agent`, it persists active deltas in the SQLite control plane, runs a mock-provider `NestedMV2Agent` turn through the normal chat/context path, and verifies activation logging. Initial fixtures cover policy approval gates, `.mv2` canonical memory, and repeated validation retry strategy.

Goal: compare baseline behavior vs behavior-with-delta using deterministic scenarios.

Files likely involved:

- Create: `scripts/eval_behavior_deltas.py`
- Create fixtures under: `tests/evals/behavior_deltas/`
- Add tests: `tests/test_behavior_delta_replay.py`

Replay requirements:

- Use mock providers wherever possible.
- Emit structured JSON results.
- Fail policy/approval-gate scenarios on any gate violation.
- Include at least:
  - policy write requires approval,
  - `.mv2` canonical memory constraint,
  - repeated validation retry requires changed strategy.

Validation target:

```bash
python scripts/eval_behavior_deltas.py --scenario tests/evals/behavior_deltas/policy_write_requires_approval.json --provider mock
python scripts/eval_behavior_deltas.py --scenario tests/evals/behavior_deltas/policy_write_requires_approval.json --provider mock --mode agent --fail-on-regression
python -m pytest -q tests/test_behavior_delta_replay.py
```

### Phase 7: Outcomes and reporting

Status: first CLI/reporting slice implemented. `BehaviorDeltaLedger.report_deltas()` now emits JSON-capable summary and per-delta rows with useful/failure/rollback/never-activated rates, activation counts, reporting-window filtering, and advisory-only recommendations. The CLI exposes this through:

```bash
nest-agent memory deltas ledger --since 30d --json
```

The recommendations are intentionally descriptive only; they do not update thresholds, activate deltas, or roll back deltas automatically.

Goal: make behavior-delta utility visible to operators.

Files likely involved:

- Extend: `src/nested_memvid_agent/behavior_delta_ledger.py`
- Add CLI/API only after ledger reporting tests exist.
- Add tests: `tests/test_behavior_delta_reporting.py`

Reporting requirements:

- JSON-capable summaries.
- Rates for useful, failure, rollback, never activated.
- Advisory recommendations only.
- No automatic threshold tuning.

### Phase 8: Skill integration

Status: first preview-only slice implemented. `BehaviorDeltaKind.SKILL_CANDIDATE` deltas can now render a validated instruction-only skill manifest and `SKILL.md` preview without writing files, installing the skill, or generating executable code. The CLI exposes this through:

```bash
nest-agent memory deltas skill-preview <delta_id> --json
```

The preview remains non-installable (`installable=false`). Any actual skill installation must continue through the existing validation and approval-gated skill install path.

Goal: allow procedural deltas to become skill candidates without automatic executable install.

Files likely involved:

- Create or extend a skill preview helper.
- Integrate with existing skill manifest validation and `skill.install` approval gates.
- Add tests: `tests/test_behavior_delta_skill_candidates.py`

Skill candidate requirements:

- Render `SKILL.md` preview with trigger, procedure, verification, pitfalls, evidence.
- Do not install executable code automatically.
- Failed validation keeps candidate staged or rejected.
- Activation/outcome tracking remains tied to the originating delta.

### Phase 9: ORACLE shadow integration

Status: first shadow-only behavior-delta utility slice implemented. Learned routing can now extract behavior-delta counterfactual examples from the delta ledger and include them in `eval_memory_router.py --include-behavior-deltas --json`. The output is advisory only: `authority=shadow_only`, `gate_authority=mutation_gate`, and `policy_write_authority=false`.

Goal: include behavior-delta outcome features in learned-routing evaluation while keeping gates rule-based.

Files likely involved:

- Extend: `src/nested_memvid_agent/learned_routing.py`
- Extend: `scripts/eval_memory_router.py` or add a behavior-delta-specific shadow eval.

ORACLE requirements:

- Shadow-only.
- Can recommend, abstain, and provide counterfactuals.
- Must not decide policy activation.
- Must not bypass `MutationGate`.

### Phase 10: Review UI/API

Status: first read-only API slice implemented. FastAPI now exposes `/api/memory/deltas`, `/api/memory/deltas/{delta_id}`, and `/api/memory/deltas/{delta_id}/skill-preview` for operator review. Mutating actions are intentionally blocked with `405 read_only_review_api`; web UI panels and approval-gated activation/reject/rollback endpoints remain future work.

Goal: make controlled self-modification visible and reviewable.

Files likely involved:

- API routes for list/show/replay/activate/reject/rollback.
- Web workbench panel after backend and CLI are stable.

UI/API requirements:

- No hidden active deltas.
- Show evidence refs, risk, status, replay result, validation requirements, activation history, and outcome stats.
- High-risk deltas must visibly show approval requirements.
- Rollback must be obvious and auditable.

## Suggested implementation sequence

Future work should proceed in small test-first slices:

1. Add extractor tests for explicit instruction parsing and vague proposal rejection.
2. Implement the minimal proposal extractor.
3. Add dry-run proposal CLI tests.
4. Add mutation gate tests for low/medium/high/critical tiers.
5. Implement `MutationGate` without activation side effects.
6. Add disabled-flag parity tests for context compilation.
7. Implement `BehaviorCompiler` behind `NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=0` default.
8. Add activation logging tests.
9. Add replay fixtures and script.
10. Add reporting and operator review surfaces.

Each slice should end with a narrow commit and targeted validation before broad suite runs.

## Definition of done for controlled self-modification

The full project is done when Kestrel can demonstrate this loop:

1. A run produces an evidence-rich trace or capsule.
2. Kestrel extracts a specific behavior-change proposal.
3. The proposal is typed, risk-classified, and linked to evidence.
4. The mutation gate stages, rejects, or requires validation.
5. Replay validation compares baseline vs delta behavior.
6. An approved active delta is compiled into a future run only when relevant.
7. Activation is logged.
8. Outcomes are recorded.
9. Ledger reporting shows whether the delta helped, failed, or was never useful.
10. Rollback can disable the delta without destroying audit history.

## Validation commands

Use narrow tests while developing, then broaden before committing:

```bash
python -m compileall -q src tests scripts
python -m pytest -q tests/test_behavior_delta.py tests/test_behavior_delta_ledger.py tests/test_nested_learning.py
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
PYTHONPATH=src python -m nested_memvid_agent.cli chat --backend memory --provider mock --message "hello"
```

If web/API files are touched:

```bash
npm run test --prefix web
npm run build --prefix web
```

If installer/startup files are touched:

```bash
bash -n install.sh
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_DRY_RUN=1 KESTREL_START_SERVER=0 bash install.sh
```
