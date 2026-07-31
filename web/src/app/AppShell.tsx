import type { ReactNode } from "react";
import {
  DESTINATIONS,
  destinationLocation,
  formatAppLocation,
  type AppDestination,
  type AppLocation,
} from "./destinations";

export type AppShellProps = {
  location: AppLocation;
  onNavigate: (destination: AppDestination) => void;
  children: ReactNode;
  contextRail?: ReactNode;
  contentOwnsMain?: boolean;
};

export function AppShell({
  location,
  onNavigate,
  children,
  contextRail,
  contentOwnsMain = false,
}: AppShellProps) {
  return (
    <div className="workbench-shell">
      <div className="workbench-shell-header">
        <a
          className="workbench-shell-brand"
          href={formatAppLocation(destinationLocation("mission"))}
          onClick={(event) => {
            event.preventDefault();
            onNavigate("mission");
          }}
        >
          <span className="workbench-shell-brand-mark" aria-hidden="true">
            K
          </span>
          <span>
            <strong>Kestrel</strong>
            <small>Wildflower Workbench</small>
          </span>
        </a>
        <nav
          className="workbench-destinations"
          aria-label="Workbench destinations"
        >
          {DESTINATIONS.map((destination) => {
            const Icon = destination.icon;
            const current = destination.id === location.destination;
            return (
              <a
                data-destination={destination.id}
                href={formatAppLocation(destinationLocation(destination.id))}
                aria-label={destination.label}
                aria-current={current ? "page" : undefined}
                title={destination.label}
                key={destination.id}
                onClick={(event) => {
                  event.preventDefault();
                  onNavigate(destination.id);
                }}
              >
                <Icon size={17} aria-hidden="true" />
                <span>{destination.label}</span>
                {current ? <span className="sr-only">Current</span> : null}
              </a>
            );
          })}
        </nav>
      </div>
      <div
        className={`workbench-shell-body ${
          contextRail ? "with-context" : ""
        }`}
      >
        {contentOwnsMain ? (
          <div className="workbench-shell-content">{children}</div>
        ) : (
          <main
            className="workbench-shell-content"
            id="workspace"
            tabIndex={-1}
          >
            {children}
          </main>
        )}
        {contextRail ? (
          <aside className="workbench-context-rail" aria-label="Context">
            {contextRail}
          </aside>
        ) : null}
      </div>
    </div>
  );
}
