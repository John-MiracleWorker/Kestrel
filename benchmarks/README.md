# Kestrel Benchmarks

This directory contains comparative benchmarks for Kestrel's layered memory system and end-to-end agent runtime.

## Quick Start

Run the full suite:

```bash
python benchmarks/run_all.py --output benchmark_results/report.json
```

Run just memory benchmarks:

```bash
python benchmarks/memory_benchmark.py --k 5 --output benchmark_results/memory.json
```

Run the large layered-memory comparison directly:

```bash
python benchmarks/memory_benchmark_large.py \
  --k 5 \
  --output benchmark_results/memory-large.json
```

Run the unified built-in and optional-backend comparison:

```bash
python benchmarks/unified_memory_benchmark.py \
  --k 5 \
  --output benchmark_results/unified-memory.json
```

Run the flat-transcript comparative benchmark (Kestrel vs flat TF-IDF RAG vs
recency-biased flat transcript, including the recency-stress scenario):

```bash
python benchmarks/memory_benchmark_transcript.py \
  --output benchmark_results/memory-transcript.json
```

Run the breadth benchmark (fixed multi-seed / k / checkpoint / scenario
matrix, raw digest-bound results, aggregate metrics, deterministic
confidence intervals — BENCH-003/BENCH-004):

```bash
python benchmarks/memory_benchmark_breadth.py \
  --output benchmark_results/memory-breadth.json
```

You can narrow the matrix (e.g. for a quick smoke run) with
`--seeds`, `--k-values`, `--checkpoints`, and `--scenarios`.

The unified runner always attempts the built-in lexical Kestrel and TF-IDF backends first. Dense
VectorRAG, Qdrant, and Chroma are optional; when their local packages are absent, the JSON report
records an explicit `skipped` row and an install hint instead of crashing at import time. Missing
optional packages do not make the command fail. A backend that is installed but fails its benchmark,
returns no useful results, or falls below the versioned nonzero recall, precision, or MRR floors is
recorded as `failed` and makes the command exit nonzero. The same quality gate applies to required
and installed optional backends. Install all three optional comparisons with:

```bash
python -m pip install sentence-transformers qdrant-client chromadb
```

Run just agent benchmarks with the deterministic mock provider:

```bash
python benchmarks/agent_benchmark.py --provider mock --output benchmark_results/agent.json
```

Run the deterministic end-to-end learning release gate:

```bash
python benchmarks/real_agent_learning_benchmark.py \
  --output benchmark_results/agent_learning_gate.json
```

Run agent benchmarks with a real provider:

```bash
export OPENAI_API_KEY=...
python benchmarks/agent_benchmark.py --provider openai --model gpt-4.1-nano --backend memory
```

## What's Measured

### Memory Benchmark (`memory_benchmark.py`)

Head-to-head retrieval comparison between Kestrel's 6-layer Memvid-backed memory and a naive flat TF-IDF RAG baseline.

**Metrics**
- Recall@k
- Precision@k
- Mean Reciprocal Rank (MRR)
- Latency (avg + p99)

**Dataset**
- 50 synthetic documents across semantic, episodic, and procedural layers
- 30 ground-truth queries with known relevant documents
- Distractor documents mixed in to test precision

**Why the baseline is fair**
The baseline uses pure-Python TF-IDF with cosine similarity — a common "first RAG" implementation. It has no layers, no promotion gates, no trust ordering, and no context packing. This isolates the value of Kestrel's architecture.

Both standalone memory entrypoints fail closed. They require nonempty per-query evidence, versioned
absolute Recall@k, Precision@k, and MRR floors, and Kestrel quality that is not below the baseline.
The small runner uses `kestrel.memory-quality-floor.v1` (0.80 / 0.20 / 0.75); the large runner uses
`kestrel.large-memory-quality-floor.v1` (0.30 / 0.06 / 0.15). A zero-result retriever therefore
cannot produce a successful process exit even if the comparison baseline is also zero.

### Flat-Transcript Benchmark (`memory_benchmark_transcript.py`)

Three-way comparative benchmark on the same corpus and ground-truth queries:

1. **Kestrel** layered memory (in-memory benchmark backend, layer-aware
   retrieval)
