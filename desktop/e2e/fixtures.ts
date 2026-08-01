/**
 * Deterministic Demo-mode fixtures for the installed-renderer e2e harness.
 *
 * These payloads mirror `web/src/testing/apiFixtures.ts` (the jsdom fixture
 * authority) so the rendered workbench sees the same deterministic Demo
 * provider state in the browser that the unit suites see in jsdom. No live
 * provider is ever contacted: every `/api/*` request is fulfilled from this
 * module, and any request outside this module fails the test loudly.
 *
 * Theme and motion are driven through the SUPPORTED preferences
 * (`web/src/design/theme.ts` localStorage keys), never by injecting CSS
 * into the production page.
 */

export const THEME_STORAGE_KEY = "kestrel.theme.preference.v1";
export const MOTION_STORAGE_KEY = "kestrel.motion.preference.v1";

const fixtureTime = "2026-07-31T00:00:00Z";

export const demoProject = Object.freeze({
  project_id: "project_fixture",
  display_name: "Fixture project",
  repository_path: "/fixture/project",
  remote: null,
  default_branch: "main",
  allowed_paths: ["."],
  provider_policy: {},
  cost_budget: 1,
  privacy_class: "local_required",
  test_recipes: [{ name: "tests", command: "pytest -q" }],
  build_recipes: [],
  capability_ceiling: ["file.read"],
  baseline_index_digest: null,
  archived_at: null,
  revision: 1,
  created_at: fixtureTime,
  updated_at: fixtureTime,
});

export const demoRun = Object.freeze({
  run_id: "run_e2e_demo",
  project_id: demoProject.project_id,
  status: "queued",
  message: "Summarize this project",
  session_id: "session_e2e_demo",
  workspace: demoProject.repository_path,
  provider: "mock",
  model: "mock",
  assistant_message: "",
  tool_count: 0,
  context_chars: 0,
  stop_reason: "",
  created_at: fixtureTime,
  updated_at: fixtureTime,
});

/**
 * Active variant of the demo run used by approval-pending mode: the Mission
 * destination only renders the EngineeringRunPanel (which surfaces the exact
 * resource digest) for a run that is actively executing, so this status
 * drives MissionControl into the active-mission view.
 */
export const demoActiveRun = Object.freeze({
  ...demoRun,
  run_id: "run_e2e_active",
  status: "running",
});

/**
 * Engineering approval packet carrying the exact-call resource digest the
 * approval-queue e2e asserts on. The digest value matches `demoApproval` so
 * both the Mission approval surface and the Engineering panel agree.
 */
export const demoApprovalPacket = Object.freeze({
  packet_id: "packet_e2e_demo",
  objective: "Summarize this project",
  checkpoint: "pre_tool",
  packet_digest: "f".repeat(64),
  status: "pending",
  authorization_record_count: 1,
  calls: [
    {
      tool_call_id: "tool_call_e2e_demo",
      tool_name: "file.write",
      arguments: { path: "docs/notes.md", content: "demo" },
      call_digest: "0".repeat(64),
      risk: "high",
      capability_revision: 3,
      resource_digest:
        "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      reason: "Write within approved paths",
      resource_scope: "docs/notes.md",
      expected_side_effect: "Creates docs/notes.md",
      rollback: "git checkout -- docs/notes.md",
      status: "pending",
    },
  ],
});

export const demoTaskGraph = Object.freeze({
  tasks: [],
  ready_tasks: [],
  approval_blocked_tasks: [],
  subagents: [],
});

export const demoApproval = Object.freeze({
  approval_id: "approval_e2e_demo",
  run_id: demoActiveRun.run_id,
  tool_call_id: "tool_call_e2e_demo",
  tool_name: "file.write",
  arguments: { path: "docs/notes.md", content: "demo" },
  risk: "high",
  principal: "owner",
  expires_at: "2099-01-01T00:00:00Z",
  capability_revision: 3,
  resource_digest:
    "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  status: "pending",
  decision: null,
  result: null,
  created_at: fixtureTime,
  updated_at: fixtureTime,
});

/**
 * Successful preflight projection for the fixture project. Mirrors the
 * `kestrel.mission_preflight.v1` payloads the jsdom suites use
 * (`web/src/mission/MissionControl.test.tsx`) so Review mission resolves
 * with launch authority exactly like the unit-tested Demo path.
 */
