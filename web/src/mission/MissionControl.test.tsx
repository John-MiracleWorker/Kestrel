import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Run, TaskGraph } from "../types";
import { MissionControl } from "./MissionControl";
import type { MissionLaunch, MissionPreflight, ProjectProfile } from "./types";

const project: ProjectProfile = {
  project_id: "project_kestrel",
  display_name: "Kestrel",
  repository_path: "/tmp/kestrel",
  remote: "git@example.invalid:kestrel.git",
  default_branch: "main",
  allowed_paths: ["."],
  provider_policy: { preset: "balanced" },
  cost_budget: 1.5,
  privacy_class: "local_required",
  test_recipes: [{ name: "pytest", command: "pytest -q" }],
  build_recipes: [],
  capability_ceiling: ["file.read", "repair.prepare", "repair.validate"],
  baseline_index_digest: "sha256:index",
  archived_at: null,
  revision: 1,
  created_at: "2026-07-27T12:00:00Z",
  updated_at: "2026-07-27T12:00:00Z"
};

const preflight: MissionPreflight = {
  schema: "kestrel.mission_preflight.v1",
  project_id: project.project_id,
  project_name: project.display_name,
  repository_path: project.repository_path,
  objective: "Fix the failing authentication test without changing the public API",
  template_id: "fix_failing_test",
  branch: "main",
  working_tree: { state: "dirty", summary: "2 local changes" },
  route_policy: "Balanced",
  budget: { currency: "USD", limit: 1.5, estimate: 0.42 },
  effective_capabilities: ["Read repo", "isolated write", "validation"],
  likely_approvals: ["repair.apply_patch", "git.commit"],
  validation_recipes: ["pytest targeted", "pytest full"],
  rollback: "Worktree + signed review",
  index: {
    freshness: "current",
    digest: "sha256:index",
    indexed_at: "2026-07-27T12:00:00Z",
    detail: "Current · 3 min"
  },
  provider: { status: "pass", detail: "Local model ready" },
  checks: [
    { check_id: "route", title: "Route", status: "pass", detail: "Balanced" },
    { check_id: "budget", title: "Budget", status: "pass", detail: "$1.50 cap" },
    { check_id: "capabilities", title: "Permissions", status: "pass", detail: "Narrowed" },
    { check_id: "validation", title: "Validation", status: "pass", detail: "Two recipes" },
    { check_id: "rollback", title: "Rollback", status: "pass", detail: "Available" }
  ],
  tasks: [
    {
      task_id: "map",
      title: "Map the failure",
      rationale: "Reproduce the failure and inspect relevant code paths.",
      dependencies: [],
      acceptance_criteria: ["Failure reproduced", "Root cause stated"],
      required_tools: ["repo.context_pack", "test.run"],
      risk: "low"
    },
    {
      task_id: "repair",
      title: "Repair in isolation",
      rationale: "Apply the smallest compatible change.",
      dependencies: ["map"],
      acceptance_criteria: ["Targeted test passes"],
      required_tools: ["repair.prepare", "repair.apply_patch"],
      risk: "medium"
    },
    {
      task_id: "prove",
      title: "Prove and review",
      rationale: "Run broader validation and create review evidence.",
      dependencies: ["repair"],
      acceptance_criteria: ["Full suite passes", "Signed review current"],
      required_tools: ["repair.validate", "repair.review"],
      risk: "medium"
    }
  ],
  warnings: ["Working tree has 2 local changes; isolation will preserve them."],
  blockers: [],
  can_start: true,
  generated_at: "2026-07-27T12:03:00Z"
};

