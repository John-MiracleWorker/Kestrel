import { describe, expect, it, vi } from "vitest";
import {
  DESKTOP_IPC_CHANNELS,
  type DesktopConnection,
  type DesktopUpdateStatus
} from "../contracts";
import {
  chooseCanonicalDirectory,
  installDesktopIpc,
  openReviewedExternalUrl,
  projectDesktopConnection,
  type DesktopIpcAdapters,
  type DesktopIpcEvent,
  type DesktopIpcMain,
  type DesktopIpcWebContents
} from "./ipc";

const readyConnection: DesktopConnection = {
  schema: "kestrel.desktop.connection.v1",
  state: "ready",
  generation: 3,
  baseUrl: "http://127.0.0.1:43123/",
  profileId: "default",
  sidecarVersion: "0.5.0",
  recovery: null
};
const unavailableUpdate: DesktopUpdateStatus = {
  schema: "kestrel.desktop.update.v1",
  state: "unavailable",
  reason: "not_configured"
};

class FakeWebContents implements DesktopIpcWebContents {
  destroyed = false;
  readonly sent: Array<{ channel: string; payload: unknown }> = [];
  readonly destroyedListeners: Array<() => void> = [];
  readonly mainFrame = { url: "kestrel://app/index.html" };

  constructor(readonly id: number) {}

  isDestroyed(): boolean {
    return this.destroyed;
  }

  once(event: "destroyed", listener: () => void): this {
    if (event === "destroyed") this.destroyedListeners.push(listener);
    return this;
  }

  send(channel: string, payload: unknown): void {
    this.sent.push({ channel, payload });
  }

  destroy(): void {
    this.destroyed = true;
    for (const listener of this.destroyedListeners) listener();
  }
}

function adapters(): DesktopIpcAdapters {
  return {
    readConnection: () => readyConnection,
    subscribeLifecycle: () => () => undefined,
    readUpdateStatus: () => unavailableUpdate,
    subscribeUpdateStatus: () => () => undefined,
    chooseProjectFolder: async () => ({ status: "cancelled" }),
    chooseStorageFolder: async () => ({ status: "cancelled" }),
    exportSupportBundle: async () => {
      throw new Error("must-not-leak");
    },
    getAppVersion: () => "0.5.0",
    openCredentialDialog: async () => {
      throw new Error("must-not-leak");
    },
    openExternalUrl: async () => undefined,
    performRecoveryAction: async () => {
      throw new Error("must-not-leak");
    },
    runtimeMarker: () => ({
      schema: "kestrel.desktop.runtime.v1",
      baseUrl: "http://127.0.0.1:43123/",
      generation: 3
    })
  };
}

function harness(adapterOverrides: Partial<DesktopIpcAdapters> = {}): {
  authority: ReturnType<typeof installDesktopIpc>;
  handlers: Map<
    string,
    (event: DesktopIpcEvent, request: unknown) => Promise<unknown>
  >;
  syncHandlers: Map<
    string,
    (event: DesktopIpcEvent, request: unknown) => void
  >;
} {
  const handlers = new Map<
    string,
    (event: DesktopIpcEvent, request: unknown) => Promise<unknown>
  >();
  const syncHandlers = new Map<
    string,
    (event: DesktopIpcEvent, request: unknown) => void
  >();
  const ipcMain: DesktopIpcMain = {
    handle(channel, listener) {
      handlers.set(channel, listener);
    },
    on(channel, listener) {
      syncHandlers.set(channel, listener);
    }
  };
  return {
    authority: installDesktopIpc(ipcMain, {
      ...adapters(),
      ...adapterOverrides
    }),
    handlers,
    syncHandlers
  };
}

function eventFor(
  webContents: FakeWebContents,
  overrides: Partial<DesktopIpcEvent> = {}
): DesktopIpcEvent {
  return {
    sender: webContents,
    senderFrame: webContents.mainFrame,
    ...overrides
  };
}

