# Everyday Kestrel v0.5 Implementation Plan

> Execute task by task with `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`. Review every trust-boundary task independently.

**Goal:** Extend the verified launch façade into the smallest task-first
engineering journey: register a project, build a digest-keyed repository index,
discover a usable provider target, inspect a mission preflight, run existing
durable orchestration, and review evidence without weakening Kestrel's current
single-user/private-node contract.

**Checkpoint:** This plan starts at `33664c7` on
`feat/everyday-kestrel-v05`. The qualified launch checkpoint remains on
`feat/kestrel-launch-ease-p0`.

## Global constraints

- Keep Memvid v2 `.mv2` memory and the SQLite control plane distinct.
- Keep the repository index rebuildable and outside canonical memory.
- Never make stale index evidence authoritative; every result carries the
  indexed content digest and freshness state.
- Project capability policy may only narrow global owner capabilities.
- Project export excludes global self/policy memory and secrets by default.
- Preserve exact-call approval, isolated repair, literal-tree commit,
  validation receipt, rollback, and protected-branch invariants.
- Provider probes are bounded, evidence-producing, and never turn an
  ineligible target into an eligible one.
- Keep deterministic mocks and local fixtures for all default tests.
- Run `pytest -q` after each phase. Keep Memvid integration behind
  `RUN_MEMVID_INTEGRATION=1`.

## Task 1 — Durable project profiles

**Files**

- Add `src/nested_memvid_agent/projects.py`
- Modify `src/nested_memvid_agent/state_store.py`
- Modify `src/nested_memvid_agent/server_models.py`
- Add `src/nested_memvid_agent/server_project_routes.py`
- Modify `src/nested_memvid_agent/server.py`
- Add `tests/test_projects.py`
- Add `tests/test_server_projects.py`
- Modify schema migration tests

**Contract**

- Add schema v20 with a `projects` table and nullable `project_id` on new runs.
- A project stores: stable ID, display name, canonical repository path,
  optional remote, default branch, allowed relative paths, provider policy,
  cost budget, privacy class, test/build recipes, capability ceiling,
  baseline index digest, timestamps, and optimistic revision.
- Canonical repository paths must be absolute, existing, owner-controlled
  directories; reject symlink roots and duplicate canonical paths.
- Capability ceilings accept only known capability keys and may never widen
  the active global configuration.
- Implement create/list/get/update/archive plus redacted export/import.
- Export omits secret values, global self/policy content, runtime state, and
  unrelated projects.
- Add `/api/projects`, `/api/projects/{id}`, and export/import routes with
  strict request models and 409 revision conflicts.

**Acceptance**

- Schema 19 upgrades transactionally to 20 and reopens idempotently.
- Two projects with different paths cannot retrieve or mutate each other's
  profile/recipe data.
- Import/export round trips only reviewable project metadata.
- Existing runs and databases remain readable with `project_id = null`.

## Task 2 — Digest-keyed repository index

**Files**

- Add `src/nested_memvid_agent/repo_index/__init__.py`
- Add `src/nested_memvid_agent/repo_index/models.py`
- Add `src/nested_memvid_agent/repo_index/store.py`
- Add `src/nested_memvid_agent/repo_index/indexer.py`
- Add `src/nested_memvid_agent/repo_index/parsers.py`
- Add `tests/test_repo_index.py`
- Add `tests/fixtures/repo_index/**`

**Contract**

- Store one rebuildable SQLite index per project under
  `.nest/repo-index/<project-id>.sqlite`; do not write `.mv2`.
- Record index schema, project ID, repository root identity, Git HEAD/tree
  identity when available, aggregate content digest, indexed timestamp, and
  parser versions.
- Record bounded regular files, file digests, language, symbols, imports,
  lexical references, test files, and symbol/test relationships.
- Parse Python with `ast`; provide conservative language adapters for
  JavaScript/TypeScript, Go, Rust, Java/Kotlin, Swift, and a text fallback.
- Ignore VCS metadata, build/cache/vendor directories, symlinks, private
  Kestrel paths, binary files, and files above configured limits.
- Incremental rebuild hashes only changed candidates and removes deleted-file
  rows in one transaction.
- A query must recompute/compare a cheap repository freshness fingerprint.
  Stale results are labeled stale and excluded from authoritative context
  packs unless the caller explicitly requests diagnostic stale evidence.

**Acceptance**

- Incremental no-change rebuild does not rewrite unchanged file/symbol rows.
- Content change and deletion update the aggregate digest deterministically.
- Symbol definitions, imports, references, and test ownership match the
  multi-language fixture.
- Moving or replacing the repository root fails closed.

## Task 3 — Repository intelligence tools and context packs

**Files**

- Add `src/nested_memvid_agent/tools/repository_intelligence_tools.py`
- Modify `src/nested_memvid_agent/tools/builtin.py`
- Modify `src/nested_memvid_agent/tools/base.py` only if an index handle is
  required in `ToolContext`
- Modify `src/nested_memvid_agent/context_compiler.py`
- Add `tests/test_repository_intelligence_tools.py`
- Add `tests/test_repository_context_compiler.py`

