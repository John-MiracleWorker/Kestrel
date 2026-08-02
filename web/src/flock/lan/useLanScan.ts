import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { ApiAuthError } from "../../api";
import {
  getLanScan,
  streamLanScanEvents,
} from "./api";
import type {
  LanClientScanStatus,
  LanEventSequence,
  LanScanDetail,
  LanScanEvent,
  LanScanEventStreamOptions,
} from "./types";

export type LanScanConnectionStatus =
  | "idle"
  | "loading"
  | "streaming"
  | "reconnecting"
  | "closed";

export type LanScanReader = (
  scanId: string,
  signal: AbortSignal,
) => Promise<LanScanDetail>;

export type LanScanEventReader = (
  scanId: string,
  options: LanScanEventStreamOptions,
) => Promise<void>;

export type UseLanScanOptions = Readonly<{
  enabled?: boolean;
  getScan?: LanScanReader;
  readEvents?: LanScanEventReader;
  reconnectDelayMs?: number;
  onAuthRequired?: () => void;
}>;

export type LanScanHookState = Readonly<{
  scan: LanScanDetail | null;
  status: LanClientScanStatus;
  connection: LanScanConnectionStatus;
  lastEvent: LanScanEvent | null;
  lastEventSequence: LanEventSequence;
  error: string | null;
  refresh: () => Promise<void>;
}>;

type ActiveSession = {
  scanId: string;
  refresh: () => Promise<LanScanDetail | null>;
};

type ReconcileWaiter = {
  resolve: (value: LanScanDetail | null) => void;
  reject: (reason: unknown) => void;
};

const TERMINAL_STATUSES = new Set([
  "cancelled",
  "completed",
  "failed",
  "interrupted",
]);
const MAX_EVENT_SEQUENCE = 9_223_372_036_854_775_807n;

const defaultGetScan: LanScanReader = (scanId, signal) =>
  getLanScan(scanId, signal);

