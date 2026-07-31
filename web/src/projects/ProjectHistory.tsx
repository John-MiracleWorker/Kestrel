import { History } from "lucide-react";
import type { ProjectProfile } from "../mission/types";
import { EmptyState, Panel } from "../components";

export function ProjectHistory({
  project,
}: {
  project: ProjectProfile | null;
}) {
  return (
    <Panel title="Project history" icon={<History size={19} />}>
      <section aria-label="Project history" className="projects-history">
        {project ? (
          <dl className="projects-facts">
            <div>
              <dt>Created</dt>
              <dd>{project.created_at}</dd>
            </div>
            <div>
              <dt>Last updated</dt>
              <dd>{project.updated_at}</dd>
            </div>
            <div>
              <dt>Revision</dt>
              <dd>rev {project.revision}</dd>
            </div>
            <div>
              <dt>Baseline index digest</dt>
              <dd>{project.baseline_index_digest ?? "not bound"}</dd>
            </div>
          </dl>
        ) : (
          <EmptyState>
            Select a project to inspect its recorded history.
          </EmptyState>
        )}
        <p className="muted">
          Per-run outcomes and memory coverage for each project live on the
          Mission and Memory surfaces, which read the same server records.
        </p>
      </section>
    </Panel>
  );
}
