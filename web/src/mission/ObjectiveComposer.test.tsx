import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ObjectiveComposer } from "./ObjectiveComposer";
import type {
  MissionGoalTemplate,
  MissionPlanTask,
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

const template: MissionGoalTemplate = {
  template_id: "explain_repository",
  label: "Explain repo",
  description: "Map architecture and entry points.",
  default_objective: "Explain this repository.",
};

const task: MissionPlanTask = {
  task_id: "map",
  title: "Map the repository",
  rationale: "Ground the answer in source evidence.",
  dependencies: [],
  acceptance_criteria: ["Entry points identified"],
  required_tools: ["file.read"],
  risk: "low",
};

describe("ObjectiveComposer", () => {
  afterEach(cleanup);

  it("keeps project, goal, objective, and review action in one composer", () => {
    const onProjectChange = vi.fn();
    const onTemplateSelect = vi.fn();
    const onObjectiveChange = vi.fn();
    const onReview = vi.fn();

    render(
      <ObjectiveComposer
        projects={[project]}
        selectedProjectId={project.project_id}
        templates={[template]}
        selectedTemplateId={template.template_id}
        objective=""
        plan={[]}
        editingPlan={false}
        pending={false}
        onProjectChange={onProjectChange}
        onTemplateSelect={onTemplateSelect}
        onObjectiveChange={onObjectiveChange}
        onReview={onReview}
        onTogglePlanEditing={vi.fn()}
        onTaskChange={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Project")).toHaveValue(
      project.project_id,
    );
    expect(
      screen.getByRole("button", { name: "Explain repo" }),
    ).toHaveAttribute("aria-pressed", "true");
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: "Explain the failing unit test" },
    });
    expect(onObjectiveChange).toHaveBeenCalledWith(
      "Explain the failing unit test",
    );
    expect(
      screen.getByRole("button", { name: "Review mission" }),
    ).toBeDisabled();

    fireEvent.click(
      screen.getByRole("button", { name: "Explain repo" }),
    );
    expect(onTemplateSelect).toHaveBeenCalledWith(template);
  });

  it("makes the acceptance plan editable before re-review", () => {
    const onTaskChange = vi.fn();
    const onTogglePlanEditing = vi.fn();
    const { rerender } = render(
      <ObjectiveComposer
        projects={[project]}
        selectedProjectId={project.project_id}
        templates={[template]}
        selectedTemplateId={template.template_id}
        objective="Explain the repository"
        plan={[task]}
        editingPlan={false}
        pending={false}
        onProjectChange={vi.fn()}
        onTemplateSelect={vi.fn()}
        onObjectiveChange={vi.fn()}
        onReview={vi.fn()}
        onTogglePlanEditing={onTogglePlanEditing}
        onTaskChange={onTaskChange}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Edit acceptance plan" }),
    );
    expect(onTogglePlanEditing).toHaveBeenCalledOnce();

    rerender(
      <ObjectiveComposer
        projects={[project]}
        selectedProjectId={project.project_id}
        templates={[template]}
        selectedTemplateId={template.template_id}
        objective="Explain the repository"
        plan={[task]}
        editingPlan
        pending={false}
        onProjectChange={vi.fn()}
        onTemplateSelect={vi.fn()}
        onObjectiveChange={vi.fn()}
        onReview={vi.fn()}
        onTogglePlanEditing={onTogglePlanEditing}
        onTaskChange={onTaskChange}
      />,
    );
    fireEvent.change(screen.getByLabelText("Task 1 title"), {
      target: { value: "Map public entry points" },
    });
    fireEvent.change(
      screen.getByLabelText("Task 1 acceptance criteria"),
      {
        target: {
          value: "CLI entry identified\nDesktop entry identified",
        },
      },
    );
    expect(onTaskChange).toHaveBeenCalledWith(
      "map",
      "title",
      "Map public entry points",
    );
    expect(onTaskChange).toHaveBeenCalledWith(
      "map",
      "acceptance",
      "CLI entry identified\nDesktop entry identified",
    );
  });
});
