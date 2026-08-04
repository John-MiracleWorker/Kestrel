# GUI-First Kestrel and Adaptive Flock Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved local/private/single-owner Kestrel product as a fully bundled, GUI-first desktop application for macOS, Windows, and Linux, including explicit LAN model discovery and evidence-gated Adaptive Flock production routing.

**Architecture:** One authoritative frozen Python sidecar owns every Kestrel behavior and trust boundary. A hardened Electron shell provides native lifecycle and a Wildflower Workshop React product surface. Explicit LAN discovery creates disabled evidence-backed targets. Adaptive Flock qualification converts bounded hybrid-corpus evidence into immutable receipts, then separate owner decisions create exact scoped grants. Native signed artifacts package the integrated system and qualify install, update, rollback, and uninstall on clean machines.

**Tech Stack:** Python 3.11, Memvid v2, SQLite, FastAPI/Uvicorn, existing Kestrel routing/runtime, React 19, TypeScript 5.9, Vite 7, Electron 43, electron-builder 26, PyInstaller 6.21, Vitest/Testing Library/axe, native platform signing, and GitHub Actions.

## Approved Source Specifications

- [GUI-First Desktop Product Design](../specs/2026-07-29-gui-first-desktop-product-design.md)
- [Adaptive Flock Production Qualification and Scoped Activation Design](../specs/2026-07-29-adaptive-flock-production-qualification-design.md)

The supported release profile remains:

- one trusted owner;
- one local or privately networked Kestrel node;
- no hosted accounts, tenants, or multi-user authorization;
- local-first canonical Memvid v2 memory;
- SQLite control-plane state;
- exact-call approvals and explicit capability enablement;
- deterministic fallback whenever learned authority is absent.

## Detailed Track Plans

1. [GUI-First Desktop Foundation](2026-07-29-gui-first-desktop-foundation.md) — 11 tasks
2. [Wildflower Workbench and GUI-First Product](2026-07-29-wildflower-workbench.md) — 14 tasks
3. [Explicit LAN Model Discovery](2026-07-29-explicit-lan-model-discovery.md) — 10 tasks
4. [Adaptive Flock Qualification and Scoped Activation](2026-07-29-adaptive-flock-qualification-activation.md) — 23 tasks
5. [Cross-Platform Desktop Packaging, Updates, and Recovery](2026-07-29-desktop-packaging-updates-recovery.md) — 15 tasks

Total: 11 + 14 + 10 + 23 + 15 = **73 implementation tasks**, each independently checkable with red/green tests, phase gates, and commit boundaries.

## Global Constraints

- Execute in clean worktrees. The dirty primary checkout is not an implementation target.
- Use the exact integration sequence in this index even if independent tasks are developed in separate worktrees.
- Preserve all non-negotiables in `AGENTS.md`.
- Keep one `.mv2` file per permanent memory layer and Memvid v2 only.
- Never call `create(path)` on an existing `.mv2`.
- Keep the existing conversational CLI and deterministic mocks working throughout.
- Never use Electron or React as an authority bypass.
- Never infer provider, LAN, plugin, MCP, containment, learned-routing, or dangerous-tool authority from discovery/installation alone.
- Never store raw secrets in renderer state, model context, logs, events, support bundles, SQLite evidence, or Memvid.
- No policy memory write from a single ordinary event, qualification run, or activation.
- Every high-risk call remains configuration-gated and exact-call approved.
- Every memory promotion retains evidence, provenance, confidence, and validation status.
- Run `pytest -q` after every phase, plus affected web/desktop tests and builds.
- Keep Memvid integration behind `RUN_MEMVID_INTEGRATION=1`.
- Distinguish source-ready, developer-bundle-ready, internally artifact-qualified, and publicly released states.
- Building or rehearsing artifacts does not authorize a public tag, release, update feed, or production activation.

---

## Integration Sequence

### Milestone 0: Establish the implementation branch

- [ ] Create a clean integration worktree from the approved release baseline:

```bash
git worktree add ../kestrel-gui-first-integration -b feat/gui-first-kestrel-desktop release/v0.5.0
cd ../kestrel-gui-first-integration
git status --short --branch
```