export function useLanScan(
  scanId: string | null,
  {
    enabled = true,
    getScan: readScan = defaultGetScan,
    readEvents = streamLanScanEvents,
    reconnectDelayMs = 750,
    onAuthRequired,
  }: UseLanScanOptions = {},
): LanScanHookState {
  const [stateScanId, setStateScanId] = useState<string | null>(null);
  const [scan, setScan] = useState<LanScanDetail | null>(null);
  const [connection, setConnection] =
    useState<LanScanConnectionStatus>("idle");
  const [lastEvent, setLastEvent] = useState<LanScanEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const activeSessionRef = useRef<ActiveSession | null>(null);
  const onAuthRequiredRef = useRef(onAuthRequired);

  useEffect(() => {
    onAuthRequiredRef.current = onAuthRequired;
  }, [onAuthRequired]);

  const refresh = useCallback(async () => {
    const session = activeSessionRef.current;
    if (session === null) return;
    await session.refresh();
  }, []);

  useEffect(() => {
    if (!enabled || scanId === null) {
      activeSessionRef.current = null;
      setStateScanId(null);
      setScan(null);
      setConnection("idle");
      setLastEvent(null);
      setError(null);
      return;
    }

    let active = true;
    let authoritative: LanScanDetail | null = null;
    let cursor: LanEventSequence = "0";
    let terminal = false;
    let suspended = false;
    let authNotified = false;
    let reconcileRequested = false;
    let reconcileRunning = false;
    const reconcileWaiters: ReconcileWaiter[] = [];
    let getController: AbortController | null = null;
    let streamController: AbortController | null = null;
    let streamEpoch = 0;
    let streamLoop: Promise<void> | null = null;
    const sessionController = new AbortController();
    const sessionSignal = sessionController.signal;
    setStateScanId(scanId);
    setScan(null);
    setConnection("loading");
    setLastEvent(null);
    setError(null);

    const isCurrent = () => active && !sessionSignal.aborted;

    const stopStream = () => {
      streamEpoch += 1;
      streamController?.abort();
      streamController = null;
    };

    const suspend = (value: unknown) => {
      if (!isCurrent()) return;
      suspended = true;
      reconcileRequested = false;
      stopStream();
      getController?.abort();
      setError(errorMessage(value));
      setConnection("closed");
      if (value instanceof ApiAuthError && !authNotified) {
        authNotified = true;
        try {
          onAuthRequiredRef.current?.();
        } catch {
          // Authentication UI callbacks cannot restart this automatic loop.
        }
      }
    };

    const applyAuthoritative = (next: LanScanDetail): LanScanDetail => {
      if (next.scan_id !== scanId) {
        throw new Error("lan_scan_authority_mismatch");
      }
      if (
        authoritative !== null &&
        next.revision === authoritative.revision &&
        next.status !== authoritative.status
      ) {
        throw new Error("lan_scan_revision_conflict");
      }
      if (
        authoritative === null ||
        next.revision > authoritative.revision
      ) {
        authoritative = next;
        setScan(next);
      }
      authNotified = false;
      setError(null);
      if (
        authoritative !== null &&
        TERMINAL_STATUSES.has(authoritative.status)
      ) {
        terminal = true;
        reconcileRequested = false;
        stopStream();
        setConnection("closed");
      }
      return authoritative;
    };

    const readAuthoritative = async (): Promise<LanScanDetail | null> => {
      const controller = new AbortController();
      getController = controller;
      try {
        const next = await readScan(scanId, controller.signal);
        if (!isCurrent() || controller.signal.aborted) return authoritative;
        return applyAuthoritative(next);
      } catch (value) {
        if (!isCurrent() || controller.signal.aborted || isAbortError(value)) {
          return authoritative;
        }
        if (value instanceof ApiAuthError || isProtocolError(value)) {
          suspend(value);
        } else {
          setError(errorMessage(value));
        }
        throw value;
      } finally {
        if (getController === controller) getController = null;
      }
    };

    const resolveReconcileWaiters = (value: LanScanDetail | null) => {
      for (const waiter of reconcileWaiters.splice(0)) {
        waiter.resolve(value);
      }
    };

    const rejectReconcileWaiters = (reason: unknown) => {
      for (const waiter of reconcileWaiters.splice(0)) {
        waiter.reject(reason);
      }
    };

    const drainReconciliations = async (): Promise<void> => {
      if (reconcileRunning) return;
      reconcileRunning = true;
      try {
        while (
          isCurrent() &&
          !terminal &&
          !suspended &&
          reconcileRequested
        ) {
          reconcileRequested = false;
          const batch = reconcileWaiters.splice(0);
          try {
            const result = await readAuthoritative();
            for (const waiter of batch) waiter.resolve(result);
          } catch (value) {
            for (const waiter of batch) waiter.reject(value);
            reconcileRequested = false;
            rejectReconcileWaiters(value);
            return;
          }
        }
        reconcileRequested = false;
        resolveReconcileWaiters(authoritative);
      } finally {
        reconcileRunning = false;
        if (
          reconcileRequested &&
          isCurrent() &&
          !terminal &&
          !suspended
        ) {
          void drainReconciliations();
        }
      }
    };

    const requestReconcile = (
      allowSuspended = false,
    ): Promise<LanScanDetail | null> => {
      if (!isCurrent() || terminal) return Promise.resolve(authoritative);
      if (suspended) {
        if (!allowSuspended) return Promise.resolve(authoritative);
        suspended = false;
        setConnection("loading");
      }
      reconcileRequested = true;
      const result = new Promise<LanScanDetail | null>((resolve, reject) => {
        reconcileWaiters.push({ resolve, reject });
      });
      void drainReconciliations();
      return result;
    };

    const runStreamLoop = async () => {
      while (isCurrent() && !terminal && !suspended) {
        const controller = new AbortController();
        streamController = controller;
        const epoch = ++streamEpoch;
        setConnection("streaming");
        try {
          await readEvents(scanId, {
            afterSequence: cursor,
            signal: controller.signal,
            onEvent: (next) => {
              if (
                !isCurrent() ||
                controller.signal.aborted ||
                epoch !== streamEpoch
              ) {
                return;
              }
              if (
                next.scan_id !== scanId ||
                !isCanonicalEventSequence(next.sequence)
              ) {
                suspend(new Error("lan_event_stream_invalid"));
                return;
              }
              if (!isLaterSequence(next.sequence, cursor)) return;
              cursor = next.sequence;
              setLastEvent(next);
              void requestReconcile().catch(() => undefined);
            },
          });
        } catch (value) {
          if (!isCurrent() || terminal || suspended) return;
          if (controller.signal.aborted || isAbortError(value)) return;
          if (value instanceof ApiAuthError || isProtocolError(value)) {
            suspend(value);
            return;
          }
          setError(errorMessage(value));
        } finally {
          if (streamController === controller) streamController = null;
        }
        if (!isCurrent() || terminal || suspended) return;

        setConnection("reconnecting");
        try {
          await requestReconcile();
        } catch {
          // Preserve the last authoritative scan and retry the acceleration channel.
        }
        if (!isCurrent() || terminal || suspended) return;
        try {
          await reconnectDelay(reconnectDelayMs, sessionSignal);
        } catch {
          return;
        }
      }
    };

    const ensureStreamLoop = () => {
      if (
        streamLoop !== null ||
        !isCurrent() ||
        terminal ||
        suspended ||
        authoritative === null
      ) {
        return;
      }
      const work = runStreamLoop();
      streamLoop = work.finally(() => {
        streamLoop = null;
        if (
          isCurrent() &&
          !terminal &&
          !suspended &&
          authoritative !== null
        ) {
          ensureStreamLoop();
        }
      });
    };

    const session: ActiveSession = {
      scanId,
      refresh: async () => {
        const wasSuspended = suspended;
        try {
          const next = await requestReconcile(true);
          if (next !== null && !terminal && !suspended) ensureStreamLoop();
          return next;
        } catch (value) {
          if (wasSuspended && isCurrent() && !suspended) {
            suspended = true;
            setConnection("closed");
          }
          throw value;
        }
      },
    };
    activeSessionRef.current = session;

    void requestReconcile()
      .then((initial) => {
        if (initial !== null && !terminal && !suspended) ensureStreamLoop();
      })
      .catch(() => {
        if (isCurrent() && !suspended) setConnection("closed");
      });
    return () => {
      active = false;
      sessionController.abort();
      getController?.abort();
      stopStream();
      if (activeSessionRef.current === session) {
        activeSessionRef.current = null;
      }
    };
  }, [enabled, readEvents, readScan, reconnectDelayMs, scanId]);

  const ownsCurrentState =
    enabled && scanId !== null && stateScanId === scanId;
  const currentScan =
    ownsCurrentState && scan?.scan_id === scanId ? scan : null;
  const currentLastEvent =
    ownsCurrentState && lastEvent?.scan_id === scanId ? lastEvent : null;
  return {
    scan: currentScan,
    status: currentScan?.status ?? "unknown",
    connection: ownsCurrentState
      ? connection
      : enabled && scanId !== null
        ? "loading"
        : "idle",
    lastEvent: currentLastEvent,
    lastEventSequence: currentLastEvent?.sequence ?? "0",
    error: ownsCurrentState ? error : null,
    refresh,
  };
}

function isLaterSequence(
  candidate: LanEventSequence,
  current: LanEventSequence,
): boolean {
  if (
    !isCanonicalEventSequence(candidate) ||
    !isCanonicalEventSequence(current)
  ) {
    return false;
  }
  try {
    return BigInt(candidate) > BigInt(current);
  } catch {
    return false;
  }
}

function isCanonicalEventSequence(value: string): boolean {
  if (!/^(?:0|[1-9][0-9]{0,18})$/.test(value)) return false;
  try {
    return BigInt(value) <= MAX_EVENT_SEQUENCE;
  } catch {
    return false;
  }
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
  return value instanceof Error ? value.message : "lan_scan_unavailable";
}

function isProtocolError(value: unknown): boolean {
  return (
    value instanceof Error &&
    (/^lan_[a-z0-9_]*invalid$/.test(value.message) ||
      value.message === "lan_scan_authority_mismatch" ||
      value.message === "lan_scan_revision_conflict")
  );
}
