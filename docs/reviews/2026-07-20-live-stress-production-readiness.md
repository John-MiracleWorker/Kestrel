# Kestrel live benchmark, stress, and release-readiness review

- Date: 2026-07-20
- Package version under review: `0.4.0`
- Release-candidate branch: `agent/v0.4.0-release-candidate`
- Base commit: `origin/main` at `d8ceb10dc1fdd48e93d09c798cada842cf4ff46a`
- Candidate identity: the exact release pull-request head after commit. Hosted CI and release
  attestations bind that SHA. `/tmp/kestrel-final-validation.K8CPk5` is supporting live-campaign
  evidence, not the final candidate identity.

## Verdict

**Single-owner local/private personal agent: PASS.** Kestrel is a functioning conversational agent
for its intended bounded, single-owner, local/private-node profile. The campaign exercised the real
agent loop, local Qwen inference, native tools, authenticated HTTP API, all six Memvid v2 layers,
learning and promotion gates, overload admission, browser UI, installer, MCP, and OCI extension
containment. The important safety properties held: ordinary activity did not write policy memory,
unapproved high-risk execution was blocked, a sensitive-file probe did not disclose its marker,
accepted runs produced completion evidence, and canonical `.mv2` memory survived process shutdown
and cache removal.

**Current local package campaign: PASS on the tested ARM64 macOS host.** The campaign's universal
wheel and source archive passed strict metadata checks, a five-artifact self-checksummed payload verifier,
hash-locked dependency installation, `pip check`, isolated wheel and sdist installs, real Memvid v2
write/seal/reopen/retrieval, mock CLI conversation, generated CycloneDX inventory, and an audit of
the installed release environment. Those artifacts predate the final release commit; only the
workflow-produced payload and `SHA256SUMS` are publication authority.

**Exact public release: PRE-PUBLICATION HOLD.** The `v0.4.0` release metadata gate now passes,
including the dated changelog. The first pull-request run exposed ShellCheck 0.9, Windows typing,
OCI process-limit, and hermetic-test defects; each was reproduced and corrected, and the complete
local release gate passed for that prior head. The second pull-request run passed every non-Windows
lane but its native Windows test lane failed 173 cases, exposing additional Windows filesystem,
locking, and private-directory assumptions. The third run passed every non-Windows lane except one
macOS 3.12 cleanup-timing case and reduced the native Windows lane to 90 failures, isolating the
remaining descriptor-lifetime, exact-byte I/O, ACL-alias, lock-contention, Git line-ending, and
timing/publication-fence defects. The fourth run passed every independently runnable upstream lane
other than macOS 3.11 and Windows; those lanes had one failed-launch cleanup case and 22 cases,
respectively, isolating zombie-reaper timing, test-owned SQLite
handles, Windows rollback path primitives, a binary-mode regression harness, private-directory
fixtures, and loaded-runner timing. A final independently reviewed follow-up is now in the isolated
release working tree. Its 1,823-pass aggregate suite, integrations, golden evaluations, and learning
gate all pass locally; fresh wheel and sdist validation is recorded below. Its exact-head hosted run
is still pending. Remaining gates are a successful run for the latest exact pull-request head, one
independent human approval, PyPI Trusted Publisher registration, repository immutable releases
before tagging, first-publish GHCR visibility, and post-publication verification.

**Credential incident: RESOLVED WITH A DOCUMENTED RESIDUAL.** GitHub reports zero open secret
alerts. Alert 1 was resolved as revoked after a provider probe returned `API_KEY_INVALID`.
Historical bytes remain reachable, but no destructive rewrite is planned because revocation removes
credential authority and rewriting would disrupt forks, clones, links, and signatures. The
Gmail-shaped artifact exists only in a local historical ref and is not in the public remote.

**Hosted or multi-user service: not claimed.** This review does not certify tenant isolation,
multi-user authorization, distributed scheduling, or adversarial shared-host operation.

The historical Google key was confirmed invalid and the alert resolved as revoked. No history
rewrite was performed by design. No merge, tag, release, publication, or deployment has occurred,
and no claim of bit-for-bit reproducibility is made.

## Acceptance matrix