- [ ] Cherry-pick the approved design/plan documentation commits if the branch does not already contain them.
- [ ] Record baseline:

```bash
git rev-parse HEAD
git status --short
uv lock --check
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
npm --prefix web ci
npm --prefix web test
npm --prefix web run build
```

- [ ] If baseline tests fail, stop and record the exact pre-existing failure. Do not begin feature work on an unqualified baseline.

**Exit gate:** clean branch, exact baseline SHA, Python suite green, existing Workbench suite/build green.

### Milestone 1: Desktop lifecycle foundation

Execute the [Desktop Foundation plan](2026-07-29-gui-first-desktop-foundation.md) completely.

Critical deliverables:

- pinned Electron workspace;
- strict sidecar bootstrap/readiness;
- portable profile lease shared by Desktop/CLI;
- port-zero frozen-compatible sidecar;
- secure custom-protocol renderer;
- manifest verification and one-restart supervisor;
- main-process auth injection;
- narrow preload bridge;
- isolated credential flow;
- recovery projection;
- unsigned developer directory bundle.

**Exit gate:**

- source tests pass on Linux/macOS/Windows;
- developer bundle starts offline Demo;
- six `.mv2` layers initialize/reopen;
- renderer receives no token/raw secret;
- CLI/Desktop concurrent writers are rejected safely;
- unrelated listeners are untouched;
- no signed-installer claim yet.

### Milestone 2: Wildflower GUI-first product

Execute the [Wildflower Workbench plan](2026-07-29-wildflower-workbench.md) completely.

Critical deliverables:

- behavior-preserving extraction from monolithic `App.tsx`;
- seven stable destinations;
- Wildflower tokens, bundled fonts, and primitives;
- Mission Command Center layout;
- permanent five-stage Setup Center;
- task-first Mission, Projects, Memory, Automate, Extend, and Settings;
- configured/effective/blocker settings projection;
- WCAG/reduced-motion/narrow-window/rendered Electron gates.

**Exit gate:**

- a new owner reaches a useful offline Demo mission without terminal/setup commands;
- every current Kestrel feature is reachable in the seven-destination shell;
- browser Workbench remains supported;
- light/dark Wildflower renders pass;
- setup/mission are keyboard-completable;
- LAN/Qualification Flock routes exist as truthful unavailable/dependency cards, not fake controls.

### Milestone 3: Explicit LAN provider discovery

Execute the [LAN Discovery plan](2026-07-29-explicit-lan-model-discovery.md) completely.

Critical deliverables:

- private interface/scope preview;
- routing schema `2 -> 3`;
- bounded mDNS and active scan;
- literal-private-address no-redirect transport;
- disabled provider/target draft import;
- separate owner trust/role/privacy review;
- durable scan manager/API/SSE;
- Flock scan/results/review UI;
- adversarial and controlled two-machine evidence.

**Exit gate:**

- no scan before explicit owner action;
- no public/wide/arbitrary port scan;
- every discovered target remains disabled/unconfirmed;
- stale endpoint/model/certificate/capability drift removes eligibility;
- routing schema v2 evidence is unchanged by v3 migration;
- installed app finds a controlled model server on another private-network computer.

### Milestone 4: Adaptive Flock qualification and activation

Execute the [Adaptive Flock plan](2026-07-29-adaptive-flock-qualification-activation.md) completely.

Critical deliverables:

- exact money/scope/price/corpus models;
- routing schema `3 -> 4`;
- authenticated immutable receipts;
- hybrid deterministic/real-project corpus;
- all-eligible-target snapshot/preview;
- immutable default/editable USD 50 hard cap;
- isolated durable execution with attributable usage/cost;
- 20-repeat replay and transparent per-scope metrics;
- owner-only exact grants;
- runtime effective-grant evaluation;
- automatic suspension/revocation/static fallback;
- Flock Qualification/Activations UI;
- no-authority-expansion/no-policy-memory gates;
- live-provider evidence contract.

**Exit gate:**

