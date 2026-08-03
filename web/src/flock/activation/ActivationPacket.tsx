/**
 * Activation packet (Adaptive Flock plan, Task 20).
 *
 * Shows the exact authority change one owner confirmation would create and
 * activates only explicitly selected, qualified scopes.  Abstained and
 * deterministic-only scopes are disabled — never selectable, never sent.
 * The confirmation binds the exact receipt digest and run revision; the
 * server re-checks every binding digest fail-closed.
 */

import { useState } from "react";
import { InlineMeta, JsonBlock, StatusBadge } from "../../components";
import { createActivation } from "./api";
import type {
  ActivationBindingsInput,
  ActivationPreview,
  ActivationResult,
  ActivationScopePreview,
} from "./types";

export type ActivationPacketClient = Readonly<{
  create: typeof createActivation;
}>;

const defaultClient: ActivationPacketClient = { create: createActivation };

const EMPTY_BINDINGS: ActivationBindingsInput = {
  projectAuthority: {},
  targetSnapshot: {},
  priceSnapshot: {},
  policyPayload: {},
  learnedPayload: {},
};

type ScopeState = "qualified" | "abstained" | "deterministic-only";

function scopeState(scope: ActivationScopePreview): ScopeState {
  if (scope.qualified) return "qualified";
  return scope.risk === "high" || scope.risk === "critical"
    ? "deterministic-only"
    : "abstained";
}

export function ActivationPacket({
  preview,
  bindings,
  client = defaultClient,
  onActivated,
  onError,
}: {
  preview: ActivationPreview;
  bindings?: ActivationBindingsInput;
  client?: ActivationPacketClient;
  onActivated?: (result: ActivationResult) => void;
  onError?: (message: string) => void;
}) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ActivationResult | null>(null);

  const changedBindings = Object.entries(preview.binding_changes)
    .filter(([, changed]) => changed)
    .map(([key]) => key);

  function toggle(scope: ActivationScopePreview, checked: boolean) {
    if (!scope.qualified) return;
    setSelected((current) => {
      const next = new Set(current);
      if (checked) {
        next.add(scope.scope_digest);
      } else {
        next.delete(scope.scope_digest);
      }
      return next;
    });
  }

  async function activate() {
    if (!confirmed || selected.size === 0 || busy) return;
    setBusy(true);
    try {
      // Only explicitly selected, qualified digests — in receipt order.
      const scopeDigests = preview.scopes
        .filter((scope) => scope.qualified && selected.has(scope.scope_digest))
        .map((scope) => scope.scope_digest);
      const activated = await client.create({
        receiptId: preview.receipt_id,
        scopeDigests,
        expectedReceiptDigest: preview.receipt_digest,
        expectedRunRevision: preview.run_revision,
        bindings: bindings ?? EMPTY_BINDINGS,
      });
      setResult(activated);
      onActivated?.(activated);
    } catch (value) {
      onError?.(value instanceof Error ? value.message : String(value));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="activation-packet" aria-label="Activation packet">
      <h3>Activation packet</h3>
      <InlineMeta
        items={[
          preview.receipt_id,
          `${preview.run_id} revision ${preview.run_revision}`,
          preview.owner_principal,
        ]}
      />
      <p className="muted">
        Receipt digest <code>{preview.receipt_digest}</code>
      </p>

      {preview.authority_changed || changedBindings.length > 0 ? (
        <p className="activation-binding-warning" role="alert">
          Bindings changed since qualification: {changedBindings.join(", ")}.
          The server rejects stale activations fail-closed; requalify before
          activating.
        </p>
      ) : null}

      <section className="activation-authority" aria-label="Exact authority change">
        <h4>Exact authority change</h4>
        <p className="muted">
          Activation never expands a task&apos;s tools, workspace, approvals,
          or privacy policy. It only lets the qualified learned target lease
          the route for each selected scope.
        </p>
        <ul className="activation-scope-list">
          {preview.scopes.map((scope) => {
            const state = scopeState(scope);
            const selectable = state === "qualified";
            return (
              <li className="activation-scope-row" key={scope.scope_digest}>
                <label>
                  <input
                    type="checkbox"
                    aria-label={`Scope ${scope.task_family} ${state}`}
                    disabled={!selectable}
                    checked={selected.has(scope.scope_digest)}
                    onChange={(event) => toggle(scope, event.target.checked)}
                  />
                  <strong>{scope.task_family}</strong>
                  <StatusBadge value={state} />
                </label>
                <InlineMeta
                  items={[
                    scope.project_id,
                    scope.risk,
                    scope.capabilities.join(" · "),
                    `${scope.static_target_id} → ${
                      scope.selected_target_id ?? "no learned route"
                    }`,
                    `support ${scope.selected_target_support}/${scope.total_support}`,
                    `confidence ${scope.confidence}`,
                    scope.estimated_savings_usd === null
                      ? "savings not estimated"
                      : `est. savings $${scope.estimated_savings_usd.toFixed(2)}`,
                  ]}
                />
                {scope.reasons.length > 0 ? (
                  <ul className="activation-scope-reasons">
                    {scope.reasons.map((reason) => (
                      <li key={reason}>
                        <code>{reason}</code>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            );
          })}
        </ul>
      </section>

      <section className="activation-conditions" aria-label="Suspension and revocation">
        <h4>Suspension conditions</h4>
        <ul className="activation-scope-reasons">
          {preview.suspension_conditions.map((condition) => (
            <li key={condition}>
              <code>{condition}</code>
            </li>
          ))}
        </ul>
        <h4>Revocation behavior</h4>
        <p className="muted">{preview.revocation_behavior}</p>
      </section>

      <details className="activation-evidence">
        <summary>Binding digests / Advanced</summary>
        <JsonBlock
          value={{
            binding_digests: preview.binding_digests,
            binding_changes: preview.binding_changes,
            replay: preview.replay,
          }}
        />
      </details>

      <label className="activation-confirm">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        I understand this activation grants only the exact learned-route
        authority shown above, for the scopes I explicitly selected.
      </label>
      <button
        type="button"
        disabled={!confirmed || selected.size === 0 || busy}
        onClick={() => void activate()}
      >
        {busy
          ? "Activating…"
          : `Activate ${selected.size} scope${selected.size === 1 ? "" : "s"}`}
      </button>

      {result !== null ? (
        <p className="activation-result" role="status">
          {result.grants.length} grant
          {result.grants.length === 1 ? "" : "s"} activated
          {result.superseded.length > 0
            ? `; ${result.superseded.length} prior grant(s) superseded`
            : ""}
          .
        </p>
      ) : null}
    </div>
  );
}
