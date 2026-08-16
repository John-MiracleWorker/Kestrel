# Codex handoff — LAN preview/confirm gate uncommitted

Captured: 2026-08-02T10:55:27-0400 (2026-08-02T14:55:27Z)

This record freezes the exact state before any further implementation. It does
not authorize changes in the primary checkout, activation, release, or external
publication.

## 1. Authoritative checkout and never-touch boundary

- Authoritative worktree:
  `/Volumes/12.45/Codex-Offload/kestrel-gui-first-integration`
- Branch: `feat/gui-first-kestrel-desktop`
- Committed HEAD:
  `85e6c01e3a60e6c4cf167b7f7fbc4f1c2e554757`
  (`feat: paginate LAN scan observations`)
- Index: clean; there are no staged changes.
- Primary checkout: `/Users/tiuni/kestrel`, observed read-only through
  `git worktree list` at `31c38e9148c96b5f7502b1dad27c4f53f3155492`
  on `fix/approval-continuation-capability`.

Never edit, reset, restore, checkout, clean, rebase, merge, commit, or run
stateful commands in `/Users/tiuni/kestrel`. Continue only in the authoritative
external-volume worktree. Do not touch launchd service `ai.kestrel.daemon`,
detached installer/supervisor worktrees, unrelated listeners/processes, or
user-owned state. Reinspect ownership and ancestry before any targeted process
action.

## 2. Dirty-file manifest

The preview/confirm gate itself contains exactly these nine unstaged modified
files:

```text
 M src/nested_memvid_agent/lan_discovery_service.py
 M src/nested_memvid_agent/routing/ledger_registry.py
 M src/nested_memvid_agent/server_routing_routes.py
 M tests/test_lan_discovery_service.py
 M tests/test_server_routing_discovery.py
 M web/src/flock/lan/api.test.ts
 M web/src/flock/lan/types.ts
 M web/src/routing/api.ts
 M web/src/routing/types.ts
```

This handoff adds one untracked documentation file after that capture:

```text
?? docs/CODEX_HANDOFF_2026-08-02.md
```

Do not reset or overwrite any of these files.

## 3. Preview/confirm gate status and exact patch identity

The current server-owned preview/confirm gate is **not committed**. HEAD still
ends at the committed pagination change above. No commit was created for the
service, route, or client preview/confirm work.

Patch digest algorithm:

```bash
git diff --binary --no-ext-diff HEAD -- <the nine gate paths> | shasum -a 256
```

Whole gate patch SHA-256, excluding this handoff file:

```text
fd2abfd611e60b10806fa1c3e52b0a1c6a91476c919a36f4bbf4df1e366e5005
```

Per-file patch SHA-256 and current Git postimage blob ID:

| Path | Patch SHA-256 | Postimage blob |
|---|---|---|
| `src/nested_memvid_agent/lan_discovery_service.py` | `c37a65e3f64bb6a3f763a539c1ecc738eea660c7f06fce571411061b2887914c` | `de5338c7b5452c0ecc920c9a71aa0a6ff9e510f1` |
| `src/nested_memvid_agent/routing/ledger_registry.py` | `fc4dc8ce21543fe41ab938b0a36ac1e836c352e97c8c7ebc43a92e4e5bb31218` | `41fae0a2756a92352083ddfd13a5ca9d9878131e` |
| `src/nested_memvid_agent/server_routing_routes.py` | `52dd38f3ee636d723a6dda43bbfcfecfd2edbf9eb295319b4845c97acd8696b5` | `5c10d2dc46b5a13efeb8293486e66f03493901d2` |
| `tests/test_lan_discovery_service.py` | `9633dc57fbd58ca52389946dda37b4f7c9b2a11ed24f0a5323b1e7505bdce75e` | `8f37cab99ef4340693fbd1ea7020dcff1b91ddf6` |
| `tests/test_server_routing_discovery.py` | `82e7fd20195e1b8c07a63d73bfa3268bb2f2d67012d76c449e6e2c2b38c2d7b1` | `71ebc2d28a5009997ed48d0a20fc334f78804e72` |
| `web/src/flock/lan/api.test.ts` | `643ca46f4b239d1a595647b75309cc23b96b854a766b2ac919a965fe31efaf03` | `70749c4dd18f9257da0fc0084df35136958fee53` |
| `web/src/flock/lan/types.ts` | `c40930fcb9d5054d94f05daabebd652aac905e9faab4606546086567a03c1c87` | `ff5fece1e654ffced71cdcc4b55bd031baf7a20a` |
| `web/src/routing/api.ts` | `e3e91a876ee6a6585267f4111dd58ba31c3e8556cc117c171724d72396f2f361` | `9b00ef7f9473e64c5e8c5f39afd108684c13fa0e` |
| `web/src/routing/types.ts` | `f7b2a2f010da6420909d3fd4a4f8e40281ead2947f2ccec68cbba78ebdebf3cd` | `f8b381c522f0fe0bb8d29c0ed5b99248220fd954` |