- environment booleans alone cannot activate learned routing;
- high-risk remains deterministic;
- missing price/usage is not free;
- qualification creates no authority;
- exact owner grants are required per scope;
- drift/revocation produces durable static fallback;
- 20/20 replay has one projection digest;
- at least one explicitly authorized installed-artifact live qualification uses two real eligible targets and real project evidence before any production learned-routing claim.

### Milestone 5: Native packaging, update, rollback, and uninstall

Execute the [Packaging/Updates/Recovery plan](2026-07-29-desktop-packaging-updates-recovery.md) completely.

Critical deliverables:

- exact native build matrix/toolchain;
- complete PyInstaller sidecar/recovery helper;
- signed resource manifest and combined SBOM;
- DMG/ZIP, NSIS, AppImage, then `.deb`/`.rpm`;
- platform credential policy;
- signed opt-in update manifest;
- idle preflight and consistent snapshot;
- owner-confirmed install;
- external acceptance watchdog;
- matching byte/state rollback;
- uninstall preserving owner data;
- native rehearsal/clean-machine qualification;
- protected release fan-in.

**Exit gate:**

- all six primary platform/architecture artifacts are signed and exact-SHA;
- clean machines need no Python/Node/terminal;
- offline Demo and first mission work;
- update/forced failure/rollback work;
- uninstall preserves data;
- tampered artifacts fail closed;
- 20 lifecycle cycles leave no residue;
- all release gates pass before any public publication.

### Milestone 6: Final integrated qualification

- [ ] Merge/rebase all tracks into the integration branch in Milestone order.
- [ ] Resolve conflicts by preserving the later schema/API contract and rerun the earlier track’s full regression gate.
- [ ] Run exact final source gates:

```bash
uv lock --check
uv run python -m compileall -q benchmarks src tests scripts
uv run ruff check scripts src tests
uv run mypy src
uv run bandit -q -r src -lll -iii
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
npm --prefix web ci
npm --prefix web run licenses:check
npm --prefix web run test:typecheck
npm --prefix web test
npm --prefix web run build
npm --prefix desktop ci
npm --prefix desktop run licenses:check
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run build
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7
```

- [ ] Run gated foundations:

```bash
RUN_MEMVID_INTEGRATION=1 RUN_MCP_INTEGRATION=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/integration/test_memvid_backend_integration.py \
  tests/integration/test_memvid_memory_system.py \
  tests/integration/test_memvid_context_frames.py \
  tests/integration/test_mcp_stdio_integration.py
```

- [ ] Run 20-repeat routing/qualification determinism.
- [ ] Run all installed Electron E2E journeys from packaged artifacts.
- [ ] Require all native clean-machine artifact receipts.
- [ ] Run separately authorized controlled LAN and live-provider qualification.
- [ ] Inspect one final release candidate on each platform manually for visual/interaction quality.
- [ ] Record exact final SHA and receipt digests in the release checklist.

**Exit gate:** the exact integrated candidate satisfies every source, security, determinism, live-integration, and installed-artifact acceptance criterion. Only then is it a production release candidate; public release still requires explicit owner authorization.

---

## Cross-Plan Ownership and Collision Rules

| Shared surface | First owner | Later owner | Integration rule |
|---|---|---|---|
| `src/nested_memvid_agent/server.py` | Desktop foundation | GUI settings, LAN, Flock, update | Keep route registration modular; never inline feature logic back into `server.py`. |
| `routing/ledger_schema.py` | Existing v2 | LAN v3, then Flock v4 | LAN must merge first. Flock migration tests start from real v3 fixtures and preserve all prior digests. |
| `pyproject.toml` / `uv.lock` | Desktop foundation | LAN, packaging | Regenerate once per dependency change; never hand-merge lock records. Run `uv lock --check`. |
| `desktop/package*.json` | Desktop foundation | Wildflower E2E, packaging | Keep exact pins. Regenerate lock with one npm version on the owning branch. |
| `web/src/App.tsx` | Wildflower | none | LAN/Flock mount through `web/src/flock`, not back into `App.tsx`. |
| `web/src/flock/FlockWorkspace.tsx` | Wildflower shell | LAN, then Flock qualification | Add stable subroutes: Overview, Providers, Qualification, Activations, History. |
| `provider_probe.py` / routing discovery | Existing provider discovery | LAN | Reuse capability evidence; LAN adds transport/scope, not a competing capability vocabulary. |
| `routing/coordinator.py` | Existing Adaptive Flock | Qualification executor/effective grants | Direct qualification target still passes hard filters; ordinary learned route requires a grant. |
| `runtime_settings.py` / effective settings | Desktop/GUI settings | Flock kill switches, update opt-in | One settings store/projection; no feature-specific shadow config file. |
| `agent_backup.py` / recovery | Existing | Flock receipt key, update snapshot | Backup state DB and routing key together; Memvid stays canonical and separately manifest-bound. |
| `.github/workflows/ci.yml` | Existing | every track | Add jobs/steps without weakening current Python, security, Memvid, web, Docker, or CodeQL gates. |
| `.github/workflows/release.yml` | Existing | packaging only | Do not add partial desktop publication before all exact-SHA artifact receipts exist. |

