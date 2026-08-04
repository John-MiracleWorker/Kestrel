/**
 * Grant card (Adaptive Flock plan, Task 20).
 *
 * One card per activation grant: exact scope, receipt, binding health, and
 * the server-side evaluation (the only effectiveness signal — never
 * ``status === "active"`` alone).  Reason codes pass through verbatim.
 * Revocation is owner-confirmed and warns that new route leases lose the
 * grant immediately while an in-flight attempt keeps its existing route
 * lease.  Suspended/revoked grants offer Requalify only — there is no
 * reactivate control.
 */

import { useCallback, useEffect, useState } from "react";
import { InlineMeta, StatusBadge } from "../../components";
import { evaluateActivation, revokeActivation } from "./api";
import type {
  ActivationGrant,
  ActivationTransition,
  GrantEvaluation,
  RevokeActivationInput,
} from "./types";

export type GrantCardClient = Readonly<{
  evaluate: (grantId: string) => Promise<GrantEvaluation>;
  revoke: (input: RevokeActivationInput) => Promise<ActivationTransition>;
}>;

const defaultClient: GrantCardClient = {
  evaluate: evaluateActivation,
  revoke: revokeActivation,
};

const BINDING_LABELS: Readonly<Record<string, string>> = {
  target_inventory: "Target inventory",
  price: "Price snapshot",
  policy: "Routing policy",
  learned: "Learned configuration",
  project_authority: "Project authority",
};

export function GrantCard({
  grant,
  client = defaultClient,
  onChanged,
  onError,
}: {
  grant: ActivationGrant;
  client?: GrantCardClient;
  onChanged?: () => void;
  onError?: (message: string) => void;
}) {
  const [evaluation, setEvaluation] = useState<GrantEvaluation | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("owner_revocation");
  const [busy, setBusy] = useState(false);

  const reportError = useCallback(
    (value: unknown) => {
      onError?.(value instanceof Error ? value.message : String(value));
    },
    [onError],
  );

  useEffect(() => {
    let active = true;
    client
      .evaluate(grant.grant_id)
      .then((next) => {
        if (active) setEvaluation(next);
      })
      .catch((value: unknown) => {
        if (active) reportError(value);
      });
    return () => {
      active = false;
    };
  }, [client, grant.grant_id, reportError]);

  async function revoke() {
    if (evaluation === null || busy) return;
    setBusy(true);
    try {
      const expectedRevision =
        evaluation.latest_transition?.sequence ?? evaluation.transition_count;
      await client.revoke({
        grantId: grant.grant_id,
        expectedRevision: Math.max(1, expectedRevision),
        reason: reason.trim() || "owner_revocation",
      });
      setConfirming(false);
      setEvaluation(await client.evaluate(grant.grant_id));
      onChanged?.();
    } catch (value) {
      reportError(value);
    } finally {
      setBusy(false);
    }
  }

  const status = evaluation?.status ?? "inactive";
  const effective = evaluation?.effective ?? false;
  const revocable = status === "active" || status === "suspended";
  const requalifiable = status === "suspended" || status === "revoked";

  return (
    <article className="grant-card">
      <header className="grant-card-head">
        <code>{shortDigest(grant.scope_digest)}</code>
        <StatusBadge value={status} />
        <StatusBadge value={effective ? "effective" : "ineffective"} />
      </header>

      {evaluation === null ? (
        <p className="grant-loading">Evaluating grant against current bindings…</p>
      ) : (
        <>
          {effective ? (
            <p className="grant-state">
              Effective — the learned route may be leased for this exact scope.
            </p>
          ) : null}
          {status === "active" && !effective ? (
            <p className="grant-state" role="alert">
              Suspension pending — the grant is active but not effective under
              the current bindings.
            </p>
          ) : null}
          {status === "suspended" ? (
            <p className="grant-state" role="alert">
              Suspended — the learned route is not leased for this scope.
            </p>
          ) : null}
          {status === "revoked" ? (
            <p className="grant-state">
              Revoked — terminal. A revoked grant never returns to active;
              regaining the learned route requires fresh qualification plus
              owner confirmation.
            </p>
          ) : null}

          {evaluation.reason_codes.length > 0 ? (
            <ul className="grant-reasons">
              {evaluation.reason_codes.map((code) => (
                <li key={code}>
                  <span>{humanizeReason(code)}</span> <code>{code}</code>
                </li>
              ))}
            </ul>
          ) : null}

          <ul className="grant-facts">
            <li>
              Scope: {grant.scope.project_id} · {grant.scope.task_family} ·{" "}
              {grant.scope.risk} · {grant.scope.capability_key}
            </li>
            <li>
              Granted target: {grant.target_id} (policy {grant.policy_id} @
              revision {grant.policy_revision})
            </li>
            <li>
              Targets in scope: {grant.scope.target_ids.join(", ")}
            </li>
            <li>
              Qualification receipt: <code>{grant.qualification_receipt_id}</code>
            </li>
            <li>
              Activated by {grant.created_by} at {grant.created_at}
            </li>
            <li>
              Receipt authenticates:{" "}
              {evaluation.receipt_authenticates ? "yes" : "no"}
            </li>
          </ul>

          <section
            className="grant-bindings"
            aria-label="Binding health"
          >
            <h4>Binding health</h4>
            <ul>
              {Object.entries(BINDING_LABELS).map(([key, label]) => {
                const changed = evaluation.binding_changes[key] === true;
                return (
                  <li key={key}>
                    {label}:{" "}
                    <StatusBadge value={changed ? "changed" : "unchanged"} />
                  </li>
                );
              })}
            </ul>
          </section>

          <p className="grant-evidence-links">
            <a href="#/flock/routing">Route decisions &amp; shadow evidence</a>
            {" · "}
            <a href="#/flock/qualification">Qualification runs</a>
          </p>

          <div className="grant-actions">
            {revocable ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirming((current) => !current)}
              >
                Revoke
              </button>
            ) : null}
            {requalifiable ? (
              <a className="btn subtle" href="#/flock/qualification">
                Requalify
              </a>
            ) : null}
          </div>

          {confirming && revocable ? (
            <div className="revoke-confirm" role="alert">
              <p>
                Revocation affects new route leases immediately: the learned
                route is no longer leased for this scope.
              </p>
              <p>
                An in-flight attempt keeps its existing route lease; it is
                never rerouted mid-attempt.
              </p>
              <label>
                Revocation reason
                <input
                  value={reason}
                  maxLength={240}
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
              <div className="grant-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void revoke()}
                >
                  Confirm revocation
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}

function shortDigest(digest: string): string {
  return digest.length > 16 ? `${digest.slice(0, 12)}…` : digest;
}

function humanizeReason(code: string): string {
  const spaced = code.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
