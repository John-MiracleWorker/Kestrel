# Nested Learning Framework Specification

## Operating principle

The agent is modeled as a set of nested learning/update loops. Each loop compresses a different kind of context flow into a different memory layer.

The LLM is not the only learner. The full agent system learns through memory extraction, retrieval feedback, evaluation, consolidation, correction, and policy updates.

This implementation maps the paper "Nested Learning: The Illusion of Deep Learning Architectures" to agent memory infrastructure rather than model-weight training. The paper frames learning systems as nested, multi-level and/or parallel optimization problems with their own context flows, treats optimizers as associative memories that compress their context, and proposes continuum memory as a spectrum of update frequencies. In this repo, those ideas become explicit context-flow records, optimizer traces, and conservative memory routing gates.

## Paper-guided implementation

The executable bridge lives in `src/nested_memvid_agent/nested_learning.py`.

- `ContextFlow` represents a nested optimization loop with a level, update frequency, source layers, target layer, objective, compression rule, and retention behavior.
- `OptimizerTrace` records the associative-memory update trace for a learning decision: surprise, validation score, repeat count, compression ratio, confidence delta, and effective confidence.
- `NestedLearningKernel` decides whether a validated signal should be rejected, written, or promoted, and attaches context-flow metadata to the resulting memory record.
- `memory.learn` compresses a validated learning signal into the appropriate layer. Policy writes are still blocked unless the policy gate and config enablement both pass.
- `memory.consolidate` now uses the same kernel so promotion metadata includes the context flow and optimizer trace.

This is not a claim that the repo implements HOPE or neural self-modifying weights. It is the agent-runtime analogue: nested context-flow optimization over durable memory layers.

## Layers

### L0: Event log

- Storage: JSONL, not retrieval memory.
- Purpose: immutable audit trail.
- Inputs: user messages, tool calls, command output, code patches, test results, retrieval traces.
- Promotion: extract candidates into working/episodic memory.

### L1: Working memory

- Storage: `working.mv2`.
- Update cadence: every step.
- Purpose: active task state, unresolved assumptions, current failures.
- Retrieval mode: lexical by default.
- Retention: short.
- Promotion: to episodic after validation.

### L2: Episodic memory

- Storage: `episodic.mv2`.
- Update cadence: meaningful events and session summaries.
- Purpose: what happened, what failed, what worked, what the user decided.
- Retrieval mode: hybrid.
- Promotion: to semantic or procedural memory.

### L3: Semantic/project memory

- Storage: `semantic.mv2`.
- Update cadence: validated facts.
- Purpose: stable repo facts, user preferences, API rules, architecture details.
- Retrieval mode: hybrid.
- Promotion: usually none; can feed procedural if fact supports a recipe.

### L4: Procedural memory

- Storage: `procedural.mv2`.
- Update cadence: repeated validated success.
- Purpose: recipes, checklists, debugging playbooks, tool-use skills.
- Retrieval mode: hybrid.
- Promotion: to policy only after repeated high-confidence validation.

### L5: Policy memory

- Storage: `policy.mv2`, optionally encrypted `.mv2e`.
- Update cadence: rare.
- Purpose: global behavior constraints and high-value safety rules.
- Retrieval mode: lexical exactness preferred.
- Promotion: manual review or strong automated gate.

## Promotion thresholds

| Source | Target | Gate |
|---|---|---|
| Working | Episodic | validation_score >= 0.65 |
| Episodic | Semantic | validation_score >= 0.78 and fact-like |
| Episodic | Procedural | validation_score >= 0.78 and repeat_count >= 2 for failure/procedure |
| Procedural | Policy | validation_score >= 0.95 and repeat_count >= 5 |

The implementation adds one more policy constraint: policy promotion must be based on an explicit instruction or reviewed rule. A repeated ordinary event can become semantic/procedural memory, but it must not become policy by accident.

## Confidence model

Every memory has:

- `confidence`: how likely the content is true.
- `importance`: how useful it is for future tasks.
- `evidence`: source refs.
- `layer`: update loop.
- `kind`: fact/event/failure/procedure/policy/etc.

Use confidence for write gates. Use importance for ranking. Use evidence for trust.

## Corrections

When a memory is wrong, do not silently overwrite it. Store a correction and demote or tombstone the bad record where supported.

Memvid’s correction APIs can be used for boosted retrieval once Codex hardens the backend.

## Forgetting

Forgetting is not deleting blindly. Recommended behavior:

- Working memory expires aggressively.
- Episodic memory is summarized after sessions.
- Semantic memory is corrected rather than deleted.
- Procedural memory is demoted when recipes fail repeatedly.
- Policy memory requires explicit review for deletion or modification.

## Context compiler contract

The compiler must produce:

- Current objective.
- Relevant policy/procedural/semantic/episodic/working memories.
- Scores and confidence.
- Evidence pointers.
- Conflict warnings where applicable.
- Next-step instruction.

It must not produce:

- Full conversation dump by default.
- Uncited claims as if they are facts.
- Permanent-memory updates inside the prompt without validation.