## Schema and Contract Sequence

```text
Current routing schema v2
        |
        v
LAN discovery v3
  - routing_lan_scans
  - routing_lan_observations
  - routing_lan_scan_events
        |
        v
Adaptive Flock v4
  - qualification runs/cases/attempts/events/receipts
  - activation grants/transitions
  - decision grant/receipt/reason binding
```

Rules:

- Each migration is additive.
- No migration rewrites existing decisions, outcomes, shadows, calibrations, or LAN observations.
- Every plan keeps a fixture of the immediately previous schema.
- Downgrade is unsupported; update compatibility fails closed and uses matching-byte/state rollback.
- Main `AgentStateStore` schema changes are avoided unless a feature cannot remain in its owning routing/settings store.
- Receipt keys live beside state and are backup-bound, not in Memvid or SQLite.

## Release-State Vocabulary

Use these exact terms in handoffs and UI:

| State | Meaning |
|---|---|
| Source complete | Code and deterministic source tests pass in a development environment. |
| Developer bundle complete | Unsigned current-platform app directory launches and passes local smoke. |
| Rehearsed | Disposable/internal build jobs produced verifiable artifacts; nothing is public. |
| Signed artifact built | Platform signing completed; clean-machine qualification may still be open. |
| Artifact qualified | The exact signed artifact passed installed install/run/update/rollback/uninstall gates. |
| Production routing qualified | A separate live receipt passed two-real-target, real-project, cost, replay, and guardrail gates. |
| Production release candidate | All required exact-SHA source and artifact receipts pass. |
| Publicly released | Owner explicitly authorized tag/release/update-feed publication and it completed. |

Never substitute one state for a later state.

## Mandatory End-to-End Journeys

### Fresh offline owner

1. Install one platform artifact.
2. Open Kestrel without a terminal.
3. Verify bundled core and private data paths.
4. Continue with Demo offline.
5. Add or skip a project.
6. Review conservative safety defaults.
7. Run a first useful mission.
8. Restart and see state/memory preserved.

### Local model on the same computer

1. Open Flock / Providers.
2. Explicitly discover local endpoints.
3. Review catalog/capability evidence.
4. Assign trust/roles and enable selected target.
5. Preview route and run a bounded mission.

### LAN model on another computer

1. Click Scan network.
2. Choose and confirm exact interface/private scope.
3. Review bounded scan size/ports/deadline.
4. Discover controlled remote model server.
5. Inspect address, transport, models, capability provenance, privacy warning.
6. Assign trust/roles and explicitly enable.
7. Change remote model/catalog and verify target becomes stale/ineligible.

### Adaptive Flock qualification

1. Select project, low/medium task families, hybrid corpus, and policy.
2. See every eligible target and every exclusion.
3. Change default `$50.00` cap before launch.
4. Start, view spend/reserves/cost coverage, pause/resume.
5. Complete evidence and 20/20 replay.
6. Inspect qualified/abstained/deterministic-only scopes.
7. Confirm that qualification alone changed no routing authority.
8. Preview/select/confirm exact owner activation.
9. Route a matching new task through the grant.
10. Revoke and verify new tasks fall back static while an existing lease stays sticky.

