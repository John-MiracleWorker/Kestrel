export type FixtureRequest = Readonly<{
  path: string;
  method: string;
  body: unknown;
}>;

export type LegacyMutationContract = Readonly<{
  method: "DELETE" | "GET" | "POST" | "PUT";
  path: string;
  requiredBodyFields: readonly string[];
}>;

const fixtureTime = "2026-07-31T00:00:00Z";

const rawApiFixtures = {
  "/api/approvals": [],
  "/api/capabilities": {
    items: [],
    counts: {
      total: 0,
      configured_enabled: 0,
      effective_enabled: 0,
      blocked: 0,
    },
  },
  "/api/channels": [],
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
  "/api/memory/layers": [
    "working",
    "episodic",
    "semantic",
    "procedural",
    "self",
    "policy",
  ].map((layer) => ({
    layer,
    path: `/fixture/memory/${layer}.mv2`,
    exists: true,
    ok: true,
    backend: "InMemoryBackend",
  })),
  "/api/mcp/servers": [],
  "/api/plugins": [],
  "/api/product/setup": {
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
  },
  "/api/projects": {
    items: [
      {
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
      },
    ],
    count: 1,
  },
  "/api/routing/status": {
    schema: "kestrel.adaptive_flock.status.v1",
    runtime: {
      enabled: false,
      mode: "off",
      policy_id: "balanced",
    },
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
  },
  "/api/runs": [],
  "/api/runtime/config": {
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
  },
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
  "/api/runtime/settings": {
    provider: "mock",
    model: "mock",
    backend: "memory",
    memory_dir: "/fixture/memory",
    workspace: "/fixture/project",
    stream: false,
    autonomy_mode: "background",
    revision: "fixture-runtime-revision-1",
    persisted: false,
  },
  "/api/secrets": [],
  "/api/sessions": [],
  "/api/skills": [],
  "/api/tools": [],
} as const;

export const apiFixtures = deepFreeze(rawApiFixtures);

const startupSupportFixtures = deepFreeze({
  "/api/health": { status: "ok" },
  "/api/self": {
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
  },
  "/api/self/onboarding": {
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
  },
  "/api/logs?limit=120": [],
  "/api/cognition/lessons?k=20": { items: [] },
  "/api/cognition/failures?k=20": { items: [] },
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
});

export const currentAppStartupRequests = Object.freeze([
  "/api/health",
  "/api/runs",
  "/api/sessions",
  "/api/tools",
  "/api/capabilities",
  "/api/approvals?status=pending",
  "/api/approvals",
  "/api/mcp/servers",
  "/api/skills",
  "/api/plugins",
  "/api/channels",
  "/api/secrets",
  "/api/memory/layers",
  "/api/runtime/config",
  "/api/self",
  "/api/self/onboarding",
  "/api/product/setup",
  "/api/logs?limit=120",
  "/api/cognition/lessons?k=20",
  "/api/cognition/failures?k=20",
  "/api/memory/deltas?since=all",
  "/api/learning/dashboard?since=all",
  "/api/runtime/models?provider=mock",
] as const);