export const demoPreflight = Object.freeze({
  schema: "kestrel.mission_preflight.v1",
  project_id: demoProject.project_id,
  project_revision: demoProject.revision,
  project_name: demoProject.display_name,
  repository_path: demoProject.repository_path,
  objective: "Summarize this project",
  template_id: "explain_repository",
  branch: "main",
  working_tree: { state: "clean", summary: "No local changes" },
  route_policy: "Balanced",
  budget: { currency: "USD", limit: 1, estimate: 0 },
  effective_capabilities: ["Read repo"],
  likely_approvals: [],
  validation_recipes: ["pytest -q"],
  rollback: "Worktree + signed review",
  index: {
    freshness: "current",
    digest: null,
    indexed_at: fixtureTime,
    detail: "Fixture index",
  },
  provider: { status: "pass", detail: "Deterministic Demo ready" },
  launch_binding: {
    schema: "kestrel.mission_launch_binding.v1",
    project_id: demoProject.project_id,
    project_revision: demoProject.revision,
    objective_digest: "a".repeat(64),
    template_id: "explain_repository",
    config_digest: "b".repeat(64),
    routing_enabled: false,
    routing_mode: "off",
    policy_id: "balanced",
    policy_revision: 1,
    inventory_digest: "c".repeat(64),
    preflight_digest: "e".repeat(64),
    plan_digest: "f".repeat(64),
    binding_digest: "d".repeat(64),
  },
  checks: [
    { check_id: "route", title: "Route", status: "pass", detail: "Balanced" },
    { check_id: "budget", title: "Budget", status: "pass", detail: "Cap ok" },
    {
      check_id: "capabilities",
      title: "Permissions",
      status: "pass",
      detail: "Narrowed",
    },
    {
      check_id: "validation",
      title: "Validation",
      status: "pass",
      detail: "pytest -q",
    },
    {
      check_id: "rollback",
      title: "Rollback",
      status: "pass",
      detail: "Available",
    },
  ],
  tasks: [
    {
      task_id: "summarize",
      title: "Summarize the project",
      rationale: "Read the repository and produce a grounded summary.",
      dependencies: [],
      acceptance_criteria: ["Summary delivered"],
      required_tools: ["repo.context_pack"],
      risk: "low",
    },
  ],
  warnings: [],
  blockers: [],
  can_start: true,
  generated_at: fixtureTime,
});

const setupReadiness = Object.freeze({
  schema: "kestrel.setup_readiness.v1",
  ready: true,
  experience_mode: "demo",
  pass_count: 2,
  warn_count: 0,
  fail_count: 0,
  next_action:
    "Demo is ready. Connect a live model when provider-backed responses are needed.",
  checks: [
    {
      check_id: "provider_configuration",
      title: "Provider configuration",
      status: "pass",
      detail: "Deterministic Demo is selected.",
      recovery: "Choose a live provider later.",
    },
    {
      check_id: "provider_operational",
      title: "Provider operational health",
      status: "pass",
      detail: "Offline Demo responses are available.",
      recovery: "Connect and validate a live provider later.",
    },
  ],
});

const runtimeConfig = Object.freeze({
  name: "Kestrel",
  version: "0.5.0",
  schema_version: 19,
  provider: {
    name: "mock",
    model: "mock",
    api_key_env: null,
    api_key_configured: false,
  },
  feature_flags: {
    enable_autonomous_scheduler: false,
    require_approval_for_high_risk_tools: true,
    allow_shell: false,
    allow_file_write: false,
    allow_policy_writes: false,
  },
  limits: { max_tool_rounds: 6 },
  paths: {
    workspace: "/fixture/project",
    memory_dir: "/fixture/memory",
  },
  settings: {
    runtime: {
      provider: "mock",
      model: "mock",
      backend: "memory",
      memory_dir: "/fixture/memory",
      workspace: "/fixture/project",
      stream: false,
      require_api_auth: false,
      autonomy_mode: "background",
      revision: "fixture-runtime-revision-1",
      persisted: false,
    },
  },
  validation_commands: ["pytest -q"],
});

