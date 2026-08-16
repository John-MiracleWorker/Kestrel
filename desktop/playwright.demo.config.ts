/**
 * Playwright config for the narrated demo video capture.
 * Same harness as playwright.config.ts but with video recording on.
 */
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "demo.spec.ts",
  timeout: 420_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "./demo-output",
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
    viewport: { width: 1440, height: 900 },
    video: { mode: "on", size: { width: 1440, height: 900 } },
    screenshot: "off",
    trace: "off",
  },
});