const connectionRequest = {
  schema: "kestrel.desktop.connection.request.v1"
};

describe("Desktop main IPC authority", () => {
  it("projects ready authority only when sidecar and runtime marker exactly agree", () => {
    const ready = {
      kind: "ready" as const,
      profileId: "default",
      baseUrl: "http://127.0.0.1:43123/",
      sidecarVersion: "0.5.0"
    };
    const marker = {
      schema: "kestrel.desktop.runtime.v1" as const,
      baseUrl: "http://127.0.0.1:43123/",
      generation: 9
    };

    expect(projectDesktopConnection(ready, marker)).toEqual({
      schema: "kestrel.desktop.connection.v1",
      state: "ready",
      generation: 9,
      baseUrl: "http://127.0.0.1:43123/",
      profileId: "default",
      sidecarVersion: "0.5.0",
      recovery: null
    });
    expect(projectDesktopConnection(ready, null)).toEqual({
      schema: "kestrel.desktop.connection.v1",
      state: "recovery",
      generation: null,
      baseUrl: null,
      profileId: null,
      sidecarVersion: null,
      recovery: { reason: "sidecar_unverified" }
    });
    expect(
      projectDesktopConnection(
        { kind: "starting" },
        marker
      )
    ).toEqual({
      schema: "kestrel.desktop.connection.v1",
      state: "starting",
      generation: null,
      baseUrl: null,
      profileId: null,
      sidecarVersion: null,
      recovery: null
    });
  });

  it("registers only explicit reviewed invoke and push channels", () => {
    const { handlers, syncHandlers } = harness();

    expect([...handlers.keys()].sort()).toEqual([
      DESKTOP_IPC_CHANNELS.appVersion,
      DESKTOP_IPC_CHANNELS.chooseProjectFolder,
      DESKTOP_IPC_CHANNELS.chooseStorageFolder,
      DESKTOP_IPC_CHANNELS.connection,
      DESKTOP_IPC_CHANNELS.credentialDialog,
      DESKTOP_IPC_CHANNELS.exportSupportBundle,
      DESKTOP_IPC_CHANNELS.openExternalUrl,
      DESKTOP_IPC_CHANNELS.recoveryAction,
      DESKTOP_IPC_CHANNELS.updateStatus
    ].sort());
    expect([...syncHandlers.keys()]).toEqual([
      DESKTOP_IPC_CHANNELS.runtimeBootstrap
    ]);
    expect(handlers.has(DESKTOP_IPC_CHANNELS.lifecycleEvent)).toBe(false);
    expect(handlers.has(DESKTOP_IPC_CHANNELS.updateStatusEvent)).toBe(false);
  });

  it("requires the exact registered live sender object, id, and normalized main frame", async () => {
    const { authority, handlers } = harness();
    const registered = new FakeWebContents(17);
    const substituted = new FakeWebContents(17);
    authority.bindRenderer(registered);
    const handler = handlers.get(DESKTOP_IPC_CHANNELS.connection)!;

    await expect(
      handler(eventFor(registered), connectionRequest)
    ).resolves.toEqual({ ok: true, value: readyConnection });

    for (const event of [
      eventFor(substituted),
      eventFor(registered, { senderFrame: { url: "https://evil.test/" } }),
      eventFor(registered, {
        senderFrame: { url: "kestrel://app.evil/index.html" }
      }),
      eventFor(registered, {
        senderFrame: { url: "kestrel://app/%2e%2e/secret" }
      }),
      eventFor(registered, { senderFrame: null })
    ]) {
      await expect(handler(event, connectionRequest)).resolves.toEqual({
        ok: false,
        error: { code: "desktop_sender_untrusted" }
      });
    }

    registered.destroyed = true;
    await expect(
      handler(eventFor(registered), connectionRequest)
    ).resolves.toEqual({
      ok: false,
      error: { code: "desktop_sender_untrusted" }
    });
  });

  it("does not let stale destruction unbind a replacement with the same id", async () => {
    const { authority, handlers } = harness();
    const oldRenderer = new FakeWebContents(23);
    const replacement = new FakeWebContents(23);
    authority.bindRenderer(oldRenderer);
    authority.bindRenderer(replacement);
    oldRenderer.destroy();

    await expect(
      handlers.get(DESKTOP_IPC_CHANNELS.connection)!(
        eventFor(replacement),
        connectionRequest
      )
    ).resolves.toEqual({ ok: true, value: readyConnection });

    replacement.destroy();
    await expect(
      handlers.get(DESKTOP_IPC_CHANNELS.connection)!(
        eventFor(replacement),
        connectionRequest
      )
    ).resolves.toEqual({
      ok: false,
      error: { code: "desktop_sender_untrusted" }
    });
  });

  it("rejects malformed and oversized requests before adapter execution", async () => {
    const readConnection = vi.fn(() => readyConnection);
    const { authority, handlers } = harness({ readConnection });
    const renderer = new FakeWebContents(29);
    authority.bindRenderer(renderer);
    const handler = handlers.get(DESKTOP_IPC_CHANNELS.connection)!;

    for (const request of [
      {},
      { ...connectionRequest, extra: true },
      { schema: "x".repeat(10_000) },
      null,
      ["not", "an", "object"]
    ]) {
      await expect(handler(eventFor(renderer), request)).resolves.toEqual({
        ok: false,
        error: { code: "invalid_desktop_request" }
      });
    }
    expect(readConnection).not.toHaveBeenCalled();
  });

  it("never forwards malformed native output or native exception text", async () => {
    const secrets = [
      "native-output-secret",
      "native-exception-secret"
    ];
    const cases: Partial<DesktopIpcAdapters>[] = [
      {
        getAppVersion: () =>
          ({ version: secrets[0], extra: true }) as never
      },
      {
        getAppVersion: () => {
          throw new Error(secrets[1]);
        }
      }
    ];
    for (const adapterCase of cases) {
      const { authority, handlers } = harness(adapterCase);
      const renderer = new FakeWebContents(31);
      authority.bindRenderer(renderer);
      const result = await handlers.get(
        DESKTOP_IPC_CHANNELS.appVersion
      )!(eventFor(renderer), {
        schema: "kestrel.desktop.app-version.request.v1"
      });

      expect(result).toEqual({
        ok: false,
        error: {
          code:
            adapterCase === cases[0]
              ? "invalid_desktop_response"
              : "desktop_operation_failed"
        }
      });
      expect(JSON.stringify(result)).not.toContain("secret");
    }
  });

  it("rejects accessor-backed native output even when its values look valid", async () => {
    const accessorConnection = Object.defineProperties(
      {},
      Object.fromEntries(
        Object.entries(readyConnection).map(([key, value]) => [
          key,
          {
            enumerable: true,
            get: () => value
          }
        ])
      )
    ) as DesktopConnection;
    const { authority, handlers } = harness({
      readConnection: () => accessorConnection
    });
    const renderer = new FakeWebContents(35);
    authority.bindRenderer(renderer);

    await expect(
      handlers.get(DESKTOP_IPC_CHANNELS.connection)!(
        eventFor(renderer),
        connectionRequest
      )
    ).resolves.toEqual({
      ok: false,
      error: { code: "invalid_desktop_response" }
    });
  });

  it("bootstraps a marker only when it exactly agrees with ready connection", () => {
    const mismatch = {
      schema: "kestrel.desktop.runtime.v1" as const,
      baseUrl: "http://127.0.0.1:43124/",
      generation: 3
    };
    const { authority, syncHandlers } = harness({
      runtimeMarker: () => mismatch
    });
    const renderer = new FakeWebContents(37);
    authority.bindRenderer(renderer);
    const event = eventFor(renderer);

    syncHandlers.get(DESKTOP_IPC_CHANNELS.runtimeBootstrap)!(event, {
      schema: "kestrel.desktop.runtime-bootstrap.request.v1"
    });
    expect(event.returnValue).toEqual({
      ok: true,
      value: { marker: null }
    });
  });

  it("pushes validated lifecycle data to exact live bindings and isolates subscribers", () => {
    let lifecycleListener: ((value: DesktopConnection) => void) | undefined;
    const { authority } = harness({
      subscribeLifecycle(listener) {
        lifecycleListener = listener;
        return () => {
          lifecycleListener = undefined;
        };
      }
    });
    const renderer = new FakeWebContents(41);
    authority.bindRenderer(renderer);

    lifecycleListener?.(readyConnection);
    expect(renderer.sent).toEqual([
      {
        channel: DESKTOP_IPC_CHANNELS.lifecycleEvent,
        payload: readyConnection
      }
    ]);

    lifecycleListener?.({
      ...readyConnection,
      apiToken: "must-not-cross"
    } as never);
    expect(renderer.sent).toHaveLength(1);
    renderer.destroy();
    lifecycleListener?.(readyConnection);
    expect(renderer.sent).toHaveLength(1);
  });
});

