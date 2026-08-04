# Wildflower Workbench e2e snapshots

This directory documents the visual-baseline policy for the
installed-renderer e2e suite (`desktop/e2e/workbench.spec.ts`).

## Baseline policy

Committed baselines are **DOM/style assertions with a documented
tolerance, not raw platform screenshots**. Raw PNGs are platform-fragile
(font rasterization, subpixel AA, and GPU differences between macOS and
Linux runners), so no screenshot bytes are committed to the repository.
Instead each journey asserts:

- exactly one `<main>` landmark is mounted;
- `body` `overflow-x` is not `scroll` and
  `document.body.scrollWidth <= document.documentElement.clientWidth`
  (zero-tolerance horizontal-overflow contract at both 1440x960 and
  960x720);
- the expected destination chrome (nav rail link, Review/Start mission
  buttons, approval digest text) is visible;
- fonts have finished loading before any assertion that depends on
  layout (`document.fonts.status === "loaded"`);
- motion is disabled through the **supported reduced-motion setting**
  (`kestrel.motion.preference.v1 = "reduce"` in localStorage, applied by
  `web/src/design/theme.ts`) — never by injecting arbitrary CSS into the
  production page.

Tolerance: horizontal-overflow and landmark assertions are exact;
visibility assertions use Playwright's default actionability tolerance
(auto-wait up to the 15s expect timeout).

Per-run screenshots are still captured for human inspection but are
written to Playwright's gitignored output directory
(`desktop/test-results/`), never committed.

## Deterministic Demo fixtures

All API traffic is served from `desktop/e2e/fixtures.ts` (a browser-context
port of `web/src/testing/apiFixtures.ts`): the bundled Demo/mock provider,
deterministic project/run/approval payloads, and a fetch stub that throws
on any unmocked or mutating request. No live provider is ever contacted.
Electron runs use a temporary owner-data directory
(`KESTREL_E2E_OWNER_DATA`), never the real profile.

## Executed vs. scaffolded (2026-07-31, Task 14)

- **Executed locally (macOS 26.5, main drive `~/kestrel-e2e`):** the
  browser-context journeys against the built `web/dist` renderer — **16/16
  passing**. Keyboard mission journey at 1440x960 and 960x720 (objective →
  Review → reopen context rail at compact width → Start → "The mission is
  queued."); light/dark Mission captures; Setup, Settings, Flock
  placeholder, approval-queue exact-call digest, and route recovery at both
  widths.
- **Scaffolded for CI:** the GitHub Actions `desktop` job
  (`.github/workflows/ci.yml`) runs the same `npm run e2e` command after
  building `web/dist` and installing the pinned Chromium browser.
- **Electron-bundle mode:** `KESTREL_E2E_BUNDLE=<path-to-bundle>` is the
  documented switch for launching the built Electron directory bundle
  against a temporary owner-data directory. Electron launch on the
  authoring host was not executed in the Task 14 budget; the browser
  context renders the identical built assets. Do not claim an Electron
  run that was not executed.
