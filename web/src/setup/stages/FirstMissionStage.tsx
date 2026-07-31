import {
  ArrowRight,
  Bird,
  FolderKanban,
  Sparkles,
} from "lucide-react";
import {
  Button,
  Card,
  Notice,
  StatusPill,
} from "../../components";
import type {
  SetupFirstMissionPreflight,
  SetupSnapshot,
} from "../types";

export function FirstMissionStage({
  snapshot,
  projectSkipped,
  preflight,
  preflightError,
  onOpenMission,
  onOpenSettings,
}: {
  snapshot: SetupSnapshot;
  projectSkipped: boolean;
  preflight: SetupFirstMissionPreflight | null;
  preflightError: string | null;
  onOpenMission: () => void;
  onOpenSettings: () => void;
}) {
  const project = snapshot.projects[0] ?? null;
  const demo = snapshot.readiness.experience_mode === "demo";
  const ready = Boolean(project && preflight?.canStart);

  return (
    <div className="setup-stage setup-first-mission">
      <header className="setup-stage-heading">
        <p className="page-eyebrow">
          {ready ? "Ready to explore" : "Review before launch"}
        </p>
        <h2 tabIndex={-1} data-setup-stage-heading>
          {ready
            ? "Start the first mission"
            : "Review first mission readiness"}
        </h2>
        <p>
          Setup did not start work or grant permissions. This summary comes
          from the current server preflight; Mission Command remains the
          final launch surface.
        </p>
      </header>

      {ready ? (
        <Notice variant="success" title="First mission preflight passed">
          Current repository, route, permissions, and budget evidence admit
          the read-only Explain mission.
        </Notice>
      ) : preflightError ? (
        <Notice variant="danger" title="Preflight could not be verified">
          {preflightError}
        </Notice>
      ) : project && preflight ? (
        <Notice variant="danger" title="First mission is not runnable yet">
          <ul>
            {preflight.blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </Notice>
      ) : project ? (
        <Notice variant="caution" title="Preflight verification is required">
          Return to Safety and verify current server truth before opening the
          first mission.
        </Notice>
      ) : (
        <Notice variant="caution" title="A project is still required">
          Setup review is saved, but Mission Command cannot run an
          engineering mission until a project profile is registered.
        </Notice>
      )}

      <div className="setup-launch-summary">
        <Card
          title="Intelligence"
          icon={<Bird size={18} />}
          headingLevel={3}
          actions={
            <StatusPill state="healthy">
              {demo ? "Demo" : "Connected"}
            </StatusPill>
          }
        >
          <p>
            {demo
              ? "Bundled deterministic responses; no network or API key required."
              : `${snapshot.runtime.provider} / ${snapshot.runtime.model}`}
          </p>
        </Card>
        <Card
          title="Project"
          icon={<FolderKanban size={18} />}
          headingLevel={3}
          actions={
            <StatusPill
              state={
                ready
                  ? "healthy"
                  : project
                    ? "blocked"
                    : "inactive"
              }
            >
              {ready
                ? "Preflight passed"
                : project
                  ? preflight
                    ? "Needs attention"
                    : "Preflight required"
                  : "Required"}
            </StatusPill>
          }
        >
          <p>
            {project
              ? `${project.display_name} · ${project.repository_path}`
              : projectSkipped
                ? "Skipped for now. Register a project before starting an engineering mission."
                : "No project is registered yet."}
          </p>
        </Card>
      </div>

      {preflight?.warnings.length ? (
        <Notice variant="caution" title="Preflight cautions">
          <ul>
            {preflight.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Notice>
      ) : null}

      <footer className="setup-stage-actions">
        <Button variant="secondary" onClick={onOpenSettings}>
          Tune more settings
        </Button>
        <Button variant="primary" onClick={onOpenMission}>
          <Sparkles size={16} aria-hidden="true" />
          {ready ? "Open Mission Command" : "Review in Mission Command"}
          <ArrowRight size={16} aria-hidden="true" />
        </Button>
      </footer>
    </div>
  );
}
