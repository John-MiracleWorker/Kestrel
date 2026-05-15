# Codex Build Instructions

You are building a complete nested-learning agent runtime, not only a memory library.

Primary handoff file: `docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md`.

Non-negotiables:

1. Use Memvid v2 `.mv2` files only. Do not implement QR/video-frame v1 behavior.
2. Keep one `.mv2` file per nested memory layer unless a test proves a better layout.
3. Never call `create(path)` on an existing `.mv2` file.
4. The agent must be conversational from CLI before any optional UI work starts.
5. The mock backend and mock LLM must keep tests deterministic.
6. No policy memory writes from a single ordinary event.
7. High-risk tools require explicit config enablement and, later, interactive approval.
8. Every memory promotion needs evidence, provenance, confidence, and validation status.
9. Run `pytest -q` after each phase.
10. Add Memvid integration tests behind `RUN_MEMVID_INTEGRATION=1`.