Recompute before trusting these identities. Any substantive edit to a gate file
must change the corresponding patch digest and whole-gate digest.

## 4. What the uncommitted gate implements

- Server-derived import selector, review options, authority, preview, and
  confirmation dataclasses.
- Read-only preview over an in-memory SQLite backup.
- Confirmation recomputation under the same durable `BEGIN IMMEDIATE`
  transaction that commits.
- `hmac.compare_digest` for preview-digest confirmation.
- Exact confirmation results shaped as `{preview_digest, result}`.
- Four strict LAN HTTP routes:
  - `POST /api/routing/lan/import/preview`
  - `POST /api/routing/lan/import`
  - `POST /api/routing/lan/targets/{target_id}/review/preview`
  - `POST /api/routing/lan/targets/{target_id}/review`
- Legacy client-built authority fields rejected at ingress.
- Typed client preview/confirm functions, exact-key response parsing, Python
  code-point affinity order, real 16-affinity limit, and no invented cap on
  import revision/material/target arrays.
- No schema migration. Legacy service APIs remain compatibility paths.

## 5. Current gate test receipts

Passing receipts:

| Scope | Receipt |
|---|---|
| Service/registry | `tests/test_lan_discovery_service.py`: **296 passed** |
| Routes | `tests/test_server_routing_discovery.py`: **79 passed** |
| Routes plus security headers | **81 passed** |
| Typed client | `web/src/flock/lan/api.test.ts`: **48 passed** |
| Client static | `npm --prefix web run test:typecheck`: exit 0 |
| Python static/security | Ruff, strict mypy, py_compile, Bandit: passed for the service slice; route Ruff/mypy passed |
| Patch hygiene | `git diff --check`: passed before this handoff file was added |

Observed TDD RED receipts before the current GREEN state:

- Before route wiring, the two preview happy paths returned 404 and the two
  confirmation bodies returned 422 against the legacy models.
- The first client cutover run exposed two valid review confirmations rejected
  as `lan_request_invalid` by a four-key/seven-key validator mismatch.
- Three added client hardening cases initially resolved instead of rejecting:
  selected-endpoint/current-profile mismatch, enabled-review runtime-binding
  mismatch, and noncanonical `+00:00` preview expiry. Those three cases are now
  green in the 48-test focused receipt.

Qualification not completed for the dirty gate:

- The combined command
  `.venv/bin/pytest -q tests/test_lan_discovery_service.py tests/test_server_routing_discovery.py`
  was deliberately aborted after about 8.3 seconds. It is not a receipt, and no
  matching pytest process remained afterward.
- No repository-wide Python suite has run on this dirty gate.
- No full Web suite or production build has run on this dirty gate after the
  final parser hardening.
- No commit may be described as green or review-approved while the open P1s in
  section 8 remain.

## 6. Completed commit table

### Wildflower Workbench Tasks 1–14

