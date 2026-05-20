# Codex Memory Handoff Prompt

Last updated: 2026-05-20

This is the older memory-subsystem handoff. The current primary handoff for full-agent work is `docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md`.

Use this file only when the task is specifically about Kestrel's nested memory subsystem. For the current repo state, also read:

```text
README.md
docs/IMPLEMENTATION_STATUS.md
docs/MEMVID_INTEGRATION.md
docs/MV2_CONTEXT_PACKING.md
docs/NESTED_LEARNING_MODEL.md
docs/TASK_CAPSULES.md
```

## Current Memory Contract

- Use Memvid v2 `.mv2` files as the primary persistent memory backend.
- Keep one permanent `.mv2` file per layer: working, episodic, semantic, procedural, self, and policy.
- Keep run-scoped `complete.mv2` capsules separate from permanent layers.
- Never call `create(path)` on an existing `.mv2` file.
- Do not implement Memvid v1 QR/video-frame behavior.
- Do not replace `.mv2` memory with SQLite, Postgres, Chroma, FAISS, or JSON.
- SQLite is control-plane state only.
- Preserve evidence, provenance, confidence, validation status, and promotion gate metadata on learning decisions.
- Do not write policy memory from a single ordinary event.

## Current Validation

Fast path:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "hello"
```

Memvid path:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

## Current Memory Tools

- `memory.search`
- `memory.write`
- `memory.consolidate`
- `memory.learn`
- `memory.conflicts`
- `memory.inspect`
- `memory.export`
- `memory.import`
- `context.pack`
- `context.expand`
- `memvid.verify`
- `memvid.doctor`
- `memvid.stats`
- `capsule.summarize`
- `capsule.apply`

`memory.import` and `capsule.apply` are high-risk approval-gated paths. Policy writes remain separately gated by config and validation rules.

## Remaining Memory Hardening

- Stronger correction/tombstone lifecycle.
- Better review UI for proposed learning signals.
- More extensive Memvid SDK upgrade tests.
- Optional encrypted `.mv2e` guidance if that becomes a runtime target.
- Longer-running retrieval drift and consolidation regression suites.
