# Kestrel Recovery Progress

## Milestone 0: Truth pass

Status: Completed

Completed

- Added a real `typecheck` script for `@kestrel/web`.
- Updated CI to stop pretending TypeScript lint is a required gate.
- Normalized the default gateway port to `8741` in tracked startup examples and web dev/Tauri client config.
- Added `KESTREL_FEATURE_MODE=core` to tracked startup examples.
- Fixed root repository metadata placeholders.
- Updated the CLI default API URL and local developer test helpers to use `8741`.

Verification

- `npm run typecheck -w @kestrel/web`
- `npm run typecheck -w @kestrel/gateway`

## Milestone 1: Core isolation foundation

Status: Completed

Completed

- Added `KESTREL_FEATURE_MODE` support with `core`, `ops`, and `labs`.
- Introduced a Brain composition root in `packages/brain/app.py` and reduced `packages/brain/server.py` to a thin entrypoint shim.
- Extended the runtime context with feature-mode and startup metadata.
- Added task-profile and tool-bundle policy helpers.
- Made chat, task, and automation entrypoints build filtered registries from the active mode and task profile.
- Gated council and simulation-style behaviors away from Core mode.
- Exposed feature mode, execution runtime, and enabled bundle metadata through the capabilities surface.
- Added focused Brain tests for mode policy and task-profile filtering.
- Regenerated stale `_generated` Brain stubs so they once again match `brain.proto`.

Verification

- `pytest packages/brain/tests/test_feature_mode_policy.py packages/brain/tests/test_task_profiles.py -q`
- `python3 -m py_compile packages/brain/app.py packages/brain/core/feature_mode.py packages/brain/core/runtime.py packages/brain/agent/task_profiles.py packages/brain/agent/loop.py packages/brain/services/agent_service.py packages/brain/services/chat_service.py packages/brain/services/system_service.py packages/brain/core/cron.py packages/brain/agent/tools/__init__.py packages/brain/server.py packages/cli/kestrel.py`

## Milestone 2: Architecture isolation and truthfulness

Status: Completed

### What changed

- **Feature-mode default parity**: `get_feature_mode()` now defaults to CORE (not OPS) when `KESTREL_FEATURE_MODE` is unset, matching all documented startup examples.
- **Real mode isolation in tool registration**: `build_tool_registry()` no longer imports all tool modules unconditionally. Three internal functions (`_register_core_tools`, `_register_ops_tools`, `_register_labs_tools`) are called conditionally based on the active feature mode. Labs-only modules (computer_use, media_gen, daemon_control, etc.) are never imported in CORE or OPS mode.
- **chat_service.py decomposition**: Broke the 708-line `ChatServicerMixin` into focused modules:
  - `request_context.py` — provider/model/API key resolution
  - `task_factory.py` — AgentTask creation, guardrails, complexity detection
  - `model_resolution.py` — ModelRouter with workspace-aware provider probing
  - `stream_coordinator.py` — agent loop background task and response streaming
  - `post_response_hooks.py` — message persistence, RAG embedding, persona learning, memory graph

### Tests added/updated

- `test_feature_mode_policy.py`: Renamed `test_feature_mode_defaults_to_ops` → `test_feature_mode_defaults_to_core`. Added `test_parse_feature_mode_invalid_returns_core`, `test_parse_feature_mode_explicit_values`, `test_get_feature_mode_explicit_env`.
- `test_tool_registration.py` (new): `test_core_mode_skips_ops_and_labs_imports`, `test_ops_mode_registers_core_and_ops`, `test_labs_mode_registers_all_tiers`, `test_bundle_filtering_preserved_after_mode_registration`.

### Risks introduced

- **Default mode change**: Deployments relying on the implicit OPS default without setting `KESTREL_FEATURE_MODE` will now get CORE. This is intentional and matches all documented behavior.
- **Import-time side effects**: If any ops/labs tool module had import-time side effects, the conditional import could surface latent bugs. Mitigated by the fact that imports were already lazy (inside `build_tool_registry()`).
- **chat_service decomposition**: Behavior-preserving refactor. The agent loop wiring section remains inline in the orchestrator to avoid parameter explosion.

### Verification

```
pytest packages/brain/tests/test_feature_mode_policy.py packages/brain/tests/test_task_profiles.py packages/brain/tests/test_tool_registration.py -q
python3 -m py_compile packages/brain/core/feature_mode.py packages/brain/agent/tools/__init__.py packages/brain/services/chat_service.py packages/brain/services/request_context.py packages/brain/services/task_factory.py packages/brain/services/stream_coordinator.py packages/brain/services/model_resolution.py packages/brain/services/post_response_hooks.py
```

## Phase 3 targets

- Agent-kernel extraction: separate the AgentLoop's planning, execution, and reflection phases into distinct modules.
- Frontend state-machine cleanup and golden-path browser coverage.
- Integration test coverage for the chat service pipeline (end-to-end with mocked providers).
- Evaluate whether the remaining inline agent loop wiring in `StreamChat` warrants extraction once the agent kernel is cleaner.
