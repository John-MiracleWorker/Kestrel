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

Upcoming milestone

- Phase 2 agent-kernel extraction, then frontend state-machine cleanup and golden-path browser coverage.
