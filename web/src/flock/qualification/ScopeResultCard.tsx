/**
 * Per-scope qualification result card (Adaptive Flock plan, Task 19).
 *
 * The scope ``state`` (qualified / abstained / deterministic-only) is the
 * only qualification signal — never the run status.  Reason codes pass
 * through verbatim.  Raw attempt/provider evidence is linked by digest under
 * Evidence / Advanced with secrets redacted.
 */

import { JsonBlock, StatusBadge } from "../../components";
import type { ScopeQualificationResult } from "./types";

const SECRET_KEY =
  /secret|token|api[-_]?key|authorization|password|credential/i;

export function ScopeResultCard({
  result,
}: {
  result: ScopeQualificationResult;
}) {
  const stateLabel =
    result.state === "deterministic_only"
      ? "deterministic-only"
      : result.state;
  const replayRuns = numericFact(result.router_state, "replay_runs");
  const replaySuccesses = numericFact(result.router_state, "replay_successes");
  return (
    <article className="qual-scope-card">
      <header className="qual-scope-head">
        <code>{shortDigest(result.scope_digest)}</code>
        <StatusBadge value={stateLabel} />
      </header>
      <ul className="qual-scope-facts">
        <li>
          Support: {result.selected_target_support} of {result.total_support}
        </li>
        <li>Confidence: {result.confidence}</li>
        <li>
          Utility: static {nullableNumber(result.static_utility)} / learned{" "}
          {nullableNumber(result.learned_utility)} (delta {result.utility_delta})
        </li>
        <li>Cost coverage: {result.cost_coverage}</li>
        <li>
          Estimated savings: {nullableMoney(result.estimated_savings_usd)} /
          regret: {nullableMoney(result.estimated_regret_usd)}
        </li>
        <li>Guardrail violations: {result.guardrail_violations}</li>
        {replayRuns !== null && replaySuccesses !== null ? (
          <li>
            Replay: {replaySuccesses}/{replayRuns} successes
          </li>
        ) : null}
        <li>Static target: {result.static_target_id}</li>
        <li>
          Selected target: {result.selected_target_id ?? "none — static route kept"}
        </li>
      </ul>
      {result.reasons.length > 0 ? (
        <div className="qual-scope-reasons">
          <h4>Reasons</h4>
          <ul>
            {result.reasons.map((reason) => (
              <li key={reason}>
                <code>{reason}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <details className="qual-evidence">
        <summary>Evidence / Advanced</summary>
        <p className="muted">
          Raw attempt and provider receipts are stored server-side and linked
          here by digest. Secrets are redacted from this view.
        </p>
        <JsonBlock
          value={redact({
            scope_digest: result.scope_digest,
            thresholds_digest: result.thresholds_digest,
            static_target_id: result.static_target_id,
            selected_target_id: result.selected_target_id,
            evaluated_target_ids: result.evaluated_target_ids,
            router_state: result.router_state,
          })}
        />
      </details>
    </article>
  );
}

function shortDigest(digest: string): string {
  return digest.length > 16 ? `${digest.slice(0, 12)}…` : digest;
}

function nullableNumber(value: number | null): string {
  return value === null ? "not measured" : String(value);
}

function nullableMoney(value: number | null): string {
  return value === null ? "not estimated" : `$${value.toFixed(2)}`;
}

function numericFact(
  state: Readonly<Record<string, unknown>>,
  key: string,
): number | null {
  const value = state[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redact);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [
        key,
        SECRET_KEY.test(key) ? "[redacted]" : redact(child),
      ]),
    );
  }
  return value;
}
