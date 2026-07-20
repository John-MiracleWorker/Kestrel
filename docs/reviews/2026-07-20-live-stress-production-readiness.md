# Kestrel live stress and production-readiness review

Date: 2026-07-20  
Branch: `fix/release-setuptools-audit`  
Reviewed working-tree base: `3562205db9e37f5f8728337be72d2f10ca25f13e`  
Candidate package: `0.4.0`  
Supported profile: one trusted owner, one local/private node, bounded concurrent runs

## Verdict

**Local/private personal agent: PASS.** Kestrel is a functional, installable, conversational
personal-agent candidate for its documented single-owner local/private profile. The exact final
runtime source passed the full Python suite, deterministic learning and golden gates, live Memvid
integration, local Ollama text and native-tool turns, authenticated API soaks, browser checks,
isolated wheel/sdist installs, native ARM64 and emulated AMD64 containers, and real OCI extension
containment.

**Public open-source release: HOLD.** The repository is public, but two potential Google API keys
remain in remote Git history, an additional credential-shaped Gmail artifact remains in a local
`refs/original` history ref, and the GitHub repository currently has secret scanning, push
protection, private vulnerability reporting, Dependabot security updates, and `main` branch
protection disabled. The candidate is also a large dirty working tree rather than a reviewed commit,
and its exact commit/tag has not passed the hosted Linux/macOS/Windows release matrix. Those are
release blockers, not code-test waivers.

**Hosted/team agent: not claimed.** This review does not claim tenant isolation, multi-user roles,
distributed scheduling, or a public hosted service. Passing the local/private profile does not make
those product boundaries production-ready.

No commit, push, pull request, merge, tag, publication, deployment, credential rotation, history
rewrite, or GitHub setting mutation was performed.

## Final acceptance matrix

| Area | Result |
|---|---|
| Python suite | **PASS** — 1,352 collected; 1,302 passed; 50 intentional optional/live skips; 0 failed |
| Compile, lint, types | **PASS** — `compileall`; Ruff over scripts/source/tests; Mypy over 121 source modules; strict Mypy for the learning benchmark and golden runner |
| Deterministic learning | **PASS** — 7/7 assertions; control transfer failed, learned treatment succeeded, zero oracle lessons |
| Broad golden evals | **PASS** — memory 21/21 and Memvid 21/21; runner now exits nonzero on aggregate failure |
| Memvid integration | **PASS** — final enabled integration 5/5; cache-deletion replay, logical-ID parity, chunked records, pagination, hash-chain, and concurrency coverage |
| MCP integration | **PASS** — credential-free stdio integration 1/1 |
| OCI extension containment | **PASS** — 3/3 real-container isolation/timeout cases |
| Web application | **PASS** — 54/54 tests, 106-package notice check, npm audit 0 vulnerabilities, production build |
| Python security/static | **PASS** — configured Bandit high/high gate, ShellCheck, Bash syntax, Actionlint, and `git diff --check` |
| Current-source secret scan | **PASS** — 410 candidate files / 7.25 MiB, zero findings after exact synthetic-fixture fingerprints |
| Remote-history secret scan | **FAIL / release hold** — 482 commits, two non-fixture `gcp-api-key` findings |
| Package artifacts | **PASS** — Twine, metadata, licenses, prompt, SPA, exclusions, wheel/sdist isolated installs |
| Dependency/SBOM | **PASS** — exact installed environment has zero known Python vulnerabilities; CycloneDX SBOM has 55 components; npm audit clean |
| Exact packaged Memvid soak | **PASS** — 30/30 at concurrency 4, 0 failed/overloaded, p95 8.905 s, all six layers healthy |
| Containers | **PASS** — native ARM64 and emulated AMD64 build and runtime; nonroot; doctor/chat/Memvid/pip checks |
| GitHub release controls | **FAIL / release hold** — confidential reporting, scanning/protection, Dependabot updates, and branch protection disabled |
| Hosted exact-candidate CI | **NOT RUN / release hold** — the dirty tree is not an exact reviewed commit/tag on the cross-platform matrix |

The only recurring test warning is the known third-party Starlette/httpx TestClient deprecation. The
jsdom suite also reports its known non-failing canvas implementation warning.

## Live behavior and stress evidence

### Conversational and native-tool path

The local `qwen3:4b` path was exercised as a real provider, not a mock-only adapter check:

- ordinary memory-backed turn: 19.54 s;
- cold default CLI turn after retry-budget correction: 40.37 s;
- Memvid-backed text turn: 24.53 s, followed by deletion of disposable exact-record caches and
  successful reconstruction/search from all six canonical `.mv2` files;
