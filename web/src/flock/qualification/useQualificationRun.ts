/**
 * Reconnectable qualification run state (Adaptive Flock plan, Task 18).
 *
 * The durable GET is the only authority for run state.  The SSE channel only
 * accelerates reconciliation: every event triggers an authoritative GET, and
 * after any stream drop the hook re-reads the run via GET before reconnecting
 * the stream from the persisted cursor.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiAuthError } from "../../api";
import { getQualification, streamQualificationEvents } from "./api";
import type {
  QualificationEvent,
  QualificationEventStreamOptions,
  QualificationRun,
} from "./types";

export type QualificationRunConnection =
  | "idle"
  | "loading"
  | "streaming"
  | "reconnecting"
  | "closed";

export type QualificationRunReader = (
  runId: string,
  signal: AbortSignal,
) => Promise<QualificationRun>;

export type QualificationEventReader = (
  runId: string,
  options: QualificationEventStreamOptions,
) => Promise<void>;

export type UseQualificationRunOptions = Readonly<{
  enabled?: boolean;
  getRun?: QualificationRunReader;
  readEvents?: QualificationEventReader;
  reconnectDelayMs?: number;
  onAuthRequired?: () => void;
}>;

export type QualificationRunHookState = Readonly<{
  run: QualificationRun | null;
  connection: QualificationRunConnection;
  lastEvent: QualificationEvent | null;
  lastEventSequence: string;
  error: string | null;
  refresh: () => Promise<void>;
}>;

const TERMINAL_STATUSES = new Set(["cancelled", "failed", "completed"]);

const defaultGetRun: QualificationRunReader = (runId, signal) =>
  getQualification(runId, signal);

export function useQualificationRun(
  runId: string | null,
  {
    enabled = true,
    getRun: readRun = defaultGetRun,
    readEvents = streamQualificationEvents,
    reconnectDelayMs = 750,
    onAuthRequired,
  }: UseQualificationRunOptions = {},
): QualificationRunHookState {
  const [stateRunId, setStateRunId] = useState<string | null>(null);
  const [run, setRun] = useState<QualificationRun | null>(null);
  const [connection, setConnection] =
    useState<QualificationRunConnection>("idle");
  const [lastEvent, setLastEvent] = useState<QualificationEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const activeSessionRef = useRef<{ refresh: () => Promise<void> } | null>(null);
  const onAuthRequiredRef = useRef(onAuthRequired);

  useEffect(() => {
    onAuthRequiredRef.current = onAuthRequired;
  }, [onAuthRequired]);

  const refresh = useCallback(async () => {
    await activeSessionRef.current?.refresh();
  }, []);

  useEffect(() => {
    if (!enabled || runId === null) {
      activeSessionRef.current = null;
      setStateRunId(null);
      setRun(null);
      setConnection("idle");
      setLastEvent(null);
      setError(null);
      return;
    }

    let active = true;
    let authoritative: QualificationRun | null = null;
    let cursor = "0";
    let terminal = false;
    let halted = false;
    let authNotified = false;
    let reconcileChain: Promise<void> = Promise.resolve();
    const controller = new AbortController();
    const signal = controller.signal;
    setStateRunId(runId);
    setRun(null);
    setConnection("loading");
    setLastEvent(null);
    setError(null);

    const isCurrent = () => active && !signal.aborted;

    const applyAuthoritative = (next: QualificationRun) => {
      if (next.run_id !== runId) {
        throw new Error("flock_run_authority_mismatch");
      }
      if (authoritative === null || next.revision >= authoritative.revision) {
        authoritative = next;
        setRun(next);
      }
      if (TERMINAL_STATUSES.has(next.status)) terminal = true;
    };

    const reconcile = (): Promise<void> => {
      reconcileChain = reconcileChain.then(async () => {
        if (!isCurrent() || terminal || halted) return;
        try {
          const next = await readRun(runId, signal);
          if (!isCurrent() || signal.aborted) return;
          applyAuthoritative(next);
          authNotified = false;
          setError(null);
        } catch (value) {
          if (!isCurrent() || signal.aborted || isAbortError(value)) return;
          setError(errorMessage(value));
          if (value instanceof ApiAuthError) {
            halted = true;
            if (!authNotified) {
              authNotified = true;
              try {
                onAuthRequiredRef.current?.();
              } catch {
                // Authentication UI callbacks cannot restart this loop.
              }
            }
          }
        }
      });
      return reconcileChain;
    };

    const loop = async () => {
      await reconcile();
      if (!isCurrent()) return;
      if (authoritative === null || terminal || halted) {
        setConnection("closed");
        return;
      }
      while (isCurrent() && !terminal && !halted) {
        setConnection("streaming");
        try {
          await readEvents(runId, {
            afterSequence: cursor,
            signal,
            onEvent: (event) => {
              if (!isCurrent()) return;
              cursor = event.sequence;
              setLastEvent(event);
              // SSE only accelerates: the GET reconcile decides the state.
              void reconcile();
            },
          });
        } catch (value) {
          if (!isCurrent() || signal.aborted || isAbortError(value)) return;
          if (value instanceof ApiAuthError) {
            setError(errorMessage(value));
            halted = true;
            if (!authNotified) {
              authNotified = true;
              try {
                onAuthRequiredRef.current?.();
              } catch {
                // Authentication UI callbacks cannot restart this loop.
              }
            }
            break;
          }
          setError(errorMessage(value));
        }
        if (!isCurrent() || terminal || halted) break;
        setConnection("reconnecting");
        // After any stream drop the durable GET is the authority again.
        await reconcile();
        if (!isCurrent() || terminal || halted) break;
        try {
          await reconnectDelay(reconnectDelayMs, signal);
        } catch {
          return;
        }
      }
      if (!isCurrent()) return;
      setConnection("closed");
    };

    const session = {
      refresh: async () => {
        await reconcile();
      },
    };
    activeSessionRef.current = session;
    void loop();
    return () => {
      active = false;
      controller.abort();
      if (activeSessionRef.current === session) {
        activeSessionRef.current = null;
      }
    };
  }, [enabled, readEvents, readRun, reconnectDelayMs, runId]);

  const ownsCurrentState = enabled && runId !== null && stateRunId === runId;
  const currentRun =
    ownsCurrentState && run?.run_id === runId ? run : null;
  const currentLastEvent =
    ownsCurrentState && lastEvent !== null ? lastEvent : null;
  return {
    run: currentRun,
    connection: ownsCurrentState
      ? connection
      : enabled && runId !== null
        ? "loading"
        : "idle",
    lastEvent: currentLastEvent,
    lastEventSequence: currentLastEvent?.sequence ?? "0",
    error: ownsCurrentState ? error : null,
    refresh,
  };
}

function reconnectDelay(
  milliseconds: number,
  signal: AbortSignal,
): Promise<void> {
  const bounded = Number.isFinite(milliseconds)
    ? Math.min(5_000, Math.max(0, milliseconds))
    : 750;
  if (signal.aborted) {
    return Promise.reject(new DOMException("Aborted", "AbortError"));
  }
  if (bounded === 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, bounded);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function isAbortError(value: unknown): boolean {
  return value instanceof DOMException && value.name === "AbortError";
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "flock_run_unavailable";
}
