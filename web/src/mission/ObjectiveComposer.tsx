import {
  BookOpen,
  FileCode2,
  FileText,
  ListChecks,
  Pencil,
  SearchCode,
  ShieldCheck,
  TestTube2,
  Wrench,
} from "lucide-react";
import {
  Button,
  EmptyState,
} from "../components";
import type {
  MissionGoalTemplate,
  MissionPlanTask,
  ProjectProfile,
} from "./types";

const templateIcons = {
  explain_repository: BookOpen,
  fix_failing_test: TestTube2,
  implement_feature: FileCode2,
  safe_refactor: Wrench,
  security_review: ShieldCheck,
  documentation: FileText,
} as const;

export function ObjectiveComposer({
  projects,
  selectedProjectId,
  templates,
  selectedTemplateId,
  objective,
  plan,
  editingPlan,
  pending,
  onProjectChange,
  onTemplateSelect,
  onObjectiveChange,
  onReview,
  onTogglePlanEditing,
  onTaskChange,
}: {
  projects: ProjectProfile[];
  selectedProjectId: string;
  templates: MissionGoalTemplate[];
  selectedTemplateId: string;
  objective: string;
  plan: MissionPlanTask[];
  editingPlan: boolean;
  pending: boolean;
  onProjectChange: (projectId: string) => void;
  onTemplateSelect: (template: MissionGoalTemplate) => void;
  onObjectiveChange: (objective: string) => void;
  onReview: () => void;
  onTogglePlanEditing: () => void;
  onTaskChange: (
    taskId: string,
    field: "title" | "acceptance",
    value: string,
  ) => void;
}) {
  const canReview = Boolean(
    selectedProjectId && objective.trim() && !pending,
  );

  return (
    <section
      className="mission-objective-composer"
      aria-labelledby="mission-compose-heading"
    >
      <header className="mission-objective-heading">
        <p className="page-eyebrow">Mission Command</p>
        <h1 id="mission-compose-heading">
          What should Kestrel accomplish?
        </h1>
        <p>
          Shape the goal in plain language. Kestrel will inspect the
          route, permissions, spend, validation, and rollback before
          anything starts.
        </p>
      </header>

      <div className="mission-compose-fields">
        <label className="mission-compose-project">
          <span>Project</span>
          <select
            aria-label="Project"
            value={selectedProjectId}
            disabled={projects.length === 0}
            onChange={(event) =>
              onProjectChange(event.currentTarget.value)
            }
          >
            {projects.length === 0 ? (
              <option value="">No projects registered</option>
            ) : null}
            {projects.map((project) => (
              <option
                key={project.project_id}
                value={project.project_id}
              >
                {project.display_name}
              </option>
            ))}
          </select>
        </label>

        <fieldset className="mission-goal-template">
          <legend>Goal template</legend>
          <div className="mission-template-row">
            {templates.map((template) => {
              const Icon =
                templateIcons[
                  template.template_id as keyof typeof templateIcons
                ] ?? ListChecks;
              const selected =
                selectedTemplateId === template.template_id;
              return (
                <button
                  type="button"
                  key={template.template_id}
                  aria-pressed={selected}
                  className={selected ? "active" : ""}
                  title={template.description}
                  onClick={() => onTemplateSelect(template)}
                >
                  <Icon size={16} aria-hidden="true" />
                  {template.label}
                </button>
              );
            })}
          </div>
        </fieldset>

        <label className="mission-objective-field">
          <span>Objective</span>
          <textarea
            aria-label="Objective"
            rows={5}
            value={objective}
            placeholder="Explain the failing unit test and identify the smallest safe repair"
            onChange={(event) =>
              onObjectiveChange(event.currentTarget.value)
            }
          />
        </label>
        <div className="mission-compose-review">
          <span>
            Review creates a read-only projection. It never queues a
            run.
          </span>
          <Button
            variant="primary"
            pending={pending}
            disabled={!canReview}
            onClick={onReview}
          >
            <ListChecks size={16} aria-hidden="true" />
            Review mission
          </Button>
        </div>
      </div>

      <section
        className="mission-acceptance-plan"
        aria-labelledby="mission-plan-heading"
      >
        <header className="mission-section-rule">
          <div>
            <p className="page-eyebrow">Acceptance plan</p>
            <h2 id="mission-plan-heading">How success will be proved</h2>
          </div>
          {plan.length > 0 ? (
            <Button
              variant="quiet"
              size="small"
              disabled={pending}
              onClick={onTogglePlanEditing}
            >
              <Pencil size={14} aria-hidden="true" />
              {editingPlan
                ? "Finish editing"
                : "Edit acceptance plan"}
            </Button>
          ) : null}
        </header>
        {plan.length === 0 ? (
          <EmptyState
            icon={<SearchCode size={22} />}
            title="Review the mission to draft its acceptance plan"
            headingLevel={3}
          >
            Kestrel will return evidence-oriented tasks, required
            tools, and acceptance criteria for your review.
          </EmptyState>
        ) : (
          <ol className="mission-task-list">
            {plan.map((task, index) => (
              <li key={task.task_id}>
                <span className="mission-task-number">
                  {index + 1}
                </span>
                <div className="mission-task-body">
                  {editingPlan ? (
                    <input
                      aria-label={`Task ${index + 1} title`}
                      value={task.title}
                      onChange={(event) =>
                        onTaskChange(
                          task.task_id,
                          "title",
                          event.currentTarget.value,
                        )
                      }
                    />
                  ) : (
                    <h3>{task.title}</h3>
                  )}
                  <p>
                    <strong>Why:</strong> {task.rationale}
                  </p>
                  {editingPlan ? (
                    <textarea
                      aria-label={`Task ${index + 1} acceptance criteria`}
                      rows={Math.max(
                        2,
                        task.acceptance_criteria.length,
                      )}
                      value={task.acceptance_criteria.join("\n")}
                      onChange={(event) =>
                        onTaskChange(
                          task.task_id,
                          "acceptance",
                          event.currentTarget.value,
                        )
                      }
                    />
                  ) : (
                    <p>
                      <strong>Proof:</strong>{" "}
                      {task.acceptance_criteria.join("; ") ||
                        "Evidence required."}
                    </p>
                  )}
                </div>
                <span
                  className={`mission-risk mission-risk-${task.risk}`}
                >
                  {task.risk || "low"}
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>
    </section>
  );
}
