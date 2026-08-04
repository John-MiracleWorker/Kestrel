# Wildflower Workbench and GUI-First Product Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing React Workbench into Kestrel’s primary product experience: a warm, expressive Wildflower Workshop interface with Mission Command at its center, a permanent Setup Center, seven stable destinations, truthful tunable settings, and progressive access to every existing capability.

**Architecture:** Keep one React application shared by the browser-served advanced Workbench and the Electron renderer. First extract the 5,500-line `App.tsx` into a typed application shell, workspace modules, and feature-owned API/state hooks without changing server authority. Then layer in Wildflower design tokens and accessible primitives. Desktop-native operations go through the narrow bridge from the desktop foundation plan; browser mode retains safe fallbacks. New feature tracks—LAN discovery and Flock qualification—mount into stable Flock routes without reshaping the shell.

**Tech Stack:** React 19.2.1, TypeScript 5.9.3, Vite 7.3.6, Vitest 4.1.6, Testing Library, axe-core 4.11.4, Lucide, CSS custom properties, bundled Fraunces and Atkinson Hyperlegible Next fonts under the SIL Open Font License, existing FastAPI APIs, and the desktop bridge contract.

## Global Constraints

- Begin only after the desktop foundation’s transport and preload contracts are merged into the integration branch.
- Preserve browser-served Workbench use. Desktop-native actions must have a clear browser fallback or an explicit “available in Desktop” explanation.
- Do not move authoritative decisions into React. The UI may draft, preview, and submit; FastAPI revalidates and commits.
- Keep exact-call approvals, capability ceilings, project path ceilings, budgets, provenance, evidence, rollback, and effective blockers visible.
- Do not hide unsupported or unfinished behavior behind optimistic UI. Use `configured`, `effective`, `blocked`, `pending_restart`, and `unavailable` states.
- Raw IDs, digests, JSON, and trace data live under contextual Evidence or Advanced disclosure, not the default task path.
- Every top-level feature must be keyboard reachable, screen-reader named, responsive at a 960 px narrow desktop window, and usable with reduced motion.
- Meet WCAG 2.2 AA. Color is never the sole status, risk, approval, or validation signal.
- Bundle all fonts/icons/assets. Do not load Google Fonts, analytics, remote scripts, remote images, or remote CSS.
- Keep the Wildflower palette expressive in both light and dark themes; do not collapse dark mode to generic gray.
- Do not add new global state libraries in this phase. Use feature hooks, reducer/context boundaries, and the existing typed API utilities.
- Keep each extraction behavior-preserving before visual changes. Snapshot old API calls and user flows before moving them.
- Keep deterministic mocks and use fake desktop bridges in renderer tests.
- Run `npm --prefix web test` and `npm --prefix web run build` after every phase, and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q` after phases that change server contracts.

---

## Phase 1: Extract a Stable Product Shell

### Task 1: Freeze existing Workbench behavior with contract tests

**Files:**

- Create: `web/src/app/legacyBehavior.contract.test.tsx`
- Create: `web/src/testing/apiFixtures.ts`
- Create: `web/src/testing/fakeDesktopBridge.ts`
- Modify: `web/src/App.test.tsx`
- Modify: `web/src/routing/RoutingCenter.test.tsx`
- Modify: `web/src/mission/MissionControl.test.tsx`

**Interfaces:**

- Consume: current HTTP endpoints and hash routes.
- Produce: a fixture inventory for runs, projects, approvals, memory, capabilities, providers, targets, routines, extensions, setup readiness, and settings.
- Invariant: refactoring must not change request method/path/body or omit revision fields.

- [ ] **Step 1: Add failing fixture-coverage assertions**

```ts
it("has a deterministic fixture for every current App startup request", () => {
  expect(Object.keys(apiFixtures).sort()).toEqual([
    "/api/approvals",
    "/api/capabilities",
    "/api/channels",
    "/api/learning/dashboard?since=all",
    "/api/memory/layers",
    "/api/mcp/servers",
    "/api/plugins",
    "/api/product/setup",
    "/api/projects",
    "/api/routing/status",
    "/api/runs",
    "/api/runtime/config",
    "/api/runtime/models",
    "/api/runtime/settings",
    "/api/secrets",
    "/api/sessions",
    "/api/skills",
    "/api/tools"
  ]);
});
```

Record tests for: first-run setup; mission preview/launch; approval decision; capability toggle with expected revision; settings save; provider/target save; routine run; memory search; extension review; and browser token prompt.

- [ ] **Step 2: Run tests and identify fixture gaps**

Run:

```bash
npm --prefix web test -- legacyBehavior App MissionControl RoutingCenter
```

Expected: the new inventory test fails until every current startup call and mutation is represented.

- [ ] **Step 3: Complete fixtures without changing production code**

Extract only test helpers. Keep sentinel secrets out of serialized fixtures. Make unknown endpoints fail tests rather than return an empty success.

- [ ] **Step 4: Run the current renderer suite**

Run:

```bash
npm --prefix web test
npm --prefix web run build
```

Expected: all existing behavior remains green.

- [ ] **Step 5: Commit**

```bash
git add web/src/testing web/src/app/legacyBehavior.contract.test.tsx \
  web/src/App.test.tsx \
  web/src/routing/RoutingCenter.test.tsx \
  web/src/mission/MissionControl.test.tsx
