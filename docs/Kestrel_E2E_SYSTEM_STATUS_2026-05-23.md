# Kestrel End-to-End System Status

Date: 2026-05-23 EDT

Scope: end-to-end local validation of Kestrel, with emphasis on memory, learning, and self-improvement. Live-provider checks used Ollama Cloud with `deepseek-v4-pro`. The API key was supplied only as a process environment variable for live commands and is intentionally not recorded here.

## Executive Summary

Kestrel is functioning as a local-first agent runtime, not just a memory library. The CLI, FastAPI control plane, web app build, deterministic test suite, Memvid `.mv2` substrate, memory promotion gates, learning eval harnesses, approval gates, and live Ollama Cloud provider path all passed the checks run in this pass.

The strongest result is the memory/learning stack: deterministic memory-system evals passed 9/9 cases; real Memvid integration passed; live Ollama Cloud learning E2E passed 7/7 cases on both `memory` and `memvid` backends; and the live Memvid run created the expected one `.mv2` file per permanent layer plus a run-scoped `complete.mv2` capsule.

The main caveat is product maturity. Kestrel's own product-readiness report still marks the product as not production-ready because production auth/users/workspaces are missing. Safe autonomous learning now has an opt-in, low-risk-only auto-activation path through the mutation gate; code-changing self-improvement remains bounded by review, repair, rollback, and approval gates rather than fully autonomous commits.

## Test Evidence

| Area | Command or check | Result |
| --- | --- | --- |
| Python baseline | `.venv/bin/python -m compileall -q src tests scripts` | Passed |
| Unit suite | `.venv/bin/pytest -q` | Passed |
| Golden evals | `.venv/bin/python scripts/run_golden_evals.py --backend memory --provider mock` | Passed, 21/21 cases |
| Memory evals | `.venv/bin/python scripts/run_memory_system_evals.py --backend memory --provider mock` | Passed, 9/9 cases |
| Learning architecture evals | `.venv/bin/python scripts/eval_learning_architecture.py --provider mock --backend memory --all --json` | Passed, 5 passed, 1 skipped live-only scenario |
| CLI chat | `PYTHONPATH=src .venv/bin/python -m nested_memvid_agent.cli chat --backend memory --provider mock --message hello` | Passed, completed mock response |
| Memvid focused integration | `RUN_MEMVID_INTEGRATION=1 .venv/bin/pytest -q tests/integration/test_memvid_memory_system.py` | Passed, 1/1 |
| Memvid backend/context integration | `RUN_MEMVID_INTEGRATION=1 .venv/bin/pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py` | Passed, 6/6 |
| Live provider smoke | `RUN_PROVIDER_INTEGRATION=1 ... test_provider_live_integration.py -k ollama` with Ollama Cloud `deepseek-v4-pro` | Passed for Ollama Cloud generate and stream; local Ollama cases skipped |
| Live learning E2E, memory backend | `scripts/run_live_learning_eval.py --provider ollama-cloud --model deepseek-v4-pro --backend memory` | Passed, 7/7 |
| Live learning E2E, Memvid backend | `scripts/run_live_learning_eval.py --provider ollama-cloud --model deepseek-v4-pro --backend memvid` | Passed, 7/7 |
| Ollama Cloud model catalog | live model catalog fetch | Passed, provider returned 39 models and included `deepseek-v4-pro` |
| Web tests | `npm run test --prefix web` | Passed, 3 files, 30 tests |
| Web build | `npm run build --prefix web` | Passed |
| Setup readiness | `nest-agent product setup --backend memory --provider mock --json` | Ready true, 6 pass, 2 warn, 0 fail |
| Provider certification report | `nest-agent product provider-certification --backend memory --provider mock --json` | Release certified false; mock certified; several providers blocked without env credentials |
| Support bundle | `nest-agent product support-bundle ... --json` | Passed; bundle manifest states raw secrets excluded and env vars presence-only |
| Local API smoke | isolated server on `127.0.0.1:8799` | Passed health, runtime config, memory layers, product readiness, create run, get run, task graph, session runs, memory search |
| Browser/UI smoke | headless Google Chrome against `http://127.0.0.1:8799/` | Passed; title `Nested MV2 Agent`, visible Kestrel cockpit content loaded |

