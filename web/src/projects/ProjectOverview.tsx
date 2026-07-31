import { FolderKanban } from "lucide-react";
import type { ProjectProfile } from "../mission/types";
import {
  EmptyState,
  InlineMeta,
  Notice,
  Panel,
  StatusBadge,
} from "../components";

export function ProjectOverview({
  projects,
  error,
  selectedProjectId,
  onSelectProject,
}: {
  projects: ProjectProfile[];
  error: string | null;
  selectedProjectId: string | null;
  onSelectProject: (projectId: string) => void;
}) {
  return (
    <Panel title="Project profiles" icon={<FolderKanban size={19} />}>
      <section
        aria-label="Project profiles"
        className="projects-overview"
      >
        {error ? (
          <Notice variant="danger" title="Project profiles unavailable">
            {error}. The list below may be stale; the server remains the
            authority.
          </Notice>
        ) : null}
        {projects.length === 0 && !error ? (
          <EmptyState>
            No project profiles yet. Add a project to bind a repository to
            Kestrel's authority model.
          </EmptyState>
        ) : null}
        {projects.length > 0 ? (
          <table className="projects-table">
            <thead>
              <tr>
                <th scope="col">Project</th>
                <th scope="col">Privacy</th>
                <th scope="col">Budget</th>
                <th scope="col">Revision</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((project) => (
                <tr
                  key={project.project_id}
                  aria-label={project.display_name}
                  data-selected={
                    project.project_id === selectedProjectId
                      ? "true"
                      : undefined
                  }
                >
                  <td>
                    <button
                      type="button"
                      className="projects-select"
                      onClick={() => onSelectProject(project.project_id)}
                    >
                      <strong>{project.display_name}</strong>
                    </button>
                    <InlineMeta
                      items={[
                        project.repository_path,
                        `branch ${project.default_branch}`,
                      ]}
                    />
                  </td>
                  <td>{project.privacy_class}</td>
                  <td>
                    {project.cost_budget === null ||
                    project.cost_budget === undefined
                      ? "unbounded"
                      : `$${project.cost_budget.toFixed(2)}`}
                  </td>
                  <td>rev {project.revision}</td>
                  <td>
                    <StatusBadge
                      value={project.archived_at ? "archived" : "active"}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>
    </Panel>
  );
}
