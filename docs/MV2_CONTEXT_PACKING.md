# MV2 Context Packing

Kestrel uses Memvid v2 `.mv2` files as the durable memory substrate. Context packing does not create infinite context and does not change model weights. It creates a pseudo-context window by retrieving relevant `.mv2` memories, compressing them into structured context frames, selecting the most useful frames, and packing them under a token budget.

## Context Frames

`MV2ContextFrame` is the structured shape used for memory-backed context:

- `raw_chunk`: exact source text, tool output, code excerpt, or raw evidence.
- `section_summary`, `task_summary`, `session_summary`: compact summaries that point back to raw chunks.
- `skill_card`: reusable procedural recipe.
- `failure_note`: validated failure or pitfall.
- `correction`: memory correction that should override stale assumptions.
- `conflict_set`: explicit conflict grouping.
- `trace_stub`: compact pointer to longer audit or optimizer trace data.

Every frame carries layer, kind, parent/child ids, source URI/span, content hash, token estimate, confidence, importance, provenance, and validation metadata. Summaries should keep parent/child pointers to raw chunks so exact evidence can be expanded later.

## Packing Order

The packer prefers memory layers in this order:

1. policy
2. procedural
3. semantic
4. episodic
5. working

Within those layers, summaries are preferred before raw chunks. Raw chunks are expanded only when the request asks for it, confidence is low, the item is a correction/failure/conflict, or exact evidence/code is required.

## Prompt Shape

The packed prompt contains:

- Current objective
- Hard policy constraints
- Relevant procedures
- Stable facts
- Recent episodic/task state
- Working memory
- Conflict warnings
- Evidence pointers
- Retrieval telemetry
- Next-step instruction

The packer deduplicates by content hash and high token overlap. It warns on `conflict_group_id` matches and simple high-confidence polarity disagreements instead of silently merging memories.

## Tools and API

Built-in tools:

- `context.pack`: build a bounded pseudo-context window.
- `context.expand`: expand a specific frame/record into raw evidence.
- `memory.conflicts`: search for conflicting memories around a claim.

API routes:

- `POST /api/context/pack`
- `POST /api/context/expand`
- `GET /api/memory/conflicts`

SQLite remains control-plane storage only. It may track runs, approvals, MCP servers, subagents, and UI state, but retrieval memory remains `.mv2` layer files.
