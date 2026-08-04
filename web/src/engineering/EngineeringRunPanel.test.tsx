import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EngineeringRunPanel } from "./EngineeringRunPanel";

const packet = {
  packet_id: "packet_1",
  objective: "Ship the reviewed repair",
  checkpoint: "before remote mutation",
  packet_digest: "a".repeat(64),
  status: "pending",
  authorization_record_count: 2,
  calls: [
    {
      tool_call_id: "commit_1",
      tool_name: "git.commit",
      arguments: {
        message: "repair: commit reviewed candidate",
        repair_review_id: "repair_review_1"
      },
      call_digest: "b".repeat(64),
      risk: "high",
      capability_revision: 7,
      resource_digest: "d".repeat(64),
      reason: "Commit the reviewed candidate",
      resource_scope: "branch kestrel/repair",
      expected_side_effect: "one local commit",
      rollback: "reset reviewed branch",
      status: "pending"
    },
    {
      tool_call_id: "publish_1",
      tool_name: "github.pr.create",
      arguments: {
        repository: "owner/repo",
        head: "kestrel/repair"
      },
      call_digest: "c".repeat(64),
      risk: "high",
      capability_revision: 9,
      resource_digest: "e".repeat(64),
      reason: "Publish the reviewed branch",
      resource_scope: "repository owner/repo",
      expected_side_effect: "one pull request",
      rollback: "close pull request",
      status: "pending"
    }
  ]
};

const githubRequest = {
  request_id: "request_1",
  review_id: "review_1",
  title: "Repair authentication",
  base_branch: "main",
  head_branch: "kestrel/repair",
  status: "ci_failed",
  request_digest: "d".repeat(64),
  external_number: 42,
  external_url: "https://github.example/owner/repo/pull/42",
  publish_tool_request: {
    tool_name: "github.pr.create",
    arguments: { request_id: "request_1", expected_request_digest: "d".repeat(64) }
  },
  sync_tool_request: {
    tool_name: "github.pr.sync",
    arguments: { request_id: "request_1", expected_request_digest: "d".repeat(64) }
  },
  feedback: [{ event_id: "check_1", kind: "check", status: "failed" }]
};