describe("Desktop native adapters", () => {
  it("resolves a selected symlink and returns the canonical existing directory", async () => {
    const result = await chooseCanonicalDirectory({
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: ["/selected/link"]
      }),
      realpath: async () => "/canonical/project",
      stat: async () => ({ isDirectory: () => true }),
      isAbsolute: (value) => value.startsWith("/"),
      basename: () => "project"
    });

    expect(result).toEqual({
      status: "selected",
      path: "/canonical/project",
      displayLabel: "project"
    });
  });

  it.each([
    {
      name: "relative result",
      selected: "relative/project",
      canonical: "/canonical/project",
      directory: true
    },
    {
      name: "NUL result",
      selected: "/selected/\0project",
      canonical: "/selected/\0project",
      directory: true
    },
    {
      name: "oversized result",
      selected: `/${"x".repeat(5_000)}`,
      canonical: `/${"x".repeat(5_000)}`,
      directory: true
    },
    {
      name: "non-directory result",
      selected: "/selected/file",
      canonical: "/selected/file",
      directory: false
    }
  ])("rejects an invalid picker $name", async ({ selected, canonical, directory }) => {
    await expect(
      chooseCanonicalDirectory({
        showOpenDialog: async () => ({
          canceled: false,
          filePaths: [selected]
        }),
        realpath: async () => canonical,
        stat: async () => ({ isDirectory: () => directory }),
        isAbsolute: (value) => value.startsWith("/"),
        basename: () => "project"
      })
    ).rejects.toMatchObject({ code: "desktop_operation_failed" });
  });

  it("opens only an exact allowlisted external HTTPS URL", async () => {
    const opened: string[] = [];
    await expect(
      openReviewedExternalUrl(
        {
          purpose: "documentation",
          url: "https://github.com/John-MiracleWorker/Kestrel"
        },
        async (url) => {
          opened.push(url);
        }
      )
    ).resolves.toEqual({ opened: true });
    expect(opened).toEqual([
      "https://github.com/John-MiracleWorker/Kestrel"
    ]);
  });

  it.each([
    "http://github.com/John-MiracleWorker/Kestrel",
    "https://user@github.com/John-MiracleWorker/Kestrel",
    "https://github.com:444/John-MiracleWorker/Kestrel",
    "https://github.com/John-MiracleWorker/Kestrel#secret",
    "https://github.com.evil.test/John-MiracleWorker/Kestrel",
    "https://%67ithub.com/John-MiracleWorker/Kestrel"
  ])("rejects an unreviewed external URL %s", async (url) => {
    const open = vi.fn(async () => undefined);
    await expect(
      openReviewedExternalUrl(
        { purpose: "documentation", url },
        open
      )
    ).rejects.toMatchObject({ code: "invalid_desktop_request" });
    expect(open).not.toHaveBeenCalled();
  });
});
