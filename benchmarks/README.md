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

### Agent Benchmark (`agent_benchmark.py`)

End-to-end task success measurement in sandboxed workspaces.

**Tasks**
1. **memory_persistence** — Remember a user preference across two turns
2. **file_navigation** — Find a specific file in a nested directory tree
3. **git_detective** — Use git tools to find the most recent commit message
4. **repair_diagnosis** — Diagnose a simple code bug

**Metrics**
- Success rate
- Tool calls per task
- Total elapsed time

**Deterministic mode**
With `--provider mock`, the benchmark uses pre-programmed mock responses so it runs instantly and always produces the same results. This is useful for CI regression testing.

**Real-provider mode**
With a real LLM provider, the benchmark becomes a true capability evaluation. The mock responses are discarded and the agent's actual reasoning and tool selection are measured.

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

## Interpreting Results

A healthy Kestrel installation should show:

- **Memory**: Kestrel should match or exceed the TF-IDF baseline on recall while maintaining higher precision due to layer-specific retrieval and trust ordering. MRR should be noticeably better because stable semantic/procedural layers surface high-confidence facts above noisy working memory.
- **Agent**: 100% success rate with mock provider (this validates the task harness). With real providers, success rate depends on model capability and tool description quality.

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
  datasets/
    memory_corpus.py         # Synthetic memory corpora and queries
```
