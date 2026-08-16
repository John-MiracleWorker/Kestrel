# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: desktop/e2e/demo.spec.ts >> Kestrel narrated product demo
- Location: desktop/e2e/demo.spec.ts:41:1

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/", waiting until "load"

```

# Test source

```ts
  1   | /**
  2   |  * Kestrel UI demo — narrated walkthrough (video-capture spec).
  3   |  *
  4   |  * Full owner journey at narration pace: Mission -> approval -> Setup ->
  5   |  * LAN discovery -> Flock qualification ($50 draft -> start -> pause/resume
  6   |  * -> receipt) -> activation -> learned route -> revoke -> static fallback.
  7   |  * All responses come from the deterministic Demo fixtures — the same
  8   |  * authority as the CI suites. No live provider, network, or credential.
  9   |  *
  10  |  * Run: npx playwright test demo --config playwright.demo.config.ts
  11  |  */
  12  | import { expect, test, type Page } from "@playwright/test";
  13  | import { existsSync } from "node:fs";
  14  | import { dirname, resolve } from "node:path";
  15  | import { fileURLToPath } from "node:url";
  16  | import { demoFixtureInitScript, lanDiscoveryFixtureInitScript, flockQualificationFixtureInitScript } from "./fixtures";
  17  | 
  18  | const here = dirname(fileURLToPath(import.meta.url));
  19  | const indexPath = resolve(here, "../../web/dist/index.html");
  20  | 
  21  | test.skip(
  22  |   !existsSync(indexPath),
  23  |   "web/dist is not built; run `npm --prefix web run build` first",
  24  | );
  25  | 
  26  | const VIEWPORT = { width: 1440, height: 900 } as const;
  27  | 
  28  | async function beat(page: Page, ms: number): Promise<void> {
  29  |   await page.waitForTimeout(ms);
  30  | }
  31  | 
  32  | async function fontsReady(page: Page): Promise<void> {
  33  |   await page.waitForFunction(() => document.fonts.status === "loaded");
  34  | }
  35  | 
  36  | async function nav(page: Page, label: string): Promise<void> {
  37  |   await page.getByRole("link", { name: label, exact: true }).first().click();
  38  |   await beat(page, 1200);
  39  | }
  40  | 
  41  | test("Kestrel narrated product demo", async ({ page }) => {
  42  |   test.setTimeout(420_000);
  43  |   await page.setViewportSize(VIEWPORT);
  44  | 
  45  |   /* ACT 1 — Cold open: the shell. */
  46  |   await page.addInitScript(demoFixtureInitScript());
> 47  |   await page.goto("/");
      |              ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  48  |   await expect(page.locator("main")).toHaveCount(1);
  49  |   await fontsReady(page);
  50  |   await beat(page, 4000);
  51  | 
  52  |   /* ACT 2 — Mission: type, review, launch. */
  53  |   await nav(page, "Mission");
  54  |   await beat(page, 2500);
  55  |   const objective = page.getByLabel("Objective", { exact: true });
  56  |   await objective.click();
  57  |   await page.keyboard.type("Summarize this project", { delay: 50 });
  58  |   await expect(objective).toHaveValue("Summarize this project");
  59  |   await beat(page, 1800);
  60  |   await page.getByRole("button", { name: "Review mission" }).click();
  61  |   await beat(page, 3500);
  62  |   const startButton = page.getByRole("button", { name: "Start mission" });
  63  |   await expect(startButton).toBeEnabled({ timeout: 20_000 });
  64  |   await beat(page, 1200);
  65  |   await startButton.click();
  66  |   await expect(page.getByText("The mission is queued.")).toBeVisible();
  67  |   await beat(page, 3000);
  68  | 
  69  |   /* ACT 3 — Approval queue: exact-call control. */
  70  |   await page.addInitScript(demoFixtureInitScript({ mode: "approval-pending" }));
  71  |   await page.goto("/");
  72  |   await fontsReady(page);
  73  |   await nav(page, "Mission");
  74  |   await page.getByRole("button", { name: /Summarize this project/ }).first().click();
  75  |   await beat(page, 4000);
  76  | 
  77  |   /* ACT 4 — Settings + Setup Center. */
  78  |   await nav(page, "Settings");
  79  |   await beat(page, 1800);
  80  |   await page.getByRole("button", { name: "Setup Center" }).first().click();
  81  |   await beat(page, 3200);
  82  | 
  83  |   /* ACT 5 — LAN discovery. */
  84  |   await page.addInitScript(demoFixtureInitScript());
  85  |   await page.addInitScript(lanDiscoveryFixtureInitScript());
  86  |   await page.goto("/#/flock/lan");
  87  |   await fontsReady(page);
  88  |   await beat(page, 2000);
  89  |   const scanButton = page.getByRole("button", { name: /scan/i }).first();
  90  |   if (await scanButton.count()) {
  91  |     await scanButton.click();
  92  |     await beat(page, 3500);
  93  |   }
  94  |   await beat(page, 1500);
  95  | 
  96  |   /* ACT 6 — Flock qualification journey. */
  97  |   await page.addInitScript(demoFixtureInitScript());
  98  |   await page.addInitScript(flockQualificationFixtureInitScript());
  99  |   await page.goto("/#/flock/qualification");
  100 |   await fontsReady(page);
  101 |   await expect(
  102 |     page.getByRole("heading", { name: "Adaptive Flock qualification" }),
  103 |   ).toBeVisible();
  104 |   await beat(page, 2500);
  105 | 
  106 |   const capField = page.getByLabel("Maximum provider spend");
  107 |   await expect(capField).toHaveValue("50.00");
  108 |   await beat(page, 1500);
  109 |   const ceilingField = page.getByLabel("Per-attempt cost ceiling");
  110 |   await ceilingField.fill("7.50");
  111 |   await beat(page, 1200);
  112 | 
  113 |   await page.getByRole("button", { name: "Refresh preview" }).click();
  114 |   await expect(page.getByRole("heading", { name: "Target matrix" })).toBeVisible();
  115 |   await beat(page, 4000);
  116 | 
  117 |   await page.getByRole("checkbox", { name: /I have reviewed the preview/ }).check();
  118 |   await beat(page, 800);
  119 |   await page.getByRole("button", { name: "Create and start qualification" }).click();
  120 |   await expect(page.locator(".banner.success")).toContainText(
  121 |     "Qualification started for 1 scope(s)",
  122 |   );
  123 |   await beat(page, 2500);
  124 | 
  125 |   const progress = page.locator(".qual-progress");
  126 |   await expect(progress.locator(".badge").first()).toContainText("running");
  127 |   await beat(page, 1500);
  128 |   await progress.getByRole("button", { name: "Pause" }).click();
  129 |   await expect(progress.locator(".badge").first()).toContainText("paused");
  130 |   await beat(page, 1800);
  131 |   await progress.getByRole("button", { name: "Resume" }).click();
  132 |   await expect(progress.locator(".badge").first()).toContainText("running");
  133 |   await beat(page, 1500);
  134 | 
  135 |   const results = page.locator(".qual-results");
  136 |   await expect(results.getByText("Evidence collection completed")).toBeVisible({
  137 |     timeout: 30_000,
  138 |   });
  139 |   await beat(page, 2000);
  140 |   await expect(results.getByText("1 scope qualified")).toBeVisible();
  141 |   await expect(results.getByText("Guardrail violations: 0")).toBeVisible();
  142 |   await beat(page, 4000);
  143 | 
  144 |   /* ACT 7 — Activation. */
  145 |   await page.goto("/#/flock/activations");
  146 |   await fontsReady(page);
  147 |   await expect(page.getByRole("heading", { name: "Flock activations" })).toBeVisible();
```