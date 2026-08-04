import { FolderPlus, FolderSearch } from "lucide-react";
import type { ProjectSetupDraft } from "../setup/types";
import {
  Disclosure,
  EmptyState,
  InlineMeta,
  JsonBlock,
  Notice,
  Panel,
} from "../components";

export function ProjectEditor({
  nativePickerAvailable,
  draft,
  inspecting,
  pickerMessage,
  onAddProject,
  onConfirmSave,
  onDismissPreview,
}: {
  nativePickerAvailable: boolean;
  draft: ProjectSetupDraft | null;
  inspecting: boolean;
  pickerMessage: string | null;
  onAddProject: () => void;
  onConfirmSave: () => void;
  onDismissPreview: () => void;
}) {
  return (
    <Panel title="Add project" icon={<FolderPlus size={19} />}>
      <div className="projects-editor">
        <p className="muted">
          New projects are bound through the native folder picker. Kestrel
          inspects the chosen folder on the server and previews the exact
          authority profile before anything is saved.
        </p>
        <button
          type="button"
          onClick={onAddProject}
          disabled={!nativePickerAvailable || inspecting}
        >
          <FolderSearch size={15} aria-hidden="true" /> Add project
        </button>
        {!nativePickerAvailable ? (
          <p className="muted">
            The native project picker requires the Kestrel desktop shell.
          </p>
        ) : null}
        {pickerMessage ? (
          <Notice variant="info">{pickerMessage}</Notice>
        ) : null}
        {draft ? (
          <section
            aria-label="Project authority preview"
            className="projects-authority-preview"
          >
            <h3>Project authority preview</h3>
            <p className="muted">
              Nothing is saved yet. Review the server-computed authority
              profile, then confirm to create the project.
            </p>
            <dl className="projects-facts">
              <div>
                <dt>Repository</dt>
                <dd>{draft.inspection.canonical_path}</dd>
              </div>
              <div>
                <dt>Git</dt>
                <dd>
                  {draft.inspection.git.branch} ·{" "}
                  {draft.inspection.git.state} ·{" "}
                  {draft.inspection.git.summary}
                </dd>
              </div>
              <div>
                <dt>Allowed path ceiling</dt>
                <dd>
                  {draft.create_input.allowed_paths.map((path) => (
                    <span key={path} className="projects-path">
                      {path}
                    </span>
                  ))}
                </dd>
              </div>
              <div>
                <dt>Capability ceiling</dt>
                <dd>
                  {draft.create_input.capability_ceiling.length === 0
                    ? "none"
                    : draft.create_input.capability_ceiling.join(", ")}
                </dd>
              </div>
              <div>
                <dt>Privacy</dt>
                <dd>{draft.create_input.privacy_class}</dd>
              </div>
              <div>
                <dt>Cost budget</dt>
                <dd>
                  {draft.create_input.cost_budget === null
                    ? "unbounded"
                    : `$${draft.create_input.cost_budget.toFixed(2)}`}
                </dd>
              </div>
              <div>
                <dt>Index plan</dt>
                <dd>{draft.inspection.index.detail}</dd>
              </div>
              <div>
                <dt>Recipes</dt>
                <dd>
                  <InlineMeta
                    items={[
                      ...draft.create_input.test_recipes.map(
                        (recipe) => `test: ${recipe.name}`,
                      ),
                      ...draft.create_input.build_recipes.map(
                        (recipe) => `build: ${recipe.name}`,
                      ),
                    ]}
                  />
                </dd>
              </div>
            </dl>
            <div className="page-actions">
              <button type="button" onClick={onConfirmSave}>
                Confirm and save project
              </button>
              <button type="button" onClick={onDismissPreview}>
                Discard preview
              </button>
            </div>
            <Disclosure title="Raw setup draft evidence">
              <JsonBlock value={draft} maxHeight="260px" />
            </Disclosure>
          </section>
        ) : (
          <EmptyState>
            Choose a folder to preview its project authority profile.
          </EmptyState>
        )}
      </div>
    </Panel>
  );
}
