import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MissionPreflightCard } from "./MissionPreflightCard";
import type {
  MissionPreflight,
  ProjectProfile,
} from "./types";

const project: ProjectProfile = {
  project_id: "project_kestrel",
  display_name: "Kestrel",
  repository_path: "/tmp/kestrel",
  default_branch: "main",
  allowed_paths: ["."],
  provider_policy: { preset: "local_only" },
  cost_budget: 0,
  privacy_class: "local_required",
  test_recipes: [],
  build_recipes: [],
  capability_ceiling: ["tool:file.read"],
  revision: 1,
  created_at: "2026-07-29T00:00:00Z",
  updated_at: "2026-07-29T00:00:00Z",
};

function preflight(
  overrides: Partial<MissionPreflight> = {},
): MissionPreflight {
  return {
    schema: "kestrel.mission_preflight.v1",
    project_id: project.project_id,
    project_revision: 1,
    project_name: project.display_name,
    repository_path: project.repository_path,
    objective: "Explain the repository",
    template_id: "explain_repository",
    branch: "main",
    working_tree: {
      state: "clean",
      summary: "Working tree is clean.",
    },
    route_policy: "Demo · local only",
    budget: { currency: "USD", limit: 0, estimate: 0 },
    effective_capabilities: ["tool:file.read"],
    likely_approvals: [],
    validation_recipes: ["pytest -q"],
    rollback: "Read-only mission; no repository mutation.",
    index: {
      freshness: "current",
      detail: "Repository index is current.",
    },
    provider: {
      status: "pass",
      detail: "Bundled deterministic Demo",
    },
    launch_binding: {
      schema: "kestrel.mission_launch_binding.v1",
      project_id: project.project_id,
      project_revision: 1,
      objective_digest: "a".repeat(64),
      template_id: "explain_repository",
      config_digest: "b".repeat(64),
      routing_enabled: false,
      routing_mode: "off",
      policy_id: "local_only",
      policy_revision: 1,
      inventory_digest: "c".repeat(64),
      preflight_digest: "d".repeat(64),
      plan_digest: "e".repeat(64),
      binding_digest: "f".repeat(64),
    },
    checks: [
      {
        check_id: "route",
        title: "Route",
        status: "pass",
        detail: "Demo route",
      },
      {
        check_id: "budget",
        title: "Budget",
        status: "pass",
        detail: "No external spend",
      },
      {
        check_id: "capabilities",
        title: "Permissions",
        status: "pass",
        detail: "Read-only",
      },
    ],
    tasks: [],
    warnings: [],
    blockers: [],
    can_start: true,
    generated_at: "2026-07-29T00:00:00Z",
    ...overrides,
  };
}

describe("MissionPreflightCard", () => {
  afterEach(cleanup);

  it("labels a safe Demo mission as no external spend", () => {
    const onStart = vi.fn();
    render(
      <MissionPreflightCard
        project={project}
        preflight={preflight()}
        error={null}
        launchPending={false}
        indexPending={false}
        editingPlan={false}
        onStart={onStart}
        onRebuildIndex={vi.fn()}
      />,
    );

    expect(screen.getByText("No external spend")).toBeVisible();
    expect(screen.getByText("Demo · local only")).toBeVisible();
    expect(screen.getByText("tool:file.read")).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Start mission" }),
    );
    expect(onStart).toHaveBeenCalledOnce();
  });

  it("never collapses a containment blocker into an unexplained disabled button", () => {
    render(
      <MissionPreflightCard
        project={project}
        preflight={preflight({
          checks: [
            {
              check_id: "containment",
              title: "Containment",
              status: "fail",
              detail:
                "A containment engine is required for this mission.",
              recovery:
                "Configure a supported containment engine before launch.",
            },
          ],
          blockers: [
            "A containment engine is required for this mission.",
          ],
          can_start: false,
        })}
        error={null}
        launchPending={false}
        indexPending={false}
        editingPlan={false}
        onStart={vi.fn()}
        onRebuildIndex={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Start mission" }),
    ).toBeDisabled();
    expect(
      screen.getAllByText(/containment engine is required/i)
        .length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("link", {
        name: "Open Containment settings",
      }),
    ).toHaveAttribute("href", "#/settings/containment");
  });
});
