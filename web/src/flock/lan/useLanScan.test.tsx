import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiAuthError } from "../../api";
import type {
  LanScanDetail,
  LanScanEvent,
  LanScanStatus,
} from "./types";
import {
  type LanScanEventReader,
  type LanScanReader,
  useLanScan,
} from "./useLanScan";

const scanId = `lan_${"3".repeat(32)}`;
const digest = `sha256:${"a".repeat(64)}`;

function scan(
  status: LanScanStatus = "running",
  revision = 2,
  identifier = scanId,
): LanScanDetail {
  return {
    scan_id: identifier,
    status,
    revision,
    confirmed_interface_id: `sha256:${"b".repeat(64)}`,
    network: "192.168.50.0/24",
    limits: {
      known_model_service_ports: [1_234, 8_000, 8_080, 11_434],
      max_active_hosts: 256,
      max_scan_concurrency: 16,
      tcp_connect_timeout_seconds: 0.75,
      http_probe_timeout_seconds: 2,
      total_scan_deadline_seconds: 45,
      max_probe_response_bytes: 262_144,
      max_discovered_models: 8,
      mdns_window_seconds: 2.5,
    },
    limits_digest: digest,
    preview_digest: digest,
    created_at: "2026-08-01T12:00:00Z",
    updated_at: "2026-08-01T12:00:01Z",
    started_at: "2026-08-01T12:00:01Z",
    finished_at:
      status === "running" ? null : "2026-08-01T12:00:02Z",
    cancel_reason: status === "cancelled" ? "owner_cancelled" : null,
    terminal_reason:
      status === "completed"
        ? "scan_complete"
        : status === "interrupted"
          ? "worker_interrupted"
          : status === "cancelled"
            ? "owner_cancelled"
            : null,
    candidate_count: status === "running" ? null : 1,
    error_count: status === "running" ? null : 0,
    timeout_count: status === "running" ? null : 0,
    terminal_receipt_digest: status === "running" ? null : digest,
    observations: [],
    observation_total_count: 0,
    observations_truncated: false,
  };
}

function event(
  sequence: number,
  eventType: "scan_progress" | "scan_completed" = "scan_progress",
  identifier = scanId,
): LanScanEvent {
  if (eventType === "scan_completed") {
    return {
      scan_id: identifier,
      sequence: String(sequence),
      event_type: "scan_completed",
      payload: {
        status: "completed",
        terminal_reason: "scan_complete",
        cancel_reason: null,
      },
      created_at: "2026-08-01T12:00:01Z",
    };
  }
  return {
    scan_id: identifier,
    sequence: String(sequence),
    event_type: "scan_progress",
    payload: {
      planned_count: 4,
      admitted_count: 4,
      completed_count: 2,
      persisted_observation_count: 1,
      error_category_counts: {},
      timeout_count: 0,
      mdns_status: "available",
    },
    created_at: "2026-08-01T12:00:01Z",
  };
}

function Probe({
  getScan,
  readEvents,
  scanIdentifier = scanId,
  onAuthRequired,
  reconnectDelayMs = 0,
}: {
  getScan: LanScanReader;
  readEvents: LanScanEventReader;
  scanIdentifier?: string;
  onAuthRequired?: () => void;
  reconnectDelayMs?: number;
}) {
  const state = useLanScan(scanIdentifier, {
    getScan,
    readEvents,
    reconnectDelayMs,
    onAuthRequired,
  });
  return (
    <>
      <output aria-label="scan identifier">{state.scan?.scan_id ?? ""}</output>
      <output aria-label="scan status">{state.status}</output>
      <output aria-label="connection status">{state.connection}</output>
      <output aria-label="scan revision">{state.scan?.revision ?? 0}</output>
      <output aria-label="event sequence">{state.lastEventSequence}</output>
      <output aria-label="scan error">{state.error ?? ""}</output>
      <button type="button" onClick={() => void state.refresh().catch(() => undefined)}>
        Refresh
      </button>
    </>
  );
}

