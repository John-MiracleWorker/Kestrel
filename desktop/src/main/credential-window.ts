import {
  DESKTOP_CREDENTIAL_ENTRY_URL,
  type DesktopCredentialIntent,
  type DesktopCredentialProviderId,
  type DesktopCredentialResult,
  type DesktopErrorCode
} from "../contracts.js";

export interface CredentialModalCallbacks {
  onSubmit(valueBytes: Uint8Array): Promise<void>;
  onCancel(): void;
  onClose(): void;
  onFailure(): void;
}

export interface CredentialModalHandle {
  close(): void;
  preventOwnerClose(): void;
}

interface CredentialWindowWebContents {}

interface CredentialWindow {
  readonly webContents: CredentialWindowWebContents;
  loadURL(url: string): Promise<unknown>;
  once(event: string, listener: () => void): unknown;
  isDestroyed(): boolean;
  show(): void;
  close(): void;
  destroy(): void;
}

export interface CredentialWindowResult {
  readonly window: CredentialWindow;
  readonly loaded: Promise<void>;
}

export function credentialWindowOptions(options: {
  parent: unknown;
  preloadPath: string;
}): Readonly<Record<string, unknown>> {
  return Object.freeze({
    parent: options.parent,
    modal: true,
    show: false,
    width: 590,
    height: 590,
    minWidth: 590,
    minHeight: 590,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    autoHideMenuBar: true,
    backgroundColor: "#fffdf3",
    title: "Kestrel credential",
    webPreferences: Object.freeze({
      preload: options.preloadPath,
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
    })
  });
}

export function createCredentialWindow(options: {
  parent: unknown;
  preloadPath: string;
  createWindow(
    options: Readonly<Record<string, unknown>>
  ): CredentialWindow;
  installCredentialSecurity(
    webContents: CredentialWindowWebContents
  ): void;
  bindCredentialIpc(
    webContents: CredentialWindowWebContents
  ): void | (() => void);
  onFailure?(): void;
}): CredentialWindowResult {
  const window = options.createWindow(
    credentialWindowOptions({
      parent: options.parent,
      preloadPath: options.preloadPath
    })
  );
  let disposeIpc: (() => void) | undefined;
  const disposePartialWindow = (): void => {
    try {
      disposeIpc?.();
    } catch {
      // Setup failure stays sanitized even if private IPC cleanup fails.
    }
    if (!window.isDestroyed()) {
      try {
        window.destroy();
      } catch {
        // Setup failure stays sanitized even if native destruction fails.
      }
    }
  };
  let loaded: Promise<void>;
  try {
    options.installCredentialSecurity(window.webContents);
    disposeIpc =
      options.bindCredentialIpc(window.webContents) ?? undefined;
    window.once("ready-to-show", () => {
      if (!window.isDestroyed()) {
        window.show();
      }
    });
    loaded = Promise.resolve(
      window.loadURL(DESKTOP_CREDENTIAL_ENTRY_URL)
    )
      .then(() => {
        if (window.isDestroyed()) {
          throw new Error("credential_window_destroyed");
        }
      })
      .catch(() => {
        try {
          options.onFailure?.();
        } catch {
          // Native teardown must continue after the fixed failure wins.
        }
        disposePartialWindow();
        throw new Error("credential_window_setup_failed");
      });
  } catch {
    disposePartialWindow();
    throw new Error("credential_window_setup_failed");
  }
  return Object.freeze({ window, loaded });
}

type StoredCredential = Extract<
  DesktopCredentialResult,
  { status: "stored" }
>;

interface CredentialDialogDependencies {
  currentGeneration(): number | null;
  openModal(
    intent: DesktopCredentialIntent,
    callbacks: CredentialModalCallbacks
  ): CredentialModalHandle;
  storeProviderCredential(request: {
    providerId: DesktopCredentialProviderId;
    expectedGeneration: number;
    valueBytes: Uint8Array;
    signal: AbortSignal;
  }): Promise<StoredCredential>;
  enterReconciliationRequired(): void;
  scheduleTimeout?(
    callback: () => void,
    milliseconds: number
  ): unknown;
  cancelTimeout?(handle: unknown): void;
  subscribeDeactivation?(
    listener: (generation: number) => void
  ): () => void;
}

