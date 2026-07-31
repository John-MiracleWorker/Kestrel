import {
  Activity,
  AlertTriangle,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  FileCode2,
  FileText,
  GitBranch,
  ListChecks,
  LoaderCircle,
  Pencil,
  Play,
  RefreshCw,
  SearchCode,
  Settings2,
  ShieldCheck,
  TestTube2,
  Wrench,
  XCircle
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getJson, postJson } from "../api";
import { EngineeringRunPanel } from "../engineering/EngineeringRunPanel";
import { RepairReviewPanel } from "../repair/RepairReviewPanel";
import { activityItemsForEvents, deriveThreadTitle } from "../runActivity";
import type { Approval, Run, TaskGraph, TraceEvent } from "../types";
import type {
  MissionGoalTemplate,
  MissionLaunch,
  MissionPlanTask,
  MissionPreflight,
  MissionPreflightCheck,
  ProjectIndexRebuildResponse,
  ProjectListResponse,
  ProjectProfile
} from "./types";
import "./mission.css";

const GOAL_TEMPLATES: MissionGoalTemplate[] = [
  {
    template_id: "explain_repository",
    label: "Explain repo",
    description: "Map architecture, entry points, and important flows.",
    default_objective: "Explain this repository's architecture, entry points, and important execution flows."
  },
  {
    template_id: "fix_failing_test",
    label: "Fix failing test",
    description: "Reproduce, repair in isolation, and prove the result.",
    default_objective: "Reproduce and fix the failing test without weakening the public contract."
  },
  {
    template_id: "implement_feature",
    label: "Implement feature",
    description: "Plan and deliver a bounded feature with validation.",
    default_objective: "Implement the requested feature with focused tests and evidence."
  },
  {
    template_id: "safe_refactor",
    label: "Refactor safely",
    description: "Characterize behavior before making structural changes.",
    default_objective: "Refactor the selected area while preserving current behavior and compatibility."
  },
  {
    template_id: "security_review",
    label: "Security review",
    description: "Inspect trust boundaries and validate concrete findings.",
    default_objective: "Review this repository for concrete security risks and produce evidence-backed findings."
  },
  {
    template_id: "documentation",
    label: "Documentation",
    description: "Create documentation grounded in repository evidence.",
    default_objective: "Generate accurate documentation for the selected repository area."
  }
];

const TEMPLATE_ICONS = {
  explain_repository: BookOpen,
  fix_failing_test: TestTube2,
  implement_feature: FileCode2,
  safe_refactor: Wrench,
  security_review: ShieldCheck,
  documentation: FileText
} as const;

