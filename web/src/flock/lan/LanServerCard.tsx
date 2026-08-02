import { useState } from "react";
import { StatusBadge } from "../../components";
import { confirmLanImport, previewLanImport } from "../../routing/api";
import type {
  LanImportConfirmationResult,
  LanImportPreview,
} from "../../routing/types";
import type { LanObservation } from "./types";

export function LanServerCard({
  observation,
  scanId,
  importable,
  onImported,
  onError,
}: {
  observation: LanObservation;
  scanId: string;
  importable: boolean;
  onImported: (result: LanImportConfirmationResult) => void;
  onError: (message: string) => void;
}) {
  const [importPreview, setImportPreview] = useState<LanImportPreview | null>(
    null,
  );
  const [imported, setImported] =
    useState<LanImportConfirmationResult | null>(null);
  const [busy, setBusy] = useState(false);

  const payload = observation.public_payload;
  const durable = "schema" in payload;
  const modelIds = durable
    ? payload.model_ids
    : (payload.model_ids ?? []);
  const transportText = durable
    ? payload.transport_security === "plain_http"
      ? "Plain HTTP — traffic to this target is not encrypted"
      : "Transport security not established"
    : observation.tls_enabled
      ? "TLS enabled"
      : "Plain HTTP — traffic to this target is not encrypted";

  async function runImportPreview() {
    setBusy(true);
    try {
      setImportPreview(
        await previewLanImport({
          scanId,
          endpointId: observation.endpoint_id,
          replacementProviderProfileId: null,
        }),
      );
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function runImportConfirm() {
    if (importPreview === null) return;
    setBusy(true);
    try {
      const result = await confirmLanImport({
        selector: {
          scanId,
          endpointId: observation.endpoint_id,
          replacementProviderProfileId: null,
        },
        previewDigest: importPreview.preview_digest,
        confirmed: true,
      });
      setImported(result);
      onImported(result);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="lan-server-card">
      <header className="lan-server-head">
        <strong>
          {observation.address}:{observation.port}
        </strong>
        <StatusBadge value="disabled" />
        <span className="lan-server-state">not enabled</span>
      </header>
      <ul className="lan-server-facts">
        <li>Source: {observation.source}</li>
        <li>Interface: {observation.interface_id}</li>
        <li>
          API shape: {observation.api_shape ?? "unknown"}
        </li>
        <li>Transport security: {transportText}</li>
        <li>
          Freshness: <time>{observation.freshness_timestamp}</time>
        </li>
      </ul>
      <div className="lan-server-models">
        <span>Model inventory:</span>
        {modelIds.length === 0 ? (
          <span> none reported</span>
        ) : (
          <ul>
            {modelIds.map((model) => (
              <li key={model}>{model}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="lan-server-capabilities">
        <span>Capability provenance:</span>
        <ul>
          {durable
            ? payload.capabilities.map((capability) => (
                <li key={capability.capability}>
                  {capability.capability} — provenance{" "}
                  {capability.provenance} — status {capability.status}
                </li>
              ))
            : (payload.capabilities ?? []).map((capability) => (
                <li key={capability}>{capability} — provenance declared</li>
              ))}
        </ul>
      </div>
      <p className="lan-privacy-warning">
        Enabling this target means prompts and code leave this computer.
      </p>
      <details className="lan-server-evidence">
        <summary>Evidence</summary>
        <ul>
          <li>
            Endpoint digest: <code>{observation.endpoint_id}</code>
          </li>
          <li>
            Catalog digest:{" "}
            {observation.catalog_digest ? (
              <code>{observation.catalog_digest}</code>
            ) : (
              "missing"
            )}
          </li>
          <li>
            Capability digest:{" "}
            {observation.capability_digest ? (
              <code>{observation.capability_digest}</code>
            ) : (
              "missing"
            )}
          </li>
          {durable ? (
            <li>
              Observation digest: <code>{payload.observation_digest}</code>
            </li>
          ) : null}
          {observation.certificate_sha256 ? (
            <li>
              Certificate: <code>{observation.certificate_sha256}</code>
            </li>
          ) : null}
          {observation.error_category ? (
            <li>Error: {observation.error_category}</li>
          ) : null}
        </ul>
      </details>
      {importable && imported === null ? (
        <div className="lan-server-import">
          {importPreview === null ? (
            <button type="button" onClick={runImportPreview} disabled={busy}>
              Review import
            </button>
          ) : (
            <div className="lan-import-preview">
              <p>
                Import creates a provider profile and{" "}
                {importPreview.result.targets.length} model target
                {importPreview.result.targets.length === 1 ? "" : "s"}, all
                disabled. Evidence expires{" "}
                {importPreview.evidence_expires_at}.
              </p>
              <button
                type="button"
                onClick={runImportConfirm}
                disabled={busy}
              >
                Confirm import
              </button>
            </div>
          )}
        </div>
      ) : null}
      {imported !== null ? (
        <p className="lan-import-done">
          Imported {imported.result.targets.length} disabled target
          {imported.result.targets.length === 1 ? "" : "s"} for review.
        </p>
      ) : null}
    </article>
  );
}
