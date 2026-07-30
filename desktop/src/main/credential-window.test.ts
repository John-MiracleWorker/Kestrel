import { describe, expect, it, vi } from "vitest";
import {
  DESKTOP_CREDENTIAL_ENTRY_URL,
  type DesktopCredentialIntent
} from "../contracts";
import {
  createCredentialDialogController,
  createCredentialWindow,
  credentialWindowOptions,
  type CredentialModalCallbacks
} from "./credential-window";

const intent: DesktopCredentialIntent = {
  providerId: "openai",
  purpose: "provider_api_key"
};

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

describe("isolated credential window", () => {
  it("uses an exact modal parent, verified preload path, and hardened preferences", () => {
    const parent = { id: "primary-window" };
    expect(
      credentialWindowOptions({
        parent,
        preloadPath:
          "/verified/desktop/dist/credential/preload.js"
      })
    ).toMatchObject({
      parent,
      modal: true,
      show: false,
      resizable: false,
      minimizable: false,
      maximizable: false,
      webPreferences: {
        preload:
          "/verified/desktop/dist/credential/preload.js",
        devTools: false,
        spellcheck: false,
        nodeIntegration: false,
        nodeIntegrationInWorker: false,
        nodeIntegrationInSubFrames: false,
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        webviewTag: false,
        allowRunningInsecureContent: false
      }
    });
  });

  it("loads only the credential host, installs its own boundary, and never binds primary authority", async () => {
    const order: string[] = [];
    const loaded: string[] = [];
    const show = vi.fn();
    const listeners = new Map<string, () => void>();
    const webContents = {};
    const window = {
      webContents,
      loadURL: vi.fn(async (url: string) => {
        order.push("load");
        loaded.push(url);
      }),
      once: vi.fn((event: string, listener: () => void) => {
        listeners.set(event, listener);
      }),
      isDestroyed: () => false,
      show,
      close: vi.fn(),
      destroy: vi.fn()
    };
    const installCredentialSecurity = vi.fn(() => {
      order.push("security");
    });
    const bindCredentialIpc = vi.fn(() => {
      order.push("bind");
    });

    const opened = createCredentialWindow({
      parent: { id: "primary-window" },
      preloadPath:
        "/verified/desktop/dist/credential/preload.js",
      createWindow: () => window,
      installCredentialSecurity,
      bindCredentialIpc
    });
    await opened.loaded;

    expect(order).toEqual(["security", "bind", "load"]);
    expect(loaded).toEqual([DESKTOP_CREDENTIAL_ENTRY_URL]);
    expect(installCredentialSecurity).toHaveBeenCalledWith(
      webContents
    );
    expect(bindCredentialIpc).toHaveBeenCalledWith(webContents);
    expect(show).not.toHaveBeenCalled();
    listeners.get("ready-to-show")?.();
    expect(show).toHaveBeenCalledOnce();
    expect(opened).not.toHaveProperty("bindApiSession");
    expect(opened).not.toHaveProperty("bindDesktopIpc");
  });

  it("disposes private IPC and destroys the exact partial modal when setup throws", () => {
    const dispose = vi.fn();
    const destroy = vi.fn();
    const window = {
      webContents: {},
      loadURL: vi.fn(() => {
        throw new Error("load_setup_failed");
      }),
      once: vi.fn(),
      isDestroyed: () => false,
      show: vi.fn(),
      close: vi.fn(),
      destroy
    };

    expect(() =>
      createCredentialWindow({
        parent: { id: "primary-window" },
        preloadPath:
          "/verified/desktop/dist/credential/preload.js",
        createWindow: () => window,
        installCredentialSecurity: vi.fn(),
        bindCredentialIpc: () => dispose
      })
    ).toThrow("credential_window_setup_failed");
    expect(dispose).toHaveBeenCalledOnce();
    expect(destroy).toHaveBeenCalledOnce();
  });
});

