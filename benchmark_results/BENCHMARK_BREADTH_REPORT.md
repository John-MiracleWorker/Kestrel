# S10 — Breadth benchmark & public artifact (BENCH-003 / BENCH-004)

Date: 2026-08-23 (EDT)
Worker: kestrel-deepseek-worker (task t_bfbf0f10)
Branch: `codex/v06-s10-benchmark-breadth`

## 1. What was built

Two new files under `benchmarks/` plus a test file:

- `benchmarks/datasets_corpus/memory_corpus_breadth.py` — the fixed
  scenario matrix: `baseline`, `recency`, `conflict`, `update`, `obsolete`,
  `distractor`, `overlap`, `common_term`, each built deterministically from
  (scenario, seed, checkpoint), with ground-truth queries and growth filler.
- `benchmarks/memory_benchmark_breadth.py` — the v3 benchmark runner
  (schema `kestrel.memory_benchmark.v3`), CLI, methodology gate, and digests.
- `tests/test_memory_benchmark_breadth.py` — 12 tests (all green).
- `benchmarks/run_all.py` — new `--breadth-only` mode with assertions.
- `benchmarks/README.md` — breadth benchmark documentation.
- `benchmark_results/memory-breadth.json` — the published artifact (raw
  per-query rows + aggregates + digests).

## 2. The fixed matrix (BENCH-003)

    seeds      = 42, 1337, 2026            (multi-seed)
    k values   = 3, 5
    checkpoints = 0, 1, 2                  (corpus growth; cp i adds 6i filler docs)
    scenarios  = baseline, recency, conflict, update, obsolete,
                 distractor, overlap, common_term

144 cells x 3 arms (Kestrel layered memory, flat TF-IDF RAG, recency-biased
flat transcript) = 11,988 raw per-query rows published in the artifact.

Scenario semantics (all deterministic, none hands the Kestrel arm the
ground-truth layer label — BENCH-002 carried forward):

- `recency`: 20 recent episodic chatter turns appended after the facts.
- `conflict`: documents that contradict base facts (wrong values, same vocab).
- `update`: newer documents that supersede base facts; ground truth for the
  updated topics is the NEW document.
- `obsolete`: documents explicitly describing retired/obsolete behavior; the
  query asks for the current state.
- `distractor`: near-duplicate documents sharing query vocabulary.
- `overlap`: documents sharing a broad common vocabulary; the query asks for
  the one document with the specific detail.
- `common_term`: a self-contained corpus of ubiquitous-term documents; queries
  are made of high-document-frequency words plus one rare discriminator.

## 3. Reported metrics (BENCH-004)

Per arm per cell: Recall@k, Precision@k, MRR, latency p50/p95/p99, plus a
deterministic percentile-bootstrap 95% CI (fixed seed, 2000 replicates) on the
quality metrics. Recency and growth degradation are reported as deltas for
every arm. All digests are sha256 over canonical JSON:

- `fixture_digest` — exact documents + queries of every cell
  (verified: recomputing from the matrix reproduces
  `fddbf80fa90d6438c6495ffa9f5e1ec91f1b81e62f6ee9e66b3d2db378326b5a`).
- `environment_digest` — runtime snapshot (python, platform, backend,
  package versions, seed mode).
- `methodology_digest` — matrix config + gate definition.

Determinism was verified by running the full matrix twice (standalone and via
`run_all --breadth-only`): identical fixture digest and identical quality
metrics. Latency percentiles are wall-clock measurements and are reported as
measured (environment-sensitive), as BENCH-004 requires.

## 4. Honest results (exactly as measured; no arm required to win)

The acceptance gate is methodological and fails closed: every arm must return
evidence for every query in every cell and all metrics/CI bounds must be
finite. It passed 2160/2160 checks. No arm is required to beat another.

Headline quality metrics (k=5, seed=42, checkpoint 0):

