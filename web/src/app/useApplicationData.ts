import { useEffect, useState } from "react";
import { getJson } from "../api";
import type {
  Approval,
  Run,
  RuntimeConfig,
  Session,
  SetupReadinessReport,
} from "../types";

export type ApplicationSliceStatus =
  | "idle"
  | "loading"
  | "ready"
  | "error";

export type ApplicationSlice<T> = Readonly<{
  status: ApplicationSliceStatus;
  data: T | null;
  error: string | null;
}>;

export type MissionApplicationData = Readonly<{
  runs: Run[];
  sessions: Session[];
  pendingApprovals: Approval[];
}>;

export type ApplicationSnapshot = Readonly<{
  runtime: ApplicationSlice<RuntimeConfig>;
  mission: ApplicationSlice<MissionApplicationData>;
  setup: ApplicationSlice<SetupReadinessReport>;
}>;

export type ApplicationDataLoader = (
  path: string,
  options: { signal: AbortSignal },
) => Promise<unknown>;

export type UseApplicationDataOptions = {
  enabled?: boolean;
  load?: ApplicationDataLoader;
};

const idleSlice = Object.freeze({
  status: "idle",
  data: null,
  error: null,
}) as ApplicationSlice<never>;

const loadingSlice = Object.freeze({
  status: "loading",
  data: null,
  error: null,
}) as ApplicationSlice<never>;

export const EMPTY_APPLICATION_SNAPSHOT: ApplicationSnapshot =
  Object.freeze({
    runtime: idleSlice,
    mission: idleSlice,
    setup: idleSlice,
  });

const defaultLoader: ApplicationDataLoader = (path, options) =>
  getJson<unknown>(path, options);

export function useApplicationData({
  enabled = true,
  load = defaultLoader,
}: UseApplicationDataOptions = {}): ApplicationSnapshot {
  const [snapshot, setSnapshot] = useState<ApplicationSnapshot>(
    EMPTY_APPLICATION_SNAPSHOT,
  );

  useEffect(() => {
    if (!enabled) {
      setSnapshot(EMPTY_APPLICATION_SNAPSHOT);
      return;
    }

    let active = true;
    const controllers = Array.from(
      { length: 5 },
      () => new AbortController(),
    );
    setSnapshot({
      runtime: loadingSlice,
      mission: loadingSlice,
      setup: loadingSlice,
    });

    void load("/api/runtime/config", {
      signal: controllers[0].signal,
    })
      .then((data) => {
        if (!active) return;
        setSnapshot((current) => ({
          ...current,
          runtime: readySlice(data as RuntimeConfig),
        }));
      })
      .catch((error) => {
        if (!active || controllers[0].signal.aborted) return;
        setSnapshot((current) => ({
          ...current,
          runtime: errorSlice(error),
        }));
      });

    void Promise.all([
      load("/api/runs", { signal: controllers[1].signal }),
      load("/api/sessions", { signal: controllers[2].signal }),
      load("/api/approvals?status=pending", {
        signal: controllers[3].signal,
      }),
    ])
      .then(([runs, sessions, pendingApprovals]) => {
        if (!active) return;
        setSnapshot((current) => ({
          ...current,
          mission: readySlice({
            runs: runs as Run[],
            sessions: sessions as Session[],
            pendingApprovals: pendingApprovals as Approval[],
          }),
        }));
      })
      .catch((error) => {
        if (
          !active ||
          controllers.slice(1, 4).some((controller) =>
            controller.signal.aborted
          )
        ) {
          return;
        }
        setSnapshot((current) => ({
          ...current,
          mission: errorSlice(error),
        }));
      });

    void load("/api/product/setup", {
      signal: controllers[4].signal,
    })
      .then((data) => {
        if (!active) return;
        setSnapshot((current) => ({
          ...current,
          setup: readySlice(data as SetupReadinessReport),
        }));
      })
      .catch((error) => {
        if (!active || controllers[4].signal.aborted) return;
        setSnapshot((current) => ({
          ...current,
          setup: errorSlice(error),
        }));
      });

    return () => {
      active = false;
      controllers.forEach((controller) => controller.abort());
    };
  }, [enabled, load]);

  return snapshot;
}

function readySlice<T>(data: T): ApplicationSlice<T> {
  return { status: "ready", data, error: null };
}

function errorSlice<T>(error: unknown): ApplicationSlice<T> {
  return {
    status: "error",
    data: null,
    error: error instanceof Error ? error.message : String(error),
  };
}