describe("one-shot credential dialog controller", () => {
  it("cancels normally and rejects a concurrent open without dispatch", async () => {
    let callbacks: CredentialModalCallbacks | undefined;
    const storeProviderCredential = vi.fn();
    const controller = createCredentialDialogController({
      currentGeneration: () => 7,
      openModal: (
        _intent: DesktopCredentialIntent,
        nextCallbacks: CredentialModalCallbacks
      ) => {
        callbacks = nextCallbacks;
        return { close: vi.fn(), preventOwnerClose: vi.fn() };
      },
      storeProviderCredential,
      enterReconciliationRequired: vi.fn()
    });

    const first = controller.open(intent);
    await expect(controller.open(intent)).rejects.toEqual({
      code: "desktop_operation_in_progress"
    });
    callbacks?.onCancel();

    await expect(first).resolves.toEqual({
      status: "cancelled"
    });
    expect(storeProviderCredential).not.toHaveBeenCalled();
  });

  it("transitions synchronously and lets only the first submit write", async () => {
    let callbacks: CredentialModalCallbacks | undefined;
    const stored = deferred<{
      status: "stored";
      secretRef: string;
      validation: "unverified";
      fingerprint: string;
    }>();
    const storeProviderCredential = vi.fn(() => stored.promise);
    const preventOwnerClose = vi.fn();
    const close = vi.fn();
    const controller = createCredentialDialogController({
      currentGeneration: () => 7,
      openModal: (
        _intent: DesktopCredentialIntent,
        nextCallbacks: CredentialModalCallbacks
      ) => {
        callbacks = nextCallbacks;
        return { close, preventOwnerClose };
      },
      storeProviderCredential,
      enterReconciliationRequired: vi.fn()
    });
    const result = controller.open(intent);
    const firstBytes = new TextEncoder().encode("first-private");
    const duplicateBytes =
      new TextEncoder().encode("duplicate-private");

    const firstSubmit = callbacks!.onSubmit(firstBytes);
    const duplicateSubmit = callbacks!.onSubmit(duplicateBytes);
    callbacks!.onCancel();
    callbacks!.onClose();

    expect(preventOwnerClose).toHaveBeenCalledOnce();
    expect(storeProviderCredential).toHaveBeenCalledTimes(1);
    expect(storeProviderCredential).toHaveBeenCalledWith({
      providerId: "openai",
      expectedGeneration: 7,
      valueBytes: firstBytes
    });
    stored.resolve({
      status: "stored",
      secretRef: "secret://openai_api_key",
      validation: "unverified",
      fingerprint: "sha256:0123456789ab"
    });

    await firstSubmit;
    await duplicateSubmit;
    await expect(result).resolves.toEqual({
      status: "stored",
      secretRef: "secret://openai_api_key",
      validation: "unverified",
      fingerprint: "sha256:0123456789ab"
    });
    expect(close).toHaveBeenCalledOnce();
  });

  it("surfaces an ambiguous post-dispatch result once and enters conservative recovery", async () => {
    let callbacks: CredentialModalCallbacks | undefined;
    const enterReconciliationRequired = vi.fn();
    const controller = createCredentialDialogController({
      currentGeneration: () => 7,
      openModal: (
        _intent: DesktopCredentialIntent,
        nextCallbacks: CredentialModalCallbacks
      ) => {
        callbacks = nextCallbacks;
        return { close: vi.fn(), preventOwnerClose: vi.fn() };
      },
      storeProviderCredential: async () => {
        throw { code: "desktop_operation_ambiguous" };
      },
      enterReconciliationRequired
    });

    const result = controller.open(intent);
    await callbacks!.onSubmit(
      new TextEncoder().encode("ambiguous-private")
    );

    await expect(result).rejects.toEqual({
      code: "desktop_operation_ambiguous"
    });
    expect(enterReconciliationRequired).toHaveBeenCalledOnce();
  });

  it("treats close before submit as cancellation and teardown as a fixed abort", async () => {
    for (const terminal of ["close", "teardown"] as const) {
      let callbacks: CredentialModalCallbacks | undefined;
      const close = vi.fn();
      const storeProviderCredential = vi.fn();
      const controller = createCredentialDialogController({
        currentGeneration: () => 7,
        openModal: (
          _intent: DesktopCredentialIntent,
          nextCallbacks: CredentialModalCallbacks
        ) => {
          callbacks = nextCallbacks;
          return {
            close,
            preventOwnerClose: vi.fn()
          };
        },
        storeProviderCredential,
        enterReconciliationRequired: vi.fn()
      });
      const result = controller.open(intent);

      if (terminal === "close") {
        callbacks!.onClose();
        await expect(result).resolves.toEqual({
          status: "cancelled"
        });
      } else {
        controller.abort();
        await expect(result).rejects.toEqual({
          code: "desktop_operation_failed"
        });
        expect(close).toHaveBeenCalledOnce();
      }
      expect(storeProviderCredential).not.toHaveBeenCalled();
    }
  });

  it("ignores owner close while submitting and preserves the generation captured at open", async () => {
    let generation = 7;
    let callbacks: CredentialModalCallbacks | undefined;
    let settled = false;
    const stored = deferred<{
      status: "stored";
      secretRef: string;
      validation: "unverified";
      fingerprint: string;
    }>();
    const storeProviderCredential = vi.fn(() => stored.promise);
    const controller = createCredentialDialogController({
      currentGeneration: () => generation,
      openModal: (
        _intent: DesktopCredentialIntent,
        nextCallbacks: CredentialModalCallbacks
      ) => {
        callbacks = nextCallbacks;
        return {
          close: vi.fn(),
          preventOwnerClose: vi.fn()
        };
      },
      storeProviderCredential,
      enterReconciliationRequired: vi.fn()
    });
    const result = controller.open(intent);
    result.finally(() => {
      settled = true;
    });
    generation = 8;

    void callbacks!.onSubmit(
      new TextEncoder().encode("captured-generation")
    );
    callbacks!.onClose();
    await Promise.resolve();

    expect(settled).toBe(false);
    expect(storeProviderCredential).toHaveBeenCalledWith({
      providerId: "openai",
      expectedGeneration: 7,
      valueBytes: expect.any(Uint8Array)
    });
    stored.resolve({
      status: "stored",
      secretRef: "secret://openai_api_key",
      validation: "unverified",
      fingerprint: "sha256:0123456789ab"
    });
    await expect(result).resolves.toMatchObject({
      status: "stored"
    });
  });

  it("uses an exact 60-second deadline and makes in-flight timeout ambiguous", async () => {
    let callbacks: CredentialModalCallbacks | undefined;
    let timeout:
      | {
          callback(): void;
          milliseconds: number;
        }
      | undefined;
    const cancelTimeout = vi.fn();
    const close = vi.fn();
    const enterReconciliationRequired = vi.fn();
    const pending = deferred<never>();
    const controller = createCredentialDialogController({
      currentGeneration: () => 7,
      openModal: (
        _intent: DesktopCredentialIntent,
        nextCallbacks: CredentialModalCallbacks
      ) => {
        callbacks = nextCallbacks;
        return {
          close,
          preventOwnerClose: vi.fn()
        };
      },
      storeProviderCredential: () => pending.promise,
      enterReconciliationRequired,
      scheduleTimeout: (
        callback: () => void,
        milliseconds: number
      ) => {
        timeout = { callback, milliseconds };
        return 91;
      },
      cancelTimeout
    });
    const result = controller.open(intent);

    void callbacks!.onSubmit(
      new TextEncoder().encode("timeout-private")
    );
    expect(timeout?.milliseconds).toBe(60_000);
    timeout?.callback();

    await expect(result).rejects.toEqual({
      code: "desktop_operation_ambiguous"
    });
    expect(enterReconciliationRequired).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
    expect(cancelTimeout).toHaveBeenCalledWith(91);
  });

  it("treats authority deactivation during dispatch as ambiguous and unsubscribes once", async () => {
    let callbacks: CredentialModalCallbacks | undefined;
    let deactivated:
      | ((generation: number) => void)
      | undefined;
    const unsubscribe = vi.fn();
    const enterReconciliationRequired = vi.fn();
    const pending = deferred<never>();
    const close = vi.fn();
    const controller = createCredentialDialogController({
      currentGeneration: () => 7,
      openModal: (
        _intent: DesktopCredentialIntent,
        nextCallbacks: CredentialModalCallbacks
      ) => {
        callbacks = nextCallbacks;
        return {
          close,
          preventOwnerClose: vi.fn()
        };
      },
      storeProviderCredential: () => pending.promise,
      enterReconciliationRequired,
      subscribeDeactivation: (
        listener: (generation: number) => void
      ) => {
        deactivated = listener;
        return unsubscribe;
      }
    });
    const result = controller.open(intent);
    void callbacks!.onSubmit(
      new TextEncoder().encode("deactivated-private")
    );

    deactivated?.(7);
    deactivated?.(7);
    callbacks!.onCancel();
    callbacks!.onClose();

    await expect(result).rejects.toEqual({
      code: "desktop_operation_ambiguous"
    });
    expect(enterReconciliationRequired).toHaveBeenCalledOnce();
    expect(unsubscribe).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });

  it("lets the first collecting terminal event win with idempotent cleanup", async () => {
    let callbacks: CredentialModalCallbacks | undefined;
    let timeoutCallback: (() => void) | undefined;
    let deactivationCallback:
      | ((generation: number) => void)
      | undefined;
    const cancelTimeout = vi.fn();
    const unsubscribe = vi.fn();
    const close = vi.fn();
    const storeProviderCredential = vi.fn();
    const controller = createCredentialDialogController({
      currentGeneration: () => 7,
      openModal: (
        _intent: DesktopCredentialIntent,
        nextCallbacks: CredentialModalCallbacks
      ) => {
        callbacks = nextCallbacks;
        return {
          close,
          preventOwnerClose: vi.fn()
        };
      },
      storeProviderCredential,
      enterReconciliationRequired: vi.fn(),
      scheduleTimeout: (callback: () => void) => {
        timeoutCallback = callback;
        return 37;
      },
      cancelTimeout,
      subscribeDeactivation: (
        listener: (generation: number) => void
      ) => {
        deactivationCallback = listener;
        return unsubscribe;
      }
    });
    const result = controller.open(intent);

    callbacks!.onCancel();
    callbacks!.onCancel();
    callbacks!.onClose();
    timeoutCallback?.();
    deactivationCallback?.(7);
    controller.abort();

    await expect(result).resolves.toEqual({
      status: "cancelled"
    });
    expect(storeProviderCredential).not.toHaveBeenCalled();
    expect(close.mock.calls.length).toBeLessThanOrEqual(1);
    expect(unsubscribe).toHaveBeenCalledOnce();
    expect(cancelTimeout.mock.calls.length).toBeLessThanOrEqual(
      1
    );
  });

  it("ignores a stale load failure after a later operation has opened", async () => {
    const callbacks: CredentialModalCallbacks[] = [];
    const controller = createCredentialDialogController({
      currentGeneration: () => 7,
      openModal: (
        _intent: DesktopCredentialIntent,
        nextCallbacks: CredentialModalCallbacks
      ) => {
        callbacks.push(nextCallbacks);
        return {
          close: vi.fn(),
          preventOwnerClose: vi.fn()
        };
      },
      storeProviderCredential: vi.fn(),
      enterReconciliationRequired: vi.fn()
    });

    const first = controller.open(intent);
    callbacks[0]?.onCancel();
    await expect(first).resolves.toEqual({ status: "cancelled" });

    const second = controller.open(intent);
    callbacks[0]?.onFailure();
    let secondSettled = false;
    void second.finally(() => {
      secondSettled = true;
    });
    await Promise.resolve();
    expect(secondSettled).toBe(false);

    callbacks[1]?.onCancel();
    await expect(second).resolves.toEqual({ status: "cancelled" });
  });

  it("settles load failure before synchronous native destruction can report close", async () => {
    const load = deferred<unknown>();
    let loaded: Promise<void> | undefined;
    let destroyed = false;
    const controller = createCredentialDialogController({
      currentGeneration: () => 7,
      openModal: (
        _intent: DesktopCredentialIntent,
        callbacks: CredentialModalCallbacks
      ) => {
        const opened = createCredentialWindow({
          parent: { id: "primary-window" },
          preloadPath:
            "/verified/desktop/dist/credential/preload.js",
          createWindow: () => ({
            webContents: {},
            loadURL: () => load.promise,
            once: vi.fn(),
            isDestroyed: () => destroyed,
            show: vi.fn(),
            close: vi.fn(),
            destroy: () => {
              destroyed = true;
              callbacks.onClose();
            }
          }),
          installCredentialSecurity: vi.fn(),
          bindCredentialIpc: vi.fn(),
          onFailure: callbacks.onFailure
        });
        loaded = opened.loaded;
        return {
          close: vi.fn(),
          preventOwnerClose: vi.fn()
        };
      },
      storeProviderCredential: vi.fn(),
      enterReconciliationRequired: vi.fn()
    });

    const result = controller.open(intent);
    load.reject(new Error("native_load_failed"));

    await expect(loaded).rejects.toThrow(
      "credential_window_setup_failed"
    );
    await expect(result).rejects.toEqual({
      code: "desktop_operation_failed"
    });
  });
});
