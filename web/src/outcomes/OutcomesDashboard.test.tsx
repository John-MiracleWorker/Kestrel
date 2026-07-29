import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OutcomesDashboard } from "./OutcomesDashboard";

const zeroMetric = {
  value: 0,
  sample_count: 1,
  population: 1,
  coverage: 1,
  missing: false
};

const missingMetric = {
  value: null,
  sample_count: 0,
  population: 1,
  coverage: 0,
  missing: true
};

const report = {
  schema: "kestrel.outcomes.v1",
  generated_at: "2026-07-28T12:00:00Z",
  filters: {},
  summary: {
    run_count: zeroMetric,
    validated_completion_rate: missingMetric,
    actual_cost_usd: missingMetric
  },
  groups: [],
  baselines: [{
    baseline: "static_policy",
    available: false,
    sample_count: 0,
    validated_completion_rate: missingMetric,
    validated_success_per_dollar: missingMetric,
    inference: "insufficient evidence"
  }],
  evidence_coverage: {
    cost: { covered: 0, total: 1, rate: 0, missing: false },
    validation: { covered: 0, total: 0, rate: null, missing: true }
  }
};

describe("OutcomesDashboard", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("distinguishes zero from missing evidence and creates a redacted private case", async () => {
    const requests: Array<{ path: string; method: string; body: unknown }> = [];
    vi.stubGlobal("crypto", { randomUUID: () => "00000000-0000-4000-8000-000000000001" });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      requests.push({ path, method, body });
      if (path === "/api/projects") {
        return jsonResponse({
          items: [{ project_id: "project_1", display_name: "Kestrel" }]
        });
      }
      if (path.startsWith("/api/outcomes")) return jsonResponse(report);
      if (path.startsWith("/api/benchmarks") && method === "GET") {
        return jsonResponse({ items: [] });
      }
      if (path === "/api/benchmarks" && method === "POST") {
        const record = body as Record<string, unknown>;
        return jsonResponse({
          ...record,
          case_digest: "a".repeat(64),
          status: "active"
        });
      }
      return jsonResponse({ detail: "not_found" }, 404);
    }));

    render(<OutcomesDashboard onBack={() => undefined} />);

    expect(await screen.findByRole("heading", { name: "Outcomes and benchmarks" })).toBeInTheDocument();
    const runsCard = screen.getByText("Runs").closest("article");
    expect(runsCard).not.toBeNull();
    expect(runsCard).toHaveTextContent("0");
    const costCard = screen.getByText("Attributed cost").closest("article");
    expect(costCard).not.toBeNull();
    expect(costCard).toHaveTextContent("Missing");
    expect(screen.getByText("0.0%")).toBeInTheDocument();
    expect(screen.getAllByText("Missing").length).toBeGreaterThan(1);

    fireEvent.change(screen.getByLabelText("Project"), {
      target: { value: "project_1" }
    });
    await waitFor(() => expect(requests.some((request) =>
      request.path === "/api/benchmarks?project_id=project_1"
    )).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: /New case/ }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Repair auth regression" }
    });
    fireEvent.change(screen.getByLabelText("Redacted objective"), {
      target: { value: "Repair a redacted authentication regression" }
    });
    fireEvent.change(screen.getByLabelText("Acceptance criteria, one per line"), {
      target: { value: "Targeted test passes\nPublic API remains stable" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Save private case" }));

    await waitFor(() => expect(requests).toContainEqual(expect.objectContaining({
      path: "/api/benchmarks",
      method: "POST",
      body: expect.objectContaining({
        project_id: "project_1",
        name: "Repair auth regression",
        fixture: {
          objective: "Repair a redacted authentication regression",
          redacted: true
        },
        acceptance_criteria: [
          "Targeted test passes",
          "Public API remains stable"
        ]
      })
    })));
    expect(await screen.findByText("Repair auth regression")).toBeInTheDocument();
  });
});

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
