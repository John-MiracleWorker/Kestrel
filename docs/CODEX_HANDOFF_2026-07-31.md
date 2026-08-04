# Handoff — Wildflower Workbench Complete (Tasks 1–14)

**Date:** 2026-07-31
**From:** Hermes (Claude, via Kimi relay)
**To:** Codex
**Branch:** `feat/gui-first-kestrel-desktop`
**HEAD:** `9eef69c7364bfe21e6a4a936fda819c79a4d419e` — clean working tree
**Worktree:** `/Volumes/12.45/Codex-Offload/kestrel-gui-first-integration`

---

## 1. Where we are

The **Wildflower Workbench milestone (Tasks 1–14)** from
`docs/superpowers/plans/2026-07-29-wildflower-workbench.md` is **fully complete**.
Tasks 1–7 were Codex's work (already committed); **Tasks 8–14 were completed in
this session** and each committed green after full gates.

| Task | Commit | What |
|---|---|---|
| 8 | `a9116896` | Mission as the primary task surface (context rail, rail collapse/reopen, approvals migration) |
| 9 | `212d8165` | Projects + Memory workspaces (project authority profiles, index freshness, promotion history, behavior deltas) |
| 10 | `90da3e0f` | Automate + Extend workspaces (routines, channels, capability controls, MCP/skills/plugins panels; LegacyWorkbench slimmed ~600 lines) |
| 11 | `5766a286` | Server-side effective settings projection (`effective_settings.py`, `/api/settings` GET + revision-checked PUT, CAS conflict payload) |
| 12 | `e6056ce6` | Searchable Settings workspace (settings index, command-palette deep links) |
| 13 | `397a0167` | Accessibility gates (axe, keyboard journeys, reduced-motion token gate, contrast; dark `--selected` fix) |
| 14 | `9eef69c7` | Rendered-validation e2e (Playwright harness, deterministic Demo fixtures, keyboard + destination journeys) |

**Test tallies at HEAD:** web suite 47 files / 324 tests; a11y suites 25/25; desktop
e2e 16/16; Python full suite 2663 passed / 83 skipped (minus the 3 environmental
process/supervisor files — see §4).

**SDD ledger:** `.superpowers/sdd/2026-07-31-wildflower-workbench/progress.md`
(**gitignored — never `git add` it**) has per-task RED/GREEN evidence and commit SHAs
for Tasks 8–14.

---

## 2. What was done this session (Tasks 8–14)

Process: strict TDD relay — failing test first, smallest trust-preserving repair,
never weaken a gate, never fake a run. Each task ended only after all gates green
(full web suite + build + licenses, Python suite, `git diff --check`), then a single
commit. Renderer is never an authority source; honesty over coverage theater.

Key fixes worth knowing about (all committed):

- **Task 8:** `preflightPending` wedge fixed at component level; dark-theme contrast.
- **Task 10:** DOM-order test ambiguity — after extracting `McpPanel`, ambiguous
  `getAllByLabelText("Arguments JSON")[0]` hit the MCP form; fixed by scoping with
  `id="connected-tools"` (LegacyWorkbench.tsx:2423) + `id="mcp-tool-invoke"` (McpPanel.tsx:196)
  and `within(...)` queries. **Pattern for the future:** after extracting a panel,
  scope ambiguous queries with `within(region)`.
- **Task 11:** `PUT /api/settings/{id}` has **no `require_owner_api()` gate** — this
  matches existing `PUT /api/runtime/settings` behavior but was **flagged for later
  security review** (see §5).
