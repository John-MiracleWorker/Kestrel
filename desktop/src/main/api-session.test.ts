import { describe, expect, it, vi } from "vitest";
import {
  installDesktopApiSession,
  type ApiSessionRequestDetails,
  type ApiSessionWebContents,
  type ApiSessionWebRequest
} from "./api-session";

class FakeWebContents implements ApiSessionWebContents {
  destroyed = false;
  private readonly destroyedListeners: Array<() => void> = [];

  constructor(readonly id: number) {}

  isDestroyed(): boolean {
    return this.destroyed;
  }

  once(event: "destroyed", listener: () => void): this {
    if (event === "destroyed") {
      this.destroyedListeners.push(listener);
    }
    return this;
  }

  destroy(): void {
    if (this.destroyed) {
      return;
    }
    this.destroyed = true;
    for (const listener of this.destroyedListeners) {
      listener();
    }
  }
}

function harness(): {
  authority: ReturnType<typeof installDesktopApiSession>;
  webRequest: ApiSessionWebRequest;
  filter: { urls: string[] };
  send(
    webContents: FakeWebContents,
    overrides?: Partial<ApiSessionRequestDetails>
  ): Record<string, string>;
} {
  let filter: { urls: string[] } | undefined;
  let listener:
    | Parameters<ApiSessionWebRequest["onBeforeSendHeaders"]>[1]
    | undefined;
  const webRequest: ApiSessionWebRequest = {
    onBeforeSendHeaders(nextFilter, nextListener) {
      filter = nextFilter;
      listener = nextListener;
    }
  };
  const authority = installDesktopApiSession(webRequest);
  const send = (
    webContents: FakeWebContents,
    overrides: Partial<ApiSessionRequestDetails> = {}
  ): Record<string, string> => {
    if (listener === undefined) {
      throw new Error("request listener was not installed");
    }
    let result: { requestHeaders: Record<string, string> } | undefined;
    listener(
      {
        id: 1,
        url: "http://127.0.0.1:43123/api/health",
        method: "GET",
        webContentsId: webContents.id,
        webContents,
        frame: { url: "kestrel://app/index.html" },
        requestHeaders: { Accept: "application/json" },
        ...overrides
      },
      (response) => {
        result = response;
      }
    );
    if (result === undefined) {
      throw new Error("request callback was not called");
    }
    return result.requestHeaders;
  };
  if (filter === undefined) {
    throw new Error("request filter was not installed");
  }
  return { authority, webRequest, filter, send };
}