| Task | Commit | Subject |
|---|---|---|
| 1 | `0cb926c899bee7bff1dca7d4ef2293014a63a7ae` | Freeze Workbench behavior before shell refactor |
| 2 | `d00ecec62304da345aff124eb02e01e9246fe4a8` | Establish seven-destination Workbench shell |
| 3 | `cdb5668d7569202a2f919337ee16eaa6f2942b97` | Split Workbench into feature workspaces |
| 4 | `e6a6533bca0bb6f0693230541291485ddcf1050b` | Add accessible Wildflower theme foundation |
| 5 | `e7de44f279c266ad261e980583af37948664d284` | Add Wildflower interface primitives |
| 6 | `6889047977ea230757f2bf8b7d9e9db7a13a3dd2` | Shape Mission Command Center layout |
| 7 | `fe4af586b59ec6ec1dd0fb044795d49460245cd0` | Replace onboarding modal with permanent Setup Center |
| 8 | `a9116896a39e0ec1ce10e6644cfa93620feddb80` | Make Mission the primary task surface |
| 9 | `212d81655e882e3b6820f0521825abb3554c3723` | Complete Projects and Memory workspaces |
| 10 | `90da3e0f0dc8e30de1b023969c27c39fdb10eb7a` | Complete Automate and Extend workspaces |
| 11 | `5766a286b25fccec0050fe3fdff989b175d00106` | Add server-side effective settings projection |
| 12 | `e6056ce66c642ca07c1a713eaea19f27c9c9de5e` | Build searchable Settings workspace |
| 13 | `397a0167aacbbc2d58dd378acfe65e5b2f236db0` | Enforce Workbench accessibility journeys |
| 14 | `9eef69c7364bfe21e6a4a936fda819c79a4d419e` | Validate rendered Wildflower Workbench |
| Handoff | `de2011efe9b7edb971f093d425932d142b909262` | Hand off completed Wildflower Workbench milestone |

### Explicit LAN discovery through committed HEAD

| Plan position | Commit | Subject |
|---|---|---|
| Task 1 | `0fe1fdfc6fa05195bbcc37acfd61d9c983417517` | Define bounded private LAN scan scopes |
| Task 1 hardening | `f42aa3e10521e8d0f427b977f5e35cc8142f5c90` | Bind LAN endpoints to confirmed scopes |
| Task 2 | `dfa7fa042f8ad2c411726b5a2efc88d8701b9fd7` | Persist immutable LAN discovery evidence |
| Task 2 hardening | `84f86af4c3b23358c81603f75d7bd2b3969a2367` | Harden immutable LAN discovery evidence |
| Task 2 hardening | `0b7fde8e1ab56b4d9d56b0ee2b954263d6faf977` | Close LAN terminal receipt bypasses |
| Task 2 hardening | `3c1432c2399a121bae3606ee143091b67fa5937f` | Make LAN scan identity immutable |
| Task 2 hardening | `578aa5d8f55cc30794381ef7354276abb9cea667` | Make LAN scan identity null-safe |
| Task 3 | `568c4629079aebd29b668d09c09aa5a226d9a8db` | Collect bounded LAN model mDNS candidates |
| Task 3 hardening | `532cf5fe64aad46552f55806d2ac462f2d2049ab` | Harden bounded LAN mDNS collection |
| Task 3 hardening | `12c8ac364fea1967812f93cdda83d7babed4e0b1` | Preserve fresh LAN mDNS evidence |
| Task 4 | `fc2cff7f86e82205b15fb0e7f5803e0b91713ca8` | Probe only bounded private model endpoints |
| Task 4 hardening | `bf91cb8b31030010cf10f98c79df6c9e6f98dd48` | Harden LAN probe evidence boundaries |
| Task 5 | `da26eea046afb6804df73044c7a95cb1133234b7` | Import LAN endpoints as disabled target drafts |
| Task 5 hardening | `424378a2d95a32458a61ca077d5a4f5d9e129f2e` | Close LAN draft review gaps |
| Task 5 hardening | `dbbc32b1573a743a694f05a6b2de493c1b7d2de1` | Harden LAN model runtime transport |
| Task 5 hardening | `1eea68b201e6a7ea55d15548a8dbc55479b72735` | Preserve LAN profile authority invariants |
| Task 6 | `f82c335ed2b0a3183b23ca6033774c093318538a` | Run durable owner-controlled LAN scans |
| Task 7 | `9ad43dbf18b2d5c10e7ada5cfc30d977169a4b82` | Expose explicit LAN discovery API |
| Task 7 manual authority | `407a52a9490870a77b0ce062a07eeeb2460b980f` | Add exact manual LAN probe authority |
| Task 7 hardening | `af2e5808b7025756d6a19db6f9ea3974a815f288` | Close LAN discovery boundary leaks |
| Task 8 | `00c5b93d3139ae14f7106bfb1426445e17fa1f70` | Add typed LAN discovery client |
| Cross-cutting wire fix | `ee39382566ba90baaf59f396739d63db23d22daa` | Normalize LAN event wire envelopes |
| Cross-cutting pagination | `85e6c01e3a60e6c4cf167b7f7fbc4f1c2e554757` | Paginate LAN scan observations |

