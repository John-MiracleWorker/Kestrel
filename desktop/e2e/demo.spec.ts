/**
 * Kestrel UI demo — narrated walkthrough (video-capture spec).
 *
 * Full owner journey at narration pace: Mission -> approval -> Setup ->
 * LAN discovery -> Flock qualification ($50 draft -> start -> pause/resume
 * -> receipt) -> activation -> learned route -> revoke -> static fallback.
 * All responses come from the deterministic Demo fixtures — the same
 * authority as the CI suites. No live provider, network, or credential.
 *
 * Run: npx playwright test demo --config playwright.demo.config.ts
 */
import { expect, test, type Page } from "@playwright/test";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { demoFixtureInitScript, lanDiscoveryFixtureInitScript, flockQualificationFixtureInitScript } from "./fixtures";

const here = dirname(fileURLToPath(import.meta.url));
const indexPath = resolve(here, "../../web/dist/index.html");

test.skip(
  !existsSync(indexPath),
  "web/dist is not built; run `npm --prefix web run build` first",
);

const VIEWPORT = { width: 1440, height: 900 } as const;

async function beat(page: Page, ms: number): Promise<void> {
  await page.waitForTimeout(ms);
}

async function fontsReady(page: Page): Promise<void> {
  await page.waitForFunction(() => document.fonts.status === "loaded");
}

async function nav(page: Page, label: string): Promise<void> {
  await page.getByRole("link", { name: label, exact: true }).first().click();
  await beat(page, 1200);
}

