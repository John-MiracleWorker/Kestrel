/**
 * Playwright configuration for the Wildflower Workbench installed-renderer
 * e2e suite.
 *
 * The suite drives the exact built renderer (`web/dist`) in Chromium with
 * deterministic Demo fixtures (`e2e/fixtures.ts`). When the
 * `KESTREL_E2E_BUNDLE` environment variable names a built Electron
 * directory bundle, the same journeys run against the installed Electron
 * renderer via `_electron.launch`; otherwise they run in a plain browser
 * context against the same built assets so the visual/narrow-window
 * contracts stay executable in CI without an Electron download.
 */
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  // Serve the built renderer over loopback http: ES-module subresource fetches
  // are CORS-blocked from a file:// origin, and the runtime transport
  // deliberately refuses cross-origin API calls from a file: page. Only the
  // fixture-stubbed API surface answers; any unstubbed request fails the test.
  webServer: {
    command:
      "./node_modules/.bin/vite preview --host 127.0.0.1 --port 48765 --strictPort",
    cwd: "../web",
    url: "http://127.0.0.1:48765/",
    reuseExistingServer: false,
    timeout: 30_000,
  },
  use: {
    baseURL: "http://127.0.0.1:48765",
    viewport: { width: 1440, height: 960 },
    // Capture only; baselines are DOM/style assertions (see
    // e2e/snapshots/README.md), never raw platform screenshots.
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