const runtimeSettings = Object.freeze({
  provider: "mock",
  model: "mock",
  backend: "memory",
  memory_dir: "/fixture/memory",
  workspace: "/fixture/project",
  stream: false,
  autonomy_mode: "background",
  revision: "fixture-runtime-revision-1",
  persisted: false,
});

const selfDescription = Object.freeze({
  identity: {
    name: "Kestrel",
    display_name: "Kestrel",
    description: "Fixture local-first agent.",
  },
  provider: {
    provider: "mock",
    model: "mock",
    api_key_env: null,
    api_key_configured: false,
  },
  config: { allow_self_modification: false, allow_web: false },
  memory_layers: [
    "working",
    "episodic",
    "semantic",
    "procedural",
    "self",
    "policy",
  ].map((layer) => ({ layer, mv2_file: `${layer}.mv2` })),
  tools: [],
  skills: [],
  plugins: [],
  mcp_servers: [],
});

const onboarding = Object.freeze({
  completed: true,
  profile: {
    schema_version: "kestrel_onboarding_profile.v1",
    setup_complete: true,
    agent_name: "Kestrel",
    user_name: "Fixture owner",
    preferred_name: "Owner",
    persona: "steady",
    persona_name: "Steady Companion",
    persona_summary: "Warm, grounded, and concise.",
    persona_guidance: "Be warm and direct.",
    working_style: "Evidence first.",
    goals: ["Keep Kestrel local and dependable."],
    interests: ["Local-first software"],
    communication_notes: "Prefer concise status.",
    continuous_learning: true,
    updated_at: fixtureTime,
  },
  personas: [],
  reflection: "Fixture onboarding is complete.",
});

const memoryLayers = Object.freeze(
  ["working", "episodic", "semantic", "procedural", "self", "policy"].map(
    (layer) => ({
      layer,
      path: `/fixture/memory/${layer}.mv2`,
      exists: true,
      ok: true,
      backend: "InMemoryBackend",
    }),
  ),
);

const emptySettingsProjection = Object.freeze({
  schema: "kestrel.effective_settings.v1",
  revision: "fixture-settings-revision-1",
  categories: [],
  items: [],
  items_by_id: {},
  counts: { total: 0, blocked: 0, restart_required: 0 },
});

/**
 * Server-authoritative index freshness for the fixture project (Task 9's
 * `kestrel.project_index_status.v1`). Mission preflight blocks until this
 * resolves — freshness is never inferred client-side.
 */
const projectIndexStatus = Object.freeze({
  schema: "kestrel.project_index_status.v1",
  project_id: demoProject.project_id,
  freshness: "current",
  digest: null,
  indexed_at: fixtureTime,
  detail: "Fixture index is current.",
});

const routingStatus = Object.freeze({
  schema: "kestrel.adaptive_flock.status.v1",
  runtime: { enabled: false, mode: "off", policy_id: "balanced" },
  routing_schema_version: 2,
  counts: {
    provider_profiles: 0,
    enabled_provider_profiles: 0,
    model_targets: 0,
    enabled_model_targets: 0,
    policies: 0,
    enabled_policies: 0,
    calibrations: 0,
  },
});

/** Payloads that always answer the same way regardless of mode. */
function baseGetPayloads(): Record<string, unknown> {
  return {
    "/api/health": { status: "ok" },
    "/api/self": selfDescription,
    "/api/self/onboarding": onboarding,
    "/api/product/setup": setupReadiness,
    "/api/runtime/config": runtimeConfig,
    "/api/runtime/settings": runtimeSettings,
    "/api/runtime/models": {
      provider: "mock",
      models: ["mock"],
      fallback_models: ["mock"],
      source: "static",
      ok: true,
      fetchable: false,
      error: null,
      base_url_configured: false,
      api_key_env: null,
      api_key_configured: false,
      fetched_at: fixtureTime,
    },
    "/api/projects": { items: [demoProject], count: 1 },
    [`/api/projects/${demoProject.project_id}/index`]: projectIndexStatus,
    "/api/sessions": [],
    "/api/secrets": [],
    "/api/approvals": [],
    "/api/tools": [],
    "/api/skills": [],
    "/api/plugins": [],
    "/api/mcp/servers": [],
    "/api/channels": [],
    "/api/settings": emptySettingsProjection,
    "/api/capabilities": { items: [], counts: { total: 0, enabled: 0 } },
    "/api/routing/status": routingStatus,
    "/api/memory/layers": memoryLayers,
    "/api/logs?limit=120": [],
    "/api/cognition/lessons?k=20": { items: [] },
    "/api/cognition/failures?k=20": { items: [] },
    "/api/learning/dashboard?since=all": {
      since: null,
      headline: {
        auto_activations: 0,
        rollbacks: 0,
        false_positive_rate: 0,
        activations_then_rolled_back: 0,
        average_time_to_rollback_hours: null,
      },
      layers: [],
    },
    "/api/memory/deltas?since=all": {
      summary: {
        total_deltas: 0,
        active_deltas: 0,
        activated_deltas: 0,
        never_activated: 0,
        useful_rate: 0,
        failure_rate: 0,
        rollback_rate: 0,
        never_activated_rate: 0,
        outcomes: {},
      },
      deltas: [],
      recommendations: [],
    },
    "/api/routines": [],
    "/api/routines/deliveries": [],
    "/api/routing/policies": [],
    "/api/routing/providers": [],
    "/api/routing/targets": [],
  };
}

