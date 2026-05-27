# Kestrel Productization Roadmap

Last updated: 2026-05-21

## Thesis

Kestrel is beyond a demo technically: it has a real local agent runtime, deterministic mock evals, Memvid `.mv2` layered memory, approval gates, repair primitives, task graphs, behavior deltas, and a web control plane.

It is not yet a full product operationally. The remaining work is to make Kestrel boringly dependable, visibly safe, easy to adopt, and measurable.

The product promise should be:

> Kestrel is a local-first AI engineering agent that actually learns from its work, with audit, rollback, and safety gates.

Kestrel should not compete as another chatbot with tools. Its durable advantage is controlled learning: it gets measurably better over repeated engineering work without becoming reckless.

## Product identity

Recommended positioning:

- **Primary:** local-first AI engineering agent that learns from its work.
- **Secondary:** safe autonomous development cockpit with memory, approvals, repair, and rollback.
- **Differentiator:** behavior changes are typed, evidence-backed, validated, audited, and reversible.

Non-goals:

- Do not become a generic vector-memory chatbot.
- Do not replace `.mv2` as canonical durable memory.
- Do not allow hidden self-modification.
- Do not remove approval gates for high-risk actions.
- Do not ship executable skills/plugins without sandbox and review controls.

## Golden user journey

The first product-quality experience should be one excellent end-to-end workflow:

1. User installs Kestrel.
2. User connects or selects a repository.
3. Kestrel scans the repo and records a baseline.
4. User asks Kestrel to fix a failing test or implement a small feature.
5. Kestrel creates a visible plan.
6. Kestrel executes in an isolated branch/worktree.
7. Kestrel diagnoses failures and retries with changed strategy.
8. Kestrel proposes a patch.
9. Kestrel runs targeted and broader validation.
10. Kestrel creates a review artifact with diff, tests, risk, and rollback notes.
11. User approves commit/PR creation.
12. Kestrel records what it learned.
13. A later similar task uses that learning and shows measurable improvement.

Definition of done:

> A new developer can install Kestrel, connect a real repo, ask it to fix something, review the patch, approve it, and see what Kestrel learned.

## Productization pillars

### 1. Local product stability

Goal: make the local developer experience reliable before hosted/team work.

Required work:

- One-command install that consistently reaches a healthy app.
- Provider setup wizard with validation.
- Repo connection and baseline scan.
- Golden repair workflow.
- Clear local memory status and backup instructions.
- Deterministic smoke tests for first-run behavior.
- Demo repo with known failing tests.

Acceptance criteria:

- Fresh install succeeds on supported macOS/Linux environments.
- User can complete the golden workflow without reading implementation docs.
- Failed setup produces actionable recovery messages.

### 2. Safe autonomous learning

Goal: move from advisory memory to learning that acts by default only where safe.

Required work:

- Auto-activate low-risk validated behavior deltas. ✅ Implemented behind `NEST_AGENT_ENABLE_AUTO_ACTIVATE_LOW_RISK_DELTAS=0`, with `MutationGate` checks and audit rows.
- Keep medium-risk deltas staged for review.
- Keep high-risk and policy deltas approval-gated.
- Record outcomes for every active delta.
- Show activation, usefulness, false-positive, and rollback rates.
- Make rollback obvious and one-click in the UI.
- Replay behavior-delta scenarios routinely.

Acceptance criteria:

- Kestrel can prove a learned procedural delta changed future behavior.
- Rollback disables future compilation while preserving audit history.
- No unauthorized policy writes occur.

### 3. Safe repair and code modification

Goal: make Kestrel useful on real code without risking uncontrolled mutation.

Required work:

- Branch/worktree isolation by default for repair runs.
- Patch proposal flow.
- Targeted validation and full validation.
- Durable review artifact before commit.
- Approval-before-commit and approval-before-push.
- Stale review detection.
- Rollback artifacts.
- Optional PR creation behind explicit enablement.

