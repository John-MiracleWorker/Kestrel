# Kestrel GUI-First and Adaptive Flock Implementation Handoff

**Handoff date:** 2026-07-31  
**Intended reader:** the next coding agent continuing implementation  
**Authoritative implementation worktree:** `/Volumes/12.45/Codex-Offload/kestrel-gui-first-integration`  
**Branch:** `feat/gui-first-kestrel-desktop`  
**Current committed HEAD:** `fe4af586b59ec6ec1dd0fb044795d49460245cd0`  
**Current task:** Wildflower Workbench Task 8, implemented and tested but not approved or committed

## 1. Start here

Do not work in `/Users/tiuni/kestrel`. That primary checkout is a separate dirty worktree on `fix/approval-continuation-capability` at `31c38e9` and contains user-owned state.

Continue only in:

```bash
cd /Volumes/12.45/Codex-Offload/kestrel-gui-first-integration
git branch --show-current
git rev-parse HEAD
git status --short
```

Expected branch and HEAD:

```text
feat/gui-first-kestrel-desktop
fe4af586b59ec6ec1dd0fb044795d49460245cd0
```

Expected status is dirty because Task 8 is deliberately uncommitted. Do not reset, restore, checkout, clean, or overwrite these files.

Read these sources before editing:

1. [`AGENTS.md`](../AGENTS.md)
2. [Program index](superpowers/plans/2026-07-29-gui-first-flock-program-index.md)
3. [Wildflower Workbench plan](superpowers/plans/2026-07-29-wildflower-workbench.md)
4. [Wildflower execution ledger](../.superpowers/sdd/2026-07-31-wildflower-workbench/progress.md)
5. [Desktop foundation ledger](../.superpowers/sdd/2026-07-29-gui-first-desktop-foundation/progress.md)
6. The approved source specifications linked from the program index.

## 2. Product decisions that are already approved

Do not reopen these decisions unless the owner explicitly changes them:

- Supported product profile: one trusted owner on one local or privately networked Kestrel node.
- Desktop targets: macOS, Windows, and Linux.
- Distribution: fully bundled core; a clean user should not need Python, Node, or terminal setup.
- Everyday product direction: GUI-first Wildflower Workshop, while retaining the conversational CLI and advanced operator compatibility.
- Stable destinations: Mission, Projects, Memory, Flock, Automate, Extend, and Settings.
- Setup: a permanent five-stage Setup Center, not a dismissible onboarding modal.
- Local provider discovery must include explicitly initiated LAN discovery for models served by other computers on the same private network.
- Worker count must be owner-tunable within bounded policy.
- Adaptive Flock requires evidence-gated qualification and separate exact owner activation.
- Installation, updates, recovery, and uninstall must eventually be native, reversible, and qualified on all three platforms.
- No public release, production activation, tag, release entry, or update-feed publication is authorized by this handoff.

## 3. Non-negotiable engineering boundaries

Preserve all of the following throughout implementation:

- Memvid v2 `.mv2` only.
- One `.mv2` per permanent nested memory layer unless a test proves a different layout.
- Never call `create(path)` on an existing `.mv2` file.
- SQLite remains the authoritative control plane; Memvid remains canonical memory.
- The CLI remains conversational and deterministic mocks remain usable.
- No policy-memory promotion from one ordinary event, qualification, or activation.
- Every promotion retains evidence, provenance, confidence, and validation status.
- High-risk tools require explicit configuration and exact-call owner approval.
- Renderer/UI code never becomes an authority source or bypass.
- Discovery, installation, scanning, qualification, or environment flags never imply enablement.
- Secrets and bearer tokens never enter renderer state, logs, model context, SQLite evidence, Memvid, or support bundles.
- Preserve approval bindings, capability revalidation, lease fencing, terminal immutability, rollback, and unknown-side-effect handling.
- Use exact release-state vocabulary from the program index. Local tests are not signed-artifact or public-release evidence.
- Run `pytest -q` after every completed phase; Memvid integration remains behind `RUN_MEMVID_INTEGRATION=1`.

Use the repository interpreter, never bare `python`:

