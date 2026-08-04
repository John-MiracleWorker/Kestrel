import { EmptyState, InlineMeta, StatusBadge } from "../components";
import type { RoutineDelivery } from "../types";

export type DeliveryHistoryProps = {
  deliveries: RoutineDelivery[];
  historyLoading: boolean;
  mutationPending: boolean;
  dispatcherEnabled: boolean;
  formatDate: (value: string | null | undefined) => string;
  onReconcile: (delivery: RoutineDelivery, resolution: "retry" | "delivered" | "failed") => void;
};

export function DeliveryHistory({
  deliveries,
  historyLoading,
  mutationPending,
  dispatcherEnabled,
  formatDate,
  onReconcile
}: DeliveryHistoryProps) {
  return (
    <section className="routine-history delivery-history" aria-labelledby="routine-delivery-title">
      <div className="routine-history-head">
        <h3 id="routine-delivery-title">Delivery history</h3>
        <StatusBadge value={`${deliveries.length} records`} />
      </div>
      <p className="muted delivery-truth-note">
        External destinations use idempotent admission plus a connector receipt; retries reuse the
        original key and reconcile uncertain outcomes rather than assuming delivery.
      </p>
      <div className="list compact-list">
        {deliveries.map((delivery) => (
          <article className="data-row" key={delivery.delivery_id}>
            <div className="run-title">
              <strong>{delivery.destination.channel_id} / {delivery.destination.conversation_id}</strong>
              <StatusBadge value={delivery.status} />
            </div>
            <InlineMeta items={[
              `attempt ${delivery.attempt_count}`,
              delivery.idempotency_key,
              formatDate(delivery.delivered_at ?? delivery.updated_at)
            ]} />
            {delivery.error ? <p className="danger-text">{delivery.error}</p> : null}
            {["uncertain", "failed", "blocked"].includes(delivery.status) ? (
              <div className="page-actions">
                <button
                  type="button"
                  disabled={mutationPending || !dispatcherEnabled}
                  onClick={() => onReconcile(delivery, "retry")}
                >
                  Retry with same key
                </button>
                <button
                  type="button"
                  disabled={mutationPending || !dispatcherEnabled}
                  onClick={() => onReconcile(delivery, "delivered")}
                >
                  Mark delivered
                </button>
                <button
                  type="button"
                  disabled={mutationPending || !dispatcherEnabled}
                  onClick={() => onReconcile(delivery, "failed")}
                >
                  Mark failed
                </button>
              </div>
            ) : null}
          </article>
        ))}
        {!historyLoading && deliveries.length === 0 ? (
          <EmptyState>No delivery attempts recorded for this routine.</EmptyState>
        ) : null}
      </div>
    </section>
  );
}