- final native-tool agent turn: 92.378 s total, with 63 registered tools bounded to 12 advertised
  tools per round, exactly one successful `diagnosis.classify` execution, and a clean second-round
  final response with no duplicate call;
- provider-native live certification: local Ollama passed; nine unavailable credentialed provider
  cases skipped rather than being represented as tested.

The native-tool stress pass found and fixed three important classes of defect: malformed provider
call blocks being coerced into executable empty arguments, assistant call/result continuity being
lost between provider rounds, and registry discovery being unable to carry a validated hidden tool
into the next bounded catalog. Adapters now reject malformed names, structures, JSON, non-object
arguments, non-finite values, cycles, and invalid streaming fragments before `ToolCall`
construction. Exact successful duplicates are suppressed by canonical name plus arguments; changed
arguments remain callable.

### Final exact packaged soak

The authoritative packaged soak used the final wheel with Memvid, mock provider, API authentication,
and concurrency 4:

| Metric | Result |
|---|---:|
| Warmup | 1 completed |
| Measured runs | 30/30 completed |
| Failed / overloaded | 0 / 0 |
| Elapsed | 57.895 s |
| Minimum | 2.551 s |
| Median | 7.573 s |
| p95 | 8.905 s |
| Maximum | 8.978 s |

Authenticated readiness returned 200; unauthenticated readiness returned 401. After the soak, all
31 runs including warmup were completed in schema-v19 SQLite, all six `.mv2` layers verified, no
operational alerts were active, and all 31 run capsules had both `complete.mv2` and completion
markers. An unvalidated stable-memory candidate remained fail-closed.

The performance characterization matrix used a provisional, code-near artifact and is not the
authoritative release hash, but it isolates scaling behavior. Every cell completed 30/30:

| Backend | c1 p95 | c4 p95 | c8 p95 |
|---|---:|---:|---:|
| In-memory | 0.256 s | 0.886 s | 1.762 s |
| Memvid | 2.957 s | 9.058 s | 16.500 s |

At Memvid c8, queue p95 was 8.385 s because runtime capacity is deliberately four active runs;
active p95 was 9.021 s. The dominant active cost was context plus durable Memvid writes, not the
first seal. Memvid ended around 168–170 MiB RSS versus 81–84 MiB for the in-memory backend. This is a
real optimization target, but the documented normal c4 soak passes. Operators should not interpret
the bounded queue as linear high-concurrency throughput.

### Long-run admission and overload campaign

A separate pre-final-artifact stress campaign exercised the admission/state/capsule path at larger
scale. It is supporting stress evidence, not the final artifact acceptance stamp:

- 500/500 bounded API runs completed, with zero retries, 429s, or 5xx responses during the bounded
  phase and 22,722 authenticated status reads;
- terminal throughput was 2.017 runs/s; submit p50/p95/p99 were
  128.477/271.667/333.882 ms; terminal p50/p95/p99 were
  22.009/30.471/33.593 s, including queue time;
- peak outstanding was 48 under the configured 8-active/64-queued capacity;
- peak RSS was approximately 124 MiB;
- a 200-request simultaneous overload probe accepted exactly 72 and rejected 128 with deterministic
  HTTP 429, zero 5xx, and all 72 accepted runs completed;
- post-overload state contained 572 completed runs, schema 19, SQLite integrity `ok`, zero foreign-key
  violations, 572 capsule completions, zero capsule failures, and exactly 100 retained run
  directories / 400 retained files.

Evidence hashes:

```text
eb9de3922d7ae7906b90a7ac94e88e56358727ea0b2b7a52b5e191230a4c37a3  soak-runtime-evidence.json
2e353c381119998b46a671f81cdcb3ab739eade7ec528bb6f3c2ad05c919ead4  soak-client-report.json
cd953eab25843222decfd64b1d84a2841863d727130f7d895a0dc92b1d957e63  post-overload-integrity.json
6d9861fe2bae705bdd8836f7b31c79f06066ac498c1a6b6bac9603ce99c2862d  overload-probe.json
```

### Memory integrity and learning

- A 256 KiB logical record spanning 220 physical Memvid frames replayed exactly after cache deletion.
- Canonical event envelopes now carry logical sequence and previous-event digest continuity;
  pagination is over logical records, not physical chunks; corrupt origin/chain/cursor state fails
  closed.
- The final Memvid backend returns the logical `MemoryRecord.id` from `put` and `upsert`, matching the
  in-memory contract and allowing immediate subject-bound validation lookup.