| Scenario    | Kestrel MRR | TF-IDF MRR | Transcript MRR |
|-------------|-------------|------------|----------------|
| baseline    | 0.8167      | 0.8778     | 0.8678         |
| recency     | 0.6956      | 0.8261     | 0.7778         |
| conflict    | 0.7844      | 0.8761     | 0.8389         |
| update      | 0.7980      | 0.8384     | 0.8556         |
| obsolete    | 0.7904      | 0.8510     | 0.8217         |
| distractor  | 0.7872      | 0.8622     | 0.8417         |
| overlap     | 0.8021      | 0.8698     | 0.8552         |
| common_term | 1.0000      | 1.0000     | 1.0000         |

Recall@k is 1.0000 for every arm in every scenario at k=5/cp=0 except the
recency-stressed Kestrel arm (0.9333) — matching the S9 honest post-BENCH-002
finding.

Recency degradation (MRR delta, baseline → recency, all seeds/k/checkpoints):

- Kestrel: -0.1004
- TF-IDF: -0.0540
- Transcript: -0.0984

Growth degradation (MRR delta, cp0 → cp2, all seeds/k, baseline scenario):

- Kestrel: -0.0222
- TF-IDF: 0.0000
- Transcript: -0.0167

Honest limitations surfaced by the breadth matrix:

1. **Kestrel does not win on most scenarios.** On every scenario except the
   pathological `common_term` (where all arms are perfect), the flat TF-IDF
   RAG and/or the recency-biased transcript achieve higher MRR. These are
   reported exactly as measured (BENCH-004). The flat baselines are strong
   lexical retrievers on this synthetic corpus, and Kestrel's layered
   retrieval does not currently add ranking value over them on this fixture.
2. **Recency still hurts Kestrel most.** After BENCH-002 removed the oracle
   layer filter, the recency stress degrades Kestrel's MRR by ~0.10 — more
   than either flat baseline. This matches the S9 honest result (stressed
   Kestrel MRR 0.696 vs tfidf 0.826 vs transcript 0.778) and is not hidden.
3. **The update scenario is hard for every arm.** For the "current rate
   limits" update query all three arms rank the superseded base fact above
   the newer update doc (measured, not fixed). The benchmark records this as
   an honest unfavorable result rather than engineering the fixture so
   Kestrel wins.

These results are the point of the benchmark: breadth and growth are measured
reproducibly and unfavorable results are reported honestly. The methodology
gate never requires Kestrel to win.

## 5. Verification evidence

- `tests/test_memory_benchmark_breadth.py`: 12/12 green (schema + fixed
  matrix, methodology gate with no required winner, raw rows + p50/95/99,
  deterministic CIs, reproducible digests, fixture-digest binding, recency +
  growth degradation for every arm, honest recency degradation, no-oracle
  Kestrel arm, CLI writes machine-readable report, deterministic corpus +
  grounded expected ids, common-term evidence).
- `tests/test_memory_transcript_benchmark.py` + `tests/test_benchmark_runners.py`:
  12/12 green (no regression to S9 benchmark).
- `benchmarks/memory_benchmark_breadth.py` standalone: exit 0, acceptance
  passed (144 cells, 2160 checks), wrote
  `benchmark_results/memory-breadth.json` (7.8 MB).
- `benchmarks/run_all.py --breadth-only`: exit 0, acceptance passed with all
  three breadth assertions true.
- Determinism: two full-matrix runs produced the identical fixture digest
  `fddbf80f...` and identical quality metrics.
- Ruff: clean. Mypy (CI style, `MYPYPATH=src:benchmarks --strict`): clean on
  both new modules.

## 6. Scope note

This implements S10 + BENCH-003/BENCH-004 only. It grants no evidence or
authority for S11+, installed-artifact final release, promotion, publication,
or final v0.6 qualification. The SOT slice table and ledger updates are
orchestrator-owned closure work, not part of this branch.