### Update and rollback

1. Opt in to update checks.
2. Download a signed exact-platform update.
3. Attempt install during active work and see truthful blocker.
4. Reach idle, confirm install, create snapshot/recovery receipt.
5. Accept a healthy new version.
6. Repeat with injected startup/migration failure.
7. Verify previous signed bytes and matching SQLite/key state restore.
8. Verify Memvid remains intact and no high-risk/ambiguous request replays.

### Uninstall/reinstall

1. Uninstall Kestrel.
2. Verify app bytes/integration removed.
3. Verify owner state/memory/projects/settings remain.
4. Reinstall exact or newer compatible version.
5. Reopen preserved six-layer memory and state.

## Quantitative Acceptance Gates

- Zero terminal commands for install, setup, launch, ordinary operation, update, recovery, and uninstall.
- Zero external runtime dependencies for offline Demo.
- Exactly six permanent Memvid v2 layer files.
- Zero automatic LAN scans.
- At most 256 active IPv4 hosts, 4 known ports/host, 16 concurrent probes, 45-second total scan deadline, zero redirects.
- Zero auto-enabled discovered targets.
- Qualification default maximum: USD 50.00; zero cap increases after start.
- Minimum qualification examples: 5/scope and 3/selected target.
- Minimum confidence: 0.70.
- Minimum utility margin: 0.08.
- Minimum attributable live cost coverage: 0.80.
- Evidence decay half-life: 30 days.
- Hard guardrail violations: 0.
- Replay: 20/20 identical projections.
- Minimum real eligible targets for production activation: 2.
- High-risk learned activations: 0.
- Policy-memory writes from qualification/activation: 0.
- Renderer token/raw-secret exposures: 0.
- Automatic sidecar restarts after one unexpected exit: at most 1.
- Installed lifecycle/update cycles per primary artifact: 20.
- Orphan processes/listeners/launcher residue after cycles: 0.

## Risk Register and Required Mitigations

| Risk | Failure signal | Required mitigation/gate |
|---|---|---|
| Memvid native freezing differs by platform | sidecar imports but layer open fails | Native sidecar build and gated six-layer reopen on every artifact; never cross-compile. |
| Desktop/CLI both own a profile | lock conflict, SQLite/Memvid writer race | OS lock authority plus nonce/version/profile readiness; refuse concurrent writer. |
| Renderer compromise reaches native power | unexpected IPC/filesystem/process access | Exact preload surface snapshot, sender/schema/fuzz tests, no generic IPC/shell/filesystem. |
| GUI refactor changes API behavior | request path/body/revision drift | Freeze legacy request contracts before extraction; migrate feature by feature. |
| LAN discovery becomes SSRF/port scan | public destination, redirect, broad host set | Canonical private scope, exact port list, literal IP transport, host/concurrency/deadline/size caps. |
| Discovered target becomes eligible implicitly | enabled/unconfirmed inventory entry | Atomic disabled draft defaults and separate review transaction. |
| Qualification exceeds owner budget | admission above cap or unknown treated zero | Micro-USD reservations in SQLite transaction; unresolved reserve retained. |
| Provider outage corrupts learned quality | outage counted as validation failure | Typed normalization and explicit exclusion from task-quality sample. |
| Environment flag bypasses grants | adaptive learned target without grant | Coordinator requires effective durable grant; regression test seeds no grant. |
| Grant survives material drift | learned routing after endpoint/policy/price change | Per-decision evaluator and append-only automatic suspension. |
| Update migrates state then cannot launch | new bytes fail before acceptance | Idle snapshot, new-version preflight lock, external watchdog, matching byte/state rollback. |
| Recovery damages owner data | broad path operation or mixed snapshot | Exact canonical paths, authenticated instruction, sibling staging, no automatic Memvid replacement. |
| Platform signing succeeds only on outer artifact | inner sidecar/resource tampered | Verify inner binaries, resource manifest, SBOM, and outer signature together. |
| Mock evidence is presented as production utility | synthetic-only qualifying receipt | Receipt marks evidence class; live verifier requires two real targets and real project tasks. |
| Release publishes partial platform set | missing arch or failing clean-machine gate | Protected fan-in validates all primary targets and receipts before publication. |