## 7. Latest committed full-suite receipts

At Wildflower HEAD `9eef69c`:

- Web: 47 files / 324 tests.
- Accessibility suites: 25/25.
- Desktop browser-context e2e: 16/16.
- Python: 2663 passed / 83 skipped with the documented host-sensitive
  supervisor/process exclusions.

At current committed HEAD `85e6c01` after observation pagination:

- Exact backend suite with the six documented host-sensitive deselections:
  exit 0. The aggregate pass/skip count was not retained in the surviving
  receipt; do not invent it.
- Full Web: 49 files / 383 tests passed.
- Production Web build: passed, with the known pre-existing chunk-size warning.
- Ruff, mypy, and `git diff --check`: passed.
- Independent pagination review: APPROVE, high confidence, no P0–P3 findings.

The six exact backend deselections used for that committed-HEAD receipt were:

1. `tests/test_install_script.py::test_failed_server_health_stops_tracked_supervisor_and_child`
2. `tests/test_install_script.py::test_failed_child_pid_publication_still_cleans_via_supervisor_identity`
3. `tests/test_install_script.py::test_failed_health_kills_term_ignoring_server_and_grandchild_group`
4. `tests/test_service_control.py::test_system_process_inspector_and_signaler_touch_only_test_owned_process`
5. `tests/test_tools.py::test_same_public_call_id_across_runs_keeps_process_tracking_isolated`
6. `tests/test_tools.py::test_subprocess_tool_timeout_kills_child_process_and_caps_requested_timeout`

These are qualification exclusions, not passes. Revalidate against the current
host before changing the list.

## 8. Open review findings

### P1 — valid existing-profile outage imports are rejected by the client

`web/src/routing/api.ts` currently requires current profile/current-target LAN
metadata `observation_digest` to equal the top-level import result digest for
every import. A legitimate outage result can carry the new outage observation
at the top level while preserving the prior successful observation in an
existing profile/target. Both preview and confirmation can therefore fail with
`lan_response_invalid`.

Required next RED: an existing-profile outage preview and confirmation using the
real backend result shape. Gate metadata equality to non-outage imports without
weakening endpoint/profile binding.

### P1 — replacement authority is not correlated to replacement targets

The client permits targets under the selected replacement profile ID but does
not require their LAN metadata endpoint fingerprint to match
`authority.replacement.expected_endpoint_fingerprint`, nor prove that the
replacement material-binding set agrees with invalidated effects. The current
65-target fixture masks this with generic current-endpoint metadata and equal
fingerprints.

Required next RED: a replacement-family target with contradictory fingerprint
or material authority, followed by a valid 65-target fixture using distinct
current/replacement endpoint evidence.

### P2 — review receipt correlation is partial

The client correlates observation, endpoint fingerprint, privacy digest,
reviewed material, and runtime binding, but not all exposed review authority:
expected revisions, terminal receipt, pre-review material, stale-reason
acknowledgement, or review digest can still contradict target metadata.

Required decision: either make these fields display-only server receipts and
document that boundary, or add a narrow typed metadata projection and exact
correlation tests.

### P2 — replacement profile mutation is hidden from the public preview result

The internal import plan hashes the post-mutation replacement profile, but the
public `LanImportResult` exposes only the current profile plus targets. The
replacement authority identifies the old profile, and its targets are visible,
but the profile disable/trust mutation itself is not projected for owner review.