- Live shared-handle concurrency, including six threads with 15 writes and 30 reads, completed with
  zero errors; larger focused integration passes also completed cleanly.
- Stable promotion now uses durable owner-only keys, exact candidate/proposal digests, run/session and
  evidence-bucket binding, authenticated current-subject receipts, two separately approved policy
  phases, and recall-time revalidation. Raw artifacts, caller-asserted human evidence, cross-claim,
  cross-run, and wrong-bucket replay are rejected.
- Unvalidated capsule candidates are staged truthfully as provisional episodic evidence; they are
  not reported as semantic/procedural promotion and cannot enter policy.
- The memory signing key passed 4,800 concurrent first-opens across 50 fresh directories; the repair
  signing key passed 1,920 across 20. Both use crash-consistent temp write, file sync, atomic
  no-clobber publication, directory sync, and safe recovery. Fault matrices cover partial writes,
  file/directory sync failures, publication failure, external winners, orphan temps, post-link
  crashes, and link attacks.
- The deterministic A/B learning gate passed all seven assertions: Task 2 failed in the fresh-memory
  control and succeeded when the lesson produced by Task 1 was available, with zero oracle lesson
  injection.

### Browser/workbench behavior

Desktop 1280×720 and mobile 390×844 rendered checks passed. A 2,806-character message wrapped without
horizontal overflow; the mobile composer remained inside the viewport; route changes reset scroll;
same-thread runs created outside the selected view appeared automatically without scroll jump
(position remained 140); and browser console warnings/errors were empty. The corresponding web suite
passed 54/54.

The pre-existing workbench remained untouched at `127.0.0.1:8766`, PID 80370. Temporary test ports
and containers were stopped.

## Defects found and corrected during this review

The review did not merely rerun green tests. It found and fixed release-significant defects:

1. CLI wait budgets did not cover configured provider retries and summary work; shutdown could end
   with a traceback while a provider retry was still active.
2. LLM summarization ignored configured timeout/retry options and could turn an otherwise completed
   user turn into a failure instead of falling back deterministically.
3. Host-only Ollama URLs missed the `/v1` compatibility root, and HTTP 404 was classified opaquely.
4. Native tool schemas were overexposed and duplicated; provider continuation, malformed-call
   taxonomy, bounded discovery, and exact duplicate behavior were incomplete.
5. Memvid's disposable JSON cache could become de facto authority; large chunked records were counted
   as physical frames; logical pagination and chain verification were incomplete.
6. Memvid returned physical SDK frame IDs rather than logical record IDs, breaking immediate
   subject-bound workflows.
7. Ordinary semantic/procedural learning dead-ended, while policy validation receipts could be
   replayed, relabeled across buckets, or invalidated on restart by process-local keys.
8. Repair and memory signing-key creation exposed a zero/partial final inode during first open; two
   independent concurrency races were found. Publication is now crash-consistent and no-clobber.
9. Legacy backup restore could preserve incompatible live trust material. It now removes those
   components and warns, failing closed.
10. `capsule.apply` could imply stable application for an unvalidated candidate. It now stages only
    provisional episodic evidence with explicit provenance/status.
11. The broad golden evaluation printed `passed: false` but exited 0, and several scenarios bypassed
    the new stable sink. The runner now fails CI and all scenarios follow the real trust model.
12. The workbench did not reliably refresh a selected thread when an external run appeared; the fix
    preserves scroll while adding the new run.
13. Task-capsule retention and local operator API parity contained concurrency/lifecycle races.
14. Release validation did not require exact tag ancestry and an exact cross-platform matrix; current
    source/history secret scanning was not enforced with a pinned scanner image.

## Final artifacts

Authoritative local evidence root:
`/tmp/kestrel-release-final-exact.9XsOih`

```text
c7264cdf2ff0c3fbf54b2d01c62d4bf11471582c2577fdb58390d06843d61356  nested_memvid_agent-0.4.0-py3-none-any.whl  (742,409 bytes)
1d3bf4312a970083a404b2952752bede299bb277f1659a86086a733f6dc4582e  nested_memvid_agent-0.4.0.tar.gz                 (986,895 bytes)
```

Both artifacts passed Twine and metadata checks, contained the required Apache license/third-party
notices, system prompt, and built SPA, and excluded `tiuni-fun`, environment files, private keys,
runtime state, caches, `.nest`, `.venv`, `node_modules`, and bytecode. Wheel and sdist were installed
in separate hash-locked environments; both passed `pip check`, six-layer Memvid initialization and
verification, doctor, and mock chat.

