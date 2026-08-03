// @vitest-environment jsdom
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiAuthError } from "../../api";
import type { QualificationEvent, QualificationRun } from "./types";
import {
  useQualificationRun,
  type QualificationEventReader,
  type QualificationRunReader,
} from "./useQualificationRun";

const runId = `qual_${"a".repeat(24)}`;
const digestA = "1".repeat(64);
const digestB = "2".repeat(64);
const digestC = "3".repeat(64);
const digestD = "4".repeat(64);
const digestE = "5".repeat(64);
const digestF = "6".repeat(64);
const digestH = "8".repeat(64);
const digestI = "9".repeat(64);
const digestJ = "a".repeat(64);

function typedRun(
  status: QualificationRun["status"],
  revision: number,
): QualificationRun {
  return {
    run_id: runId,
    status,
    revision,
    owner_principal: "owner:local-runtime:v1",
    scope_digest: digestA,
    corpus_digest: digestD,
    target_digest: digestB,
    price_digest: digestH,
    policy_digest: digestC,
    learned_digest: digestF,
    project_authority_digest: digestE,
    thresholds_digest: digestI,
    build_digest: digestJ,
    caps: {
      max_spend_micros: 50_000_000,
      max_spend_usd: "50.00",
      effective_stop_cap_micros: 50_000_000,
      effective_stop_cap_usd: "50.00",
      attempt_ceiling_micros: 5_000_000,
      attempt_ceiling_usd: "5.00",
    },
    spend: {
      actual_spend_micros: 0,
      actual_spend_usd: "0.00",
      unresolved_reserve_micros: 0,
      inflight_reserve_micros: 0,
    },
    blockers: [],
    created_at: "2026-08-01T12:00:00+00:00",
    updated_at: "2026-08-01T12:00:01+00:00",
    started_at: "2026-08-01T12:00:01+00:00",
    finished_at:
      status === "completed" || status === "cancelled" || status === "failed"
        ? "2026-08-01T12:00:02+00:00"
        : null,
    terminal_reason: status === "completed" ? "qualification_complete" : null,
  };
}

function progressEvent(sequence: string): QualificationEvent {
  return {
    sequence,
    event_type: "budget_projection_overrun",
    payload: {
      attempt_id: "att-1",
      reserve_micros: 100,
      actual_micros: 200,
      scope_digest: digestA,
    },
    created_at: "2026-08-01T12:00:01+00:00",
  };
}

function pendingStream(
  calls: { afterSequence: string | undefined }[],
): QualificationEventReader {
  return (_id, options) => {
    calls.push({ afterSequence: options.afterSequence });
    return new Promise((_, reject) => {
      options.signal.addEventListener("abort", () => {
        reject(new DOMException("Aborted", "AbortError"));
      });
    });
  };
}

describe("useQualificationRun", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads the authoritative run before opening the event stream", async () => {
    const getRun: QualificationRunReader = vi.fn(async () =>
      typedRun("running", 2),
    );
    const streamCalls: { afterSequence: string | undefined }[] = [];
    const readEvents = pendingStream(streamCalls);

    const { result, unmount } = renderHook(() =>
      useQualificationRun(runId, { getRun, readEvents, reconnectDelayMs: 0 }),
    );

    await waitFor(() => {
      expect(result.current.run?.status).toBe("running");
    });
    await waitFor(() => {
      expect(result.current.connection).toBe("streaming");
    });
    expect(streamCalls[0]?.afterSequence).toBe("0");
    unmount();
  });

  it("treats GET as the authority after a reconnect", async () => {
    const reads: QualificationRun["status"][] = [];
    const getRun: QualificationRunReader = vi.fn(async () => {
      const status = reads.length === 0 ? "running" : "paused";
      reads.push(status);
      return typedRun(status, reads.length === 1 ? 2 : 3);
    });
    const streamCalls: { afterSequence: string | undefined }[] = [];
    let firstStream = true;
    const readEvents: QualificationEventReader = (_id, options) => {
      streamCalls.push({ afterSequence: options.afterSequence });
      if (firstStream) {
        firstStream = false;
        // The event only accelerates; it carries no run projection.
        options.onEvent(progressEvent("1"));
        return Promise.resolve();
      }
      return new Promise((_, reject) => {
        options.signal.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    };

    const { result, unmount } = renderHook(() =>
      useQualificationRun(runId, { getRun, readEvents, reconnectDelayMs: 0 }),
    );

    await waitFor(() => {
      expect(result.current.run?.status).toBe("paused");
    });
    // The paused state came from the authoritative GET, not the SSE frame.
    expect(reads).toContain("paused");
    expect(result.current.run?.revision).toBe(3);
    expect(result.current.lastEvent?.sequence).toBe("1");
    // The stream reconnects from the persisted cursor.
    await waitFor(() => {
      expect(streamCalls.length).toBeGreaterThanOrEqual(2);
    });
    expect(streamCalls[1]?.afterSequence).toBe("1");
    await waitFor(() => {
      expect(result.current.connection).toBe("streaming");
    });
    unmount();
  });

  it("closes without streaming when the authoritative run is terminal", async () => {
    const getRun: QualificationRunReader = vi.fn(async () =>
      typedRun("completed", 4),
    );
    const readEvents: QualificationEventReader = vi.fn(pendingStream([]));

    const { result, unmount } = renderHook(() =>
      useQualificationRun(runId, { getRun, readEvents, reconnectDelayMs: 0 }),
    );

    await waitFor(() => {
      expect(result.current.connection).toBe("closed");
    });
    expect(result.current.run?.status).toBe("completed");
    expect(readEvents).not.toHaveBeenCalled();
    unmount();
  });

  it("surfaces auth failures and stops reconnecting", async () => {
    const onAuthRequired = vi.fn();
    const getRun: QualificationRunReader = vi.fn(async () => {
      throw new ApiAuthError();
    });
    const readEvents: QualificationEventReader = vi.fn(pendingStream([]));

    const { result, unmount } = renderHook(() =>
      useQualificationRun(runId, {
        getRun,
        readEvents,
        reconnectDelayMs: 0,
        onAuthRequired,
      }),
    );

    await waitFor(() => {
      expect(result.current.connection).toBe("closed");
    });
    expect(result.current.run).toBeNull();
    expect(result.current.error).not.toBeNull();
    expect(onAuthRequired).toHaveBeenCalledTimes(1);
    expect(readEvents).not.toHaveBeenCalled();
    unmount();
  });

  it("stays idle when disabled", () => {
    const getRun: QualificationRunReader = vi.fn(async () =>
      typedRun("running", 2),
    );
    const readEvents: QualificationEventReader = vi.fn(pendingStream([]));

    const { result } = renderHook(() =>
      useQualificationRun(runId, { enabled: false, getRun, readEvents }),
    );

    expect(result.current.connection).toBe("idle");
    expect(result.current.run).toBeNull();
    expect(getRun).not.toHaveBeenCalled();
    expect(readEvents).not.toHaveBeenCalled();
  });
});