export const legacyMutationContracts = deepFreeze({
  approvalDecision: {
    method: "POST",
    path: "/api/approvals/:approvalId/decision",
    requiredBodyFields: ["approved", "arguments"],
  },
  browserTokenPrompt: {
    method: "GET",
    path: "/api/health",
    requiredBodyFields: [],
  },
  capabilityToggle: {
    method: "PUT",
    path: "/api/capabilities/:kind/:id",
    requiredBodyFields: ["enabled", "expected_revision"],
  },
  extensionReview: {
    method: "POST",
    path: "/api/plugins/review",
    requiredBodyFields: ["source", "ref"],
  },
  firstRunSetup: {
    method: "POST",
    path: "/api/self/onboarding",
    requiredBodyFields: [
      "agent_name",
      "user_name",
      "preferred_name",
      "persona",
      "working_style",
      "goals",
      "interests",
      "communication_notes",
      "continuous_learning",
    ],
  },
  memorySearch: {
    method: "GET",
    path: "/api/memory/search?query=:query&k=12",
    requiredBodyFields: [],
  },
  missionLaunch: {
    method: "POST",
    path: "/api/runs",
    requiredBodyFields: [
      "message",
      "session_id",
      "autonomy_mode",
      "project_id",
      "mission_plan",
      "project_revision",
      "mission_template_id",
      "mission_binding",
    ],
  },
  missionPreflight: {
    method: "POST",
    path: "/api/projects/:projectId/mission/preflight",
    requiredBodyFields: ["objective", "template_id"],
  },
  providerSave: {
    method: "POST",
    path: "/api/routing/providers",
    requiredBodyFields: [
      "profile_id",
      "display_name",
      "adapter",
      "enabled",
      "locality",
      "trust_class",
      "max_concurrency",
      "expected_revision",
    ],
  },
  routineRun: {
    method: "POST",
    path: "/api/routines/:routineId/actions/run-now",
    requiredBodyFields: ["expected_revision", "idempotency_key"],
  },
  settingsSave: {
    method: "PUT",
    path: "/api/runtime/settings",
    requiredBodyFields: ["expected_revision"],
  },
  targetSave: {
    method: "POST",
    path: "/api/routing/targets",
    requiredBodyFields: [
      "target_id",
      "provider_profile_id",
      "provider",
      "model",
      "enabled",
      "locality",
      "trust_class",
      "expected_revision",
    ],
  },
} satisfies Record<string, LegacyMutationContract>);

export function requestMatchesLegacyContract(
  name: keyof typeof legacyMutationContracts,
  request: FixtureRequest,
): boolean {
  const contract = legacyMutationContracts[name];
  if (
    request.method.toUpperCase() !== contract.method ||
    !pathMatches(contract.path, request.path)
  ) {
    return false;
  }
  if (contract.requiredBodyFields.length === 0) {
    return true;
  }
  if (
    typeof request.body !== "object" ||
    request.body === null ||
    Array.isArray(request.body)
  ) {
    return false;
  }
  const body = request.body as Record<string, unknown>;
  return contract.requiredBodyFields.every((field) =>
    Object.hasOwn(body, field),
  );
}

export function createFixtureFetch(
  onRequest?: (request: FixtureRequest) => void,
): typeof fetch {
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = requestPath(input);
    const method = String(init?.method ?? "GET").toUpperCase();
    const body =
      typeof init?.body === "string"
        ? JSON.parse(init.body)
        : null;
    onRequest?.({ path, method, body });
    const payload = fixturePayload(path);
    if (method !== "GET" || payload === missingFixture) {
      throw new Error(`unhandled_fixture_request:${method}:${path}`);
    }
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
}

const missingFixture = Symbol("missing_fixture");

function fixturePayload(path: string): unknown | typeof missingFixture {
  if (Object.hasOwn(apiFixtures, path)) {
    return apiFixtures[path as keyof typeof apiFixtures];
  }
  if (Object.hasOwn(startupSupportFixtures, path)) {
    return startupSupportFixtures[
      path as keyof typeof startupSupportFixtures
    ];
  }
  if (path === "/api/approvals?status=pending") {
    return apiFixtures["/api/approvals"];
  }
  if (path.startsWith("/api/runtime/models?provider=")) {
    return apiFixtures["/api/runtime/models"];
  }
  return missingFixture;
}

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return relativeRequestPath(input);
  }
  if (input instanceof URL) {
    return `${input.pathname}${input.search}`;
  }
  return relativeRequestPath(input.url);
}

function relativeRequestPath(value: string): string {
  if (value.startsWith("/")) {
    return value;
  }
  const parsed = new URL(value);
  return `${parsed.pathname}${parsed.search}`;
}

function pathMatches(template: string, actual: string): boolean {
  const pattern = template
    .split(/(:[A-Za-z][A-Za-z0-9_]*)/)
    .map((part) =>
      part.startsWith(":")
        ? "[^/?&]+"
        : part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
    )
    .join("");
  return new RegExp(`^${pattern}$`).test(actual);
}

function deepFreeze<T>(value: T): Readonly<T> {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) {
    return value;
  }
  for (const child of Object.values(value)) {
    deepFreeze(child);
  }
  return Object.freeze(value);
}
