import { useCallback, useState } from "react";
import { Network } from "lucide-react";
import { EmptyState, Field, Panel } from "../../components";
import {
  cancelLanScan,
  createLanScan,
  getLanInterfaces,
  previewLanScope,
  startLanScan,
} from "./api";
import { LanScanProgress } from "./LanScanProgress";
import { LanScopePreview } from "./LanScopePreview";
import { LanServerCard } from "./LanServerCard";
import { ManualEndpointForm } from "./ManualEndpointForm";
import { useLanScan } from "./useLanScan";
import type {
  LanInterface,
  LanScan,
  LanScopePreview as LanScopePreviewData,
} from "./types";
import "./lan.css";

const TERMINAL_STATUSES = new Set([
  "cancelled",
  "completed",
  "failed",
  "interrupted",
]);

export function LanDiscoveryPanel({
  onError,
  onNotice,
}: {
  onError: (message: string) => void;
  onNotice: (message: string) => void;
}) {
  const [interfaces, setInterfaces] = useState<LanInterface[] | null>(null);
  const [interfacesLoading, setInterfacesLoading] = useState(false);
  const [selectedInterfaceId, setSelectedInterfaceId] = useState("");
  const [network, setNetwork] = useState("");
  const [preview, setPreview] = useState<LanScopePreviewData | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [scanId, setScanId] = useState<string | null>(null);

  const { scan, status, connection, error, refresh } = useLanScan(scanId);

  const scanActive = scanId !== null && !TERMINAL_STATUSES.has(status);
  const scopeLocked = scanActive;

  const reportError = useCallback(
    (value: unknown) => {
      onError(value instanceof Error ? value.message : String(value));
    },
    [onError],
  );

  const loadInterfaces = useCallback(async () => {
    setInterfacesLoading(true);
    try {
      const loaded = await getLanInterfaces();
      setInterfaces(loaded);
    } catch (value) {
      reportError(value);
    } finally {
      setInterfacesLoading(false);
    }
  }, [reportError]);

  function selectInterface(interfaceId: string) {
    setSelectedInterfaceId(interfaceId);
    setPreview(null);
    const selected = interfaces?.find(
      (item) => item.interface_id === interfaceId,
    );
    setNetwork(deriveNetwork(selected?.addresses[0] ?? ""));
  }

  async function runScopePreview() {
    setPreviewing(true);
    try {
      setPreview(
        await previewLanScope({
          interfaceId: selectedInterfaceId,
          network: network.trim(),
        }),
      );
    } catch (value) {
      reportError(value);
    } finally {
      setPreviewing(false);
    }
  }

  async function confirmAndScan() {
    if (preview === null) return;
    setStarting(true);
    try {
      const draft = await createLanScan({
        previewDigest: preview.preview_digest,
        expectedRevision: 0,
        confirmed: true,
      });
      const started = await startLanScan({
        scanId: draft.scan_id,
        expectedRevision: draft.revision,
        previewDigest: preview.preview_digest,
        confirmed: true,
      });
      setScanId(started.scan_id);
      onNotice(`LAN scan ${started.scan_id} started for ${started.network}.`);
    } catch (value) {
      reportError(value);
    } finally {
      setStarting(false);
    }
  }

  async function cancelActiveScan() {
    if (scanId === null || scan === null) return;
    setCancelling(true);
    try {
      await cancelLanScan({
        scanId,
        expectedRevision: scan.revision,
      });
      await refresh();
    } catch (value) {
      reportError(value);
    } finally {
      setCancelling(false);
    }
  }

  function handleProbeStarted(scan: LanScan) {
    setScanId(scan.scan_id);
    onNotice(`Manual probe ${scan.scan_id} started.`);
  }

  const observations = scan?.observations ?? [];

  return (
    <section
      className="content-grid wide-left lan-workspace"
      aria-label="LAN discovery workspace"
    >
      <Panel title="LAN model discovery" icon={<Network size={19} />}>
        <p>
          LAN discovery is explicit. No LAN scan has run, and Kestrel has not
          trusted or enabled any network model. Nothing is probed until you
          choose Scan network, inspect the exact scope, and confirm it.
        </p>
        <button
          type="button"
          onClick={() => void loadInterfaces()}
          disabled={interfacesLoading || scanActive}
        >
          Scan network
        </button>
        {interfaces !== null ? (
          <div className="lan-scope">
            {interfaces.length === 0 ? (
              <EmptyState>
                No network interfaces are available for discovery.
              </EmptyState>
            ) : (
              <>
                <div
                  role="radiogroup"
                  aria-label="Network interface"
                  className="lan-interface-list"
                >
                  {interfaces.map((item) => (
                    <label key={item.interface_id}>
                      <input
                        type="radio"
                        name="lan-interface"
                        checked={selectedInterfaceId === item.interface_id}
                        disabled={scopeLocked}
                        onChange={() => selectInterface(item.interface_id)}
                      />
                      {item.display_name}
                      <span className="muted">
                        {" "}
                        {item.addresses.join(", ")}
                      </span>
                    </label>
                  ))}
                </div>
                <Field
                  label="Network scope"
                  hint="The exact private range that will be probed. It cannot be broadened while a scan is running."
                >
                  <input
                    value={network}
                    disabled={scopeLocked}
                    onChange={(event) => {
                      setNetwork(event.target.value);
                      setPreview(null);
                    }}
                  />
                </Field>
                <button
                  type="button"
                  onClick={() => void runScopePreview()}
                  disabled={
                    previewing ||
                    scopeLocked ||
                    selectedInterfaceId === "" ||
                    network.trim() === ""
                  }
                >
                  Preview scope
                </button>
              </>
            )}
          </div>
        ) : null}
        {preview !== null ? (
          <div className="lan-scope-confirm">
            <LanScopePreview preview={preview} />
            <button
              type="button"
              onClick={() => void confirmAndScan()}
              disabled={starting || scopeLocked}
            >
              Confirm and scan
            </button>
          </div>
        ) : null}
      </Panel>

      {scanId !== null ? (
        <Panel title="Scan progress" icon={<Network size={19} />}>
          <LanScanProgress
            status={status}
            scan={scan}
            connection={connection}
            error={error}
            cancelling={cancelling}
            onCancel={() => void cancelActiveScan()}
          />
        </Panel>
      ) : null}

      {status === "completed" && scan !== null ? (
        <Panel title="Discovered servers" icon={<Network size={19} />}>
          {observations.length === 0 ? (
            <EmptyState>
              The scan completed without discovering a model server.
            </EmptyState>
          ) : (
            <div className="lan-server-list">
              {observations.map((observation) => (
                <LanServerCard
                  key={observation.endpoint_id}
                  observation={observation}
                  scanId={scan.scan_id}
                  importable={scan.terminal_receipt_digest !== null}
                  onImported={() => {
                    onNotice(
                      "LAN endpoint imported with all targets disabled.",
                    );
                  }}
                  onError={onError}
                />
              ))}
            </div>
          )}
          {scan.observations_truncated ? (
            <p className="muted">
              Additional observations are stored on the server; only the first
              page is shown.
            </p>
          ) : null}
        </Panel>
      ) : null}

      <Panel title="Manual endpoint" icon={<Network size={19} />}>
        <ManualEndpointForm
          interfaces={interfaces}
          interfacesLoading={interfacesLoading}
          onLoadInterfaces={() => void loadInterfaces()}
          onProbeStarted={handleProbeStarted}
          onError={onError}
        />
      </Panel>
    </section>
  );
}

function deriveNetwork(address: string): string {
  const ipv4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.\d{1,3}$/.exec(address);
  if (ipv4 !== null) {
    return `${ipv4[1]}.${ipv4[2]}.${ipv4[3]}.0/24`;
  }
  return "";
}
