# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: demo.spec.ts >> Kestrel narrated product demo
- Location: e2e/demo.spec.ts:41:1

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByRole('heading', { name: 'Target matrix' })
Expected: visible
Timeout: 15000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" with timeout 15000ms
  - waiting for getByRole('heading', { name: 'Target matrix' })

```

```yaml
- strong: Kestrel
- text: Wildflower Workbench
- navigation "Workbench destinations":
  - link "Mission":
    - /url: "#/mission/command"
  - link "Projects":
    - /url: "#/projects/overview"
  - link "Memory":
    - /url: "#/memory/layers"
  - link "Flock":
    - /url: "#/flock/overview"
    - text: Flock Current
  - link "Automate":
    - /url: "#/automate/routines"
  - link "Extend":
    - /url: "#/extend/catalog"
  - link "Settings":
    - /url: "#/settings/general"
- paragraph: Local · private · single owner
- banner:
  - paragraph: Mission Command
  - text: Ask, inspect, tune, and approve from one workbench.
  - button "Open command palette": Search Kestrel ⌘K / Ctrl K
- main:
  - link "Kestrel Steady Companion":
    - /url: "#workspace"
  - navigation "Primary":
    - button "Workbench"
    - button "History"
    - button "Outcomes"
    - button "Advanced"
  - button "Setup"
  - text: Needs approval
  - link "Skip to workspace":
    - /url: "#workspace"
  - complementary "Threads":
    - heading "Chats 0" [level=2]
    - button "New chat"
    - textbox "Search threads..."
    - region "Conversation threads": No threads yet.
  - region "Adaptive Flock routing workbench":
    - text: Adaptive execution
    - heading "Adaptive Flock Routing" [level=1]
    - paragraph: Configure provider pools, inspect route policies, and preview why Kestrel selects a worker.
    - button "Back to chat"
    - text: e2e_fixture_refused_mutation:POST /api/flock/qualifications/preview
    - region "Qualification workspace":
      - heading "Adaptive Flock qualification" [level=2]
      - paragraph: Nothing runs from this draft. A qualification run starts only after you refresh the preview, review the exact target matrix and corpus, and explicitly confirm the review.
      - term: Project
      - definition: project-1
      - term: Task families
      - definition: code_repair
      - term: Policy
      - definition: balanced (revision 1)
      - term: Default privacy class
      - definition: approved_cloud
      - text: Maximum provider spend
      - textbox "Maximum provider spend": "50.00"
      - text: Decimal text, forwarded verbatim. This becomes the immutable maximum once the run starts; afterwards the stop cap can only move down. Per-attempt cost ceiling
      - textbox "Per-attempt cost ceiling": "7.50"
      - text: No single attempt may reserve more than this.
      - group "Qualification thresholds":
        - text: Qualification thresholds Minimum examples per scope
        - textbox "Minimum examples per scope": "5"
        - text: Minimum examples per target
        - textbox "Minimum examples per target": "3"
        - text: Confidence threshold
        - textbox "Confidence threshold": "0.7"
        - text: Utility margin
        - textbox "Utility margin": "0.08"
        - text: Cost coverage threshold
        - textbox "Cost coverage threshold": "0.8"
        - text: Decay half-life (days)
        - textbox "Decay half-life (days)": "30"
        - text: Maximum guardrail violations
        - textbox "Maximum guardrail violations": "0"
        - text: Replay runs
        - textbox "Replay runs": "20"
        - text: Replay successes required
        - textbox "Replay successes required": "20"
      - heading "Corpus review" [level=3]
      - list:
        - listitem:
          - strong: sample-task-1
          - img "Information"
          - text: low
          - list:
            - listitem: "Task family: code_repair"
            - listitem: "Capabilities: generation"
            - listitem: "Evidence: synthetic"
            - listitem: Actionable
      - button "Refresh preview"
```

# Test source

```ts
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
  47  |   await page.goto("/");
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
> 114 |   await expect(page.getByRole("heading", { name: "Target matrix" })).toBeVisible();
      |                                                                      ^ Error: expect(locator).toBeVisible() failed
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
  148 |   await beat(page, 2000);
  149 |   await page.getByLabel("Qualification receipt ID").fill("rcpt_" + "c".repeat(24));
  150 |   await page.getByLabel("Scope digests").fill("1".repeat(64));
  151 |   await page.getByRole("button", { name: "Preview activation" }).click();
  152 |   await expect(page.getByRole("heading", { name: "Activation packet" })).toBeVisible();
  153 |   await beat(page, 3500);
  154 |   await page.getByRole("checkbox", { name: "Scope code_repair qualified" }).check();
  155 |   await page.getByRole("checkbox", { name: /I understand this activation grants/ }).check();
  156 |   await beat(page, 800);
  157 |   await page.getByRole("button", { name: /Activate 1 scope/ }).click();
  158 |   await expect(page.locator(".banner.success")).toContainText("1 grant activated.");
  159 |   const grantCard = page.locator(".grant-card");
  160 |   await expect(grantCard.locator(".badge", { hasText: "effective" })).toBeVisible();
  161 |   await beat(page, 3500);
  162 | 
  163 |   /* ACT 8 — Learned route preview. */
  164 |   await page.goto("/#/flock/routing");
  165 |   await fontsReady(page);
  166 |   await page.getByLabel("Task ID").fill("task-e2e-flock");
  167 |   await page.getByRole("button", { name: "Preview decision" }).click();
  168 |   await expect(page.locator(".run-detail").getByText("durable_grant_active")).toBeVisible();
  169 |   await beat(page, 3500);
  170 | 
  171 |   /* ACT 9 — Revocation + static fallback. */
  172 |   await page.goto("/#/flock/activations");
  173 |   await fontsReady(page);
  174 |   await page.locator(".grant-card").getByRole("button", { name: "Revoke" }).click();
  175 |   await beat(page, 1200);
  176 |   await page.locator(".grant-card").getByRole("button", { name: "Confirm revocation" }).click();
  177 |   await expect(page.locator(".grant-card").getByText(/Revoked — terminal/)).toBeVisible();
  178 |   await beat(page, 3000);
  179 | 
  180 |   await page.goto("/#/flock/routing");
  181 |   await fontsReady(page);
  182 |   await page.getByLabel("Task ID").fill("task-e2e-flock");
  183 |   await page.getByRole("button", { name: "Preview decision" }).click();
  184 |   await expect(page.locator(".run-detail").getByText(/grant_revoked/)).toBeVisible();
  185 |   await beat(page, 2000);
  186 |   await beat(page, 2500); // closing beat
  187 | });
  188 | 
```