describe("Desktop API session authority", () => {
  it("installs one all-request hook and injects only after stripping renderer auth", () => {
    const { authority, filter, send } = harness();
    const renderer = new FakeWebContents(17);
    authority.activate({
      baseUrl: "http://127.0.0.1:43123/",
      apiToken: "main-process-token",
      generation: 1
    });
    authority.bindRenderer(renderer);

    const headers = send(renderer, {
      requestHeaders: {
        Accept: "application/json",
        aUtHoRiZaTiOn: "Bearer renderer-controlled",
        "X-kEsTrEl-ApI-kEy": "renderer-controlled"
      }
    });

    expect(filter).toEqual({ urls: ["<all_urls>"] });
    expect(headers).toEqual({
      Accept: "application/json",
      Authorization: "Bearer main-process-token"
    });
  });

  it.each([
    {
      name: "missing frame",
      request: { frame: null }
    },
    {
      name: "HTTP frame with forged referrer",
      request: {
        frame: { url: "http://127.0.0.1:5173/" },
        referrer: "kestrel://app/index.html"
      }
    },
    {
      name: "lookalike frame host",
      request: { frame: { url: "kestrel://app.evil/index.html" } }
    },
    {
      name: "credentialed frame",
      request: { frame: { url: "kestrel://user@app/index.html" } }
    },
    {
      name: "ported frame",
      request: { frame: { url: "kestrel://app:43123/index.html" } }
    },
    {
      name: "wrong target port",
      request: { url: "http://127.0.0.1:43124/api/health" }
    },
    {
      name: "non-loopback lookalike",
      request: { url: "http://127.0.0.1.evil:43123/api/health" }
    },
    {
      name: "HTTPS target",
      request: { url: "https://127.0.0.1:43123/api/health" }
    },
    {
      name: "non-API path",
      request: { url: "http://127.0.0.1:43123/apiary" }
    },
    {
      name: "CORS preflight",
      request: { method: "options" }
    }
  ])("strips auth and refuses a $name request", ({ request }) => {
    const { authority, send } = harness();
    const renderer = new FakeWebContents(23);
    authority.activate({
      baseUrl: "http://127.0.0.1:43123/",
      apiToken: "main-process-token",
      generation: 7
    });
    authority.bindRenderer(renderer);

    const headers = send(renderer, {
      requestHeaders: {
        Accept: "application/json",
        AUTHORIZATION: "Bearer renderer-controlled",
        "x-kestrel-api-key": "renderer-controlled"
      },
      ...request
    });

    expect(headers).toEqual({ Accept: "application/json" });
  });

  it("requires both the registered ID and exact live WebContents object", () => {
    const { authority, send } = harness();
    const registered = new FakeWebContents(31);
    const substituted = new FakeWebContents(31);
    authority.activate({
      baseUrl: "http://127.0.0.1:43123/",
      apiToken: "main-process-token",
      generation: 1
    });
    authority.bindRenderer(registered);

    expect(
      send(substituted, {
        requestHeaders: { Authorization: "Bearer renderer-controlled" }
      })
    ).toEqual({});

    expect(
      send(registered, {
        webContentsId: 999,
        requestHeaders: { Authorization: "Bearer renderer-controlled" }
      })
    ).toEqual({});

    registered.destroyed = true;
    expect(
      send(registered, {
        requestHeaders: { Authorization: "Bearer renderer-controlled" }
      })
    ).toEqual({});
  });

  it("does not let destruction of an old object unbind a reused ID", () => {
    const { authority, send } = harness();
    const oldRenderer = new FakeWebContents(41);
    const replacement = new FakeWebContents(41);
    authority.activate({
      baseUrl: "http://127.0.0.1:43123/",
      apiToken: "main-process-token",
      generation: 1
    });
    authority.bindRenderer(oldRenderer);
    authority.bindRenderer(replacement);

    oldRenderer.destroy();
    expect(send(replacement).Authorization).toBe(
      "Bearer main-process-token"
    );

    replacement.destroy();
    expect(send(replacement)).not.toHaveProperty("Authorization");
  });

  it("invalidates stale generation bindings and ignores stale deactivation", () => {
    const { authority, send } = harness();
    const renderer = new FakeWebContents(53);
    authority.activate({
      baseUrl: "http://127.0.0.1:43123/",
      apiToken: "generation-one-token",
      generation: 1
    });
    authority.bindRenderer(renderer);
    expect(send(renderer).Authorization).toBe(
      "Bearer generation-one-token"
    );

    authority.deactivate(1);
    expect(send(renderer)).not.toHaveProperty("Authorization");
    authority.activate({
      baseUrl: "http://127.0.0.1:43124/",
      apiToken: "generation-two-token",
      generation: 2
    });
    expect(send(renderer, {
      url: "http://127.0.0.1:43124/api/health"
    })).not.toHaveProperty("Authorization");

    authority.bindRenderer(renderer);
    authority.deactivate(1);
    expect(
      send(renderer, {
        url: "http://127.0.0.1:43124/api/health"
      }).Authorization
    ).toBe("Bearer generation-two-token");

    authority.deactivate(2);
    expect(
      send(renderer, {
        url: "http://127.0.0.1:43124/api/health"
      })
    ).not.toHaveProperty("Authorization");
  });

  it("re-evaluates a redirected request and never carries the bearer token", () => {
    const { authority, send } = harness();
    const renderer = new FakeWebContents(61);
    authority.activate({
      baseUrl: "http://127.0.0.1:43123/",
      apiToken: "main-process-token",
      generation: 1
    });
    authority.bindRenderer(renderer);
    const first = send(renderer);
    expect(first.Authorization).toBe("Bearer main-process-token");

    const redirected = send(renderer, {
      url: "http://127.0.0.1:43124/api/health",
      requestHeaders: first
    });
    expect(redirected).toEqual({ Accept: "application/json" });
  });

  it("scrubs a prior injected token after the bound renderer is actually destroyed", () => {
    const { authority, send } = harness();
    const renderer = new FakeWebContents(67);
    authority.activate({
      baseUrl: "http://127.0.0.1:43123/",
      apiToken: "main-process-token",
      generation: 1
    });
    authority.bindRenderer(renderer);
    const injected = send(renderer);
    expect(injected.Authorization).toBe("Bearer main-process-token");

    renderer.destroy();
    const replayed = send(renderer, {
      url: "http://127.0.0.1:43124/redirected",
      frame: null,
      webContentsId: undefined,
      webContents: undefined,
      requestHeaders: {
        ...injected,
        "x-KESTREL-api-KEY": "renderer-controlled"
      }
    });

    expect(replayed).toEqual({ Accept: "application/json" });
  });

  it("scrubs prior authority headers when request provenance is absent", () => {
    const { authority, send } = harness();
    const renderer = new FakeWebContents(71);
    authority.activate({
      baseUrl: "http://127.0.0.1:43123/",
      apiToken: "main-process-token",
      generation: 1
    });
    authority.bindRenderer(renderer);
    const injected = send(renderer);
    expect(injected.Authorization).toBe("Bearer main-process-token");

    const replayed = send(renderer, {
      frame: null,
      webContentsId: undefined,
      webContents: undefined,
      requestHeaders: {
        ...injected,
        "X-Kestrel-API-Key": "renderer-controlled"
      }
    });

    expect(replayed).toEqual({ Accept: "application/json" });
  });

  it.each([
    "https://127.0.0.1:43123/",
    "http://localhost:43123/",
    "http://127.0.0.1/",
    "http://user@127.0.0.1:43123/",
    "http://127.0.0.1:43123/path",
    "http://127.0.0.1:43123/?query=1",
    "http://127.0.0.1:43123/#fragment"
  ])("rejects an invalid activation base URL without exposing its token: %s", (baseUrl) => {
    const { authority } = harness();
    const secret = "must-never-appear-in-errors";

    expect(() =>
      authority.activate({
        baseUrl,
        apiToken: secret,
        generation: 1
      })
    ).toThrow("desktop_api_session_activation_invalid");
    expect(JSON.stringify(authority)).not.toContain(secret);
  });
});