git commit -m "test: freeze workbench behavior before shell refactor"
```

### Task 2: Extract navigation, routing, and application layout from `App.tsx`

**Files:**

- Create: `web/src/app/destinations.ts`
- Create: `web/src/app/destinations.test.ts`
- Create: `web/src/app/AppShell.tsx`
- Create: `web/src/app/AppShell.test.tsx`
- Create: `web/src/app/AppRouter.tsx`
- Create: `web/src/app/AppRouter.test.tsx`
- Create: `web/src/app/ApplicationContext.tsx`
- Create: `web/src/app/useApplicationData.ts`
- Create: `web/src/app/useApplicationData.test.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/main.tsx`
- Modify: `web/src/styles.css`

**Interfaces:**

- Top-level destinations: `mission`, `projects`, `memory`, `flock`, `automate`, `extend`, and `settings`.
- Nested route format: `#/<destination>/<subroute>?<query>`.
- Browser history and Electron single-instance deep links use the same parser.
- Produce: `ApplicationSnapshot` with independently loading feature slices; one failed optional slice must not blank Mission.

- [ ] **Step 1: Write failing route and shell tests**

```ts
it.each([
  ["#/mission", { destination: "mission", subroute: "command" }],
  ["#/projects/repo_1", { destination: "projects", subroute: "repo_1" }],
  ["#/flock/qualification", { destination: "flock", subroute: "qualification" }],
  ["#/settings/updates", { destination: "settings", subroute: "updates" }]
])("parses %s", (hash, expected) => {
  expect(parseAppLocation(hash)).toMatchObject(expected);
});

it("defaults unknown routes to Mission without discarding evidence query", () => {
  expect(parseAppLocation("#/unknown?run_id=run_1")).toEqual({
    destination: "mission",
    subroute: "command",
    query: { run_id: "run_1" },
    recoveryReason: "unknown_route"
  });
});
```

Render the shell and assert exactly seven destination links, one `<main>`, one optional context rail, and an accessible current-page marker.

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- destinations AppShell AppRouter useApplicationData
```

Expected: modules absent.

- [ ] **Step 3: Implement an incremental shell**

Define:

```ts
export const DESTINATIONS = [
  { id: "mission", label: "Mission", icon: Target },
  { id: "projects", label: "Projects", icon: FolderKanban },
  { id: "memory", label: "Memory", icon: Brain },
  { id: "flock", label: "Flock", icon: Bird },
  { id: "automate", label: "Automate", icon: Workflow },
  { id: "extend", label: "Extend", icon: Blocks },
  { id: "settings", label: "Settings", icon: SlidersHorizontal }
] as const;
```

Move routing and layout first, leaving legacy panels mounted through adapters. Split startup fetching into feature slices with abort support and stable error boundaries. Do not add a second copy of any mutation function.

- [ ] **Step 4: Verify behavior parity and size reduction**

Run:

```bash
npm --prefix web test
npm --prefix web run build
wc -l web/src/App.tsx
```

Expected: all tests pass and `App.tsx` is below 1,500 lines. If not, continue extracting state/actions before visual work.

- [ ] **Step 5: Commit**

```bash
git add web/src/app web/src/App.tsx web/src/main.tsx web/src/styles.css
git commit -m "refactor: establish seven-destination workbench shell"
```

### Task 3: Extract feature workspaces without behavior changes

**Files:**

- Create: `web/src/projects/ProjectsWorkspace.tsx`
- Create: `web/src/projects/ProjectsWorkspace.test.tsx`
- Create: `web/src/memory/MemoryWorkspace.tsx`
- Create: `web/src/memory/MemoryWorkspace.test.tsx`
- Create: `web/src/automate/AutomateWorkspace.tsx`
- Create: `web/src/automate/AutomateWorkspace.test.tsx`
- Create: `web/src/extend/ExtendWorkspace.tsx`
- Create: `web/src/extend/ExtendWorkspace.test.tsx`
- Create: `web/src/settings/SettingsWorkspace.tsx`
- Create: `web/src/settings/SettingsWorkspace.test.tsx`
- Create: `web/src/chat/ConversationPanel.tsx`
- Create: `web/src/chat/ConversationPanel.test.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/app/AppRouter.tsx`

**Interfaces:**

- Each workspace owns its feature state and actions but consumes shared current project/run/session selection from `ApplicationContext`.
- Existing `RoutineWorkbench` moves intact into `AutomateWorkspace` before redesign.
- Existing routing UI moves under `Flock`; qualification and LAN tabs are route stubs with truthful dependency status until their plans land.

- [ ] **Step 1: Write failing ownership tests**

```ts
it("does not fetch extension inventory while only Mission is mounted", async () => {
  renderAppAt("#/mission");
  await screen.findByRole("heading", { name: /mission/i });
  expect(requests()).not.toContain("/api/plugins");
  expect(requests()).not.toContain("/api/mcp/servers");
});

it("loads Extend inventory when Extend becomes active", async () => {
  renderAppAt("#/extend");
  await screen.findByRole("heading", { name: "Extend" });
  expect(requests()).toContain("/api/plugins");
});
```

- [ ] **Step 2: Run and verify current eager-loading failure**

Run:

```bash
npm --prefix web test -- ProjectsWorkspace MemoryWorkspace \
  AutomateWorkspace ExtendWorkspace SettingsWorkspace ConversationPanel
