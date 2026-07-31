import {
  Activity,
  Circle,
  GitBranch,
  RefreshCw,
  Settings2,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { getJson, postJson } from "../api";
import { useAppShellContextRail } from "../app/AppShell";
import { EngineeringRunPanel } from "../engineering/EngineeringRunPanel";
import { RepairReviewPanel } from "../repair/RepairReviewPanel";
import { deriveThreadTitle } from "../runActivity";
import type {
  Approval,
  Run,
  TaskGraph,
  TraceEvent,
} from "../types";
import { ActiveMission } from "./ActiveMission";
import { MissionPreflightCard } from "./MissionPreflightCard";
import { ObjectiveComposer } from "./ObjectiveComposer";
import type {
  MissionGoalTemplate,
  MissionLaunch,
  MissionPlanTask,
  MissionPreflight,
  MissionState,
  ProjectIndexRebuildResponse,
  ProjectListResponse,
  ProjectProfile,
} from "./types";
import "./mission.css";

const GOAL_TEMPLATES: MissionGoalTemplate[] = [
  {
    template_id: "explain_repository",
    label: "Explain repo",
    description: "Map architecture, entry points, and important flows.",
    default_objective:
      "Explain this repository's architecture, entry points, and important execution flows.",
  },
  {
    template_id: "fix_failing_test",
    label: "Fix failing test",
    description: "Reproduce, repair in isolation, and prove the result.",
    default_objective:
      "Reproduce and fix the failing test without weakening the public contract.",
  },
  {
    template_id: "implement_feature",
    label: "Implement feature",
    description: "Plan and deliver a bounded feature with validation.",
    default_objective:
      "Implement the requested feature with focused tests and evidence.",
  },
  {
    template_id: "safe_refactor",
    label: "Refactor safely",
    description:
      "Characterize behavior before making structural changes.",
    default_objective:
      "Refactor the selected area while preserving current behavior and compatibility.",
  },
  {
    template_id: "security_review",
    label: "Security review",
    description:
      "Inspect trust boundaries and validate concrete findings.",
    default_objective:
      "Review this repository for concrete security risks and produce evidence-backed findings.",
  },
  {
    template_id: "documentation",
    label: "Documentation",
    description:
      "Create documentation grounded in repository evidence.",
    default_objective:
      "Generate accurate documentation for the selected repository area.",
  },
];

export type MissionControlProps = {
  runs: Run[];
  activeRun: Run | null;
  taskGraph: TaskGraph | null;
  approvals: Approval[];
  events: TraceEvent[];
  onLaunch: (mission: MissionLaunch) => Promise<void>;
  onOpenRun: (run: Run) => void;
  onOpenHistory: () => void;
  onOpenAdvanced: () => void;
  onOpenDiagnostics: () => void;
  onPrepareTool: (
    name: string,
    args: Record<string, unknown>,
  ) => void;
  onDecideApproval: (
    approval: Approval,
    approved: boolean,
  ) => void | Promise<void>;
  onContinueConversation: (message: string) => Promise<void>;
  onAuthRequired: () => void;
};

export function MissionControl({
  runs,
  activeRun,
  taskGraph,
  approvals,
  events,
  onLaunch,
  onOpenRun,
  onOpenHistory,
  onOpenAdvanced,
  onOpenDiagnostics,
  onPrepareTool,
  onDecideApproval,
  onContinueConversation,
  onAuthRequired,
}: MissionControlProps) {
  const [projects, setProjects] = useState<ProjectProfile[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [selectedTemplateId, setSelectedTemplateId] =
    useState("fix_failing_test");
  const [objective, setObjective] = useState("");
  const [preflight, setPreflight] =
    useState<MissionPreflight | null>(null);
  const [plan, setPlan] = useState<MissionPlanTask[]>([]);
  const [editingPlan, setEditingPlan] = useState(false);
  const [composeNew, setComposeNew] = useState(!activeRun);
  const [loadingProjects, setLoadingProjects] = useState(true);
  const [preflightPending, setPreflightPending] = useState(false);
  const [launchPending, setLaunchPending] = useState(false);
  const [indexRebuildPending, setIndexRebuildPending] =
    useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // P1-3: monotonic preflight generation. Any bound-input change or new
  // review bumps the generation; only responses from the latest
  // generation may repopulate launch authority, and inputs captured at
  // review time must still match current state when a response lands.
  const preflightGeneration = useRef(0);

  const selectedProject = useMemo(
    () =>
      projects.find(
        (project) => project.project_id === selectedProjectId,
      ) ?? null,
    [projects, selectedProjectId],
  );
  const displayedRun = composeNew ? null : activeRun;
  const recentMissions = useMemo(() => {
    const filtered = selectedProject
      ? runs.filter(
          (run) =>
            !run.project_id ||
            run.project_id === selectedProject.project_id,
        )
      : runs;
    return [...filtered]
      .sort((left, right) =>
        right.updated_at.localeCompare(left.updated_at),
      )
      .slice(0, 6);
  }, [runs, selectedProject]);

  const clearProjection = useCallback(() => {
    preflightGeneration.current += 1;
    setPreflight(null);
    setPlan([]);
    setEditingPlan(false);
  }, []);

  const loadProjects = useCallback(
    async (signal?: AbortSignal) => {
      setLoadingProjects(true);
      setLoadError(null);
      try {
        const response = await getJson<ProjectListResponse>(
          "/api/projects",
          { signal },
        );
        setProjects(response.items);
        setSelectedProjectId((current) => {
          if (
            response.items.some(
              (project) => project.project_id === current,
            )
          ) {
            return current;
          }
          const activeProject = response.items.find(
            (project) =>
              project.project_id === activeRun?.project_id,
          );
          return (
            activeProject?.project_id ??
            response.items[0]?.project_id ??
            ""
          );
        });
      } catch (error) {
        if (signal?.aborted) return;
        if (isAuthError(error)) {
          onAuthRequired();
          return;
        }
        setLoadError(errorMessage(error));
      } finally {
        if (!signal?.aborted) setLoadingProjects(false);
      }
    },
    [activeRun?.project_id, onAuthRequired],
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadProjects(controller.signal);
    return () => controller.abort();
  }, [loadProjects]);

  const inspectPlan = useCallback(
    async (
      candidatePlan?: MissionPlanTask[],
      projectOverride?: ProjectProfile,
    ): Promise<boolean> => {
      const requestProject = projectOverride ?? selectedProject;
      if (!requestProject || !objective.trim()) return false;
      // Invalidate any prior launch authority immediately; only this
      // generation's response may repopulate launch authority, and
      // inputs captured at review time must still match current state
      // when a response lands.
      const generation = ++preflightGeneration.current;
      setPreflight(null);
      const requestObjective = objective.trim();
      const requestTemplateId = selectedTemplateId;
      setPreflightPending(true);
      setLoadError(null);
      try {
        const request: Record<string, unknown> = {
          objective: requestObjective,
          template_id: requestTemplateId,
        };
        if (candidatePlan) request.mission_plan = candidatePlan;
        const projection = await postJson<MissionPreflight>(
          `/api/projects/${encodeURIComponent(requestProject.project_id)}/mission/preflight`,
          request,
        );
        // Only the latest review may repopulate launch authority; and a
        // projection is accepted only when the mission inputs captured
        // at review time (objective/template/project) are still the
        // current inputs — an edit made while the request was in flight
        // invalidates even an on-time response.
        if (
          generation !== preflightGeneration.current ||
          (projectOverride === undefined &&
            (requestProject !== selectedProject ||
              requestObjective !== objective.trim() ||
              requestTemplateId !== selectedTemplateId))
        ) {
          return false;
        }
        if (
          projection.project_id !== requestProject.project_id ||
          projection.project_revision !== requestProject.revision ||
          projection.objective !== requestObjective ||
          projection.template_id !== requestTemplateId
        ) {
          setLoadError(
            "Reviewed projection no longer matches the current objective, template, or project revision. Review the mission again.",
          );
          return false;
        }
        setPreflight(projection);
        setPlan(projection.tasks);
        return true;
      } catch (error) {
        if (generation !== preflightGeneration.current) {
          return false;
        }
        if (isAuthError(error)) {
          onAuthRequired();
          return false;
        }
        setLoadError(errorMessage(error));
        return false;
      } finally {
        // Clear pending even when this generation was superseded; the
        // newer request re-set it and its own finally will clear again.
        // Holding it here would wedge the composer after a stale
        // response is rejected.
        setPreflightPending(false);
      }
    },
    [
      objective,
      onAuthRequired,
      selectedProject,
      selectedTemplateId,
    ],
  );

  const launchMission = useCallback(async () => {
    if (!selectedProject || !preflight || !preflight.can_start) {
      return;
    }
    // Re-verify the accepted projection still matches the current
    // inputs; never launch against stale authority.
    if (
      preflight.project_id !== selectedProject.project_id ||
      preflight.project_revision !== selectedProject.revision ||
      preflight.objective !== objective.trim() ||
      preflight.template_id !== selectedTemplateId ||
      plan.length === 0
    ) {
      setPreflight(null);
      setPlan([]);
      setEditingPlan(false);
      setLoadError(
        "Mission inputs changed after review. Review the mission again before starting.",
      );
      return;
    }
    setLaunchPending(true);
    setLoadError(null);
    try {
      await onLaunch({
        objective: objective.trim(),
        project: selectedProject,
        templateId: selectedTemplateId,
        plan,
        preflight,
      });
      setComposeNew(false);
    } catch (error) {
      if (isAuthError(error)) {
        onAuthRequired();
        return;
      }
      setLoadError(errorMessage(error));
    } finally {
      setLaunchPending(false);
    }
  }, [
    objective,
    onAuthRequired,
    onLaunch,
    plan,
    preflight,
    selectedProject,
    selectedTemplateId,
  ]);

  const rebuildIndex = useCallback(async () => {
    if (!selectedProject || indexRebuildPending) return;
    setIndexRebuildPending(true);
    setLoadError(null);
    try {
      const response =
        await postJson<ProjectIndexRebuildResponse>(
          `/api/projects/${encodeURIComponent(selectedProject.project_id)}/index/rebuild`,
          {
            expected_project_revision: selectedProject.revision,
          },
        );
      setProjects((current) =>
        current.map((project) =>
          project.project_id === response.project.project_id
            ? response.project
            : project,
        ),
      );
      setPreflight(null);
      setLoadError(null);
      // Re-inspect against the durable project returned by the
      // rebuild; the accepted projection must bind to the rebuilt
      // revision, never to the stale pre-rebuild profile.
      await inspectPlan(undefined, response.project);
    } catch (error) {
      if (isAuthError(error)) {
        onAuthRequired();
        return;
      }
      setLoadError(errorMessage(error));
    } finally {
      setIndexRebuildPending(false);
    }
  }, [
    indexRebuildPending,
    inspectPlan,
    onAuthRequired,
    selectedProject,
  ]);

  const preflightContext = (
    <MissionPreflightCard
      project={selectedProject}
      preflight={preflight}
      activeRun={displayedRun}
      error={loadError}
      launchPending={launchPending}
      indexPending={indexRebuildPending}
      editingPlan={editingPlan}
      currentObjective={objective}
      currentTemplateId={selectedTemplateId}
      showLaunchAction={!displayedRun}
      onStart={() => void launchMission()}
      onRebuildIndex={() => void rebuildIndex()}
    />
  );
  const contextRegistration =
    useAppShellContextRail(preflightContext);
  const missionState = displayedRun
    ? deriveMissionState(displayedRun, approvals, taskGraph)
    : preflight
      ? "preflight"
      : "compose";

  function selectTemplate(template: MissionGoalTemplate) {
    setSelectedTemplateId(template.template_id);
    setObjective((current) =>
      current.trim() ? current : template.default_objective,
    );
    clearProjection();
  }

  async function togglePlanEditing() {
    if (!editingPlan) {
      setEditingPlan(true);
      return;
    }
    if (preflightPending) return;
    if (await inspectPlan(plan)) setEditingPlan(false);
  }

  function updateTask(
    taskId: string,
    field: "title" | "acceptance",
    value: string,
  ) {
    setPlan((current) =>
      current.map((task) => {
        if (task.task_id !== taskId) return task;
        return field === "title"
          ? { ...task, title: value }
          : {
              ...task,
              acceptance_criteria: value
                .split("\n")
                .map((item) => item.trim())
                .filter(Boolean),
            };
      }),
    );
  }

  function beginNewMission() {
    setComposeNew(true);
    setObjective("");
    clearProjection();
  }

  return (
    <div
      className={`mission-shell ${
        contextRegistration.hosted ? "is-context-hosted" : ""
      }`}
      id="mission-workspace-root"
      data-active-section="mission"
      data-mission-state={missionState}
    >
      <aside
        className="mission-project-rail"
        aria-label="Projects and recent missions"
      >
        <section
          className="mission-repository-summary"
          aria-label="Selected repository"
        >
          <div className="mission-rail-heading">
            <h2>Repository</h2>
            <button
              type="button"
              aria-label="Refresh projects"
              disabled={loadingProjects}
              onClick={() => void loadProjects()}
            >
              <RefreshCw
                className={loadingProjects ? "spin" : ""}
                size={14}
              />
            </button>
          </div>
          <p>
            <GitBranch size={14} aria-hidden="true" />
            <strong>
              {preflight?.branch ??
                selectedProject?.default_branch ??
                "Not inspected"}
            </strong>
          </p>
          <small title={selectedProject?.repository_path}>
            {selectedProject?.repository_path ??
              "Register a project to begin."}
          </small>
        </section>

        <section
          className="mission-recents"
          aria-label="Recent missions"
        >
          <div className="mission-rail-heading">
            <h2>Recent missions</h2>
            <button type="button" onClick={onOpenHistory}>
              View all
            </button>
          </div>
          <div className="mission-recent-list">
            {recentMissions.length === 0 ? (
              <p className="mission-empty-copy">
                No missions for this project yet.
              </p>
            ) : (
              recentMissions.map((run) => (
                <button
                  type="button"
                  key={run.run_id}
                  className={
                    run.run_id === displayedRun?.run_id
                      ? "active"
                      : ""
                  }
                  onClick={() => {
                    setComposeNew(false);
                    onOpenRun(run);
                  }}
                >
                  <strong>{deriveThreadTitle(run.message)}</strong>
                  <span>
                    {runStatusLabel(run.status)} ·{" "}
                    {relativeTime(run.updated_at)}
                  </span>
                </button>
              ))
            )}
          </div>
        </section>

        <nav
          className="mission-rail-actions"
          aria-label="Secondary workbench"
        >
          <button type="button" onClick={onOpenAdvanced}>
            <Settings2 size={15} aria-hidden="true" /> Advanced
          </button>
          <button type="button" onClick={onOpenDiagnostics}>
            <Activity size={15} aria-hidden="true" /> Diagnostics
          </button>
        </nav>
      </aside>

      <section
        className="mission-workspace"
        aria-label="Mission workspace"
      >
        <div className="mission-scroll">
          {displayedRun ? (
            <ActiveMission
              missionState={missionState}
              run={displayedRun}
              taskGraph={taskGraph}
              approvals={approvals}
              events={events}
              onDecision={onDecideApproval}
              onContinue={onContinueConversation}
              onNewMission={beginNewMission}
              onOpenHistory={onOpenHistory}
              onAuthRequired={onAuthRequired}
            >
              <RepairReviewPanel
                tasks={taskGraph?.tasks ?? []}
                allowedPaths={selectedProject?.allowed_paths ?? []}
                onPrepareTool={onPrepareTool}
              />
              <EngineeringRunPanel
                runId={displayedRun.run_id}
                refreshToken={displayedRun.updated_at}
                tasks={taskGraph?.tasks ?? []}
                defaultBranch={
                  selectedProject?.default_branch ?? "main"
                }
                onPrepareTool={onPrepareTool}
              />
            </ActiveMission>
          ) : (
            <ObjectiveComposer
              projects={projects}
              selectedProjectId={selectedProjectId}
              templates={GOAL_TEMPLATES}
              selectedTemplateId={selectedTemplateId}
              objective={objective}
              plan={plan}
              editingPlan={editingPlan}
              pending={preflightPending}
              onProjectChange={(projectId) => {
                setSelectedProjectId(projectId);
                clearProjection();
              }}
              onTemplateSelect={selectTemplate}
              onObjectiveChange={(value) => {
                setObjective(value);
                clearProjection();
              }}
              onReview={() => void inspectPlan()}
              onTogglePlanEditing={() => void togglePlanEditing()}
              onTaskChange={updateTask}
            />
          )}
        </div>
      </section>

      {!contextRegistration.hosted ? (
        <aside
          className="mission-preflight"
          aria-label="Mission preflight"
        >
          {preflightContext}
        </aside>
      ) : null}
      {contextRegistration.portal}
    </div>
  );
}

function deriveMissionState(
  run: Run,
  approvals: Approval[],
  taskGraph: TaskGraph | null,
): MissionState {
  const needsOwner = approvals.some(
    (approval) =>
      approval.run_id === run.run_id &&
      approval.status === "pending",
  );
  if (needsOwner) return "needs-owner";
  if (run.status === "completed") return "completed";
  if (
    ["blocked", "failed", "cancelled", "canceled"].includes(
      run.status,
    )
  ) {
    return "blocked";
  }
  const reviewing =
    run.status === "reviewing" ||
    taskGraph?.tasks.some((task) =>
      ["reviewing", "awaiting_review"].includes(task.status),
    );
  return reviewing ? "reviewing" : "active";
}

function runStatusLabel(status: string): string {
  return status
    .replaceAll("_", " ")
    .replace(/\b\w/g, (value) => value.toUpperCase());
}

function relativeTime(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "time unknown";
  const seconds = Math.max(
    0,
    Math.floor((Date.now() - timestamp) / 1000),
  );
  if (seconds < 60) return "just now";
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)} min ago`;
  }
  if (seconds < 86_400) {
    return `${Math.floor(seconds / 3600)} hr ago`;
  }
  return `${Math.floor(seconds / 86_400)} days ago`;
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

function isAuthError(value: unknown): boolean {
  return value instanceof Error && value.name === "ApiAuthError";
}
