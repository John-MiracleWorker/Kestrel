# Memvid Integration Plan

## Current Memvid mental model

Use Memvid v2 `.mv2` files. Do not build against QR-code or video-frame Memvid v1 behavior.

The scaffold uses `MemvidBackend` as a thin adapter around `memvid_sdk`:

- `create(path, enable_vec=True, enable_lex=True)` for missing files.
- `use("basic", path)` for existing files.
- `put(title, label, metadata, text=..., uri=..., track=..., kind=...)` to store records.
- `find(query, mode=..., k=..., scope="track:<layer>", adaptive=True, ...)` to retrieve.
- `seal()` to commit.
- `verify(deep=True)` to validate file integrity.

Codex should verify these calls against the installed SDK and update the adapter if names differ.

## Storage policy

Use one `.mv2` file per nested layer:

```text
working.mv2
episodic.mv2
semantic.mv2
procedural.mv2
policy.mv2
```

Use `track=<layer>` and `label=<layer>` even inside per-layer files. This makes future merge/export easier.

## Metadata contract

Every Memvid frame should include:

```json
{
  "id": "mem_xxx",
  "nested_layer": "semantic",
  "nested_kind": "fact",
  "nested_confidence": 0.91,
  "nested_importance": 0.75,
  "content_hash": "sha256...",
  "evidence": [
    {"source": "terminal", "locator": "run_001", "quote": "..."}
  ],
  "created_at": "2026-05-15T...Z",
  "updated_at": "2026-05-15T...Z"
}
```

## Search modes

Use layer-specific defaults:

- Working: `lex`, because active state often contains exact errors and paths.
- Episodic: `auto`, because failures/events need both exact and semantic recall.
- Semantic: `auto`.
- Procedural: `auto`.
- Policy: `lex`, because policy wording and exact constraints matter.

## Adaptive retrieval

Use adaptive retrieval for open-ended recall. Disable adaptive retrieval only when the context compiler requires a fixed number of chunks for a deterministic budget test.

## Memory cards / Logic Mesh

Use Memory Cards for stable entities:

- repo → framework
- service → dependency
- user → preference
- agent → provider auth profile
- procedure → required validation

For codebase understanding, Logic Mesh can map relationships such as:

- module depends_on package
- endpoint uses service
- agent uses provider
- provider requires credential

## ACL/security

For single-user local dev, ACL can be disabled. For team/enterprise use:

- Store tenant and role metadata on every frame.
- Query with ACL enforcement.
- Encrypt policy or sensitive project capsules to `.mv2e` when needed.

## Validation hooks

After each write batch:

1. `seal()` all changed capsules.
2. `verify(deep=True)` each changed capsule.
3. Run golden retrieval questions.
4. Run context compiler budget tests.
5. Replay recorded sessions when debugging retrieval drift.

## Open tasks for Codex

1. Install `memvid-sdk` and run the integration smoke test.
2. Adjust import paths if the current SDK exposes `memvid` instead of `memvid_sdk`.
3. Confirm exact shape of `find()` results.
4. Add a skipped pytest integration test that unskips when `RUN_MEMVID_INTEGRATION=1`.
5. Add CLI commands for `doctor`, `enrich`, `session_start`, `session_replay`, and `correct`.
6. Add optional Memory Card extraction for semantic/procedural layers.