export type DemoMode = "idle" | "mission-started" | "approval-pending";

/**
 * Engineering feeds every active run loads (EngineeringRunPanel always
 * requests these five). Provided for BOTH runs in every mode so a run that
 * becomes active — whether launched by the keyboard journey or pre-seeded by
 * approval-pending mode — never trips the loud missing-fixture error.
 */
function engineeringFeeds(): Record<string, unknown> {
  const feeds: Record<string, unknown> = {};
  for (const run of [demoRun, demoActiveRun]) {
    feeds[`/api/runs/${run.run_id}/approval-packets`] = { items: [] };
    feeds[`/api/runs/${run.run_id}/graph/amendments`] = { items: [] };
    feeds[`/api/runs/${run.run_id}/candidate-fanouts`] = { items: [] };
    feeds[`/api/runs/${run.run_id}/browser-validations`] = { items: [] };
    feeds[`/api/runs/${run.run_id}/github-change-requests`] = { items: [] };
  }
  // The seeded active run carries the pending approval packet with the exact
  // resource digest the approval-queue e2e asserts on.
  feeds[`/api/runs/${demoActiveRun.run_id}/approval-packets`] = {
    items: [demoApprovalPacket],
  };
  return feeds;
}

/** Mode-dependent payloads for the run/approval surfaces. */
function modePayloads(mode: DemoMode): Record<string, unknown> {
  const feeds = engineeringFeeds();
  // RunTrace shape (web/src/types.ts): { run, summary, timeline, traces }.
  // LegacyWorkbench reads runTrace.run.run_id — a bare {run_id, items} payload
  // crashes the active-mission view.
  const traceFor = (run: typeof demoRun) => ({
    run,
    summary: {
      event_count: 0,
      span_count: 0,
      first_event_at: null,
      last_event_at: null,
      trace_counts: {},
      span_counts: {},
    },
    timeline: [],
    spans: [],
    traces: {},
  });
  switch (mode) {
    case "mission-started":
      return {
        ...feeds,
        "/api/runs": [demoRun],
        [`/api/runs/${demoRun.run_id}/task-graph`]: demoTaskGraph,
        [`/api/runs/${demoRun.run_id}/trace?limit=700`]: traceFor(demoRun),
      };
    case "approval-pending":
      return {
        ...feeds,
        "/api/runs": [demoActiveRun],
        "/api/approvals": [demoApproval],
        [`/api/runs/${demoActiveRun.run_id}/task-graph`]: demoTaskGraph,
        [`/api/runs/${demoActiveRun.run_id}/trace?limit=700`]:
          traceFor(demoActiveRun),
      };
    default:
      return { ...feeds, "/api/runs": [] };
  }
}

function pathOf(url: string): string {
  const parsed = new URL(url);
  return `${parsed.pathname}${parsed.search}`;
}

function json(payload: unknown): string {
  return JSON.stringify(payload);
}

export type InstallDemoFixturesOptions = Readonly<{
  theme?: "light" | "dark";
  mode?: DemoMode;
}>;

export type DemoFixtureController = Readonly<{
  /** Paths of every request the page made, in order. */
  requestedPaths: string[];
}>;

