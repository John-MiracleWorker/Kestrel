/**
 * Activations workspace (Adaptive Flock plan, Task 20).
 *
 * Owner flow: enter a terminal qualification receipt + scope digests →
 * preview the exact authority change → explicitly select qualified scopes →
 * confirm → activate.  Existing grants are listed with their server-side
 * evaluation and owner-only revoke/requalify controls.  Activation is
 * separate from provider and target enablement, which stays in Routing.
 */

import { KeyRound } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { EmptyState, Field, Panel } from "../../components";
import { ActivationPacket } from "./ActivationPacket";
import { GrantCard } from "./GrantCard";
import {
  createActivation,
  evaluateActivation,
  listActivations,
  previewActivation,
  revokeActivation,
} from "./api";
import type {
  ActivationBindingsInput,
  ActivationGrant,
  ActivationPreview,
} from "./types";
import "./activation.css";

export type ActivationsWorkspaceClient = Readonly<{
  preview: typeof previewActivation;
  create: typeof createActivation;
  list: typeof listActivations;
  evaluate: (grantId: string) => ReturnType<typeof evaluateActivation>;
  revoke: typeof revokeActivation;
}>;

const defaultClient: ActivationsWorkspaceClient = {
  preview: previewActivation,
  create: createActivation,
  list: listActivations,
  evaluate: evaluateActivation,
  revoke: revokeActivation,
};

const BINDING_FIELDS = [
  ["projectAuthority", "Project authority JSON"],
  ["targetSnapshot", "Target snapshot JSON"],
  ["priceSnapshot", "Price snapshot JSON"],
  ["policyPayload", "Policy payload JSON"],
  ["learnedPayload", "Learned payload JSON"],
] as const;

type BindingKey = (typeof BINDING_FIELDS)[number][0];

export function ActivationsWorkspace({
  onError,
  onNotice,
  client = defaultClient,
}: {
  onError: (message: string) => void;
  onNotice: (message: string) => void;
  client?: ActivationsWorkspaceClient;
}) {
  const [receiptId, setReceiptId] = useState("");
  const [digestsText, setDigestsText] = useState("");
  const [preview, setPreview] = useState<ActivationPreview | null>(null);
  const [grants, setGrants] = useState<ActivationGrant[]>([]);
  const [busy, setBusy] = useState(false);
  const [bindingText, setBindingText] = useState<Record<BindingKey, string>>({
    projectAuthority: "{}",
    targetSnapshot: "{}",
    priceSnapshot: "{}",
    policyPayload: "{}",
    learnedPayload: "{}",
  });
  const [bindings, setBindings] = useState<ActivationBindingsInput>({
    projectAuthority: {},
    targetSnapshot: {},
    priceSnapshot: {},
    policyPayload: {},
    learnedPayload: {},
  });

  const reportError = useCallback(
    (value: unknown) => {
      onError(value instanceof Error ? value.message : String(value));
    },
    [onError],
  );

  const reloadGrants = useCallback(async () => {
    try {
      setGrants(await client.list({}));
    } catch (value) {
      reportError(value);
    }
  }, [client, reportError]);

  useEffect(() => {
    void reloadGrants();
  }, [reloadGrants]);

  function updateBinding(key: BindingKey, text: string) {
    setBindingText((current) => ({ ...current, [key]: text }));
    try {
      const parsed: unknown = JSON.parse(text || "{}");
      if (
        typeof parsed !== "object" ||
        parsed === null ||
        Array.isArray(parsed)
      ) {
        throw new Error("binding must be a JSON object");
      }
      setBindings((current) => ({
        ...current,
        [key]: parsed as Record<string, unknown>,
      }));
    } catch (value) {
      reportError(value);
    }
  }

  async function runPreview() {
    if (busy) return;
    const scopeDigests = [
      ...new Set(digestsText.split(/[\s,]+/).filter(Boolean)),
    ];
    setBusy(true);
    try {
      setPreview(
        await client.preview({
          receiptId: receiptId.trim(),
          scopeDigests,
        }),
      );
    } catch (value) {
      reportError(value);
    } finally {
      setBusy(false);
    }
  }

  const packetClient = { create: client.create };
  const grantClient = { evaluate: client.evaluate, revoke: client.revoke };

  return (
    <section
      className="content-grid wide-left activation-workspace"
      aria-label="Activations workspace"
    >
      <Panel title="Flock activations" icon={<KeyRound size={19} />}>
        <p className="muted">
          Activation is owner-confirmed per exact scope and never enables a
          provider or model target; inventory enablement stays in{" "}
          <a href="#/flock/routing">Routing inventory</a>, and enabling a
          target there never activates a grant here.
        </p>

        <div className="activation-preview-form">
          <Field
            label="Qualification receipt ID"
            hint="Terminal receipt from a completed qualification run."
          >
            <input
              value={receiptId}
              onChange={(event) => setReceiptId(event.target.value)}
              placeholder="rcpt_…"
            />
          </Field>
          <Field
            label="Scope digests"
            hint="Comma or space separated, from the receipt scope results."
          >
            <input
              value={digestsText}
              onChange={(event) => setDigestsText(event.target.value)}
            />
          </Field>
          <button
            type="button"
            disabled={busy || !receiptId.trim() || !digestsText.trim()}
            onClick={() => void runPreview()}
          >
            {busy ? "Previewing…" : "Preview activation"}
          </button>
        </div>

        {preview !== null ? (
          <>
            <details className="activation-bindings">
              <summary>Activation bindings (JSON, advanced)</summary>
              <p className="muted">
                These payloads must match the digests bound at qualification;
                the server recomputes every digest and rejects drift
                fail-closed.
              </p>
              {BINDING_FIELDS.map(([key, label]) => (
                <Field key={key} label={label}>
                  <textarea
                    rows={2}
                    value={bindingText[key]}
                    onChange={(event) => updateBinding(key, event.target.value)}
                  />
                </Field>
              ))}
            </details>
            <ActivationPacket
              preview={preview}
              bindings={bindings}
              client={packetClient}
              onError={onError}
              onActivated={(result) => {
                setPreview(null);
                onNotice(
                  `${result.grants.length} grant${
                    result.grants.length === 1 ? "" : "s"
                  } activated.`,
                );
                void reloadGrants();
              }}
            />
          </>
        ) : null}
      </Panel>

      <Panel title="Activation grants" icon={<KeyRound size={19} />}>
        {grants.length === 0 ? (
          <EmptyState>No activation grants yet.</EmptyState>
        ) : (
          <div className="grant-list">
            {grants.map((grant) => (
              <GrantCard
                key={grant.grant_id}
                grant={grant}
                client={grantClient}
                onChanged={() => void reloadGrants()}
                onError={onError}
              />
            ))}
          </div>
        )}
      </Panel>
    </section>
  );
}
