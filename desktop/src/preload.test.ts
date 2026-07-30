import { describe, expect, it, vi } from "vitest";
import {
  DESKTOP_IPC_CHANNELS,
  type DesktopConnection,
  type DesktopRuntimeMarker,
  type DesktopUpdateStatus
} from "./contracts";
import {
  createDesktopPreload,
  type DesktopPreloadIpc
} from "./preload";

const readyConnection: DesktopConnection = {
  schema: "kestrel.desktop.connection.v1",
  state: "ready",
  generation: 7,
  baseUrl: "http://127.0.0.1:43123/",
  profileId: "default",
  sidecarVersion: "0.5.0",
  recovery: null
};

const runtimeMarker: DesktopRuntimeMarker = {
  schema: "kestrel.desktop.runtime.v1",
  baseUrl: "http://127.0.0.1:43123/",
  generation: 7
};

const unavailableUpdate: DesktopUpdateStatus = {
  schema: "kestrel.desktop.update.v1",
  state: "unavailable",
  reason: "not_configured"
};

function success(value: unknown): unknown {
  return { ok: true, value };
}

function harness(
  invokeResult: (channel: string, request: unknown) => unknown = (
    channel
  ) => {
    switch (channel) {
      case DESKTOP_IPC_CHANNELS.connection:
        return success(readyConnection);
      case DESKTOP_IPC_CHANNELS.chooseProjectFolder:
      case DESKTOP_IPC_CHANNELS.chooseStorageFolder:
        return success({ status: "cancelled" });
      case DESKTOP_IPC_CHANNELS.appVersion:
        return success({ version: "0.5.0" });
      case DESKTOP_IPC_CHANNELS.updateStatus:
        return success(unavailableUpdate);
      default:
        return {
          ok: false,
          error: { code: "desktop_feature_unavailable" }
        };
    }
  }
): {
  preload: ReturnType<typeof createDesktopPreload>;
  ipc: DesktopPreloadIpc;
  emit(channel: string, payload: unknown): void;
} {
  const listeners = new Map<
    string,
    Set<(event: unknown, payload: unknown) => void>
  >();
  const ipc: DesktopPreloadIpc = {
    invoke: vi.fn(async (channel, request) =>
      invokeResult(channel, request)
    ),
    sendSync: vi.fn(() =>
      success({ marker: runtimeMarker })
    ),
    on(channel, listener) {
      const current = listeners.get(channel) ?? new Set();
      current.add(listener);
      listeners.set(channel, current);
      return this;
    },
    removeListener(channel, listener) {
      listeners.get(channel)?.delete(listener);
      return this;
    }
  };
  return {
    preload: createDesktopPreload(ipc),
    ipc,
    emit(channel, payload) {
      for (const listener of listeners.get(channel) ?? []) {
        listener({ secretElectronEvent: true }, payload);
      }
    }
  };
}