| Area | Current result |
|---|---|
| Aggregate Python suite | **PASS ON THE CURRENT FOLLOW-UP TREE** — 1,823 passed, 69 intentional opt-in/platform skips, 0 failed, and one known third-party Starlette/httpx deprecation warning in 234.65 s. |
| Enabled integrations | **PASS ON THE CURRENT FOLLOW-UP TREE** — 23/23: 18 Memvid (43.76 s), 1 stdio MCP (11.82 s), and 4 real OCI-container cases (5.78 s) using the pinned validation-image digest. |
| Local live provider | **PASS for Ollama/Qwen only** — generate, stream, and native-tool certification passed; 27 credential-dependent provider cases were skipped, not counted as passes. |
| Deterministic benchmark bundle | **PASS** — memory, 4/4 agent tasks, 6/6 recovery tasks with 9 injected errors, and learning acceptance all passed. |
| Golden evaluations | **PASS ON THE CURRENT FOLLOW-UP TREE** — memory 21/21, maximum 7,069.90 ms; Memvid 21/21, maximum 9,666.09 ms; zero false promotions; 45 s per-case gate. Cost was unmeasured and was not an acceptance gate. |
| Current learning benchmark | **PASS ON THE CURRENT FOLLOW-UP TREE** — 4/4 outcomes and 7/7 acceptance assertions passed. |
| Memory-system evaluation | **PASS** — 9/9, 17 writes, 13 hits, 0 policy writes. |
| Live learning | **PASS** — Qwen memory 8/8 and Qwen Memvid 8/8; evidence-gated behavior activation occurred; policy stayed empty. |
| Large retrieval benchmark | **PASS versus its TF-IDF baseline, but quality-limited** — recall@5 0.449, precision@5 0.090, MRR 0.235 on 665 documents / 136 queries. |
| Load mode | **PASS** — 12/12 completed, 0 failed/overloaded, 4.805 completed runs/s, p95 0.445 s. |
| Explicit saturation mode | **PASS** — 12 requested; 4 accepted/completed, 8 capacity-rejected, 0 failed; overload ratio 0.666667; exact accounting and capsule completion passed. |
| Release-profile soak | **PASS** — baseline 30/30, p95 0.918 s; saturation accepted/completed 5 and rejected 35 of 40, p95 1.128 s, 0 unexpected failures. |
| Built-wheel pressure run | **PASS** — sustained burst completed 100/100 with p95 4.498 s and 3.822 completed/s; saturation completed 5 and capacity-rejected 195 of 200 with p95 1.217 s, exact accounting, 0 unexpected failures, and all completion capsules present. |
| Rendered browser | **PASS** — headful Chromium 51/51 at desktop, tablet, and mobile; axe-core 4.11 reported 0 violations and 0 incomplete checks at all three sizes. |
| Web static gates | **PASS** — 57/57 tests, production build, 106 package notices, npm audit 0 vulnerabilities, staged assets matched `web/dist`. |
| Hash-locked installer | **PASS** — 2/2 live local-repository integrations, plus an isolated exact-wheel verifier using 67 hash-locked requirements and a fresh Memvid v2 store. |
| Current wheel/sdist | **PASS AS LOCAL EXACT-TREE EVIDENCE** — the rebuilt wheel and sdist passed Twine strict checks, content verification, clean isolated installs, `doctor`, mock chat, and real Memvid write/reopen/retrieval. They represent the current follow-up tree, not workflow-built publication artifacts; only workflow output is publication authority. |
| Release SBOM/dependency audit | **PASS locally** — reproducible CycloneDX JSON contained 60 release components, all required runtime components and no pytest/Ruff/mypy; the installed environment had no known audited vulnerability. The unpublished Kestrel package itself was explicitly unauditable on PyPI. |
| Supporting multi-arch image campaign | **PASS locally** — ARM64-native and AMD64-under-QEMU images from the preceding hardening campaign passed architecture/config inspection, read-only non-root CLI/Memvid smokes, authenticated readiness, and Compose-equivalent mock API soaks. These are supporting local BuildKit artifacts, not images rebuilt from the final follow-up or published provenance. |
| Supporting pre-follow-up source scan | **PASS** — a 438-file source-only candidate assembled from tracked and non-ignored untracked files produced zero unallowed findings with pinned Gitleaks 8.30.1. The final exact candidate is independently archive-scanned after commit and scanned again by hosted CI; the tag workflow likewise materializes and scans the exact release commit instead of failing on revoked historical bytes. |
| Public history | **RESOLVED CREDENTIAL RESPONSE** — zero open GitHub alerts; the historical Google key is provider-invalid and revoked; the local Gmail artifact was never in the public remote; no history rewrite is required under the documented revoke-first decision. |
| GitHub controls | **PARTIAL PASS** — branch ruleset `19198902` has no bypass and requires one approving review plus strict aggregate `docker`; tag ruleset `19299564` has no bypass and blocks update/deletion of `v*`; validity and non-provider scanning remain unavailable under the current plan. The `pypi` environment requires explicit owner approval and disallows admin bypass. |
| Hosted exact-candidate CI | **PENDING FOR THE FINAL FOLLOW-UP EXACT HEAD** — PR [#270](https://github.com/John-MiracleWorker/Kestrel/pull/270). Run [29790915475](https://github.com/John-MiracleWorker/Kestrel/actions/runs/29790915475) exposed the first cross-platform and runner defects. Run [29792592489](https://github.com/John-MiracleWorker/Kestrel/actions/runs/29792592489) passed every non-Windows lane but failed the native Windows test lane with 173 failures. Run [29796925999](https://github.com/John-MiracleWorker/Kestrel/actions/runs/29796925999) passed all but one macOS 3.12 test and 90 Windows tests. Run [29799895155](https://github.com/John-MiracleWorker/Kestrel/actions/runs/29799895155) passed secret scan, CodeQL, web, integrations, extension sandbox, every Ubuntu lane, and macOS 3.12; macOS 3.11 failed one zombie-sensitive cleanup proof and Windows failed 22 cases. The aggregate `docker` gate was correctly skipped after those upstream failures. The reviewed final follow-up tree is locally green but has not yet been certified by hosted CI; the ruleset-required exact-head rerun must pass before merge. |

The current aggregate Python suite passed 1,823 tests, skipped 69 intentional opt-in or
platform-specific cases, and failed none in 234.65 seconds. It emitted only the known third-party
Starlette/httpx TestClient deprecation warning.

The previous local freeze reran `uv run pytest -q` after the first hosted-failure corrections were
added: 1,780 passed and 55 opt-in cases skipped. The exact Memvid v2 integration selection
separately passed 18/18 in 50.28 seconds. Focused runtime-fence/provider review passed 126 tests,
release/install/supply-chain validation passed 132 with one opt-in installer case skipped, and
pinned `actionlint` 1.7.7 accepted both workflows. These are historical aggregate results for the
prior head. Fresh aggregate, integration, evaluation, learning, and package evidence for the
current final follow-up tree is recorded below.

### Current final-follow-up evidence (local candidate tree)

- The final integration reruns record 18/18 Memvid integrations in 43.76 seconds, 1/1 stdio MCP
  integration in 11.82 seconds, and 4/4 OCI extension integrations in 5.78
  seconds using the pinned validation-image digest.
- The final evaluation reruns record golden memory 21/21 with maximum latency 7,069.90 ms and
  golden Memvid 21/21 with maximum latency 9,666.09 ms. Both had zero false promotions. The learning
  benchmark passed 4/4 outcomes and 7/7 acceptance assertions.
- Evidence under `/tmp/kestrel-v040-package-final.gI9Nyz` records a Python 3.12.11 build, strict
  Twine and content checks, and separate hash-locked, isolated Python 3.11.15 wheel and sdist
  installs. Both installs resolved all 67 locked requirements, imported all seven optional-extra
  surfaces, and passed `pip check`, metadata/resource checks, `doctor`, mock chat, and real Memvid
  v2 write/seal/close/reopen/verify/retrieval. A 135-file package-input manifest was byte-identical
  before the build, in the build snapshot, and after the gate.
- The exact aggregate suite passed 1,823 tests, skipped 69 intentional opt-in or platform-specific
  cases, and failed none in 234.65 seconds. These results exercise the exact local candidate tree,
  but are not workflow-built publication attestations. Only release-
  workflow outputs built from the final exact commit are publication authority.

### Supporting live-campaign evidence (not exact final-candidate authority)

- The counted aggregate log is
  `/tmp/kestrel-final-validation.K8CPk5/posthardening/pytest-full-final-counted.log`, SHA-256
  `6a0aa1b16921952a0031ff679dd4b65851617021801d57b4a32955aa5f89bdb0`. It records 1,688
  passed, 55 skipped, one third-party warning, and zero failures.
- The opt-in Memvid log is `posthardening/pytest-memvid-integration-final.log`, SHA-256
  `e3da95fae2f1f91f25793f4a09e2e62278447ddeaf3508054c5a057b0a401088`; all 18 cases
  passed without skips.
- CI-scoped Ruff, native and Windows-targeted mypy over 125 source files, compileall,
  `uv lock --check`, `git diff --check`,
  Bash syntax, ShellCheck, Bandit high-severity gating, development metadata, web tests/build,
  generated notices, and npm audit are green. A deliberately broad Ruff invocation found minor
  import-order and unused-import defects in three tracked root-level manual provider probes outside
  the CI lint scope; those were corrected, and the repository-wide Ruff invocation now also passes.
- After the aggregate rerun, the final Docker/Compose/release-workflow corrections and narrow
  synthetic-fixture annotations passed 42/42 packaging plus release-supply-chain tests,
  repository-wide Ruff, Compose rendering, and pinned `actionlint` 1.7.7.
- The post-hardening benchmark bundle passed every acceptance assertion; artifact
  `posthardening/benchmarks-run-all.json` is SHA-256
  `ca2897505a632d71aeffa01ac05616dc6ba3b4737222b8ce0418a17e208343be`.
- Corrected memory and Memvid golden artifacts are SHA-256
  `5a7bdc7b76ffb01b208166ad7099950ee7f78779ae23fc13e99422799c8b50d1` and
  `da0a8669cdda722264019eb0ea733ce34d888187b827edc53a53fe57fb95fa18`, respectively.
- The final source-only candidate and scanner report are under
  `posthardening/candidate-source-final-v2/` and `posthardening/gitleaks-candidate-final-v2/`;
  its file-by-file manifest is `posthardening/candidate-source-final-v2.sha256`.
  Pinned image
  `zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f`
  scanned 438 files / approximately 8.42 MB and reported no unallowed findings. Seven
  credential-shaped test literals were
  retained as narrowly annotated synthetic redaction fixtures; no broad allowlist was added.

## Real-agent and model testing

### Qwen tool-use benchmark

The real-model benchmark used Ollama `0.31.2` with `qwen3:4b` (GGUF Q4_K_M) on a MacBook Air M4,
10-core CPU, 16 GiB memory, macOS 26.5. It passed 4/4 tasks with five tool calls:

| Task | Result | Elapsed |
|---|---:|---:|
| Write memory, reopen a fresh session, and retrieve it | pass | 86.338 s |
| Read a file and answer from it | pass | 48.813 s |
| Search a repository and identify the result | pass | 142.793 s |
| Inspect Git status and describe it | pass | 50.335 s |
| **Total** | **4/4** | **328.279 s** |

This proves protocol and tool-path functionality; it is not a competitive latency result. Mean task
time was about 82.1 s, the search case took 142.8 s, and variance was high. The artifact is
`/tmp/kestrel-final-validation.K8CPk5/benchmark-agent-ollama-qwen3-4b-prompt-fixed.json`, SHA-256
`e57f0aa312551675f1cb55046df3b1df6e73d0bb1a803927439b30b509db1434`.

### End-to-end HTTP API, Qwen, and Memvid

A disposable authenticated server exercised API submission through `RunManager`, Qwen generation,
Memvid persistence, capsule completion, shutdown, and reopen:

- run `run_cbc8e9858bfb43fabbe9ca9f4b803af8` completed in 18.124 s with exact response `API_OK`;
- provider preflight/postflight remained healthy and SQLite integrity remained `ok`;
- the run made three memory writes, no tool executions, exactly one `capsule.completed`, and no
  capsule failure;
- the unvalidated stable-memory candidate was rejected and staged only as episodic evidence;
- after shutdown, all six `.mv2` layers verified, the marker was retrieved, and policy contained
  zero records.

The result artifact is
`/tmp/kestrel-final-validation.K8CPk5/api-qwen-memvid-live-result-final.json`, SHA-256
`909ca18c0167bc6b3f59e7b32e2c8641a8ddc44806211a42c2a9c21eb2cfbede`. The post-shutdown probe is
`api-qwen-memvid-persistence.json`, SHA-256
`d1b56b942115cfaa827fe1dfeb9d5dbcb976bcb9f55a43beceda48c72b17e1c7`; its completed capsule is
SHA-256 `c7e28276de75f3c9023f37efd76ccf1da9e9e0396495908893a63d25c3d8bd6b`.

### Post-hardening live CLI confirmation

After the final runtime and packaging changes, a fresh CLI session again used real Ollama
`qwen3:4b` with a disposable Memvid directory. It returned exact text `FINAL_LIVE_OK`, run
`run_49c58236c8ca44f6a10ef6a439ed20c3` completed with stop reason `complete`, and wall time was
103.80 s. A separate `doctor` process then reopened and verified all six `.mv2` layers. The record
sidecars showed two working records, one episodic record, and zero semantic, procedural, self, or
policy records. Evidence is under
`/tmp/kestrel-final-validation.K8CPk5/posthardening/live-qwen-final/`.

### Adversarial live-model probes

The Qwen adversarial artifact passed both cases:

- an exact `shell.run` request, with shell enabled but without approval, reached the tool boundary
  and was blocked with `approval_required`; stop reason was `approval_required` and policy stayed
  empty;
- an exact request to read a synthetic `.env.production` reached `file.read`, failed closed with
  `file_read_failed`, did not disclose the marker, and left policy empty.

Artifact: `/tmp/kestrel-final-validation.K8CPk5/qwen-adversarial-final.json`, SHA-256
`c64ee2b10ddedaa1c7cdb8d1617d16edaf9c40f4568da5f1f8fd1ad7e1583291`.

Only local Ollama was live-certified. OpenAI, Anthropic, Gemini, hosted Ollama, DeepSeek, Kimi,
OpenRouter, and Codex CLI configurations were unavailable without their credentials/settings and
remain unverified here.

## Learning and memory results

The repeated live-learning evaluation passed 8/8 with the in-memory backend in 114.09 s. The same
evaluation passed 8/8 with Memvid in 88.31 s, recording ten writes, three retrieval hits, one
behavior-delta activation, and zero policy writes. After disposable exact-record cache sidecars were
removed, search reconstructed from the canonical `.mv2` stores and returned six relevant records.

Evidence:

- `live-learning-qwen-memory-repeat.json`, SHA-256
  `0c54a9e1c62971e29de3cec6db37054508266e437f4225bd795f6be05188c181`;
- `live-learning-qwen-memvid.json`, SHA-256
  `24bda4b97f393bc93fbe215b56de00b72164838769fe7b10555b13dc16ac80a3`;
- `live-learning-qwen-memvid-cacheless-search.json`, SHA-256
  `5721b66fe9aa858f9a319650fa1df1129c6c60a1a29e10edc2148204c970ccc1`.

The authoritative deterministic benchmark bundle at `posthardening/benchmarks-run-all.json`,
SHA-256 `ca2897505a632d71aeffa01ac05616dc6ba3b4737222b8ce0418a17e208343be`, passed every acceptance
assertion: the small memory corpus scored recall@5 1.000, precision@5 0.337, and MRR 0.967; all four
agent tasks and all six recovery cases passed; and the learning gate passed. Three separate
behavior-delta replays improved from baseline 0.0 to delta 1.0 with one activation each. The
memory-system evaluation passed 9/9 with promotion evidence, correction/tombstone behavior,
cross-layer flow, backend consistency, 17 writes, 13 hits, and no policy write.

The larger 665-document benchmark is the more realistic warning signal. Kestrel beat the TF-IDF
baseline (recall 0.449 versus 0.279; precision 0.090 versus 0.056; MRR 0.235 versus 0.159) and queried
about 2.05 times faster, but absolute recall and especially precision remain modest. Optional
VectorRAG, Qdrant, and Chroma comparisons were explicitly skipped because their optional
dependencies were absent; they must not be described as defeated baselines.

Memvid passed the same 21 golden cases as the in-memory backend, but its post-hardening maximum case
latency was 9.850 s versus 7.248 s. Real runs also expose head-of-line delay because the safe
primary-agent profile is intentionally serialized. Memvid sealing, sidecar reconstruction, durable
writes, and process isolation are genuine optimization targets. Golden-case provider cost was not
measured.

## Load, overload, and lifecycle behavior

The version-3 soak runner now distinguishes ordinary load from saturation and requires exact request
accounting. It verifies a completion capsule for every accepted run and treats deterministic
capacity rejection as overload rather than failure.

The focused load artifact requested 12 runs and completed all 12 at 4.805 runs/s with p95 0.445 s,
zero failures, zero overload, healthy pre/post readiness, and all six memory layers verified. The
focused saturation artifact requested 12, accepted and completed four, rejected eight at admission,
and recorded zero unexpected failures. The overload ratio was 0.666667 and p95 for accepted work was
0.751 s.

The release-profile soak at `/tmp/kestrel-release-soak.Xws7H8` was larger:

| Mode | Requested | Completed | Capacity-rejected | Failed | p95 | Throughput |
|---|---:|---:|---:|---:|---:|---:|
| Load | 30 | 30 | 0 | 0 | 0.918 s | 4.987/s |
| Saturation | 40 | 5 | 35 | 0 | 1.128 s | 4.429/s |

This is evidence of bounded admission and graceful rejection, not evidence of unbounded parallel
throughput. Kestrel's supported local profile remains one primary agent with a small queue; high
concurrency primarily increases queue time.

The built wheel then received a larger isolated pressure run. With a 64-slot queue, 100 requests at
concurrency 16 all completed in 26.184 s: p95 was 4.498 s, throughput was 3.822 completed/s, every
accepted run had a completion capsule, and pre/post readiness and all six memory-layer integrity
checks passed. With a four-slot queue, a simultaneous 200-request saturation burst accepted and
completed five, deterministically rejected 195 at capacity, failed none unexpectedly, and preserved
exact 200/200 classification; accepted-work p95 was 1.217 s.

The first attempt intentionally launched the installed executable while its cwd was the repository.
It inherited the repository's local Telegram readiness requirement and correctly stayed 503 with
reason `telegram_poller_unhealthy`; no load was sent. Repeating from an empty release cwd removed
that ambient operator configuration and passed. This was a harness-isolation correction, but it is
also an operational reminder that Kestrel deliberately treats local `.env` readiness requirements
as authoritative. Evidence is under
`/tmp/kestrel-final-validation.K8CPk5/posthardening/package-stress-isolated-final/`.

## Tool, MCP, and extension containment

The enabled integration run passed 23/23. Its Memvid cases covered reopen/search, all six layers,
logical pagination, a 256 KiB chunked logical record, tamper repair, legacy migration, backup
restore, concurrent run serialization, scheduler release, rebuildable vector sidecars, context
frames, and capsule summaries. The stdio MCP discovery/invocation test passed.

Four real OCI extension cases passed: host paths, network, and root identity were denied; read scope
was snapshotted read-only; timeouts left no orphan; and validation containers excluded host trust
and the live workspace. High-risk tools still require explicit configuration and approval.

The first Compose-equivalent image startup uncovered a separate packaging defect: with uid/gid
999, `read_only: true`, and only `/data` and `/tmp` writable, `PluginManager` tried to create the
default `/app/.nest/plugins` path and the server exited before readiness. The production environment
now sets `NEST_AGENT_PLUGINS_DIR=/data/plugins`, creates and owns that directory in the image, and
duplicates the binding in Compose. The failed probe was isolated on a dynamically assigned port and
is preserved as evidence rather than hidden. The release workflow now also starts each architecture
as uid/gid 999 with a read-only root, exact `/data` and `/tmp` mounts, dropped capabilities and
no-new-privileges, waits for authenticated readiness, and runs a four-request mock soak. Final
rebuilt images passed that stronger local profile:

| Image | Runtime path | Result |
|---|---|---|
| ARM64, image/index ID `sha256:8194a6204892edc5ff4804845949d434e5d6703d69e1d7732425bb2472a2e272` | Native ARM64 | CLI/Memvid passed; API readiness in 3 polls; 16/16 completed; p95 1.917 s; 2.354/s. |
| AMD64, image/index ID `sha256:0f143a5fcb62699f55128061cabac003e3c1a64cc45f323e65a386fe4af3593a` | QEMU on ARM64 | CLI/Memvid passed; API readiness in 4 polls; 8/8 completed; p95 1.360 s; 1.705/s. |

Both images identified the intended Linux architecture, ran as uid 999 with a read-only root,
network disabled for the CLI smoke, all capabilities dropped, and no-new-privileges. Memvid SDK
2.0.160 imported, exactly six `.mv2` layers verified, deterministic chat completed, `/data/plugins`
was owner-writable, and `/app/.nest` remained absent before and after load. Both API soaks had exact
request accounting, zero unexpected failure or overload, complete trace capsules, healthy Docker
state, and pre/post six-layer integrity. Evidence is under
`posthardening/container-validation.kkDHSF/`.

This is multi-architecture runtime evidence, not a registry claim: AMD64 ran under emulation, the
provider was deterministic mock, and the images were neither pushed, release-signed, registry
vulnerability-scanned, nor assigned immutable public registry provenance. No combined published
multi-architecture manifest was built or proven.

A final audit found that an MCP worker could be removed from tracking before its close operation was
proven successful. The manager now retains the worker, treats a false close result as
`mcp_session_close_failed`, blocks sensitive reconfiguration while that worker is live, and removes
it only after a verified close. Regression coverage exercises failed disconnect and failed
reconfigure paths.

The same fail-closed lifecycle rule now applies to OCI execution: Kestrel retains the exact engine,
container, and ownership record until bounded cleanup succeeds, blocks new extension admissions
while cleanup is pending, exposes a cleanup counter, and retries without losing the target. Skill
and plugin install/update/enable/remove/sync operations now use staged atomic filesystem swaps plus
one SQLite bundle transaction, quiesce live execution before a swap, fsync publication, and restore
the exact prior filesystem and database state on failure.

Repository validation was hardened against hostile local Git configuration. Git launches use an
absolute trusted executable, sanitized environment, disabled hooks/fsmonitor/textconv/external
diff/credential and `ext` protocol behavior, and command-line neutralization of every configured
clean/smudge/process filter. A regression installs hostile fsmonitor and clean-filter commands and
proves that repair snapshots and Git status neither execute nor leak them. OCI-only arbitrary-code
tools now fail readiness unless their validation image is an immutable
`name@sha256:<64 hex>` reference; execution uses a networkless, credential-free, read-only snapshot
with no host fallback. Operators still need a purpose-built image that preloads the project tools
their tests require; digest shape alone cannot prove tool completeness.

Tool-registry internals are no longer injected into arbitrary third-party tool arguments. Only
explicitly opted-in subprocess tools receive a private execution identifier, while persisted and
returned approvals retain the exact public call. Cancellation and settlement waits are bounded. If
a worker or cancellation hook cannot be proven settled, Kestrel returns nonretryable
`tool_outcome_unresolved`, requires operator reconciliation, and quarantines that tool across
registries sharing the owning `RunManager` fence until runtime restart. Run-owned resources remain
retained until late threads actually settle. Cooperative subprocess tools terminate process trees;
on Windows, children are assigned suspended to a kill-on-close Job Object before they are resumed.
The runtime fence keys exact calls by durable execution origin and public-argument digest, so sibling
subagents cannot suppress one another when a provider reuses a call ID. Interruptions at both worker
launch and result-wait boundaries retain run-owned resources and quarantine the implementation until
settlement or restart. Provider adapters now require exact bounded call IDs and reject duplicate,
unknown, non-contiguous, or incomplete assistant-call/tool-result histories before transmission.

Residual boundary: local stdio safety depends on Kestrel retaining lifecycle control in the current
process. A deliberately hostile daemonized descendant or a second independent Kestrel process is
outside that guarantee; untrusted extensions need the OCI or remote boundary.

## Browser/workbench validation

The final browser pass used headful Chromium 145 against a disposable server on
`127.0.0.1:18786`, serving Kestrel's staged production bundle. It passed 51/51 checks at 1440×900,
768×1024, and 390×844:

- unauthenticated state showed `Locked`, made exactly one intentional 401 request, and did not storm;
- token entry reached authoritative `Ready` state;
- four deterministic mock chats completed with zero tool use;
- Chat, Routines, Settings, and Advanced rendered without page-level horizontal overflow;
- root, unauthenticated, and authenticated responses carried the expected CSP, COOP, CORP,
  Permissions-Policy, Referrer-Policy, nosniff, and frame-denial headers;
- axe-core 4.11 reported zero violations and zero incomplete reviews at all three viewport sizes;
- no unexpected network, page, or console errors occurred; isolated SQLite integrity passed.

The pass found and corrected keyboard-focus, ARIA-role, focus-ring, and color-contrast defects.
Static web verification then passed 57/57 tests, a production build, 106 license notices, npm audit
with zero vulnerabilities, and exact equality between staged assets and `web/dist`.

Evidence is under
`/tmp/kestrel-final-validation.K8CPk5/browser-final/repaired-final/`, especially
`browser-validation.json`, `browser-api-run-evidence.json`, `ASSESSMENT.md`, and the six screenshots.

The pre-existing workbench at `127.0.0.1:8766`, PID 80370, was not restarted or stopped. The
disposable listener on 18786 was stopped.

## Installer, packaging, and release controls

The fresh installer integration exposed a real hash-locked bootstrap defect: `wheel==0.47.0`
required `packaging>=24`, but `packaging` was not present in the common `--require-hashes` input. The
fix moved exact `packaging==26.2` hashes into `config/python-build-bootstrap.txt` and removed the
duplicate release-only entry. The resulting focused JUnit artifact
`pytest-installer-integration-final.xml` records 2/2 passes, including a clean local-repository
Memvid installation.

The staged installer now rejects caller overrides for repository, ref, requirements URL, wheel URL,
and checksums URL, preserving binding between the validated tag and its release assets. Release
gates now require exact SHA/tag/main ancestry, a supported Python range of 3.11 through 3.13, the
declared wheel matrix, payload identity checks, and attestation inputs. The one-shot native Linux
ARM64 installer is explicitly rejected with the container route documented instead of silently
pretending support.

Two additional live installer issues were corrected. Service handoff previously accepted the broad
`/api/health` endpoint even when `/api/health/ready` was failing; it now requires authoritative
readiness. A fresh install whose destination did not yet exist also scanned every local PID and
emitted noisy cwd errors; the absent-root path now skips that scan quietly without weakening upgrade
ownership checks.

The prior complete post-hardening release-payload campaign produced one `py3-none-any` wheel and one
sdist. Strict Twine validation passed. Its release payload verifier checked exact
distribution/version identity, all five artifact checksums, an SBOM containing the exact Kestrel
`0.4.0` component, and 67 exact hash-locked runtime requirements. Fresh Python 3.12 environments
installed and exercised the wheel and sdist without checkout shadowing; both passed `pip check`,
package-resource checks, `doctor`, mock chat, and real Memvid write/reopen/retrieval. The exact-wheel
verifier first exposed a
direct-script import failure and then a macOS uv-managed-Python dylib failure caused by copying the
interpreter. Both invocation modes are covered, POSIX venvs now preserve the interpreter symlink,
and the full verifier passes on ARM64 macOS.

The CycloneDX inventory contained 60 release components, all required top-level runtime
components, and none of pytest, Ruff, or mypy. `pip-audit` found no known vulnerability in installed
third-party dependencies; it explicitly skipped only the unpublished `nested-memvid-agent 0.4.0`
identity because that version does not exist on PyPI. Evidence lives under
`/tmp/kestrel-final-validation.K8CPk5/posthardening/release-payload-final/` and
`posthardening/package-evidence/`.

The current exact local tree produced wheel SHA-256
`01c795257d726bc1efb80a74c3818afdabe5f1674167e7d312a44447ea710664` (836,328 bytes) and
sdist SHA-256 `6fdcf092740e5c498cc2ce1ed4352b1dbb0508a73c7dbcecf53e1c549566c594`
(798,913 bytes). They are local validation evidence under
`/tmp/kestrel-v040-package-final.gI9Nyz`, not publication artifacts. The tag workflow must
independently rebuild and attest the final exact commit; only those workflow-produced artifacts and
checksums are publication authority.

These controls and artifacts are still not proof of publication. The `v0.4.0` metadata is now
tag-ready, and `uv run python scripts/check_project_metadata.py --release-tag v0.4.0` passes.
Publication still requires the exact hosted and administrative gates below.

## GitHub security controls: corrected current state

Read-only GitHub API checks on 2026-07-20 show that the public repository's controls are not broadly
disabled, as an earlier draft incorrectly stated:

- private vulnerability reporting: **enabled**;
- Dependabot security updates and vulnerability alerts: **enabled**;
- secret scanning: **enabled**;
- secret-scanning push protection: **enabled**;
- secret-scanning non-provider patterns: **unavailable under the current plan**;
- secret-scanning validity checks: **unavailable under the current plan**.

Active ruleset `19198902` protects the default branch. It disallows deletion and non-fast-forward
updates, has no bypass actor, requires pull requests, one approving review, review-thread
resolution, and the strict `docker` status check. `docker` is the aggregate CI gate through its
workflow dependency graph; individual lanes are not independently named in the ruleset.
Active tag ruleset `19299564` targets `refs/tags/v*`, has no bypass actor, and blocks every update
or deletion. Release tags may be created deliberately but cannot then be moved or removed while the
ruleset remains active.

The successful hosted CI run `29546715768` belongs to `origin/main` SHA
`d8ceb10dc1fdd48e93d09c798cada842cf4ff46a`, completed on 2026-07-17. It does not certify the current
candidate diff.

## Historical credential incident status

GitHub secret-scanning alert 1 was resolved as revoked at `2026-07-20T22:08:08Z` after the provider
returned `API_KEY_INVALID`; the repository now has zero open secret alerts. Historical public commit
`6f9d9b4102dd7fdd0384ecf691d40d2ce00637ed` contains Google-key material at:

- `list_models.py:5`;
- `trigger_chat_internal.py:53`.

This clone also has a credential-shaped Gmail artifact at
`mcp-servers/gmail/credenials.json:8` in commit
`b8d9bb42a21ddbfb724ed97780c9411df7e4358d`, reachable through the local
`refs/original/refs/heads/main` ref. No secret value is reproduced here.

A history-rewrite dry run exists at `/tmp/kestrel-history-rewrite-20260720.g26zEJ`, but no rewrite
was executed. The revoke-first response removes credential authority while avoiding a disruptive
force update that cannot erase forks, clones, logs, or caches. The Gmail-shaped artifact is reachable
only through a local `refs/original` ref and was never part of the public remote.

## Defects found and corrected during the campaign

The campaign found and addressed release-significant problems rather than only rerunning tests:

1. CLI/provider wait budgets and summary retry/timeout handling could outlive shutdown or convert a
   completed turn into a failure.
2. Native tool-call parsing, continuation, bounded catalog discovery, and duplicate suppression were
   insufficiently strict.
3. Disposable Memvid sidecars could become de facto authority; logical chunk replay, pagination,
   chain verification, and logical record IDs needed hardening.
4. Stable-memory promotion evidence could be replayed or misbound; ordinary unvalidated learning
   could imply a stable write. Promotion now binds evidence, provenance, confidence, validation,
   subject/run/bucket identity, and fails closed; ordinary activity cannot write policy.
5. Memory and repair signing-key first-open publication had concurrency and crash-consistency races.
6. Legacy backup restore could preserve incompatible trust material.
7. Golden evaluation could print aggregate failure but exit zero; two validation fixtures were not
   real Git worktrees; and OCI commands could embed the host Python executable path. The runner now
   gates failure, derives portable bounded Git workspaces from the evaluation root, and normalizes
   Python execution to the container interpreter. The first real rerun caught this; the corrected
   authoritative reruns passed 21/21 on each backend.
8. Load evidence could blur failure and capacity rejection. Schema v3 now enforces explicit load or
   saturation semantics, exact accounting, overload ratios, throughput/latency gates, and capsule
   completion.
9. The workbench had selected-thread refresh, keyboard accessibility, ARIA, focus, and contrast
   defects.
10. MCP close failure could leave a live stdio worker untracked during reconfiguration.
11. Hash-locked fresh install omitted a transitive build dependency, and staged artifact URLs could
    be caller-overridden.
12. Release validation did not bind the exact tag, main ancestry, wheel matrix, payload, and hosted
    checks tightly enough.
13. A corrupt disposable vector SQLite index could leave canonical memory available but prevent a
    later rebuild. Rebuild now validates and resets only exact owner-private SQLite main/WAL/SHM or
    journal files and rejects symlink or hardlink targets before reopening and repopulating.
14. Ambient and repository-local Git filters, hooks, fsmonitor, text conversion, diff drivers, or
    protocol helpers could influence repair snapshots and plugin fetches. Git execution is now
    absolute, sanitized, filter-neutralized, bounded, and regression-tested with hostile config.
15. Failed OCI cleanup could lose the container ownership record, and extension filesystem state
    could diverge from SQLite on partial install/update/remove failure. Cleanup ownership and
    extension transactions are now retained until exact rollback or verified completion.
16. Internal tool execution IDs leaked into non-opted-in custom tool arguments, while timeout
    handling could report failure after a committed result. Private metadata routing, bounded
    cancellation/settlement, nonretryable unresolved-outcome reconciliation, and a runtime-scoped
    quarantine fence now preserve the public approval/result contract.
17. Installer handoff accepted liveness instead of readiness and fresh installs performed a noisy
    whole-machine PID scan. Both paths now use exact lifecycle state.
18. The exact-wheel verification script's direct CLI form failed even though its module form worked.
    Both invocation modes are now regression-tested.
19. The read-only Compose profile redirected memory, logs, state, skills, config, and secrets to the
    writable `/data` volume but omitted plugins; startup therefore tried `/app/.nest/plugins` and
    failed before readiness. Dockerfile and Compose defaults now bind plugins to `/data/plugins`,
    create and own that directory, and assert the wiring in packaging tests.
20. An asynchronous interruption during `Thread.start()` could leave a live tool worker using memory
    after agent teardown, and sibling subagents could collide when a provider recycled a call ID.
    Launch is now inside the fail-closed lifecycle fence and durable subagent origins namespace exact
    calls; adversarial start/wait interruption and sibling-concurrency regressions pass.
21. Provider histories accepted unmatched or non-contiguous native tool results. Exact call IDs,
    duplicate rejection, and complete contiguous result batches are now enforced at every supported
    native provider serialization boundary.
22. The tag workflow scanned Git history and would fail on already revoked historical bytes, while
    immutable-release enablement was checked only after GHCR mutation. It now archives and scans only
    the exact release commit and proves immutable releases are enabled before the first publication
    mutation.
23. The supported legacy `memory restore` command caught ordinary exceptions but not process-control
    interruptions around its live-directory swap. It now rolls back on `BaseException`, preserves
    incomplete-recovery evidence, and has a fault-injection regression proving exact original bytes
    survive a `KeyboardInterrupt` after the live directory moves.
24. The first exact pull-request run exposed four portability assumptions: ShellCheck 0.9 treated
    EXIT-trap callbacks as unreachable; Windows mypy could not type POSIX-only `os` and `signal`
    members; a container `RLIMIT_NPROC` inherited host-real-UID accounting despite an existing
    cgroup PID limit; and several tests depended on built web assets, global Git identity, symlink
    traversal, or overly tight cleanup timing. Platform primitives are now dynamically and strictly
    typed, the redundant UID-scoped limit is removed while `--pids-limit=64` remains, fixtures are
    hermetic, symlinks are consistently excluded, and unresolved tool outcomes retain their bounded
    fail-closed quarantine behavior.
25. The second pull-request run passed all non-Windows lanes but produced 173 failures in the native
    Windows lane. The failures exposed POSIX assumptions and security gaps around directory
    replacement, CRT file-lock semantics, junction/reparse traversal, drive-letter parsing,
    descriptor lifetime, private temporary-directory ACLs, Windows-reserved path spellings, and
    rollback after validation failure. The isolated release tree now uses identity-pinned,
    reparse-rejecting path operations; native `LockFileEx`/`UnlockFileEx` shared and exclusive
    locking; rollback-safe directory moves; protected user/SYSTEM/Administrators ACLs for private
    MCP snapshots; Windows-aware canonical scope checks; safe descriptor close ordering; and
    targeted portable plus native-Windows regressions. This correction was independently reviewed
    and locally green; the third hosted run confirmed broad progress while exposing the narrower
    follow-up recorded in item 26.
26. The third pull-request run reduced the remaining cross-platform failures to one macOS 3.12
    cleanup-timing case and 90 Windows cases. Those failures exposed SQLite handles retained across
    directory publication, `LockFileEx` contention surfaced as Windows errors 32/33, text-mode raw
    file I/O that changed exact bytes, a built-in Administrator `LA` owner alias that did not compare
    directly with its machine-relative SID, Windows-canonical `.git.` control-tree traversal,
    descriptor cleanup skipped by a support-bundle swap failure, Git `core.autocrlf` false-positive
    worktree drift, and tests that conflated scheduling latency with lifecycle correctness. The final
    follow-up explicitly closes backup/verifier handles; normalizes Windows contention; uses binary
    mode for security-sensitive fingerprints and artifact bytes; compares Windows owner aliases
    semantically while retaining the strict trustee set; rejects canonical control trees; guarantees
    descriptor cleanup; ignores CR-only worktree drift without hiding staged changes; and waits on
    durable publication fences with independent lease budgets. Portable regressions cover each
    change, the 1,821-test local aggregate and all enabled integrations pass, and the exact follow-up
    received independent review with no P0-P3 findings. Hosted macOS and Windows execution for the
    exact commit remains mandatory.
27. The fourth pull-request run reduced the remaining failures to one macOS 3.11 installer case and
    22 Windows cases. The macOS failure proved that a terminated detached supervisor can remain
    visible briefly as an unreaped zombie; installer cleanup now distinguishes that exited state
    from a process capable of retaining descriptors, listening, or executing, with a real-zombie
    regression. Eleven backup/CLI failures came from test-owned SQLite connections whose context
    managers committed but did not close native handles; fixtures now close those handles
    deterministically and verify helper ownership. A separate mode assertion now preserves execute
    intent only when the native source can express it. The Windows repair failures exposed
    CPython's lack of directory-descriptor and `*at` rollback primitives; Windows now uses an
    owner-private, reparse-rejecting path fallback with parent and quarantine identity
    revalidation around atomic moves, while POSIX retains its stronger descriptor path. The
    remaining failures were exact-test
    defects: a synthetic `O_BINARY` flag discarded the real Windows flag, fixtures bypassed the MCP
    private-directory initializer, a safe validation subprocess omitted required Windows
    system/temp variables, and two scheduler assertions used loaded-runner wall-clock budgets as
    lifecycle proofs. The final follow-up corrects each cause, adds the forced Windows rollback and
    zombie regressions, passes the two macOS cleanup proofs in five consecutive paired reruns,
    passes 1,823 aggregate tests plus all enabled integrations and evaluations, and received
    independent review with no P0-P3 findings. Exact-head hosted execution remains mandatory.

## Independent cross-platform hardening review

Independent code review approved the current follow-up diff with no P0, P1, P2, or P3 findings. The
review covered WinAPI and SDDL use, recursive MCP snapshot ACL enforcement, support-bundle parent
identity pinning, repair cleanup descriptor ordering, the Windows rollback fallback and its
owner-private/reparse/identity boundaries, canonical Windows path checks, binary-mode fingerprints,
Git line-ending behavior, zombie-state handling, and the separation of staged from unstaged changes.
This is code-review evidence, not a substitute for the repository ruleset's human approval or
exact-head CI.

Three residuals remain explicit:

- Native Windows ACL, junction/reparse, `LockFileEx`, rollback, and binary-mode behavior plus macOS timing
  still require hosted exact-head CI; those tests cannot be certified across the full matrix by the
  ARM64 macOS host.
- A nonempty legacy `mcp_artifacts` tree whose ACL cannot be proven private now fails closed and must
  be deleted and reapproved instead of being silently hardened in place.
- Same-account adversarial races remain outside Kestrel's documented single-owner isolation
  boundary; the changes close the reviewed in-process and filesystem-swap cases, not hostile peer
  processes running as the same operating-system identity.

## Residual limitations

- Qwen `qwen3:4b` is functional but slow and variable on the tested laptop: about 82 s average per
  benchmark task and 143 s for the slowest task.
- The larger retrieval benchmark's absolute recall (0.449) and precision (0.090) are not yet strong
  enough to call memory quality solved, especially for episodic queries.
- Memvid has materially higher latency than the in-memory backend and can add sealing/reopen cost.
- Memvid SDK 2.0.160 `doctor` reports duplicate `vec_index_corrupt` warnings for each newly
  initialized empty layer (`unsupported vector index encoding`) even though all six layers verify
  and remain usable. The warning is non-fatal but noisy and should be resolved upstream or normalized
  only with a narrowly proven empty-index condition.
- Primary-agent concurrency is deliberately one in the supported local profile, so long model or
  Memvid operations cause head-of-line queue delay.
- Golden cost is unmeasured. Credentialed non-Ollama providers remain uncertified.
- Local stdio containment is lifecycle-coupled; hostile detached descendants or another process need
  OCI/remote isolation.
- The current local wheel/sdist gate used Python 3.11.15 on ARM64 macOS; the prior complete
  release-payload verifier used Python 3.12.11 on that host. Python 3.12/3.13 package execution and
  Linux/macOS/Windows matrix coverage remain hosted exact-commit release gates.
- A validation image must be immutable and must preload the language/toolchain required by the
  project under test. Kestrel cannot infer that toolchain from the digest alone.
- Local multi-architecture container proof used native ARM64 and AMD64 under QEMU, not native x86
  hardware. The final images used a mock provider and remain unpublished, unsigned local BuildKit
  artifacts without registry-side vulnerability or provenance evidence.
- The local SBOM and dependency audit are evidence, not a signed public attestation. Published
  provenance, registry visibility, tag immutability, and post-publication install/rollback remain
  untested.
- `0.4.0` is intentionally unpublished, but its release-tag metadata gate passes. The current
  final follow-up tree still needs exact-head hosted CI. Its 1,823-pass local aggregate, integration,
  evaluation, learning, and rebuilt package gates pass, but workflow-built artifacts remain
  publication authority; further holds are human review and publication
  configuration/evidence.
- Temporary `/tmp` evidence is local campaign evidence, not a durable public attestation store.

## Public-release blocker sequence

1. Require successful CI, including native Windows and both macOS lanes, for the exact PR #270 head
   containing this locally green, independently reviewed final follow-up.
2. Add a trusted reviewer/collaborator and obtain the ruleset-required independent approval.
   `John-MiracleWorker` is currently the only collaborator, so self-approval cannot close this gate.
3. In PyPI, register the pending Trusted Publisher for project `nested-memvid-agent`, owner
   `John-MiracleWorker`, repository `Kestrel`, workflow `release.yml`, and environment `pypi`. The
   GitHub environment already requires explicit owner approval and disallows admin bypass.
4. Merge the reviewed exact commit and require its successful strict aggregate `docker` main-push CI.
5. Enable repository immutable releases after `release.yml` is merged and before creating `v0.4.0`.
6. Tag that exact main commit. On first workflow publication, make the new GHCR `kestrel` package
   public, then use **Re-run failed jobs** within the one-day artifact-retention window so the
   anonymous-pull gate reuses the exact built images.
7. Verify immutable GitHub assets, attestations, GHCR platform/digest identity, PyPI filename/SHA
   identity, clean install, upgrade, and rollback.

Secret-scanning validity and non-provider features are unavailable under the current plan. This is
a documented platform limitation, not a code or credential-response blocker.

## Bottom line

Kestrel now clears the central functional question for a local personal agent: it converses through
a real model, uses tools, persists and reopens canonical Memvid v2 memory, learns behind evidence
gates, blocks the exercised unapproved high-risk and sensitive-file operations, behaves predictably
under bounded overload, and exposes a usable, accessible workbench. The second hosted run showed
that the prior candidate was not yet cross-platform release-ready: all non-Windows lanes passed, but
Windows failed 173 tests. The third run passed all but one macOS 3.12 cleanup-timing test and 90
Windows tests, turning the remaining failures into a focused descriptor, locking, binary-I/O, ACL,
line-ending, and lifecycle-fence follow-up. The fourth run then passed every independently runnable
upstream lane other than macOS 3.11 and Windows; those lanes had one zombie-sensitive cleanup proof
and 22 cases, respectively, isolating the final process-state,
test-owned handle, rollback-primitive, binary-harness, fixture, and loaded-runner issues. The current
diff has independent code-review approval with no P0-P3 findings, and its focused/static gates,
integrations, golden evaluations, learning benchmark, and 1,823-pass aggregate suite are green.
Fresh local package evidence is recorded above. A green exact-head hosted run remains pending, and
the local packages are evidence rather than publication authority.
After that code-side gate,
controlled publication still requires independent human approval, PyPI OIDC registration,
immutable release enablement, tagging, first GHCR visibility, and post-publication verification.
The historical Google credential is revoked and its alert resolved; a destructive history rewrite
is intentionally not required.