Acceptance criteria:

- Repair branch commits cannot happen without current review artifact and exact-call approval.
- Protected branches are never mutated directly.
- UI shows diff, diagnosis, validation, risk, and rollback path.

### 4. Production auth, users, and workspaces

Goal: prepare Kestrel for hosted or team use.

Required work:

- Real user accounts and sessions.
- Workspace/project ownership.
- Role-based permissions.
- Per-user and per-project memory boundaries.
- Audit logs by actor/action.
- Token rotation and revocation.
- Production-safe CORS and session policy.

Acceptance criteria:

- One user cannot inspect or mutate another user’s runs, memory, repos, secrets, or approvals.
- Dangerous actions require permissions and audit trails.

### 5. Skills, plugins, MCP, and sandboxing

Goal: make extensibility powerful but not terrifying.

Required work:

- Container-grade or equivalent runtime isolation for executable skills/plugins.
- Dependency management for plugins and skills.
- Per-tool permission model.
- Filesystem and network scopes.
- Secret access scopes.
- Plugin review UX.
- MCP SSE/HTTP transport fixtures and soak tests.

Acceptance criteria:

- User can see what a plugin/skill/MCP server can access before enabling it.
- Risky tools remain blocked or approval-gated by default.

### 6. Provider and eval certification

Goal: make provider support trustworthy.

Required work:

- Credentialed validation matrix for OpenAI, OpenRouter, Anthropic, Gemini, Ollama Cloud, local Ollama/OpenAI-compatible, and Codex CLI.
- Provider-specific golden suites.
- Live learning E2E harnesses.
- Cost/time/call guards.
- Redacted reports.

Acceptance criteria:

- A release cannot claim provider support unless its certification suite passed.
- Mock tests remain deterministic and fast.

### 7. Product UX and onboarding

Goal: turn implemented surfaces into a coherent cockpit.

Required work:

- First-run setup wizard.
- Repo connection screen.
- Provider/model validation screen.
- Run timeline and task graph explanation.
- Approval inbox.
- Patch/diff review screen.
- Learning dashboard.
- Memory health panel.
- Rollback panel.
- Empty states and guided next actions.

Acceptance criteria:

- User can always answer: what is Kestrel doing, why is it doing it, and how can I undo it?

### 8. Operations and release engineering

Goal: make Kestrel maintainable as software, not just impressive as code.

Required work:

- Versioning and changelog.
- Release checklist with automated gates.
- Docker image and Compose verification.
- Database migrations and rollback strategy.
- `.mv2` memory backup/restore.
- Health checks and process supervision.
- Logs/traces export.
- Support bundle generation. ✅ CLI/API export now writes a redacted diagnostic archive with readiness, runtime, git, state-summary, and log-tail metadata.

Acceptance criteria:

- A release can be built, tested, installed, upgraded, backed up, and diagnosed by following documented procedures.

### 9. Channels and external ingress

Goal: make Telegram/Discord/webhooks safe production surfaces.

Required work:

- Bot identity verification.
- Telegram webhook setup/status/test helpers with secret-token verification. ✅
- Single-owner Telegram admin gating with natural-language read actions and inline-confirmed writes. ✅
- Platform-specific rate-limit behavior.
- Retry/backoff.
- Threading correctness.
- Attachment handling.
- Per-channel permissions and memory boundaries.
- Command confirmation for dangerous operations.
- Refuse raw secret entry through non-local chat surfaces. ✅
- Secret rotation for webhook/HMAC secrets.

Acceptance criteria:

- Channel-originated actions are attributable, scoped, auditable, and bounded by the same approval model as local UI/CLI actions.

### 10. Metrics that prove the product works

Product metrics:

- time to first successful run
- install success rate
- setup recovery rate
- task completion rate
- user intervention count
- cost per successful task
- latency per task

Agent quality metrics:

- repair success rate
- repeated-failure reduction
- same-action retry reduction
- memory retrieval usefulness
- behavior-delta useful rate
- false-positive promotion rate
- rollback rate
- approval block rate

Killer metric:

> After repeated tasks, does Kestrel measurably perform better than before?

## Prioritized roadmap

### Phase A — Stabilize the local product

Build the golden local developer workflow.

Tasks:

1. Add a product-readiness checklist/report across CLI/API/UI. ✅ CLI/API/UI readiness dashboard landed.
2. Add first-run setup wizard checks for provider, memory, workspace, and permissions. ✅ Non-secret CLI/API setup readiness checks now load into the guided UI setup wizard.
3. Create a demo repo fixture with known failure and expected repair. ✅ `examples/golden_repair_demo` seeds a deterministic failing test plus expected fix patch.
4. Finish branch/worktree isolated repair run as the default for code modification. ✅ Repair/code-modification scheduler tasks now default to git worktree isolation when the workspace supports worktrees.
5. Persist a coherent repair workspace across the full repair DAG instead of creating a separate worker worktree per isolated task. ✅ Repair DAG tasks now reuse one coherent git worktree for the run.
6. Add patch review UI with validation and rollback state.
7. Add support bundle export. ✅ `nest-agent product support-bundle` and `POST /api/product/support-bundle` now generate redacted local diagnostic archives.
8. Add provider certification reporting. ✅ `nest-agent product provider-certification` and `GET /api/product/provider-certification` now expose redacted per-provider status and live-validation commands.

- `examples/golden_repair_demo/` — deterministic fixture repo with one failing test and `expected_fix.patch` for the golden repair journey.

### Phase B — Make learning actually act

Tasks:

1. Auto-activate low-risk validated behavior deltas. ✅ Implemented behind the default-off low-risk auto-activation flag.
2. Expand validation-window analytics and live-provider regression coverage.
3. Add one-click rollback in UI.
4. Add repeated-task improvement report.
5. Add behavior-delta replay suite to release validation.

### Phase C — Product-grade safety

Tasks:

1. Implement user/session/workspace model.
2. Add role-scoped permissions.
3. Add sandboxed skill/plugin execution.
4. Harden plugin/MCP review and enablement.
5. Add production audit-log export.

### Phase D — Hosted/team version

Tasks:

1. Hosted deployment architecture.
2. Organization/workspace support.
3. Team approval flows.
4. Billing/limits if commercialized.
5. Production monitoring and incident playbooks.

## First implementation slice

The first implementation slice is deliberately low-risk:

> Add a product-readiness checklist/report that makes alpha-to-product gaps visible from CLI/API/UI without changing runtime behavior.

Why first:

- It turns this roadmap into an operational dashboard.
- It gives every future implementation pass a visible target.
- It is read-only and safe.
- It avoids pretending the product is done while surfacing real progress.

Initial checklist categories:

- local product stability
- golden repair workflow
- safe autonomous learning
- production auth/workspaces
- sandboxed extensibility
- provider certification
- product UX/onboarding
- operations/release engineering
- channels/ingress
- metrics/proof

The report should classify each category as:

- `ready`
- `partial`
- `missing`

and include evidence, remaining work, and recommended next action.

## Product definition of done

Kestrel becomes a full product when:

1. A new user can install and complete the golden repo workflow.
2. Kestrel can safely modify code in isolated branches with review, validation, approval, and rollback.
3. Learning affects future behavior for low-risk validated cases and remains audited/reversible.
4. High-risk actions remain approval-gated.
5. Users, workspaces, secrets, and memory are isolated in production mode.
6. Skills/plugins/MCP tools have visible permissions and sandboxing.
7. Provider support is release-certified.
8. The UI explains runs, approvals, memory, learning, and rollback clearly.
9. Releases are versioned, tested, installable, upgradeable, and supportable.
10. Metrics prove Kestrel improves over repeated tasks.
