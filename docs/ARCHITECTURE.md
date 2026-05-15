# Architecture: Nested Learning Agent Memory with Memvid

## Goal

Replace slow, flat agentic RAG with a nested memory system where each layer has its own update rule, confidence gate, retention policy, and Memvid `.mv2` capsule.

This does not remove the LLM context window. It replaces the context window as the **main memory strategy**. The context window becomes a compiled workbench, not the whole brain.

## Core loop

```text
User / Repo / Tool Output
        ↓
Raw Event Log JSONL
        ↓
Memory Extractor
        ↓
Nested Memory Layers (.mv2 capsules)
        ↓
Retriever + Context Compiler
        ↓
LLM / Agent Executor
        ↓
Evaluator / Tests / User Feedback
        ↓
Consolidator
        ↓
Promotions / Corrections / Forgetting
```

## Layer files

```text
memory/working.mv2      fast scratch state
memory/episodic.mv2     events, failures, decisions
memory/semantic.mv2     stable facts
memory/procedural.mv2   reusable skills and recipes
memory/policy.mv2       slow-changing behavior rules
logs/events.jsonl       raw audit trail
```

## Why one `.mv2` per layer?

Memvid can use tracks inside one file, but one file per nested layer is cleaner for an agent runtime:

- Different write rates: working memory updates constantly; policy memory barely changes.
- Different safety gates: policy should be locked down and optionally encrypted.
- Different compaction/retention: working memory can expire; procedural memory persists.
- Faster small-layer reads: retrieve from the layers that matter instead of waking one giant capsule.
- Easier debugging: inspect or replay one layer at a time.

A future optimization can create a `unified.mv2` for distribution while retaining per-layer source capsules during development.

## Nested Learning mapping

The Nested Learning idea is translated into external agent loops:

| Nested Learning concept | Agent implementation |
|---|---|
| Context flow | Raw events, tool outputs, user turns, retrieved evidence |
| Fast memory | Working `.mv2` + current task state |
| Slower memory | Episodic and semantic `.mv2` capsules |
| Continuum memory | Multiple layers with different update frequencies |
| Self-modifying module | Consolidator updates procedural/policy memory after validation |
| Expressive optimizer | Promotion rules, scoring, corrections, retrieval feedback |

## Read path

1. Start with an objective.
2. Generate a retrieval query from objective + active task state.
3. Search layers using layer-specific modes and budgets.
4. Rank hits by score, confidence, importance, and layer priority.
5. Compile a compact prompt with evidence and conflict warnings.
6. Hand only the compiled cognitive state to the LLM.

## Write path

1. Every raw event goes to `logs/events.jsonl`.
2. Useful observations go to working memory.
3. Validated events go to episodic memory.
4. Stable facts go to semantic memory.
5. Repeated successful procedures go to procedural memory.
6. Rare, highly validated rules go to policy memory.

## Anti-patterns

- Do not save everything as policy.
- Do not summarize away primary evidence without keeping provenance.
- Do not use semantic vector search for exact config/file-path facts when lexical search is better.
- Do not let the LLM self-promote rules without validation.
- Do not treat memory hits as truth; treat them as hypotheses with provenance.