```

Expected: modules absent; the eager-loading contract may expose current coupling.

- [ ] **Step 3: Move code one workspace at a time**

After each move, run that workspace test and `App.test.tsx`. Preserve endpoint contracts and revision fields. Use lazy feature loading only at module boundaries; never defer an approval, blocker, or active-run status needed by Mission.

- [ ] **Step 4: Run full suite and enforce maintainable module sizes**

Run:

```bash
npm --prefix web test
npm --prefix web run build
wc -l web/src/App.tsx web/src/*/*Workspace.tsx
```

Expected: `App.tsx` below 500 lines; no new workspace above 1,000 lines. Split feature-owned panels if a workspace exceeds the limit.

- [ ] **Step 5: Commit**

```bash
git add web/src/projects web/src/memory web/src/automate web/src/extend \
  web/src/settings web/src/chat web/src/App.tsx web/src/app/AppRouter.tsx
git commit -m "refactor: split workbench into feature workspaces"
```

---

## Phase 2: Build the Wildflower Workshop Design System

### Task 4: Add bundled typography, color tokens, and theme semantics

**Files:**

- Create: `web/public/fonts/fraunces-latin-variable.woff2`
- Create: `web/public/fonts/atkinson-hyperlegible-next-latin-variable.woff2`
- Create: `web/public/fonts/OFL-Fraunces.txt`
- Create: `web/public/fonts/OFL-Atkinson-Hyperlegible-Next.txt`
- Create: `web/src/design/tokens.css`
- Create: `web/src/design/typography.css`
- Create: `web/src/design/theme.ts`
- Create: `web/src/design/theme.test.ts`
- Modify: `web/src/main.tsx`
- Modify: `web/src/styles.css`
- Modify: `web/public/THIRD_PARTY_NOTICES.txt`
- Modify: `scripts/generate-web-third-party-notices.mjs`
- Modify: `web/src/styles.test.ts`

**Interfaces:**

- Semantic tokens, not raw component colors: canvas, surface, ink, muted, structural, action, success, attention, caution, info, danger, focus, border, and shadow.
- Themes: `light`, `dark`, and `system`.
- Reduced motion follows `prefers-reduced-motion`; the setting may force reduction but never force motion against OS preference.

- [ ] **Step 1: Write failing token and license tests**

```ts
it("defines every semantic token in light and dark themes", () => {
  const css = readFileSync("src/design/tokens.css", "utf8");
  for (const token of REQUIRED_TOKENS) {
    expect(lightTheme(css)).toContain(`--${token}:`);
    expect(darkTheme(css)).toContain(`--${token}:`);
  }
});

it("does not reference remote fonts or assets", () => {
  expect(allStyleText()).not.toMatch(/https?:\\/\\//);
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- theme styles
npm --prefix web run licenses:check
```

Expected: token/font files absent and notices incomplete.

- [ ] **Step 3: Implement the Wildflower foundation**

Use accessibility-adjusted production values centered on:

```css
:root {
  --canvas: #f7f0df;
  --surface: #fffaf0;
  --ink: #2b172c;
  --muted-ink: #675b64;
  --structural: #4a204c;
  --action: #ef6a5b;
  --success: #2f8f7b;
  --selected: #b9d936;
  --info: #5f78c9;
  --caution: #c98722;
  --danger: #b23a48;
  --focus: #2d67c8;
  --border-strong: #3a2439;
  --shadow-offset: 4px 4px 0 color-mix(in srgb, var(--structural) 34%, transparent);
}
```

Adjust any failing pair to AA rather than preserving these seed values literally. Use Fraunces only for major headings/data stories and Atkinson for controls/body/evidence/code-adjacent labels. Include font file SHA-256 and licenses in notices.

- [ ] **Step 4: Run style, license, and build checks**

Run:

```bash
npm --prefix web run licenses:generate
npm --prefix web run licenses:check
npm --prefix web test -- theme styles
npm --prefix web run build
```

Expected: no remote asset references and both themes define all tokens.

- [ ] **Step 5: Commit**

```bash
git add web/public/fonts web/public/THIRD_PARTY_NOTICES.txt \
  web/src/design web/src/main.tsx web/src/styles.css web/src/styles.test.ts \
  scripts/generate-web-third-party-notices.mjs
git commit -m "style: add accessible Wildflower theme foundation"
```

### Task 5: Build accessible Wildflower primitives

**Files:**

- Create: `web/src/design/Button.tsx`
- Create: `web/src/design/Button.test.tsx`
- Create: `web/src/design/Card.tsx`
- Create: `web/src/design/Card.test.tsx`
- Create: `web/src/design/StatusPill.tsx`
- Create: `web/src/design/StatusPill.test.tsx`
- Create: `web/src/design/Disclosure.tsx`
- Create: `web/src/design/Disclosure.test.tsx`
- Create: `web/src/design/Field.tsx`
- Create: `web/src/design/Field.test.tsx`
- Create: `web/src/design/Notice.tsx`
- Create: `web/src/design/EmptyState.tsx`
- Create: `web/src/design/Skeleton.tsx`
- Create: `web/src/design/design-system.css`
- Modify: `web/src/components.tsx`

**Interfaces:**

- Variants encode semantic intent: primary, secondary, quiet, danger; healthy, blocked, waiting, caution, inactive.
- All interactive primitives forward native props and refs; keyboard behavior remains native.
- Status always renders icon plus text.

- [ ] **Step 1: Write failing accessibility tests**

```ts
it("communicates status without color", () => {
  render(<StatusPill state="blocked">Needs approval</StatusPill>);
  expect(screen.getByText("Needs approval")).toBeVisible();
  expect(screen.getByTestId("status-icon")).toHaveAccessibleName("Blocked");
});

it("keeps disclosure state available to assistive technology", async () => {
  render(<Disclosure title="Evidence">digest</Disclosure>);
  const button = screen.getByRole("button", { name: "Evidence" });
  expect(button).toHaveAttribute("aria-expanded", "false");
  await user.click(button);
  expect(button).toHaveAttribute("aria-expanded", "true");
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- Button Card StatusPill Disclosure Field
```

Expected: primitives absent.

- [ ] **Step 3: Implement and migrate shared components**

Keep `components.tsx` as temporary compatibility exports. Replace old panel/field/status styles incrementally. Offset shadows move on active press but disappear under reduced motion. Focus uses a high-contrast two-layer ring, never `outline: none`.

- [ ] **Step 4: Run component and app tests**

Run:

```bash
npm --prefix web test
npm --prefix web run build
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/design web/src/components.tsx
git commit -m "feat: add Wildflower interface primitives"
```

### Task 6: Apply the Mission Command Center layout

**Files:**

- Create: `web/src/app/NavigationRail.tsx`
- Create: `web/src/app/NavigationRail.test.tsx`
- Create: `web/src/app/ContextRail.tsx`
- Create: `web/src/app/ContextRail.test.tsx`
- Create: `web/src/app/CommandBar.tsx`
- Create: `web/src/app/CommandBar.test.tsx`
- Create: `web/src/app/shell.css`
- Modify: `web/src/app/AppShell.tsx`
- Modify: `web/src/app/AppShell.test.tsx`

**Interfaces:**

- Stable left navigation.
- Dominant central `<main>`.
- Optional right context rail for project, route, budget, permissions, approvals, and evidence.
- Global command/search opens features and settings; it does not execute dangerous calls.

- [ ] **Step 1: Write failing layout and keyboard tests**

```ts
it("moves focus through navigation, main, and context in document order", async () => {
  render(<AppShell fixture={missionFixture} />);
  await user.tab();
  expect(screen.getByRole("link", { name: "Mission" })).toHaveFocus();
  await tabUntil(() => screen.getByRole("main"));
  await tabUntil(() => screen.getByRole("complementary", { name: "Mission context" }));
});

it("collapses the context rail without removing its toggle", () => {
  setViewport(960, 760);
  render(<AppShell fixture={missionFixture} />);
  expect(screen.getByRole("button", { name: "Show mission context" })).toBeVisible();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- NavigationRail ContextRail CommandBar AppShell
```

Expected: shell subcomponents absent.

- [ ] **Step 3: Implement responsive shell**

At wide widths, use navigation / main / context columns. At narrow desktop widths, collapse labels and make context a non-modal drawer with focus management. Never create horizontal page scrolling; only evidence tables/code blocks may scroll within named regions.

- [ ] **Step 4: Run renderer tests/build**

Run:

```bash
npm --prefix web test
npm --prefix web run build
```

Expected: all pass at wide and narrow viewport fixtures.

- [ ] **Step 5: Commit**

```bash
git add web/src/app
git commit -m "feat: shape Mission Command Center layout"
```

---

## Phase 3: Make First Use and Daily Missions GUI-First

### Task 7: Rebuild first run as a five-stage Setup Center

**Files:**

- Create: `web/src/setup/SetupCenter.tsx`
- Create: `web/src/setup/SetupCenter.test.tsx`
- Create: `web/src/setup/SetupProgress.tsx`
- Create: `web/src/setup/stages/CoreCheckStage.tsx`
- Create: `web/src/setup/stages/IntelligenceStage.tsx`
- Create: `web/src/setup/stages/ProjectStage.tsx`
- Create: `web/src/setup/stages/SafetyStage.tsx`
- Create: `web/src/setup/stages/FirstMissionStage.tsx`
- Create: `web/src/setup/types.ts`
- Create: `web/src/setup/api.ts`
- Create: `web/src/setup/setup.css`
- Modify: `web/src/settings/SettingsWorkspace.tsx`
- Modify: `web/src/app/AppRouter.tsx`
- Modify: `web/src/App.tsx`

**Interfaces:**

- Stages: bundled-core check, choose intelligence, add project, review safety defaults, start first mission.
- Setup Center remains reachable at `#/settings/setup` after completion.
- Consume: `/api/product/setup`, runtime model catalogs, projects, Secret Broker metadata, and desktop native pickers.
- Invariant: setup never enables dangerous capabilities merely to remove a warning.

- [ ] **Step 1: Write failing stage and resume tests**

```ts
it("can continue offline with Demo and no project", async () => {
  renderSetup({ provider: "mock", network: "offline" });
  await user.click(screen.getByRole("button", { name: "Continue with Demo" }));
  expect(screen.getByRole("heading", { name: "Add a project" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Do this later" }));
  expect(screen.getByRole("heading", { name: "Review safety defaults" })).toBeVisible();
});

it("resumes at the first incomplete server-backed stage after reload", () => {
  renderSetup({ completedStages: ["core", "intelligence"] });
  expect(screen.getByRole("heading", { name: "Add a project" })).toBeVisible();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- SetupCenter
```

Expected: new Setup Center absent.

- [ ] **Step 3: Implement server-truth-based progression**

Do not use `kestrel.setup.dismissed` as authority. Local storage may remember presentation only; readiness and saved project/provider state come from the server. Each failed check renders reason, evidence, and GUI repair action. A command may appear only inside Advanced diagnostics.

The project stage calls the native folder picker when available, previews the canonical path, allowed ceiling, Git state, recipes, budgets, capabilities, provider policy, and rollback strategy, then saves through the existing project API.

- [ ] **Step 4: Run setup, app, and build tests**

Run:

```bash
npm --prefix web test -- SetupCenter App
npm --prefix web run build
```

Expected: keyboard-only setup completes through Demo mode.

- [ ] **Step 5: Commit**

```bash
git add web/src/setup web/src/settings/SettingsWorkspace.tsx \
  web/src/app/AppRouter.tsx web/src/App.tsx
git commit -m "feat: replace onboarding modal with permanent Setup Center"
```

### Task 8: Redesign Mission as the everyday command surface

**Files:**

- Create: `web/src/mission/ObjectiveComposer.tsx`
- Create: `web/src/mission/ObjectiveComposer.test.tsx`
- Create: `web/src/mission/MissionPreflightCard.tsx`
- Create: `web/src/mission/MissionPreflightCard.test.tsx`
- Create: `web/src/mission/ActiveMission.tsx`
- Create: `web/src/mission/ActiveMission.test.tsx`
- Create: `web/src/mission/ApprovalQueue.tsx`
- Create: `web/src/mission/ApprovalQueue.test.tsx`
- Create: `web/src/mission/EvidenceDrawer.tsx`
- Create: `web/src/mission/EvidenceDrawer.test.tsx`
- Modify: `web/src/mission/MissionControl.tsx`
- Modify: `web/src/mission/MissionControl.test.tsx`
- Modify: `web/src/mission/mission.css`
- Modify: `web/src/engineering/EngineeringRunPanel.tsx`
- Modify: `web/src/repair/RepairReviewPanel.tsx`

**Interfaces:**

- Mission states: compose, preflight, active, needs-owner, reviewing, completed, blocked.
- Compose includes project, goal template, objective, editable acceptance plan, route preview, budget, and capability ceiling.
- Active mission projects task graph, worker activity, approvals, candidate comparison, validation, diff, browser evidence, and GitHub handoff.
- Invariant: an approval card shows exact call, arguments, capability, target resource/digest, expiry, and consequences before decision.

- [ ] **Step 1: Write failing primary-path tests**

```ts
it("launches a safe Demo mission without visiting Settings", async () => {
  renderMission({ project: projectFixture, provider: "mock" });
  await user.type(screen.getByLabelText("Objective"), "Explain the failing unit test");
  await user.click(screen.getByRole("button", { name: "Review mission" }));
  expect(screen.getByText("No external spend")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Start mission" }));
  expect(lastRequest()).toMatchObject({ path: "/api/runs", method: "POST" });
});

it("never collapses a blocker into a disabled button without explanation", () => {
  renderMission({ preflight: failedContainmentPreflight });
  expect(screen.getByRole("button", { name: "Start mission" })).toBeDisabled();
  expect(screen.getByText(/containment engine is required/i)).toBeVisible();
  expect(screen.getByRole("link", { name: "Open Containment settings" })).toBeVisible();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- ObjectiveComposer MissionPreflightCard \
  ActiveMission ApprovalQueue EvidenceDrawer MissionControl
```

Expected: new mission components absent.

- [ ] **Step 3: Implement progressive Mission states**

Keep existing `MissionControl` API contracts. Move raw records into `EvidenceDrawer`. Place project, route, budget, and permission summaries in the context rail. Merge chat as a mission conversation panel rather than a competing top-level destination. Preserve thread/session history under Mission.

- [ ] **Step 4: Run mission, engineering, repair, and app tests**

Run:

```bash
npm --prefix web test -- MissionControl EngineeringRunPanel RepairReviewPanel App
npm --prefix web run build
```

Expected: all pass, including exact-call approval and candidate-selection flows.

- [ ] **Step 5: Commit**

```bash
git add web/src/mission \
  web/src/engineering/EngineeringRunPanel.tsx \
  web/src/repair/RepairReviewPanel.tsx
git commit -m "feat: make Mission the primary task surface"
```

### Task 9: Complete Projects and Memory product surfaces

**Files:**

- Create: `web/src/projects/ProjectOverview.tsx`
- Create: `web/src/projects/ProjectEditor.tsx`
- Create: `web/src/projects/ProjectIndexStatus.tsx`
- Create: `web/src/projects/ProjectHistory.tsx`
- Create: `web/src/projects/projects.css`
- Create: `web/src/projects/ProjectsWorkspace.test.tsx`
- Create: `web/src/memory/MemoryHealth.tsx`
- Create: `web/src/memory/MemorySearch.tsx`
- Create: `web/src/memory/PromotionHistory.tsx`
- Create: `web/src/memory/BehaviorDeltaWorkspace.tsx`
- Create: `web/src/memory/memory.css`
- Modify: `web/src/projects/ProjectsWorkspace.tsx`
- Modify: `web/src/memory/MemoryWorkspace.tsx`

**Interfaces:**

- Projects exposes profiles, native folder selection, allowed paths, recipes, budgets, policies, capabilities, index freshness, history, outcomes, and memory coverage.
- Memory exposes six layer health records, search/evidence, run capsules, promotion history, behavior deltas, activation/outcome/rollback, and consolidation.
- Invariant: ordinary memory learning is never presented as policy authority; self and policy actions show stronger gates.

- [ ] **Step 1: Write failing authority-label tests**

```ts
it("labels policy memory as gated authority", () => {
  render(<MemoryWorkspace fixture={memoryFixture} />);
  expect(screen.getByRole("row", { name: /policy/i })).toHaveTextContent(
    "Manual or repeated validated evidence required"
  );
});

it("uses a native picker and previews project authority before save", async () => {
  renderProjectsWithDesktopBridge("/workspace/repo");
  await user.click(screen.getByRole("button", { name: "Add project" }));
  expect(screen.getByText("/workspace/repo")).toBeVisible();
  expect(screen.getByText(/allowed path ceiling/i)).toBeVisible();
  expect(saveProjectRequest()).toBeUndefined();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- ProjectsWorkspace MemoryWorkspace
```

Expected: new subviews absent.

- [ ] **Step 3: Implement friendly summaries with Evidence disclosures**

Use server-provided state; do not infer health from file names or client timers. Every move-storage action is a disabled affordance linked to the packaging/recovery plan until its transactional server contract exists.

- [ ] **Step 4: Run feature and build tests**

Run:

```bash
npm --prefix web test -- ProjectsWorkspace MemoryWorkspace
npm --prefix web run build
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/projects web/src/memory
git commit -m "feat: complete Projects and Memory workspaces"
```

### Task 10: Complete Automate and Extend surfaces

**Files:**

- Create: `web/src/automate/RoutinesList.tsx`
- Create: `web/src/automate/RoutineEditor.tsx`
- Create: `web/src/automate/DeliveryHistory.tsx`
- Create: `web/src/automate/ChannelsPanel.tsx`
- Create: `web/src/automate/automate.css`
- Create: `web/src/extend/CapabilityOverview.tsx`
- Create: `web/src/extend/McpPanel.tsx`
- Create: `web/src/extend/SkillsPanel.tsx`
- Create: `web/src/extend/PluginsPanel.tsx`
- Create: `web/src/extend/extend.css`
- Modify: `web/src/automate/AutomateWorkspace.tsx`
- Modify: `web/src/extend/ExtendWorkspace.tsx`

**Interfaces:**

- Automate exposes routines, schedule/timezone, occurrences, channels, destinations, delivery receipts, and uncertain delivery reconciliation.
- Extend exposes built-in capabilities, effective blockers, MCP, skills, plugins, provenance, locks, compatibility, and containment blockers.
- Invariant: installation/review and authority enablement are separate transactions.
- Invariant: delivery language says “idempotent admission and connector receipts,” not universal exactly-once delivery.

- [ ] **Step 1: Write failing truthful-language tests**

```ts
it("does not claim exactly-once external delivery", () => {
  render(<AutomateWorkspace fixture={routineFixture} />);
  expect(screen.queryByText(/exactly once/i)).not.toBeInTheDocument();
  expect(screen.getByText(/connector receipt/i)).toBeVisible();
});

it("keeps plugin review separate from enablement", async () => {
  render(<ExtendWorkspace fixture={pluginFixture} />);
  await user.click(screen.getByRole("button", { name: "Review plugin" }));
  expect(screen.getByRole("button", { name: "Enable plugin" })).toBeDisabled();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- AutomateWorkspace ExtendWorkspace
```

Expected: new panels absent or current copy fails truthful-language assertions.

- [ ] **Step 3: Implement task-first panels**

Retain revision and receipt handling from the extracted legacy components. Default to summary cards and action queues. Keep raw manifests, schemas, provider responses, and logs inside Evidence/Advanced.

- [ ] **Step 4: Run suites**

Run:

```bash
npm --prefix web test -- AutomateWorkspace ExtendWorkspace App
npm --prefix web run build
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/automate web/src/extend
git commit -m "feat: complete Automate and Extend workspaces"
```

---

## Phase 4: Make Settings Truthful and Reversible

### Task 11: Add a server-side effective settings projection

**Files:**

- Create: `src/nested_memvid_agent/effective_settings.py`
- Create: `src/nested_memvid_agent/server_settings_routes.py`
- Create: `tests/test_effective_settings.py`
- Create: `tests/test_server_settings_routes.py`
- Modify: `src/nested_memvid_agent/runtime_settings.py`
- Modify: `src/nested_memvid_agent/server.py`
- Modify: `src/nested_memvid_agent/server_runtime_routes.py`
- Modify: `tests/test_server_runtime_routes.py`

**Interfaces:**

- API: `GET /api/settings` and revision-checked `PUT /api/settings/{setting_id}`.
- Every setting returns ID, category, type, configured value, effective value, blockers, authority/privacy impact, apply timing, revision, provenance, undo availability, allowed values/range, and restart requirement.
- Writes continue through `RuntimeSettingsStore.transactional_update`; feature-specific settings delegate to their owning services.

- [ ] **Step 1: Write failing configured/effective tests**

```python
def test_effective_setting_exposes_parent_blocker(tmp_path: Path) -> None:
    projection = project_settings(
        runtime=runtime_with_web_search_enabled(),
        capabilities=capabilities_with_network_disabled(),
    )
    web_search = projection.require("tools.web_search.enabled")
    assert web_search.configured_value is True
    assert web_search.effective_value is False
    assert web_search.blockers == ("capability:network_disabled",)
    assert web_search.applies == "new_runs"
```

Add a route conflict test requiring `expected_revision` and returning current projection on `409`.

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_effective_settings.py \
  tests/test_server_settings_routes.py \
  tests/test_server_runtime_routes.py
```

Expected: modules/routes absent.

- [ ] **Step 3: Implement the projection and mutation registry**

Define a `SettingDescriptor` registry, but keep values sourced from current stores and managers. Do not introduce a second settings database. Each mutator returns the fresh server projection after commit and any revoked approvals/authority changes.

Categories are exactly: General; Models and providers; Safety and permissions; Storage and memory; Containment; Appearance; Notifications; Updates; Diagnostics; Advanced.

- [ ] **Step 4: Run settings and full Python suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_effective_settings.py \
  tests/test_server_settings_routes.py \
  tests/test_server_runtime_routes.py \
  tests/test_capability_control_plane.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/effective_settings.py \
  src/nested_memvid_agent/server_settings_routes.py \
  src/nested_memvid_agent/runtime_settings.py \
  src/nested_memvid_agent/server.py \
  src/nested_memvid_agent/server_runtime_routes.py \
  tests/test_effective_settings.py \
  tests/test_server_settings_routes.py \
  tests/test_server_runtime_routes.py
git commit -m "feat: expose truthful effective settings"
```

### Task 12: Build the searchable Settings workspace

**Files:**

- Create: `web/src/settings/types.ts`
- Create: `web/src/settings/api.ts`
- Create: `web/src/settings/SettingsIndex.tsx`
- Create: `web/src/settings/SettingsIndex.test.tsx`
- Create: `web/src/settings/SettingControl.tsx`
- Create: `web/src/settings/SettingControl.test.tsx`
- Create: `web/src/settings/SettingsSearch.tsx`
- Create: `web/src/settings/SettingsSearch.test.tsx`
- Create: `web/src/settings/settings.css`
- Modify: `web/src/settings/SettingsWorkspace.tsx`
- Modify: `web/src/app/CommandBar.tsx`

**Interfaces:**

- Search returns settings and the owning feature surface.
- A control shows configured/effective state, blocker, authority impact, timing, provenance, and undo before/after mutation.
- Optimistic UI may show “saving”; it may not show effective success until the server returns the committed projection.

- [ ] **Step 1: Write failing truthfulness and revision tests**

```ts
it("shows configured on but effective blocked", () => {
  renderSetting(blockedWebSearchSetting);
  expect(screen.getByRole("switch")).toBeChecked();
  expect(screen.getByText("Currently blocked")).toBeVisible();
  expect(screen.getByText("Network capability is disabled")).toBeVisible();
});

it("recovers from a revision conflict with server truth", async () => {
  server.respondNext(409, newerSettingProjection);
  await user.click(screen.getByRole("switch"));
  expect(await screen.findByText("Changed elsewhere; review the current value")).toBeVisible();
  expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- SettingsIndex SettingControl SettingsSearch
```

Expected: modules absent.

- [ ] **Step 3: Implement typed controls**

Render boolean, enum, bounded number, path, duration, and read-only evidence controls from server descriptors. Use native folder pickers for paths in Desktop. Raw JSON export/import stays in Advanced and never becomes the primary editor.

- [ ] **Step 4: Run server, renderer, and build tests**

Run:

```bash
npm --prefix web test -- Settings
npm --prefix web run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_effective_settings.py \
  tests/test_server_settings_routes.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/settings web/src/app/CommandBar.tsx
git commit -m "feat: add searchable truthful Settings workspace"
```

---

## Phase 5: Accessibility, Responsive Behavior, and Rendered Validation

### Task 13: Add application-wide accessibility gates

**Files:**

- Create: `web/src/accessibility/a11y.test.tsx`
- Create: `web/src/accessibility/keyboardJourneys.test.tsx`
- Create: `web/src/accessibility/reducedMotion.test.ts`
- Create: `web/src/accessibility/contrast.test.ts`
- Modify: `web/src/styles.css`
- Modify: `web/src/design/tokens.css`
- Modify: affected workspace components

**Interfaces:**

- Axe coverage for all seven destinations, Setup Center, Mission preflight, active Mission, approval packet, Settings blocker, and recovery state.
- Keyboard journeys for setup and first mission.
- Programmatic contrast checks for text/control token pairs.

- [ ] **Step 1: Add gates and run them red**

```ts
it.each(allPrimaryScreens)("%s has no serious axe violations", async (_, screen) => {
  const { container } = render(screen);
  const report = await axe(container);
  expect(report.violations.filter(isSerious)).toEqual([]);
});
```

Run:

```bash
npm --prefix web test -- a11y keyboardJourneys reducedMotion contrast
```

Expected: initial failures identify missing labels, landmarks, focus behavior, or contrast.

- [ ] **Step 2: Fix violations at primitive or owning feature level**

Do not suppress axe rules globally. Add visible labels, descriptions, status text, focus restoration, live regions only for meaningful state changes, and table captions. Decorative flight/field shapes must be `aria-hidden`.

- [ ] **Step 3: Run complete renderer gate**

Run:

```bash
npm --prefix web test
npm --prefix web run build
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add web/src/accessibility web/src/styles.css web/src/design \
  web/src/app web/src/mission web/src/setup web/src/projects web/src/memory \
  web/src/automate web/src/extend web/src/settings
git commit -m "test: enforce Workbench accessibility journeys"
```

### Task 14: Add installed-renderer visual and narrow-window validation

**Files:**

- Create: `desktop/e2e/workbench.spec.ts`
- Create: `desktop/e2e/fixtures.ts`
- Create: `desktop/e2e/snapshots/README.md`
- Create: `desktop/playwright.config.ts`
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_packaging_deployment.py`

**Interfaces:**

- Launch the built Electron directory against deterministic Demo fixtures.
- Capture light/dark Mission, Setup, Settings, Flock placeholder, approval, and recovery at 1440×960 and 960×720.
- Invariant: committed baselines are platform-normalized screenshots or DOM/style assertions with a documented tolerance; font loading must complete before capture.

- [ ] **Step 1: Write failing Electron journey**

```ts
test("first useful mission is keyboard-completable", async () => {
  const app = await launchKestrelDeveloperBundle();
  const page = await app.firstWindow();
  await completeSetupWithDemoByKeyboard(page);
  await launchMissionByKeyboard(page, "Summarize this project");
  await expect(page.getByText("Mission started")).toBeVisible();
  await expect(page.locator("body")).not.toHaveCSS("overflow-x", "scroll");
});
```

- [ ] **Step 2: Run and verify the initial failure**

Run:

```bash
npm --prefix desktop run e2e -- workbench
```

Expected: E2E config/dependency absent.

- [ ] **Step 3: Add pinned Electron Playwright support and deterministic fixtures**

Pin the exact tested Playwright dependency in `desktop/package-lock.json`. Never call a live provider. Use the bundled Demo provider and a temporary owner-data directory. Disable animations for screenshot capture through the supported reduced-motion setting, not by injecting arbitrary CSS into production pages.

- [ ] **Step 4: Run rendered validation**

Run:

```bash
npm --prefix desktop run e2e
npm --prefix web test
npm --prefix web run build
npm --prefix desktop test
npm --prefix desktop run build
```

Expected: keyboard journey, both themes, narrow window, and screenshots pass.

- [ ] **Step 5: Commit**

```bash
git add desktop/e2e desktop/playwright.config.ts \
  desktop/package.json desktop/package-lock.json \
  .github/workflows/ci.yml tests/test_packaging_deployment.py
git commit -m "test: validate rendered Wildflower Workbench"
```

---

## Final Verification

- [ ] Run all renderer gates:

```bash
npm --prefix web run licenses:check
npm --prefix web run test:typecheck
npm --prefix web test
npm --prefix web run build
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run build
npm --prefix desktop run e2e
```

- [ ] Run all Python gates affected by setup/settings:

```bash
uv run python -m compileall -q src tests
uv run ruff check src tests
uv run mypy src
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

- [ ] Manually inspect the exact built renderer, not the Vite source server:

  - fresh offline owner reaches working Demo mode;
  - Setup Center stays available after completion;
  - a project can be added with a native folder picker and reviewed authority;
  - Mission is the default home and can launch a safe first task;
  - every existing feature is reachable through one of seven destinations;
  - configured/effective/blocker state is truthful;
  - raw JSON is not required for setup;
  - no horizontal overflow occurs at 960×720;
  - light and dark themes retain Wildflower character;
  - keyboard and screen-reader landmarks are coherent;
  - reduced motion removes ambient movement;
  - Desktop raw credentials and API tokens do not enter the primary renderer.

- [ ] Inspect for duplicated authority and obsolete navigation:

```bash
git diff --check
rg -n 'type AppSection|\"chat\" \\| \"outcomes\"|kestrel\\.setup\\.dismissed' web/src
rg -n 'fetch\\(|postJson\\(|putJson\\(|deleteJson\\(' web/src | sort
rg -n 'https?://' web/src web/public --glob '!THIRD_PARTY_NOTICES.txt'
git status --short
```

Expected: no old competing top-level section type; setup dismissal is presentation-only if retained; all remote asset matches are absent.

- [ ] Record the exact final commit SHA and rendered validation receipt in the program index.

## Completion Criteria

- Kestrel opens to Mission Command with seven stable destinations.
- A new owner can complete setup and a Demo mission without terminal knowledge.
- All current features are reachable through Mission, Projects, Memory, Flock, Automate, Extend, or Settings.
- Setup, project creation, provider selection, safety review, and settings are friendly but preserve server-side authority.
- Wildflower Workshop is visibly warm, expressive, tactile, and non-generic in light and dark modes.
- Every primary journey passes keyboard, screen-reader, contrast, reduced-motion, and narrow-window gates.
- The UI distinguishes configured state from effective authority and exposes exact blockers.
- Browser Workbench compatibility remains intact.
- LAN discovery and qualification can land later inside the already stable Flock routes without another shell rewrite.