## Memory Status

Status: working.

What works:

- The six permanent layers are present in the contract: `working`, `episodic`, `semantic`, `procedural`, `self`, and `policy`.
- Deterministic evals confirmed the expected default `.mv2` filenames: `working.mv2`, `episodic.mv2`, `semantic.mv2`, `procedural.mv2`, `self.mv2`, and `policy.mv2`.
- Real Memvid integration passed and the live Memvid E2E created these files under `/private/tmp/kestrel-live-learning-ollama-memvid/memory/`.
- Run-scoped task capsule output created `/private/tmp/kestrel-live-learning-ollama-memvid/runs/.../complete.mv2`.
- Retrieval works across layers. The API memory search after a mock run returned both the episodic turn summary and working user-message record.
- Context packing works in the tested contracts: summary-first packing, exact raw expansion, conflict warnings, and trust-order rendering all passed.
- Correction/tombstone behavior works in deterministic evals and live E2E. Corrected records are hidden from normal retrieval but auditable through inactive retrieval paths.
- Policy-write safety works. Ordinary events did not promote to policy, direct policy writes are blocked, and stable direct `memory.write` remains rejected.

Problems or caveats:

- The isolated API server smoke used the `memory` backend, so `/api/memory/layers` correctly reported `InMemoryBackend` with `exists=false` for `.mv2` paths. Real `.mv2` existence was verified separately through the Memvid integration and live Memvid E2E outputs.
- The live memory-backend `correction_frame` case passed but stopped with `stop_reason=max_tool_rounds`; the equivalent Memvid case completed normally. This suggests the live model/tool loop can spend too many rounds on correction-style tasks in at least one harness path.
- The live Memvid `memory ledger` command against the live E2E state returned no promotion ledger rows. The deterministic memory eval proves ledger rows can be produced, but this specific live harness records behavior-delta activation evidence more than promotion-outcome evidence.

## Learning Status

Status: working for gated learning and opt-in autonomous low-risk behavior deltas.

What works:

- `scripts/run_memory_system_evals.py` passed all 9 cases, including layer contracts, retrieval, context packing, promotion gates, correction/tombstone behavior, promotion ledger behavior, tool surface checks, cross-layer flow, and backend consistency.
- `scripts/eval_learning_architecture.py --provider mock --backend memory --all --json` passed all compatible mock scenarios: changed-strategy retry behavior, canonical `.mv2` constraint, policy-write approval staging, repeated tool failure delta generation, and rollback of an active delta. The live-only scenario was skipped as expected in mock mode.
- Live Ollama Cloud learning E2E passed 7/7 on both `memory` and `memvid` backends:
  - provider handshake
  - durable memory reopen
  - correction frame
  - procedural promotion gate
  - task capsule learning signal
  - unapproved high-risk tool blocked
  - behavior-delta activation logging
- The live Memvid learning dashboard saw one procedural behavior-delta activation, with no rollbacks and no false positives.
- After this E2E pass, Kestrel gained a default-off low-risk auto-activation path: staged/proposed low-risk deltas can become active before behavior compilation only when `enable_auto_activate_low_risk_deltas` is enabled and explicit validation metadata plus `MutationGate` repeat/replay/evidence/rollback checks pass. The ledger records these with `auto_activated_low_risk_threshold_met` for dashboard visibility.

Problems or caveats:

- Behavior-delta runtime compilation remains default-off in normal runtime config. Auto-activation is also default-off and applies only to low-risk deltas; ordinary runs will not automatically use deltas unless behavior deltas and low-risk auto-activation are explicitly enabled.
- The live behavior-delta ledger had one active activated delta but no recorded useful/ignored/failed outcome. Outcome capture needs a richer real-run feedback loop before autonomous learning can claim measurable improvement.
- Product readiness now treats safe autonomous learning as ready for the current local alpha scope; the remaining gap is broader validation-window analytics and live-provider regression coverage as learning volume grows.

## Self-Improvement Status

Status: bounded and reviewable; not fully autonomous.

What works:

- Behavior deltas can be proposed, staged, gated, compiled, activated, logged, reviewed, and rolled back in deterministic evals.
- High-risk tools remain exact-call approval gated. The live E2E confirmed `memory.import` is blocked without approval.
- Local self-improvement boundaries are intact: remote mutation and git push are disabled by default, protected branch policy is visible in runtime config, and dangerous capabilities are off in the isolated server smoke.
- Support-bundle export works and explicitly excludes raw secret values.
- Product readiness reports the repair/self-improvement stack as partial rather than pretending it is complete.

What does not work yet:

- Fully autonomous code self-improvement is not complete. Kestrel has repair primitives, behavior deltas, task graphs, approval gates, rollback paths, and review artifacts, but not a polished autonomous patch-propose-validate-review-commit loop.
- Production-grade plugin/skill sandboxing and managed dependency isolation remain partial.
- The API smoke created a completed run, but the task graph endpoint still showed the starter graph tasks as `queued` and `ready_tasks` even after the run itself completed. That may be intentional scheduler-resume metadata, but it is confusing and should be reconciled for the operator UI.

## Provider Status

Status: Ollama Cloud `deepseek-v4-pro` works live; full provider matrix remains partial.

What works:

- Ollama Cloud `deepseek-v4-pro` generated and streamed successfully in the provider integration smoke.
- Live learning E2E passed on both backend modes with `deepseek-v4-pro`.
- Live model catalog fetch returned 39 Ollama Cloud models and included `deepseek-v4-pro`.
- The provider adapter exposed usage metrics in live E2E handshakes.

Problems or caveats:

- The provider certification report is environment-sensitive. Without a persisted `OLLAMA_API_KEY`, `ollama-cloud` appears blocked in the normal report even though the live command passed when the key was supplied ephemerally.
- Release certification across OpenAI, OpenRouter, Anthropic, Gemini, Kimi, DeepSeek direct, local Ollama, and Codex CLI is still not complete.

## API And Web UI Status

Status: working locally; product UX still partial.

What works:

- Isolated FastAPI server launched successfully after escalation for localhost bind.
- `/api/health` returned `ok=true`.
- `/api/runtime/config` reported schema version `11`, provider/model, feature flags, git safety, limits, paths, and validation commands.
- `/api/runtime/settings` now persists `max_tool_rounds`; the Settings UI exposes it as “Max tool calls” for new runs.
- `/api/product/readiness` returned a structured product-readiness report.
- `/api/runs` created a background mock run, and `/api/runs/{id}` returned a completed result.
- `/api/memory/search?query=smoke` retrieved the just-created working and episodic records.
- Web unit tests and production build passed.
- Headless Chrome loaded the UI and saw the Kestrel cockpit content.

Problems or caveats:

- Local API auth is disabled by default. Setup readiness marks that as a warning and says it is acceptable only for trusted local development.
- Worker/worktree isolation is disabled by default. Setup readiness marks this as a warning for golden repair workflows.
- The product-readiness report is intentionally strict and still says product-ready is false.

## Environment And Tooling Issues Encountered

- Binding a local server inside the sandbox failed with `operation not permitted`; running the same server with local bind escalation succeeded.
- Sandboxed `curl` could not reach the escalated localhost server; escalated localhost probes succeeded.
- The in-app browser connector was unavailable in this session (`iab` not present).
- The Playwright skill's bundled Chromium executable was missing, so direct Playwright launch failed. Falling back to installed Google Chrome in headless mode worked.
- `zsh` treated the unquoted `?query` URL as a glob on the first memory-search probe. Quoting the URL fixed it.

## Recommended Next Fixes

1. Reconcile run lifecycle and task graph status so completed simple runs do not leave confusing queued starter tasks in operator surfaces.
2. Investigate why the live memory-backend correction-frame case reached `max_tool_rounds` while still producing the expected correction frames; the new user-facing Max tool calls setting can mitigate long tool loops while this is investigated.
3. Add live E2E outcome recording for behavior deltas so the learning dashboard can distinguish useful, ignored, and harmful activations in real runs.
4. Promote ephemeral live-provider evidence into the provider certification report without storing raw secrets.
5. Make the first-run/golden-repair path product-grade: enable worker isolation guidance, run repair through patch -> validate -> review -> approved commit, and surface clear rollback evidence.
6. Keep production auth/workspace isolation as the highest productization gap before any hosted or team-oriented use.