describe("MissionControl", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("keeps project, plan, proof forecast, editing, and launch in one task-first flow", async () => {
    const onLaunch = vi.fn<(mission: MissionLaunch) => Promise<void>>(async () => undefined);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") {
        return jsonResponse({ items: [project], count: 1 });
      }
      if (path === `/api/projects/${project.project_id}/mission/preflight` && init?.method === "POST") {
        return jsonResponse(preflight);
      }
      return jsonResponse({ detail: "not_found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(
      <MissionControl
        runs={[]}
        activeRun={null}
        taskGraph={null}
        approvals={[]}
        events={[]}
        onLaunch={onLaunch}
        onOpenRun={() => undefined}
        onOpenHistory={() => undefined}
        onOpenAdvanced={() => undefined}
        onOpenDiagnostics={() => undefined}
        onPrepareTool={() => undefined}
        onAuthRequired={() => undefined}
      />
    );

    expect(await screen.findByRole("option", { name: "Kestrel" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "What should Kestrel accomplish?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run mission" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Engineering objective"), {
      target: { value: preflight.objective }
    });
    fireEvent.click(screen.getByRole("button", { name: "Inspect plan" }));

    expect(await screen.findByText("Map the failure")).toBeInTheDocument();
    expect(screen.getAllByText("2 local changes")).toHaveLength(2);
    expect(screen.getByText("Local model ready")).toBeInTheDocument();
    expect(screen.getByText("Ready to run")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run mission" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Edit plan" }));
    const title = screen.getByLabelText("Task 1 title");
    fireEvent.change(title, { target: { value: "Map the auth failure" } });
    expect(screen.getByRole("button", { name: "Finish editing plan" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Finish editing" }));
    fireEvent.click(screen.getByRole("button", { name: "Run mission" }));

    await waitFor(() => expect(onLaunch).toHaveBeenCalledTimes(1));
    expect(onLaunch.mock.calls[0]?.[0].plan[0]?.title).toBe("Map the auth failure");
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/projects/${project.project_id}/mission/preflight`,
      expect.objectContaining({ method: "POST" })
    );

    const report = await axe.run(container);
    expect(report.violations).toEqual([]);
  });

  it("shows blockers truthfully and never enables launch", async () => {
    const blocked = {
      ...preflight,
      provider: { status: "fail" as const, detail: "No validated target" },
      blockers: ["Connect and validate a provider target."],
      can_start: false
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") return jsonResponse({ items: [project], count: 1 });
      if (init?.method === "POST") return jsonResponse(blocked);
      return jsonResponse({ detail: path }, 404);
    }));

    render(
      <MissionControl
        runs={[]}
        activeRun={null}
        taskGraph={null}
        approvals={[]}
        events={[]}
        onLaunch={async () => undefined}
        onOpenRun={() => undefined}
        onOpenHistory={() => undefined}
        onOpenAdvanced={() => undefined}
        onOpenDiagnostics={() => undefined}
        onPrepareTool={() => undefined}
        onAuthRequired={() => undefined}
      />
    );

    await screen.findByRole("option", { name: "Kestrel" });
    fireEvent.change(screen.getByLabelText("Engineering objective"), {
      target: { value: preflight.objective }
    });
    fireEvent.click(screen.getByRole("button", { name: "Inspect plan" }));

    expect(await screen.findByText("Connect and validate a provider target.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run mission" })).toBeDisabled();
  });

  it("keeps completed repair proof and gated acceptance in the mission timeline", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") return jsonResponse({ items: [project], count: 1 });
      return jsonResponse({ detail: path }, 404);
    }));
    const onPrepareTool = vi.fn();
    const run: Run = {
      run_id: "run_reviewed",
      project_id: project.project_id,
      status: "completed",
      message: "Fix authentication",
      session_id: "session_reviewed",
      workspace: project.repository_path,
      model: "local",
      assistant_message: "Validated candidate ready.",
      tool_count: 4,
      context_chars: 1000,
      stop_reason: "complete",
      created_at: "2026-07-27T12:00:00Z",
      updated_at: "2026-07-27T12:05:00Z"
    };
    const digest = "d".repeat(64);
    const reviewId = `repair_review_${"a".repeat(24)}`;
    const validationId = `repair_validation_${"b".repeat(24)}`;
    const preview = [
      "diff --git a/src/auth.py b/src/auth.py",
      "--- a/src/auth.py",
      "+++ b/src/auth.py",
      "@@ -1 +1 @@",
      "-return False",
      "+return True"
    ].join("\n");
    const taskGraph: TaskGraph = {
      tasks: [
        {
          task_id: "validate",
          title: "Validate repair",
          goal: "Run tests",
          profile: "worker",
          status: "completed",
          approved: true,
          required_tools: ["repair.validate"],
          acceptance_criteria: ["Authentication regression passes"],
          result: {
            repair_artifact: {
              tool: "repair.validate",
              validation_id: validationId,
              repair_snapshot: {
                branch: "kestrel/worker/run-review/repair",
                head_sha: "1".repeat(40),
                diff_digest: digest
              }
            }
          }
        },
        {
          task_id: "review",
          title: "Review repair",
          goal: "Bind evidence",
          profile: "reviewer",
          status: "completed",
          approved: true,
          required_tools: ["repair.review"],
          acceptance_criteria: ["Signed review remains current"],
          result: {
            repair_artifact: {
              tool: "repair.review",
              validation_id: validationId,
              review_id: reviewId,
              repair_snapshot: {
                branch: "kestrel/worker/run-review/repair",
                head_sha: "1".repeat(40),
                diff_digest: digest
              },
              changed_files: ["src/auth.py"],
              diff_preview: {
                content: preview,
                omitted_files: 0,
                truncated: false
              },
              commit_gate: {
                commit_allowed: true,
                approval_required_before_commit: true
              }
            }
          }
        }
      ],
      ready_tasks: [],
      approval_blocked_tasks: [],
      subagents: []
    };

    render(
      <MissionControl
        runs={[run]}
        activeRun={run}
        taskGraph={taskGraph}
        approvals={[]}
        events={[]}
        onLaunch={async () => undefined}
        onOpenRun={() => undefined}
        onOpenHistory={() => undefined}
        onOpenAdvanced={() => undefined}
        onOpenDiagnostics={() => undefined}
        onPrepareTool={onPrepareTool}
        onAuthRequired={() => undefined}
      />
    );

    expect(await screen.findByRole("option", { name: "Kestrel" })).toBeInTheDocument();
    expect(screen.getByLabelText("Repair Patch Review")).toBeInTheDocument();
    expect(screen.getByText("Authentication regression passes")).toBeInTheDocument();
    expect(screen.getByText(/\+return True/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Prepare exact-call git.commit request" }));
    expect(onPrepareTool).toHaveBeenCalledWith(
      "git.commit",
      expect.objectContaining({ repair_review_id: reviewId })
    );
  });
});

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
