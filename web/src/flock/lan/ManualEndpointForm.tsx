import { useState } from "react";
import { Field } from "../../components";
import {
  confirmManualLanProbe,
  previewManualLanProbe,
} from "./api";
import type {
  LanInterface,
  LanManualPreview,
  LanScan,
} from "./types";

export function ManualEndpointForm({
  interfaces,
  interfacesLoading,
  onLoadInterfaces,
  onProbeStarted,
  onError,
}: {
  interfaces: LanInterface[] | null;
  interfacesLoading: boolean;
  onLoadInterfaces: () => void;
  onProbeStarted: (scan: LanScan) => void;
  onError: (message: string) => void;
}) {
  const [interfaceId, setInterfaceId] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [preview, setPreview] = useState<LanManualPreview | null>(null);
  const [selectedAddress, setSelectedAddress] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);

  const portNumber = Number(port);
  const portValid =
    port.trim() !== "" &&
    Number.isSafeInteger(portNumber) &&
    portNumber >= 1 &&
    portNumber <= 65_535;
  const previewReady =
    interfaceId !== "" && host.trim() !== "" && portValid && !busy;

  function reportError(error: unknown) {
    onError(error instanceof Error ? error.message : String(error));
  }

  async function runPreview() {
    setBusy(true);
    try {
      const result = await previewManualLanProbe({
        interfaceId,
        host: host.trim(),
        port: portNumber,
      });
      setPreview(result);
      setSelectedAddress(result.resolved_addresses[0] ?? "");
      setAcknowledged(false);
    } catch (error) {
      reportError(error);
    } finally {
      setBusy(false);
    }
  }

  async function runProbe() {
    if (preview === null || selectedAddress === "") return;
    setBusy(true);
    try {
      const scan = await confirmManualLanProbe({
        expectedRevision: 0,
        previewDigest: preview.preview_digest,
        selectedAddress,
        confirmed: true,
        privacyAcknowledged: true,
      });
      setPreview(null);
      onProbeStarted(scan);
    } catch (error) {
      reportError(error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="lan-manual-form">
      <p className="muted">
        Manual probing is separate from network scanning and contacts exactly
        one host and port that you name.
      </p>
      {interfaces === null ? (
        <button
          type="button"
          onClick={onLoadInterfaces}
          disabled={interfacesLoading}
        >
          Load interfaces
        </button>
      ) : null}
      <div className="field-row">
        <Field label="Manual interface">
          <select
            value={interfaceId}
            disabled={interfaces === null}
            onChange={(event) => {
              setInterfaceId(event.target.value);
              setPreview(null);
            }}
          >
            <option value="">Select interface</option>
            {(interfaces ?? []).map((item) => (
              <option key={item.interface_id} value={item.interface_id}>
                {item.display_name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Exact host">
          <input
            value={host}
            onChange={(event) => {
              setHost(event.target.value);
              setPreview(null);
            }}
            placeholder="192.168.1.20"
          />
        </Field>
        <Field label="Exact port">
          <input
            value={port}
            inputMode="numeric"
            onChange={(event) => {
              setPort(event.target.value);
              setPreview(null);
            }}
            placeholder="11434"
          />
        </Field>
      </div>
      <button
        type="button"
        onClick={runPreview}
        disabled={!previewReady}
      >
        Preview endpoint
      </button>
      {preview !== null ? (
        <div className="lan-manual-preview">
          <p>
            Resolved {preview.resolved_addresses.length} address
            {preview.resolved_addresses.length === 1 ? "" : "es"} on port{" "}
            {preview.port}. Only the selected address will be probed.
          </p>
          <div role="radiogroup" aria-label="Resolved address">
            {preview.resolved_addresses.map((address) => (
              <label key={address}>
                <input
                  type="radio"
                  name="lan-manual-address"
                  checked={selectedAddress === address}
                  onChange={() => setSelectedAddress(address)}
                />
                {address}
              </label>
            ))}
          </div>
          <label className="check-row lan-privacy-ack">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <span>
              I understand that prompts and code leave this computer when this
              endpoint is used.
            </span>
          </label>
          <button
            type="button"
            onClick={runProbe}
            disabled={busy || !acknowledged || selectedAddress === ""}
          >
            Probe endpoint
          </button>
        </div>
      ) : null}
    </div>
  );
}