2. **Flat TF-IDF RAG** (document store, cosine similarity, no layers)
3. **Flat transcript** (recency-biased chronological transcript — the
   "typical agent memory" proxy for chat-log style runtimes such as Hermes
   or OpenClaw: one transcript, no layers, no promotion, no trust ordering,
   recent entries win ties)

Two phases:

- **Baseline corpus**: the standard memory corpus.
- **Recency stress**: the same corpus with a burst of recent episodic
  "conversation chatter" appended after the facts. This models a real agent
  whose recent chat history buries older important facts. All three arms
  search the full corpus (no arm receives the ground-truth layer label):
  the flat transcript's recency bias is amplified by the chatter, and the
  benchmark measures how each arm's ranking degrades as a result.

The acceptance gate is methodological and fails closed: every arm must
return evidence for every query in both phases (a retriever that silently
returns nothing cannot pass, even if a comparison arm also returns
nothing), and all metrics must be finite. No arm is required to win: the
recency-stress scenario measurably degrades the flat transcript's recency
bias, and the deltas for every arm are reported exactly as measured —
including when Kestrel is not ahead. This follows BENCH-002, which removed
the oracle layer filter that previously let the Kestrel arm ignore the
episodic chatter and manufactured its recency advantage.

### Breadth Benchmark (`memory_benchmark_breadth.py`)

Fixed reproducible matrix over the same three arms (Kestrel layered memory,
flat TF-IDF RAG, recency-biased flat transcript):

- **seeds** (default `42, 1337, 2026`) — multi-seed breadth and
  seed-level variance.
- **k values** (default `3, 5`) — cutoff sensitivity.
- **corpus checkpoints** (default `0, 1, 2`) — growth: checkpoint `i` adds
  `i * 6` deterministic ground-truth-free filler documents, so growth
  degradation is measured against the same query set.
- **scenarios** — `baseline`, `recency`, `conflict`, `update`, `obsolete`,
  `distractor`, `overlap`, `common_term`. Each stressor is scenario-owned
  (deterministic documents + ground-truth queries) and none hands the
  Kestrel arm the ground-truth layer label (BENCH-002).

Published artifact (schema `kestrel.memory_benchmark.v3`):

- **Raw per-query rows** for every arm in every cell: query, layer,
  expected ids, retrieved ids, Recall@k, Precision@k, MRR, latency.
- **Aggregate metrics** per arm per cell: Recall@k, Precision@k, MRR,
  p50/p95/p99 latency.
- **Deterministic confidence intervals** — percentile bootstrap over the
  per-query metric values with a fixed RNG seed; deterministic because the
  per-query metrics are deterministic for the fixed matrix.
- **Degradation** — recency (baseline vs recency scenario) and growth
  (smallest vs largest checkpoint) deltas for every arm, exactly as
  measured, including when Kestrel is not ahead.
- **Digests** — `fixture_digest` (exact documents + queries of every cell),
  `environment_digest` (runtime snapshot), `methodology_digest` (matrix
  config + gate definition), all sha256 of canonical JSON.

The acceptance gate is methodological and fails closed (BENCH-004): every
arm must return evidence for every query in every cell and all per-query
metrics and CI bounds must be finite. **No arm is required to win**; the
report is honest about unfavorable results (for example, under recency
stress the flat transcript's recency bias degrades, and the honest
post-BENCH-002 comparison shows Kestrel can also degrade — those deltas are
reported exactly as measured).

### Agent Benchmark (`agent_benchmark.py`)

End-to-end task success measurement in sandboxed workspaces.

**Tasks**
1. **memory_persistence** — Write a specific preference with `memory.write`, verify that exact
   tool-created record, then retrieve and answer from it in a different session
2. **file_read_qa** — Read the exact requested file and answer from its observed contents
3. **repo_search** — Execute the exact repository search and name the matching file
4. **git_status** — Execute `git.status` and report the observed untracked file

**Metrics**
- Success rate
- Tool calls per task
- Total elapsed time

**Deterministic mode**
With `--provider mock`, the benchmark uses pre-programmed mock responses so it runs instantly and always produces the same results. This is useful for CI regression testing.
The report labels the executed memory backend as `in_memory` in mock mode and retains the CLI value
separately as `requested_backend`; all control-plane and extension paths are isolated in the task's
temporary directory.