describe("EngineeringRunPanel", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("keeps exact-call decisions individual and renders browser and shipping evidence", async () => {
    const requests: Array<{ path: string; method: string; body: unknown }> = [];
    const onPrepareTool = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      requests.push({ path, method, body });
      if (method === "POST" && path.includes("/approval-packets/packet_1/decision")) {
        return jsonResponse({
          ...packet,
          status: "partially_approved",
          calls: packet.calls.map((call) => ({
            ...call,
            status: call.tool_call_id === "commit_1" ? "approved" : "denied"
          }))
        });
      }
      if (method === "POST" && path.includes("/github-change-requests/request_1/recover")) {
        return jsonResponse({ amendment_id: "amend_1", status: "applied" });
      }
      if (path.endsWith("/approval-packets")) return jsonResponse({ items: [packet] });
      if (path.endsWith("/graph/amendments")) return jsonResponse({ items: [] });
      if (path.endsWith("/candidate-fanouts")) return jsonResponse({ items: [] });
      if (path.endsWith("/browser-validations")) {
        return jsonResponse({
          items: [{
            validation_id: "browser_1",
            task_id: "task_1",
            candidate_id: "candidate_1",
            candidate_digest: "e".repeat(64),
            target_url: "http://host.kestrel.internal:4173/",
            status: "passed",
            failure_codes: [],
            report: {
              screenshot: {
                data_url: "data:image/png;base64,iVBORw0KGgo=",
                sha256: "f".repeat(64),
                width: 320,
                height: 200,
                bytes: 20
              }
            }
          }]
        });
      }
      if (path.endsWith("/github-change-requests")) {
        return jsonResponse({ items: [githubRequest] });
      }
      return jsonResponse({ detail: "not_found" }, 404);
    }));

    render(
      <EngineeringRunPanel
        runId="run_1"
        refreshToken="0"
        tasks={[]}
        defaultBranch="main"
        onPrepareTool={onPrepareTool}
      />
    );

    expect(await screen.findByRole("heading", { name: /Approval packets/ })).toBeInTheDocument();
    expect(screen.getByRole("img", {
      name: "Browser validation for http://host.kestrel.internal:4173/"
    })).toBeInTheDocument();
    expect(screen.getByText("tool:git.commit")).toBeVisible();
    expect(screen.getByText("revision 7")).toBeVisible();
    expect(screen.getByText("d".repeat(64))).toBeVisible();
    expect(screen.getByText("commit_1")).toBeVisible();
    expect(
      screen.getAllByText("Valid until decision or binding change").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(/repair_review_id/),
    ).not.toBeVisible();
    fireEvent.click(
      screen.getByRole("button", {
        name: "git.commit exact arguments evidence",
      }),
    );
    expect(screen.getByText(/repair_review_id/)).toBeVisible();

    const submit = screen.getByRole("button", { name: "Submit individual decisions" });
    expect(submit).toBeDisabled();
    fireEvent.click(within(screen.getByRole("group", {
      name: "Decision for git.commit"
    })).getByRole("button", { name: "Approve" }));
    expect(submit).toBeDisabled();
    fireEvent.click(within(screen.getByRole("group", {
      name: "Decision for github.pr.create"
    })).getByRole("button", { name: "Deny" }));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(requests).toContainEqual(expect.objectContaining({
      method: "POST",
      body: {
        expected_packet_digest: packet.packet_digest,
        decisions: { commit_1: true, publish_1: false }
      }
    })));

    fireEvent.click(screen.getByRole("button", { name: "Prepare exact-call publish" }));
    expect(onPrepareTool).toHaveBeenCalledWith(
      "github.pr.create",
      githubRequest.publish_tool_request.arguments
    );
  });

  // §5.4: candidate selection is exercised end to end at the panel
  // level — a running fan-out lists isolated candidates, the owner
  // triggers selection, and the selected candidate is reflected from
  // the refreshed fan-out projection.
  it("selects a candidate from trusted fan-out evidence", async () => {
    const fanout = {
      fanout_id: "fanout_1",
      source_task_id: "repair",
      status: "running",
      selected_candidate_id: null as string | null,
      estimated_budget_delta_usd: 0.05,
      candidates: [
        {
          candidate_id: "candidate_a",
          branch: "kestrel/candidate/a",
          status: "validated",
          validation_id: "repair_validation_a",
          validation_passed: true,
          reviewer_identities: ["reviewer:local"],
          changed_file_count: 1,
          changed_line_count: 4,
          risk_notes: [],
          actual_cost_usd: 0.01,
          latency_seconds: 12
        },
        {
          candidate_id: "candidate_b",
          branch: "kestrel/candidate/b",
          status: "validated",
          validation_id: "repair_validation_b",
          validation_passed: false,
          reviewer_identities: [],
          changed_file_count: 3,
          changed_line_count: 41,
          risk_notes: ["touches public API"],
          actual_cost_usd: 0.02,
          latency_seconds: 19
        }
      ]
    };
    const requests: Array<{ path: string; method: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      requests.push({ path, method });
      if (method === "POST" && path.includes("/candidate-fanouts/fanout_1/select")) {
        return jsonResponse({ ...fanout, status: "selected", selected_candidate_id: "candidate_a" });
      }
      if (path.endsWith("/approval-packets")) return jsonResponse({ items: [] });
      if (path.endsWith("/graph/amendments")) return jsonResponse({ items: [] });
      if (path.endsWith("/candidate-fanouts")) {
        const selected = requests.some(
          (request) => request.method === "POST" && request.path.includes("/select"),
        );
        return jsonResponse({
          items: [selected
            ? { ...fanout, status: "selected", selected_candidate_id: "candidate_a" }
            : fanout]
        });
      }
      if (path.endsWith("/browser-validations")) return jsonResponse({ items: [] });
      if (path.endsWith("/github-change-requests")) return jsonResponse({ items: [] });
      return jsonResponse({ detail: "not_found" }, 404);
    }));

    render(
      <EngineeringRunPanel
        runId="run_1"
        refreshToken="0"
        tasks={[]}
        defaultBranch="main"
        onPrepareTool={vi.fn()}
      />
    );

    expect(
      await screen.findByRole("heading", { name: /Candidate comparison/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("kestrel/candidate/a")).toBeInTheDocument();
    expect(screen.getByText(/Validated · 1 reviewer\(s\)/)).toBeInTheDocument();
    expect(screen.getByText("Validation failed")).toBeInTheDocument();
    expect(screen.getByText(/touches public API/)).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Select from trusted evidence" }),
    );
    await waitFor(() =>
      expect(requests).toContainEqual(
        expect.objectContaining({
          method: "POST",
          path: expect.stringContaining("/candidate-fanouts/fanout_1/select"),
        }),
      ),
    );
    // After the post-selection refresh, the selected candidate carries
    // the durable selected marker.
    await waitFor(() =>
      expect(
        document.querySelector(".candidate.selected strong")?.textContent,
      ).toBe("kestrel/candidate/a"),
    );
  });
});

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