function blockedUntilAbort(signal: AbortSignal): Promise<void> {
  return new Promise((_resolve, reject) => {
    signal.addEventListener(
      "abort",
      () => reject(new DOMException("Aborted", "AbortError")),
      { once: true },
    );
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

describe("useLanScan", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("refetches authoritative server status after a disconnected stream", async () => {
    const reads = [scan("running", 1), scan("running", 2)];
    const getScan = vi.fn<LanScanReader>(async () => reads.shift() ?? scan("running", 2));
    const sequences: string[] = [];
    const streamSignals: AbortSignal[] = [];
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      sequences.push(options.afterSequence);
      streamSignals.push(options.signal);
      if (sequences.length === 1) return;
      await blockedUntilAbort(options.signal);
    });

    const rendered = render(<Probe getScan={getScan} readEvents={readEvents} />);

    await waitFor(() => expect(getScan).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(readEvents).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("scan status")).toHaveTextContent("running");
    expect(screen.getByLabelText("scan revision")).toHaveTextContent("2");
    expect(screen.getByLabelText("connection status")).toHaveTextContent("streaming");
    expect(sequences).toEqual(["0", "0"]);

    rendered.unmount();
    expect(streamSignals.at(-1)?.aborted).toBe(true);
  });

  it("does not open the acceleration stream for an initially terminal scan", async () => {
    const getScan = vi.fn<LanScanReader>(async () => scan("completed", 4));
    const readEvents = vi.fn<LanScanEventReader>();

    render(<Probe getScan={getScan} readEvents={readEvents} />);

    await waitFor(() =>
      expect(screen.getByLabelText("scan status")).toHaveTextContent("completed"),
    );
    expect(screen.getByLabelText("connection status")).toHaveTextContent("closed");
    expect(readEvents).not.toHaveBeenCalled();
  });

  it("reconnects with the highest persisted event sequence", async () => {
    const getScan = vi.fn<LanScanReader>(async () => scan("running", 2));
    const sequences: string[] = [];
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      sequences.push(options.afterSequence);
      if (sequences.length === 1) {
        options.onEvent(event(7));
        return;
      }
      await blockedUntilAbort(options.signal);
    });

    render(<Probe getScan={getScan} readEvents={readEvents} />);

    await waitFor(() => expect(readEvents).toHaveBeenCalledTimes(2));
    expect(sequences).toEqual(["0", "7"]);
    expect(screen.getByLabelText("event sequence")).toHaveTextContent("7");
    expect(screen.getByLabelText("scan status")).toHaveTextContent("running");
  });

  it.each(["interrupted", "cancelled"] satisfies LanScanStatus[])(
    "never infers completion from a terminal event when GET says %s",
    async (authoritativeStatus) => {
      const authoritative = deferred<LanScanDetail>();
      let readCount = 0;
      const getScan = vi.fn<LanScanReader>(async () => {
        readCount += 1;
        if (readCount === 1) return scan("running", 1);
        return authoritative.promise;
      });
      const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
        options.onEvent(event(9, "scan_completed"));
      });

      render(<Probe getScan={getScan} readEvents={readEvents} />);

      await waitFor(() =>
        expect(screen.getByLabelText("event sequence")).toHaveTextContent("9"),
      );
      expect(screen.getByLabelText("scan status")).toHaveTextContent("running");

      authoritative.resolve(scan(authoritativeStatus, 2));
      await waitFor(() =>
        expect(screen.getByLabelText("scan status")).toHaveTextContent(
          authoritativeStatus,
        ),
      );
      expect(screen.getByLabelText("scan status")).not.toHaveTextContent("completed");
    },
  );

  it("closes a blocked stream when event reconciliation becomes terminal", async () => {
    const terminal = deferred<LanScanDetail>();
    let reads = 0;
    const getScan = vi.fn<LanScanReader>(async () => {
      reads += 1;
      if (reads === 1) return scan("running", 1);
      return terminal.promise;
    });
    const streamSignals: AbortSignal[] = [];
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      streamSignals.push(options.signal);
      options.onEvent(event(3, "scan_completed"));
      await blockedUntilAbort(options.signal);
    });

    render(<Probe getScan={getScan} readEvents={readEvents} />);
    await waitFor(() => expect(getScan).toHaveBeenCalledTimes(2));

    terminal.resolve(scan("completed", 2));
    await waitFor(() =>
      expect(screen.getByLabelText("scan status")).toHaveTextContent("completed"),
    );
    expect(screen.getByLabelText("connection status")).toHaveTextContent("closed");
    expect(streamSignals[0]?.aborted).toBe(true);
    expect(readEvents).toHaveBeenCalledTimes(1);
  });

  it("coalesces event bursts into serialized authoritative reads", async () => {
    const firstReconcile = deferred<LanScanDetail>();
    const queuedReconcile = deferred<LanScanDetail>();
    let readCount = 0;
    let activeReads = 0;
    let maximumActiveReads = 0;
    const getScan = vi.fn<LanScanReader>(async () => {
      readCount += 1;
      activeReads += 1;
      maximumActiveReads = Math.max(maximumActiveReads, activeReads);
      try {
        if (readCount === 1) return scan("running", 1);
        if (readCount === 2) return await firstReconcile.promise;
        return await queuedReconcile.promise;
      } finally {
        activeReads -= 1;
      }
    });
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      options.onEvent(event(4));
      options.onEvent(event(5));
      options.onEvent(event(6));
      await blockedUntilAbort(options.signal);
    });

    render(<Probe getScan={getScan} readEvents={readEvents} />);
    await waitFor(() => expect(getScan).toHaveBeenCalledTimes(2));
    expect(maximumActiveReads).toBe(1);

    firstReconcile.resolve(scan("running", 2));
    await waitFor(() => expect(getScan).toHaveBeenCalledTimes(3));
    expect(maximumActiveReads).toBe(1);

    queuedReconcile.resolve(scan("running", 3));
    await waitFor(() =>
      expect(screen.getByLabelText("scan revision")).toHaveTextContent("3"),
    );
    expect(getScan).toHaveBeenCalledTimes(3);
    expect(maximumActiveReads).toBe(1);
    expect(screen.getByLabelText("event sequence")).toHaveTextContent("6");
  });

  it("does not lose reconciliation queued during promise settlement", async () => {
    const settlingRead = deferred<LanScanDetail>();
    let reads = 0;
    const getScan = vi.fn<LanScanReader>(() => {
      reads += 1;
      if (reads === 1) return Promise.resolve(scan("running", 1));
      if (reads === 2) return settlingRead.promise;
      return Promise.resolve(scan("running", 3));
    });
    let onEvent!: (next: LanScanEvent) => void;
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      onEvent = options.onEvent;
      await blockedUntilAbort(options.signal);
    });

    render(<Probe getScan={getScan} readEvents={readEvents} />);
    await waitFor(() => expect(readEvents).toHaveBeenCalledTimes(1));
    act(() => onEvent(event(1)));
    await waitFor(() => expect(getScan).toHaveBeenCalledTimes(2));

    await act(async () => {
      settlingRead.resolve(scan("running", 2));
      queueMicrotask(() => queueMicrotask(() => onEvent(event(2))));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => expect(getScan).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.getByLabelText("scan revision")).toHaveTextContent("3"),
    );
    expect(screen.getByLabelText("event sequence")).toHaveTextContent("2");
  });

  it("aborts the open stream when an explicit refresh becomes terminal", async () => {
    const reads = [scan("running", 1), scan("completed", 2)];
    const getScan = vi.fn<LanScanReader>(async () => reads.shift() ?? scan("completed", 2));
    const streamSignals: AbortSignal[] = [];
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      streamSignals.push(options.signal);
      await blockedUntilAbort(options.signal);
    });

    render(<Probe getScan={getScan} readEvents={readEvents} />);
    await waitFor(() => expect(readEvents).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() =>
      expect(screen.getByLabelText("scan status")).toHaveTextContent("completed"),
    );
    expect(screen.getByLabelText("connection status")).toHaveTextContent("closed");
    expect(streamSignals).toHaveLength(1);
    expect(streamSignals[0]?.aborted).toBe(true);
    expect(readEvents).toHaveBeenCalledTimes(1);
  });

  it("suspends after one auth failure and requests credentials once", async () => {
    const getScan = vi.fn<LanScanReader>(async () => scan("running", 1));
    const onAuthRequired = vi.fn();
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      if (readEvents.mock.calls.length === 1) {
        throw new ApiAuthError("lan_auth_required");
      }
      await blockedUntilAbort(options.signal);
    });

    render(
      <Probe
        getScan={getScan}
        readEvents={readEvents}
        onAuthRequired={onAuthRequired}
      />,
    );

    await waitFor(() => expect(onAuthRequired).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText("connection status")).toHaveTextContent("closed");
    expect(screen.getByLabelText("scan error")).toHaveTextContent("lan_auth_required");
    expect(readEvents).toHaveBeenCalledTimes(1);
    expect(getScan).toHaveBeenCalledTimes(1);
  });

  it("re-arms auth notification after an explicit successful recovery", async () => {
    const getScan = vi.fn<LanScanReader>(async () => scan("running", 1));
    const onAuthRequired = vi.fn();
    const readEvents = vi.fn<LanScanEventReader>(async () => {
      throw new ApiAuthError(
        readEvents.mock.calls.length === 1
          ? "first_auth_required"
          : "second_auth_required",
      );
    });

    render(
      <Probe
        getScan={getScan}
        readEvents={readEvents}
        onAuthRequired={onAuthRequired}
      />,
    );
    await waitFor(() => expect(onAuthRequired).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(readEvents).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onAuthRequired).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("connection status")).toHaveTextContent("closed");
    expect(screen.getByLabelText("scan error")).toHaveTextContent(
      "second_auth_required",
    );
    expect(getScan).toHaveBeenCalledTimes(2);
  });

  it("suspends an initial auth failure before opening a stream", async () => {
    const getScan = vi.fn<LanScanReader>(async () => {
      throw new ApiAuthError("lan_auth_required");
    });
    const readEvents = vi.fn<LanScanEventReader>();
    const onAuthRequired = vi.fn();

    render(
      <Probe
        getScan={getScan}
        readEvents={readEvents}
        onAuthRequired={onAuthRequired}
      />,
    );

    await waitFor(() => expect(onAuthRequired).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText("scan status")).toHaveTextContent("unknown");
    expect(screen.getByLabelText("connection status")).toHaveTextContent("closed");
    expect(readEvents).not.toHaveBeenCalled();
    expect(getScan).toHaveBeenCalledTimes(1);
  });

  it("suspends instead of retrying a malformed event stream", async () => {
    const getScan = vi.fn<LanScanReader>(async () => scan("running", 1));
    const readEvents = vi.fn<LanScanEventReader>(async () => {
      throw new Error("lan_event_stream_invalid");
    });

    render(<Probe getScan={getScan} readEvents={readEvents} />);

    await waitFor(() =>
      expect(screen.getByLabelText("connection status")).toHaveTextContent("closed"),
    );
    expect(screen.getByLabelText("scan error")).toHaveTextContent(
      "lan_event_stream_invalid",
    );
    expect(readEvents).toHaveBeenCalledTimes(1);
    expect(getScan).toHaveBeenCalledTimes(1);
  });

  it("ignores a late response from a replaced scan session", async () => {
    const otherScanId = `lan_${"4".repeat(32)}`;
    const first = deferred<LanScanDetail>();
    const getScan = vi.fn<LanScanReader>(async (identifier) => {
      if (identifier === scanId) return first.promise;
      return scan("running", 7, otherScanId);
    });
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      await blockedUntilAbort(options.signal);
    });
    const rendered = render(
      <Probe getScan={getScan} readEvents={readEvents} scanIdentifier={scanId} />,
    );

    rendered.rerender(
      <Probe
        getScan={getScan}
        readEvents={readEvents}
        scanIdentifier={otherScanId}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("scan identifier")).toHaveTextContent(otherScanId),
    );

    first.resolve(scan("completed", 99, scanId));
    await Promise.resolve();
    expect(screen.getByLabelText("scan identifier")).toHaveTextContent(otherScanId);
    expect(screen.getByLabelText("scan revision")).toHaveTextContent("7");
    expect(screen.getByLabelText("scan status")).toHaveTextContent("running");
  });

  it("ignores callbacks retained by an obsolete stream generation", async () => {
    const callbacks: Array<(next: LanScanEvent) => void> = [];
    const getScan = vi.fn<LanScanReader>(async () => scan("running", 2));
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      callbacks.push(options.onEvent);
      if (callbacks.length === 1) return;
      await blockedUntilAbort(options.signal);
    });

    render(<Probe getScan={getScan} readEvents={readEvents} />);
    await waitFor(() => expect(readEvents).toHaveBeenCalledTimes(2));

    callbacks[0]?.(event(99));
    await Promise.resolve();
    expect(screen.getByLabelText("event sequence")).toHaveTextContent("0");

    callbacks[1]?.(event(7));
    await waitFor(() =>
      expect(screen.getByLabelText("event sequence")).toHaveTextContent("7"),
    );
  });

  it("fails closed when GET returns a different scan authority", async () => {
    const otherScanId = `lan_${"5".repeat(32)}`;
    const getScan = vi.fn<LanScanReader>(async () =>
      scan("running", 1, otherScanId),
    );
    const readEvents = vi.fn<LanScanEventReader>();

    render(<Probe getScan={getScan} readEvents={readEvents} />);

    await waitFor(() =>
      expect(screen.getByLabelText("scan error")).toHaveTextContent(
        "lan_scan_authority_mismatch",
      ),
    );
    expect(screen.getByLabelText("scan status")).toHaveTextContent("unknown");
    expect(screen.getByLabelText("connection status")).toHaveTextContent("closed");
    expect(readEvents).not.toHaveBeenCalled();
  });

  it("preserves the last running status when disconnect reconciliation fails", async () => {
    let reads = 0;
    const getScan = vi.fn<LanScanReader>(async () => {
      reads += 1;
      if (reads === 1) return scan("running", 1);
      throw new Error("status_temporarily_unavailable");
    });
    const readEvents = vi.fn<LanScanEventReader>(async (_id, options) => {
      if (readEvents.mock.calls.length === 1) return;
      await blockedUntilAbort(options.signal);
    });

    render(<Probe getScan={getScan} readEvents={readEvents} />);

    await waitFor(() => expect(readEvents).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("scan status")).toHaveTextContent("running");
    expect(screen.getByLabelText("scan revision")).toHaveTextContent("1");
    expect(screen.getByLabelText("scan error")).toHaveTextContent(
      "status_temporarily_unavailable",
    );
  });

  it("preserves explicit unknown when no authoritative scan can be read", async () => {
    const getScan = vi.fn<LanScanReader>(async () => {
      throw new Error("server status unavailable");
    });
    const readEvents = vi.fn<LanScanEventReader>();

    render(<Probe getScan={getScan} readEvents={readEvents} />);

    await waitFor(() =>
      expect(screen.getByLabelText("scan error")).toHaveTextContent(
        "server status unavailable",
      ),
    );
    expect(screen.getByLabelText("scan status")).toHaveTextContent("unknown");
    expect(screen.getByLabelText("connection status")).toHaveTextContent("closed");
    expect(readEvents).not.toHaveBeenCalled();
  });
});
