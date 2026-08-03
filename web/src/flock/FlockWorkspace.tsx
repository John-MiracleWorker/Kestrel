import { Bird } from "lucide-react";
import { StatusBadge } from "../components";
import { RoutingCenter } from "../routing/RoutingCenter";
import { ActivationsWorkspace } from "./activation/ActivationsWorkspace";
import { LanDiscoveryPanel } from "./lan/LanDiscoveryPanel";
import { QualificationWorkspace } from "./qualification/QualificationWorkspace";

export function FlockWorkspace({
  subroute,
  activeRunId,
  activeTaskId,
  onError,
  onNotice,
}: {
  subroute: string;
  activeRunId: string | null;
  activeTaskId: string | null;
  onError: (message: string | null) => void;
  onNotice: (message: string | null) => void;
}) {
  if (subroute === "qualification") {
    return (
      <QualificationWorkspace onError={onError} onNotice={onNotice} />
    );
  }
  if (subroute === "activations") {
    return <ActivationsWorkspace onError={onError} onNotice={onNotice} />;
  }
  if (subroute === "lan" || subroute === "discovery") {
    return <LanDiscoveryPanel onError={onError} onNotice={onNotice} />;
  }

  return (
    <>
      {subroute === "overview" ? (
        <section className="flock-route-overview" aria-label="Flock readiness">
          <h2>
            <Bird size={20} /> Flock readiness
          </h2>
          <div className="metric-grid">
            <a className="data-row" href="#/flock/routing">
              <strong>Deterministic routing</strong>
              <StatusBadge value="available" />
              <span>Configure and preview the current static routing policy.</span>
            </a>
            <a className="data-row" href="#/flock/qualification">
              <strong>Adaptive qualification</strong>
              <StatusBadge value="available" />
              <span>
                Bounded, owner-reviewed evidence runs with decimal-text caps.
              </span>
            </a>
            <a className="data-row" href="#/flock/activations">
              <strong>Scoped activation</strong>
              <StatusBadge value="available" />
              <span>
                Exact, owner-confirmed grant activation, suspension, and
                revocation.
              </span>
            </a>
            <a className="data-row" href="#/flock/lan">
              <strong>LAN model discovery</strong>
              <StatusBadge value="available" />
              <span>Explicit owner-confirmed private-network discovery.</span>
            </a>
          </div>
        </section>
      ) : null}
      <RoutingCenter
        activeRunId={activeRunId}
        activeTaskId={activeTaskId}
        onError={onError}
        onNotice={onNotice}
      />
    </>
  );
}