A semantically correct sentence is necessary but not sufficient for task success. Every task also
requires a successful execution of the exact expected tool and arguments. Memory persistence uses
conjunctive evidence: the current `memory.write` result and record must match, the recall turn must
use a different session, `memory.search` must surface the written content, and the final answer must
be correct. Empty task sets and any failed task make the standalone command exit nonzero.

**Real-provider mode**
With a real LLM provider, the benchmark becomes a true capability evaluation. The mock responses are discarded and the agent's actual reasoning and tool selection are measured.

### Error-Recovery Benchmark (`error_recovery_benchmark.py`)

Controlled registry faults exercise transient recovery, terminal missing/empty results, alias
resolution, changed-strategy retries, and the maximum tool-round boundary. A task passes only when
the configured fault was actually injected and the observed executions prove the expected recovery
or bounded failure behavior. In particular, the strategy task requires an affirmative
`retry_allowed` decision, while the persistent-failure task requires a nonzero number of injected
failures no greater than the configured maximum. The standalone command exits nonzero for an empty
task set or any failed task.

### Agent Learning Gate (`real_agent_learning_benchmark.py`)

This is a deterministic production-path gate, despite the historical filename. It does not seed an
oracle lesson. Task 1 runs through the normal agentic failure cycle: a mock validation tool fails,
the runtime persists a `FailureEpisode`, a changed strategy produces a successful validation, and
the runtime persists a `LessonCard` linked to both failure and validation evidence. Task 2 must
retrieve that exact lesson as untrusted evidence, apply it, and improve from a fresh-memory control
failure to treatment success. High-risk mock file/test calls still require exact operator approval.

The command exits nonzero if the evidence/provenance/validation chain, retrieval transfer, expected
outcomes, or approval checks do not match. Use `scripts/run_live_learning_eval.py` for optional
real-provider learning evaluation; this deterministic release gate intentionally accepts only the
mock provider.

### Learning-Path Regression Benchmark (`learning_benchmark.py`)

This deterministic bundle exercises few-shot retrieval, recorded-mistake avoidance, promotion-gate
classification, outcome-calibrated routing, and procedural consolidation. Interpret two dimensions
narrowly:

- `promotion_gate_conformance` checks that decisions match the declared evidence-and-repeat contract.
  Its expected labels are not independent evidence that promoted memories will be useful.
- `router_calibration` trains on one synthetic set and evaluates a disjoint held-out set, but the
  reported utility delta remains a projection because the replay has no counterfactual outcomes for
  routes that were not taken.

These are regression gates, not standalone proof that an agent learns effectively in the wild. Use
`real_agent_learning_benchmark.py` for the deterministic production path and
`scripts/run_live_learning_eval.py` for provider-backed evidence.

## Interpreting Results

A healthy Kestrel installation should show:

- **Memory**: Kestrel should match or exceed the TF-IDF baseline on recall while maintaining higher precision due to layer-specific retrieval and trust ordering. MRR should be noticeably better because stable semantic/procedural layers surface high-confidence facts above noisy working memory.
- **Agent**: 100% success rate with mock provider (this validates the task harness). With real providers, success rate depends on model capability and tool description quality.

`run_all.py` also enforces the versioned absolute Kestrel memory floors in
`kestrel.aggregate-memory-quality-floor.v1`: Recall@k 0.80, Precision@k 0.20, and MRR 0.75. Relative
parity with a degraded baseline cannot make an otherwise low-quality run pass.

## Adding New Benchmarks

### New memory tasks

Edit `datasets/memory_corpus.py` and add documents + queries to the appropriate `_make_*_corpus()` function.

### New agent tasks

1. Add a task function in `agent_benchmark.py` that accepts `(agent, workspace)` and returns a dict with at least `task`, `success`, and `final_answer`.
2. Add a mock response program in `_mock_for_task()` if you want deterministic coverage.
3. Register the task name in `task_fns`.

## Files

```
benchmarks/
  README.md                  # This file
  baseline_rag.py            # Pure-Python TF-IDF RAG baseline
  memory_benchmark.py        # Head-to-head memory retrieval benchmark
  agent_benchmark.py         # End-to-end agent task benchmark
  real_agent_learning_benchmark.py # Production-path learning release gate
  run_all.py                 # Orchestrator
  datasets_corpus/
    memory_corpus.py         # Synthetic memory corpora and queries
```
