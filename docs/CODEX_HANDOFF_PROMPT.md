# Codex Handoff Prompt

You are implementing a buildable Nested Learning-inspired agent memory system using Memvid `.mv2` files as the storage layer.

## Repository goal

Turn this scaffold into a production-grade memory subsystem for an agent like Kestrel. The system must replace flat agentic RAG with nested memory layers, fast local recall, strict validation, and a context compiler that produces compact LLM-ready cognitive state.

## Important facts from research

- Memvid v2 uses `.mv2` binary memory files. Do **not** build against old QR/video-frame Memvid v1 behavior.
- Memvid supports single-file memory capsules, lexical BM25, optional vector search, time index, embedded WAL, Memory Cards, enrichment, session replay, corrections, verification/doctor, and ACL metadata.
- The safest architecture is one `.mv2` file per nested memory layer: `working.mv2`, `episodic.mv2`, `semantic.mv2`, `procedural.mv2`, `policy.mv2`.
- `create()` may overwrite existing files. Always check existence and use `use("basic", path)` for existing files.

## Implementation order

### Step 1: Baseline

Run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pytest
ruff check .
mypy src
```

Fix any scaffold issues before changing behavior.

### Step 2: Harden Memvid backend

Open `src/nested_memvid_agent/backends/memvid_backend.py`.

Tasks:

1. Install `memvid-sdk`.
2. Confirm the correct import path: likely `from memvid_sdk import create, use`, but adjust if installed SDK differs.
3. Confirm `create(path, enable_vec=True, enable_lex=True)` works.
4. Confirm `use("basic", path)` works for existing files.
5. Confirm `mem.put(title, label, metadata, text=..., uri=..., track=..., kind=...)` works.
6. Confirm `mem.find(query, mode="auto", k=..., scope="track:<layer>", adaptive=True, ...)` result shape.
7. Confirm `seal()`, `verify(deep=True)`, and `close()` behavior.
8. Preserve the current backend contract: `open`, `put`, `find`, `seal`, `verify`, `close`.

Add an integration test:

```text
tests/integration/test_memvid_backend.py
```

The test must be skipped unless `RUN_MEMVID_INTEGRATION=1` is set.

Acceptance:

```bash
RUN_MEMVID_INTEGRATION=1 pytest tests/integration/test_memvid_backend.py
```

must create a temp `.mv2`, write one record, seal, verify, retrieve it, and close cleanly.

### Step 3: Add golden validation loader

Create:

```text
golden/retrieval.example.json
src/nested_memvid_agent/golden.py
```

JSON format:

```json
[
  {
    "name": "auth profile failure",
    "query": "provider specific auth startup failure",
    "expected_terms": ["auth profile", "provider"],
    "layers": ["episodic", "semantic", "procedural"],
    "min_hits": 1
  }
]
```

Add CLI command:

```bash
nested-memvid validate --backend memvid --golden golden/retrieval.example.json
```

Acceptance:

- Valid JSON loads into `GoldenQuestion` objects.
- Missing expected terms fail with a useful reason.
- Passing cases print a summary.

### Step 4: Add memory extraction pipeline

Create:

```text
src/nested_memvid_agent/extraction.py
```

Implement deterministic candidate extraction first. Do not require an LLM yet.

Inputs:

- user message,
- tool output,
- test result,
- code patch summary,
- final answer.

Outputs:

- `MemoryRecord` candidates with layer, kind, confidence, importance, evidence.

Rules:

- Error logs and failed tests → working/episodic failure.
- User stable preferences → semantic fact/preference candidate.
- Repeated successful fix → procedural candidate.
- Global behavior rule → policy candidate only if confidence >= 0.95 and source says explicit user instruction or repeated validated rule.

Acceptance:

- Unit tests for extraction rules.
- No policy candidate from a single ordinary tool result.

### Step 5: Add consolidation transaction log

Create:

```text
src/nested_memvid_agent/consolidation_log.py
```

Every promotion attempt should log:

- source id,
- source layer,
- target layer,
- validation score,
- repeat count,
- reason,
- accepted/rejected,
- timestamp.

Acceptance:

- Promotions are auditable.
- Failed promotions do not write target memory.

### Step 6: Add Memvid corrections

Extend backend interface with optional `correct()`:

```python
def correct(self, statement: str, source: str, topics: list[str], boost: float = 2.0) -> str: ...
```

For backends that do not support it, store correction as `MemoryKind.CORRECTION`.

Acceptance:

- Correction retrieval outranks stale memories in tests.
- Memvid backend calls `mem.correct(...)` if available.

### Step 7: Add Memory Cards and Logic Mesh hooks

Add backend methods where available:

- `add_memory_cards(cards)`
- `state(entity)`
- `enrich(engine="rules")`
- `traverse(...)`

Do not make core runtime depend on them. They are acceleration hooks for stable semantic/project memory.

Acceptance:

- Semantic/project facts can be queried by entity state when Memvid supports it.
- Fallback path still works with plain retrieval.

### Step 8: Add context compiler conflict detection

If two high-confidence hits disagree, the compiler should flag conflict instead of smoothing it over.

Implement a simple first pass:

- same title or same `metadata["entity"]`, conflicting content hashes, both confidence > 0.8 → conflict warning.

Acceptance:

- Unit test creates two conflicting semantic facts.
- Compiled prompt contains `CONFLICT WARNING`.

### Step 9: Add performance harness

Create:

```text
scripts/benchmark_retrieval.py
```

Measure:

- per-layer retrieval latency,
- total compile latency,
- hits per layer,
- prompt chars,
- cold start vs warm start.

Acceptance:

- Prints JSON metrics.
- Does not require an LLM.

### Step 10: Ship CLI workflow

CLI should support:

```bash
nested-memvid init --backend memvid --memory-dir ./memory
nested-memvid put --backend memvid --layer semantic --title "..." --text "..."
nested-memvid search --backend memvid --query "..."
nested-memvid compile-context --backend memvid --objective "..."
nested-memvid validate --backend memvid --golden golden/retrieval.example.json
nested-memvid doctor --backend memvid
nested-memvid enrich --backend memvid --layer semantic --engine rules
```

Acceptance:

- All commands work with `--backend memory`.
- Memvid commands work after installing `memvid-sdk`.
- Commands never overwrite existing `.mv2` files accidentally.

## Non-negotiable guardrails

- Do not use policy memory as a dumping ground.
- Do not promote without evidence.
- Do not remove provenance.
- Do not invent Memvid API behavior; verify against the installed SDK.
- Do not silently overwrite `.mv2` files.
- Do not require cloud APIs for unit tests.
- Keep the in-memory backend passing so development remains fast.

## Final acceptance checklist

```bash
pytest
ruff check .
mypy src
python scripts/bootstrap_memory.py --backend memory --memory-dir ./memory
python scripts/run_validation.py --backend memory --memory-dir ./memory
nested-memvid compile-context --backend memory --objective "Explain nested memory promotion"
```

If Memvid is installed:

```bash
python scripts/bootstrap_memory.py --backend memvid --memory-dir ./memory
python scripts/run_validation.py --backend memvid --memory-dir ./memory
RUN_MEMVID_INTEGRATION=1 pytest tests/integration/test_memvid_backend.py
```

When all of that passes, the project is ready for agent integration.
