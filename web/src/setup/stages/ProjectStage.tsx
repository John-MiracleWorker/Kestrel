import {
  Archive,
  FolderOpen,
  GitBranch,
  ScanSearch,
  ShieldCheck,
} from "lucide-react";
import {
  Button,
  Card,
  Field,
  Notice,
  StatusPill,
} from "../../components";
import type { ProjectProfile } from "../../mission/types";
import type {
  ProjectCreateInput,
  ProjectSetupDraft,
  SetupFolderChoice,
} from "../types";

export function ProjectStage({
  projects,
  supportsNativePicker,
  folder,
  displayName,
  budget,
  estimatedCost,
  draft,
  pending,
  error,
  onChooseFolder,
  onFolderChange,
  onDisplayNameChange,
  onBudgetChange,
  onEstimatedCostChange,
  onInspect,
  onSave,
  onContinueExisting,
  onOpenCapabilities,
  onSkip,
}: {
  projects: ProjectProfile[];
  supportsNativePicker: boolean;
  folder: SetupFolderChoice;
  displayName: string;
  budget: string;
  estimatedCost: string;
  draft: ProjectSetupDraft | null;
  pending: boolean;
  error: string | null;
  onChooseFolder: () => void;
  onFolderChange: (path: string) => void;
  onDisplayNameChange: (name: string) => void;
  onBudgetChange: (budget: string) => void;
  onEstimatedCostChange: (cost: string) => void;
  onInspect: () => void;
  onSave: (input: ProjectCreateInput) => void;
  onContinueExisting: () => void;
  onOpenCapabilities: () => void;
  onSkip: () => void;
}) {
  const selectedPath =
    folder.status === "selected" ? folder.path : "";
  const budgetValid = optionalNonNegativeNumber(budget);
  const estimateValid = optionalNonNegativeNumber(estimatedCost);
  const draftMatchesPath =
    draft?.inspection.canonical_path === selectedPath;
  const canInspect = Boolean(
    selectedPath && budgetValid && estimateValid && !pending,
  );
  const canSave = Boolean(
    draft &&
      draftMatchesPath &&
      draft.first_mission.can_start &&
      displayName.trim() &&
      !pending,
  );

  return (
    <div className="setup-stage">
      <header className="setup-stage-heading">
        <p className="page-eyebrow">Project boundary</p>
        <h2 tabIndex={-1} data-setup-stage-heading>
          Add a project
        </h2>
        <p>
          Choose the repository Kestrel may reason about. The server
          inspects Git and manifests without changing files, then returns
          the exact authority profile for review.
        </p>
      </header>

      {error ? (
        <Notice variant="danger" title="Project setup needs attention">
          {error}
        </Notice>
      ) : null}

      {projects.length ? (
        <Notice
          variant="success"
          title={`${projects.length} project${
            projects.length === 1 ? "" : "s"
          } already registered`}
          actions={
            <Button
              variant="secondary"
              size="small"
              onClick={onContinueExisting}
            >
              Inspect existing project
            </Button>
          }
        >
          {projects.map((project) => project.display_name).join(", ")}
        </Notice>
      ) : null}

      <Card
        title="Repository folder"
        icon={<FolderOpen size={18} />}
        headingLevel={3}
      >
        {supportsNativePicker ? (
          <Button variant="primary" onClick={onChooseFolder}>
            <FolderOpen size={16} aria-hidden="true" />
            Choose project folder
          </Button>
        ) : (
          <Field
            label="Project folder"
            hint="Enter the canonical absolute path exposed to the local Kestrel server."
          >
            <input
              type="text"
              value={selectedPath}
              onChange={(event) =>
                onFolderChange(event.currentTarget.value)
              }
              placeholder="/Users/you/project"
            />
          </Field>
        )}
        {selectedPath ? (
          <code className="setup-selected-path">{selectedPath}</code>
        ) : (
          <p className="setup-muted">
            No folder selected. You can also continue without a project.
          </p>
        )}
        <div className="setup-project-fields">
          <Field label="Project name">
            <input
              value={displayName}
              onChange={(event) =>
                onDisplayNameChange(event.currentTarget.value)
              }
            />
          </Field>
          <Field
            label="External spend limit"
            hint="Required for an external provider. The first Explain mission estimates three calls."
          >
            <input
              type="number"
              min="0"
              step="0.01"
              value={budget}
              onChange={(event) =>
                onBudgetChange(event.currentTarget.value)
              }
            />
          </Field>
          <Field
            label="Estimated cost per provider call"
            hint="Enter the provider's current estimate. Local and Demo routes are server-classified at $0."
          >
            <input
              type="number"
              min="0"
              step="0.0001"
              value={estimatedCost}
              onChange={(event) =>
                onEstimatedCostChange(event.currentTarget.value)
              }
            />
          </Field>
        </div>
        <Button
          variant="secondary"
          pending={pending}
          disabled={!canInspect}
          onClick={onInspect}
        >
          <ScanSearch size={16} aria-hidden="true" />
          Inspect project authority
        </Button>
      </Card>

      {selectedPath && !draft ? (
        <Notice variant="caution" title="Inspection required">
          Inspect this folder before saving. Setup will not guess its branch,
          recipes, provider boundary, budget, or capability ceiling.
        </Notice>
      ) : null}

      {draft && draftMatchesPath ? (
        <AuthorityPreview
          draft={draft}
          onOpenCapabilities={onOpenCapabilities}
        />
      ) : null}

      <footer className="setup-stage-actions">
        <Button variant="quiet" onClick={onSkip}>
          Do this later
        </Button>
        <Button
          variant="primary"
          pending={pending}
          disabled={!canSave}
          onClick={() => {
            if (!draft) return;
            onSave({
              ...draft.create_input,
              display_name: displayName.trim(),
            });
          }}
        >
          Save reviewed project
        </Button>
      </footer>
    </div>
  );
}

