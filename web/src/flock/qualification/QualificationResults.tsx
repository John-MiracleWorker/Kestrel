/**
 * Terminal qualification receipt view (Adaptive Flock plan, Task 19).
 *
 * A completed run is evidence, not approval: the headline never calls the run
 * "qualified".  Qualification is per scope and each scope card carries its own
 * state and verbatim reasons.
 */

import { StatusBadge } from "../../components";
import { ScopeResultCard } from "./ScopeResultCard";
import type { QualificationReceipt } from "./types";

export function QualificationResults({
  receipt,
}: {
  receipt: QualificationReceipt;
}) {
  const { payload } = receipt;
  const scopes = payload.scopes;
  const qualified = scopes.filter((scope) => scope.state === "qualified");
  const abstained = scopes.filter((scope) => scope.state === "abstained");
  const deterministicOnly = scopes.filter(
    (scope) => scope.state === "deterministic_only",
  );
  const headline =
    payload.status === "completed"
      ? "Evidence collection completed"
      : payload.status === "cancelled"
        ? "Evidence collection cancelled"
        : "Evidence collection failed";

  return (
    <div className="qual-results">
      <div className="qual-results-head">
        <StatusBadge value={payload.status} />
        <p className="qual-result-headline">{headline}</p>
        <p className="muted">
          A finished run is not a qualified run. Qualification is decided per
          scope below; abstained and deterministic-only scopes keep the static
          route.
        </p>
      </div>
      <div className="qual-result-counts">
        <span>
          {qualified.length}{" "}
          {qualified.length === 1 ? "scope" : "scopes"} qualified
        </span>
        {abstained.length > 0 ? (
          <span>
            {abstained.length}{" "}
            {abstained.length === 1 ? "scope" : "scopes"} abstained
          </span>
        ) : null}
        {deterministicOnly.length > 0 ? (
          <span>
            {deterministicOnly.length}{" "}
            {deterministicOnly.length === 1 ? "scope" : "scopes"}{" "}
            deterministic-only
          </span>
        ) : null}
      </div>
      <p className="qual-terminal-reason">
        Terminal reason: <code>{payload.terminal_reason}</code>
      </p>
      <div className="qual-scope-list">
        {scopes.map((scope) => (
          <ScopeResultCard key={scope.scope_digest} result={scope} />
        ))}
      </div>
      <p className="muted">
        Receipt {receipt.receipt_id} · payload digest{" "}
        <code>{receipt.payload_digest.slice(0, 12)}…</code> ·{" "}
        {receipt.created_at}
      </p>
    </div>
  );
}
