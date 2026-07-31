import { basename, resolve, sep } from "node:path";
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
  mainFrame: {
    url: string;
    processId: number;
    routingId: number;
  };

  constructor(readonly id: number) {
    this.mainFrame = {
      url: "kestrel://app/index.html",
      processId: 1_000 + id,
      routingId: 2_000 + id
    };
  }

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

function frameFor(
  webContents: FakeWebContents,
  overrides: Partial<FakeWebContents["mainFrame"]> = {}
): FakeWebContents["mainFrame"] {
  return {
    ...webContents.mainFrame,
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
      eventFor(registered, {
        senderFrame: frameFor(registered, {
          url: "https://evil.test/"
        })
      }),
      eventFor(registered, {
        senderFrame: frameFor(registered, {
          url: "kestrel://app.evil/index.html"
        })
      }),
      eventFor(registered, {
        senderFrame: frameFor(registered, {
          url: "kestrel://app/%2e%2e/secret"
        })
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

  it("accepts a distinct Electron frame wrapper with the live main-frame identity", async () => {
    const { authority, handlers } = harness();
    const registered = new FakeWebContents(19);
    authority.bindRenderer(registered);
    const eventFrame = frameFor(registered);

    expect(eventFrame).not.toBe(registered.mainFrame);
    await expect(
      handlers.get(DESKTOP_IPC_CHANNELS.connection)!(
        eventFor(registered, { senderFrame: eventFrame }),
        connectionRequest
      )
    ).resolves.toEqual({ ok: true, value: readyConnection });
  });

  it("rejects mismatched, subframe, and stale Electron frame identities", async () => {
    const { authority, handlers } = harness();
    const registered = new FakeWebContents(21);
    authority.bindRenderer(registered);
    const handler = handlers.get(DESKTOP_IPC_CHANNELS.connection)!;

    for (const senderFrame of [
      frameFor(registered, {
        processId: registered.mainFrame.processId + 1
      }),
      frameFor(registered, {
        routingId: registered.mainFrame.routingId + 1
      })
    ]) {
      await expect(
        handler(
          eventFor(registered, { senderFrame }),
          connectionRequest
        )
      ).resolves.toEqual({
        ok: false,
        error: { code: "desktop_sender_untrusted" }
      });
    }

    const staleNavigationFrame = frameFor(registered);
    registered.mainFrame = frameFor(registered, {
      url: "kestrel://app/index.html#mission"
    });
    await expect(
      handler(
        eventFor(registered, {
          senderFrame: staleNavigationFrame
        }),
        connectionRequest
      )
    ).resolves.toEqual({
      ok: false,
      error: { code: "desktop_sender_untrusted" }
    });

    const staleIdentityFrame = frameFor(registered);
    registered.mainFrame = frameFor(registered, {
      routingId: registered.mainFrame.routingId + 2
    });
    await expect(
      handler(
        eventFor(registered, {
          senderFrame: staleIdentityFrame
        }),
        connectionRequest
      )
    ).resolves.toEqual({
      ok: false,
      error: { code: "desktop_sender_untrusted" }
    });
  });

  it("keeps IPC authority across every stable destination and reviewed legacy route", async () => {
    const { authority, handlers } = harness();
    const registered = new FakeWebContents(22);
    authority.bindRenderer(registered);
    const handler = handlers.get(DESKTOP_IPC_CHANNELS.connection)!;

    const urls = [
      "kestrel://app/",
      ...[
        "mission/command",
        "projects/overview",
        "memory/layers",
        "flock/overview",
        "automate/routines",
        "extend/catalog",
        "settings/general"
      ].flatMap((route) => [
        `kestrel://app/#/${route}`,
        `kestrel://app/index.html#/${route}`
      ]),
      "kestrel://app/index.html#/flock/qualification?run_id=run+1&task_id=proof%2F2",
      ...["", "index.html"].flatMap((path) =>
        [
          "mission",
          "chat",
          "outcomes",
          "routines",
          "routing",
          "advanced",
          "settings",
          "workspace",
          "tools"
        ].map(
          (route) => `kestrel://app/${path}#${route}`
        )
      )
    ];
    for (const url of urls) {
      registered.mainFrame = frameFor(registered, {
        url
      });
      await expect(
        handler(eventFor(registered), connectionRequest),
        url
      ).resolves.toEqual({ ok: true, value: readyConnection });
    }
  });

  it.each([
    "kestrel://app/mission#mission",
    "kestrel://app/index.html?route=mission",
    "kestrel://app/index.html?route=mission#mission",
    "kestrel://user@app/index.html#mission",
    "kestrel://app:444/index.html#mission",
    "kestrel://app.evil/index.html#mission",
    "kestrel://app/index.html#",
    "kestrel://app/index.html#Mission",
    "kestrel://app/index.html#%6dission",
    "kestrel://app/index.html#mission/extra",
    "kestrel://app/index.html#unknown",
    `kestrel://app/index.html#${"x".repeat(300)}`,
    "kestrel://app\\index.html#mission",
    "kestrel://app/index.html#mission\0hidden",
    "kestrel://app/index.html#mission\nhidden",
    "kestrel://app/%2e%2e/index.html#mission",
    "kestrel://app/index.html#mission%00hidden",
    "kestrel://app/index.html#mission%2fhidden",
    "kestrel://app/index.html#mission%5chidden"
  ])("rejects an unreviewed app frame URL %s", async (url) => {
    const { authority, handlers } = harness();
    const registered = new FakeWebContents(24);
    registered.mainFrame = frameFor(registered, { url });
    authority.bindRenderer(registered);

    await expect(
      handlers.get(DESKTOP_IPC_CHANNELS.connection)!(
        eventFor(registered),
        connectionRequest
      )
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

  it("returns a fixed bounded recovery retry rejection", async () => {
    const { authority, handlers } = harness({
      performRecoveryAction: async () => ({
        accepted: false,
        reason: "retry_rate_limited"
      })
    });
    const renderer = new FakeWebContents(33);
    authority.bindRenderer(renderer);

    await expect(
      handlers.get(DESKTOP_IPC_CHANNELS.recoveryAction)!(
        eventFor(renderer),
        {
          schema: "kestrel.desktop.recovery-action.request.v1",
          request: { action: "retry_readiness" }
        }
      )
    ).resolves.toEqual({
      ok: true,
      value: {
        accepted: false,
        reason: "retry_rate_limited"
      }
    });
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

  it.each([
    DESKTOP_IPC_CHANNELS.chooseProjectFolder,
    DESKTOP_IPC_CHANNELS.chooseStorageFolder
  ])("accepts canonical absolute and cancelled folder results on %s", async (channel) => {
    const canonicalPath = resolve("canonical", "project");
    const selected = {
      status: "selected" as const,
      path: canonicalPath,
      displayLabel: basename(canonicalPath)
    };
    const selectedHarness = harness({
      chooseProjectFolder: async () => selected,
      chooseStorageFolder: async () => selected
    });
    const selectedRenderer = new FakeWebContents(43);
    selectedHarness.authority.bindRenderer(selectedRenderer);

    await expect(
      selectedHarness.handlers.get(channel)!(
        eventFor(selectedRenderer),
        {
          schema:
            channel === DESKTOP_IPC_CHANNELS.chooseProjectFolder
              ? "kestrel.desktop.choose-project-folder.request.v1"
              : "kestrel.desktop.choose-storage-folder.request.v1"
        }
      )
    ).resolves.toEqual({ ok: true, value: selected });

    const cancelledHarness = harness();
    const cancelledRenderer = new FakeWebContents(45);
    cancelledHarness.authority.bindRenderer(cancelledRenderer);
    await expect(
      cancelledHarness.handlers.get(channel)!(
        eventFor(cancelledRenderer),
        {
          schema:
            channel === DESKTOP_IPC_CHANNELS.chooseProjectFolder
              ? "kestrel.desktop.choose-project-folder.request.v1"
              : "kestrel.desktop.choose-storage-folder.request.v1"
        }
      )
    ).resolves.toEqual({
      ok: true,
      value: { status: "cancelled" }
    });
  });

  it.each([
    {
      name: "relative path",
      path: ["relative", "project"].join(sep)
    },
    {
      name: "dot-segment path",
      path: `${resolve("canonical", "parent")}${sep}..${sep}project`
    },
    {
      name: "NUL path",
      path: `${resolve("canonical")}${sep}project\0hidden`
    },
    {
      name: "oversized path",
      path: `${resolve("canonical")}${sep}${"x".repeat(5_000)}`
    }
  ])("rejects an injected adapter folder result with a $name", async ({ path }) => {
    const malformed = {
      status: "selected" as const,
      path,
      displayLabel: "project"
    };
    const { authority, handlers } = harness({
      chooseProjectFolder: async () => malformed,
      chooseStorageFolder: async () => malformed
    });
    const renderer = new FakeWebContents(47);
    authority.bindRenderer(renderer);

    for (const [channel, schema] of [
      [
        DESKTOP_IPC_CHANNELS.chooseProjectFolder,
        "kestrel.desktop.choose-project-folder.request.v1"
      ],
      [
        DESKTOP_IPC_CHANNELS.chooseStorageFolder,
        "kestrel.desktop.choose-storage-folder.request.v1"
      ]
    ] as const) {
      await expect(
        handlers.get(channel)!(eventFor(renderer), { schema })
      ).resolves.toEqual({
        ok: false,
        error: { code: "invalid_desktop_response" }
      });
    }
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
