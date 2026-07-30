import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  deleteJson,
  getJson,
  postJson,
  putJson,
  subscribeJsonEvents
} from "../api";
import { apiAuthHeaders, getApiToken, setApiToken } from "../auth";
import {
  DESKTOP_RUNTIME_MARKER_KEY,
  runtimeTransport,
  type DesktopRuntimeMarker
} from "./runtimeTransport";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function installDesktopMarker(
  marker: unknown = Object.freeze({
    schema: "kestrel.desktop.runtime.v1",
    baseUrl: "http://127.0.0.1:43123/",
    generation: 3
  } satisfies DesktopRuntimeMarker)
): void {
  Object.defineProperty(globalThis, DESKTOP_RUNTIME_MARKER_KEY, {
    configurable: true,
    enumerable: false,
    writable: false,
    value: marker
  });
}

function removeDesktopMarker(): void {
  Reflect.deleteProperty(globalThis, DESKTOP_RUNTIME_MARKER_KEY);
}

function headersFromCall(call: unknown[]): Headers {
  const init = call[1] as RequestInit | undefined;
  return new Headers(init?.headers);
}

describe("runtime transport", () => {
  beforeEach(() => {
    removeDesktopMarker();
    sessionStorage.clear();
    localStorage.clear();
  });

  afterEach(() => {
    removeDesktopMarker();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    sessionStorage.clear();
    localStorage.clear();
  });

  it("routes Desktop verbs and SSE to the exact origin without token storage or auth headers", async () => {
    sessionStorage.setItem("kestrel.apiToken", "browser-token-must-be-ignored");
    installDesktopMarker();
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const removeItem = vi.spyOn(Storage.prototype, "removeItem");
    const encoder = new TextEncoder();
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ verb: "GET" }))
      .mockResolvedValueOnce(jsonResponse({ verb: "POST" }))
      .mockResolvedValueOnce(jsonResponse({ verb: "PUT" }))
      .mockResolvedValueOnce(jsonResponse({ verb: "DELETE" }))
      .mockResolvedValueOnce(
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  'event: run.updated\ndata: {"type":"run.updated","id":7}\n\n'
                )
              );
              controller.close();
            }
          }),
          { status: 200, headers: { "Content-Type": "text/event-stream" } }
        )
      );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal(
      "EventSource",
      class {
        constructor() {
          throw new Error("Desktop SSE must not use EventSource");
        }
      }
    );

    await expect(getJson("/api/health")).resolves.toEqual({ verb: "GET" });
    await expect(postJson("/api/runs", { goal: "test" })).resolves.toEqual({
      verb: "POST"
    });
    await expect(putJson("/api/runtime/settings", { revision: 1 })).resolves.toEqual({
      verb: "PUT"
    });
    await expect(deleteJson("/api/runs/7")).resolves.toEqual({
      verb: "DELETE"
    });
    const event = new Promise<Record<string, unknown>>((resolve, reject) => {
      subscribeJsonEvents<Record<string, unknown>>(
        "/api/events",
        ["run.updated"],
        resolve,
        reject
      );
    });
    await expect(event).resolves.toEqual({ type: "run.updated", id: 7 });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:43123/api/health",
      "http://127.0.0.1:43123/api/runs",
      "http://127.0.0.1:43123/api/runtime/settings",
      "http://127.0.0.1:43123/api/runs/7",
      "http://127.0.0.1:43123/api/events"
    ]);
    expect(fetchMock.mock.calls.map(headersFromCall)).toEqual(
      expect.arrayContaining([
        expect.not.objectContaining({
          Authorization: expect.anything()
        })
      ])
    );
    for (const call of fetchMock.mock.calls) {
      const headers = headersFromCall(call);
      expect(headers.has("authorization")).toBe(false);
      expect(headers.has("x-kestrel-api-key")).toBe(false);
    }
    expect(getItem).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
    expect(removeItem).not.toHaveBeenCalled();
  });

  it("strips renderer auth headers and rejects credentialed fetches in Desktop mode", async () => {
    installDesktopMarker();
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    const transport = runtimeTransport(() => ({
      Authorization: "Bearer browser-token"
    }));

    await transport.fetch("/api/health", {
      headers: {
        AUTHORIZATION: "Bearer renderer-controlled",
        "x-KESTREL-api-KEY": "renderer-controlled",
        Accept: "application/json"
      }
    });
    const headers = headersFromCall(fetchMock.mock.calls[0] ?? []);
    expect(headers.get("accept")).toBe("application/json");
    expect(headers.has("authorization")).toBe(false);
    expect(headers.has("x-kestrel-api-key")).toBe(false);

    await expect(
      transport.fetch("/api/health", { credentials: "include" })
    ).rejects.toThrow("desktop_runtime_request_invalid");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    {
      label: "unfrozen",
      marker: {
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://127.0.0.1:43123/",
        generation: 1
      }
    },
    {
      label: "wrong schema",
      marker: Object.freeze({
        schema: "kestrel.desktop.runtime.v2",
        baseUrl: "http://127.0.0.1:43123/",
        generation: 1
      })
    },
    {
      label: "extra authority field",
      marker: Object.freeze({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://127.0.0.1:43123/",
        generation: 1,
        token: "renderer-token"
      })
    },
    {
      label: "localhost",
      marker: Object.freeze({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://localhost:43123/",
        generation: 1
      })
    },
    {
      label: "missing port",
      marker: Object.freeze({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://127.0.0.1/",
        generation: 1
      })
    },
    {
      label: "base path",
      marker: Object.freeze({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://127.0.0.1:43123/root",
        generation: 1
      })
    },
    {
      label: "credentials",
      marker: Object.freeze({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://user@127.0.0.1:43123/",
        generation: 1
      })
    },
    {
      label: "zero generation",
      marker: Object.freeze({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://127.0.0.1:43123/",
        generation: 0
      })
    },
    {
      label: "non-object",
      marker: "desktop"
    }
  ])("fails closed for a $label Desktop marker", async ({ marker }) => {
    sessionStorage.setItem("kestrel.apiToken", "browser-token-must-not-leak");
    installDesktopMarker(marker);
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(getJson("/api/health")).rejects.toThrow(
      "desktop_runtime_marker_invalid"
    );
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getItem).not.toHaveBeenCalled();
  });

  it.each([
    "/health",
    "/apiary",
    "//evil.example/api/health",
    "http://127.0.0.1:43124/api/health",
    "https://127.0.0.1:43123/api/health",
    "http://user@127.0.0.1:43123/api/health",
    "/api/health#fragment",
    "/api/%2e%2e/private"
  ])("rejects a Desktop path or origin escape without fetching: %s", async (path) => {
    installDesktopMarker();
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(runtimeTransport().fetch(path)).rejects.toThrow(
      "desktop_runtime_request_invalid"
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("allows the active absolute API URL but never serializes browser tokens into errors", async () => {
    installDesktopMarker();
    sessionStorage.setItem("kestrel.apiToken", "browser-token-must-not-leak");
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      runtimeTransport().fetch(
        "http://127.0.0.1:43123/api/health?detail=true"
      )
    ).resolves.toBeInstanceOf(Response);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:43123/api/health?detail=true"
    );
    await expect(runtimeTransport().fetch("/outside")).rejects.not.toThrow(
      /browser-token-must-not-leak/
    );
  });

  it("keeps Desktop auth helpers inert without touching either storage", () => {
    installDesktopMarker();
    sessionStorage.setItem("kestrel.apiToken", "browser-token-must-be-ignored");
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const removeItem = vi.spyOn(Storage.prototype, "removeItem");

    expect(getApiToken()).toBe("");
    expect(apiAuthHeaders()).toEqual({});
    setApiToken("renderer-token", true);

    expect(getItem).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
    expect(removeItem).not.toHaveBeenCalled();
  });

  it("preserves browser relative URLs, token headers, and EventSource fallback", async () => {
    sessionStorage.setItem("kestrel.apiToken", "browser-token");
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getJson("/api/health")).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/health");
    expect(headersFromCall(fetchMock.mock.calls[0] ?? []).get("authorization")).toBe(
      "Bearer browser-token"
    );

    sessionStorage.clear();
    const createdUrls: string[] = [];
    class BrowserEventSource {
      onmessage: ((event: MessageEvent) => void) | null = null;

      constructor(readonly url: string) {
        createdUrls.push(url);
      }

      addEventListener(): void {}

      close(): void {}
    }
    vi.stubGlobal("EventSource", BrowserEventSource);
    const stop = subscribeJsonEvents(
      "/api/events",
      ["run.updated"],
      () => undefined,
      () => undefined
    );
    expect(createdUrls).toEqual(["/api/events"]);
    stop();
  });
});
