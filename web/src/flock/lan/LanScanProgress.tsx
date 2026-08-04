import { StatusBadge } from "../../components";
import type {
  LanClientScanStatus,
  LanScanDetail,
} from "./types";
import type { LanScanConnectionStatus } from "./useLanScan";

export function LanScanProgress({
  status,
  scan,
  connection,
  error,
  cancelling,
  onCancel,
}: {
  status: LanClientScanStatus;
  scan: LanScanDetail | null;
  connection: LanScanConnectionStatus;
  error: string | null;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const running = status === "running" || status === "cancelling";
  return (
    <div className="lan-scan-progress">
      <p role="status" className="lan-scan-status">
        <StatusBadge value={status} /> Scan status: {status}
        {connection === "reconnecting" ? " (reconnecting)" : ""}
      </p>
      {scan ? (
        <ul className="lan-scan-facts">
          <li>Network: {scan.network}</li>
          {scan.candidate_count !== null ? (
            <li>Candidate endpoints: {scan.candidate_count}</li>
          ) : null}
          {scan.error_count !== null ? (
            <li>Errors: {scan.error_count}</li>
          ) : null}
          {scan.timeout_count !== null ? (
            <li>Timeouts: {scan.timeout_count}</li>
          ) : null}
          {scan.terminal_reason ? (
            <li>Terminal reason: {scan.terminal_reason.replaceAll("_", " ")}</li>
          ) : null}
        </ul>
      ) : null}
      {error ? <p className="lan-scan-error">Stream notice: {error}</p> : null}
      {running ? (
        <button type="button" onClick={onCancel} disabled={cancelling}>
          Cancel scan
        </button>
      ) : null}
    </div>
  );
}
