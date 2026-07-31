import {
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  LegacyWorkbench,
  type LegacyWorkbenchSection,
} from "../LegacyWorkbench";
import { AppShell } from "./AppShell";
import {
  destinationLocation,
  formatAppLocation,
  parseAppLocation,
  type AppDestination,
  type AppLocation,
} from "./destinations";

export type AppRouterProps = {
  initialHash?: string;
  renderContent?: (location: AppLocation) => ReactNode;
};

export function AppRouter({
  initialHash,
  renderContent,
}: AppRouterProps = {}) {
  const [location, setLocation] = useState<AppLocation>(() =>
    initialHash !== undefined
      ? parseAppLocation(initialHash)
      : readBrowserLocation(),
  );

  useEffect(() => {
    if (initialHash !== undefined) {
      setLocation(parseAppLocation(initialHash));
      return;
    }
    const synchronize = () => setLocation(readBrowserLocation());
    window.addEventListener("hashchange", synchronize);
    return () => window.removeEventListener("hashchange", synchronize);
  }, [initialHash]);

  const commitLocation = useCallback(
    (next: AppLocation) => {
      setLocation(next);
      if (initialHash === undefined && typeof window !== "undefined") {
        window.history.pushState(null, "", formatAppLocation(next));
      }
    },
    [initialHash],
  );

  const navigate = useCallback(
    (destination: AppDestination) => {
      commitLocation(destinationLocation(destination));
    },
    [commitLocation],
  );

  const routeLegacySection = useCallback(
    (section: LegacyWorkbenchSection) => {
      commitLocation(locationForLegacySection(section));
    },
    [commitLocation],
  );

  const routeSetupCenter = useCallback(() => {
    commitLocation({
      destination: "settings",
      subroute: "setup",
      query: {},
    });
  }, [commitLocation]);

  const routeMissionCommand = useCallback(() => {
    commitLocation(destinationLocation("mission"));
  }, [commitLocation]);

  const content = renderContent ? (
    renderContent(location)
  ) : (
    <LegacyWorkbench
      requestedSection={legacySectionForLocation(location)}
      requestedSubroute={location.subroute}
      onRouteSection={routeLegacySection}
      onOpenSetup={routeSetupCenter}
      onOpenMission={routeMissionCommand}
    />
  );

  return (
    <AppShell
      location={location}
      onNavigate={navigate}
    >
      {location.recoveryReason === "unknown_route" ? (
        <div
          className="workbench-route-recovery"
          role="status"
          aria-label="Route recovery"
        >
          Unknown destination. Mission Command is open and the supplied
          evidence query has been preserved.
        </div>
      ) : null}
      {content}
    </AppShell>
  );
}

function readBrowserLocation(): AppLocation {
  const next =
    typeof window === "undefined"
      ? parseAppLocation("#/mission")
      : parseAppLocation(window.location.hash);
  if (typeof window !== "undefined") {
    const canonicalHash = formatAppLocation(next);
    if (window.location.hash !== canonicalHash) {
      window.history.replaceState(null, "", canonicalHash);
    }
  }
  return next;
}

export function legacySectionForLocation(
  location: AppLocation,
): LegacyWorkbenchSection {
  if (location.destination === "mission") {
    if (location.subroute === "history") return "chat";
    if (location.subroute === "outcomes") return "outcomes";
    return "mission";
  }
  if (location.destination === "projects") return "mission";
  if (location.destination === "memory") return "memory";
  if (location.destination === "flock") return "routing";
  if (location.destination === "automate") return "routines";
  if (location.destination === "settings") return "settings";
  return "advanced";
}

function locationForLegacySection(
  section: LegacyWorkbenchSection,
): AppLocation {
  if (section === "chat") {
    return {
      destination: "mission",
      subroute: "history",
      query: {},
    };
  }
  if (section === "outcomes") {
    return {
      destination: "mission",
      subroute: "outcomes",
      query: {},
    };
  }
  if (section === "routines") {
    return destinationLocation("automate");
  }
  if (section === "routing") {
    return {
      destination: "flock",
      subroute: "routing",
      query: {},
    };
  }
  if (section === "memory") {
    return destinationLocation("memory");
  }
  if (section === "advanced") {
    return {
      destination: "extend",
      subroute: "capabilities",
      query: {},
    };
  }
  if (section === "settings") {
    return destinationLocation("settings");
  }
  return destinationLocation("mission");
}
