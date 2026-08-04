import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  EMPTY_APPLICATION_SNAPSHOT,
  type ApplicationSnapshot,
} from "./useApplicationData";

export type ApplicationSelection = Readonly<{
  projectId: string | null;
  runId: string | null;
  sessionId: string | null;
}>;

export type ApplicationContextValue = Readonly<{
  snapshot: ApplicationSnapshot;
  selection: ApplicationSelection;
  selectProject: (projectId: string | null) => void;
  selectRun: (runId: string | null) => void;
  selectSession: (sessionId: string | null) => void;
}>;

const unavailableSelection = Object.freeze({
  projectId: null,
  runId: null,
  sessionId: null,
});

const ApplicationContext = createContext<ApplicationContextValue>({
  snapshot: EMPTY_APPLICATION_SNAPSHOT,
  selection: unavailableSelection,
  selectProject: () => undefined,
  selectRun: () => undefined,
  selectSession: () => undefined,
});

export function ApplicationProvider({
  children,
  snapshot = EMPTY_APPLICATION_SNAPSHOT,
}: {
  children: ReactNode;
  snapshot?: ApplicationSnapshot;
}) {
  const [selection, setSelection] = useState<ApplicationSelection>(
    unavailableSelection,
  );
  const value = useMemo<ApplicationContextValue>(
    () => ({
      snapshot,
      selection,
      selectProject: (projectId) =>
        setSelection((current) => ({ ...current, projectId })),
      selectRun: (runId) =>
        setSelection((current) => ({ ...current, runId })),
      selectSession: (sessionId) =>
        setSelection((current) => ({ ...current, sessionId })),
    }),
    [selection, snapshot],
  );
  return (
    <ApplicationContext.Provider value={value}>
      {children}
    </ApplicationContext.Provider>
  );
}

export function useApplicationContext(): ApplicationContextValue {
  return useContext(ApplicationContext);
}