export interface CredentialDialogController {
  open(
    intent: DesktopCredentialIntent
  ): Promise<DesktopCredentialResult>;
  abort(): void;
}

function fixedError(code: DesktopErrorCode): Readonly<{
  code: DesktopErrorCode;
}> {
  return Object.freeze({ code });
}

function exactAmbiguousError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    Object.getPrototypeOf(error) === Object.prototype &&
    Reflect.ownKeys(error).length === 1 &&
    Object.getOwnPropertyDescriptor(error, "code")?.value ===
      "desktop_operation_ambiguous"
  );
}

export function createCredentialDialogController(
  dependencies: CredentialDialogDependencies
): CredentialDialogController {
  const scheduleTimeout =
    dependencies.scheduleTimeout ??
    ((callback: () => void, milliseconds: number) =>
      setTimeout(callback, milliseconds));
  const cancelTimeout =
    dependencies.cancelTimeout ??
    ((handle: unknown) =>
      clearTimeout(handle as ReturnType<typeof setTimeout>));
  let active:
    | {
        state: "collecting" | "submitting" | "settled";
        generation: number;
        modal: CredentialModalHandle;
        resolve(value: DesktopCredentialResult): void;
        reject(error: Readonly<{ code: DesktopErrorCode }>): void;
        timeout: unknown;
        unsubscribe(): void;
        cleaned: boolean;
        recoveryEntered: boolean;
        abortController: AbortController;
        valueBytes?: Uint8Array;
      }
    | undefined;

  const cleanup = (
    operation: NonNullable<typeof active>
  ): void => {
    if (operation.cleaned) {
      return;
    }
    operation.cleaned = true;
    cancelTimeout(operation.timeout);
    operation.unsubscribe();
    if (!operation.modal) {
      return;
    }
    try {
      operation.modal.close();
    } catch {
      // Cleanup remains terminal even if the native window already died.
    }
  };

  const scrubTrackedBytes = (
    operation: NonNullable<typeof active>
  ): void => {
    operation.valueBytes?.fill(0);
    operation.valueBytes = undefined;
  };

  const enterAmbiguousRecovery = (
    operation: NonNullable<typeof active>
  ): void => {
    if (operation.recoveryEntered) {
      return;
    }
    operation.recoveryEntered = true;
    try {
      dependencies.enterReconciliationRequired();
    } catch {
      // The caller still receives the conservative fixed error.
    }
  };

  const settleResult = (
    operation: NonNullable<typeof active>,
    result: DesktopCredentialResult
  ): void => {
    if (
      active !== operation ||
      operation.state === "settled"
    ) {
      return;
    }
    operation.state = "settled";
    active = undefined;
    scrubTrackedBytes(operation);
    cleanup(operation);
    operation.resolve(Object.freeze(result));
  };

  const settleError = (
    operation: NonNullable<typeof active>,
    code: DesktopErrorCode
  ): void => {
    if (
      active !== operation ||
      operation.state === "settled"
    ) {
      return;
    }
    const wasSubmitting =
      operation.state === "submitting";
    operation.state = "settled";
    if (code === "desktop_operation_ambiguous") {
      enterAmbiguousRecovery(operation);
    }
    if (wasSubmitting) {
      operation.abortController.abort();
    }
    active = undefined;
    scrubTrackedBytes(operation);
    cleanup(operation);
    operation.reject(fixedError(code));
  };

  const controller: CredentialDialogController = {
    open(intent) {
      if (active !== undefined) {
        return Promise.reject(
          fixedError("desktop_operation_in_progress")
        );
      }
      const generation = dependencies.currentGeneration();
      if (
        generation === null ||
        !Number.isSafeInteger(generation) ||
        generation <= 0
      ) {
        return Promise.reject(
          fixedError("desktop_operation_failed")
        );
      }
      let resolve!: (value: DesktopCredentialResult) => void;
      let reject!: (
        error: Readonly<{ code: DesktopErrorCode }>
      ) => void;
      const result = new Promise<DesktopCredentialResult>(
        (nextResolve, nextReject) => {
          resolve = nextResolve;
          reject = nextReject;
        }
      );
      const operation = {
        state: "collecting" as
          | "collecting"
          | "submitting"
          | "settled",
        generation,
        modal: undefined as unknown as CredentialModalHandle,
        resolve,
        reject,
        timeout: undefined as unknown,
        unsubscribe: (() => undefined) as () => void,
        cleaned: false,
        recoveryEntered: false,
        abortController: new AbortController(),
        valueBytes: undefined as Uint8Array | undefined
      };
      const callbacks: CredentialModalCallbacks = {
        onSubmit: async (valueBytes) => {
          if (
            active !== operation ||
            operation.state !== "collecting"
          ) {
            valueBytes.fill(0);
            return;
          }
          operation.state = "submitting";
          operation.valueBytes = valueBytes;
          try {
            operation.modal.preventOwnerClose();
          } catch {
            settleError(
              operation,
              "desktop_operation_failed"
            );
            throw fixedError("desktop_operation_failed");
          }
          try {
            const stored =
              await dependencies.storeProviderCredential({
                providerId: intent.providerId,
                expectedGeneration: operation.generation,
                valueBytes,
                signal: operation.abortController.signal
              });
            if (
              active !== operation ||
              operation.state !== "submitting"
            ) {
              throw fixedError(
                "desktop_operation_ambiguous"
              );
            }
            settleResult(operation, stored);
          } catch (error) {
            const code = exactAmbiguousError(error)
              ? "desktop_operation_ambiguous"
              : "desktop_operation_failed";
            const stillActive =
              active === operation &&
              operation.state === "submitting";
            settleError(
              operation,
              code
            );
            if (!stillActive) {
              throw fixedError(
                "desktop_operation_ambiguous"
              );
            }
            throw fixedError(code);
          } finally {
            valueBytes.fill(0);
            if (operation.valueBytes === valueBytes) {
              operation.valueBytes = undefined;
            }
          }
        },
        onCancel: () => {
          if (
            active === operation &&
            operation.state === "collecting"
          ) {
            settleResult(operation, {
              status: "cancelled"
            });
          }
        },
        onClose: () => {
          if (
            active === operation &&
            operation.state === "collecting"
          ) {
            settleResult(operation, {
              status: "cancelled"
            });
          }
        },
        onFailure: () => {
          if (
            active === operation &&
            operation.state !== "settled"
          ) {
            settleError(
              operation,
              operation.state === "submitting"
                ? "desktop_operation_ambiguous"
                : "desktop_operation_failed"
            );
          }
        }
      };
      try {
        operation.modal = dependencies.openModal(
          intent,
          callbacks
        );
      } catch {
        reject(fixedError("desktop_operation_failed"));
        return result;
      }
      active = operation;
      operation.timeout = scheduleTimeout(() => {
        settleError(
          operation,
          operation.state === "submitting"
            ? "desktop_operation_ambiguous"
            : "desktop_operation_failed"
        );
      }, 60_000);
      operation.unsubscribe =
        dependencies.subscribeDeactivation?.(
          (deactivatedGeneration) => {
            if (
              deactivatedGeneration !== operation.generation
            ) {
              return;
            }
            settleError(
              operation,
              operation.state === "submitting"
                ? "desktop_operation_ambiguous"
                : "desktop_operation_failed"
            );
          }
        ) ?? (() => undefined);
      return result;
    },
    abort(): void {
      const operation = active;
      if (operation === undefined) {
        return;
      }
      settleError(
        operation,
        operation.state === "submitting"
          ? "desktop_operation_ambiguous"
          : "desktop_operation_failed"
      );
    }
  };
  return Object.freeze(controller);
}
