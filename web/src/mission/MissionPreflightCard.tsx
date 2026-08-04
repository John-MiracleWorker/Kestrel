import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  GitBranch,
  Play,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import {
  Button,
  Notice,
  StatusPill,
} from "../components";
import type { Run } from "../types";
import type {
  MissionCheckStatus,
  MissionPreflight,
  ProjectProfile,
} from "./types";

export function MissionPreflightCard({
  project,
  preflight,
  activeRun = null,
  error,
  launchPending,
  indexPending,
  editingPlan,
  currentObjective,
  currentTemplateId,
  showLaunchAction = true,
  onStart,
  onRebuildIndex,
}: {
  project: ProjectProfile | null;
  preflight: MissionPreflight | null;
  activeRun?: Run | null;
  error: string | null;
  launchPending: boolean;
  indexPending: boolean;
  editingPlan: boolean;
  currentObjective?: string;
  currentTemplateId?: string;
  showLaunchAction?: boolean;
  onStart: () => void;
  onRebuildIndex: () => void;
}) {
  // P2-1: a durable active mission reloaded without an in-memory
  // preflight gets its own authority snapshot built only from durable
  // run/project evidence — never compose-time "not inspected" copy.
  if (activeRun && !preflight) {
    return (
      <ActiveRunAuthoritySnapshot project={project} run={activeRun} />
    );
  }
  const failures =
    preflight?.checks.filter(
      (check) => check.status === "fail",
    ) ?? [];
  const containmentBlocked =
    failures.some((check) =>
      check.check_id.toLowerCase().includes("containment"),
    ) ||
    Boolean(
      preflight?.blockers.some((blocker) =>
        blocker.toLowerCase().includes("containment"),
      ),
    );
  // The Start button stays enabled only while the accepted projection
  // still matches the current objective, template, and project revision.
  const projectionMatchesInputs = Boolean(
    preflight &&
      project &&
      preflight.project_id === project.project_id &&
      preflight.project_revision === project.revision &&
      (currentObjective === undefined ||
        preflight.objective === currentObjective.trim()) &&
      (currentTemplateId === undefined ||
        preflight.template_id === currentTemplateId),
  );
  const canStart = Boolean(
    preflight?.can_start &&
      projectionMatchesInputs &&
      !editingPlan &&
      !launchPending,
  );

  return (
    <div className="mission-context-content">
      <header className="mission-preflight-head">
        <div>
          <p className="page-eyebrow">Current authority</p>
          <h2>Mission preflight</h2>
        </div>
        {preflight ? (
          <time dateTime={preflight.generated_at}>
            {formatTime(preflight.generated_at)}
          </time>
        ) : null}
      </header>

      {error ? (
        <Notice variant="danger" title="Preflight unavailable">
          {error}
        </Notice>
      ) : null}

      <div className="mission-context-facts">
        <ContextFact
          label="Project"
          value={project?.display_name ?? "No project selected"}
          status={project ? "pass" : "fail"}
        />
        <ContextFact
          label="Git"
          value={
            preflight
              ? `${preflight.branch} · ${preflight.working_tree.summary}`
              : project?.default_branch ?? "Not inspected"
          }
          status={
            !preflight
              ? "unknown"
              : preflight.working_tree.state === "dirty"
                ? "warn"
                : "pass"
          }
          icon={<GitBranch size={15} aria-hidden="true" />}
        />
        <ContextFact
          label="Route"
          value={preflight?.route_policy ?? "Not inspected"}
          status={checkStatus(preflight, "route")}
        />
        <ContextFact
          label="Budget"
          value={budgetLabel(preflight)}
          status={checkStatus(preflight, "budget")}
        />
        <ContextFact
          label="Capability ceiling"
          value={
            preflight?.effective_capabilities.join(", ") ||
            project?.capability_ceiling.join(", ") ||
            "Not inspected"
          }
          status={checkStatus(preflight, "capabilities")}
          icon={<ShieldCheck size={15} aria-hidden="true" />}
        />
        <ContextFact
          label="Index"
          value={preflight?.index.detail ?? "Not inspected"}
          status={indexStatus(preflight)}
        />
        {preflight &&
        preflight.index.freshness !== "current" ? (
          <Button
            variant="quiet"
            size="small"
            pending={indexPending}
            onClick={onRebuildIndex}
          >
            <RefreshCw size={14} aria-hidden="true" />
            Rebuild project index
          </Button>
        ) : null}
        <ContextFact
          label="Provider"
          value={preflight?.provider.detail ?? "Not inspected"}
          status={preflight?.provider.status ?? "unknown"}
        />
        <ContextFact
          label="Validation"
          value={
            preflight?.validation_recipes.join(", ") ||
            "Not inspected"
          }
          status={checkStatus(preflight, "validation")}
        />
        <ContextFact
          label="Rollback"
          value={preflight?.rollback ?? "Not inspected"}
          status={checkStatus(preflight, "rollback")}
        />
      </div>

      {preflight && !preflight.can_start ? (
        <Notice variant="danger" title="Mission is blocked">
          <ul>
            {preflight.blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
          {failures.map((check) =>
            check.recovery ? (
              <p key={check.check_id}>
                <strong>{check.title}:</strong> {check.recovery}
              </p>
            ) : null,
          )}
          {containmentBlocked ? (
            <a href="#/settings/containment">
              Open Containment settings
            </a>
          ) : null}
        </Notice>
      ) : preflight ? (
        <Notice variant="success" title="Ready to start">
          {preflight.warnings[0] ??
            "All required checks have current authoritative evidence."}
        </Notice>
      ) : (
        <Notice variant="info" title="Review required">
          No run can start until Kestrel returns current route,
          permission, budget, validation, and rollback evidence.
        </Notice>
      )}

      {showLaunchAction ? (
        <>
          <Button
            className="mission-launch-button"
            variant="primary"
            pending={launchPending}
            disabled={!canStart}
            onClick={onStart}
          >
            <Play size={16} aria-hidden="true" />
            {editingPlan ? "Finish editing plan" : "Start mission"}
          </Button>
          <p className="mission-launch-note">
            Starting creates a project-bound durable run. High-risk exact
            calls still require owner approval.
          </p>
        </>
      ) : (
        <p className="mission-launch-note">
          This context is read-only while the durable mission is active.
          Exact-call approvals remain scoped to their displayed evidence.
        </p>
      )}
    </div>
  );
}

function ActiveRunAuthoritySnapshot({
  project,
  run,
}: {
  project: ProjectProfile | null;
  run: Run;
}) {
  // Every fact below comes from durable run/project evidence. Where the
  // current Run projection does not persist launch-time authority (the
  // mission launch binding, route/budget/validation/rollback checks),
  // the snapshot states that absence explicitly instead of substituting
  // compose-time preflight language or fabricating authority.
  return (
    <div
      className="mission-context-content"
      aria-label="Active run authority"
    >
      <header className="mission-preflight-head">
        <div>
          <p className="page-eyebrow">Current authority</p>
          <h2>Active run</h2>
        </div>
        <time dateTime={run.updated_at}>
          {formatTime(run.updated_at)}
        </time>
      </header>

      <Notice variant="info" title="Durable mission in progress">
        Launch-time binding not persisted in current projection. This
        snapshot shows only durable run and project evidence recorded
        after launch.
      </Notice>

      <div className="mission-context-facts">
        <ContextFact
          label="Project"
          value={
            project?.display_name ??
            run.project_id ??
            "Project not loaded"
          }
          status={project ? "pass" : "unknown"}
        />
        <ContextFact
          label="Run status"
          value={run.status.replaceAll("_", " ")}
          status={
            run.status === "completed"
              ? "pass"
              : run.status === "failed" || run.status === "blocked"
                ? "fail"
                : "warn"
          }
        />
        <ContextFact
          label="Workspace"
          value={run.workspace}
          status="unknown"
          icon={<GitBranch size={15} aria-hidden="true" />}
        />
        <ContextFact
          label="Provider"
          value={
            run.provider
              ? `${run.provider} · ${run.model}`
              : run.model
          }
          status="unknown"
        />
        <ContextFact
          label="Launch binding"
          value="Launch-time binding not persisted in current projection"
          status="unknown"
          icon={<ShieldCheck size={15} aria-hidden="true" />}
        />
      </div>

      <p className="mission-launch-note">
        This context is read-only while the durable mission is active.
        Exact-call approvals remain scoped to their displayed evidence.
      </p>
    </div>
  );
}

function ContextFact({
  label,
  value,
  status,
  icon,
}: {
  label: string;
  value: string;
  status: MissionCheckStatus;
  icon?: React.ReactNode;
}) {
  return (
    <div className={`mission-context-fact is-${status}`}>
      <div>
        {icon}
        <strong>{label}</strong>
      </div>
      <span>{value}</span>
      <StatusPill state={statusState(status)}>
        {statusLabel(status)}
      </StatusPill>
    </div>
  );
}

function checkStatus(
  preflight: MissionPreflight | null,
  id: string,
): MissionCheckStatus {
  return (
    preflight?.checks.find((check) => check.check_id === id)
      ?.status ?? "unknown"
  );
}

function indexStatus(
  preflight: MissionPreflight | null,
): MissionCheckStatus {
  if (!preflight) return "unknown";
  if (preflight.index.freshness === "current") return "pass";
  if (preflight.index.freshness === "stale") return "warn";
  return "fail";
}

function budgetLabel(
  preflight: MissionPreflight | null,
): string {
  if (!preflight) return "Not inspected";
  const { currency, estimate, limit } = preflight.budget;
  if (
    (estimate === null || estimate === 0) &&
    (limit === null || limit === 0)
  ) {
    return "No external spend";
  }
  const prefix = currency === "USD" ? "$" : `${currency} `;
  if (limit === null) {
    return estimate === null
      ? "No project cap"
      : `${prefix}${estimate.toFixed(2)} estimated`;
  }
  return estimate === null
    ? `${prefix}${limit.toFixed(2)} cap`
    : `${prefix}${estimate.toFixed(2)} estimate · ${prefix}${limit.toFixed(2)} cap`;
}

function statusState(status: MissionCheckStatus) {
  if (status === "pass") return "healthy" as const;
  if (status === "warn") return "caution" as const;
  if (status === "fail") return "blocked" as const;
  return "inactive" as const;
}

function statusLabel(status: MissionCheckStatus): string {
  if (status === "pass") return "Ready";
  if (status === "warn") return "Caution";
  if (status === "fail") return "Blocked";
  return "Unverified";
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}
