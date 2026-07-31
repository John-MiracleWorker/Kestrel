import { Database } from "lucide-react";
import type { MemoryLayerStatus } from "../types";
import {
  Disclosure,
  EmptyState,
  InlineMeta,
  JsonBlock,
  Panel,
  StatusBadge,
} from "../components";

export const POLICY_AUTHORITY_LABEL =
  "Manual or repeated validated evidence required";

const LAYER_ORDER = [
  "policy",
  "self",
  "procedural",
  "semantic",
  "episodic",
  "working",
];

function authorityLabel(layer: MemoryLayerStatus): string {
  if (layer.layer === "policy") {
    return POLICY_AUTHORITY_LABEL;
  }
  if (layer.layer === "self") {
    return "Activation and rollback stay behind review gates";
  }
  return "Advisory recall only; never acts as policy authority";
}

export function MemoryHealth({
  layers,
}: {
  layers: MemoryLayerStatus[];
}) {
  const ordered = [...layers].sort(
    (left, right) =>
      LAYER_ORDER.indexOf(left.layer) - LAYER_ORDER.indexOf(right.layer),
  );
  return (
    <Panel title="Layer health" icon={<Database size={19} />}>
      <section aria-label="Layer health" className="memory-health">
        <p className="muted">
          Health records come from the server's live layer reads. Ordinary
          learning layers stay advisory; policy and self layers carry stronger
          authority gates.
        </p>
        {ordered.length === 0 ? (
          <EmptyState>No memory layers reported by the server.</EmptyState>
        ) : (
          <table className="memory-health-table">
            <thead>
              <tr>
                <th scope="col">Layer</th>
                <th scope="col">Status</th>
                <th scope="col">Backend</th>
                <th scope="col">Authority</th>
              </tr>
            </thead>
            <tbody>
              {ordered.map((layer) => (
                <tr key={layer.layer} aria-label={layer.layer}>
                  <td>
                    <strong>{layer.layer}</strong>
                  </td>
                  <td>
                    <StatusBadge value={layer.ok ? "ok" : "failed"} />
                    <InlineMeta
                      items={[layer.exists ? "present" : "missing"]}
                    />
                  </td>
                  <td>{layer.backend}</td>
                  <td>{authorityLabel(layer)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {ordered.length > 0 ? (
          <Disclosure title="Layer record evidence">
            <JsonBlock value={ordered} maxHeight="260px" />
          </Disclosure>
        ) : null}
      </section>
    </Panel>
  );
}