type MissionControlProps = {
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
  onPrepareTool: (name: string, args: Record<string, unknown>) => void;
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
  onAuthRequired
}: MissionControlProps) {
  const [projects, setProjects] = useState<ProjectProfile[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [selectedTemplateId, setSelectedTemplateId] = useState("fix_failing_test");
  const [objective, setObjective] = useState("");
  const [preflight, setPreflight] = useState<MissionPreflight | null>(null);
  const [plan, setPlan] = useState<MissionPlanTask[]>([]);
  const [editingPlan, setEditingPlan] = useState(false);
  const [loadingProjects, setLoadingProjects] = useState(true);
  const [preflightPending, setPreflightPending] = useState(false);
  const [launchPending, setLaunchPending] = useState(false);
  const [indexRebuildPending, setIndexRebuildPending] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const selectedProject = useMemo(
    () => projects.find((project) => project.project_id === selectedProjectId) ?? null,
    [projects, selectedProjectId]
  );
  const recentMissions = useMemo(() => {
    const filtered = selectedProject
      ? runs.filter((run) => !run.project_id || run.project_id === selectedProject.project_id)
      : runs;
    return [...filtered]
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
      .slice(0, 6);
  }, [runs, selectedProject]);
  const activity = useMemo(() => activityItemsForEvents(events), [events]);

  const loadProjects = useCallback(async (signal?: AbortSignal) => {
    setLoadingProjects(true);
    setLoadError(null);
    try {
      const response = await getJson<ProjectListResponse>("/api/projects", { signal });
      setProjects(response.items);
      setSelectedProjectId((current) => {
        if (response.items.some((project) => project.project_id === current)) return current;
        return response.items[0]?.project_id ?? "";
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
  }, [onAuthRequired]);

  useEffect(() => {
    const controller = new AbortController();
    void loadProjects(controller.signal);
    return () => controller.abort();
  }, [loadProjects]);

  function selectTemplate(template: MissionGoalTemplate) {
    setSelectedTemplateId(template.template_id);
    setObjective((current) => current.trim() ? current : template.default_objective);
    clearProjection();
  }

  function clearProjection() {
    setPreflight(null);
    setPlan([]);
    setEditingPlan(false);
  }

  async function inspectPlan(candidatePlan?: MissionPlanTask[]): Promise<boolean> {
    if (!selectedProject || !objective.trim()) return false;
    setPreflightPending(true);
    setLoadError(null);
    try {
      const request: Record<string, unknown> = {
        objective: objective.trim(),
        template_id: selectedTemplateId
      };
      if (candidatePlan) request.mission_plan = candidatePlan;
      const projection = await postJson<MissionPreflight>(
        `/api/projects/${encodeURIComponent(selectedProject.project_id)}/mission/preflight`,
        request
      );
      setPreflight(projection);
      setPlan(projection.tasks);
      return true;
    } catch (error) {
      if (isAuthError(error)) {
        onAuthRequired();
        return false;
      }
      setLoadError(errorMessage(error));
      return false;
    } finally {
      setPreflightPending(false);
    }
  }

  async function finishEditingPlan() {
    if (preflightPending) return;
    if (await inspectPlan(plan)) setEditingPlan(false);
  }

  async function launchMission() {
    if (!selectedProject || !preflight || !preflight.can_start) return;
    setLaunchPending(true);
    setLoadError(null);
    try {
      await onLaunch({
        objective: objective.trim(),
        project: selectedProject,
        templateId: selectedTemplateId,
        plan,
        preflight
      });
    } catch (error) {
      if (isAuthError(error)) {
        onAuthRequired();
        return;
      }
      setLoadError(errorMessage(error));
    } finally {
      setLaunchPending(false);
    }
  }

  async function rebuildIndex() {
    if (!selectedProject || indexRebuildPending) return;
    setIndexRebuildPending(true);
    setLoadError(null);
    try {
      const response = await postJson<ProjectIndexRebuildResponse>(
        `/api/projects/${encodeURIComponent(selectedProject.project_id)}/index/rebuild`,
        { expected_project_revision: selectedProject.revision }
      );
      setProjects((current) => current.map((project) => (
        project.project_id === response.project.project_id ? response.project : project
      )));
      clearProjection();
      await inspectPlan();
    } catch (error) {
      if (isAuthError(error)) {
        onAuthRequired();
        return;
      }
      setLoadError(errorMessage(error));
    } finally {
      setIndexRebuildPending(false);
    }
  }

  function updateTask(taskId: string, field: "title" | "acceptance", value: string) {
    setPlan((current) => current.map((task) => {
      if (task.task_id !== taskId) return task;
      return field === "title"
        ? { ...task, title: value }
        : {
            ...task,
            acceptance_criteria: value
              .split("\n")
              .map((item) => item.trim())
              .filter(Boolean)
          };
    }));
  }

  const branch = preflight?.branch ?? selectedProject?.default_branch ?? "Not inspected";
  const treeSummary = preflight?.working_tree.summary ?? "Inspect a plan to read Git state.";
  const indexDetail = preflight?.index.detail ?? "Not inspected";
  const providerDetail = preflight?.provider.detail ?? "Not inspected";

  return (
    <div
      className="mission-shell"
      id="mission-workspace-root"
      data-active-section="mission"
    >
      <aside className="mission-project-rail" aria-label="Projects and recent missions">
        <div className="mission-project-picker">
          <label htmlFor="mission-project">Project</label>
          <div className="mission-project-select-row">
            <select
              id="mission-project"
              value={selectedProjectId}
              disabled={loadingProjects || projects.length === 0}
              onChange={(event) => {
                setSelectedProjectId(event.target.value);
                clearProjection();
              }}
            >
              {projects.length === 0 ? <option value="">No projects</option> : null}
              {projects.map((project) => (
                <option key={project.project_id} value={project.project_id}>
                  {project.display_name}
                </option>
              ))}
            </select>
            <button type="button" aria-label="Refresh projects" onClick={() => void loadProjects()}>
              <RefreshCw size={14} />
            </button>
          </div>
        </div>

        <section className="mission-repository-summary" aria-label="Selected repository">
          <h2>Repository</h2>
          <p><GitBranch size={14} /> <strong>{branch}</strong></p>
          <p className={preflight?.working_tree.state === "dirty" ? "mission-warn-text" : ""}>
            <StatusGlyph status={preflight?.working_tree.state === "dirty" ? "warn" : preflight ? "pass" : "unknown"} />
            {treeSummary}
          </p>
          <small title={selectedProject?.repository_path}>{selectedProject?.repository_path ?? "Register a project to begin."}</small>
        </section>

        <section className="mission-recents" aria-label="Recent missions">
          <div className="mission-rail-heading">
            <h2>Recent missions</h2>
            <button type="button" onClick={onOpenHistory}>View all</button>
          </div>
          <div className="mission-recent-list">
            {recentMissions.length === 0 ? (
              <p className="mission-empty-copy">No missions for this project yet.</p>
            ) : recentMissions.map((run) => (
              <button
                type="button"
                key={run.run_id}
                className={run.run_id === activeRun?.run_id ? "active" : ""}
                onClick={() => onOpenRun(run)}
              >
                <strong>{deriveThreadTitle(run.message)}</strong>
                <span>{runStatusLabel(run.status)} · {relativeTime(run.updated_at)}</span>
              </button>
            ))}
          </div>
        </section>

        <nav className="mission-rail-actions" aria-label="Secondary workbench">
          <button type="button" onClick={onOpenAdvanced}><Settings2 size={15} /> Advanced</button>
          <button type="button" onClick={onOpenDiagnostics}><Activity size={15} /> Diagnostics</button>
        </nav>
      </aside>

      <section className="mission-workspace" aria-label="Mission workspace">
        <div className="mission-scroll">
          <header className="mission-objective">
            <h1>What should Kestrel accomplish?</h1>
            <div className="mission-composer">
              <label htmlFor="mission-objective">Engineering objective</label>
              <textarea
                id="mission-objective"
                rows={4}
                value={objective}
                placeholder="Fix the failing authentication test without changing the public API"
                onChange={(event) => {
                  setObjective(event.target.value);
                  clearProjection();
                }}
              />
              <div className="mission-composer-actions">
                <span>{selectedProject ? selectedProject.display_name : "Choose a project"}</span>
                <button
                  type="button"
                  disabled={!selectedProject || !objective.trim() || preflightPending}
                  onClick={() => void inspectPlan()}
                >
                  {preflightPending ? <LoaderCircle className="spin" size={15} /> : <ListChecks size={15} />}
                  Inspect plan
                </button>
              </div>
            </div>
          </header>

          <section className="mission-templates" aria-labelledby="mission-templates-heading">
            <div className="mission-section-rule">
              <h2 id="mission-templates-heading">Goal templates</h2>
            </div>
            <div className="mission-template-row">
              {GOAL_TEMPLATES.map((template) => {
                const Icon = TEMPLATE_ICONS[template.template_id as keyof typeof TEMPLATE_ICONS];
                return (
                  <button
                    type="button"
                    className={selectedTemplateId === template.template_id ? "active" : ""}
                    key={template.template_id}
                    title={template.description}
                    onClick={() => selectTemplate(template)}
                  >
                    <Icon size={16} />
                    {template.label}
                  </button>
                );
              })}
            </div>
          </section>

          <section className="mission-plan" aria-labelledby="mission-plan-heading">
            <div className="mission-section-rule">
              <h2 id="mission-plan-heading">Task plan</h2>
              {plan.length > 0 ? (
                <button
                  type="button"
                  disabled={preflightPending}
                  onClick={() => {
                    if (editingPlan) {
                      void finishEditingPlan();
                    } else {
                      setEditingPlan(true);
                    }
                  }}
                >
                  <Pencil size={13} /> {editingPlan ? "Finish editing" : "Edit plan"}
                </button>
              ) : null}
            </div>
            {plan.length === 0 ? (
              <div className="mission-plan-empty">
                <SearchCode size={22} />
                <p>Inspect the objective to generate an evidence-oriented plan, route, budget, and approval forecast.</p>
              </div>
            ) : (
              <ol className="mission-task-list">
                {plan.map((task, index) => (
                  <li key={task.task_id}>
                    <span className="mission-task-number">{index + 1}</span>
                    <div className="mission-task-body">
                      {editingPlan ? (
                        <input
                          aria-label={`Task ${index + 1} title`}
                          value={task.title}
                          onChange={(event) => updateTask(task.task_id, "title", event.target.value)}
                        />
                      ) : <h3>{task.title}</h3>}
                      <p><strong>Rationale:</strong> {task.rationale}</p>
                      {editingPlan ? (
                        <textarea
                          aria-label={`Task ${index + 1} acceptance criteria`}
                          rows={Math.max(2, task.acceptance_criteria.length)}
                          value={task.acceptance_criteria.join("\n")}
                          onChange={(event) => updateTask(task.task_id, "acceptance", event.target.value)}
                        />
                      ) : (
                        <p><strong>Acceptance:</strong> {task.acceptance_criteria.join("; ") || "Evidence required."}</p>
                      )}
                    </div>
                    <span className={`mission-risk mission-risk-${task.risk}`}>{task.risk || "low"}</span>
                  </li>
                ))}
              </ol>
            )}
          </section>

          <section className="mission-timeline" aria-labelledby="mission-timeline-heading">
            <div className="mission-section-rule">
              <h2 id="mission-timeline-heading">Run timeline</h2>
              {activeRun ? <span className="mission-live-label">{runStatusLabel(activeRun.status)}</span> : null}
            </div>
            <MissionTimeline
              activeRun={activeRun}
              approvals={approvals}
              activity={activity}
              taskGraph={taskGraph}
            />
            <RepairReviewPanel
              tasks={taskGraph?.tasks ?? []}
              allowedPaths={selectedProject?.allowed_paths ?? []}
              onPrepareTool={onPrepareTool}
            />
            <EngineeringRunPanel
              runId={activeRun?.run_id ?? null}
              refreshToken={activeRun?.updated_at ?? ""}
              tasks={taskGraph?.tasks ?? []}
              defaultBranch={selectedProject?.default_branch ?? "main"}
              onPrepareTool={onPrepareTool}
            />
          </section>
        </div>
      </section>

      <aside className="mission-preflight" aria-label="Mission preflight">
        <div className="mission-preflight-head">
          <h2>Preflight</h2>
          {preflight ? <time>{formatTime(preflight.generated_at)}</time> : null}
        </div>

        {loadError ? (
          <div className="mission-load-error" role="alert">
            <XCircle size={17} />
            <div>
              <strong>Preflight unavailable</strong>
              <p>{loadError}</p>
            </div>
          </div>
        ) : null}

        <div className="mission-check-list">
          <PreflightRow title="Project" detail={selectedProject?.display_name ?? "No project selected"} status={selectedProject ? "pass" : "fail"} />
          <PreflightRow title="Branch" detail={branch} status={preflight ? "pass" : "unknown"} />
          <PreflightRow
            title="Working tree"
            detail={treeSummary}
            status={preflight?.working_tree.state === "dirty" ? "warn" : preflight ? "pass" : "unknown"}
          />
          <PreflightRow title="Route" detail={preflight?.route_policy ?? "Not inspected"} status={checkStatus(preflight, "route")} />
          <PreflightRow title="Budget" detail={budgetLabel(preflight)} status={checkStatus(preflight, "budget")} />
          <PreflightRow
            title="Permissions"
            detail={preflight?.effective_capabilities.join(", ") || "Not inspected"}
            status={checkStatus(preflight, "capabilities")}
          />
          <PreflightRow title="Index" detail={indexDetail} status={indexStatus(preflight)} />
          {preflight && preflight.index.freshness !== "current" ? (
            <button
              type="button"
              className="mission-index-action"
              onClick={() => void rebuildIndex()}
              disabled={indexRebuildPending}
            >
              <RefreshCw className={indexRebuildPending ? "spin" : ""} size={13} />
              {indexRebuildPending ? "Rebuilding index…" : "Rebuild project index"}
            </button>
          ) : null}
          <PreflightRow title="Provider" detail={providerDetail} status={preflight?.provider.status ?? "unknown"} />
          <PreflightRow
            title="Validation"
            detail={preflight?.validation_recipes.join(", ") || "Not inspected"}
            status={checkStatus(preflight, "validation")}
          />
          <PreflightRow title="Rollback" detail={preflight?.rollback ?? "Not inspected"} status={checkStatus(preflight, "rollback")} />
        </div>

        {preflight ? (
          <section className={`mission-decision ${preflight.can_start ? "ready" : "blocked"}`}>
            {preflight.can_start ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}
            <div>
              <strong>{preflight.can_start ? "Ready to run" : "Blocked"}</strong>
              <p>
                {preflight.can_start
                  ? preflight.warnings[0] ?? "All required checks have authoritative evidence."
                  : preflight.blockers[0] ?? "Resolve the blocking checks before launch."}
              </p>
            </div>
          </section>
        ) : (
          <section className="mission-decision neutral">
            <Circle size={18} />
            <div>
              <strong>Plan not inspected</strong>
              <p>No run will start until you inspect the route, permissions, validation, and rollback projection.</p>
            </div>
          </section>
        )}

        <button
          type="button"
          className="mission-launch-button"
          disabled={!preflight?.can_start || launchPending || editingPlan}
          onClick={() => void launchMission()}
        >
          {launchPending ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
          {editingPlan ? "Finish editing plan" : "Run mission"}
        </button>
        <p className="mission-launch-note">
          Starting creates a project-bound durable run. High-risk exact calls still require approval.
        </p>
      </aside>
    </div>
  );
}

function MissionTimeline({
  activeRun,
  approvals,
  activity,
  taskGraph
}: {
  activeRun: Run | null;
  approvals: Approval[];
  activity: ReturnType<typeof activityItemsForEvents>;
  taskGraph: TaskGraph | null;
}) {
  if (!activeRun) {
    return (
      <div className="mission-timeline-empty">
        <Circle size={18} />
        <p>The run timeline will keep approvals, tool activity, recovery, validation, and review in one place.</p>
      </div>
    );
  }

  const pending = approvals.filter((approval) => approval.run_id === activeRun.run_id && approval.status === "pending");
  const completedTasks = taskGraph?.tasks.filter((task) => task.status === "completed").length ?? 0;
  const taskCount = taskGraph?.tasks.length ?? 0;
  return (
    <ol className="mission-timeline-list">
      {pending.map((approval) => (
        <li key={approval.approval_id} className="warn">
          <span><AlertTriangle size={15} /></span>
          <div><strong>Approval checkpoint</strong><p>{approval.tool_name} needs an exact-call decision.</p></div>
          <small>Waiting</small>
        </li>
      ))}
      {activity.map((item) => (
        <li key={item.id} className={item.status}>
          <span>{item.status === "failed" ? <XCircle size={15} /> : item.status === "completed" ? <Check size={15} /> : <Activity size={15} />}</span>
          <div><strong>{item.label}</strong><p>{item.detail || item.meta || "Durable event recorded."}</p></div>
          <small>{item.status}</small>
        </li>
      ))}
      <li className={activeRun.status === "completed" ? "completed" : "info"}>
        <span>{activeRun.status === "completed" ? <Check size={15} /> : <Circle size={15} />}</span>
        <div>
          <strong>{activeRun.status === "completed" ? "Final review" : "Proof and review"}</strong>
          <p>{taskCount ? `${completedTasks} of ${taskCount} planned tasks completed.` : "Validation evidence will appear here."}</p>
        </div>
        <small>{runStatusLabel(activeRun.status)}</small>
      </li>
    </ol>
  );
}

function PreflightRow({ title, detail, status }: { title: string; detail: string; status: MissionPreflightCheck["status"] }) {
  return (
    <div className={`mission-check mission-check-${status}`}>
      <StatusGlyph status={status} />
      <div><strong>{title}</strong><span>{detail}</span></div>
      <ChevronRight size={14} />
    </div>
  );
}

function StatusGlyph({ status }: { status: MissionPreflightCheck["status"] }) {
  if (status === "pass") return <CheckCircle2 size={17} aria-label="Ready" />;
  if (status === "warn") return <AlertTriangle size={17} aria-label="Warning" />;
  if (status === "fail") return <XCircle size={17} aria-label="Blocked" />;
  return <Circle size={17} aria-label="Not inspected" />;
}

function checkStatus(preflight: MissionPreflight | null, id: string): MissionPreflightCheck["status"] {
  return preflight?.checks.find((check) => check.check_id === id)?.status ?? (preflight ? "unknown" : "unknown");
}

function indexStatus(preflight: MissionPreflight | null): MissionPreflightCheck["status"] {
  if (!preflight) return "unknown";
  if (preflight.index.freshness === "current") return "pass";
  if (preflight.index.freshness === "stale") return "warn";
  return "fail";
}

function budgetLabel(preflight: MissionPreflight | null): string {
  if (!preflight) return "Not inspected";
  const { currency, limit, estimate } = preflight.budget;
  const prefix = currency === "USD" ? "$" : `${currency} `;
  if (limit === null) return estimate === null ? "No project cap" : `${prefix}${estimate.toFixed(2)} estimated`;
  return estimate === null
    ? `${prefix}${limit.toFixed(2)} cap`
    : `${prefix}${estimate.toFixed(2)} est. · ${prefix}${limit.toFixed(2)} cap`;
}

function runStatusLabel(status: string): string {
  return status.replaceAll("_", " ").replace(/\b\w/g, (value) => value.toUpperCase());
}

function relativeTime(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "time unknown";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} hr ago`;
  return `${Math.floor(seconds / 86_400)} days ago`;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

function isAuthError(value: unknown): boolean {
  return value instanceof Error && value.name === "ApiAuthError";
}