The exact installed Python environment had zero known vulnerabilities according to pip-audit; the
unpublished Kestrel package itself was the only unresolvable index entry. The CycloneDX SBOM at
`/tmp/kestrel-release-final-exact.9XsOih/sbom.cdx.json` contains 55 components, includes release
dependencies, and excludes development dependencies.

Container evidence:

```text
ARM64  sha256:83b0356509f35e16a76449f4114a193cc3da8dfcc1136cf0c702ca486e25569a  149,423,418 bytes
AMD64  sha256:867233f416590b2754c012f975288b22a18d21e4c262410cf414bb6e8657fbbf  152,978,185 bytes
```

Both images run as user `kestrel` and passed Memvid init/verify, doctor with dangerous flags disabled,
mock chat, `pip check`, and runtime hygiene checks proving that build-only Rust/uv/temp payloads were
absent. The authenticated container server returned readiness 200 and unauthenticated readiness 401.
The pinned OCI extension image passed 3/3 tests for network/host isolation and nonroot identity,
read-only snapshot/rootfs enforcement, and timeout orphan cleanup.

Docker Scout's OS-layer CVE attestation could not run because Docker Desktop is not authenticated to
a Docker ID. This does not invalidate the zero-vulnerability Python audit, but it remains an external
container-publication hold. The unfiltered Bandit inventory contains 25 low and 13 medium heuristic
findings and zero high findings; the configured high-severity/high-confidence release gate passes.

## Public-release blockers and required sequence

The current-source scan is clean. The remote-history scan is not:

- public commit `6f9d9b4102dd7fdd0384ecf691d40d2ce00637ed` contains two `gcp-api-key`
  findings, in `list_models.py:5` and `trigger_chat_internal.py:53`;
- local commit `b8d9bb42a21ddbfb724ed97780c9411df7e4358d` contains a credential-shaped
  finding in `mcp-servers/gmail/credenials.json:8`, reachable only from
  `refs/original/refs/heads/main` in this clone, not from the clean remote clone.

No secret values were printed or copied into this report. Treat every finding as exposed until the
provider confirms otherwise.

Required release sequence:

1. Revoke/rotate the two public-history Google credentials and the local-history Gmail credential.
   Rotation comes before history rewriting because existing clones and caches may retain the bytes.
2. Plan and perform a coordinated public-history rewrite across affected branches/tags, clean local
   `refs/original` and reflogs after rotation, force-update only under an announced incident procedure,
   and require collaborators to re-clone. Re-run the history scan and require zero non-synthetic
   findings.
3. Enable GitHub private vulnerability reporting.
4. Enable GitHub secret scanning, push protection, validity checks, and non-provider-pattern scanning;
   enable Dependabot security updates.
5. Protect `main`: require the exact cross-platform CI checks and review policy, and disallow deletion
   and force pushes except for a deliberate incident-recovery window.
6. Curate and commit only intended Kestrel changes. Exclude `tiuni-fun/`, local env/runtime data,
   review temp files, caches, and credentials. Obtain independent review of the exact committed diff.
7. Run the exact commit through Linux Python 3.11/3.12/3.13, macOS 3.11/3.12, Windows 3.11, frontend,
   Memvid, MCP, OCI, dependency, package, and secret gates. Then create a tag on `main` and require the
   release workflow to validate that exact tag commit before publication.
8. Authenticate the container scanner and produce an OS-layer CVE attestation for both architectures.
9. Run credentialed live certification for every external provider the release claims. This review
   certifies local Ollama; it does not convert unavailable provider skips into passes.
10. Only after all holds are green: sign/checksum, publish, verify a clean public install, and exercise
    upgrade and rollback. Keep the reviewed artifacts and a pre-upgrade memory backup until the
    post-publication soak passes.

As checked on 2026-07-20, the public repository reports private vulnerability reporting disabled;
secret scanning, push protection, validity checks, non-provider patterns, and Dependabot security
updates disabled; and branch protection disabled on `main`.

## Bottom line

Kestrel's remaining barrier is no longer “does the agent actually work?” The local/private runtime
does work, learns under evidence, retains canonical Memvid memory, executes bounded tools, installs,
survives stress, and ships in both tested container architectures. The barrier to a responsible true
open-source release is repository trust: rotate and remove historical credentials, enable the public
security controls, turn the dirty candidate into an exact reviewed commit/tag, and prove those exact
bytes on hosted cross-platform CI.
