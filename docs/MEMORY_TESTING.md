# Memory System Testing Findings

This document records the deterministic memory-system test pass added for Kestrel's layered `.mv2` memory architecture. The suite exercises behavior around layer defaults, retrieval, context packing, promotion gates, correction/tombstone handling, promotion-ledger diagnostics, cross-layer flow, and backend consistency.

## Scope

The tests preserve the current architecture:

- one permanent memory file per layer: `working.mv2`, `episodic.mv2`, `semantic.mv2`, `procedural.mv2`, `self.mv2`, and `policy.mv2`
- run-scoped task capsules under `.nest/runs/{run_id}/complete.mv2`
- `InMemoryBackend` for deterministic default tests
- Memvid integration behind `RUN_MEMVID_INTEGRATION=1`
- SQLite only as control-plane state for ledger/runtime data, not as primary retrieval memory

## Added Coverage

New deterministic tests:

- `tests/test_memory_retrieval_contract.py`
- `tests/test_memory_context_packing.py`
- `tests/test_memory_promotion_gates.py`
- `tests/test_memory_layer_interactions.py`
- `tests/test_memory_backend_consistency.py`
- `tests/integration/test_memvid_memory_system.py`

New eval harness:

- `scripts/run_memory_system_evals.py`

New read-only diagnostics surface:

- `memory.ledger`

## Findings

Layer contract:

- All six permanent layers are constructed by `LayeredMemorySystem.from_backend_factory`.
- Each layer maps to the expected default `.mv2` filename.
- Working memory has a lower write threshold and shorter retention than stable layers.
- Policy memory stays lexical-only even when vector settings are supplied.
- Procedural memory only becomes hybrid when explicit complete local vector settings are present.

Retrieval contract:

- Retrieval searches every requested layer and respects `k_per_layer` plus each layer's `retrieval_k`.
- Tombstoned records are hidden from normal retrieval.
- `include_inactive=True` exposes tombstoned records for audit.
- Retrieval writes `last_retrieved_at` back to records.
- Retrieval write-back is throttled to at most once per hour per hit.

Context packing:

- Prompt sections render in trust order: policy, self, procedural, semantic, episodic, working.
- Higher-trust layers rank before noisy lower layers when relevance is comparable.
- Summary frames are preferred over raw chunks by default.
- Raw chunks are included for exact evidence requests or explicit expansion.
- Correction, failure, and conflict frames remain visible even when raw chunks are omitted.
- Child raw frames are expanded from summary frames when exact evidence is requested.
- Conflict warnings appear in the packed prompt instead of silently merging incompatible claims.

Promotion gates:

- Low-confidence writes are accepted only where layer thresholds allow them.
- Direct `memory.write` remains limited to `working` and `episodic`.
- Direct policy writes are blocked even when `allow_policy_writes=True`.
- Procedural memory requires repeated validated evidence.
- Policy memory requires explicit instruction, sufficient repeat count, high validation, high confidence, and `allow_policy_writes=True`.
- Schema-less one-off signals can no longer enter self memory through a direct target request.
- Provisional records remain retrievable, cannot promote further, and can be confirmed without duplication.

Correction and ledger behavior:

- `memory.correct` writes a correction frame and tombstones the superseded target.
- Tombstoned records carry `active=false`, `tombstone_reason`, `tombstoned_at`, and `superseded_by`.
- Corrected/tombstoned outcomes are appended to the promotion ledger.
- `memory.ledger` exposes promotion summaries without changing thresholds.
- Ledger recommendations remain deterministic advice only; no automatic threshold edits occur.

Cross-layer flow:

- The mock agent writes working user/tool records and an episodic turn summary.
- A validated learning signal can promote to semantic memory.
- A later context compile retrieves the promoted semantic record.
- The policy layer remains untouched in the end-to-end flow.

Backend consistency:

- `InMemoryBackend` now has focused contract coverage for `put`, `upsert`, `find`, `iter_records`, `get_record`, `tombstone`, `verify`, `seal`, snapshot reload, and cross-layer write rejection.
- The gated Memvid integration test checks one `.mv2` per layer, safe reopen, record write/seal/verify/retrieve, tombstone persistence, inactive retrieval, and frame metadata round-trip.

## Commands

Focused memory suite:

```bash
.venv/bin/pytest -q \
  tests/test_memory_retrieval_contract.py \
  tests/test_memory_context_packing.py \
  tests/test_memory_promotion_gates.py \
  tests/test_memory_layer_interactions.py \
  tests/test_memory_backend_consistency.py
```

Memory eval harness:

```bash
.venv/bin/python scripts/run_memory_system_evals.py --backend memory --provider mock
```

Optional Memvid integration:

```bash
RUN_MEMVID_INTEGRATION=1 .venv/bin/pytest -q tests/integration/test_memvid_memory_system.py
```

Full local validation for this pass:

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/pytest -q
.venv/bin/python scripts/run_golden_evals.py --backend memory --provider mock
.venv/bin/python scripts/run_memory_system_evals.py --backend memory --provider mock
.venv/bin/mypy src
.venv/bin/ruff check src tests scripts
git diff --check
```

## Eval JSON

Output from:

```bash
.venv/bin/python scripts/run_memory_system_evals.py --backend memory --provider mock
```

```json
{
  "backend": "memory",
  "diagnostics_schema": "kestrel.memory_system_eval.v1",
  "passed": true,
  "provider": "mock",
  "results": [
    {
      "name": "layer_contract",
      "passed": true,
      "mv2_files": {
        "episodic": "episodic.mv2",
        "policy": "policy.mv2",
        "procedural": "procedural.mv2",
        "self": "self.mv2",
        "semantic": "semantic.mv2",
        "working": "working.mv2"
      },
      "thresholds": {
        "episodic": 0.5,
        "policy": 0.95,
        "procedural": 0.82,
        "self": 0.78,
        "semantic": 0.75,
        "working": 0.2
      }
    },
    {
      "name": "retrieval_contract",
      "passed": true,
      "retrieved_layers": [
        "episodic",
        "policy",
        "procedural",
        "self",
        "semantic",
        "working"
      ],
      "memory_hit_count": 6,
      "memory_write_count": 6
    },
    {
      "name": "context_summary_first",
      "passed": true,
      "compact_titles": [
        "Eval summary",
        "Eval correction"
      ],
      "exact_titles": [
        "Eval summary",
        "Eval correction",
        "Eval raw"
      ],
      "memory_hit_count": 5
    },
    {
      "name": "promotion_gates",
      "passed": true
    },
    {
      "name": "correction_tombstone",
      "passed": true,
      "correction_record_id": "correction_00144ca884355d85",
      "ledger_outcomes": {
        "contradicted": 0,
        "corrected": 1,
        "never_retrieved": 0,
        "superseded": 0,
        "tombstoned": 1,
        "useful": 0
      },
      "memory_write_count": 2
    },
    {
      "name": "promotion_ledger",
      "passed": true,
      "rows": [
        {
          "gate": "episodic->semantic",
          "promoted": 1,
          "source_layer": "episodic",
          "target_layer": "semantic",
          "outcomes": {
            "contradicted": 0,
            "corrected": 0,
            "never_retrieved": 0,
            "superseded": 0,
            "tombstoned": 0,
            "useful": 1
          },
          "false_positive_rate": 0.0,
          "never_retrieved_rate": 0.0,
          "useful_rate": 1.0,
          "average_time_to_outcome_hours": 0.0
        }
      ]
    },
    {
      "name": "tool_surface",
      "passed": true,
      "stable_reject_error": "stable_memory_write_rejected",
      "verify_layers": {
        "episodic": true,
        "policy": true,
        "procedural": true,
        "self": true,
        "semantic": true,
        "working": true
      }
    },
    {
      "name": "agent_cross_layer_flow",
      "passed": true,
      "context_chars": 1529,
      "episodic_records": 1,
      "memory_hit_count": 2,
      "memory_write_count": 4,
      "policy_write_count": 0,
      "semantic_records": 1,
      "working_records": 2
    },
    {
      "name": "backend_consistency",
      "passed": true,
      "hits_after": 0,
      "hits_before": 1,
      "inactive_hits": 1
    }
  ],
  "summary": {
    "case_count": 9,
    "fail_count": 0,
    "memory_hit_count": 13,
    "memory_write_count": 12,
    "pass_count": 9,
    "policy_write_count": 0
  }
}
```

## Notes

The test pass found one real gate issue: direct requested self-memory writes could admit schema-less one-off signals as provisional self memory. The fix requires a self schema or explicit operator/user instruction before requested self writes can proceed.

The eval harness intentionally uses temporary directories by default. It does not write to the developer's real `.nest`.