**Contract**

- Add bounded read-only tools:
  `repo.symbols`, `repo.references`, `repo.dependencies`, `repo.tests_for`,
  `repo.impact`, and `repo.context_pack`.
- Results include exact relative path, 1-based line, symbol kind, evidence
  relation, file digest, index digest, and freshness.
- Blend lexical matches, exact/qualified symbols, imports, references, and
  test ownership with deterministic tie-breaking.
- Context packs are bounded by characters/tokens/files, separate facts from
  inferred impact, and never label stale or fallback text as authoritative.
- Use the pack during task decomposition and reviewer impact analysis without
  replacing current untrusted-memory labeling.

**Acceptance**

- A fixture rename task returns every expected definition/reference before
  mutation.
- `repo.tests_for` prefers owned tests and falls back truthfully.
- The curated navigation fixture reaches recall@5 at least 0.70.
- All tool paths remain within the project workspace and allowed-path ceiling.

## Task 4 — Project/index API and bootstrap preflight

**Files**

- Add `src/nested_memvid_agent/server_repository_routes.py`
- Modify `src/nested_memvid_agent/server.py`
- Modify `src/nested_memvid_agent/server_models.py`
- Add `src/nested_memvid_agent/mission_control.py`
- Add `src/nested_memvid_agent/server_mission_routes.py`
- Add `tests/test_server_repository.py`
- Add `tests/test_mission_control.py`

**Contract**

- Add project-scoped index status/rebuild/search/context endpoints.
- Add a read-only mission preflight projection containing project identity,
  Git status, index freshness, selected route policy, bounded budget,
  effective capability ceiling, likely approvals, validation recipes, and
  blocking checks.
- Provide goal templates: explain repository, fix failing test, implement
  feature, safe refactor, security review, and documentation.
- The projection uses existing run/task/approval/review truth and never creates
  a run or mutates a repository.

**Acceptance**

- Preflight answers what will happen, why, what is blocked, expected proof,
  permissions, and rollback.
- A dirty tree is visible, not silently normalized.
- Missing/stale index and disconnected provider are explicit blockers or
  warnings according to the chosen template.

## Task 5 — Provider discovery and bounded target bootstrap

**Files**

- Modify `src/nested_memvid_agent/llm/model_catalog.py`
- Add `src/nested_memvid_agent/provider_probe.py`
- Modify `src/nested_memvid_agent/provider_certification.py`
- Modify `src/nested_memvid_agent/server_routing_routes.py`
- Add `tests/test_provider_probe.py`
- Add `tests/test_server_routing_discovery.py`

**Contract**

- Reuse live Ollama, LM Studio, and OpenAI-compatible `/models` discovery.
- Persist catalog freshness and probe evidence, not hard-coded UI authority.
- Bounded probes cover generation, streaming, structured output, tools,
  vision declaration, observed latency, and model identity where supported.
- Create disabled target drafts first; operator confirmation controls trust,
  locality, budget, and affinity.
- Provide presets that only constrain: Local Only, Balanced,
  Cheapest Validated, Fastest, Frontier Review, Privacy First.

**Acceptance**

- A local fixture becomes a validated target draft without JSON editing.
- Removed models become stale and are excluded from new route decisions.
- Every capability is marked observed, provider-declared, operator-supplied,
  or unknown.

## Task 6 — Task-first Mission Control surface

**Files**

- Add `web/src/mission/**`
- Modify `web/src/App.tsx`
- Modify `web/src/types.ts`
- Add/modify frontend tests

**Contract**

- Make project selection plus “What should Kestrel accomplish?” the default
  first-run surface.
- Render templates, preflight, editable acceptance-oriented task graph,
  approvals/tool activity/recovery timeline, and coherent result projection.
- Put routing, memory, MCP, plugins, channels, and administration behind
  progressive disclosure without deleting them.
- Never show Ready without the connected proof implemented in the launch
  checkpoint.

## Task 7 — Review and local acceptance surface

- Build syntax-aware unified/split diff, changed-symbol/test-impact summary,
  acceptance-to-evidence mapping, risk/rollback, and learning summary.
- Gate revision/export/local branch/local commit actions on a current signed
  review artifact.
- Keep GitHub PR creation behind explicit capability and exact-call approval;
  do not publish in this plan.

## Task 8 — Determinism, Windows diagnostics, and release rehearsal

- Replace relevant wall-clock assertions with monotonic deadline polling.
- Add a 20-repeat deterministic everyday golden lane and flake report.
- Add supported WSL2/Docker Desktop doctor checks plus a PowerShell bootstrap
  that diagnoses rather than silently mutates prerequisites.
- Add disposable repository/package-namespace release rehearsal before any
  immutable production tag path.

## Final verification

```bash
git diff --check
bash -n install.sh scripts/installer-server-supervisor.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src .venv/bin/python -m pytest -q
npm test --prefix web -- --run
npm run build --prefix web
```

Also run a disposable project bootstrap/index/preflight smoke and verify no
test listener, process, state database, `.mv2`, index, or generated build
artifact remains in either accepted worktree.
