import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { requestMatchesLegacyContract } from "../testing/apiFixtures";
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
  project_revision: project.revision,
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
  launch_binding: {
    schema: "kestrel.mission_launch_binding.v1",
    project_id: project.project_id,
    project_revision: project.revision,
    objective_digest: "a".repeat(64),
    template_id: "fix_failing_test",
    config_digest: "b".repeat(64),
    routing_enabled: false,
    routing_mode: "off",
    policy_id: "balanced",
    policy_revision: 1,
    inventory_digest: "c".repeat(64),
    preflight_digest: "e".repeat(64),
    plan_digest: "f".repeat(64),
    binding_digest: "d".repeat(64)
  },
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

  function renderMissionControl(
    overrides: Partial<Parameters<typeof MissionControl>[0]> = {},
  ) {
    return render(
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
        onDecideApproval={() => undefined}
        onContinueConversation={async () => undefined}
        onAuthRequired={() => undefined}
        {...overrides}
      />,
    );
  }

  // P1-3: an out-of-order (older) preflight response that resolves after
  // the objective was edited must not repopulate launch authority for
  // the new objective.
  it("ignores a stale preflight response that resolves after the objective changed", async () => {
    const deferred: Array<{
      body: Record<string, unknown>;
      resolve: (response: Response) => void;
    }> = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") {
        return Promise.resolve(jsonResponse({ items: [project], count: 1 }));
      }
      if (path.endsWith("/mission/preflight") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        return new Promise<Response>((resolve) => {
          deferred.push({ body, resolve });
        });
      }
      return Promise.resolve(jsonResponse({ detail: "not_found" }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    const onLaunch = vi.fn<(mission: MissionLaunch) => Promise<void>>(async () => undefined);

    renderMissionControl({ onLaunch });

    await screen.findByRole("option", { name: "Kestrel" });
    const objectiveInput = screen.getByLabelText("Objective");
    fireEvent.change(objectiveInput, {
      target: { value: preflight.objective },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));
    await waitFor(() => expect(deferred).toHaveLength(1));

    // Edit the objective while the review is in flight; launch authority
    // for the old objective must be dropped immediately.
    fireEvent.change(objectiveInput, {
      target: { value: "Changed objective after review started" },
    });
    expect(screen.getByRole("button", { name: "Start mission" })).toBeDisabled();

    // The stale response arrives late and must be rejected. Wrap the
    // out-of-band resolution in act() so the rejection state flush
    // (preflightPending clearing) is committed before asserting.
    await act(async () => {
      deferred[0]?.resolve(
        jsonResponse({ ...preflight, objective: preflight.objective }),
      );
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Start mission" }),
      ).toBeDisabled(),
    );
    expect(screen.queryByText("Ready to start")).not.toBeInTheDocument();

    // A fresh review for the changed objective is accepted. Wait for
    // the in-flight review state to clear before requesting it.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Review mission" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));
    await waitFor(() => expect(deferred).toHaveLength(2));
    await act(async () => {
      deferred[1]?.resolve(
        jsonResponse({
          ...preflight,
          objective: "Changed objective after review started",
        }),
      );
    });
    expect(await screen.findByText("Ready to start")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start mission" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Start mission" }));
    await waitFor(() => expect(onLaunch).toHaveBeenCalledTimes(1));
    expect(onLaunch.mock.calls[0]?.[0].objective).toBe(
      "Changed objective after review started",
    );
  });

  // P1-3: a failed re-review must not leave an earlier can_start
  // projection available for launch.
  it("clears launch authority when a re-review fails after a successful review", async () => {
    let preflightCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") {
        return jsonResponse({ items: [project], count: 1 });
      }
      if (path.endsWith("/mission/preflight") && init?.method === "POST") {
        preflightCalls += 1;
        if (preflightCalls === 1) return jsonResponse(preflight);
        throw new Error("preflight service unavailable");
      }
      return jsonResponse({ detail: "not_found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onLaunch = vi.fn<(mission: MissionLaunch) => Promise<void>>(async () => undefined);

    renderMissionControl({ onLaunch });

    await screen.findByRole("option", { name: "Kestrel" });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: preflight.objective },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));
    expect(await screen.findByText("Ready to start")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start mission" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));
    await waitFor(() => expect(preflightCalls).toBe(2));
    await screen.findByText(/preflight service unavailable/i);

    expect(screen.getByRole("button", { name: "Start mission" })).toBeDisabled();
    expect(screen.queryByText("Ready to start")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start mission" }));
    expect(onLaunch).not.toHaveBeenCalled();
  });

  // P1-3: launchMission must re-verify that the accepted projection still
  // matches the current objective before posting a run.
  it("refuses to launch when the objective changed after review without re-review", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") {
        return jsonResponse({ items: [project], count: 1 });
      }
      if (path.endsWith("/mission/preflight") && init?.method === "POST") {
        return jsonResponse(preflight);
      }
      return jsonResponse({ detail: "not_found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onLaunch = vi.fn<(mission: MissionLaunch) => Promise<void>>(async () => undefined);

    renderMissionControl({ onLaunch });

    await screen.findByRole("option", { name: "Kestrel" });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: preflight.objective },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));
    expect(await screen.findByText("Ready to start")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start mission" })).toBeEnabled();

    // Change the objective after review; the accepted projection no longer
    // matches the current input, so launch must be refused.
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: "Unreviewed objective edit" },
    });
    expect(screen.getByRole("button", { name: "Start mission" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Start mission" }));
    expect(onLaunch).not.toHaveBeenCalled();
  });

  // P1-3: a template change after review invalidates the projection for
  // the previously reviewed template.
  it("refuses to launch when the template changed after review without re-review", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") {
        return jsonResponse({ items: [project], count: 1 });
      }
      if (path.endsWith("/mission/preflight") && init?.method === "POST") {
        return jsonResponse(preflight);
      }
      return jsonResponse({ detail: "not_found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onLaunch = vi.fn<(mission: MissionLaunch) => Promise<void>>(async () => undefined);

    renderMissionControl({ onLaunch });

    await screen.findByRole("option", { name: "Kestrel" });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: preflight.objective },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));
    expect(await screen.findByText("Ready to start")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start mission" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: /Explain repo/i }));
    expect(screen.getByRole("button", { name: "Start mission" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Start mission" }));
    expect(onLaunch).not.toHaveBeenCalled();
  });

  // P1-3: if the project revision drifts (re-indexed elsewhere) the
  // accepted projection bound to the old revision must not launch.
  it("refuses to launch when the project revision drifts after review", async () => {
    let projectsCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") {
        projectsCalls += 1;
        return jsonResponse({
          items: [projectsCalls === 1 ? project : { ...project, revision: 2 }],
          count: 1,
        });
      }
      if (path.endsWith("/mission/preflight") && init?.method === "POST") {
        return jsonResponse(preflight);
      }
      return jsonResponse({ detail: "not_found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onLaunch = vi.fn<(mission: MissionLaunch) => Promise<void>>(async () => undefined);

    renderMissionControl({ onLaunch });

    await screen.findByRole("option", { name: "Kestrel" });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: preflight.objective },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));
    expect(await screen.findByText("Ready to start")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start mission" })).toBeEnabled();

    // Simulate a project refresh returning a bumped revision.
    fireEvent.click(screen.getByRole("button", { name: "Refresh projects" }));
    await waitFor(() => expect(projectsCalls).toBe(2));

    expect(screen.getByRole("button", { name: "Start mission" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Start mission" }));
    expect(onLaunch).not.toHaveBeenCalled();
  });

  it("keeps project, plan, proof forecast, editing, and launch in one task-first flow", async () => {
    const onLaunch = vi.fn<(mission: MissionLaunch) => Promise<void>>(async () => undefined);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") {
        return jsonResponse({ items: [project], count: 1 });
      }
      if (path === `/api/projects/${project.project_id}/mission/preflight` && init?.method === "POST") {
        const request = JSON.parse(String(init.body)) as {
          mission_plan?: MissionPreflight["tasks"];
        };
        return jsonResponse(request.mission_plan ? {
          ...preflight,
          tasks: request.mission_plan,
          launch_binding: {
            ...preflight.launch_binding,
            plan_digest: "1".repeat(64),
            binding_digest: "2".repeat(64)
          }
        } : preflight);
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
        onDecideApproval={() => undefined}
        onContinueConversation={async () => undefined}
        onAuthRequired={() => undefined}
      />
    );

    expect(await screen.findByRole("option", { name: "Kestrel" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "What should Kestrel accomplish?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start mission" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: preflight.objective }
    });
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));

    expect(await screen.findByText("Map the failure")).toBeInTheDocument();
    expect(
      screen.getAllByText(/2 local changes/).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("Local model ready")).toBeInTheDocument();
    expect(screen.getByText("Ready to start")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start mission" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Edit acceptance plan" }));
    const title = screen.getByLabelText("Task 1 title");
    fireEvent.change(title, { target: { value: "Map the auth failure" } });
    expect(screen.getByRole("button", { name: "Finish editing plan" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Finish editing" }));
    await waitFor(() => expect(
      screen.getByRole("button", { name: "Start mission" })
    ).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Start mission" }));

    await waitFor(() => expect(onLaunch).toHaveBeenCalledTimes(1));
    expect(onLaunch.mock.calls[0]?.[0].plan[0]?.title).toBe("Map the auth failure");
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/projects/${project.project_id}/mission/preflight`,
      expect.objectContaining({ method: "POST" })
    );
    const initialPreflightCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input) ===
          `/api/projects/${project.project_id}/mission/preflight` &&
        init?.method === "POST" &&
        !Object.hasOwn(
          JSON.parse(String(init.body ?? "{}")),
          "mission_plan",
        ),
    );
    expect(
      initialPreflightCall &&
        requestMatchesLegacyContract("missionPreflight", {
          path: String(initialPreflightCall[0]),
          method: String(initialPreflightCall[1]?.method ?? "GET"),
          body: JSON.parse(
            String(initialPreflightCall[1]?.body ?? "{}"),
          ),
        }),
    ).toBe(true);
    const reboundCall = fetchMock.mock.calls.find(([_input, init]) => {
      const body = JSON.parse(String((init as RequestInit | undefined)?.body ?? "{}"));
      return Array.isArray(body.mission_plan);
    });
    expect(reboundCall).toBeDefined();
    expect(onLaunch.mock.calls[0]?.[0]).toMatchObject({
      project: { revision: 1 },
      preflight: {
        project_revision: 1,
        launch_binding: {
          schema: "kestrel.mission_launch_binding.v1",
          project_revision: 1,
        },
      },
    });

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
        onDecideApproval={() => undefined}
        onContinueConversation={async () => undefined}
        onAuthRequired={() => undefined}
      />
    );

    await screen.findByRole("option", { name: "Kestrel" });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: preflight.objective }
    });
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));

    expect(await screen.findByText("Connect and validate a provider target.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start mission" })).toBeDisabled();
  });

  it("rebuilds a missing project index through the revision-bound project API", async () => {
    const missingIndex = {
      ...preflight,
      index: {
        freshness: "missing" as const,
        digest: null,
        indexed_at: null,
        detail: "No repository index exists for this project."
      }
    };
    const currentIndex = {
      ...preflight,
      project_revision: 2,
      index: {
        freshness: "current" as const,
        digest: "sha256:rebuilt",
        indexed_at: "2026-07-27T12:10:00Z",
        detail: "Repository index matches the current repository snapshot."
      }
    };
    let preflightCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") {
        return jsonResponse({ items: [project], count: 1 });
      }
      if (path.endsWith("/mission/preflight") && init?.method === "POST") {
        preflightCalls += 1;
        return jsonResponse(preflightCalls === 1 ? missingIndex : currentIndex);
      }
      if (path.endsWith("/index/rebuild") && init?.method === "POST") {
        return jsonResponse({
          schema: "kestrel.project_index_rebuild.v1",
          project: {
            ...project,
            revision: 2,
            baseline_index_digest: "sha256:rebuilt"
          },
          report: {
            aggregate_digest: "sha256:rebuilt",
            changed_files: 2,
            reused_files: 0,
            deleted_files: 0,
            skipped_files: 0,
            indexed_files: 2,
            git_head: null,
            git_tree: null
          }
        });
      }
      return jsonResponse({ detail: path }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
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
        onDecideApproval={() => undefined}
        onContinueConversation={async () => undefined}
        onAuthRequired={() => undefined}
      />
    );

    await screen.findByRole("option", { name: "Kestrel" });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: preflight.objective }
    });
    fireEvent.click(screen.getByRole("button", { name: "Review mission" }));
    const rebuild = await screen.findByRole("button", {
      name: "Rebuild project index"
    });
    fireEvent.click(rebuild);

    expect(
      await screen.findByText("Repository index matches the current repository snapshot.")
    ).toBeInTheDocument();
    const rebuildCall = fetchMock.mock.calls.find(([input]) => (
      String(input).includes("/index/rebuild")
    ));
    expect(JSON.parse(String((rebuildCall?.[1] as RequestInit | undefined)?.body))).toEqual({
      expected_project_revision: 1
    });
  });

  // P2-1: an existing active run reloaded with no in-memory preflight
  // must render a distinct durable active-run authority snapshot —
  // never the compose-time "Not inspected"/"Review required" language.
  it("renders a durable active-run authority snapshot after reload", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), "http://kestrel.test").pathname;
      if (path === "/api/projects") return jsonResponse({ items: [project], count: 1 });
      return jsonResponse({ detail: path }, 404);
    }));
    const run: Run = {
      run_id: "run_active_reload",
      project_id: project.project_id,
      status: "running",
      message: "Fix the failing authentication test",
      session_id: "session_active_reload",
      workspace: project.repository_path,
      provider: "local",
      model: "local",
      assistant_message: "Working through the acceptance plan.",
      tool_count: 2,
      context_chars: 800,
      stop_reason: "",
      created_at: "2026-07-27T12:00:00Z",
      updated_at: "2026-07-27T12:05:00Z"
    };

    renderMissionControl({ runs: [run], activeRun: run });

    // Durable project/run evidence is shown.
    await waitFor(() =>
      expect(
        screen.getByLabelText("Selected repository"),
      ).toHaveTextContent("/tmp/kestrel"),
    );
    expect(screen.getByText("Active run")).toBeInTheDocument();
    // The launch-time binding is not persisted in the current Run
    // projection; that absence must be stated explicitly rather than
    // substituting compose-time preflight language.
    expect(
      screen.getAllByText(
        /launch-time binding not persisted in current projection/i,
      ).length,
    ).toBeGreaterThan(0);
    // Compose-time "never inspected" language must not appear.
    expect(
      screen.queryByText(/no run can start until/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Not inspected")).not.toBeInTheDocument();
    // The read-only active-run context never offers a launch action.
    expect(
      screen.queryByRole("button", { name: "Start mission" }),
    ).not.toBeInTheDocument();
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
              schema_version: 1,
              tool: "repair.validate",
              validation_id: validationId,
              success: true,
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
              schema_version: 1,
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
                format: "unified",
                content: preview,
                bound_diff_digest: digest,
                redacted: true,
                authoritative: false,
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
        onDecideApproval={() => undefined}
        onContinueConversation={async () => undefined}
        onAuthRequired={() => undefined}
      />
    );

    await waitFor(() =>
      expect(
        screen.getByLabelText("Selected repository"),
      ).toHaveTextContent("/tmp/kestrel"),
    );
    expect(screen.getByLabelText("Repair Patch Review")).toBeInTheDocument();
    expect(screen.getByText("Authentication regression passes")).toBeInTheDocument();
    expect(
      screen.getByText("+return True", { selector: ".diff-add" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Prepare exact-call patch export" }));
    expect(onPrepareTool).toHaveBeenCalledWith("git.export_patch", {
      repair_review_id: reviewId,
      expected_current_diff_digest: digest
    });
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
