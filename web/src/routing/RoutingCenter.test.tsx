import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestMatchesLegacyContract } from "../testing/apiFixtures";
import { RoutingCenter } from "./RoutingCenter";

const statusPayload = {
  schema: "kestrel.adaptive_flock.status.v1",
  runtime: {
    enabled: false,
    mode: "off",
    policy_id: "balanced",
    learned: {
      min_examples: 5,
      min_target_examples: 3,
      confidence_threshold: 0.7,
      activation_margin: 0.08,
      cost_coverage_threshold: 0.8,
      decay_half_life_days: 30,
      activation_replay_verified: false
    }
  },
  routing_schema_version: 2,
  counts: {
    provider_profiles: 1,
    enabled_provider_profiles: 1,
    model_targets: 1,
    enabled_model_targets: 1,
    policies: 1,
    enabled_policies: 1,
    calibrations: 1
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
  input_cost_per_million_usd: 0,
  output_cost_per_million_usd: 0,
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

const shadowObservationsPayload = [
  {
    observation_id: "shadow_obs_deterministic",
    run_id: "run-1",
    task_id: "task-1",
    subagent_id: null,
    attempt: 1,
    role: "executor",
    actual_authority: "deterministic_static",
    actual_target_id: "local-worker",
    actual_provider: "openai-compatible",
    actual_model: "qwen-coder",
    shadow_target_id: null,
    shadow_provider: "",
    shadow_model: "",
    shadow_executed: false,
    static_target_id: "local-worker",
    candidates: [],
    constraints: {},
    qualification: {},
    reason_codes: ["highest_admissible_score"],
    usage: {},
    verdict: "inconclusive",
    verdict_reason: "no_shadow_recommendation",
    evidence_basis: ["shadow_abstained"],
    counterfactual_proven: false,
    payload_digest: "d" .repeat(64),
    created_at: "2026-08-22T00:00:00Z",
    resolved_at: null,
    validation_passed: null,
    actual_cost_usd: null,
    actual_latency_seconds: null
  },
  {
    observation_id: "shadow_obs_differing",
    run_id: "run-1",
    task_id: "task-1",
    subagent_id: null,
    attempt: 2,
    role: "executor",
    actual_authority: "deterministic_static",
    actual_target_id: "frontier-review",
    actual_provider: "openai-compatible",
    actual_model: "review-model",
    shadow_target_id: "local-worker",
    shadow_provider: "openai-compatible",
    shadow_model: "qwen-coder",
    shadow_executed: false,
    static_target_id: "frontier-review",
    candidates: [],
    constraints: {},
    qualification: { utility_delta: 0.2, confidence: 0.9 },
    reason_codes: ["prior_evidence_favorable"],
    usage: {},
    verdict: "supported",
    verdict_reason: "shadow_favored_by_prior_evidence",
    evidence_basis: ["prior_evidence_favorable", "target_unexecuted"],
    counterfactual_proven: false,
    payload_digest: "e" .repeat(64),
    created_at: "2026-08-22T00:00:01Z",
    resolved_at: "2026-08-22T00:00:05Z",
    validation_passed: true,
    actual_cost_usd: 0.01,
    actual_latency_seconds: 4
  },
  {
    observation_id: "shadow_obs_activated",
    run_id: "run-1",
    task_id: "task-1",
    subagent_id: null,
    attempt: 3,
    role: "executor",
    actual_authority: "adaptive_activated",
    actual_target_id: "local-worker",
    actual_provider: "openai-compatible",
    actual_model: "qwen-coder",
    shadow_target_id: "local-worker",
    shadow_provider: "openai-compatible",
    shadow_model: "qwen-coder",
    shadow_executed: true,
    static_target_id: "frontier-review",
    candidates: [],
    constraints: {},
    qualification: { utility_delta: 0.2, confidence: 0.9 },
    reason_codes: ["learned_constrained"],
    usage: {},
    verdict: "supported",
    verdict_reason: "shadow_executed_and_passed",
    evidence_basis: ["shadow_executed", "terminal_validation_passed"],
    counterfactual_proven: true,
    payload_digest: "f" .repeat(64),
    created_at: "2026-08-22T00:00:02Z",
    resolved_at: "2026-08-22T00:00:06Z",
    validation_passed: true,
    actual_cost_usd: 0.01,
    actual_latency_seconds: 4
  },
  {
    observation_id: "shadow_obs_suspended",
    run_id: "run-1",
    task_id: "task-1",
    subagent_id: null,
    attempt: 4,
    role: "executor",
    actual_authority: "deterministic_fallback_after_suspension",
    actual_target_id: "frontier-review",
    actual_provider: "openai-compatible",
    actual_model: "review-model",
    shadow_target_id: "local-worker",
    shadow_provider: "openai-compatible",
    shadow_model: "qwen-coder",
    shadow_executed: false,
    static_target_id: "frontier-review",
    candidates: [],
    constraints: {},
    qualification: {},
    reason_codes: ["suspended_fallback"],
    usage: {},
    verdict: "inconclusive",
    verdict_reason: "no_terminal_evidence",
    evidence_basis: [],
    counterfactual_proven: false,
    payload_digest: "a" .repeat(64),
    created_at: "2026-08-22T00:00:03Z",
    resolved_at: null,
    validation_passed: null,
    actual_cost_usd: null,
    actual_latency_seconds: null
  }
];

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
      if (path === "/api/routing/targets" && method === "POST") {
        return jsonResponse({
          ...targetPayload,
          target_id: "review-worker",
        });
      }
      if (path === "/api/routing/targets") return jsonResponse([targetPayload]);
      if (path === "/api/routing/policies") return jsonResponse([policyPayload]);
      if (path === "/api/routing/preview") return jsonResponse(previewPayload);
      if (path === "/api/runs/run-1/routing?task_id=task-1") {
        return jsonResponse({
          run_id: "run-1",
          task_id: "task-1",
          decisions: [
            {
              decision_id: "decision-1",
              task_id: "task-1",
              attempt: 1,
              status: "completed",
              mode: "constrained",
              selected_target_id: "local-worker",
              selected_provider: "openai-compatible",
              selected_model: "qwen-coder",
              selection_kind: "deterministic_router",
              actionable: true,
              task_family: "bounded_code_change",
              risk: "low"
            }
          ],
          outcomes: [
            {
              decision_id: "decision-1",
              validation_passed: true,
              execution_status: "completed",
              failure_category: null,
              latency_seconds: 4,
              input_tokens: 100,
              output_tokens: 20,
              actual_cost_usd: 0,
              retry_count: 0,
              escalated: false,
              outcome_labels: ["validated_success", "cost_attributed"]
            }
          ],
          shadows: [
            {
              shadow_id: "shadow-1",
              decision_id: "decision-1",
              project_id: "project-1",
              task_family: "bounded_code_change",
              risk: "low",
              static_target_id: "frontier-review",
              learned_target_id: "local-worker",
              actual_target_id: "local-worker",
              actual_provider: "openai-compatible",
              actual_model: "qwen-coder",
              evidence_count: 12,
              target_example_count: 9,
              cost_coverage: 1,
              confidence: 0.75,
              utility_delta: 0.2,
              estimated_savings_usd: 0.03,
              route_regret_usd: 0,
              activated: true,
              abstention_reason: null,
              resolved_at: "2026-07-27T00:00:00Z",
              actual_validation_passed: true,
              actual_cost_usd: 0
            }
          ],
          calibrations: [
            {
              calibration_key: "cal-1",
              project_id: "project-1",
              target_id: "local-worker",
              task_family: "bounded_code_change",
              risk: "low",
              validation_rate: 0.9,
              recent_failure_rate: 0.1,
              provider_outage_rate: 0,
              average_cost_usd: 0,
              average_latency_seconds: 4,
              cost_coverage: 1,
              example_count: 12,
              effective_sample_size: 11.5,
              updated_at: "2026-07-27T00:00:00Z"
            }
          ],
          shadow_observations: shadowObservationsPayload
        });
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

    expect((await screen.findAllByText("Local server")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("local-worker").length).toBeGreaterThan(0);
    expect(screen.getAllByText("balanced").length).toBeGreaterThan(0);
    expect(screen.getByText("1 decisions")).toBeInTheDocument();
    expect(screen.getByText("Runtime")).toBeInTheDocument();
    expect(screen.getByText("off")).toBeInTheDocument();
    expect(screen.getByText("frontier-review → local-worker")).toBeInTheDocument();
    expect(screen.getByText("The evidence-gated learned route executed.", { exact: false })).toBeInTheDocument();
  });

  it("distinguishes deterministic, shadow, activated, and suspended-fallback states", async () => {
    render(<RoutingCenter activeRunId="run-1" activeTaskId="task-1" />);
    await screen.findAllByText("Local server");

    expect(screen.getByText(/Zero-authority shadow observations/i)).toBeInTheDocument();
    // The four routing states are distinguishable in the evidence panel.
    expect(screen.getByText("deterministic")).toBeInTheDocument();
    expect(screen.getByText("shadow")).toBeInTheDocument();
    expect(screen.getByText("activated")).toBeInTheDocument();
    expect(screen.getByText("suspended-fallback")).toBeInTheDocument();
    // Honest counterfactual framing: a differing unexecuted target is never
    // presented as proven.
    expect(
      screen.getAllByText(/Not proven: the differing target was never executed/).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText(/Terminal evidence not yet recorded/).length).toBeGreaterThan(0);
    // Evidence links back to the durable run/task.
    expect(screen.getAllByText("run run-1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("task task-1").length).toBeGreaterThan(0);
  });

  it("previews a task without executing it", async () => {
    render(<RoutingCenter activeTaskId="task-1" />);
    await screen.findAllByText("Local server");

    fireEvent.click(screen.getByRole("button", { name: "Preview decision" }));

    expect(await screen.findByText("Update bounded component")).toBeInTheDocument();
    expect(screen.getByText("score 0.830")).toBeInTheDocument();
    const request = requests.find((item) => item.path === "/api/routing/preview");
    expect(request?.body).toMatchObject({ task_id: "task-1", local_required: false });
    expect(requests.some((item) => item.path.includes("/api/runs") && item.method === "POST")).toBe(false);
  });

  it("sends but never renders a provider secret reference", async () => {
    render(<RoutingCenter />);
    await screen.findAllByText("Local server");

    const profileIdField = screen.getByText("Profile ID").closest("label");
    expect(profileIdField).not.toBeNull();
    fireEvent.change(profileIdField!.querySelector("input")!, { target: { value: "cloud" } });
    const displayNameField = screen.getByText("Display name").closest("label");
    expect(displayNameField).not.toBeNull();
    fireEvent.change(displayNameField!.querySelector("input")!, { target: { value: "Cloud account" } });
    const secretField = screen.getByText("Secret reference").closest("label");
    expect(secretField).not.toBeNull();
    fireEvent.change(secretField!.querySelector("input")!, {
      target: { value: "secret://cloud-key" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));

    await waitFor(() => {
      const request = requests.find(
        (item) => item.path === "/api/routing/providers" && item.method === "POST"
      );
      expect(request?.body).toMatchObject({ secret_ref: "secret://cloud-key" });
      expect(
        request && requestMatchesLegacyContract("providerSave", request),
      ).toBe(true);
    });
    expect(screen.queryByText("secret://cloud-key")).not.toBeInTheDocument();
  });

  it("links LAN-discovered targets to the explicit discovery workspace", async () => {
    render(<RoutingCenter />);
    await screen.findAllByText("Local server");

    const link = screen.getByRole("link", { name: /discover lan models/i });
    expect(link).toHaveAttribute("href", "#/flock/lan");
    expect(
      screen.getByText(/LAN targets are reviewed and enabled in LAN discovery/i),
    ).toBeVisible();
  });

  it("preserves the revision-aware target save contract", async () => {
    render(<RoutingCenter />);
    await screen.findAllByText("Local server");

    const targetIdField = screen.getByText("Target ID").closest("label");
    expect(targetIdField).not.toBeNull();
    fireEvent.change(targetIdField!.querySelector("input")!, {
      target: { value: "review-worker" },
    });
    const providerField = screen
      .getByText("Provider profile")
      .closest("label");
    expect(providerField).not.toBeNull();
    fireEvent.change(providerField!.querySelector("select")!, {
      target: { value: "local" },
    });
    const modelField = screen.getByText("Model").closest("label");
    expect(modelField).not.toBeNull();
    fireEvent.change(modelField!.querySelector("input")!, {
      target: { value: "review-model" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save target" }));

    await waitFor(() => {
      const request = requests.find(
        (item) =>
          item.path === "/api/routing/targets" &&
          item.method === "POST",
      );
      expect(
        request && requestMatchesLegacyContract("targetSave", request),
      ).toBe(true);
      expect(request?.body).toMatchObject({
        target_id: "review-worker",
        provider_profile_id: "local",
        provider: "openai-compatible",
        model: "review-model",
        expected_revision: null,
      });
    });
  });
});

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}
