import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
  type ReactPortal,
} from "react";
import { createPortal } from "react-dom";
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

type ContextRailBridge = {
  acquire: () => () => void;
  target: HTMLDivElement | null;
};

const AppShellContextRailBridge =
  createContext<ContextRailBridge | null>(null);

export function AppShell({
  location,
  onNavigate,
  children,
  contextRail,
  contentOwnsMain = false,
}: AppShellProps) {
  const [commandOpen, setCommandOpen] = useState(false);
  const [registeredContexts, setRegisteredContexts] = useState(0);
  const [contextTarget, setContextTarget] =
    useState<HTMLDivElement | null>(null);
  const acquireContext = useCallback(() => {
    setRegisteredContexts((current) => current + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      setRegisteredContexts((current) => Math.max(0, current - 1));
    };
  }, []);
  const contextBridge = useMemo(
    () => ({
      acquire: acquireContext,
      target: contextTarget,
    }),
    [acquireContext, contextTarget],
  );
  const hasContext =
    contextRail !== undefined || registeredContexts > 0;
  const destinationLabel =
    DESTINATIONS.find(
      (destination) => destination.id === location.destination,
    )?.label ?? "Workbench";
  const defaultContextOpen =
    typeof window === "undefined" || window.innerWidth > 1000;

  return (
    <AppShellContextRailBridge.Provider value={contextBridge}>
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
              hasContext ? "with-context" : ""
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
            {hasContext ? (
              <ContextRail
                label={`${destinationLabel} context`}
                defaultOpen={defaultContextOpen}
              >
                {contextRail}
                <div
                  className="workbench-context-portal"
                  ref={setContextTarget}
                />
              </ContextRail>
            ) : null}
          </div>
        </div>
      </div>
    </AppShellContextRailBridge.Provider>
  );
}

export function useAppShellContextRail(
  content: ReactNode,
): { hosted: boolean; portal: ReactPortal | null } {
  const bridge = useContext(AppShellContextRailBridge);
  const enabled = content !== null && content !== undefined;

  useEffect(() => {
    if (!bridge || !enabled) return;
    return bridge.acquire();
  }, [bridge?.acquire, enabled]);

  return {
    hosted: Boolean(bridge),
    portal:
      enabled && bridge?.target
        ? createPortal(content, bridge.target)
        : null,
  };
}
