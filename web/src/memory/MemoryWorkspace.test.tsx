import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MemoryLayerStatus } from "../types";
import {
  MemoryWorkspace,
  useMemoryWorkspace,
} from "./MemoryWorkspace";

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function Harness({ enabled }: { enabled: boolean }) {
  const controller = useMemoryWorkspace({
    enabled,
    activeRunId: null,
    onAuthRequired: () => undefined,
    onError: () => undefined,
    onNotice: () => undefined,
  });
  return (
    <>
      <button type="button" onClick={() => void controller.refresh()}>
        Refresh memory
      </button>
      <MemoryWorkspace controller={controller} />
    </>
  );
}

describe("MemoryWorkspace", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("owns memory inventory and learning reads only while enabled", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path =
          typeof input === "string" ? input : input.toString();
        requests.push(path);
        if (path === "/api/memory/layers") {
          return jsonResponse([]);
        }
        if (path === "/api/cognition/lessons?k=20") {
          return jsonResponse({ items: [] });
        }
        if (path === "/api/cognition/failures?k=20") {
          return jsonResponse({ items: [] });
        }
        if (path === "/api/memory/deltas?since=all") {
          return jsonResponse({
            summary: {
              total_deltas: 0,
              active_deltas: 0,
              activated_deltas: 0,
              never_activated: 0,
              useful_rate: 0,
              failure_rate: 0,
              rollback_rate: 0,
              never_activated_rate: 0,
              outcomes: {},
            },
            deltas: [],
            recommendations: [],
          });
        }
        if (path === "/api/learning/dashboard?since=all") {
          return jsonResponse({
            since: null,
            headline: {
              auto_activations: 0,
              rollbacks: 0,
              false_positive_rate: 0,
              activations_then_rolled_back: 0,
              average_time_to_rollback_hours: null,
            },
            layers: [],
          });
        }
        throw new Error(`unexpected_request:${path}`);
      }),
    );

    const rendered = render(<Harness enabled={false} />);
    expect(requests).toEqual([]);

    rendered.rerender(<Harness enabled />);

    expect(
      await screen.findByRole("heading", {
        name: "Layer health",
      }),
    ).toBeVisible();
    await waitFor(() => {
      expect(requests).toEqual(
        expect.arrayContaining([
          "/api/memory/layers",
          "/api/cognition/lessons?k=20",
          "/api/cognition/failures?k=20",
          "/api/memory/deltas?since=all",
          "/api/learning/dashboard?since=all",
        ]),
      );
    });
    expect(new Set(requests).size).toBe(requests.length);
  });

  it("serializes manual, interval, and disable refresh cancellation", async () => {
    const signals: AbortSignal[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (
          _input: RequestInfo | URL,
          init?: RequestInit,
        ): Promise<Response> => {
          const signal = init?.signal;
          if (!(signal instanceof AbortSignal)) {
            throw new Error("missing_refresh_abort_signal");
          }
          signals.push(signal);
          return await new Promise<Response>((_resolve, reject) => {
            signal.addEventListener(
              "abort",
              () => reject(new DOMException("Aborted", "AbortError")),
              { once: true },
            );
          });
        },
      ),
    );

    const rendered = render(<Harness enabled />);
    await waitFor(() => expect(signals).toHaveLength(5));
    const firstSignal = signals[0];
    expect(signals.every((signal) => signal === firstSignal)).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Refresh memory" }));

    await waitFor(() => expect(signals).toHaveLength(10));
    const secondSignal = signals[5];
    expect(firstSignal.aborted).toBe(true);
    expect(signals.slice(5).every((signal) => signal === secondSignal)).toBe(
      true,
    );
    expect(secondSignal.aborted).toBe(false);

    rendered.rerender(<Harness enabled={false} />);
    await waitFor(() => expect(secondSignal.aborted).toBe(true));
  });

  it("labels policy memory as gated authority", async () => {
    const layers: MemoryLayerStatus[] = [
      {
        layer: "policy",
        path: "/mem/policy.mv2",
        exists: true,
        ok: true,
        backend: "memvid",
      },
      {
        layer: "semantic",
        path: "/mem/semantic.mv2",
        exists: true,
        ok: true,
        backend: "memvid",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = typeof input === "string" ? input : input.toString();
        if (path === "/api/memory/layers") {
          return jsonResponse(layers);
        }
        if (path === "/api/cognition/lessons?k=20") {
          return jsonResponse({ items: [] });
        }
        if (path === "/api/cognition/failures?k=20") {
          return jsonResponse({ items: [] });
        }
        if (path === "/api/memory/deltas?since=all") {
          return jsonResponse({
            summary: {
              total_deltas: 0,
              active_deltas: 0,
              activated_deltas: 0,
              never_activated: 0,
              useful_rate: 0,
              failure_rate: 0,
              rollback_rate: 0,
              never_activated_rate: 0,
              outcomes: {},
            },
            deltas: [],
            recommendations: [],
          });
        }
        if (path === "/api/learning/dashboard?since=all") {
          return jsonResponse({
            since: null,
            headline: {
              auto_activations: 0,
              rollbacks: 0,
              false_positive_rate: 0,
              activations_then_rolled_back: 0,
              average_time_to_rollback_hours: null,
            },
            layers: [],
          });
        }
        throw new Error(`unexpected_request:${path}`);
      }),
    );

    render(<Harness enabled />);

    const region = await screen.findByRole("region", {
      name: "Layer health",
    });
    const policyRow = await within(region).findByRole("row", {
      name: /policy/i,
    });
    expect(policyRow).toHaveTextContent(
      "Manual or repeated validated evidence required",
    );
    // Ordinary learning layers must NOT be presented as policy authority.
    const semanticRow = within(region).getByRole("row", {
      name: /semantic/i,
    });
    expect(semanticRow).not.toHaveTextContent(
      "Manual or repeated validated evidence required",
    );
  });
});
