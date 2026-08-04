import type { LanScopePreview as LanScopePreviewData } from "./types";

export function LanScopePreview({
  preview,
}: {
  preview: LanScopePreviewData;
}) {
  return (
    <div className="lan-scope-preview" aria-label="Scan scope preview">
      <p className="lan-scope-bounds">
        Up to {preview.active_host_count} hosts ×{" "}
        {preview.limits.known_model_service_ports.length} known model ports
      </p>
      <ul className="lan-scope-facts">
        <li>Network: {preview.network}</li>
        <li>
          Ports probed per host:{" "}
          {preview.limits.known_model_service_ports.join(", ")}
        </li>
        <li>mDNS: {preview.mdns_status.replaceAll("_", " ")}</li>
        <li>
          Scan deadline: {preview.limits.total_scan_deadline_seconds}s with at
          most {preview.limits.max_scan_concurrency} concurrent probes
        </li>
        <li>Preview expires: {preview.expires_at}</li>
      </ul>
      {preview.passive_or_manual_only ? (
        <p className="lan-scope-passive">
          This interface allows passive or manual discovery only; no active
          probes will be sent.
        </p>
      ) : null}
    </div>
  );
}