/**
 * Install deterministic Demo fixtures into a Playwright page:
 *
 *  - localStorage theme + reduced-motion preferences via the supported
 *    storage keys (applied by `installTheme` on boot — no CSS injection);
 *  - a `window.fetch` stub that answers every `/api/*` request from the
 *    fixture tables above and throws on anything unknown, so a regression
 *    that tried to reach a live provider fails the suite instead of
 *    silently passing.
 *  - the exact Demo mutation surface the keyboard journey commits
 *    (mission preflight + mission launch) with the preflight echoing the
 *    reviewed objective so launch binding validates. Any mutation outside
 *    this allowlist throws loudly — there is no live provider to fall
 *    through to.
 *  - an `EventSource` stub that never emits (the demo has no live stream).
 *
 * Must be registered with `page.addInitScript` BEFORE navigation.
 */
export function demoFixtureInitScript(
  options: InstallDemoFixturesOptions = {},
): string {
  const theme = options.theme ?? "light";
  const mode: DemoMode = options.mode ?? "idle";
  const payloads: Record<string, unknown> = {
    ...baseGetPayloads(),
    ...modePayloads(mode),
  };
  // `?status=pending` is how the app polls the pending-approval queue.
  payloads["/api/approvals?status=pending"] = payloads["/api/approvals"];
  return `
    (() => {
      const payloads = ${json(payloads)};
      const preflight = ${json(demoPreflight)};
      const demoRun = ${json(demoRun)};
      const theme = ${json(theme)};
      try {
        window.localStorage.setItem(${json(THEME_STORAGE_KEY)}, theme);
        window.localStorage.setItem(${json(MOTION_STORAGE_KEY)}, "reduce");
      } catch { /* storage may be unavailable in exotic contexts */ }
      window.__kestrelE2eRequestedPaths = [];
      const record = (path) => {
        window.__kestrelE2eRequestedPaths.push(path);
      };
      const jsonResponse = (payload, status = 200) =>
        new Response(JSON.stringify(payload), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      window.fetch = async (input, init) => {
        const url = typeof input === "string" ? input : String(input.url ?? input);
        const parsed = new URL(url, window.location.href);
        const path = parsed.pathname + parsed.search;
        const method = init && init.method ? String(init.method).toUpperCase() : "GET";
        record(method + " " + path);
        if (!parsed.pathname.startsWith("/api/")) {
          throw new Error("e2e_fixture_refused_non_api_request:" + path);
        }
        if (method !== "GET") {
          // Deterministic Demo mutation surface (mission journey only).
          if (
            method === "POST" &&
            parsed.pathname === "/api/projects/${demoProject.project_id}/mission/preflight"
          ) {
            let objective = preflight.objective;
            let templateId = preflight.template_id;
            try {
              const body = JSON.parse(String((init && init.body) || "{}"));
              if (typeof body.objective === "string" && body.objective.trim()) {
                objective = body.objective;
              }
              if (typeof body.template_id === "string" && body.template_id) {
                templateId = body.template_id;
              }
            } catch { /* keep defaults */ }
            return jsonResponse({
              ...preflight,
              objective,
              template_id: templateId,
              launch_binding: { ...preflight.launch_binding, template_id: templateId },
            });
          }
          if (method === "POST" && parsed.pathname === "/api/runs") {
            let message = demoRun.message;
            let sessionId = demoRun.session_id;
            try {
              const body = JSON.parse(String((init && init.body) || "{}"));
              if (typeof body.message === "string" && body.message.trim()) {
                message = body.message;
              }
              if (typeof body.session_id === "string" && body.session_id) {
                sessionId = body.session_id;
              }
            } catch { /* keep defaults */ }
            return jsonResponse({
              ...demoRun,
              message,
              session_id: sessionId,
              status: "queued",
            }, 201);
          }
          throw new Error("e2e_fixture_refused_mutation:" + method + " " + path);
        }
        if (Object.prototype.hasOwnProperty.call(payloads, path)) {
          return jsonResponse(payloads[path]);
        }
        throw new Error("e2e_fixture_missing:" + path);
      };
      class StubEventSource {
        constructor(url) { this.url = url; record("EVENTSOURCE " + String(url)); }
        addEventListener() {}
        removeEventListener() {}
        close() {}
      }
      window.EventSource = StubEventSource;
    })();
  `;
}
