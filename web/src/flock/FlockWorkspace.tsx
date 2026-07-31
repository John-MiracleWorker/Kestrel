import { Bird, Network, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";
import { StatusBadge } from "../components";
import { RoutingCenter } from "../routing/RoutingCenter";

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
      <DependencySurface
        title="Adaptive Flock qualification"
        detail="Qualification is not available in this build. No corpus run, routing authority, scoped grant, or activation has been created."
        icon={<ShieldCheck size={20} />}
      />
    );
  }
  if (subroute === "lan" || subroute === "discovery") {
    return (
      <DependencySurface
        title="LAN model discovery"
        detail="LAN discovery is not available in this build. No LAN scan has run, and Kestrel has not trusted or enabled any network model."
        icon={<Network size={20} />}
      />
    );
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
              <StatusBadge value="unavailable" />
              <span>Requires the qualification and scoped-grant service.</span>
            </a>
            <a className="data-row" href="#/flock/lan">
              <strong>LAN model discovery</strong>
              <StatusBadge value="unavailable" />
              <span>Requires the explicit private-network discovery service.</span>
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

function DependencySurface({
  title,
  detail,
  icon,
}: {
  title: string;
  detail: string;
  icon: ReactNode;
}) {
  return (
    <section className="panel flock-dependency" aria-labelledby="flock-dependency-title">
      <div className="panel-head">
        <h2 id="flock-dependency-title">
          {icon}
          {title}
        </h2>
        <StatusBadge value="unavailable" />
      </div>
      <p>{detail}</p>
      <p className="muted">
        Until its evidence service is installed and qualified, Kestrel remains
        on deterministic routing with no learned authority.
      </p>
      <a className="btn subtle" href="#/flock/routing">
        Open deterministic routing
      </a>
    </section>
  );
}