test("Kestrel narrated product demo", async ({ page }) => {
  test.setTimeout(420_000);
  await page.setViewportSize(VIEWPORT);

  /* ACT 1 — Cold open: the shell. */
  await page.addInitScript(demoFixtureInitScript());
  await page.goto("/");
  await expect(page.locator("main")).toHaveCount(1);
  await fontsReady(page);
  await beat(page, 4000);

  /* ACT 2 — Mission: type, review, launch. */
  await nav(page, "Mission");
  await beat(page, 2500);
  const objective = page.getByLabel("Objective", { exact: true });
  await objective.click();
  await page.keyboard.type("Summarize this project", { delay: 50 });
  await expect(objective).toHaveValue("Summarize this project");
  await beat(page, 1800);
  await page.getByRole("button", { name: "Review mission" }).click();
  await beat(page, 3500);
  const startButton = page.getByRole("button", { name: "Start mission" });
  await expect(startButton).toBeEnabled({ timeout: 20_000 });
  await beat(page, 1200);
  await startButton.click();
  await expect(page.getByText("The mission is queued.")).toBeVisible();
  await beat(page, 3000);

  /* ACT 3 — Approval queue: exact-call control. */
  await page.addInitScript(demoFixtureInitScript({ mode: "approval-pending" }));
  await page.goto("/");
  await fontsReady(page);
  await nav(page, "Mission");
  await page.getByRole("button", { name: /Summarize this project/ }).first().click();
  await beat(page, 4000);

  /* ACT 4 — Settings + Setup Center. */
  await nav(page, "Settings");
  await beat(page, 1800);
  await page.getByRole("button", { name: "Setup Center" }).first().click();
  await beat(page, 3200);

  /* ACT 5 — Flock page: LAN discovery.
     Fresh page with demo + LAN fixtures installed before goto. */
  const lan = await page.context().newPage();
  await lan.setViewportSize(VIEWPORT);
  await lan.addInitScript(demoFixtureInitScript());
  await lan.addInitScript(lanDiscoveryFixtureInitScript());
  await lan.goto("/#/flock/lan");
  await fontsReady(lan);
  await beat(lan, 2000);
  const scanButton = lan.getByRole("button", { name: /scan/i }).first();
  if (await scanButton.count()) {
    await scanButton.click();
    await beat(lan, 3500);
  }
  await beat(lan, 1500);

  /* ACT 6 — Flock qualification journey: fresh page, demo + flock fixtures. */
  await lan.close();
  const qual = await page.context().newPage();
  await qual.setViewportSize(VIEWPORT);
  await qual.addInitScript(demoFixtureInitScript());
  await qual.addInitScript(flockQualificationFixtureInitScript());
  await qual.goto("/#/flock/qualification");
  await fontsReady(qual);
  await expect(
    qual.getByRole("heading", { name: "Adaptive Flock qualification" }),
  ).toBeVisible();
  await beat(qual, 2500);

  const capField = qual.getByLabel("Maximum provider spend");
  await expect(capField).toHaveValue("50.00");
  await beat(qual, 1500);
  const ceilingField = qual.getByLabel("Per-attempt cost ceiling");
  await ceilingField.fill("7.50");
  await beat(qual, 1200);

  await qual.getByRole("button", { name: "Refresh preview" }).click();
  await expect(qual.getByRole("heading", { name: "Target matrix" })).toBeVisible();
  await beat(qual, 4000);

  await qual.getByRole("checkbox", { name: /I have reviewed the preview/ }).check();
  await beat(qual, 800);
  await qual.getByRole("button", { name: "Create and start qualification" }).click();
  await expect(qual.locator(".banner.success")).toContainText(
    "Qualification started for 1 scope(s)",
  );
  await beat(qual, 2500);

  const progress = qual.locator(".qual-progress");
  await expect(progress.locator(".badge").first()).toContainText("running");
  await beat(qual, 1500);
  await progress.getByRole("button", { name: "Pause" }).click();
  await expect(progress.locator(".badge").first()).toContainText("paused");
  await beat(qual, 1800);
  await progress.getByRole("button", { name: "Resume" }).click();
  await expect(progress.locator(".badge").first()).toContainText("running");
  await beat(qual, 1500);

  const results = qual.locator(".qual-results");
  await expect(results.getByText("Evidence collection completed")).toBeVisible({
    timeout: 30_000,
  });
  await beat(qual, 2000);
  await expect(results.getByText("1 scope qualified")).toBeVisible();
  await expect(results.getByText("Guardrail violations: 0")).toBeVisible();
  await beat(qual, 4000);

  /* ACT 7 — Activation. */
  await qual.goto("/#/flock/activations");
  await fontsReady(qual);
  await expect(qual.getByRole("heading", { name: "Flock activations" })).toBeVisible();
  await beat(qual, 2000);
  await qual.getByLabel("Qualification receipt ID").fill("rcpt_" + "c".repeat(24));
  await qual.getByLabel("Scope digests").fill("1".repeat(64));
  await qual.getByRole("button", { name: "Preview activation" }).click();
  await expect(qual.getByRole("heading", { name: "Activation packet" })).toBeVisible();
  await beat(qual, 3500);
  await qual.getByRole("checkbox", { name: "Scope code_repair qualified" }).check();
  await qual.getByRole("checkbox", { name: /I understand this activation grants/ }).check();
  await beat(qual, 800);
  await qual.getByRole("button", { name: /Activate 1 scope/ }).click();
  await expect(qual.locator(".banner.success")).toContainText("1 grant activated.");
  const grantCard = qual.locator(".grant-card");
  await expect(grantCard.locator(".badge", { hasText: "effective" })).toBeVisible();
  await beat(qual, 3500);

  /* ACT 8 — Learned route preview. */
  await qual.goto("/#/flock/routing");
  await fontsReady(qual);
  await qual.getByLabel("Task ID").fill("task-e2e-flock");
  await qual.getByRole("button", { name: "Preview decision" }).click();
  await expect(qual.locator(".run-detail").getByText("durable_grant_active")).toBeVisible();
  await beat(qual, 3500);

  /* ACT 9 — Revocation + static fallback. */
  await qual.goto("/#/flock/activations");
  await fontsReady(qual);
  await qual.locator(".grant-card").getByRole("button", { name: "Revoke" }).click();
  await beat(qual, 1200);
  await qual.locator(".grant-card").getByRole("button", { name: "Confirm revocation" }).click();
  await expect(qual.locator(".grant-card").getByText(/Revoked — terminal/)).toBeVisible();
  await beat(qual, 3000);

  await qual.goto("/#/flock/routing");
  await fontsReady(qual);
  await qual.getByLabel("Task ID").fill("task-e2e-flock");
  await qual.getByRole("button", { name: "Preview decision" }).click();
  await expect(qual.locator(".run-detail").getByText(/grant_revoked/)).toBeVisible();
  await beat(qual, 2000);
  await beat(qual, 2500); // closing beat
});
