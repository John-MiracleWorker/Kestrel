import { useState, type ReactNode } from "react";
import {
  DESTINATIONS,
  type AppDestination,
  type AppLocation,
} from "./destinations";
import { CommandBar } from "./CommandBar";
import { ContextRail } from "./ContextRail";
import { NavigationRail } from "./NavigationRail";

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
  const [commandOpen, setCommandOpen] = useState(false);
  const destinationLabel =
    DESTINATIONS.find(
      (destination) => destination.id === location.destination,
    )?.label ?? "Workbench";
  const defaultContextOpen =
    typeof window === "undefined" || window.innerWidth > 1000;

  return (
    <div className="workbench-shell">
      <NavigationRail
        location={location}
        onNavigate={onNavigate}
        disabled={commandOpen}
      />
      <div className="workbench-shell-frame">
        <CommandBar
          onNavigate={onNavigate}
          onOpenChange={setCommandOpen}
        />
        <div
          className={`workbench-shell-body ${
            contextRail ? "with-context" : ""
          }`}
          inert={commandOpen ? true : undefined}
        >
          {contentOwnsMain ? (
            <div className="workbench-shell-content">{children}</div>
          ) : (
            <main
              className="workbench-shell-content"
              id="workspace"
              tabIndex={0}
            >
              {children}
            </main>
          )}
          {contextRail ? (
            <ContextRail
              label={`${destinationLabel} context`}
              defaultOpen={defaultContextOpen}
            >
              {contextRail}
            </ContextRail>
          ) : null}
        </div>
      </div>
    </div>
  );
}
