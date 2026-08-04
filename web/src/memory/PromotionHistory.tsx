import { GitBranch } from "lucide-react";
import type { LearningDashboard } from "../types";
import {
  Disclosure,
  EmptyState,
  InlineMeta,
  JsonBlock,
  Panel,
} from "../components";
import { POLICY_AUTHORITY_LABEL } from "./MemoryHealth";

export function PromotionHistory({
  dashboard,
  error,
}: {
  dashboard: LearningDashboard | null;
  error: string | null;
}) {
  return (
    <Panel title="Promotion history" icon={<GitBranch size={19} />}>
      <section aria-label="Promotion history" className="memory-promotions">
        <p className="muted">
          Promotions between memory layers require evidence, provenance,
          confidence, and validation. Ordinary learning never promotes itself
          into policy authority.
        </p>
        {error ? (
          <p className="danger-text">
            Promotion history unavailable: {error}
          </p>
        ) : null}
        {dashboard ? (
          <>
            {dashboard.layers.length === 0 ? (
              <EmptyState>
                No promotion activity recorded by the server.
              </EmptyState>
            ) : (
              <div className="list compact-list">
                {dashboard.layers.map((layer) => (
                  <div className="data-row" key={layer.layer}>
                    <strong>{layer.layer}</strong>
                    <InlineMeta
                      items={[
                        `${layer.activations} activations`,
                        `${layer.auto_activations} auto`,
                        `${layer.rollbacks} rollbacks`,
                      ]}
                    />
                    <p>
                      {layer.layer === "policy"
                        ? POLICY_AUTHORITY_LABEL
                        : "Advisory promotions remain recall-scoped."}
                    </p>
                  </div>
                ))}
              </div>
            )}
            <Disclosure title="Promotion ledger evidence">
              <JsonBlock value={dashboard} maxHeight="260px" />
            </Disclosure>
          </>
        ) : (
          !error && (
            <EmptyState>Promotion history is loading.</EmptyState>
          )
        )}
      </section>
    </Panel>
  );
}