```bash
export PATH="$PWD/.venv/bin:/Users/tiuni/.nvm/versions/node/v22.16.0/bin:/Users/tiuni/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"
```

## 4. Work completed and committed

### 4.1 Milestone 1: Desktop lifecycle foundation — complete

All 11 tasks in the [Desktop Foundation plan](superpowers/plans/2026-07-29-gui-first-desktop-foundation.md) are complete through `8e55acd8979cdfebae3ba0cb45b1742b1e1cd14b`.

Delivered:

- pinned Electron main/preload workspace;
- bounded sidecar bootstrap and authenticated readiness;
- portable single-writer profile lease shared by Desktop and CLI;
- frozen-compatible port-zero sidecar;
- private custom-protocol window and sender trust;
- signed resource-stage verification and bounded one-restart supervision;
- main-process loopback auth injection;
- narrow schema-validated preload bridge;
- isolated credential flow and platform-keyring readiness projection;
- recovery/reconciliation APIs;
- current-platform unsigned developer directory bundle and real lifecycle smoke.

Exact-source current-platform developer-bundle evidence at `8e55acd` proved two packaged lifecycle cycles, Mission Command load, authenticated readiness/recovery/shutdown, exactly six reopened Memvid v2 layers, clean process/listener exit, full Python/Web/Desktop gates, and independent review approval. See the Desktop ledger for receipt paths and digests.

Honest boundary: this is **developer bundle complete** for the current platform. It is not a signed installer, all-platform qualification, native credential-persistence claim, update/rollback/uninstall qualification, production release candidate, or public release.

### 4.2 Milestone 2: Wildflower Workbench Tasks 1–7 — complete

| Task | Result | Commit |
|---|---|---|
| 1 | Froze legacy Workbench request/behavior contracts | `0cb926c899bee7bff1dca7d4ef2293014a63a7ae` |
| 2 | Established seven-destination shell, stable routing, and Desktop/browser route parity | `d00ecec62304da345aff124eb02e01e9246fe4a8` |
| 3 | Split feature workspaces and lifecycle ownership without weakening core refresh paths | `cdb5668` |
| 4 | Added bundled Wildflower fonts, semantic light/dark themes, motion preferences, and license verification | `e6a6533bca0bb6f0693230541291485ddcf1050b` |
| 5 | Added accessible Wildflower primitives and compatibility seams | `e7de44f279c266ad261e980583af37948664d284` |
| 6 | Added Mission Command shell, global destination palette, context rail, responsive layout, and modal/focus hardening | `6889047977ea230757f2bf8b7d9e9db7a13a3dd2` |
| 7 | Replaced onboarding with permanent server-authoritative five-stage Setup Center | `fe4af586b59ec6ec1dd0fb044795d49460245cd0` |

Each committed task received focused tests, full Web/build checks, rendered QA where applicable, full Python phase validation, and independent review. The detailed reasoning, review findings, remediations, and exact gates are in the Wildflower ledger.

## 5. Current uncommitted Task 8 state