- **Task 13:** dark `--selected` token (#9fb937 → #5a6c14) for AA contrast; migrated
  ~18 hardcoded transition durations to `--motion-*` tokens; documented the
  keyframe-loop allowlist (spinner/skeleton/typing) in the reduced-motion gate.
- **Task 14:** `web/vite.config.ts` gained `base: "./"` so the built renderer boots
  from `file://` (installed Electron shell) and from the e2e harness. Electron-bundle
  launch is **scaffolded** via `KESTREL_E2E_BUNDLE`, **not executed** — the
  browser-context e2e runs the identical built `web/dist` assets over loopback
  (`vite preview`). Documented honestly in `desktop/e2e/snapshots/README.md`.

---

## 3. What's still required (next phases)

In plan order. The **Wildflower Workbench is done**; what follows is the
**post-milestone roadmap** — start here:

1. **LAN model discovery (schema v3)** — `docs/superpowers/plans/2026-07-29-explicit-lan-model-discovery.md`
2. **Adaptive Flock qualification (schema v4)** — `docs/superpowers/plans/2026-07-29-adaptive-flock-qualification-activation.md`
3. **Cross-platform packaging / update / rollback** — `docs/superpowers/plans/2026-07-29-desktop-packaging-updates-recovery.md`
4. **Final integrated qualification.**

Related context plans: `2026-07-29-gui-first-flock-program-index.md`,
`2026-07-29-gui-first-desktop-foundation.md`, `2026-07-28-everyday-kestrel-v05.md`.

**Task 14 follow-through for CI:** the `desktop` job in `.github/workflows/ci.yml`
now builds `web/dist`, installs pinned Chromium, and runs `npm run e2e`. The
Electron-bundle leg (`KESTREL_E2E_BUNDLE`) is scaffolded — wire a real built
directory bundle in when the packaging phase produces one.

---

## 4. Known pre-existing issues (NOT caused by this work — do not chase)

- **Packet-digest backend defect (documented, unfixed):** `run_manager.create_approval_packet`
  passes `tool_resource_digest` as `"sha256:"+hex` (`run_manager.py:1896`, `:2483`),
  but the packet store `_digest()` (`engineering/approval_packets.py:554`) accepts
  bare `[0-9a-f]{64}` only → route `server_engineering_routes.py:369` would 409.
  Canonical runtime shape IS prefixed (`tests/test_capability_control_plane.py:575`).
  Zero tests exercise the full path. **Backend authority-binding change — deserves its
  own task**, not a frontend fix.
- **Environment-sensitive supervisor/process tests (4):** `tests/test_install_script.py`
  (3: `test_failed_server_health_stops_tracked_supervisor_and_child`,
  `test_failed_child_pid_publication_still_cleans_via_supervisor_identity`,
  `test_failed_health_kills_term_ignoring_server_and_grandchild_group`) and
  `tests/test_service_control.py` (1: `test_system_process_inspector_and_signaler_touch_only_test_owned_process`).
  Verified failing on a clean baseline on **both** the external volume and the main
  drive — genuine process-group/supervisor timing sensitivity on this host, unrelated
  to any task diff. Run the suite minus these three files: **2663 passed, 83 skipped**.

---

## 5. Flags for later review

- **`PUT /api/settings/{id}` auth symmetry** (Task 11) — no `require_owner_api()` gate,
  matching existing `PUT /api/runtime/settings`. Decide whether both should be gated.
- **Vite chunk-size warning** on `web` build (>500 kB chunk) — pre-existing; consider
  `manualChunks` when convenient.
- **Starlette/httpx deprecation warnings** in Python test output — pre-existing.

---

## 6. Environment / operational notes

- **Never touch `/Users/tiuni/kestrel`** (separate dirty user-owned worktree). The
  authoritative tree is `/Volumes/12.45/Codex-Offload/kestrel-gui-first-integration`.
- **Never kill/restart the launchd `ai.kestrel.daemon`** — it holds the primary repo
  state-DB lock.
- **Gates:** web `cd web && npm test -- --run` (runs `tsc -b` first) + `npm run build`
  + `npm run licenses:check`; Python `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`
  from repo root (use `.venv`, not `uv`); `git diff --check`. Desktop e2e:
  `cd desktop && npm run e2e` (needs `web/dist` built; Chromium via `npx playwright install chromium`).
- **Direct vitest:** `NODE_ENV=test npx vitest run --environment jsdom <paths>`; use
  `fireEvent` (`@testing-library/user-event` not installed).
- **Playwright on this machine:** Homebrew wrapper; run drivers with
  `/opt/homebrew/opt/python@3.11/bin/python3.11`. Browsers cached at `~/Library/Caches/ms-playwright`.
- **Env PATH for agents:**
  `export PATH="$PWD/.venv/bin:/Users/tiuni/.nvm/versions/node/v22.16.0/bin:/Users/tiuni/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"`
