# LAN Discovery — Final Verification Evidence (2026-08-02)

- **Worktree (authoritative):** `/Volumes/12.45/Codex-Offload/kestrel-gui-first-integration`
- **Branch:** `feat/gui-first-kestrel-desktop`
- **Final HEAD:** `6694b2c` (`fix: resolve pre-existing mypy errors in settings routes and project setup`)
- **Plan spec:** `docs/superpowers/plans/2026-07-29-explicit-lan-model-discovery.md` — "Final Verification" + "Completion Criteria" sections
- **Verification mode:** verification-only. No implementation changes. Only this report was added.
- **Toolchain:** `.venv/bin/python` (3.12), `.venv/bin/pytest` with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, Node `v22.16.0` via PATH prefix.
- **Plan deviation (per task brief):** `uv lock --check` SKIPPED — the plan's `uv` commands are stale; `.venv/bin/` equivalents were substituted. All other plan gates ran as specified.

## 1. Deterministic gate receipts at HEAD 6694b2c

| Gate | Command | Result |
|------|---------|--------|
| compileall | `.venv/bin/python -m compileall -q src tests` | PASS, exit 0 |
| ruff | `.venv/bin/ruff check src tests` | PASS, exit 0 — `All checks passed!` |
| mypy | `.venv/bin/mypy src` | PASS, exit 0 — `Success: no issues found in 196 source files` |
| bandit | `.venv/bin/bandit -q -r src -lll -iii` | PASS, exit 0 — no findings at `-lll -iii` (warnings only) |
| pytest | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -q` + 6 deselections¹ | PASS, exit 0 — **4689 passed, 80 skipped, 6 deselected, 0 failed, 0 errors** (~9m35s; two consecutive full runs, both exit 0) |
| web licenses | `npm run licenses:check` (web) | PASS, exit 0 — `verified web/public/THIRD_PARTY_NOTICES.txt (106 packages, 2 font assets)` |
| web typecheck | `npm run test:typecheck` (web) | PASS, exit 0 |
| web tests | `npm run test` (web, vitest --environment jsdom) | PASS, exit 0 — **402 passed (51 test files)**, 0 failed |
| web build | `npm run build` (web) | PASS, exit 0 — built in 2.04s (chunk-size >500 kB informational warning only) |
| desktop typecheck | `npm run test:typecheck` (desktop) | PASS, exit 0 |
| desktop tests | `npm test` (desktop, vitest) | PASS, exit 0 — **316 passed (21 test files)**, 0 failed |
| desktop e2e | `npm run e2e -- lan-discovery` (desktop, playwright) | PASS, exit 0 — **4 passed (4.2s)**, spec `e2e/lan-discovery.spec.ts` |
| git hygiene | `git diff --check && git status --short` | PASS, exit 0 — no diff errors; untracked: `desktop/test-results/`, `docs/CODEX_HANDOFF_2026-08-02.md` (both pre-existing, untouched) |

¹ Deselections (from task brief; pre-existing flaky/host-environment-sensitive process-control tests):
`tests/test_install_script.py::test_failed_server_health_stops_tracked_supervisor_and_child`,
`tests/test_install_script.py::test_failed_child_pid_publication_still_cleans_via_supervisor_identity`,
`tests/test_install_script.py::test_failed_health_kills_term_ignoring_server_and_grandchild_group`,
`tests/test_service_control.py::test_system_process_inspector_and_signaler_touch_only_test_owned_process`,
`tests/test_tools.py::test_same_public_call_id_across_runs_keeps_process_tracking_isolated`,
`tests/test_tools.py::test_subprocess_tool_timeout_kills_child_process_and_caps_requested_timeout`.

**Gate history note:** a first verification attempt at HEAD `e30b4f2` was halted per the brief's STOP rule because `mypy src` failed with 3 pre-existing errors (`server_settings_routes.py:103` "None" not callable; `project_setup.py:71` and `:75` float|None assignment). Commit `6694b2c` resolved those errors; all results above are from the resumed run at `6694b2c`.

## 2. Route/schema contract checks

| Check | Command | Actual result |
|-------|---------|---------------|
| Routing schema version | `rg -n "ROUTING_SCHEMA_VERSION = 3" src/nested_memvid_agent/routing/ledger_schema.py` | MATCH — `ledger_schema.py:7: ROUTING_SCHEMA_VERSION = 3` |
| LAN route inventory | `rg -n "/api/routing/lan/" src/nested_memvid_agent/server_lan_discovery_routes.py` | 9 routes (see below) |
| Disabled-draft defaults | `rg -n "enabled=False\|trust_class=\"unconfirmed\"" src/nested_memvid_agent/lan_discovery_service.py` | **NO MATCH in that file — documented deviation, see below** |
| git hygiene | `git diff --check`, `git status --short` | clean (2 pre-existing untracked paths, untouched) |

**LAN route inventory** (`src/nested_memvid_agent/server_lan_discovery_routes.py`):

1. `GET  /api/routing/lan/interfaces` (line 858)
2. `POST /api/routing/lan/preview` (line 876)
3. `POST /api/routing/lan/manual-probe` (line 896)
4. `POST /api/routing/lan/scans` (line 951, 201)
5. `POST /api/routing/lan/scans/{scan_id}/start` (line 971, 202)
6. `GET  /api/routing/lan/scans` (line 999)
7. `GET  /api/routing/lan/scans/{scan_id}` (line 1010)
8. `POST /api/routing/lan/scans/{scan_id}/cancel` (line 1090, 202)
9. `GET  /api/routing/lan/scans/{scan_id}/events` (line 1113, SSE)

**Disabled-draft defaults — deviation documented (not edited):** the plan's contract grep targets `lan_discovery_service.py`, which contains no literal `enabled=False` / `trust_class="unconfirmed"`. The invariant is enforced at the ledger import path in `src/nested_memvid_agent/routing/ledger_registry.py`:

- `_lan_provider_model` creates every imported profile with `enabled=False`, `secret_ref=None`, `trust_class="unconfirmed"` (lines 3167–3174);
- `_lan_target_model` defaults `trust_class: str = "unconfirmed"`, `enabled: bool = False` (lines 3187, 3191);
- `_lan_mark_target_stale` force-resets drifted targets to `"enabled": False, "trust_class": "unconfirmed"` (lines 4073–4076);
- storage-level validation rejects any enabled LAN profile carrying stale reasons (`ledger_registry.py:3578–3585`).

The contract intent (every discovery result is a disabled, unconfirmed draft) holds; only the file named in the plan's grep differs.

## 3. Manual Desktop-path inspection (plan checklist)

The plan's "Manually inspect the exact built Desktop path" list contains **11 bullets** (the task brief calls it "12-point"; coverage below is item-by-item either way). The Desktop renderer is `web/dist` loaded by Electron (`desktop/e2e/lan-discovery.spec.ts:26` resolves `../../web/dist/index.html`); the LAN UI lives in `web/src/flock/lan/`. Verification method: code inspection + committed test evidence + a live run of the e2e spec at HEAD (4/4 passed).

| # | Item | Verdict | Evidence |
|---|------|---------|----------|
| 1 | No scan occurs before clicking Scan network | **Met** | Panel performs zero network calls on mount; interfaces load only on button click (`web/src/flock/lan/LanDiscoveryPanel.tsx:154–160`); intro copy "Nothing is probed until you choose Scan network" (`:149–153`); scans require explicit confirmed POST (`server_lan_discovery_routes.py:951–997`, `_CreateScanRequest.require_initial_confirmation` :103–109). Tests: `desktop/e2e/lan-discovery.spec.ts:65` (no LAN traffic before owner acts, ran green), `web/src/flock/lan/LanDiscoveryPanel.test.tsx:255`. |
| 2 | Owner sees exact private scope and bounds before confirmation | **Met** | Server-owned preview recomputed from server interface data (`src/nested_memvid_agent/lan_discovery_scope.py:95–121`); preview UI renders network, host×port counts, exact ports, deadline, concurrency, expiry (`web/src/flock/lan/LanScopePreview.tsx:9–32`); Confirm appears only after preview (`LanDiscoveryPanel.tsx:220–231`). Tests: `desktop/e2e/lan-discovery.spec.ts:79–96` (asserts `192.168.90.0/24`, "Up to 254 hosts × 4 known model ports", `1234, 8000, 8080, 11434`; ran green), `tests/test_lan_discovery_scope.py:349`. |
| 3 | Public/wide/invalid scopes rejected | **Met** | `PrivateScanScope.from_request` rejects non-private, unattached, and >256-host scopes (`lan_discovery_scope.py:38–62, 134–156`); route maps to 400 `lan_scope_invalid` (`server_lan_discovery_routes.py:889–890`). Tests: `desktop/e2e/lan-discovery.spec.ts:122–152` (8.8.8.0/24, 192.168.0.0/16, 0.0.0.0/0 rejected; ran green), `tests/test_lan_discovery_scope.py:52,58,64,79`, hostile corpus `public_range_rejection` (`tests/test_lan_discovery_security.py:1651–1655`). |
| 4 | Scan progress survives renderer reload | **Partial** | Strong: scan state is durable server-side in the SQLite ledger (`src/nested_memvid_agent/routing/lan_ledger.py:216`, `append_event` :694); SSE replays from `Last-Event-ID` (`server_lan_discovery_routes.py:624–652, 1113–1276`); stream disconnects reconnect with highest persisted cursor and reconcile via authoritative GET (`web/src/flock/lan/useLanScan.ts:290–311, 195–215, 327–339`). Tests: `tests/test_lan_discovery_ledger.py:643,2140,3363,3430,3476`; `useLanScan.test.tsx:170,208,555`. **Gap:** on a full renderer reload the panel's `scanId` is React state only (`LanDiscoveryPanel.tsx:45`) and is not restored from `GET /api/routing/lan/scans` on mount; no e2e reload test exists. The scan itself is never lost or cancelled; the live progress *view* is not re-attached after reload. |
| 5 | Cancellation stops new probes | **Met** | `POST /scans/{id}/cancel` → `LanScanManager.cancel` commits durable cancel event then signals token (`src/nested_memvid_agent/lan_scan_manager.py:747–768`); `ScanCancellation._admit` linearizes cancel vs executor admission so no new probe is submitted after cancel (`src/nested_memvid_agent/lan_scanner.py:521–547, 687–699`); unprobed endpoints recorded `NOT_ATTEMPTED`/`cancelled` (:764–769). Tests: `desktop/e2e/lan-discovery.spec.ts:154–180` (ran green), `tests/test_lan_discovery_ledger.py:1700,1735,1837`, hostile `cancellation` category (`tests/test_lan_discovery_security.py:1684–1690`). |
| 6 | Every result is a disabled draft | **Met** | Import creates `enabled=False, trust_class="unconfirmed", secret_ref=None` profiles/targets (`routing/ledger_registry.py:3167–3174, 3187–3201`); UI renders disabled badge + "not enabled" on every card (`web/src/flock/lan/LanServerCard.tsx:88–89, 177–183`). Tests: `desktop/e2e/lan-discovery.spec.ts:112–116` (ran green), `tests/test_lan_discovery_service.py:1676,1912`, corpus-wide invariant `result.enabled_targets == ()` for all 64 hostile cases (`tests/test_lan_discovery_security.py:1639`). |
| 7 | Prompt/code privacy warning is explicit | **Met** | Every card: "Enabling this target means prompts and code leave this computer." (`LanServerCard.tsx:129–131`); review dialog requires privacy-acknowledgement checkbox (`web/src/flock/lan/LanTargetReview.tsx:77–92`); server enforces `privacy_acknowledged is True` for manual probes and review (`server_lan_discovery_routes.py:188–189`; `lan_discovery_service.py:184–185, 429–435`; `ledger_registry.py:1739–1745`). Tests: `desktop/e2e/lan-discovery.spec.ts:117–119` (ran green), `LanTargetReview.test.tsx:94`. |
| 8 | Stale/certificate/catalog/capability/freshness drift blocks enablement | **Met** | Closed stale-reason set includes `certificate_changed`, `catalog_changed`, `capability_changed`, `freshness_expired` (+ address/port/interface/network/transport/api_shape/model drift) (`lan_discovery_service.py:37–65`); review revalidates the exact evidence preimage and freshness (`routing/ledger_registry.py:1800–1809, 2910–2932`); enable requires live interface binding + capability match + hardening markers (`:1906–1918`); router emits closed guard reasons `lan_binding_stale`/`lan_evidence_expired`/`lan_owner_review_required` (`src/nested_memvid_agent/routing/router.py:216–249`). Tests: `tests/test_lan_discovery_service.py:2224,3381,4425,4464,4746,5765,3795`; hostile `stale_results` disposition (`tests/test_lan_discovery_security.py:1691–1695`); `LanTargetReview.test.tsx:133`. |
| 9 | Target review requires trust, roles, privacy acknowledgement, current revisions | **Met (server) / Partial (UI mounting)** | `LanReviewRequest` requires `trust_class="operator_confirmed"` literal, intended roles, task-family affinities, privacy acknowledgement, enabled flag, and six expected revisions/digests (`lan_discovery_service.py:147–187`); ledger CAS-checks expected profile/target revisions and recomputes the review digest from its exact preimage (`routing/ledger_registry.py:1782–1793, 1941–1954`); route rejects unless `privacy_acknowledged is True` and `confirmed is True` (`server_routing_routes.py:468–490`). Tests: `tests/test_lan_discovery_service.py:4803,4954,5168,8593,5222`; `LanTargetReview.test.tsx:94`. **Gap:** `LanTargetReview.tsx` is unit-tested but not mounted by any production view at HEAD (verified by grep across `web/src`); review is exercised via API routes and component tests only. |
| 10 | No discovered target gains tools, secrets, network, workspace, budget, or approval authority | **Met** | LAN profiles forced `secret_ref=None` and validated to stay None (`routing/ledger_registry.py:3171, 3574`); LAN targets forced `supports_tools/json/vision/reasoning/streaming=False`, all cost fields None, `operator_priority=0` (`:3206–3216`), enforced by `_validate_managed_lan_target_model` (`:3722–3736`); generic registry mutations fenced off from LAN-managed rows (`tests/test_lan_discovery_ledger.py:3932,4041`); workspace/budget/approval concepts do not exist as fields on `ModelTarget`/`ProviderProfile` at all — the authority is structurally absent. Tests: `tests/test_lan_discovery_security.py:1713–1728`; `tests/test_lan_discovery_service.py:1548,1912,5596,5687,7501`. |
| 11 | No raw response, credential, or API token in logs/events/support bundles | **Met (logs/events) / Partial (LAN-specific bundle test)** | Observation public evidence is an allowlist-bounded projection rejecting raw bodies and credentials (`src/nested_memvid_agent/routing/lan_serialization.py:459–480`); SSE events pass through bounded event serializers with frame caps (`lan_serialization.py:253,397`; `server_lan_discovery_routes.py:498–579`); import adapter excludes untrusted raw identity/secret material (`lan_serialization.py:483–583`). Support bundles contain only a manifest + redacted allowlisted tail of `logs/events.jsonl` with `raw_secret_values: excluded` (`src/nested_memvid_agent/support_bundle.py:148–164, 451–454`); LAN scan events live in the routing SQLite ledger, not `events.jsonl`, so no LAN observation data enters bundles by construction. Tests: `tests/test_lan_discovery_ledger.py:749,766`; `tests/test_lan_discovery_service.py:1523`; corpus-wide sentinel-not-in-evidence invariant + `secret_reflection` category (`tests/test_lan_discovery_security.py:1640,1696–1705`; `tests/evals/lan_discovery/hostile_responses.json`, 64 cases); `tests/test_support_bundle.py:166,441,576`. **Gap:** no LAN-specific support-bundle test exists (no `lan` references in `tests/test_support_bundle.py`); the bundle leg is structural. |

## 4. Completion Criteria scorecard (plan, verbatim)

| Criterion | Status | Basis |
|-----------|--------|-------|
| Kestrel can find compatible model servers on another computer on the explicitly selected private network. | **Met (deterministic) / Live leg deferred** | Full discovery path implemented and qualified by deterministic gates (interfaces → preview → scan → SSE → disabled-draft import → review). The controlled two-machine live proof is Step 5, deferred — see §5. |
| Discovery is manual, bounded, private-scope-only, allowlisted, redacted, and cancellable. | **Met** | §3 items 1, 2, 3, 5, 11. Bounds: private ranges only, ≤256 active hosts, fixed port allowlist (1234, 8000, 8080, 11434), deadline + concurrency caps (`lan_discovery_scope.py:38–62`; `tests/test_lan_discovery_scope.py:52,58,64,79`). |
| Passive and active evidence are distinguished. | **Met** | `passive_or_manual_only` is a required strict-boolean field on observation evidence (`routing/lan_serialization.py:264,274–276,295,325,343,377`); passive mDNS collection is a fixed-window collector for a confirmed scope (`lan_mdns.py:1,332`); active evidence comes only from bounded probes/manual probe routes. |
| Every server/model is imported as a disabled, unconfirmed draft. | **Met** | §2 contract-check deviation note and §3 item 6 (`routing/ledger_registry.py:3167–3174, 3187–3201`; `tests/test_lan_discovery_service.py:1676,1912`; corpus invariant `tests/test_lan_discovery_security.py:1639`). |
| Enablement requires a separate revision-checked owner review. | **Met** | §3 item 9 — CAS-checked expected revisions + review-digest preimage recompute (`routing/ledger_registry.py:1782–1793, 1941–1954`; `tests/test_lan_discovery_service.py:4803,4954,8593`). UI-mounting gap noted in §3 item 9. |
| Address, certificate, API, model, catalog, capability, or freshness drift stales the target and removes eligibility. | **Met** | §3 item 8 — closed stale-reason set, forced disable+unconfirm on stale (`routing/ledger_registry.py:4066–4083`), router guard reasons (`routing/router.py:216–249`), storage-level stale/enabled invariant (`ledger_registry.py:3578–3585`). |
| Existing provider discovery and routing tests remain green. | **Met** | Full pytest suite green at HEAD (§1), including `tests/test_agent_routing_ledger.py`, `tests/test_server_routing_discovery.py`, `tests/test_server_lan_discovery_routes.py`, `tests/test_lan_discovery_ledger.py`. |
| Routing schema v2 upgrades additively to v3 without rewriting existing decisions or outcomes. | **Met** | `ROUTING_SCHEMA_VERSION = 3` (`routing/ledger_schema.py:7`); v3 migration only `CREATE TABLE IF NOT EXISTS routing_lan_scans` + related LAN evidence tables/indexes/guards (`ledger_schema.py:199+`, `_ensure_routing_schema_v3_guards`); migration receipt test: `tests/test_agent_routing_ledger.py:120` `test_routing_v2_migrates_to_v3_without_rewriting_route_history` (green in §1 run); also `:105` `test_routing_ledger_uses_additive_schema_and_round_trips_inventory` and `tests/test_lan_discovery_ledger.py:3715` (idempotent v3 application). |
| Deterministic mocks prove safety; a controlled two-machine test proves the installed network path. | **Partially met** | Deterministic mocks: **met** — 64-case hostile corpus (`tests/evals/lan_discovery/hostile_responses.json` via `tests/test_lan_discovery_security.py`), mock-backed service/ledger/route suites, renderer e2e against a fixture server. Two-machine live test: **NOT RUN — deferred**, see §5. |

## 5. Step-5 live-evidence deferral

Plan Step 5 (`RUN_LAN_DISCOVERY_INTEGRATION=1 pytest tests/integration/test_lan_mdns_integration.py tests/integration/test_lan_scanner_integration.py`) requires a private test network with a **second physical computer** serving a known Ollama/OpenAI-compatible fixture, plus recorded interface, scope, digests, destinations, and timeouts. That hardware arrangement is not available in this verification environment, so the controlled live evidence is **deferred, not waived**: it remains an open completion item and must be run and recorded before the plan can be called fully complete. All deterministic qualification that gates it is green at HEAD `6694b2c`.

## 6. Final receipts

- **Final HEAD SHA:** `6694b2c` (`fix: resolve pre-existing mypy errors in settings routes and project setup`), branch `feat/gui-first-kestrel-desktop`.
- **Routing schema migration receipt:** `tests/test_agent_routing_ledger.py:120` `test_routing_v2_migrates_to_v3_without_rewriting_route_history` — green (full-suite run §1).
- **Deterministic test receipt:** 4689 passed / 80 skipped / 6 deselected / 0 failed / 0 errors, exit 0 (§1). Count provenance: project `pyproject.toml` sets `addopts = "-q"`, so the brief's `-q` yields `-qq`, which suppresses pytest's own summary line; counts were derived from the run's progress-character tally and cross-validated by `--collect-only` (`4769/4775 tests collected (6 deselected)`; 4689 + 80 = 4769). Web vitest 402/402, desktop vitest 316/316, desktop e2e 4/4.
- **Controlled live evidence:** deferred (§5).
- **Working-tree state at verification end:** no diff errors (`git diff --check` exit 0); untracked `desktop/test-results/`, `docs/CODEX_HANDOFF_2026-08-02.md` (pre-existing); this report is the only added file.