Required decision: expose a bounded `replacement_profile`/profile-effect
projection or document why target effects plus authority are sufficient.

### P2 — no-op outage replay semantics are unspecified

Mutating confirmations naturally conflict after revisions change. An
uncorrelated outage writes nothing and may accept the same preview digest again
until evidence expires. Decide whether no-op confirmation is intentionally
idempotent or whether every digest needs a durable consumption receipt.

### P3 — dead route alias

`LanStaleReasonRequest` in `server_routing_routes.py` is unused after removal of
the legacy authority-bearing review request model.

### Review disposition and rejected claim

- Route review: APPROVE, high confidence; no P0–P2, one P3 above.
- Client review: REQUEST CHANGES, high confidence; the two P1s and receipt P2
  above remain open.
- Transaction review correctly verified read-only snapshots, same-transaction
  recomputation/commit, `hmac.compare_digest`, rollback, owner binding, and no
  explicit family-size cap.
- A transaction-review P1 alleging wall-clock timestamps enter the preview
  digest was rejected by direct code inspection: `_lan_entry_digest_payload()`
  returns only `{revision, model}` and excludes entry `created_at`/`updated_at`.
  `evidence_expires_at` derives from durable observation freshness. A
  preview-at-T/confirm-at-T+delta regression test is still a worthwhile missing
  coverage item.

Other useful missing transaction tests: outer commit-hook crash rollback for
the new wrappers, concurrent confirmation, cross-owner attempts, state drift
between preview and confirm, enabled-review interface drift/expiry, and a real
65-target backend replacement family.

## 9. Plan position and next authorized task

Plan: `docs/superpowers/plans/2026-07-29-explicit-lan-model-discovery.md`.

- Tasks 1–8 are committed.
- Event wire normalization and observation pagination are committed.
- The server-owned preview/confirm safety gate between Tasks 8 and 9 is dirty,
  uncommitted, and blocked on the two client P1s plus requalification.
- Task 9 (Scan Network/result review/target confirmation UI) has not started.
- Task 10 adversarial/runtime qualification has not started.

The next authorized task is **finish the existing preview/confirm gate only**:
write RED tests for the two P1s, make the smallest client corrections, resolve
or explicitly defer the P2 contract questions, obtain fresh independent review,
run focused plus full gates, and commit one reviewable gate commit. After that,
continue with LAN plan Task 9. Do not skip directly to Task 9 while this gate is
uncommitted.

After LAN Tasks 9–10, plan order remains:

1. Adaptive Flock qualification/owner-gated activation design.
2. Packaging, update, rollback, recovery, and uninstall qualification.
3. Final integrated qualification.

No production activation, persistent service change, release/tag, signed
artifact claim, update-feed publication, or public release is authorized.

## 10. External qualification boundaries and pre-existing issues

Not qualified by the current work:

- Hosted CI on the dirty gate.
- Live LAN discovery (`RUN_LAN_DISCOVERY_INTEGRATION=1`) against a real private
  model server.
- Memvid integration (`RUN_MEMVID_INTEGRATION=1`).
- Signed/notarized Electron bundle or native installers.
- Windows/Linux packaging and lifecycle qualification.
- Update, rollback, recovery, uninstall, or signed-artifact qualification.
- Adaptive Flock qualification, learned-routing activation, or owner activation.
- Production launch or public release.

Known issues not to fold into this gate:

- Approval packet digest shape mismatch in the engineering backend path.
- `PUT /api/settings/{id}` owner-auth symmetry question.
- Known Vite chunk-size warning and Starlette/httpx deprecations.
- The six host-sensitive supervisor/process/tool tests listed above.

Use `.venv/bin/python`/`.venv/bin/pytest` and Node v22.16.0 from
`/Users/tiuni/.nvm/versions/node/v22.16.0/bin`. Do not use `uv run` in this
worktree. Preserve Memvid v2-only, SQLite authority, exact owner approvals,
disabled-by-default LAN drafts, provenance, isolation, and rollback.