## Documentation and Evidence Deliverables

- [ ] Update `docs/ARCHITECTURE.md` with Desktop/sidecar/profile lease diagram.
- [ ] Update `docs/SECURITY.md` with renderer, token, credential, LAN, grant, updater, and recovery boundaries.
- [ ] Update `docs/DEPLOYMENT.md` with per-platform install/data paths and uninstall behavior.
- [ ] Update `docs/TEST_MATRIX.md` with source/integration/installed/live gates.
- [ ] Add `docs/FLOCK_QUALIFICATION_OPERATIONS.md`.
- [ ] Add `docs/DESKTOP_SIGNING.md`.
- [ ] Add `docs/DESKTOP_ARTIFACT_QUALIFICATION.md`.
- [ ] Update `docs/RELEASE_CHECKLIST.md` with exact-SHA desktop and live-routing receipts.
- [ ] Update `CHANGELOG.md` only when the target release version is chosen.
- [ ] Preserve generated receipts as internal CI artifacts with retention appropriate to release evidence; do not commit user corpus, credentials, raw project code, or private provider responses.

## Final Program Review Checklist

- [ ] Confirm every feature in the approved specs maps to at least one task.
- [ ] Confirm every task names exact files, interfaces, failing test, passing command, and commit.
- [ ] Confirm no plan contains unresolved temporary markers, fake routes, or incomplete implementation instructions:

```bash
rg -n '\bTO[D]O\b|\bT[B]D\b|\bFIX[M]E\b|\bPLACEHOLD[E]R\b|implement la[t]er|fill this i[n]' \
  docs/superpowers/plans/2026-07-29-*.md
```

- [ ] Confirm internal links resolve:

```bash
uv run python - <<'PY'
from pathlib import Path
import re

root = Path("docs/superpowers/plans")
for path in root.glob("2026-07-29-*.md"):
    for target in re.findall(r"\[[^\]]+\]\(([^)]+\\.md)\)", path.read_text()):
        resolved = (path.parent / target).resolve()
        if not resolved.is_file():
            raise SystemExit(f"{path}: broken link {target}")
print("plan links verified")
PY
```

- [ ] Confirm schema sequence is consistent in every plan:

```bash
rg -n 'ROUTING_SCHEMA_VERSION|schema .*->|schema v[234]' \
  docs/superpowers/plans/2026-07-29-*.md
```

- [ ] Confirm package/tool versions are consistent across plan files.
- [ ] Confirm all destructive operations name exact validated targets and preserve owner data by default.
- [ ] Confirm all live/network/signing/publication actions remain explicitly gated.
- [ ] Run `git diff --check` and inspect `git status --short`.

## Program Definition of Done

The program is complete only when:

1. clean users on macOS, Windows, and Linux install one signed artifact and start a useful offline Demo mission without terminal setup;
2. the installed product contains the complete Kestrel-owned core and preserves CLI compatibility;
3. Mission Command and the seven-destination Wildflower Workbench expose all current Kestrel features with truthful settings and accessible interaction;
4. local and explicitly scanned LAN model servers produce disabled evidence-backed drafts and require owner review;
5. Adaptive Flock qualification evaluates all eligible targets under an immutable owner-approved cap using hybrid evidence;
6. every qualifying scope passes support, confidence, utility, cost, guardrail, and 20/20 replay gates;
7. qualification alone grants no authority and exact owner grants are required for low/medium learned routing;
8. high-risk routing remains deterministic and every stale/revoked/ineffective scope falls back static;
9. no memory, capability, secret, workspace, network, budget, containment, or approval boundary expands;
10. signed updates are opt-in, idle-gated, snapshot-bound, and able to restore matching previous bytes/state;
11. uninstall preserves owner data by default;
12. every exact signed artifact passes clean-machine and 20-cycle gates;
13. separately gated real-provider evidence supports any production learned-routing claim;
14. public release occurs only after explicit owner authorization and protected exact-SHA publication gates.
