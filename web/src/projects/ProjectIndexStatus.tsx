import { DatabaseZap } from "lucide-react";
import {
  EmptyState,
  Notice,
  Panel,
  StatusBadge,
} from "../components";

export type ProjectIndexStatusRecord = {
  schema: "kestrel.project_index_status.v1";
  project_id: string;
  project_revision: number;
  status: string;
  freshness: string;
  aggregate_digest?: string | null;
  indexed_at?: string | null;
  indexed_files?: number | null;
  git_head?: string | null;
  git_tree?: string | null;
  detail: string;
};

export function ProjectIndexStatus({
  indexStatus,
  error,
}: {
  indexStatus: ProjectIndexStatusRecord | null;
  error: string | null;
}) {
  return (
    <Panel title="Index freshness" icon={<DatabaseZap size={19} />}>
      <section aria-label="Index freshness" className="projects-index">
        {error ? (
          <Notice variant="danger" title="Index status unavailable">
            {error}. Freshness cannot be inferred locally; the server remains
            the authority.
          </Notice>
        ) : null}
        {indexStatus ? (
          <>
            <div className="projects-index-badges">
              <StatusBadge value={indexStatus.status} />
              <StatusBadge value={indexStatus.freshness} />
            </div>
            <p>{indexStatus.detail}</p>
            <dl className="projects-facts">
              <div>
                <dt>Aggregate digest</dt>
                <dd>{indexStatus.aggregate_digest ?? "not recorded"}</dd>
              </div>
              <div>
                <dt>Indexed at</dt>
                <dd>{indexStatus.indexed_at ?? "never indexed"}</dd>
              </div>
            </dl>
          </>
        ) : (
          !error && (
            <EmptyState>
              Select a project to read its server-reported index freshness.
            </EmptyState>
          )
        )}
      </section>
    </Panel>
  );
}