function AuthorityPreview({
  draft,
  onOpenCapabilities,
}: {
  draft: ProjectSetupDraft;
  onOpenCapabilities: () => void;
}) {
  const input = draft.create_input;
  const policy =
    input.privacy_class === "approved_cloud"
      ? "External provider explicitly approved"
      : "Local models required";
  const recipes = [
    ...draft.inspection.test_recipes,
    ...draft.inspection.build_recipes,
  ];

  return (
    <section
      className="setup-authority-preview"
      aria-labelledby="authority-preview-title"
    >
      <div className="setup-subhead">
        <div>
          <h3 id="authority-preview-title">Authority preview</h3>
          <p>Server-inspected evidence and the exact unsaved ceiling.</p>
        </div>
        <StatusPill
          state={
            draft.first_mission.can_start ? "healthy" : "blocked"
          }
        >
          {draft.first_mission.can_start
            ? "Mission eligible"
            : "Blocked"}
        </StatusPill>
      </div>

      {draft.first_mission.blockers.length ? (
        <Notice
          variant="danger"
          title="First mission would be rejected"
          actions={
            draft.first_mission.missing_tools.length ? (
              <Button
                variant="secondary"
                size="small"
                onClick={onOpenCapabilities}
              >
                Review capabilities
              </Button>
            ) : undefined
          }
        >
          <ul>
            {draft.first_mission.blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </Notice>
      ) : (
        <Notice variant="success" title="First mission policy is admissible">
          Saving this exact draft is projected to admit the read-only Explain
          mission. Setup still runs the real preflight after registration.
        </Notice>
      )}

      <dl>
        <div>
          <dt>Allowed path ceiling</dt>
          <dd>{input.allowed_paths.join(", ")}</dd>
        </div>
        <div>
          <dt>Git state</dt>
          <dd>
            <GitBranch size={14} aria-hidden="true" />
            {draft.inspection.git.branch} ·{" "}
            {draft.inspection.git.state} ·{" "}
            {draft.inspection.git.summary}
          </dd>
        </div>
        <div>
          <dt>Repository index</dt>
          <dd>{draft.inspection.index.detail}</dd>
        </div>
        <div>
          <dt>Validation recipes</dt>
          <dd>
            {recipes.length
              ? recipes.map((recipe) => (
                  <code key={`${recipe.name}:${recipe.command}`}>
                    {recipe.command}
                  </code>
                ))
              : "No manifest-backed commands discovered"}
          </dd>
        </div>
        <div>
          <dt>Budget</dt>
          <dd>
            {input.cost_budget === null
              ? "No reviewed spend limit"
              : `$${input.cost_budget.toFixed(2)} external spend limit`}
          </dd>
        </div>
        <div>
          <dt>Capability ceiling</dt>
          <dd>
            <ShieldCheck size={14} aria-hidden="true" />
            {input.capability_ceiling.length} currently active,
            first-mission capabilities
            {draft.first_mission.missing_tools.length
              ? ` · missing ${draft.first_mission.missing_tools.join(", ")}`
              : ""}
          </dd>
        </div>
        <div>
          <dt>Provider policy</dt>
          <dd>{policy}</dd>
        </div>
        <div>
          <dt>Rollback</dt>
          <dd>
            <Archive size={14} aria-hidden="true" />
            Registration can be archived; project files remain untouched
          </dd>
        </div>
      </dl>
    </section>
  );
}

function optionalNonNegativeNumber(value: string): boolean {
  if (!value.trim()) return true;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0;
}