Task 8 is [“Redesign Mission as the everyday command surface”](superpowers/plans/2026-07-29-wildflower-workbench.md#task-8-redesign-mission-as-the-everyday-command-surface).

### 5.1 What is already implemented

- Progressive Mission states: compose, preflight, active, needs-owner, reviewing, completed, and blocked.
- Project/template/objective composer with editable acceptance plan.
- Server-backed read-only mission preflight with route, budget, capability, index, provider, validation, rollback, blockers, and recovery.
- Shared AppShell context-rail portal with a standalone fallback.
- Active mission conversation, follow-up composer, task/subagent activity, approvals, durable timeline, repair review, and engineering evidence.
- Exact-call approval cards with target, capability revision, resource digest, expiry, consequence, and raw evidence disclosure.
- Raw mission, repair, and engineering records moved into `EvidenceDrawer` disclosures.
- Engineering approval-packet binding validation and fail-closed approval controls.
- Repair signed-receipt disclosure.
- Wildflower Mission/approval/active/context styling in light/dark semantic tokens.
- Existing mission launch, project revision, launch-binding, approval, repair, and engineering API contracts retained.

New files:

```text
web/src/mission/ActiveMission.tsx
web/src/mission/ActiveMission.test.tsx
web/src/mission/ApprovalQueue.tsx
web/src/mission/ApprovalQueue.test.tsx
web/src/mission/EvidenceDrawer.tsx
web/src/mission/EvidenceDrawer.test.tsx
web/src/mission/MissionPreflightCard.tsx
web/src/mission/MissionPreflightCard.test.tsx
web/src/mission/ObjectiveComposer.tsx
web/src/mission/ObjectiveComposer.test.tsx
```

Modified files:

```text
web/src/App.test.tsx
web/src/LegacyWorkbench.tsx
web/src/app/AppShell.tsx
web/src/app/AppShell.test.tsx
web/src/engineering/EngineeringRunPanel.tsx
web/src/engineering/EngineeringRunPanel.test.tsx
web/src/engineering/engineering.css
web/src/mission/MissionControl.tsx
web/src/mission/MissionControl.test.tsx
web/src/mission/mission.css
web/src/mission/types.ts
web/src/projects/ProjectsWorkspace.test.tsx
web/src/repair/RepairReviewPanel.tsx
web/src/repair/RepairReviewPanel.test.tsx
web/src/types.ts
```

### 5.2 Validation already completed on the current uncommitted tree

- Focused combined frontend regression: **11 files / 100 tests passed**.
- Full Web suite: **40 files / 258 tests passed**.
- Vite production build passed.
- Full Python suite reached 100% and exited 0 with expected opt-in skips and the pre-existing Starlette/httpx deprecation warning.
- `git diff --check` passed.
- Rendered browser QA passed at the normal wide viewport and at `960x720`:
  - no horizontal layout break was observed;
  - the context rail collapsed at compact width;
  - “Show mission context” reopened the complete preflight drawer;
  - the current console contained no runtime errors.
- Known nonblocking diagnostics remain unchanged:
  - jsdom reports unimplemented `HTMLCanvasElement.getContext()` without the optional canvas package;
  - Vite reports an existing bundle chunk larger than 500 kB.

Two local preview listeners were still present when this handoff was written:

```text
127.0.0.1:8765  python PID 5332  Kestrel API
127.0.0.1:4179  node PID 3647    Vite preview
```

Treat those PIDs as historical observations. Reinspect listener ownership before using or stopping either process; never kill by port alone.

### 5.3 Independent review verdict — changes required

Task 8 is **not approved**. The independent review found four P1s and three P2s. Fix these before committing.

#### P1-1: API-shaped resource digests are rejected

`ApprovalQueue` accepts only 64 bare hex characters, while the runtime emits `sha256:<64 hex>`. The frontend test fixture masks the mismatch.

Where:

- [`ApprovalQueue.tsx`](../web/src/mission/ApprovalQueue.tsx)
- [`ApprovalQueue.test.tsx`](../web/src/mission/ApprovalQueue.test.tsx)
- backend reference: `src/nested_memvid_agent/run_manager.py` around the approval packet/resource-digest emission
- backend contract test: `tests/test_capability_control_plane.py`

How:

1. Add a failing frontend test using the exact API-shaped `sha256:<64 hex>` digest.
2. Make immutable-binding validation accept the canonical backend format while remaining fail-closed for malformed values.
3. Keep the full digest visible on the approval card.

#### P1-2: expired approvals remain actionable and can report false success

The UI verifies only that expiry parses, not that it remains in the future. `LegacyWorkbench` also reports “Approval accepted” without checking the returned approval status.

Where:

- [`ApprovalQueue.tsx`](../web/src/mission/ApprovalQueue.tsx)
- [`ApprovalQueue.test.tsx`](../web/src/mission/ApprovalQueue.test.tsx)
- [`LegacyWorkbench.tsx`](../web/src/LegacyWorkbench.tsx)
- backend behavior: `src/nested_memvid_agent/run_manager.py` approval-decision path

How:

1. Use a deterministic clock in tests.
2. Prove an expired pending packet is visibly expired and cannot be approved.
3. Preserve denial as the safe exit path.
4. Inspect the server response and show approval success only when its status is actually `approved`; report expired/stale/denied truthfully.

#### P1-3: preflight races can repopulate stale launch authority

Review requests are not invalidated or generation-bound. An older request can win after project/objective/template edits; a failed re-review can also leave an earlier `can_start` projection available.

Where:

- [`MissionControl.tsx`](../web/src/mission/MissionControl.tsx)
- [`MissionControl.test.tsx`](../web/src/mission/MissionControl.test.tsx)
- [`MissionPreflightCard.tsx`](../web/src/mission/MissionPreflightCard.tsx)

How:

1. Add failing tests for out-of-order responses, failed re-review, objective/template/project changes, and refreshed project revision drift.
2. Invalidate launch authority immediately when a review starts or any bound input changes.
3. Abort the prior request and/or gate each response with a monotonically increasing generation.
4. Accept a projection only for the current project, revision, objective, template, and reviewed plan.
5. Require the accepted projection/binding to still match current inputs in both the Start-button predicate and `launchMission`.

#### P1-4: exact arguments can be approved without being seen

The plan requires exact arguments to be shown before decision, but they are only in a collapsed evidence drawer while Approve is immediately enabled.

Where:

- [`ApprovalQueue.tsx`](../web/src/mission/ApprovalQueue.tsx)
- [`EvidenceDrawer.tsx`](../web/src/mission/EvidenceDrawer.tsx)
- [`ApprovalQueue.test.tsx`](../web/src/mission/ApprovalQueue.test.tsx)

How:

1. Show a canonical complete argument summary directly on the card, or require an explicit disclosure acknowledgment before enabling approval.
2. Retain the full raw JSON record in `EvidenceDrawer` for forensic inspection.
3. Test that the owner cannot approve before the complete exact arguments are available for review.

#### P2-1: active-run context is misleading after reload

An existing run with no in-memory preflight shows route/budget/provider/validation/rollback as “Not inspected” and “Review required,” even though the view is an active durable mission.

Where:

- [`MissionControl.tsx`](../web/src/mission/MissionControl.tsx)
- [`MissionPreflightCard.tsx`](../web/src/mission/MissionPreflightCard.tsx)

How:

- Render a distinct active-run authority snapshot using durable run/project/binding evidence.
- If the launch-time binding is not persisted in the current Run projection, state that absence explicitly instead of substituting compose-time preflight language or fabricating authority.
- Add a reload test with an existing active run.

#### P2-2: approval decisions are not serialized

`ApprovalQueue` supports `pendingApprovalId`, but `ActiveMission` never supplies it. Rapid conflicting clicks can issue approve and deny requests; backend first-decision immutability limits authority but UI notices can still become wrong.

Where:

- [`ActiveMission.tsx`](../web/src/mission/ActiveMission.tsx)
- [`ApprovalQueue.tsx`](../web/src/mission/ApprovalQueue.tsx)
- [`LegacyWorkbench.tsx`](../web/src/LegacyWorkbench.tsx)

How:

- Track a pending approval ID around an awaited decision.
- Disable both decision controls for that packet while pending.
- Use the committed server response to determine the notice and refreshed state.
- Add a rapid conflicting-click regression.

#### P2-3: mission follow-up failures are silent

The follow-up submit path has `finally` but no catch/error UI; the parent continuation is not wrapped by its existing guarded error path.

Where:

- [`ActiveMission.tsx`](../web/src/mission/ActiveMission.tsx)
- [`ActiveMission.test.tsx`](../web/src/mission/ActiveMission.test.tsx)
- [`LegacyWorkbench.tsx`](../web/src/LegacyWorkbench.tsx)

How:

- Add network and authentication rejection tests.
- Surface a stable inline error and preserve the owner’s draft for retry.
- Route authentication failures through the existing auth-recovery path.
- Do not create an unhandled rejected event.

### 5.4 Additional Task 8 coverage still required

The reviewer also noted these acceptance gaps:

- candidate selection is not exercised; the Engineering fixture returns an empty fan-out list;
- add one end-to-end safe Demo test from objective review through `POST /api/runs`;
- automate the narrow-window journey later under Task 14 even though a manual `960x720` browser pass has been completed;
- prove active-run authority after reload rather than only the compose/preflight journey.

## 6. Exact Task 8 continuation sequence

Follow test-driven development. Do not commit first and “fix forward.”

1. Reconfirm worktree, branch, HEAD, and dirty-file inventory.
2. Read the seven review findings above against the live code.
3. Add focused failing regressions for all four P1s, then implement the smallest trust-preserving repairs.
4. Add the three P2 regressions and repairs.
5. Add candidate-selection, safe-Demo end-to-end, and active-reload coverage.
6. Run the focused suite:

```bash
cd /Volumes/12.45/Codex-Offload/kestrel-gui-first-integration/web
npm test -- --run \
  src/mission/MissionControl.test.tsx \
  src/mission/ObjectiveComposer.test.tsx \
  src/mission/MissionPreflightCard.test.tsx \
  src/mission/ActiveMission.test.tsx \
  src/mission/ApprovalQueue.test.tsx \
  src/mission/EvidenceDrawer.test.tsx \
  src/engineering/EngineeringRunPanel.test.tsx \
  src/repair/RepairReviewPanel.test.tsx \
  src/App.test.tsx \
  src/projects/ProjectsWorkspace.test.tsx \
  src/app/AppShell.test.tsx
```

7. Run the full Web and build gates:

```bash
npm test -- --run
npm run licenses:check
npm run build
```

8. Re-run rendered wide and `960x720` Mission QA, including exact-argument approval review, expired approval, active-run reload, and context drawer open/close. Inspect current browser console errors, not stale HMR history.
9. Run the full Python phase gate from the repository root:

```bash
cd /Volumes/12.45/Codex-Offload/kestrel-gui-first-integration
export PATH="$PWD/.venv/bin:/Users/tiuni/.nvm/versions/node/v22.16.0/bin:/Users/tiuni/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
git diff --check
```

10. Request a fresh independent review of the entire diff from `fe4af586...`, explicitly asking the reviewer to recheck all seven findings and the new tests.
11. Address every actionable P0/P1/P2 finding and rerun affected/full gates.
12. Update `.superpowers/sdd/2026-07-31-wildflower-workbench/progress.md` with RED/GREEN evidence, rendered QA, findings, remediation, exact validation, and final review verdict.
13. Only after approval, stage the complete Task 8 scope shown by `git status --short` and commit:

```bash
git commit -m "feat: make Mission the primary task surface"
```

14. Confirm a clean status and exact new HEAD before starting Task 9.

## 7. Remaining implementation program

The Desktop foundation is complete. The unfinished program is:

### 7.1 Finish Wildflower Workbench — Tasks 8–14

Detailed instructions, exact files, RED tests, GREEN commands, and commit messages are in the [Wildflower plan](superpowers/plans/2026-07-29-wildflower-workbench.md).

- **Task 8:** fix review findings, qualify, and commit the current Mission surface.
- **Task 9:** complete Projects and Memory product surfaces in `web/src/projects` and `web/src/memory`; expose project authority/index/recipes and human-readable memory provenance/promotion state without raw-JSON dependence.
- **Task 10:** complete Automate and Extend in `web/src/automate` and `web/src/extend`; keep routines, plugins, skills, MCP, capability enablement, provenance, and failures truthful and reversible.
- **Task 11:** add one server-side effective-settings projection in the Python runtime/API. Configured, effective, inherited, restart-required, and blocked values must remain distinct.
- **Task 12:** build the searchable Settings workspace in `web/src/settings`; use progressive disclosure and reversible edits rather than several CLI commands.
- **Task 13:** add application-wide accessibility gates: axe, keyboard-only primary journeys, stable focus, semantic status, contrast, and reduced motion.
- **Task 14:** add installed-renderer visual and narrow-window validation in Desktop/Electron; qualify light/dark, wide/narrow, setup, mission, approval, and recovery journeys.

Do not begin LAN controls while their Flock routes are still truthful unavailable/dependency cards. Complete the Workbench milestone first.

### 7.2 Explicit LAN model discovery — 10 tasks

Execute [the LAN plan](superpowers/plans/2026-07-29-explicit-lan-model-discovery.md) in order.

Primary code locations:

- Python private-network primitives, routing storage/migrations, probing, scan manager, API/SSE under `src/nested_memvid_agent` and its routing modules;
- routing schema migration **v2 to v3**;
- typed client/state and Flock provider scan/review UI under `web/src/flock`;
- backend, adversarial transport, UI, and controlled two-machine tests under `tests` and `web/src/flock`.

Critical rules:

- no automatic scan;
- explicit interface and private scope preview;
- maximum 256 IPv4 hosts, four known ports per host, 16 probes, 45-second total deadline, and zero redirects;
- literal private addresses only; no public/wide/arbitrary scan or redirect SSRF;
- every discovered target imports as disabled/unconfirmed;
- separate owner trust, role, privacy, and enablement decision;
- endpoint/model/certificate/capability drift removes eligibility;
- installed-app evidence must find a controlled server on another private-network computer.

### 7.3 Adaptive Flock qualification and scoped activation — 23 tasks

Execute [the Adaptive Flock plan](superpowers/plans/2026-07-29-adaptive-flock-qualification-activation.md) only after LAN schema v3 has merged.

Primary code locations:

- routing models, ledger migration **v3 to v4**, receipt authentication, qualification manager/executor/metrics/replay, grants, coordinator gates, and APIs under `src/nested_memvid_agent/routing` and modular server routes;
- deterministic corpus fixtures and real-project import tests under `tests`/fixtures;
- typed qualification/activation client and UI under `web/src/flock`;
- installed GUI and separately authorized live-provider qualification runners.

Critical rules:

- qualification snapshots every eligible target and every exclusion;
- default hard cap USD 50.00 is owner-editable before launch and can never increase after start;
- missing price or usage is not free;
- qualification alone creates no routing authority;
- exact grants are owner-created per low/medium scope;
- high-risk learned activation remains zero;
- ordinary learned routing requires a currently effective durable grant;
- material drift, revocation, or kill switch causes durable deterministic fallback;
- 20/20 replay must have one projection digest;
- no policy-memory write or other authority expansion;
- production routing claims require a separately authorized installed-artifact receipt with two real eligible targets, real project evidence, attributable cost, and all guardrails.

### 7.4 Cross-platform packaging, update, rollback, and uninstall — 15 tasks

Execute [the packaging plan](superpowers/plans/2026-07-29-desktop-packaging-updates-recovery.md) after the product, LAN, and qualification tracks are integrated.

Primary code locations:

- `desktop` builder configuration, main/preload updater/recovery code, platform assets, and packaging tests;
- PyInstaller sidecar specs and resource manifest/SBOM tooling;
- platform-native signing/notarization/signature workflows;
- protected GitHub rehearsal/release workflows and exact-SHA qualification receipts;
- documentation listed in the program index.

Required artifacts and gates:

- macOS DMG/ZIP, Windows NSIS, Linux AppImage, then `.deb`/`.rpm` as planned;
- native builds on their target OS/architecture; no cross-compilation claim;
- signed inner sidecar/resources and outer artifact verification;
- isolated packaged credential behavior;
- opt-in signed update manifest;
- idle preflight, consistent state/key snapshot, owner-confirmed install, external watchdog, and matching-byte/state rollback;
- uninstall removes app/integration but preserves owner state/memory by default;
- clean-machine install/run/update/forced-failure/rollback/uninstall;
- 20 lifecycle cycles per primary artifact with zero orphan residue;
- no public publication without a separate explicit owner instruction.

### 7.5 Final integrated qualification

Run Milestone 6 from the [program index](superpowers/plans/2026-07-29-gui-first-flock-program-index.md) at one exact final SHA:

- source, lock, compile, Ruff, mypy, Bandit, full Python, Web, Desktop, license, build, and workflow gates;
- gated Memvid and MCP integration;
- 20-repeat routing/qualification determinism;
- all installed Electron journeys from packaged artifacts;
- all clean-machine native receipts;
- separately authorized controlled LAN and live-provider evidence;
- manual final visual/interaction inspection per platform;
- exact SHA and receipt digests in the release checklist.

Passing those gates produces a **production release candidate**, not a public release. Publication remains a separate owner-authorized operation.

## 8. Integration and collision rules

Do not change the planned order:

```text
Desktop foundation (complete)
  -> Wildflower Workbench (Task 8 in progress; Tasks 9–14 pending)
  -> LAN discovery, routing schema v2 -> v3
  -> Adaptive Flock, routing schema v3 -> v4
  -> packaging/update/recovery
  -> final integrated qualification
```

Important shared surfaces:

- Keep server route registration modular; do not put feature logic back into `server.py`.
- LAN owns schema v3 before Adaptive Flock adds v4. Never hand-merge or reorder migrations.
- Regenerate `uv.lock` after dependency changes; never hand-merge lock records.
- Keep exact Desktop pins and use one npm version for lock regeneration.
- LAN/Flock UI mounts through `web/src/flock`, not back into `App.tsx`.
- Reuse existing provider capability evidence; LAN adds transport/scope, not a competing vocabulary.
- One runtime settings store/projection owns GUI settings, Flock kill switches, and updater opt-in.
- Back up SQLite state and the routing receipt key together; Memvid stays separately manifest-bound.
- Add CI/release gates without weakening existing Python, security, Memvid, Web, Desktop, Docker, or CodeQL gates.

## 9. Working practices that prevented regressions

- Use one bounded task/commit at a time.
- Write the failing regression before repairing a review finding.
- Run focused tests while iterating, then full Web/build/Python at the task boundary.
- Perform rendered browser QA for UI work; static/jsdom checks are preflight, not visual evidence.
- Ask an independent reviewer to report only actionable P0/P1/P2 findings with file-and-line evidence.
- Treat review findings as hypotheses to verify against source, then fix with tests.
- Record exact RED, GREEN, rendered, review, and commit evidence in the ledger.
- Recheck exact HEAD after the final validation; a green suite from an earlier commit is stale evidence.
- Inspect process/listener ownership before stopping any preview or sidecar.
- Preserve unrelated dirty worktree changes.

## 10. Copy-paste prompt for the next coding agent

```text
Continue the approved Kestrel GUI-first implementation in
/Volumes/12.45/Codex-Offload/kestrel-gui-first-integration on branch
feat/gui-first-kestrel-desktop. Read AGENTS.md and
docs/CODEX_GUI_FIRST_IMPLEMENTATION_HANDOFF_2026-07-31.md completely.

Do not work in /Users/tiuni/kestrel and do not discard the uncommitted Task 8
files. Confirm HEAD is fe4af586b59ec6ec1dd0fb044795d49460245cd0.

Resume Wildflower Workbench Task 8. First reproduce and repair all four P1 and
three P2 independent-review findings in the handoff using TDD. Add the missing
candidate-selection, safe-Demo end-to-end, active-run reload, failure, race,
expiry, API-shaped digest, exact-argument, and decision-serialization coverage.
Run focused tests, full Web tests/licenses/build, rendered wide and 960x720 QA,
the full Python phase gate, and git diff --check. Obtain a clean independent
re-review, update the Wildflower SDD ledger, and only then commit with:

feat: make Mission the primary task surface

After a clean exact HEAD, continue Tasks 9–14 from the Wildflower plan in order.
Then execute LAN v3, Adaptive Flock v4, packaging, and final qualification in
the program-index order. Preserve Memvid v2, SQLite control-plane authority,
exact approvals, local/private single-owner scope, deterministic fallback,
reversibility, provenance, and honest release-state boundaries. Do not publish
or production-activate anything without explicit owner authorization.
```

## 11. Current truth in one paragraph

Kestrel now has a qualified current-platform unsigned Desktop foundation and seven committed, independently reviewed Wildflower product slices through the permanent Setup Center. The new Mission-primary surface exists and passes its current focused/full Web, build, Python, diff, and manual responsive browser gates, but independent review correctly rejected it for four approval/preflight P1 defects and three active-state/concurrency/error P2 defects. Therefore Task 8 must be repaired and re-reviewed before commit. Projects/Memory, Automate/Extend, effective/searchable Settings, application-wide accessibility, installed-renderer QA, explicit LAN discovery, Adaptive Flock qualification/activation, signed cross-platform packaging/update/rollback/uninstall, and final exact-artifact qualification remain unfinished. Nothing has been publicly released or authorized for production learned routing.
