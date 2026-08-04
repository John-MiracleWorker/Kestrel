import { ShieldCheck } from "lucide-react";
import type { BehaviorDeltaReport } from "../types";
import {
  Disclosure,
  EmptyState,
  InlineMeta,
  JsonBlock,
  Metric,
  Panel,
  StatusBadge,
} from "../components";

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "0%";
  return `${Math.round(value * 100)}%`;
}

export function BehaviorDeltaWorkspace({
  report,
  error,
}: {
  report: BehaviorDeltaReport | null;
  error: string | null;
}) {
  return (
    <Panel title="Behavior deltas" icon={<ShieldCheck size={19} />}>
      <section aria-label="Behavior deltas" className="memory-deltas">
        <p className="muted">
          Mutation actions require exact-call approval and MutationGate
          review. Activation, outcome, and rollback records below are
          server-ledger reads.
        </p>
        {error ? (
          <p className="danger-text">
            Behavior delta ledger unavailable: {error}
          </p>
        ) : null}
        {report ? (
          <>
            <div className="metric-grid">
              <Metric
                label="Total Deltas"
                value={report.summary.total_deltas}
              />
              <Metric label="Active" value={report.summary.active_deltas} />
              <Metric
                label="Useful Rate"
                value={formatPercent(report.summary.useful_rate)}
              />
              <Metric
                label="Never Activated"
                value={report.summary.never_activated}
              />
            </div>
            <div className="list compact-list">
              {report.deltas.slice(0, 12).map((delta) => (
                <div className="data-row" key={delta.delta_id}>
                  <strong>{delta.title}</strong>
                  <InlineMeta
                    items={[
                      delta.delta_id,
                      `${delta.status} · ${delta.kind} · ${delta.risk}`,
                      `${delta.activation_count} activations`,
                    ]}
                  />
                  <p>
                    {`Useful ${formatPercent(delta.useful_rate)} · Failure ${formatPercent(delta.failure_rate)} · Rollback ${formatPercent(delta.rollback_rate)}`}
                  </p>
                  <StatusBadge value={delta.target_layer} />
                </div>
              ))}
              {report.deltas.length === 0 ? (
                <EmptyState>No behavior deltas recorded.</EmptyState>
              ) : null}
            </div>
            {report.recommendations.length > 0 ? (
              <Disclosure title="Ledger recommendations evidence">
                <JsonBlock
                  value={report.recommendations}
                  maxHeight="200px"
                />
              </Disclosure>
            ) : null}
          </>
        ) : (
          !error && (
            <EmptyState>Behavior delta report is loading.</EmptyState>
          )
        )}
      </section>
    </Panel>
  );
}
