import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RoutingCenter } from "./RoutingCenter";

const statusPayload = {
  schema: "kestrel.adaptive_flock.status.v1",
  runtime: { enabled: false, mode: "off", policy_id: "balanced" },
  routing_schema_version: 1,
  counts: {
    provider_profiles: 1,
    enabled_provider_profiles: 1,
    model_targets: 1,
    enabled_model_targets: 1,
    policies: 1,
    enabled_policies: 1
  }
};

const providerPayload = {
  profile_id: "local",
  display_name: "Local server",
  adapter: "openai-compatible",
  base_url_configured: true,
  secret_configured: true,
  enabled: true,
  locality: "local",
  trust_class: "standard",
  max_concurrency: 2,
  metadata: { max_context_tokens: 131072 },
  revision: 1,
  created_at: "2026-07-23T00:00:00Z",
  updated_at: "2026-07-23T00:00:00Z"
};

const targetPayload = {
  target_id: "local-worker",
  provider_profile_id: "local",
  provider: "openai-compatible",
  model: "qwen-coder",
  enabled: true,
  locality: "local",
  trust_class: "standard",
  capability_tags: ["worker", "coding"],
  role_affinities: ["worker"],
  task_family_affinities: ["bounded_code_change"],
  max_context_tokens: 131072,
  supports_tools: true,
  supports_json: false,
  supports_vision: false,
  supports_reasoning: true,
  supports_streaming: true,
  quality_tier: 3,
  latency_tier: 2,
  operator_priority: 0,
  estimated_cost_usd: 0,
  health: "healthy",
  recent_failure_rate: 0,
  predicted_success: 0.86,
  metadata: {},
  revision: 1,
  created_at: "2026-07-23T00:00:00Z",
  updated_at: "2026-07-23T00:00:00Z"
};

const policyPayload = {
  policy_id: "balanced",
  enabled: true,
  quality_weight: 0.4,
  affinity_weight: 0.16,
  health_weight: 0.1,
  context_weight: 0.08,
  locality_weight: 0.08,
  operator_weight: 0.05,
  cost_weight: 0.08,
  latency_weight: 0.03,
  failure_weight: 0.12,
  require_different_target_for_review: false,
  require_different_model_family_for_review: false,
  prefer_different_provider_for_review: false,
  minimum_quality_by_risk: { low: 1, medium: 2, high: 3, critical: 4 },
  revision: 1,
  created_at: "2026-07-23T00:00:00Z",
  updated_at: "2026-07-23T00:00:00Z"
};

const previewPayload = {
  schema: "kestrel.adaptive_flock.preview.v1",
  task: {
    task_id: "task-1",
    run_id: "run-1",
    title: "Update bounded component",
    status: "pending"
  },
  contract: { task_family: "frontend_implementation", risk: "low" },
  decision: {
    mode: "shadow",
    policy_id: "balanced",
    contract_digest: "abc123",
    selected_target_id: "local-worker",
    selected_provider_profile_id: "local",
    selected_provider: "openai-compatible",
    selected_model: "qwen-coder",
    selection_kind: "deterministic_router",
    score: 0.83,
    reason_codes: ["highest_admissible_score"],
    actionable: false,
    candidates: [
      {
        target_id: "local-worker",
        provider_profile_id: "local",
        provider: "openai-compatible",
        model: "qwen-coder",
        eligible: true,
        score: 0.83,
        reason_codes: ["eligible"],
        components: { quality: 0.3 }
      }
    ]
  }
};

let requests: Array<{ path: string; method: string; body: unknown }>;

beforeEach(() => {
  requests = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      requests.push({ path, method, body });

      if (path === "/api/routing/status") return jsonResponse(statusPayload);
      if (path === "/api/routing/providers" && method === "GET") return jsonResponse([providerPayload]);
      if (path === "/api/routing/providers" && method === "POST") {
        return jsonResponse({ ...providerPayload, profile_id: "cloud", display_name: "Cloud account" });
      }
      if (path === "/api/routing/targets") return jsonResponse([targetPayload]);
      if (path === "/api/routing/policies") return jsonResponse([policyPayload]);
      if (path === "/api/routing/preview") return jsonResponse(previewPayload);
      if (path === "/api/runs/run-1/routing?task_id=task-1") {
        return jsonResponse({ run_id: "run-1", task_id: "task-1", decisions: [], outcomes: [] });
      }
      return new Response(JSON.stringify({ detail: `Unhandled ${method} ${path}` }), { status: 404 });
    })
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("RoutingCenter", () => {
  it("loads routing status, inventory, policies, and active run history", async () => {
    render(<RoutingCenter activeRunId="run-1" activeTaskId="task-1" />);

    expect(await screen.findByText("Local server")).toBeInTheDocument();
    expect(screen.getAllByText("local-worker").length).toBeGreaterThan(0);
    expect(screen.getAllByText("balanced").length).toBeGreaterThan(0);
    expect(screen.getByText("0 decisions")).toBeInTheDocument();
    expect(screen.getByText("Runtime")).toBeInTheDocument();
    expect(screen.getByText("off")).toBeInTheDocument();
  });

  it("previews a task without executing it", async () => {
    render(<RoutingCenter activeTaskId="task-1" />);
    await screen.findByText("Local server");

    fireEvent.click(screen.getByRole("button", { name: "Preview decision" }));

    expect(await screen.findByText("Update bounded component")).toBeInTheDocument();
    expect(screen.getByText("score 0.830")).toBeInTheDocument();
    const request = requests.find((item) => item.path === "/api/routing/preview");
    expect(request?.body).toMatchObject({ task_id: "task-1", local_required: false });
    expect(requests.some((item) => item.path.includes("/api/runs") && item.method === "POST")).toBe(false);
  });

  it("sends but never renders a provider secret reference", async () => {
    render(<RoutingCenter />);
    await screen.findByText("Local server");

    fireEvent.change(screen.getByLabelText("Profile ID"), { target: { value: "cloud" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Cloud account" } });
    fireEvent.change(screen.getByLabelText("Secret reference"), {
      target: { value: "secret://cloud-key" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));

    await waitFor(() => {
      const request = requests.find(
        (item) => item.path === "/api/routing/providers" && item.method === "POST"
      );
      expect(request?.body).toMatchObject({ secret_ref: "secret://cloud-key" });
    });
    expect(screen.queryByText("secret://cloud-key")).not.toBeInTheDocument();
  });
});

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}