describe("sandboxed Desktop preload", () => {
  it("exports only frozen reviewed methods and the frozen non-secret marker", () => {
    const { preload } = harness();

    expect(Reflect.ownKeys(preload.bridge).sort()).toEqual([
      "chooseProjectFolder",
      "chooseStorageFolder",
      "connection",
      "exportSupportBundle",
      "getAppVersion",
      "getUpdateStatus",
      "openCredentialDialog",
      "openExternalUrl",
      "performRecoveryAction",
      "subscribeLifecycle",
      "subscribeUpdateStatus"
    ]);
    expect(Object.isFrozen(preload.bridge)).toBe(true);
    expect(
      Object.values(Object.getOwnPropertyDescriptors(preload.bridge)).every(
        (descriptor) =>
          "value" in descriptor &&
          typeof descriptor.value === "function" &&
          descriptor.writable === false &&
          descriptor.configurable === false
      )
    ).toBe(true);
    expect(preload.runtimeMarker).toEqual(runtimeMarker);
    expect(Object.isFrozen(preload.runtimeMarker)).toBe(true);
    expect(JSON.stringify(preload)).not.toContain("token");
    expect(preload.bridge).not.toHaveProperty("invoke");
    expect(preload.bridge).not.toHaveProperty("on");
    expect(preload.bridge).not.toHaveProperty("ipcRenderer");
  });

  it("omits the marker when bootstrap has no verified active authority", () => {
    const ipc: DesktopPreloadIpc = {
      invoke: async () => success(readyConnection),
      sendSync: () => success({ marker: null }),
      on: () => ipc,
      removeListener: () => ipc
    };

    expect(createDesktopPreload(ipc).runtimeMarker).toBeNull();
  });

  it("validates strict requests before invoking main", async () => {
    const { preload, ipc } = harness();
    const accessorRequest = Object.defineProperties(
      {},
      {
        purpose: {
          enumerable: true,
          value: "documentation"
        },
        url: {
          enumerable: true,
          get: () =>
            "https://github.com/John-MiracleWorker/Kestrel"
        }
      }
    );
    const symbolRequest = {
      purpose: "documentation",
      url: "https://github.com/John-MiracleWorker/Kestrel",
      [Symbol("unreviewed")]: true
    };

    for (const request of [
      {
        purpose: "documentation",
        url: "https://github.com/John-MiracleWorker/Kestrel",
        extra: "not-reviewed"
      },
      accessorRequest,
      symbolRequest,
      new Proxy(
        {
          purpose: "documentation",
          url: "https://github.com/John-MiracleWorker/Kestrel"
        },
        {
          ownKeys() {
            throw new Error("proxy-secret");
          }
        }
      )
    ]) {
      await expect(
        preload.bridge.openExternalUrl(request as never)
      ).rejects.toMatchObject({ code: "invalid_desktop_request" });
    }
    await expect(
      preload.bridge.openCredentialDialog({
        providerId: "x".repeat(500),
        purpose: "provider_api_key"
      })
    ).rejects.toMatchObject({ code: "invalid_desktop_request" });
    expect(ipc.invoke).not.toHaveBeenCalled();
  });

  it("maps malformed, oversized, and rejected IPC results to fixed local errors", async () => {
    const secrets = [
      "native-error-secret",
      "malformed-response-secret",
      "oversized-response-secret"
    ];
    const results: unknown[] = [
      Promise.reject(new Error(secrets[0])),
      { ok: true, value: { version: secrets[1], extra: true } },
      { ok: true, value: { version: "x".repeat(40_000) } }
    ];
    for (const result of results) {
      const { preload } = harness(() => result);
      let caught: unknown;
      try {
        await preload.bridge.getAppVersion();
      } catch (error) {
        caught = error;
      }
      expect(caught).toEqual({ code: "invalid_desktop_response" });
      expect(Object.isFrozen(caught)).toBe(true);
      expect(JSON.stringify(caught)).not.toContain("secret");
    }
  });

  it("delivers only validated frozen lifecycle events and isolates listeners", () => {
    const { preload, emit } = harness();
    const first = vi.fn(() => {
      throw new Error("listener-secret");
    });
    const second = vi.fn();
    const unsubscribeFirst = preload.bridge.subscribeLifecycle(first);
    const unsubscribeSecond = preload.bridge.subscribeLifecycle(second);

    emit(DESKTOP_IPC_CHANNELS.lifecycleEvent, readyConnection);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
    const delivered = second.mock.calls[0]?.[0];
    expect(delivered).toEqual(readyConnection);
    expect(delivered).not.toHaveProperty("secretElectronEvent");
    expect(Object.isFrozen(delivered)).toBe(true);

    emit(DESKTOP_IPC_CHANNELS.lifecycleEvent, {
      ...readyConnection,
      apiToken: "must-not-cross"
    });
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);

    unsubscribeFirst();
    unsubscribeFirst();
    unsubscribeSecond();
    emit(DESKTOP_IPC_CHANNELS.lifecycleEvent, readyConnection);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("validates and isolates update subscriptions", () => {
    const { preload, emit } = harness();
    const listener = vi.fn();
    const unsubscribe = preload.bridge.subscribeUpdateStatus(listener);

    emit(
      DESKTOP_IPC_CHANNELS.updateStatusEvent,
      unavailableUpdate
    );
    expect(listener).toHaveBeenCalledWith(unavailableUpdate);
    expect(Object.isFrozen(listener.mock.calls[0]?.[0])).toBe(true);

    emit(DESKTOP_IPC_CHANNELS.updateStatusEvent, {
      ...unavailableUpdate,
      releaseAction: "install"
    });
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });
});
